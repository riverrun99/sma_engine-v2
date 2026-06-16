#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  start_engines.sh — Sequential cold-start: V3 → Main → Normalized
#
#  Starts each engine only after the previous one finishes its first cycle.
#  Then launches the coordinator for automatic staggering on all future cycles.
#
#  Usage:
#    cd ~/Developer/sma_engine
#    chmod +x start_engines.sh
#    ./start_engines.sh
# ─────────────────────────────────────────────────────────────────────────────

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

log() { echo "[$(date +%H:%M:%S)] $*"; }
hr()  { echo "──────────────────────────────────────────────────────────"; }

# ── Wait for a glob pattern to produce a NEW file ───────────────────────────
wait_for_new_file() {
    local pattern="$1"
    local label="$2"
    local baseline
    baseline=$(ls -t $pattern 2>/dev/null | head -1)
    log "  Waiting for $label to finish cycle 0..."
    while true; do
        local latest
        latest=$(ls -t $pattern 2>/dev/null | head -1)
        if [ -n "$latest" ] && [ "$latest" != "$baseline" ]; then
            log "  ✓ $label done → $(basename "$latest")"
            return 0
        fi
        sleep 10
    done
}

# ── Wait for a single file's mtime to advance ───────────────────────────────
wait_for_mtime() {
    local file="$1"
    local label="$2"
    local baseline=0
    [ -f "$file" ] && baseline=$(stat -f %m "$file" 2>/dev/null)
    log "  Waiting for $label to finish cycle 0..."
    while true; do
        if [ -f "$file" ]; then
            local cur
            cur=$(stat -f %m "$file" 2>/dev/null)
            if [ "$cur" -gt "$baseline" ]; then
                log "  ✓ $label done"
                return 0
            fi
        fi
        sleep 10
    done
}

# ── Stop any existing engine containers ─────────────────────────────────────
hr
log "Stopping any running engine containers..."
docker stop e47_engine e47_engine_normalized e47_engine_v3 2>/dev/null || true

# ── Stop coordinator if already running ─────────────────────────────────────
pkill -f coordinator.py 2>/dev/null && log "Stopped existing coordinator." || true

mkdir -p "$DIR/logs"
hr

# ════════════════════════════════════════════════════════════════════════════
#  STEP 1 — V3
# ════════════════════════════════════════════════════════════════════════════
log "STEP 1/3 — Starting V3..."
cd "$DIR/_v3_staging"
docker-compose -f docker-compose.v3.yml up -d
cd "$DIR"
log "  V3 is scanning. Logs: docker logs e47_engine_v3 -f"
wait_for_new_file "$DIR/output/v3/v3_*.xlsx" "V3"

hr
# ════════════════════════════════════════════════════════════════════════════
#  STEP 2 — Main
# ════════════════════════════════════════════════════════════════════════════
log "STEP 2/3 — Starting Main engine..."
docker-compose up -d
log "  Main is scanning. Logs: docker logs e47_engine -f"
wait_for_mtime "$DIR/output/signals_current.xlsx" "Main"

hr
# ════════════════════════════════════════════════════════════════════════════
#  STEP 3 — Normalized
# ════════════════════════════════════════════════════════════════════════════
log "STEP 3/3 — Starting Normalized engine..."
docker-compose -f docker-compose.normalized.yml up -d
log "  Normalized is scanning. Logs: docker logs e47_engine_normalized -f"
wait_for_new_file "$DIR/output/normalized_engine/normalized_*.xlsx" "Normalized"

hr
# ════════════════════════════════════════════════════════════════════════════
#  COORDINATOR — handles all future staggering automatically
# ════════════════════════════════════════════════════════════════════════════
log "Starting coordinator (main → normalized → V3 for all future cycles)..."
nohup python3 "$DIR/coordinator.py" >> "$DIR/logs/coordinator.log" 2>&1 &
log "  Coordinator PID: $! — logs: tail -f $DIR/logs/coordinator.log"

hr
log "ALL ENGINES RUNNING. Run ./status.sh to check at any time."
hr
