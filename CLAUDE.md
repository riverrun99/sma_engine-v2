# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This System Does

SMA Outfit Detection Engine — scans ~1,700 tickers across 10 timeframes and 41 SMA "outfits" (period sets) to detect where price is touching moving averages. Signals are ranked by **cumulative deciseconds** (time price has spent at a level, accumulated over 7 days in InfluxDB) multiplied by a convergence multiplier (0–4 layers). This is a signal detection system, not a trading bot — there is no execution, no account integration.

## Commands

### Running Tests
```bash
python3 tests.py
```
The test suite (`tests.py`) uses a custom `check()` helper with global `PASSED`/`FAILED`/`FAILURES` counters — no pytest or unittest framework. There are no other test commands.

### Running the Engine (Docker)
```bash
./start.sh                  # full stack: InfluxDB + Grafana + engine
./start.sh engines          # engine containers only (no pipeline)
./start.sh pipeline         # analysis pipeline only (discovery → backtest → confluence → trade)
./start.sh overlay          # market_overlay/ only
./stop.sh                   # stop all containers
```

### Running Without Docker
```bash
python3 daemon.py            # main loop (requires .env populated)
python3 run_pipeline.py      # single cycle runner
```

### Analysis Tools (run inside Docker container)
```bash
docker exec -it engine python3 /app/discovery_engine.py [--timeframes 30m,1d] [--min-period 100] [--absence 20]
docker exec -it engine python3 /app/run_backtest.py [--top 10] [--horizon 5]
docker exec -it engine python3 /app/confluence_engine.py [--min-score 2] [--discovery-tf 1d] [--min-sharpe 1.5]
docker exec -it engine python3 /app/trade_engine.py [--min-confidence MEDIUM] [--min-rr 1.5]
```

### Ticker Management
```bash
python3 mute.py EUO ERNA DUG        # mute tickers (hot-reloads, no restart needed)
python3 mute.py --unmute EUO ERNA   # unmute
```

### Market Overlay (standalone)
```bash
cd market_overlay && python3 overlay.py
```

## Architecture

### Data Flow
```
Webull OpenAPI (or MockClient)
    → async_fetch.py   — concurrent fetch, token-bucket rate limiting (60 req/min)
    → engine.py        — SMA computation, hit detection, hash map aggregation, ranking
    → daemon.py        — orchestration loop, regime fitting, hot-reload filters
        ├→ persistence.py    — InfluxDB writes (fail-open, never blocks engine)
        ├→ local_writer.py   — output/signals_current.xlsx + output/signals_log.csv
        ├→ sheets_writer.py  — optional Google Sheets (Current/Log tabs)
        └→ terminal_ui.py    — Rich terminal dashboard
```

### Key Abstractions

**engine.py** is the core — 2,311 lines. The central data structure is a hash map keyed by `(ticker, timeframe, outfit_id)`. Each entry (`HashMapEntry`) accumulates hit counts and **deciseconds per SMA period** across time. The period with the highest cumulative deciseconds becomes the `key_variable` for that entry.

**Outfits** — 41 named SMA period sets defined as `OUTFITS` in `engine.py`. Example: outfit `[25,50,100,200,400,800]`. Each outfit is an independent "system" that detects hits.

**Scoring** — Two-stage:
1. `rank_entries()` computes raw score from freshness-weighted deciseconds
2. `detect_convergence()` adds 0–4 bonus layers: `ohlc_detection`, `time_series`, `parm_price`, `candle_close`
3. Final gated score = `deciseconds × (1 + convergence_layers)`

**Cumulative persistence** — deciseconds from 7-day InfluxDB history are added to current-cycle values in `daemon.py` before ranking. This means a signal that has been "at level" for days outscores a fresh touch.

**Regime detection** (`regime.py`) — 3-state Gaussian HMM on SPY + UVXY + SMH features. Fitted every `ENGINE_REGIME_EVERY` cycles. Results stored in InfluxDB and displayed in terminal/Grafana.

**DataClient interface** — `engine.py` defines an abstract `DataClient` with `fetch_bars()`. `WebullClient` and `MockClient` both implement it. Switch via `ENGINE_SOURCE=mock` in `.env`.

### Analysis Engines (independent, read-only)
- **discovery_engine.py** — offline scan of candle cache for first-touch SMA events
- **backtest.py** — walk-forward + purged combinatorial CV; measures forward returns
- **confluence_engine.py** — cross-references discovery + backtest + main engine leaderboard
- **trade_engine.py** — ATR-based stops, R/R calculation, paper trade suggestions with confidence levels

### Market Overlay (`market_overlay/`)
Added 2026-06-09 as a completely isolated module — zero modifications to any original engine files. Combines:
- `the_system.py` — TraderBJones SPY 30m system (SMA10/50/200 + EMA9/21/50)
- `gamma_engine.py` — SPX zero gamma scraped from Tikitrade
- `index_gex.py` — QQQ/IWM/DIA gamma via yfinance options chains (daily cache)
- `flashalpha_gex.py` — single-stock GEX via FlashAlpha free tier (5 calls/day budget)
- `overlay.py` — terminal UI combining all sources; also writes `latest_snapshot.json` + `dashboard.html`

### Normalized Variant
`engine_normalized.py` / `daemon_normalized.py` / `docker-compose.normalized.yml` — parallel variant with normalized ticker filtering. Configured via `normalized_tickers.txt` / `muted_normalized.txt`.

### V3 Staging (`_v3_staging/`)
Next-gen engine in active development. Has its own `engine_v3.py`, `conditions_v3.py`, `scoring_v3.py`, and `docker-compose.v3.yml`. Do not modify without understanding the V3 scoring changes.

## Configuration

All configuration is via `.env` (see `.env.example` for all variables). Key ones:

| Variable | Values | Effect |
|---|---|---|
| `ENGINE_SOURCE` | `mock` / `webull` | Switch to deterministic mock data for testing |
| `ENGINE_UNIVERSE` | `tier1` / `tier2` / `all` | 143 ETFs / +40 mega-caps / ~1,705 tickers |
| `HIT_MODE` | `exact` / `wick` / `both` | Whether to detect close-only or wick touches |
| `TERMINAL_UI` | `true` / `false` | Enable/disable Rich terminal dashboard |
| `STREAM_ENABLED` | `true` / `false` | Sub-minute tick streaming via MQTT |

## Hot-Reload Files

These files are re-read every cycle without restarting the engine:
- `muted_tickers.txt` — tickers to exclude from output
- `custom_tickers.txt` — tickers to add beyond the configured universe
- `normalized_tickers.txt` / `muted_normalized.txt` — for normalized variant

## Output Files

| File | Description |
|---|---|
| `output/signals_current.xlsx` | Overwritten each cycle (top signal + leaderboard + systems + regime) |
| `output/signals_log.csv` | Append-only, one row per cycle |
| `output/ranked_log.csv` | Append-only, one row per ranked entry per cycle |
| `output/snapshots/` | Timestamped CSV snapshots |
| `output/discovery/` | Discovery engine run outputs |

## Infrastructure

- **InfluxDB 2.7** at port 8086 — 90-day auto-expiry retention policy. 5 measurements: `candles`, `hits`, `signals`, `system_states`, `regimes`. Named Docker volume persists across restarts.
- **Grafana 11.2.0** at port 3001 — auto-provisioned from `grafana/provisioning/`. Default credentials: `admin` / `element47`. Three dashboards: Live Signal, Hit Heatmap, Regime & Systems.
- **Candle cache** — gzip-pickled per-ticker DataFrames at `/cache/candle_cache/*.pkl.gz` inside the container (mounted Docker volume). Loaded at daemon startup, saved after each cycle.

## Webull API

`WebullClient` in `engine.py` uses HMAC-SHA256 request signing. Rate limiting is a token-bucket (`async_fetch.py`) targeting 60 req/min. The full-universe scan (~18k ticker/timeframe pairs) takes 25–50 min dominated by this limit. The optional `stream_client.py` subscribes to sub-minute ticks via Webull's `DataStreamingClient` SDK if installed.
