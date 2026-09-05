#!/usr/bin/env bash
# monthly-rfb-update.sh — entrypoint for the monthly auto-update.
#
# Runs in this order:
#   1. python3 scripts/run-pipeline.py download YYYY-MM   (idempotent, resumes)
#   2. python3 scripts/run-pipeline.py parse   YYYY-MM
#   3. python3 scripts/build_data_json.py --cycle YYYY-MM
#   4. nohup python3 scripts/geocode_nominatim.py --cycle YYYY-MM &  (background fill)
#   5. POST /api/delegated-result to comms-okto so Ocky can ping the customer
#
# Designed to be invoked from a crontab entry:
#   0 6 1 * *   /opt/farmacias-baixada-santista/monthly-rfb-update.sh 2026-09
#
# The cycle argument can be passed in (the crontab should compute "previous month"
# at scheduling time); if not, defaults to previous month in America/Sao_Paulo.
set -euo pipefail

PROJECT="${PROJECT_ROOT:-$(cd "$(dirname "$0")" && pwd)}"
SCRIPTS="$PROJECT/scripts"
WORK="$PROJECT/work"
LOGDIR="$WORK/log"
mkdir -p "$LOGDIR"

cycle_arg="${1:-}"
if [[ -z "$cycle_arg" ]]; then
  cycle_arg="$(TZ=America/Sao_Paulo date -d 'last month' +%Y-%m)"
fi
cycle="$cycle_arg"

echo "[$(date -Iseconds)] monthly-rfb-update cycle=$cycle starting"
LOG_FILE="$LOGDIR/monthly-${cycle}.log"

{
  set -x
  cd "$PROJECT"

  echo "--- 1. download phase"
  python3 "$SCRIPTS/run-pipeline.py" download "$cycle" || echo "download had errors, will try parse anyway"

  echo "--- 2. parse phase"
  python3 "$SCRIPTS/run-pipeline.py" parse "$cycle" || {
    echo "PARSE FAILED for $cycle"
    exit 2
  }

  echo "--- 3. data.json for the site"
  python3 "$SCRIPTS/build_data_json.py" --cycle "$cycle"

  echo "--- 4. background geocoder"
  nohup env GEOCODER_SLEEP_S=0.4 \
    python3 "$SCRIPTS/geocode_nominatim.py" --cycle "$cycle" \
    > "$LOGDIR/geocode-${cycle}.log" 2>&1 &
  echo "geocode pid $!"

  echo "--- 5. done"
  echo "[$(date -Iseconds)] monthly-rfb-update cycle=$cycle DONE"
} | tee -a "$LOG_FILE"