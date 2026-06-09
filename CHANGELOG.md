# Changelog

## [Unreleased] — 2026-06-09

### Added — `market_overlay/` (new folder, zero changes to original engine)

All new code lives exclusively in `market_overlay/`. Nothing in the root engine,
`engine.py`, `sheets_writer.py`, or any existing file was modified.

#### `market_overlay/the_system.py`
Implementation of the TraderBJones "The System" strategy on SPY 30m data.
- Indicators: SMA10, SMA50, SMA200, EMA9, EMA21, EMA50
- State: UP (UPRO) when SMA10 > SMA50, DOWN (SPXU) when SMA10 < SMA50
- Entry types: CROSS (SMA10/50 + EMA9/50 confirmation), BOUNCE (extreme oversold + reclaim SMA10)
- Two-step bearish: GO TO CASH when SMA50 not yet sloping down; ENTER SHORT only when SMA50 falling
- Choppy detection: SMA spread < 0.3% → sit on cash
- NASDAQ leading indicator: fetches QQQ 30m, reports state/direction/relative performance vs SPY
- Falls back to yfinance if Webull unavailable

#### `market_overlay/gamma_engine.py`
Zero gamma data for SPX via Tikitrade (free, no API key).
- Scrapes `tikitrade.com/gamma` HTML at 9:30 AM ET
- Parses: Zero Gamma, Call/Put Walls, Max Pain, Expected Move, Vanna Inflection, Basis Shift
- Module-level stale-data cache for resilience between refreshes
- No yfinance options chain — eliminates rate limiting entirely

#### `market_overlay/flashalpha_gex.py`
Single-stock GEX for top triangulated signals via FlashAlpha free tier.
- Free tier budget: 5 calls/day (MAX_TICKERS = 5)
- Daily disk cache (`.gex_cache.json`) — only fetches once per day
- Returns regime badge: positive γ (green) or negative γ (red)
- Silent no-op if FLASHALPHA_API_KEY not set
- **Security**: API key read from `.env` — never hardcoded

#### `market_overlay/index_gex.py`
Index-level zero gamma for QQQ, IWM, DIA computed from yfinance options chains.
- Black-Scholes gamma × OI across up to 4 nearest expiries
- Cumulative gamma zero-crossing with linear interpolation fallback
- Daily cache (`.index_gex_cache.json`) — hits yfinance once per day only
- Spot price hints passed in from The System data to avoid duplicate fetches

#### `market_overlay/sheets_sync.py`
Syncs all engine output categories to Google Sheets (separate tabs).
- Tabs written: Discovery, Confluence, Backtest, Trades, Normalized, V3, Triangulation, Overlay
- Does NOT touch original `sheets_writer.py` or Current/Log tabs
- Reads credentials from `GOOGLE_SHEETS_CREDENTIALS_PATH` and `GOOGLE_SHEET_ID` in `.env`
- Graceful no-op if credentials not configured
- **Security**: credentials path and sheet ID read from `.env` only

#### `market_overlay/overlay.py`
Terminal UI combining all data sources into a live Rich dashboard.
- Panels: The System, Zero Gamma (SPX), Index GEX (QQQ/IWM/DIA), Synthesis, Market Read, Macro, Signals
- Refreshes every 60 seconds
- Calls `sheets_sync.sync_all()` after each refresh cycle
- Writes `latest_snapshot.json` and `dashboard.html` each cycle
- FlashAlpha GEX badges on top 5 triangulated signals

#### `market_overlay/dashboard.html` *(generated — gitignored)*
Self-contained HTML dashboard regenerated each overlay cycle.
- Open in any browser, auto-refreshes every 60 seconds
- Plain-English market narrative (rule-based, zero API cost)
- Shows System state, Zero Gamma regime, index GEX, top signals

### Modified

#### `.env`
- Added: `FLASHALPHA_API_KEY=` — fill in your FlashAlpha free-tier key
- **Security note**: `.env` is gitignored and must never be committed

#### `.gitignore`
- Added entries for `market_overlay/dashboard.html`, `latest_snapshot.json`, `.gex_cache.json`

---

## Security Notes

The following files contain sensitive credentials and are gitignored:

| File | Contains | Risk if leaked |
|------|----------|----------------|
| `.env` | Webull API keys, FlashAlpha API key, Google Sheet ID | API abuse, unauthorized data access |
| `credentials/sheets.json` | Google service account private key | Full write access to your Google Sheets |
| `market_overlay/.gex_cache.json` | Cached GEX data (no credentials) | Low — market data only |
| `market_overlay/latest_snapshot.json` | Live market snapshot (no credentials) | Low — market data only |

**If you ever accidentally commit `.env` or `credentials/`:**
1. Rotate all keys immediately (Webull portal, FlashAlpha dashboard, Google Cloud console)
2. Use `git filter-branch` or BFG Repo Cleaner to purge from history
3. Force-push the cleaned history

---

## Architecture

```
sma_engine/                    ← original engine (untouched)
├── engine.py
├── sheets_writer.py
├── .env                       ← gitignored, contains secrets
├── credentials/               ← gitignored, contains Google service account key
└── market_overlay/            ← all new code lives here
    ├── overlay.py             ← main entry point: python3 overlay.py
    ├── the_system.py          ← TraderBJones SPY 30m logic
    ├── gamma_engine.py        ← Tikitrade SPX zero gamma scraper
    ├── flashalpha_gex.py      ← FlashAlpha single-stock GEX
    ├── index_gex.py           ← QQQ/IWM/DIA zero gamma (yfinance)
    └── sheets_sync.py         ← Google Sheets multi-tab sync
```
