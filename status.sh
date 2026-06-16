#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  status.sh — Quick engine status check
#  Usage: ./status.sh
# ─────────────────────────────────────────────────────────────────────────────

DIR="$(cd "$(dirname "$0")" && pwd)"

hr() { echo "──────────────────────────────────────────────────────────"; }

hr
echo "  ENGINE STATUS — $(date '+%H:%M:%S %Z')"
hr

# ── Containers ───────────────────────────────────────────────────────────────
echo ""
echo "  CONTAINERS"
docker ps --format "  {{.Names}}\t{{.Status}}" | grep e47_engine || echo "  (no engine containers running)"

# ── Latest outputs ───────────────────────────────────────────────────────────
echo ""
echo "  LATEST OUTPUTS"

main_file="$DIR/output/signals_current.xlsx"
if [ -f "$main_file" ]; then
    echo "  Main:       $(date -r "$main_file" '+%H:%M:%S') — $(basename "$main_file")"
else
    echo "  Main:       (no output yet)"
fi

norm_file=$(ls -t "$DIR/output/normalized_engine/normalized_"*.xlsx 2>/dev/null | head -1)
if [ -n "$norm_file" ]; then
    echo "  Normalized: $(date -r "$norm_file" '+%H:%M:%S') — $(basename "$norm_file")"
else
    echo "  Normalized: (no output yet)"
fi

v3_file=$(ls -t "$DIR/output/v3/v3_"*.xlsx 2>/dev/null | head -1)
if [ -n "$v3_file" ]; then
    echo "  V3:         $(date -r "$v3_file" '+%H:%M:%S') — $(basename "$v3_file")"
else
    echo "  V3:         (no output yet)"
fi

# ── Coordinator ──────────────────────────────────────────────────────────────
echo ""
echo "  COORDINATOR"
coord_pid=$(pgrep -f coordinator.py 2>/dev/null)
if [ -n "$coord_pid" ]; then
    echo "  Running (PID $coord_pid)"
    echo "  Last activity:"
    tail -3 "$DIR/logs/coordinator.log" 2>/dev/null | sed 's/^/    /'
else
    echo "  NOT RUNNING — start with: nohup python3 coordinator.py >> logs/coordinator.log 2>&1 &"
fi

# ── Log commands ─────────────────────────────────────────────────────────────
echo ""
echo "  LOG COMMANDS"
echo "  docker logs e47_engine -f"
echo "  docker logs e47_engine_normalized -f"
echo "  docker logs e47_engine_v3 -f"
echo "  tail -f $DIR/logs/coordinator.log"

hr
