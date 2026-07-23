#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  status.sh — Full engine health check
#  Usage: ./status.sh
# ─────────────────────────────────────────────────────────────────────────────

DIR="$(cd "$(dirname "$0")" && pwd)"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; DIM='\033[2m'; NC='\033[0m'
BOLD='\033[1m'

hr()   { echo "──────────────────────────────────────────────────────"; }
ok()   { echo -e "  ${GREEN}✓${NC}  $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC}  $1"; }
dead() { echo -e "  ${RED}✗${NC}  $1"; }

# ── File age helper ────────────────────────────────────────────────────────────
file_status() {
    local label="$1"
    local file="$2"
    local stale_mins="${3:-120}"
    local mtime now age_m ts

    if [[ ! -f "$file" ]]; then
        dead "$label — NO FILE"
        return
    fi
    mtime=$(stat -f "%m" "$file" 2>/dev/null || stat -c "%Y" "$file" 2>/dev/null)
    now=$(date +%s)
    age_m=$(( (now - mtime) / 60 ))
    ts=$(date -r "$file" '+%H:%M:%S' 2>/dev/null || date -d "@$mtime" '+%H:%M:%S' 2>/dev/null)

    if (( age_m < stale_mins )); then
        ok "$label — ${ts}  (${age_m}m ago)"
    elif (( age_m < stale_mins * 3 )); then
        warn "$label — ${ts}  (${age_m}m ago)  ← getting stale"
    else
        dead "$label — ${ts}  (${age_m}m ago)  ← STALE"
    fi
}

glob_status() {
    local label="$1"
    local pattern="$2"
    local stale_mins="${3:-120}"
    local file
    file=$(ls -t $pattern 2>/dev/null | head -1)
    if [[ -z "$file" ]]; then
        dead "$label — NO FILE"
        return
    fi
    file_status "$label" "$file" "$stale_mins"
}

hr
echo -e "  ${BOLD}ENGINE HEALTH — $(date '+%Y-%m-%d %H:%M:%S')${NC}"
hr

# ── Containers ────────────────────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}CONTAINERS${NC}"
for name in e47_engine e47_engine_normalized e47_engine_v3; do
    status=$(docker ps --filter "name=^/${name}$" --format "{{.Status}}" 2>/dev/null)
    if [[ -n "$status" ]]; then
        ok "$name — $status"
    else
        dead "$name — NOT RUNNING"
    fi
done

# ── Coordinator ───────────────────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}COORDINATOR${NC}"
coord_pid=$(pgrep -f "coordinator.py" 2>/dev/null | head -1)
if [[ -n "$coord_pid" ]]; then
    ok "coordinator.py — running (PID $coord_pid)"
    if [[ -f "$DIR/logs/coordinator.log" ]]; then
        echo -e "  ${DIM}Last log:${NC}"
        tail -3 "$DIR/logs/coordinator.log" 2>/dev/null | sed 's/^/    /'
    fi
else
    dead "coordinator.py — NOT RUNNING"
fi

# ── Engine outputs ────────────────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}ENGINE OUTPUTS${NC}"
file_status  "Main engine " "$DIR/output/signals_current.xlsx"                         180
glob_status  "Normalized  " "$DIR/output/normalized_engine/normalized_*.xlsx"          180
glob_status  "V3          " "$DIR/output/v3/v3_*.xlsx"                                 180
glob_status  "Discovery   " "$DIR/output/discovery/discovery_*.csv"                    360
glob_status  "Confluence  " "$DIR/output/confluence/confluence_*.csv"                  360
glob_status  "Trade engine" "$DIR/output/trades/trades_*.csv"                          360
glob_status  "Backtest    " "$DIR/output/backtest_*.csv"                               1440
glob_status  "Snapshot    " "$DIR/output/snapshots/snapshot_*.csv"                     180

# ── Signal trackers ───────────────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}SIGNAL TRACKERS${NC}"
file_status  "Triangulated" "$DIR/output/signal_tracking/triangulated_signal_log.json" 360
file_status  "Main        " "$DIR/output/signal_tracking/main_signal_log.json"         360

# ── Sheets + overlay ─────────────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}SHEETS + OVERLAY${NC}"
file_status  "Sheets sync " "$DIR/logs/sheets_sync_state.json"                         360
file_status  "Overlay snap" "$DIR/market_overlay/latest_snapshot.json"                  60

overlay_pid=$(pgrep -f "overlay.py" 2>/dev/null | head -1)
if [[ -n "$overlay_pid" ]]; then
    ok "overlay.py  — running (PID $overlay_pid)"
else
    warn "overlay.py  — not running"
fi

# ── Script directory ──────────────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}SCRIPTS${NC}"
echo -e "  ${DIM}./fullrun.sh${NC}   — stop everything, fresh start, full cycle + EOD"
echo -e "  ${DIM}./eod.sh${NC}       — EOD pipeline only (engines must be running)"
echo -e "  ${DIM}./stop.sh${NC}      — stop all engines + coordinator"
echo -e "  ${DIM}./start.sh${NC}     — start engines + overlay"
echo -e "  ${DIM}./status.sh${NC}    — this health check"

# ── Log tails ─────────────────────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}LIVE LOGS${NC}"
echo -e "  ${DIM}tail -f $DIR/logs/coordinator.log${NC}"
echo -e "  ${DIM}docker logs e47_engine -f --tail 50${NC}"
echo -e "  ${DIM}docker logs e47_engine_normalized -f --tail 50${NC}"
echo -e "  ${DIM}docker logs e47_engine_v3 -f --tail 50${NC}"

hr
