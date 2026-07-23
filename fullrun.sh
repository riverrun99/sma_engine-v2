#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# fullrun.sh — Full engine suite from scratch.
#
# Sequence:
#   0. Stop everything cleanly
#   1. Start all three engine containers
#   2. Start coordinator in background (handles main→norm→V3→discovery→
#      confluence→sheets chain)
#   3. Trigger main engine immediately (SIGUSR1)
#   4. Wait for coordinator to complete one full cycle (V3 output updated)
#   5. EOD pipeline: backtest → trade → triangulator → signal trackers →
#      sheets → telegram
# ─────────────────────────────────────────────────────────────────────────────

set -e
cd "$(dirname "$0")"
BASE="$HOME/Developer/sma_engine"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; DIM='\033[2m'; NC='\033[0m'

step()  { echo -e "\n${GREEN}▶ $1${NC}"; }
info()  { echo -e "${DIM}  $1${NC}"; }
warn()  { echo -e "${YELLOW}  ⚠ $1${NC}"; }
error() { echo -e "${RED}  ✗ $1${NC}"; exit 1; }

POLL_INTERVAL=15   # seconds between output checks
TIMEOUT=7200       # max wait for full cycle (2 hours)

echo -e "${GREEN}═══════════════════════════════════════════════${NC}"
echo -e "${GREEN}  FULL RUN — $(date '+%Y-%m-%d %H:%M')${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════${NC}"


# ── mtime helpers ─────────────────────────────────────────────────────────────
mtime() {
    local f="$1"
    [[ -f "$f" ]] && (stat -f "%m" "$f" 2>/dev/null || stat -c "%Y" "$f" 2>/dev/null) || echo 0
}

latest_mtime() {
    local latest=0
    for f in $1; do
        [[ -f "$f" ]] || continue
        t=$(mtime "$f")
        (( t > latest )) && latest=$t
    done
    echo $latest
}

wait_for_output() {
    local label="$1"
    local pattern="$2"
    local baseline="$3"
    local elapsed=0
    info "Waiting for $label..."
    while true; do
        current=$(latest_mtime "$pattern")
        if (( current > baseline )); then
            info "$label ✓  ($(date '+%H:%M:%S'))"
            return 0
        fi
        (( elapsed >= TIMEOUT )) && error "$label timed out after ${TIMEOUT}s"
        sleep $POLL_INTERVAL
        elapsed=$(( elapsed + POLL_INTERVAL ))
        info "  ...${elapsed}s"
    done
}


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 0 — Stop everything
# ══════════════════════════════════════════════════════════════════════════════

step "Stopping all engines and coordinator..."
bash "$BASE/stop.sh" 2>&1 | grep -v "^$" || true
sleep 5


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — Start containers
# ══════════════════════════════════════════════════════════════════════════════

step "Starting engine containers..."

info "Main stack (InfluxDB + engine)..."
cd "$BASE"
docker-compose up -d 2>&1 | grep -E "Started|Running|Healthy|Created" || true

# Normalized and V3 run as separate compose projects but share the main stack's
# network. When the main stack restarts it creates a NEW network, so a reused
# container from a prior run holds a stale network reference and fails to start
# ("network ... not found") — silently, because it's a separate project.
# --force-recreate rebuilds them against the current network every time.
info "Normalized engine..."
docker-compose -f docker-compose.normalized.yml up -d --force-recreate 2>&1 | grep -E "Started|Running|Created|Error" || true
if ! docker ps --format "{{.Names}}" | grep -q "^e47_engine_normalized$"; then
    warn "normalized did not start — retrying with a clean container..."
    docker rm -f e47_engine_normalized 2>/dev/null || true
    docker-compose -f docker-compose.normalized.yml up -d 2>&1 | grep -E "Started|Error" || true
fi

info "V3 engine..."
cd "$BASE/_v3_staging"
docker-compose -f docker-compose.v3.yml up -d --force-recreate 2>&1 | grep -E "Started|Running|Created|Error" || true
cd "$BASE"
if ! docker ps --format "{{.Names}}" | grep -q "^e47_engine_v3$"; then
    warn "V3 did not start — retrying with a clean container..."
    docker rm -f e47_engine_v3 2>/dev/null || true
    cd "$BASE/_v3_staging"
    docker-compose -f docker-compose.v3.yml up -d 2>&1 | grep -E "Started|Error" || true
    cd "$BASE"
fi

info "Waiting 15s for containers to initialize..."
sleep 15

# ── Verify all three engines are actually up before proceeding ────────────────
step "Confirming all engines are running..."
for c in e47_engine e47_engine_normalized e47_engine_v3; do
    if docker ps --format "{{.Names}}" | grep -q "^${c}$"; then
        info "$c ✓"
    else
        warn "$c is NOT running — the chain will be incomplete. Check: docker logs $c"
    fi
done

echo ""
docker ps --format "  {{.Names}}\t{{.Status}}" | grep e47 || true


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — Start coordinator + trigger main engine
# ══════════════════════════════════════════════════════════════════════════════

step "Starting coordinator in background..."
COORD_LOG="$BASE/logs/coordinator.log"
nohup python3 "$BASE/coordinator.py" >> "$COORD_LOG" 2>&1 &
COORD_PID=$!
info "Coordinator PID $COORD_PID → logs: $COORD_LOG"
sleep 3

# Snapshot baselines
MAIN_FILE="$BASE/output/signals_current.xlsx"
V3_GLOB="$BASE/output/v3/v3_*.xlsx"

main_baseline=$(mtime "$MAIN_FILE")
v3_baseline=$(latest_mtime "$V3_GLOB")

step "Triggering main engine scan immediately (SIGUSR1)..."
docker kill --signal=USR1 e47_engine 2>&1 | grep -v "^e47" || true
info "Main engine will run now — coordinator watching for output"


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3 — Wait for coordinator to complete one full cycle
# (coordinator handles: main → norm → V3 → discovery → confluence → sheets)
# ══════════════════════════════════════════════════════════════════════════════

step "Waiting for full engine cycle to complete..."
info "Coordinator is chaining: main → norm (180s stagger) → V3 → discovery → confluence → sheets"
info "This typically takes 45-90 minutes..."

# Wait for V3 output — signals that the full chain (main+norm+V3) is done
wait_for_output "V3 engine output" "$V3_GLOB" "$v3_baseline"

# Give coordinator a moment to finish discovery + confluence + sheets after V3
info "V3 done — waiting 60s for coordinator to finish discovery + confluence + sheets..."
sleep 60

info "Full cycle complete."


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 4 — EOD pipeline (parts coordinator doesn't run)
# ══════════════════════════════════════════════════════════════════════════════

step "Running EOD pipeline..."

step "  [1/6] Backtest..."
docker exec e47_engine python /app/run_backtest.py 2>&1 | tail -5

step "  [2/5] Trade Engine..."
docker exec e47_engine python /app/trade_engine.py \
  --min-confidence LOW --discovery-tf 1d,1w,1mo --min-rr 1.0 --min-sharpe 1.0 2>&1 | tail -5

step "  [3/5] Signal Trackers..."
docker exec e47_engine python /app/signal_tracker.py 2>&1 | tail -3
python3 "$BASE/signal_tracker_main.py" 2>&1 | tail -3
python3 "$BASE/signal_tracker_triangulated.py" 2>&1 | tail -3

step "  [4/5] Sheets Sync (final)..."
python3 "$BASE/opboard.py" --export 2>&1 | tail -2
python3 "$BASE/market_overlay/sheets_sync.py" 2>&1 | tail -5

step "  [5/5] Telegram EOD..."
if [[ -f "$BASE/telegram_eod.py" ]]; then
  python3 "$BASE/telegram_eod.py" 2>&1 | tail -5
else
  info "telegram_eod.py not present — skipping Telegram notification"
fi


echo -e "\n${GREEN}═══════════════════════════════════════════════${NC}"
echo -e "${GREEN}  FULL RUN COMPLETE — $(date '+%H:%M')${NC}"
echo -e "${GREEN}  Coordinator still running (PID $COORD_PID)${NC}"
echo -e "${GREEN}  Tail logs: tail -f $COORD_LOG${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════${NC}\n"
