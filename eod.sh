#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# eod.sh — End of Day pipeline
#
# Usage:
#   ./eod.sh
# ─────────────────────────────────────────────────────────────────────────────

set -e
cd "$(dirname "$0")"
BASE="$HOME/Developer/sma_engine"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; DIM='\033[2m'; NC='\033[0m'

step() { echo -e "\n${GREEN}▶ $1${NC}"; }
info() { echo -e "${DIM}  $1${NC}"; }
warn() { echo -e "${YELLOW}  ⚠ $1${NC}"; }

echo -e "${GREEN}═══════════════════════════════════════${NC}"
echo -e "${GREEN}  EOD ENGINE RUN — $(date '+%Y-%m-%d %H:%M')${NC}"
echo -e "${GREEN}═══════════════════════════════════════${NC}"

# ── Ensure main container is running ─────────────────────────────────────────
step "Checking containers..."
if ! docker ps --filter "name=^/e47_engine$" --format "{{.Names}}" | grep -q e47_engine; then
    warn "e47_engine not running — starting..."
    cd "$BASE" && docker-compose up -d
    sleep 10
else
    info "e47_engine running ✓"
fi

step "[1/7] Discovery (1d/1w/1mo)..."
docker exec e47_engine python /app/discovery_engine.py \
  --timeframes 1d,1w,1mo 2>&1 | tail -5

step "[2/7] Backtest..."
docker exec e47_engine python /app/run_backtest.py 2>&1 | tail -5

step "[3/7] Confluence..."
docker exec e47_engine python /app/confluence_engine.py \
  --min-score 2 --discovery-tf 1d,1w,1mo 2>&1 | tail -5

step "[4/7] Trade Engine..."
docker exec e47_engine python /app/trade_engine.py \
  --min-confidence LOW --discovery-tf 1d,1w,1mo --min-rr 1.0 --min-sharpe 1.0 2>&1 | tail -5

step "[5/7] Signal Trackers..."
docker exec e47_engine python /app/signal_tracker.py 2>&1 | tail -3
python3 "$BASE/signal_tracker_main.py" 2>&1 | tail -3
python3 "$BASE/signal_tracker_triangulated.py" 2>&1 | tail -3

step "[6/7] Sheets Sync..."
python3 "$BASE/opboard.py" --export 2>&1 | tail -2
python3 "$BASE/market_overlay/sheets_sync.py" 2>&1 | tail -5

step "[7/7] Telegram EOD Wrap-up..."
if [[ -f "$BASE/telegram_eod.py" ]]; then
  python3 "$BASE/telegram_eod.py" 2>&1 | tail -5
else
  info "telegram_eod.py not present — skipping Telegram notification"
fi

echo -e "\n${GREEN}═══════════════════════════════════════${NC}"
echo -e "${GREEN}  EOD COMPLETE — $(date '+%H:%M')${NC}"
echo -e "${GREEN}═══════════════════════════════════════${NC}\n"
