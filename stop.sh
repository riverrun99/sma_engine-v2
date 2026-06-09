#!/bin/bash
# stop.sh — Stop all engines

cd "$(dirname "$0")"
BASE="$HOME/Developer/sma_engine"

GREEN='\033[0;32m'; DIM='\033[2m'; NC='\033[0m'
step() { echo -e "\n${GREEN}▶ $1${NC}"; }

step "Stopping V3 engine..."
cd "$HOME/Developer/sma_engine_v3"
docker compose -f docker-compose.v3.yml down

step "Stopping original stack (engine, normalized, InfluxDB, Grafana)..."
cd "$BASE"
docker compose down

step "Done. All containers stopped."
docker ps --format "  {{.Names}}\t{{.Status}}" | grep e47 || echo "  (none running)"
