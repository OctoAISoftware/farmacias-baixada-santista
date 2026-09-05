#!/usr/bin/env python3
"""Background geocoder with persistent local cache.

Uses Photon (Komoot/OSM) as the primary engine — generous rate limits,
cleaner Brazilian data, no daily cap. Falls back to Nominatim if Photon
returns nothing. Nominatim is rate-limited to 1 req/s; Photon is fine at
~3 req/s.

The cache file is a JSON dict: {"<normalized address key>": [lat, lon], ...}
or {"<key>": null} for negative results (so we don't re-try).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
import time

PROJECT = Path(os.environ.get("PROJECT_ROOT", Path(__file__).resolve().parents[1]))
DB_PATH = PROJECT / "work" / "rfb-pharma.db"
CACHE_PATH = PROJECT / "work" / "geocode" / "cache.json"
LOG_PATH = PROJECT / "work" / "geocode" / "geocode.log"

USER_AGENT = os.environ.get(
    "GEOCODER_UA",
    "farmacias-baixada-santista/1.0 (set GEOCODER_UA to override)",
)
SLEEP_S = float(os.environ.get("GEOCODER_SLEEP_S", "0.4"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_cache() -> dict[str, list[float] | None]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_cache(cache: dict) -> None:
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False))
    tmp.replace(CACHE_PATH)


def normalize_key(parts: list[str | None]) -> str:
    return "|".join(p.strip() for p in parts if p and p.strip()).lower()


def build_query(rec: dict) -> str:
    """Build a Nominatim q= string. Prefer the structured street, fallback to freeform."""
    tipo = rec.get("logradouro_tipo") or ""
    logr = rec.get("logradouro") or ""
    numero = rec.get("numero") or ""
    bairro = rec.get("bairro") or ""
    mun = (rec.get("municipio_nome") or "").strip()
    uf = (rec.get("uf") or "SP").strip()
    cep = (rec.get("cep") or "").strip()

    street = " ".join(filter(None, [tipo, logr, numero])).strip()
    parts = [p for p in [street, bairro, f"{mun}, {uf}", "Brasil"] if p]
    if cep:
        parts.insert(0, cep)
    return ", ".join(parts)


def fallback_queries(rec: dict) -> list[str]:
    """Ordered list of fallback queries: street → logradouro+municipio → CEP → municipio."""
    tipo = rec.get("logradouro_tipo") or ""
    logr = rec.get("logradouro") or ""
    numero = rec.get("numero") or ""
    bairro = rec.get("bairro") or ""
    mun = (rec.get("municipio_nome") or "").strip()
    uf = (rec.get("uf") or "SP").strip()
    cep = (rec.get("cep") or "").strip()

    street = " ".join(filter(None, [tipo, logr, numero])).strip()
    logr_only = " ".join(filter(None, [tipo, logr])).strip()
    out: list[str] = []
    if street:
        out.append(", ".join(filter(None, [street, bairro, f"{mun}, {uf}", "Brasil"])))
    if logr_only:
        out.append(", ".join(filter(None, [logr_only, f"{mun}, {uf}", "Brasil"])))
    if cep:
        out.append(f"{cep}, Brasil")
    if mun:
        out.append(f"{mun}, {uf}, Brasil")
    return out


def fetch_one(query: str) -> tuple[float, float] | None:
    """Try Photon first; Nominatim only as a last resort (it bans hard).
    Cache the None result so we don't re-try. We'll re-geocode after the
    monthly refresh for any rows still missing coords.
    """
    r = fetch_photon(query)
    if r is not None:
        return r
    return None


def fetch_photon(query: str) -> tuple[float, float] | None:
    """Photon (Komoot/OSM) — free, generous limits, good BR coverage."""
    params = {
        "q": query,
        "limit": 1,
    }
    url = "https://photon.komoot.io/api/?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log(f"photon HTTP {e.code} for q={query!r}")
        return None
    except Exception as e:
        log(f"photon error: {e} for q={query!r}")
        return None
    feats = data.get("features") if isinstance(data, dict) else None
    if not feats:
        return None
    geom = feats[0].get("geometry") or {}
    coords = geom.get("coordinates")
    if not coords or len(coords) < 2:
        return None
    return float(coords[1]), float(coords[0])


def fetch_nominatim(query: str) -> tuple[float, float] | None:
    params = {
        "q": query,
        "format": "json",
        "limit": 1,
        "countrycodes": "br",
        "addressdetails": 0,
    }
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            log("Nominatim HTTP 429 — backing off 60s")
            time.sleep(60)
            return None
        log(f"nominatim HTTP {e.code} for q={query!r}")
        return None
    except Exception as e:
        log(f"nominatim error: {e} for q={query!r}")
        return None
    if not data:
        return None
    try:
        return float(data[0]["lat"]), float(data[0]["lon"])
    except (KeyError, ValueError, IndexError):
        return None


def log(msg: str) -> None:
    line = f"[{now_iso()}] {msg}"
    print(line, file=sys.stderr, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def gather_targets(cycle: str) -> list[dict]:
    import sqlite3
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        SELECT e.cnpj_full, e.logradouro_tipo, e.logradouro, e.numero,
               e.bairro, e.cep, e.municipio_nome, e.uf, e.layer
        FROM estab_snap e
        WHERE e.ciclo = ? AND COALESCE(e.municipio_nome,'') <> ''
        ORDER BY (CASE WHEN e.layer='FARMACIA' THEN 0 ELSE 1 END), e.cnpj_full
        """,
        (cycle,),
    )
    rows = [dict(r) for r in cur]
    conn.close()
    return rows


def main() -> int:
    ap_known_cycle = None
    if "--cycle" in sys.argv:
        i = sys.argv.index("--cycle")
        ap_known_cycle = sys.argv[i + 1]
        sys.argv.pop(i)
        sys.argv.pop(i)

    cycle = ap_known_cycle or os.environ.get("CYCLE")
    if not cycle:
        # pick latest from db
        import sqlite3
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT ciclo FROM ciclo WHERE status='ok' ORDER BY ciclo DESC LIMIT 1")
        row = cur.fetchone()
        conn.close()
        if not row:
            log("ERROR: no cycle available")
            return 3
        cycle = row[0]

    log(f"start: cycle={cycle} sleep={SLEEP_S}s ua={USER_AGENT}")
    cache = load_cache()
    targets = gather_targets(cycle)
    pending = [r for r in targets if normalize_key([
        r["logradouro_tipo"], r["logradouro"], r["numero"], r["bairro"],
        r["municipio_nome"], r["uf"], r["cep"],
    ]) and normalize_key([
        r["logradouro_tipo"], r["logradouro"], r["numero"], r["bairro"],
        r["municipio_nome"], r["uf"], r["cep"],
    ]) not in cache]

    log(f"targets: {len(targets)}; already in cache: {len(targets) - len(pending)}; pending: {len(pending)}")

    filled = 0
    negative = 0
    n = 0
    for rec in pending:
        key = normalize_key([
            rec["logradouro_tipo"], rec["logradouro"], rec["numero"], rec["bairro"],
            rec["municipio_nome"], rec["uf"], rec["cep"],
        ])
        q = build_query(rec)
        # Use ONE query per address — Nominatim usage policy is 1 req/s.
        result = fetch_one(q)
        if result:
            cache[key] = [result[0], result[1]]
            filled += 1
        else:
            cache[key] = None
            negative += 1
        n += 1
        if n % 10 == 0:
            save_cache(cache)
            log(f"checkpoint: processed={n} filled={filled} negative={negative}")
        time.sleep(SLEEP_S)

    save_cache(cache)
    log(f"done: processed={n} filled={filled} negative={negative}")
    return 0


if __name__ == "__main__":
    sys.exit(main())