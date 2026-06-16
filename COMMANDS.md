# SMA Engine — Command Reference

## Starting Everything

```bash
# Full sequential cold-start: V3 → Main → Normalized → Coordinator (recommended)
cd ~/Developer/sma_engine && ./start_engines.sh

# Check status of all engines, outputs, and coordinator at a glance
cd ~/Developer/sma_engine && ./status.sh

# Live coordinator log (shows when each engine is triggered)
tail -f ~/Developer/sma_engine/logs/coordinator.log
```

---

## Engine Logs (live)

```bash
docker logs e47_engine -f
docker logs e47_engine_normalized -f
docker logs e47_engine_v3 -f
```

---

## Manual Engine Control (if not using start_engines.sh)

```bash
# Start main engine only
cd ~/Developer/sma_engine && docker-compose up -d

# Start normalized engine only
cd ~/Developer/sma_engine && docker-compose -f docker-compose.normalized.yml up -d

# Start V3 engine only
cd ~/Developer/sma_engine/_v3_staging && docker-compose -f docker-compose.v3.yml up -d

# Stop all engines
docker stop e47_engine e47_engine_normalized e47_engine_v3

# Start coordinator manually (after all engines have completed cycle 0)
cd ~/Developer/sma_engine && nohup python3 coordinator.py >> logs/coordinator.log 2>&1 &
```

---

## Market Overlay

```bash
# Start overlay only (The System + Zero Gamma + Signals panel — lightweight)
cd ~/Developer/sma_engine && ./start.sh overlay

# Start everything (engines + pipeline + overlay)
cd ~/Developer/sma_engine && ./start.sh

# Stop everything
cd ~/Developer/sma_engine && ./stop.sh

# Open live dashboard in browser (auto-refreshes every 60s)
open ~/Developer/sma_engine/market_overlay/dashboard.html

# Force sync all engine outputs to Google Sheets immediately
cd ~/Developer/sma_engine/market_overlay && python3 sheets_sync.py

# Run The System analysis standalone
cd ~/Developer/sma_engine/market_overlay && python3 the_system.py

# Check zero gamma (Tikitrade)
cd ~/Developer/sma_engine/market_overlay && python3 gamma_engine.py

# Check index GEX (QQQ/IWM/DIA)
cd ~/Developer/sma_engine/market_overlay && python3 index_gex.py

# Check all index systems state (SPX/IXIC/DJI/IWM/IWV/SOX/VIX/SVIX) standalone
cd ~/Developer/sma_engine/market_overlay && python3 systems_panel.py
```

---

## Engine Management

```bash
# Start all containers
cd ~/Developer/sma_engine && docker compose up -d

# Stop all containers
cd ~/Developer/sma_engine && docker compose down

# Restart engine only (picks up muted/custom ticker changes)
cd ~/Developer/sma_engine && docker compose restart engine

# Full restart (picks up .env changes like ENGINE_LOOKBACK)
cd ~/Developer/sma_engine && docker compose down && docker compose up -d

# Check container status
cd ~/Developer/sma_engine && docker compose ps

# Live engine log stream (Ctrl+C to exit)
docker logs -f e47_engine

# Last 50 log lines
docker logs --tail 50 e47_engine

# CPU/memory usage
docker stats
```

---

## Verify Config

```bash
# Check active lookback setting
docker exec e47_engine printenv ENGINE_LOOKBACK

# Check all env vars
docker exec e47_engine printenv | grep ENGINE
```

---

## Discovery Engine

```bash
# Run discovery — all timeframes, all SMAs
docker exec e47_engine python /app/discovery_engine.py

# Run discovery — daily/weekly/monthly only (recommended)
docker exec e47_engine python /app/discovery_engine.py --timeframes 1d,1w,1mo

# Run discovery — long-term SMAs only (>= 200)
docker exec e47_engine python /app/discovery_engine.py --timeframes 1d,1w,1mo --min-period 200

# Run discovery — tighter absence window (catches more signals)
docker exec e47_engine python /app/discovery_engine.py --absence 50 --timeframes 1d,1w,1mo
```

---

## Backtest Engine

```bash
# Run backtest on current top signals
docker exec e47_engine python /app/run_backtest.py

# Run backtest — top 200 signals
docker exec e47_engine python /app/run_backtest.py --top 200

# Run backtest — custom horizon
docker exec e47_engine python /app/run_backtest.py --horizon 20
```

---

## Confluence Engine

```bash
# Cross-reference all three engines — show 2/3 and 3/3 only
docker exec e47_engine python /app/confluence_engine.py --min-score 2

# Filter discovery to daily/weekly/monthly only
docker exec e47_engine python /app/confluence_engine.py --min-score 2 --discovery-tf 1d,1w,1mo

# Show only 3/3 perfect confluence
docker exec e47_engine python /app/confluence_engine.py --min-score 3

# Raise Sharpe threshold for backtest
docker exec e47_engine python /app/confluence_engine.py --min-score 2 --min-sharpe 5.0
```

---

## Trade Engine

```bash
# Run trade suggestions — MEDIUM and HIGH confidence, R/R >= 1.5
docker exec e47_engine python /app/trade_engine.py --min-confidence MEDIUM --min-rr 1.5

# Filter discovery to daily/weekly/monthly
docker exec e47_engine python /app/trade_engine.py --min-confidence MEDIUM --discovery-tf 1d,1w,1mo --min-rr 1.5

# HIGH confidence only, strong R/R
docker exec e47_engine python /app/trade_engine.py --min-confidence HIGH --min-rr 2.0

# All suggestions including LOW confidence
docker exec e47_engine python /app/trade_engine.py --min-rr 1.0
```

---

## Signal Forward Return Tracker

```bash
# Update log + print report (run after any engine cycle)
cd ~/Developer/sma_engine && python3 signal_tracker_v2.py

# Track top 50 signals only
cd ~/Developer/sma_engine && python3 signal_tracker_v2.py --top 50

# Print report only — no price fetch, no log update
cd ~/Developer/sma_engine && python3 signal_tracker_v2.py --report

# Update + save CSV report to output/signal_tracking/
cd ~/Developer/sma_engine && python3 signal_tracker_v2.py --csv

# Drop signals older than 30 days (default 60)
cd ~/Developer/sma_engine && python3 signal_tracker_v2.py --prune 30
```

Log: `output/signal_tracking/signal_log.json`
Reports: `output/signal_tracking/performance_YYYY-MM-DD.csv`

---

## Full Pipeline (run in order)

```bash
# 1. Check engine is running
docker logs --tail 20 e47_engine

# 2. Run discovery
docker exec e47_engine python /app/discovery_engine.py --timeframes 1d,1w,1mo

# 3. Run backtest
docker exec e47_engine python /app/run_backtest.py

# 4. Run confluence
docker exec e47_engine python /app/confluence_engine.py --min-score 2 --discovery-tf 1d,1w,1mo

# 5. Run trade engine
docker exec e47_engine python /app/trade_engine.py --min-confidence MEDIUM --discovery-tf 1d,1w,1mo --min-rr 1.5
```

---

## File Inspection

```bash
# How many tickers in candle cache
docker exec e47_engine ls /cache/candle_cache/ | wc -l

# Check muted tickers
cat ~/Developer/sma_engine/muted_tickers.txt

# Check custom tickers
cat ~/Developer/sma_engine/custom_tickers.txt

# Latest snapshot
ls -lt ~/Developer/sma_engine/output/snapshots/ | head -5

# Latest discovery output
ls -lt ~/Developer/sma_engine/output/discovery/ | head -5

# Latest trade suggestions
ls -lt ~/Developer/sma_engine/output/trades/ | head -5
```

---

## Output Locations

| File/Folder | Description |
|---|---|
| `output/signals_current.xlsx` | Latest ranked output, overwrites every cycle |
| `output/xlsx_archive/` | Timestamped copy of every xlsx |
| `output/snapshots/` | CSV snapshot every cycle |
| `output/ranked_log.csv` | Full history of every ranked signal |
| `output/discovery/` | Discovery engine runs |
| `output/backtest_*.csv` | Backtest results |
| `output/confluence/` | Confluence engine runs |
| `output/trades/` | Trade engine suggestions |
| `output/v3/` | V3 engine output (grade/score/entry per signal) |
| `output/signal_tracking/` | Forward return tracker log + CSV reports |
| `output/charts/` | Chart screenshots |
| `output/discovery_charts/` | Discovery chart screenshots |
