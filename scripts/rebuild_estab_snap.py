#!/usr/bin/env python3
"""Re-run the load_estabelecimentos step (and compute_diff) for cycle 2026-08.

The empresa_snap and simples_snap tables already carry the full cycle data
(loaded by an earlier parse pass); only estab_snap is empty and needs to be
rebuilt from the on-disk Estabelecimentos*.zip files. compute_diff needs at
least one prior cycle to produce deltas — the first cycle returns zeros, so
that step is also a no-op for the first run.

Wipes the cycle's slice of estab_snap before reloading, so a re-run is safe.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(os.environ.get("PROJECT_ROOT", Path(__file__).resolve().parents[1]))
SCRIPTS = PROJECT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("run_pipeline", SCRIPTS / "run-pipeline.py")
run_pipeline = importlib.util.module_from_spec(_spec)  # type: ignore[assignment]
sys.modules["run_pipeline"] = run_pipeline
_spec.loader.exec_module(run_pipeline)  # type: ignore[union-attr]

DL_ROOT = PROJECT / "downloads"
WORK_ROOT = PROJECT / "work"
DB_PATH = WORK_ROOT / "rfb-pharma.db"
CYCLE = os.environ.get("CYCLE", "2026-08")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    print(f"[{now_iso()}] === rebuild_estab {CYCLE} ===", flush=True)

    missing = [f for f in run_pipeline.REQUIRED_FILES if not (DL_ROOT / f).exists()]
    if missing:
        print(f"ERROR: missing files: {missing}", file=sys.stderr)
        return 2

    run_pipeline.load_metadata()
    print(f"  CNAE FARMACIA: {sorted(run_pipeline.CNAE_CODES.get('FARMACIA', set()))[:4]}...", flush=True)
    print(f"  CNAE COSMETICOS: {sorted(run_pipeline.CNAE_CODES.get('COSMETICOS', set()))}", flush=True)
    print(f"  MUNICIPIOS: {len(run_pipeline.MUN_BAIXADA_SANTISTA)} codes", flush=True)

    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=180)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.row_factory = sqlite3.Row

    run_pipeline.init_db(conn)

    cur = conn.cursor()
    cur.execute("DELETE FROM estab_snap WHERE ciclo = ?", (CYCLE,))
    cur.execute("DELETE FROM delta_log WHERE ciclo = ?", (CYCLE,))
    cur.execute("DELETE FROM ciclo WHERE ciclo = ?", (CYCLE,))
    conn.commit()
    print(f"  wiped prior {CYCLE} slices", flush=True)

    t0 = time.time()
    counts = run_pipeline.load_estabelecimentos(conn, CYCLE)
    elapsed = time.time() - t0
    print(f"  load_estabelecimentos done in {elapsed:.1f}s — {json.dumps(counts, ensure_ascii=False)}", flush=True)

    diff = run_pipeline.compute_diff(conn, CYCLE)
    print(f"  compute_diff: {json.dumps(diff, ensure_ascii=False)}", flush=True)

    counts_json = json.dumps({**counts, "diff": diff}, ensure_ascii=False)
    cur.execute(
        "INSERT INTO ciclo (ciclo, source_url, started_at, finished_at, status, counts_json) "
        "VALUES (?,?,?,?,?,?)",
        (
            CYCLE,
            run_pipeline.BASE_URL_TEMPLATE.format(cycle=CYCLE),
            now_iso(),
            now_iso(),
            "ok",
            counts_json,
        ),
    )
    conn.commit()

    cur.execute(
        "SELECT layer, COUNT(*) FROM estab_snap WHERE ciclo = ? GROUP BY layer",
        (CYCLE,),
    )
    print(f"  final layer counts: {cur.fetchall()}", flush=True)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())