#!/usr/bin/env python3
"""Build a single JSON snapshot of the latest cycle from rfb-pharma.db.

The Next.js site reads /api/data.json, which this script regenerates after
every pipeline run (load + monthly cycle).

Schema:
  {
    "cycle": "2026-08",
    "generated_at": "...",
    "counts": { total, farmacia, cosmeticos, by_municipio: {...}, by_situacao: {...} },
    "geocoded": { count, total, coverage_pct },
    "delta": { novas: [...], baixas: [...], mudaram_endereco: [...], mudaram_situacao: [...] },
    "establishments": [ {...per-row payload...} ]
  }
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(os.environ.get("PROJECT_ROOT", Path(__file__).resolve().parents[1]))
DB_PATH = PROJECT / "work" / "rfb-pharma.db"
GEOCODE_CACHE = PROJECT / "work" / "geocode" / "cache.json"
SITE_PUBLIC = PROJECT / "site" / "public" / "data.json"

SITUACAO_LABELS = {
    "01": "NULA",
    "1": "NULA",
    "02": "ATIVA",
    "2": "ATIVA",
    "03": "SUSPENSA",
    "3": "SUSPENSA",
    "04": "INAPTA",
    "4": "INAPTA",
    "08": "BAIXADA",
    "8": "BAIXADA",
}


def label_situacao(code: str) -> str:
    if not code:
        return ""
    return SITUACAO_LABELS.get(code, code)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_cnpj(raw: str) -> str:
    s = "".join(c for c in (raw or "") if c.isdigit())
    if len(s) != 14:
        return raw or ""
    return f"{s[0:2]}.{s[2:5]}.{s[5:8]}/{s[8:12]}-{s[12:14]}"


def format_phone(ddd: str | None, tel: str | None) -> str:
    d = (ddd or "").strip()
    t = (tel or "").strip()
    if not t:
        return ""
    if d:
        return f"({d}) {t}"
    return t


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", default="latest")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: db not found: {DB_PATH}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    cur = conn.cursor()

    # Resolve cycle.
    if args.cycle == "latest":
        cur.execute("SELECT ciclo FROM ciclo WHERE status='ok' ORDER BY ciclo DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            print("ERROR: no ok cycle in ciclo table", file=sys.stderr)
            return 3
        cycle = row["ciclo"]
    else:
        cycle = args.cycle

    print(f"cycle={cycle}", file=sys.stderr)

    # Geocode cache.
    cache: dict[str, list[float]] = {}
    if GEOCODE_CACHE.exists():
        try:
            cache = json.loads(GEOCODE_CACHE.read_text())
        except Exception as e:
            print(f"WARN: bad geocode cache ({e}); starting empty", file=sys.stderr)
            cache = {}

    # Establishments.
    cur.execute(
        """
        SELECT e.cnpj_full, e.matriz_filial, e.cnpj_matriz,
               COALESCE(em.razao_social, '') AS razao_social,
               e.nome_fantasia,
               e.layer,
               e.cnae_principal,
               e.cnae_secundaria,
               e.situacao, e.situacao_data, e.motivo_situacao,
               e.abertura_data,
               e.municipio_codigo, e.municipio_nome, e.uf,
               e.logradouro_tipo, e.logradouro, e.numero, e.complemento, e.bairro, e.cep,
               e.ddd1, e.telefone1, e.ddd2, e.telefone2, e.email,
               COALESCE(em.capital_social, NULL) AS capital_social,
               COALESCE(em.porte, '') AS porte,
               COALESCE(s.optante_simples, '') AS optante_simples,
               COALESCE(s.optante_mei, '') AS optante_mei
        FROM estab_snap e
        LEFT JOIN empresa_snap em ON em.ciclo = e.ciclo AND em.cnpj_bas = e.cnpj_bas
        LEFT JOIN simples_snap s ON s.ciclo = e.ciclo AND s.cnpj_bas = e.cnpj_bas
        WHERE e.ciclo = ?
        ORDER BY e.municipio_nome, e.layer, e.cnpj_full
        """,
        (cycle,),
    )

    rows_out = []
    by_mun: dict[str, int] = {}
    by_layer: dict[str, int] = {"FARMACIA": 0, "COSMETICOS": 0}
    by_sit: dict[str, int] = {}
    geocoded = 0
    total = 0

    for r in cur:
        total += 1
        lat = lon = None
        # Build a normalized cache key: tipo + logradouro + numero + bairro + municipio + uf + cep
        key = "|".join(
            filter(
                None,
                [
                    (r["logradouro_tipo"] or "").strip(),
                    (r["logradouro"] or "").strip(),
                    (r["numero"] or "").strip(),
                    (r["bairro"] or "").strip(),
                    (r["municipio_nome"] or "").strip(),
                    (r["uf"] or "").strip(),
                    (r["cep"] or "").strip(),
                ],
            )
        ).lower()
        if key and key in cache:
            cached = cache[key]
            if cached is not None and len(cached) == 2:
                lat, lon = cached
                if lat is not None and lon is not None:
                    geocoded += 1

        logradouro_full = " ".join(filter(None, [r["logradouro_tipo"], r["logradouro"]]))
        cnpj_fmt = format_cnpj(r["cnpj_full"])

        rows_out.append(
            {
                "cnpj": cnpj_fmt,
                "cnpj_raw": r["cnpj_full"],
                "matriz_filial": "Matriz" if (r["matriz_filial"] == "1") else "Filial",
                "matriz_filial_code": r["matriz_filial"],
                "razao_social": r["razao_social"],
                "nome_fantasia": r["nome_fantasia"],
                "layer": r["layer"],
                "layer_label": "Farmácia" if r["layer"] == "FARMACIA" else "Cosméticos/Perfumaria",
                "cnae_principal": r["cnae_principal"],
                "cnae_secundaria": r["cnae_secundaria"],
                "situacao": label_situacao(r["situacao"]),
                "situacao_code": r["situacao"],
                "situacao_data": r["situacao_data"],
                "motivo_situacao": r["motivo_situacao"],
                "abertura_data": r["abertura_data"],
                "municipio_codigo": r["municipio_codigo"],
                "municipio_nome": (r["municipio_nome"] or "").title(),
                "uf": r["uf"],
                "logradouro_tipo": r["logradouro_tipo"],
                "logradouro": r["logradouro"],
                "logradouro_full": logradouro_full,
                "numero": r["numero"],
                "complemento": r["complemento"],
                "bairro": r["bairro"],
                "cep": r["cep"],
                "telefone1": format_phone(r["ddd1"], r["telefone1"]),
                "telefone2": format_phone(r["ddd2"], r["telefone2"]),
                "email": r["email"],
                "capital_social": r["capital_social"],
                "porte": r["porte"],
                "simples": "Sim" if (r["optante_simples"] == "S") else ("Não" if r["optante_simples"] else ""),
                "mei": "Sim" if (r["optante_mei"] == "S") else ("Não" if r["optante_mei"] else ""),
                "lat": lat,
                "lon": lon,
                "_key": key,
            }
        )

        mun = (r["municipio_nome"] or "").title()
        by_mun[mun] = by_mun.get(mun, 0) + 1
        by_layer[r["layer"]] = by_layer.get(r["layer"], 0) + 1
        sit_label = label_situacao(r["situacao"])
        by_sit[sit_label] = by_sit.get(sit_label, 0) + 1

    # Delta — last cycle's deltas.
    cur.execute(
        """
        SELECT d.ciclo, d.cnpj_full, d.kind, d.detail_json, d.detected_at,
               COALESCE(e.municipio_nome, '') AS municipio_nome,
               COALESCE(em.razao_social, '') AS razao_social,
               COALESCE(e.situacao, '') AS situacao,
               COALESCE(e.cnae_principal, '') AS cnae_principal,
               COALESCE(e.nome_fantasia, '') AS nome_fantasia
        FROM delta_log d
        LEFT JOIN estab_snap e ON e.ciclo = d.ciclo AND e.cnpj_full = d.cnpj_full
        LEFT JOIN empresa_snap em ON em.ciclo = e.ciclo AND em.cnpj_bas = substr(d.cnpj_full, 1, 8)
        WHERE d.ciclo = ?
        ORDER BY d.kind, e.municipio_nome, d.cnpj_full
        """,
        (cycle,),
    )
    delta_rows: list[dict] = []
    for d in cur:
        delta_rows.append(
            {
                "cnpj": format_cnpj(d["cnpj_full"]),
                "cnpj_raw": d["cnpj_full"],
                "kind": d["kind"],
                "detail_json": d["detail_json"],
                "detected_at": d["detected_at"],
                "municipio_nome": (d["municipio_nome"] or "").title(),
                "razao_social": d["razao_social"],
                "nome_fantasia": d["nome_fantasia"],
                "situacao": label_situacao(d["situacao"]),
                "cnae_principal": d["cnae_principal"],
            }
        )

    delta_summary = {
        "novas": sum(1 for d in delta_rows if d["kind"] == "NOVA"),
        "baixas": sum(1 for d in delta_rows if d["kind"] == "BAIXA"),
        "mudaram_endereco": sum(1 for d in delta_rows if d["kind"] == "MUDOU_ENDERECO"),
        "mudaram_situacao": sum(1 for d in delta_rows if d["kind"] == "MUDOU_SITUACAO"),
        "total": len(delta_rows),
    }

    payload = {
        "cycle": cycle,
        "generated_at": now_iso(),
        "counts": {
            "total": total,
            "farmacia": by_layer.get("FARMACIA", 0),
            "cosmeticos": by_layer.get("COSMETICOS", 0),
            "by_municipio": by_mun,
            "by_situacao": by_sit,
        },
        "geocoded": {
            "count": geocoded,
            "total": total,
            "coverage_pct": round((geocoded / total * 100), 2) if total else 0.0,
        },
        "delta": {**delta_summary, "rows": delta_rows},
        "establishments": rows_out,
    }

    SITE_PUBLIC.parent.mkdir(parents=True, exist_ok=True)
    SITE_PUBLIC.write_text(json.dumps(payload, ensure_ascii=False))
    size_mb = SITE_PUBLIC.stat().st_size / (1024 * 1024)
    print(f"wrote {SITE_PUBLIC} ({size_mb:.1f} MB) — {total} establishments, {geocoded} geocoded, {len(delta_rows)} delta rows", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())