#!/bin/bash
# stop.sh — Stop all engines and coordinator

cd "$(dirname "$0")"
BASE="$HOME/Developer/sma_engine"

GREEN='\033[0;32m'; DIM='\033[2m'; NC='\033[0m'
step() { echo -e "\n${GREEN}▶ $1${NC}"; }

step "Stopping coordinator..."
pkill -f coordinator.py 2>/dev/null && echo "  Coordinator stopped." || echo "  (coordinator not running)"

step "Stopping engines..."
docker stop e47_engine e47_engine_normalized e47_engine_v3 2>/dev/null || true

step "Done. All engines stopped."
docker ps --format "  {{.Names}}\t{{.Status}}" | grep e47 || echo "  (none running)"
