#!/usr/bin/env bash
# download-cnpj.sh — sequential, resumable, single-stream downloads of the
# Receita Federal CNPJ open-data bundle for one cycle (YYYY-MM).
#
# Order chosen so a partial run leaves us with the highest-priority files
# complete (the largest Estabelecimentos slice first; smaller fixeds last).
# This script NEVER runs multiple curls in parallel — the SERPRO mirror
# throttles concurrent streams to ~0.5MB/s each. Sequential stays at the
# full ~6MB/s single-stream rate.

set -u

SHARE_TOKEN="gn672Ad4CF8N6TK"
BASE_URL="https://arquivos.receitafederal.gov.br/public.php/webdav/Dados/Cadastros/CNPJ"
DL_ROOT="${DL_ROOT:-$(cd "$(dirname "$0")/.." && pwd)/downloads}"
LOG="${LOG:-/tmp/cnpj-dl.log}"

CYCLE="${1:-2026-08}"
mkdir -p "$(dirname "$LOG")"

# Files in priority order. Estabelecimentos0 is the biggest and most useful
# slice; the rest follow. Empresas0 is similarly large. Simples and the tiny
# reference files (Cnaes, Municipios) are last so the high-value data
# finishes first.
FILES=(
  Estabelecimentos0.zip
  Estabelecimentos1.zip
  Estabelecimentos2.zip
  Estabelecimentos3.zip
  Estabelecimentos4.zip
  Estabelecimentos5.zip
  Estabelecimentos6.zip
  Estabelecimentos7.zip
  Estabelecimentos8.zip
  Estabelecimentos9.zip
  Empresas0.zip
  Empresas1.zip
  Empresas2.zip
  Empresas3.zip
  Empresas4.zip
  Empresas5.zip
  Empresas6.zip
  Empresas7.zip
  Empresas8.zip
  Empresas9.zip
  Simples.zip
  Cnaes.zip
  Municipios.zip
)

# Optional: list expected sizes (used to skip when a file is already complete)
# We fetch this lazily on the first pass via a single PROPFIND; cached.
get_expected_sizes() {
  curl -sL --max-time 20 -u "${SHARE_TOKEN}:" \
    "${BASE_URL}/${CYCLE}/" -X PROPFIND -H "Depth: 1" \
  | awk '
    /<d:href>/ {
      match($0, /<d:href>([^<]*)/, a); f=a[1]
    }
    /<d:getcontentlength>/ {
      match($0, /<d:getcontentlength>([0-9]+)/, b); s=b[1]
      if (f && s) print f " " s
    }' \
  | sed 's|.*/||' > "${LOG}.sizes"
}

dl_one() {
    local f="$1"
    local target="${DL_ROOT}/${f}"
    local url="${BASE_URL}/${CYCLE}/${f}"
    local expected
    expected=$(awk -v F="$f" '$1==F{print $2}' "${LOG}.sizes" 2>/dev/null)
    expected=${expected:-0}

    if [ -f "$target" ] && [ "$expected" -gt 0 ]; then
        local sz
        sz=$(stat -c%s "$target")
        if [ "$sz" -ge "$expected" ]; then
            echo "[$(date +%H:%M:%S)] $f: already complete (${sz}/${expected})" | tee -a "$LOG"
            return 0
        else
            echo "[$(date +%H:%M:%S)] $f: partial ${sz}/${expected}, resuming" | tee -a "$LOG"
        fi
    fi

    local attempts=0
    local max_attempts=10
    while [ $attempts -lt $max_attempts ]; do
        attempts=$((attempts+1))
        # -C - for resume; -f fails on 4xx; -L follow redirects
        if curl -C - -fL --connect-timeout 30 --max-time 7200 \
            -u "${SHARE_TOKEN}:" \
            -A "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36" \
            "$url" -o "$target" 2>>"$LOG"; then
            local sz
            sz=$(stat -c%s "$target" 2>/dev/null || echo 0)
            echo "[$(date +%H:%M:%S)] $f: done (${sz} bytes)" | tee -a "$LOG"
            return 0
        fi
        echo "[$(date +%H:%M:%S)] $f: attempt ${attempts}/${max_attempts} failed; sleep 10s" | tee -a "$LOG"
        sleep 10
    done
    echo "[$(date +%H:%M:%S)] $f: FAILED after ${max_attempts} attempts" | tee -a "$LOG"
    return 1
}

echo "[$(date +%H:%M:%S)] cycle ${CYCLE}: fetching expected sizes..." | tee -a "$LOG"
get_expected_sizes
wc -l "${LOG}.sizes"

for f in "${FILES[@]}"; do
    dl_one "$f" || { echo "[$(date +%H:%M:%S)] aborting at $f"; exit 2; }
done

echo "[$(date +%H:%M:%S)] cycle ${CYCLE}: all downloads complete" | tee -a "$LOG"
