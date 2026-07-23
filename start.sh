#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# start.sh — Launch everything
#
# Usage:
#   ./start.sh          → start all engines + pipeline + overlay
#   ./start.sh engines  → start docker stacks only (no pipeline, no overlay)
#   ./start.sh pipeline → run pipeline only (engines must already be running)
#   ./start.sh overlay  → open overlay only
# ─────────────────────────────────────────────────────────────────────────────

set -e
cd "$(dirname "$0")"
BASE="$HOME/Developer/sma_engine"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; DIM='\033[2m'; NC='\033[0m'

step() { echo -e "\n${GREEN}▶ $1${NC}"; }
info() { echo -e "${DIM}  $1${NC}"; }
warn() { echo -e "${YELLOW}  ⚠ $1${NC}"; }

MODE="${1:-all}"

# ── 1. Docker stacks ──────────────────────────────────────────────────────────
if [[ "$MODE" == "all" || "$MODE" == "engines" ]]; then

  step "Starting original engine stack (InfluxDB + Grafana + engine + normalized)..."
  cd "$BASE"
  docker compose up -d
  info "Containers: e47_influxdb, e47_grafana, e47_engine, e47_engine_normalized"

  step "Starting V3 engine..."
  cd "$HOME/Developer/sma_engine/_v3_staging"
  docker compose -f docker-compose.v3.yml up -d
  info "Container: e47_engine_v3"

  step "Waiting 10s for engines to initialize..."
  sleep 10

  step "Container status:"
  docker ps --format "  {{.Names}}\t{{.Status}}" | grep e47

fi

# ── 2. Pipeline ───────────────────────────────────────────────────────────────
if [[ "$MODE" == "all" || "$MODE" == "pipeline" ]]; then

  step "Running discovery (1d/1w/1mo timeframes)..."
  docker exec e47_engine python /app/discovery_engine.py \
    --timeframes 1d,1w,1mo 2>&1 | tail -5

  step "Running backtest..."
  docker exec e47_engine python /app/run_backtest.py 2>&1 | tail -5

  step "Running confluence (min score 2)..."
  docker exec e47_engine python /app/confluence_engine.py \
    --min-score 2 --discovery-tf 1d,1w,1mo 2>&1 | tail -5

  step "Running trade engine..."
  docker exec e47_engine python /app/trade_engine.py \
    --min-confidence MEDIUM --discovery-tf 1d,1w,1mo --min-rr 1.5 2>&1 | tail -5

  info "Pipeline complete. Output in $BASE/output/"

fi

# ── 3. Market overlay ─────────────────────────────────────────────────────────
if [[ "$MODE" == "all" || "$MODE" == "overlay" ]]; then

  step "Launching market overlay (Ctrl+C to exit)..."
  cd "$BASE/market_overlay"
  python3 overlay.py

fi
