#!/usr/bin/env python3
"""
run-pipeline.py — durable, resumable Receita Federal CNPJ downloader + parser.

Design goals:
  * Survive 30-minute session kills: the download phase runs in a child nohup
    process that writes progress to status.json and continues after the agent
    disconnects.
  * Resume interrupted files using HTTP Range requests (curl -C - fallback or
    urllib.request range).
  * Single sequential stream: the RF WebDAV mirror throttles concurrent
    connections to ~0.5 MB/s each; a single stream reaches ~6 MB/s.
  * Parse once all required files are complete.
  * Produce SQLite + Excel output per cycle.

Usage:
  # start/resume download (detached)
  nohup python3 scripts/run-pipeline.py download 2026-08 > work/pipeline.log 2>&1 &

  # run parse when downloads are complete
  python3 scripts/run-pipeline.py parse 2026-08

  # run full flow (download then parse) — NOT recommended in 30-min sessions
  python3 scripts/run-pipeline.py full 2026-08

Status file:
  work/status.json — updated atomically by every download attempt.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(os.environ.get("PROJECT_ROOT", Path(__file__).resolve().parents[1]))
DL_ROOT = PROJECT_ROOT / "downloads"
WORK_ROOT = PROJECT_ROOT / "work"
OUTPUT_ROOT = PROJECT_ROOT / "output"
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
DB_PATH = WORK_ROOT / "rfb-pharma.db"
STATUS_PATH = WORK_ROOT / "status.json"
LOG_PATH = WORK_ROOT / "pipeline.log"

# Two tokens have been observed across prior sessions; we try both.
TOKENS = ["gn672Ad4CF8N6TK", "YggdBLfdninEJX9"]
BASE_URL_TEMPLATE = (
    "https://arquivos.receitafederal.gov.br/public.php/webdav/Dados/Cadastros/CNPJ/{cycle}"
)

REQUIRED_FILES = [
    "Cnaes.zip",
    "Municipios.zip",
    *[f"Estabelecimentos{i}.zip" for i in range(10)],
    *[f"Empresas{i}.zip" for i in range(10)],
    "Simples.zip",
]

# CNAE filter — resolved from the Cnaes metadata file.
# Primary layer: 4771-7 (comércio varejista de produtos farmacêuticos) →
#                 4771701, 4771702, 4771703, 4771704.
# Secondary layer: 4772-5 (comércio varejista de cosméticos/perfumaria) →
#                  4772500.
# The script reads Cnaes.zip at runtime and fills CNAE_CODES; this tuple is
# the safe fallback built from the 2026-08 dump metadata.
CNAE_CODES_FALLBACK = {
    "FARMACIA": {"4771701", "4771702", "4771703", "4771704"},
    "COSMETICOS": {"4772500"},
}

# RFB open-data MUNICIPIO column carries the 4-digit TOM/TCE code (not IBGE 7-digit).
# Resolved from Municipios.zip at runtime; fallback below uses exact codes
# from the 2026-08 metadata file.
MUN_BAIXADA_SANTISTA_FALLBACK = {
    "2965": "BERTIOGA",
    "6371": "CUBATAO",
    "6475": "GUARUJA",
    "6543": "ITANHAEM",
    "6723": "MONGAGUA",
    "6853": "PERUIBE",
    "6921": "PRAIA GRANDE",
    "7071": "SANTOS",
    "7121": "SAO VICENTE",
}

CNAE_CODES: dict[str, set[str]] = {}
MUN_BAIXADA_SANTISTA: dict[str, str] = {}

UF_SP = "SP"

# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_status() -> dict:
    if STATUS_PATH.exists():
        try:
            with STATUS_PATH.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "phase": "idle",
        "cycle": None,
        "files_done": [],
        "files_total": REQUIRED_FILES,
        "bytes_done": 0,
        "bytes_total": 0,
        "started_at": None,
        "last_update": None,
        "current_file": None,
        "current_file_bytes": 0,
        "current_file_total": 0,
        "errors": [],
    }


def save_status(s: dict) -> None:
    s["last_update"] = now_iso()
    tmp = STATUS_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    tmp.replace(STATUS_PATH)


# ---------------------------------------------------------------------------
# WebDAV helpers
# ---------------------------------------------------------------------------


def pick_token(cycle: str) -> str:
    """Return the first token that yields a non-empty PROPFIND listing."""
    for token in TOKENS:
        url = BASE_URL_TEMPLATE.format(cycle=cycle)
        req = urllib.request.Request(
            url,
            method="PROPFIND",
            headers={
                "Authorization": f"Basic {base64.b64encode(f'{token}:'.encode()).decode()}",
                "Depth": "1",
                "User-Agent": "okto-rf-pipeline/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read()
                if body and b"Estabelecimentos" in body:
                    return token
        except Exception:
            continue
    raise RuntimeError("No working WebDAV token found")


def list_directory(token: str, cycle: str) -> dict[str, int]:
    url = BASE_URL_TEMPLATE.format(cycle=cycle)
    req = urllib.request.Request(
        url,
        method="PROPFIND",
        headers={
            "Authorization": f"Basic {base64.b64encode(f'{token}:'.encode()).decode()}",
            "Depth": "1",
            "User-Agent": "okto-rf-pipeline/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
    root = ET.fromstring(body)
    ns = {"d": "DAV:"}
    sizes: dict[str, int] = {}
    for response in root.findall("d:response", ns):
        href = response.find("d:href", ns)
        size = response.find(".//d:getcontentlength", ns)
        if href is None or size is None:
            continue
        name = href.text.split("/")[-1]
        try:
            sizes[name] = int(size.text)
        except ValueError:
            continue
    return sizes


# ---------------------------------------------------------------------------
# Download logic
# ---------------------------------------------------------------------------


def _curl_download(token: str, cycle: str, filename: str, dest: Path, expected: int, log, status: dict) -> bool:
    """Use curl -C - for resume. Single attempt per call.

    A watcher thread updates status.json every 15s while curl runs so the
    detached process can be monitored even though curl blocks for minutes.
    """
    url = f"{BASE_URL_TEMPLATE.format(cycle=cycle)}/{filename}"
    resume = "-C -" if dest.exists() and dest.stat().st_size > 0 else ""
    cmd = [
        "curl",
        "--connect-timeout", "30",
        "--max-time", "7200",
        "-fL",
        "-u", f"{token}:",
        "-A", "okto-rf-pipeline/1.0",
    ]
    if resume:
        cmd.append("-C")
        cmd.append("-")
    cmd += [url, "-o", str(dest)]
    log.write(f"[{now_iso()}] curl start {filename}\n")
    log.flush()

    watcher_stop = threading.Event()
    def watcher():
        while not watcher_stop.wait(15):
            try:
                sz = dest.stat().st_size if dest.exists() else 0
                status["current_file_bytes"] = sz
                status["bytes_done"] = sum(
                    (DL_ROOT / f).stat().st_size if (DL_ROOT / f).exists() else 0
                    for f in REQUIRED_FILES
                )
                save_status(status)
            except Exception:
                pass

    watcher_thread = threading.Thread(target=watcher, daemon=True)
    watcher_thread.start()
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=7260)
    finally:
        watcher_stop.set()
        watcher_thread.join(timeout=2)

    output = result.stdout.decode("utf-8", errors="replace")
    if result.returncode != 0:
        log.write(f"[{now_iso()}] curl failed {filename}: rc={result.returncode}\n{output[-2000:]}\n")
        log.flush()
        return False
    sz = dest.stat().st_size if dest.exists() else 0
    if expected and sz < expected:
        log.write(f"[{now_iso()}] curl incomplete {filename}: {sz}/{expected}\n")
        log.flush()
        return False
    log.write(f"[{now_iso()}] curl done {filename}: {sz} bytes\n")
    log.flush()
    return True


def _python_download(token: str, cycle: str, filename: str, dest: Path, expected: int, log, status: dict) -> bool:
    """Pure-Python fallback downloader with resume. Updates status.json live."""
    url = f"{BASE_URL_TEMPLATE.format(cycle=cycle)}/{filename}"
    auth = base64.b64encode(f"{token}:".encode()).decode()
    pos = dest.stat().st_size if dest.exists() else 0
    attempt = 0
    t_start = time.time()
    while pos < expected:
        attempt += 1
        headers = {
            "Authorization": f"Basic {auth}",
            "User-Agent": "okto-rf-pipeline/1.0",
        }
        if pos > 0:
            headers["Range"] = f"bytes={pos}-"
        req = urllib.request.Request(url, headers=headers)
        try:
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=300) as r:
                mode = "ab" if pos > 0 else "wb"
                with open(dest, mode) as f:
                    while True:
                        chunk = r.read(4 * 1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        pos += len(chunk)
                        if time.time() - t0 > 10:
                            mb = pos / (1024 * 1024)
                            tot = expected / (1024 * 1024)
                            log.write(
                                f"[{now_iso()}] {filename} {mb:.1f}/{tot:.1f} MB "
                                f"({100 * pos / expected:.1f}%)\n"
                            )
                            log.flush()
                            status["current_file_bytes"] = pos
                            status["bytes_done"] = sum(
                                (DL_ROOT / f).stat().st_size if (DL_ROOT / f).exists() else 0
                                for f in REQUIRED_FILES
                            )
                            save_status(status)
                            t0 = time.time()
            if pos >= expected:
                log.write(f"[{now_iso()}] python done {filename}: {pos} bytes\n")
                log.flush()
                return True
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            log.write(f"[{now_iso()}] python attempt {attempt} failed {filename} at {pos}: {e}\n")
            log.flush()
            if attempt >= 6:
                return False
            time.sleep(min(60, 8 * attempt))
            pos = dest.stat().st_size if dest.exists() else 0
    return True


def download_file(token: str, cycle: str, filename: str, sizes: dict[str, int], log, status: dict) -> bool:
    dest = DL_ROOT / filename
    expected = sizes.get(filename, 0)
    if expected == 0:
        log.write(f"[{now_iso()}] {filename}: unknown size, skipping\n")
        return False

    # Already complete?
    if dest.exists() and dest.stat().st_size >= expected:
        log.write(f"[{now_iso()}] {filename}: already complete ({dest.stat().st_size}/{expected})\n")
        return True

    # Try curl first, then python fallback.
    for method in ("curl", "python"):
        for attempt in range(1, 8):
            log.write(f"[{now_iso()}] {filename}: {method} attempt {attempt}/7\n")
            log.flush()
            ok = (
                _curl_download(token, cycle, filename, dest, expected, log, status)
                if method == "curl"
                else _python_download(token, cycle, filename, dest, expected, log, status)
            )
            if ok:
                return True
            time.sleep(min(60, 10 * attempt))
    return False


def download_cycle(cycle: str, log_path: Path) -> None:
    DL_ROOT.mkdir(parents=True, exist_ok=True)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)

    status = load_status()
    status["phase"] = "downloading"
    status["cycle"] = cycle
    status["started_at"] = now_iso()
    status["files_total"] = REQUIRED_FILES
    save_status(status)

    token = pick_token(cycle)
    log = open(log_path, "a", encoding="utf-8")
    log.write(f"[{now_iso()}] === download_cycle {cycle} token={token[:4]}... ===\n")
    log.flush()

    try:
        sizes = list_directory(token, cycle)
    except Exception as e:
        log.write(f"[{now_iso()}] PROPFIND failed: {e}\n")
        log.flush()
        status["errors"].append(f"PROPFIND failed: {e}")
        status["phase"] = "failed"
        save_status(status)
        raise

    status["bytes_total"] = sum(sizes.get(f, 0) for f in REQUIRED_FILES)
    save_status(status)

    for filename in REQUIRED_FILES:
        status["current_file"] = filename
        status["current_file_total"] = sizes.get(filename, 0)
        status["current_file_bytes"] = (DL_ROOT / filename).stat().st_size if (DL_ROOT / filename).exists() else 0
        save_status(status)

        ok = download_file(token, cycle, filename, sizes, log, status)
        if not ok:
            err = f"{filename}: download failed after all methods"
            log.write(f"[{now_iso()}] {err}\n")
            log.flush()
            status["errors"].append(err)
            status["current_file"] = None
            save_status(status)
            # Continue trying the rest; a later parse will detect missing files.
            continue

        status["files_done"] = sorted(set(status["files_done"] + [filename]))
        status["current_file"] = None
        status["bytes_done"] = sum(
            (DL_ROOT / f).stat().st_size if (DL_ROOT / f).exists() else 0
            for f in REQUIRED_FILES
        )
        save_status(status)

    status["current_file"] = None
    if all(f in status["files_done"] for f in REQUIRED_FILES):
        status["phase"] = "download_complete"
    else:
        status["phase"] = "download_partial"
    save_status(status)
    log.write(f"[{now_iso()}] download phase ended: {status['phase']}\n")
    log.flush()


# ---------------------------------------------------------------------------
# Parse / ETL logic
# ---------------------------------------------------------------------------


def decode(s: bytes | str) -> str:
    if isinstance(s, bytes):
        return s.decode("iso-8859-1")
    return s


def unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s


def split_rfb(line: str) -> list[str]:
    return line.rstrip("\r\n").split(";")


def parse_date(s: str) -> Optional[str]:
    s = unquote(s)
    if not s or s == "00000000" or len(s) != 8:
        return None
    try:
        return f"{s[4:8]}-{s[2:4]}-{s[0:2]}"
    except Exception:
        return None


def parse_decimal_br(s: str) -> Optional[float]:
    s = unquote(s)
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def open_csv_from_zip(zip_path: Path) -> Iterator[str]:
    """Yield decoded lines from the matching entry inside zip_path.

    RFB zip entries are named like K3241.K03200Y0.D60808.EMPRECSV or
    F.K03200$Z.D60808.CNAECSV, so we match by the zip basename as a prefix
    or by known CSV-like suffixes.
    """
    base = zip_path.stem  # e.g. Empresas0
    stem_upper = base.upper()
    with zipfile.ZipFile(zip_path) as z:
        member = None
        for n in z.namelist():
            n_upper = n.upper()
            # Files are CSV-like if they end with CSV or are one of the
            # historical fixed names (ESTABELE, EMPRECSV, etc.).
            is_data = (
                n_upper.endswith(".CSV")
                or n_upper.endswith(".ESTABELE")
                or n_upper.endswith(".EMPRECSV")
                or n_upper.endswith(".SIMPLES")
                or n_upper.endswith(".SOCIOCSV")
                or n_upper.endswith(".PAISCSV")
                or n_upper.endswith(".MUNICCSV")
                or n_upper.endswith(".CNAECSV")
                or n_upper.endswith(".MOTICSV")
                or n_upper.endswith(".NATJUCSV")
                or n_upper.endswith(".QUALSCSV")
            )
            if is_data and (
                n_upper.startswith(stem_upper)
                or stem_upper in n_upper.replace("$", "")
            ):
                member = n
                break
        if member is None:
            # Fallback: just take the first non-directory entry.
            for n in z.namelist():
                if not n.endswith("/"):
                    member = n
                    break
        if member is None:
            raise RuntimeError(f"no CSV inside {zip_path}")
        with z.open(member) as fh:
            for line in fh:
                yield decode(line)


def load_metadata() -> None:
    """Read Cnaes.zip and Municipios.zip to resolve exact codes and city names."""
    global CNAE_CODES, MUN_BAIXADA_SANTISTA

    if not CNAE_CODES:
        try:
            zp = DL_ROOT / "Cnaes.zip"
            if zp.exists():
                codes = {"FARMACIA": set(), "COSMETICOS": set()}
                for line in open_csv_from_zip(zp):
                    cols = [unquote(c) for c in split_rfb(line)]
                    if len(cols) < 2:
                        continue
                    code = cols[0]
                    desc = cols[1].upper()
                    # 4771-7 subclasses
                    if code.startswith("47717"):
                        codes["FARMACIA"].add(code)
                    # 4772-5 exact or subclass
                    elif code.startswith("47725"):
                        codes["COSMETICOS"].add(code)
                if not codes["FARMACIA"]:
                    codes["FARMACIA"] = set(CNAE_CODES_FALLBACK["FARMACIA"])
                if not codes["COSMETICOS"]:
                    codes["COSMETICOS"] = set(CNAE_CODES_FALLBACK["COSMETICOS"])
                CNAE_CODES = codes
            else:
                CNAE_CODES = {
                    k: set(v) for k, v in CNAE_CODES_FALLBACK.items()
                }
        except Exception as exc:
            print(f"metadata load warning (CNAE): {exc}", file=sys.stderr)
            CNAE_CODES = {k: set(v) for k, v in CNAE_CODES_FALLBACK.items()}

    if not MUN_BAIXADA_SANTISTA:
        try:
            target_names = {n.upper().replace(" ", "").replace("-", "")
                             for n in MUN_BAIXADA_SANTISTA_FALLBACK.values()}
            zp = DL_ROOT / "Municipios.zip"
            if zp.exists():
                mun = {}
                for line in open_csv_from_zip(zp):
                    cols = [unquote(c) for c in split_rfb(line)]
                    if len(cols) < 2:
                        continue
                    cod, nome = cols[0], cols[1]
                    key = nome.upper().replace(" ", "").replace("-", "")
                    if key in target_names:
                        mun[cod] = MUN_BAIXADA_SANTISTA_FALLBACK.get(cod, nome)
                MUN_BAIXADA_SANTISTA = mun if mun else dict(MUN_BAIXADA_SANTISTA_FALLBACK)
            else:
                MUN_BAIXADA_SANTISTA = dict(MUN_BAIXADA_SANTISTA_FALLBACK)
        except Exception as exc:
            print(f"metadata load warning (municipios): {exc}", file=sys.stderr)
            MUN_BAIXADA_SANTISTA = dict(MUN_BAIXADA_SANTISTA_FALLBACK)


def cnae_layer(cnae_pri: str, cnae_sec: str) -> Optional[str]:
    """Return 'FARMACIA' or 'COSMETICOS' if the row matches our target CNAE.

    cnae_sec is a comma-separated list (the RFB layout concatenates all secondary
    CNAEs into a single field). We scan the full list, not just the prefix.

    Priority: FARMACIA > COSMETICOS — an establishment that sells both is still
    classified as FARMACIA for the sindicato's purposes.
    """
    load_metadata()
    farm_codes = CNAE_CODES.get("FARMACIA", set())
    cos_codes = CNAE_CODES.get("COSMETICOS", set())

    sec_list = [c.strip() for c in unquote(cnae_sec or "").split(",") if c.strip()]
    pri = unquote(cnae_pri or "").strip()
    all_codes = [pri] + sec_list if pri else sec_list

    has_farm = False
    has_cos = False
    for c in all_codes:
        if not c:
            continue
        if c in farm_codes or c.startswith("47717"):
            has_farm = True
        if c in cos_codes or c.startswith("47725"):
            has_cos = True
    if has_farm:
        return "FARMACIA"
    if has_cos:
        return "COSMETICOS"
    return None


def init_db(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS ciclo (
          id          INTEGER PRIMARY KEY,
          ciclo       TEXT UNIQUE NOT NULL,
          source_url  TEXT,
          started_at  TEXT NOT NULL,
          finished_at TEXT,
          status      TEXT NOT NULL,
          counts_json TEXT
        );

        CREATE TABLE IF NOT EXISTS empresa_snap (
          ciclo        TEXT NOT NULL,
          cnpj_bas     TEXT NOT NULL,
          razao_social TEXT,
          capital_social REAL,
          porte        TEXT,
          PRIMARY KEY (ciclo, cnpj_bas)
        );
        CREATE INDEX IF NOT EXISTS idx_empresa_snap_bas ON empresa_snap(cnpj_bas);

        CREATE TABLE IF NOT EXISTS simples_snap (
          ciclo               TEXT NOT NULL,
          cnpj_bas            TEXT NOT NULL,
          optante_simples     TEXT,
          data_opcao_simples  TEXT,
          data_exclusao_simples TEXT,
          optante_mei         TEXT,
          data_opcao_mei      TEXT,
          data_exclusao_mei   TEXT,
          PRIMARY KEY (ciclo, cnpj_bas)
        );
        CREATE INDEX IF NOT EXISTS idx_simples_snap_bas ON simples_snap(cnpj_bas);

        CREATE TABLE IF NOT EXISTS estab_snap (
          ciclo            TEXT NOT NULL,
          cnpj_full        TEXT NOT NULL,
          cnpj_bas         TEXT NOT NULL,
          cnpj_ord         TEXT,
          cnpj_dv          TEXT,
          matriz_filial    TEXT,
          cnpj_matriz      TEXT,
          nome_fantasia    TEXT,
          situacao         TEXT,
          situacao_data    TEXT,
          motivo_situacao  TEXT,
          abertura_data    TEXT,
          cnae_principal   TEXT,
          cnae_secundaria  TEXT,
          municipio_codigo TEXT,
          municipio_nome   TEXT,
          uf               TEXT,
          logradouro_tipo  TEXT,
          logradouro       TEXT,
          numero           TEXT,
          complemento      TEXT,
          bairro           TEXT,
          cep              TEXT,
          ddd1             TEXT,
          telefone1        TEXT,
          ddd2             TEXT,
          telefone2        TEXT,
          email            TEXT,
          layer            TEXT NOT NULL,
          PRIMARY KEY (ciclo, cnpj_full)
        );
        CREATE INDEX IF NOT EXISTS idx_estab_snap_bas ON estab_snap(cnpj_bas);
        CREATE INDEX IF NOT EXISTS idx_estab_snap_ciclo_mun ON estab_snap(ciclo, municipio_codigo);

        CREATE TABLE IF NOT EXISTS delta_log (
          ciclo       TEXT NOT NULL,
          cnpj_full   TEXT NOT NULL,
          kind        TEXT NOT NULL,
          detail_json TEXT,
          detected_at TEXT NOT NULL,
          PRIMARY KEY (ciclo, cnpj_full, kind)
        );
    """
    )
    conn.commit()


def wipe_cycle(conn: sqlite3.Connection, ciclo: str) -> None:
    cur = conn.cursor()
    for table in ("estab_snap", "empresa_snap", "simples_snap"):
        cur.execute(f"DELETE FROM {table} WHERE ciclo = ?", (ciclo,))
    cur.execute("DELETE FROM ciclo WHERE ciclo = ?", (ciclo,))
    conn.commit()


def load_empresas(conn: sqlite3.Connection, ciclo: str) -> int:
    cur = conn.cursor()
    total = 0
    for n in range(10):
        zp = DL_ROOT / f"Empresas{n}.zip"
        if not zp.exists():
            print(f"  Empresas{n}: missing — cannot proceed", file=sys.stderr)
            raise FileNotFoundError(zp)
        batch = []
        rows = 0
        for line in open_csv_from_zip(zp):
            cols = [unquote(c) for c in split_rfb(line)]
            if len(cols) < 6:
                continue
            batch.append((ciclo, cols[0], cols[1], parse_decimal_br(cols[4]), cols[5]))
            rows += 1
            if len(batch) >= 100_000:
                cur.executemany(
                    "INSERT OR REPLACE INTO empresa_snap VALUES (?,?,?,?,?)", batch
                )
                conn.commit()
                batch.clear()
        if batch:
            cur.executemany("INSERT OR REPLACE INTO empresa_snap VALUES (?,?,?,?,?)", batch)
            conn.commit()
        total += rows
        print(f"  Empresas{n}: {rows} rows")
    return total


def load_simples(conn: sqlite3.Connection, ciclo: str) -> int:
    zp = DL_ROOT / "Simples.zip"
    if not zp.exists():
        raise FileNotFoundError(zp)
    cur = conn.cursor()
    rows = 0
    batch = []
    for line in open_csv_from_zip(zp):
        cols = [unquote(c) for c in split_rfb(line)]
        if len(cols) < 7:
            continue
        batch.append(
            (
                ciclo,
                cols[0],
                cols[1],
                parse_date(cols[2]),
                parse_date(cols[3]),
                cols[4],
                parse_date(cols[5]),
                parse_date(cols[6]),
            )
        )
        rows += 1
        if len(batch) >= 100_000:
            cur.executemany("INSERT OR REPLACE INTO simples_snap VALUES (?,?,?,?,?,?,?,?)", batch)
            conn.commit()
            batch.clear()
    if batch:
        cur.executemany("INSERT OR REPLACE INTO simples_snap VALUES (?,?,?,?,?,?,?,?)", batch)
        conn.commit()
    print(f"  Simples: {rows} rows")
    return rows


def load_estabelecimentos(conn: sqlite3.Connection, ciclo: str) -> dict:
    cur = conn.cursor()
    counts = {
        "scanned_rows": 0,
        "estabelecimentos_kept": 0,
        "farmacia": 0,
        "cosmeticos": 0,
        "filiais": 0,
        "matrizes": 0,
        "por_municipio": {},
        "por_situacao": {},
    }
    for n in range(10):
        zp = DL_ROOT / f"Estabelecimentos{n}.zip"
        if not zp.exists():
            raise FileNotFoundError(zp)
        scanned = 0
        kept = 0
        batch = []
        for line in open_csv_from_zip(zp):
            scanned += 1
            cols = [unquote(c) for c in split_rfb(line)]
            if len(cols) < 30:
                continue
            if cols[19] != UF_SP:
                continue
            mun_codigo = cols[20]
            if mun_codigo not in MUN_BAIXADA_SANTISTA:
                continue
            layer = cnae_layer(cols[11], cols[12])
            if layer is None:
                continue
            cnpj_bas = cols[0]
            cnpj_ord = cols[1]
            cnpj_dv = cols[2]
            cnpj_full = cnpj_bas + cnpj_ord + cnpj_dv
            matriz = cols[3]
            cnpj_matriz = (cnpj_bas + "0001") if matriz == "2" else cnpj_full
            batch.append(
                (
                    ciclo,
                    cnpj_full,
                    cnpj_bas,
                    cnpj_ord,
                    cnpj_dv,
                    matriz,
                    cnpj_matriz,
                    cols[4],
                    cols[5],
                    parse_date(cols[6]),
                    cols[7],
                    parse_date(cols[10]),
                    cols[11],
                    cols[12],
                    mun_codigo,
                    MUN_BAIXADA_SANTISTA[mun_codigo],
                    cols[19],
                    cols[13],
                    cols[14],
                    cols[15],
                    cols[16],
                    cols[17],
                    cols[18],
                    cols[21],
                    cols[22],
                    cols[23],
                    cols[24],
                    cols[27],
                    layer,
                )
            )
            kept += 1
            if len(batch) >= 50_000:
                cur.executemany(
                    "INSERT OR REPLACE INTO estab_snap VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    batch,
                )
                conn.commit()
                batch.clear()
        if batch:
            cur.executemany(
                "INSERT OR REPLACE INTO estab_snap VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                batch,
            )
            conn.commit()
        counts["scanned_rows"] += scanned
        counts["estabelecimentos_kept"] += kept
        print(f"  Estabelecimentos{n}: scanned {scanned}, kept {kept}")

    cur.execute(
        "SELECT layer, matriz_filial, municipio_nome, situacao, COUNT(*) "
        "FROM estab_snap WHERE ciclo = ? GROUP BY layer, matriz_filial, municipio_nome, situacao",
        (ciclo,),
    )
    for layer, matriz, mun, sit, n in cur.fetchall():
        if layer == "FARMACIA":
            counts["farmacia"] += n
        elif layer == "COSMETICOS":
            counts["cosmeticos"] += n
        if matriz == "1":
            counts["matrizes"] += n
        elif matriz == "2":
            counts["filiais"] += n
        counts["por_municipio"][mun] = counts["por_municipio"].get(mun, 0) + n
        counts["por_situacao"][sit] = counts["por_situacao"].get(sit, 0) + n
    return counts


def compute_diff(conn: sqlite3.Connection, ciclo: str) -> dict:
    cur = conn.cursor()
    cur.execute(
        "SELECT ciclo FROM ciclo WHERE ciclo < ? AND status = 'ok' ORDER BY ciclo DESC LIMIT 1",
        (ciclo,),
    )
    row = cur.fetchone()
    if not row:
        return {"novas": 0, "baixas": 0, "mudaram": 0, "previous": None}
    prev = row[0]
    summary = {"previous": prev, "novas": 0, "baixas": 0, "mudaram": 0}
    detected_at = now_iso()

    cur.execute(
        "INSERT INTO delta_log (ciclo, cnpj_full, kind, detail_json, detected_at) "
        "SELECT ?, e.cnpj_full, 'NOVA', json_object('municipio', e.municipio_nome, "
        "  'cnae_principal', e.cnae_principal, 'layer', e.layer), ? "
        "FROM estab_snap e WHERE e.ciclo = ? AND NOT EXISTS ("
        "  SELECT 1 FROM estab_snap p WHERE p.ciclo = ? AND p.cnpj_full = e.cnpj_full)",
        (ciclo, detected_at, ciclo, prev),
    )
    summary["novas"] = cur.rowcount

    cur.execute(
        "INSERT INTO delta_log (ciclo, cnpj_full, kind, detail_json, detected_at) "
        "SELECT ?, p.cnpj_full, 'BAIXA', json_object('situacao_anterior', p.situacao, "
        "  'municipio', p.municipio_nome), ? "
        "FROM estab_snap p WHERE p.ciclo = ? AND NOT EXISTS ("
        "  SELECT 1 FROM estab_snap e WHERE e.ciclo = ? AND e.cnpj_full = p.cnpj_full)",
        (ciclo, detected_at, prev, ciclo),
    )
    summary["baixas"] = cur.rowcount

    cur.execute(
        """
        INSERT INTO delta_log (ciclo, cnpj_full, kind, detail_json, detected_at)
        SELECT ?, e.cnpj_full,
          CASE
            WHEN e.situacao <> p.situacao THEN 'MUDOU_SITUACAO'
            WHEN COALESCE(e.logradouro,'') <> COALESCE(p.logradouro,'')
              OR COALESCE(e.numero,'') <> COALESCE(p.numero,'')
              OR COALESCE(e.bairro,'') <> COALESCE(p.bairro,'')
              OR COALESCE(e.cep,'') <> COALESCE(p.cep,'')
              OR COALESCE(e.municipio_codigo,'') <> COALESCE(p.municipio_codigo,'')
              THEN 'MUDOU_ENDERECO'
            WHEN COALESCE(e.cnae_principal,'') <> COALESCE(p.cnae_principal,'')
              OR COALESCE(e.cnae_secundaria,'') <> COALESCE(p.cnae_secundaria,'')
              OR COALESCE(e.layer,'') <> COALESCE(p.layer,'')
              THEN 'MUDOU_CNAE'
            ELSE 'MUDOU_EMPRESA'
          END,
          json_object('situacao_anterior', p.situacao, 'situacao_atual', e.situacao,
                      'endereco_anterior', p.logradouro || ', ' || p.numero || ' - ' || p.bairro,
                      'endereco_atual', e.logradouro || ', ' || e.numero || ' - ' || e.bairro),
          ?
        FROM estab_snap e
        JOIN estab_snap p ON p.ciclo = ? AND p.cnpj_full = e.cnpj_full
        WHERE e.ciclo = ?
          AND (e.situacao <> p.situacao
               OR COALESCE(e.logradouro,'') <> COALESCE(p.logradouro,'')
               OR COALESCE(e.numero,'') <> COALESCE(p.numero,'')
               OR COALESCE(e.bairro,'') <> COALESCE(p.bairro,'')
               OR COALESCE(e.cep,'') <> COALESCE(p.cep,'')
               OR COALESCE(e.municipio_codigo,'') <> COALESCE(p.municipio_codigo,'')
               OR COALESCE(e.cnae_principal,'') <> COALESCE(p.cnae_principal,'')
               OR COALESCE(e.cnae_secundaria,'') <> COALESCE(p.cnae_secundaria,'')
               OR COALESCE(e.layer,'') <> COALESCE(p.layer,''))
        """,
        (ciclo, detected_at, prev, ciclo),
    )
    summary["mudaram"] = cur.rowcount
    conn.commit()
    return summary


def write_xlsx(conn: sqlite3.Connection, ciclo: str, xlsx_path: Path) -> dict:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Base"
    base_headers = [
        "Camada",
        "CNPJ",
        "Matriz/Filial",
        "Razão Social",
        "Nome Fantasia",
        "Situação Cadastral",
        "Data Situação",
        "Abertura",
        "CNAE Principal",
        "CNAEs Secundárias",
        "Município",
        "UF",
        "Tipo Logradouro",
        "Logradouro",
        "Número",
        "Complemento",
        "Bairro",
        "CEP",
        "DDD1",
        "Telefone1",
        "DDD2",
        "Telefone2",
        "E-mail",
        "Capital Social",
        "Porte",
        "Optante Simples",
        "Optante MEI",
        "Data Opção Simples",
        "Data Exclusão Simples",
        "Data Opção MEI",
        "Data Exclusão MEI",
    ]
    ws.append(base_headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="00205B")
        cell.alignment = Alignment(horizontal="left", vertical="center")
    ws.freeze_panes = "A2"

    cur = conn.cursor()
    cur.execute(
        """
        SELECT e.layer, e.cnpj_full, e.matriz_filial,
          COALESCE(em.razao_social, ''), e.nome_fantasia,
          COALESCE(e.situacao, ''), COALESCE(e.situacao_data, ''),
          COALESCE(e.abertura_data, ''),
          COALESCE(e.cnae_principal, ''), COALESCE(e.cnae_secundaria, ''),
          COALESCE(e.municipio_nome, ''), COALESCE(e.uf, ''),
          COALESCE(e.logradouro_tipo, ''), COALESCE(e.logradouro, ''),
          COALESCE(e.numero, ''), COALESCE(e.complemento, ''),
          COALESCE(e.bairro, ''), COALESCE(e.cep, ''),
          COALESCE(e.ddd1, ''), COALESCE(e.telefone1, ''),
          COALESCE(e.ddd2, ''), COALESCE(e.telefone2, ''),
          COALESCE(e.email, ''),
          COALESCE(em.capital_social, ''), COALESCE(em.porte, ''),
          COALESCE(s.optante_simples, ''), COALESCE(s.optante_mei, ''),
          COALESCE(s.data_opcao_simples, ''),
          COALESCE(s.data_exclusao_simples, ''),
          COALESCE(s.data_opcao_mei, ''), COALESCE(s.data_exclusao_mei, '')
        FROM estab_snap e
        LEFT JOIN empresa_snap em ON em.ciclo = e.ciclo AND em.cnpj_bas = e.cnpj_bas
        LEFT JOIN simples_snap s ON s.ciclo = e.ciclo AND s.cnpj_bas = e.cnpj_bas
        WHERE e.ciclo = ?
        ORDER BY e.municipio_nome, e.layer, e.cnpj_full
        """,
        (ciclo,),
    )
    counts = {"total": 0, "farmacia": 0, "cosmeticos": 0, "por_municipio": {}}
    for row in cur:
        out = list(row)
        cnpj = out[1]
        if len(cnpj) == 14:
            out[1] = f"{cnpj[0:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:14]}"
        ws.append(out)
        counts["total"] += 1
        if row[0] == "FARMACIA":
            counts["farmacia"] += 1
        elif row[0] == "COSMETICOS":
            counts["cosmeticos"] += 1
        counts["por_municipio"][row[10]] = counts["por_municipio"].get(row[10], 0) + 1

    for col_idx in range(1, len(base_headers) + 1):
        max_len = len(str(base_headers[col_idx - 1]))
        col_letter = get_column_letter(col_idx)
        for row_idx in range(2, min(202, ws.max_row + 1)):
            v = ws.cell(row=row_idx, column=col_idx).value
            if v is not None:
                max_len = max(max_len, min(60, len(str(v))))
        ws.column_dimensions[col_letter].width = max_len + 2

    ws2 = wb.create_sheet("Mudanças do ciclo")
    delta_headers = ["Ciclo", "CNPJ", "Tipo de mudança", "Detalhe", "Município", "Razão Social", "Situação atual", "CNAE Principal"]
    ws2.append(delta_headers)
    for cell in ws2[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="8B0000")
        cell.alignment = Alignment(horizontal="left", vertical="center")
    ws2.freeze_panes = "A2"
    cur.execute(
        """
        SELECT d.ciclo, d.cnpj_full, d.kind, d.detail_json,
          COALESCE(e.municipio_nome, ''),
          COALESCE(em.razao_social, ''),
          COALESCE(e.situacao, ''),
          COALESCE(e.cnae_principal, '')
        FROM delta_log d
        LEFT JOIN estab_snap e ON e.ciclo = d.ciclo AND e.cnpj_full = d.cnpj_full
        LEFT JOIN empresa_snap em ON em.ciclo = e.ciclo AND em.cnpj_bas = e.cnpj_bas
        WHERE d.ciclo = ?
        ORDER BY d.kind, e.municipio_nome, d.cnpj_full
        """,
        (ciclo,),
    )
    for row in cur:
        cnpj = row[1]
        cnpj_fmt = (
            f"{cnpj[0:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:14]}"
            if len(cnpj) == 14
            else cnpj
        )
        ws2.append([row[0], cnpj_fmt, row[2], row[3], row[4], row[5], row[6], row[7]])
    for col_idx in range(1, len(delta_headers) + 1):
        ws2.column_dimensions[get_column_letter(col_idx)].width = 22

    xlsx_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(xlsx_path)
    return counts


def parse_cycle(cycle: str, log_path: Path) -> None:
    status = load_status()
    status["phase"] = "parsing"
    status["cycle"] = cycle
    save_status(status)
    log = open(log_path, "a", encoding="utf-8")
    log.write(f"[{now_iso()}] === parse_cycle {cycle} ===\n")
    log.flush()

    # Verify required files exist and are non-empty.
    missing = []
    for f in REQUIRED_FILES:
        p = DL_ROOT / f
        if not p.exists() or p.stat().st_size == 0:
            missing.append(f)
    if missing:
        msg = f"Missing/empty files: {missing}"
        log.write(f"[{now_iso()}] {msg}\n")
        log.flush()
        status["phase"] = "download_partial"
        status["errors"].append(msg)
        save_status(status)
        raise FileNotFoundError(msg)

    # Load exact CNAE / municipio codes from the metadata files.
    load_metadata()
    log.write(f"[{now_iso()}] resolved CNAE codes: {sorted(CNAE_CODES.get('FARMACIA', set()))[:4]}..., cosmetics: {sorted(CNAE_CODES.get('COSMETICOS', set()))}\n")
    log.write(f"[{now_iso()}] resolved municipalities: {sorted(MUN_BAIXADA_SANTISTA.items())}\n")
    log.flush()

    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=120)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA temp_store = MEMORY")
    init_db(conn)
    wipe_cycle(conn, cycle)

    started = now_iso()
    try:
        n_emp = load_empresas(conn, cycle)
        n_sim = load_simples(conn, cycle)
        counts = load_estabelecimentos(conn, cycle)
        diff = compute_diff(conn, cycle)
    except Exception as e:
        conn.close()
        status["phase"] = "parse_failed"
        status["errors"].append(str(e))
        save_status(status)
        raise

    counts_json = json.dumps({**counts, "diff": diff}, ensure_ascii=False, indent=2)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO ciclo (ciclo, source_url, started_at, finished_at, status, counts_json) "
        "VALUES (?,?,?,?,?,?)",
        (
            cycle,
            BASE_URL_TEMPLATE.format(cycle=cycle),
            started,
            now_iso(),
            "ok",
            counts_json,
        ),
    )
    conn.commit()

    xlsx_path = OUTPUT_ROOT / f"rfb-pharma-baixada-santista-{cycle}.xlsx"
    headline = write_xlsx(conn, cycle, xlsx_path)
    conn.close()

    status["phase"] = "complete"
    status["xlsx_path"] = str(xlsx_path)
    status["headline"] = headline
    status["counts"] = counts
    save_status(status)

    log.write(f"[{now_iso()}] parse done: {headline}\n")
    log.flush()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["download", "parse", "full"])
    ap.add_argument("cycle", nargs="?", default="2026-08")
    ap.add_argument("--log", default=str(LOG_PATH))
    args = ap.parse_args(argv[1:])

    if args.command in ("download", "full"):
        download_cycle(args.cycle, Path(args.log))
    if args.command in ("parse", "full"):
        parse_cycle(args.cycle, Path(args.log))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
