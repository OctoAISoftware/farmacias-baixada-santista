#!/usr/bin/env python3
"""
Streaming ETL — Baixada Santista pharmacies + cosmetics from the RFB CNPJ
open-data dump. Kauê's request.

Design principle (the fix for the load-577 incident): NEVER materialize the
uncompressed national base. Each Estabelecimentos/Empresas/Simples zip is
streamed and decompressed IN MEMORY line-by-line (zipfile.open), filtered on
the fly. Only the few hundred matched rows are held. One sequential pass per
file keeps I/O gentle on the shared box.

RFB layout (headerless, ';'-delimited, latin1):
  Estabelecimentos (30 cols): 0 cnpj_basico 1 ordem 2 dv 3 matriz_filial
    4 nome_fantasia 5 situacao 6 data_situacao 7 motivo 8 cidade_ext 9 pais
    10 data_inicio 11 cnae_principal 12 cnae_secundaria(csv) 13 tipo_logr
    14 logradouro 15 numero 16 complemento 17 bairro 18 cep 19 uf 20 municipio
    21 ddd1 22 tel1 23 ddd2 24 tel2 25 ddd_fax 26 fax 27 email 28 sit_especial
    29 data_sit_especial
  Empresas (7): 0 cnpj_basico 1 razao 2 natureza 3 qual_resp 4 capital 5 porte
    6 ente
  Simples (7): 0 cnpj_basico 1 opc_simples 2 dt_opc_s 3 dt_exc_s 4 opc_mei
    5 dt_opc_mei 6 dt_exc_mei
  Municipios (2): 0 codigo 1 nome
"""
import csv, io, json, sys, unicodedata, zipfile, glob, os, datetime

DL = os.environ.get("DL_ROOT") or str(Path(__file__).resolve().parents[1] / "downloads")
OUT_DIR = os.environ.get("OUT_DIR") or str(Path(__file__).resolve().parents[1] / "output")
CYCLE = os.environ.get("CYCLE", "2026-08")

TARGET_CITIES = [
    "SAO VICENTE", "SANTOS", "GUARUJA", "CUBATAO", "PRAIA GRANDE",
    "MONGAGUA", "ITANHAEM", "PERUIBE", "BERTIOGA",
]
SITUACAO = {"01": "Nula", "02": "Ativa", "03": "Suspensa", "04": "Inapta", "08": "Baixada"}
PORTE = {"00": "Não informado", "01": "Micro empresa", "03": "Pequeno porte", "05": "Demais"}

def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().upper().strip()
    return " ".join(s.split())

def rows_from_zip(path):
    """Yield CSV rows streaming from the single CSV inside a zip, no disk extract."""
    with zipfile.ZipFile(path) as z:
        name = z.namelist()[0]
        with z.open(name) as raw:
            text = io.TextIOWrapper(raw, encoding="latin-1", newline="")
            for row in csv.reader(text, delimiter=";", quotechar='"'):
                yield row

def log(m):
    print(f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {m}", flush=True)

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    # 1) municipality name -> set of RFB codes (dup codes exist)
    codes = set()
    code_to_name = {}
    for row in rows_from_zip(f"{DL}/Municipios.zip"):
        if len(row) < 2:
            continue
        code, name = row[0].strip(), norm(row[1])
        code_to_name[code] = name
        if name in TARGET_CITIES:
            codes.add(code)
    log(f"target municipality codes ({len(codes)}): {sorted(codes)}")
    if not codes:
        log("FATAL: no municipality codes resolved"); sys.exit(1)

    def cnae_cat(principal, secundaria):
        """Return set of matched categories: 'farm' (4771) / 'cosm' (4772)."""
        cats = set()
        allc = [principal or ""] + [c for c in (secundaria or "").split(",") if c]
        for c in allc:
            c = c.strip()
            if c.startswith("4771"):
                cats.add("farm")
            elif c.startswith("4772"):
                cats.add("cosm")
        return cats

    # 2) stream establishments, filter by municipality + CNAE
    matched = {}   # cnpj_basico -> list of establishment dicts
    est_files = sorted(glob.glob(f"{DL}/Estabelecimentos*.zip"))
    for f in est_files:
        n = 0; kept = 0
        for row in rows_from_zip(f):
            n += 1
            if len(row) < 30:
                continue
            if row[20].strip() not in codes:
                continue
            cats = cnae_cat(row[11], row[12])
            if not cats:
                continue
            kept += 1
            cb = row[0].strip()
            rec = {
                "cnpj": f"{cb}{row[1].strip()}{row[2].strip()}",
                "cnpj_basico": cb,
                "matriz_filial": "Matriz" if row[3].strip() == "1" else "Filial",
                "nome_fantasia": row[4].strip(),
                "situacao": SITUACAO.get(row[5].strip(), row[5].strip()),
                "data_situacao": row[6].strip(),
                "data_inicio": row[10].strip(),
                "cnae_principal": row[11].strip(),
                "cnae_secundaria": row[12].strip(),
                "endereco": " ".join(x for x in [row[13].strip(), row[14].strip(), row[15].strip(),
                                                 row[16].strip()] if x),
                "bairro": row[17].strip(),
                "cep": row[18].strip(),
                "municipio": code_to_name.get(row[20].strip(), row[20].strip()),
                "uf": row[19].strip(),
                "telefone": " ".join(x for x in [f"({row[21].strip()}){row[22].strip()}" if row[22].strip() else "",
                                                 f"({row[23].strip()}){row[24].strip()}" if row[24].strip() else ""] if x),
                "email": row[27].strip().lower(),
                "cats": cats,
            }
            matched.setdefault(cb, []).append(rec)
        log(f"{os.path.basename(f)}: scanned {n:,} rows, kept {kept}")
    total = sum(len(v) for v in matched.values())
    log(f"matched establishments: {total} across {len(matched)} empresas")

    # 3) enrich from Empresas
    emp = {}
    for f in sorted(glob.glob(f"{DL}/Empresas*.zip")):
        for row in rows_from_zip(f):
            if len(row) < 6:
                continue
            cb = row[0].strip()
            if cb in matched:
                emp[cb] = {
                    "razao_social": row[1].strip(),
                    "capital_social": row[4].strip().replace(",", "."),
                    "porte": PORTE.get(row[5].strip(), row[5].strip()),
                }
    log(f"enriched {len(emp)} empresas from Empresas")

    # 4) Simples / MEI flags
    simples = {}
    try:
        for row in rows_from_zip(f"{DL}/Simples.zip"):
            if len(row) < 5:
                continue
            cb = row[0].strip()
            if cb in matched:
                simples[cb] = {"simples": row[1].strip(), "mei": row[4].strip()}
        log(f"Simples flags for {len(simples)} empresas")
    except Exception as e:
        log(f"Simples skipped: {e}")

    # 5) build rows
    def build(cat):
        out = []
        for cb, ests in matched.items():
            for e in ests:
                if cat not in e["cats"]:
                    continue
                company = emp.get(cb, {})
                s = simples.get(cb, {})
                out.append({
                    "CNPJ": e["cnpj"],
                    "Razão Social": company.get("razao_social", ""),
                    "Nome Fantasia": e["nome_fantasia"],
                    "Matriz/Filial": e["matriz_filial"],
                    "Situação Cadastral": e["situacao"],
                    "Data Situação": e["data_situacao"],
                    "Data Abertura": e["data_inicio"],
                    "CNAE Principal": e["cnae_principal"],
                    "CNAEs Secundárias": e["cnae_secundaria"],
                    "Endereço": e["endereco"],
                    "Bairro": e["bairro"],
                    "CEP": e["cep"],
                    "Município": e["municipio"],
                    "UF": e["uf"],
                    "Telefone": e["telefone"],
                    "E-mail": e["email"],
                    "Porte": company.get("porte", ""),
                    "Capital Social": company.get("capital_social", ""),
                    "Simples": s.get("simples", ""),
                    "MEI": s.get("mei", ""),
                })
        out.sort(key=lambda r: (r["Município"], r["Razão Social"]))
        return out

    farm = build("farm")
    cosm = build("cosm")
    log(f"Farmácias (4771): {len(farm)} | Cosméticos (4772): {len(cosm)}")

    # 6) xlsx
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    def sheet(ws, rows, title):
        ws.title = title
        if not rows:
            ws.append(["(sem registros)"]); return
        headers = list(rows[0].keys())
        ws.append(headers)
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="1B2A4E")
        for r in rows:
            ws.append([r[h] for h in headers])
        for i, h in enumerate(headers, 1):
            w = max(len(h), max((len(str(r[h])) for r in rows), default=0)) + 2
            ws.column_dimensions[chr(64+i) if i <= 26 else "A" + chr(64+i-26)].width = min(w, 55)
        ws.freeze_panes = "A2"

    sheet(wb.active, farm, "Farmácias — Base")
    sheet(wb.create_sheet(), cosm, "Cosméticos e Perfumaria")
    # Delta: baseline run -> everything is 'novo'
    dws = wb.create_sheet("Delta do ciclo")
    dws.append(["Ciclo", "Categoria", "CNPJ", "Razão Social", "Município", "Status"])
    for c in dws[1]:
        c.font = Font(bold=True)
    for r in farm:
        dws.append([CYCLE, "Farmácia", r["CNPJ"], r["Razão Social"], r["Município"], "novo"])
    for r in cosm:
        dws.append([CYCLE, "Cosméticos", r["CNPJ"], r["Razão Social"], r["Município"], "novo"])

    out_path = f"{OUT_DIR}/farmacias_baixada_santista_{CYCLE}.xlsx"
    wb.save(out_path)
    log(f"WROTE {out_path}")
    json.dump({
        "cycle": CYCLE, "farmacias": len(farm), "cosmeticos": len(cosm),
        "municipios": sorted(codes), "generated_at": datetime.datetime.now().isoformat(),
        "xlsx": out_path,
    }, open(f"{OUT_DIR}/summary.json", "w"), ensure_ascii=False, indent=2)
    log("DONE")

if __name__ == "__main__":
    main()
