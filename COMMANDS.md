# SMA Engine — Command Reference

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
| `output/charts/` | Chart screenshots |
| `output/discovery_charts/` | Discovery chart screenshots |
