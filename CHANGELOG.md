# Changelog

## [Unreleased] — 2026-06-11

### Added

#### `market_overlay/systems_panel.py` (new file)
Full multi-system index state panel for the overlay terminal UI.
- **SPX 30m [10/50/200]** — POSITIVE when MA10 > MA50. Proxy: SPY. Tracks spread %, MA200 structural level, vehicle (UPRO/SPXU).
- **IXIC 20m [20/100/250]** — POSITIVE when MA20 > MA100. Proxy: QQQ. Cascading data source: Webull M20 → Webull M30 → yfinance ^IXIC 30m → yfinance QQQ 30m. Source displayed in TF column (·yf in yellow when falling back to yfinance — signals possible MA value mismatch).
- **DJI 15m [90/300]** — Active ops timeframe. Proxy: DIA.
- **DJI 1H [90/300/900]** — Structural confirmation timeframe. Proxy: DIA.
- **IWM 2H [16/250/500]** — Russell 2000 broad market. Webull native 2H; yfinance 1H → resample fallback.
- **IWV 2H [16/250/500]** — Russell 3000 confirmation. Same fetch path as IWM.
- **SOX 30m [16/256/512]** — Semiconductor regime. Proxy: SMH.
- **VIX 1H [26/422]** — HIGH-VOL regime detection. MA26 > MA422 = regime active. Current price from CBOE direct API (see below). History from yfinance for MA calculation.
- **SVIX 1D [116/211/422]** — Structural vol support. Cluster avg of three MAs (~20); HOLDING/LOST shown.
- Under HIGH-VOL regime, state column appends `/MAxx` (the active close-rule MA) in yellow for each system.
- Alignment summary footer: ALL POSITIVE / ALL NEGATIVE / mixed count with plain-English institutional read.
- 60-second cache per instrument; VIX failures not cached (retry every cycle).

### Fixed

#### `market_overlay/systems_panel.py`
- **VIX now always shows current price** — replaced yfinance-only VIX fetch with two-stage approach:
  1. CBOE direct API (`https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json`) for current price — 15-min delayed, no key, always reliable.
  2. yfinance `^VIX` 1H history retained for MA26/MA422 calculation.
  - If yfinance history fails, row shows CBOE price with `● loading` (MAs pending) instead of `⚠ no data`.
  - If both fail, row shows `⚠ no data` and retries next cycle (not cached).
- **`_sep()` column count** — fixed from 7 to 8 empty strings to match table column count.
- **VIX state cell width** — `regime_short = "HIGH-VOL"` (8 chars) used in table cell instead of full `"HIGH-VOL ACTIVE"` (15 chars) which overflowed the State column (width=14).

#### `market_overlay/overlay.py`
- **Synthesis/Action conflict on EXIT SHORT** — when signal is EXIT SHORT or EXIT LONG, the synthesis note now correctly overrides the generic alignment note: "Price crossed above SMA50 — exit SPXU, stand aside. Await SMA10/50 bullish cross for UPRO entry."
- **Market Read NASDAQ language** — removed "as of close" phrasing during market hours; now reads "NASDAQ is confirming: QQQ [state] (MA structure), trading [direction] by X% today."
- **Zero Gamma panel staleness** — `build_index_gex_panel()` now accepts `live_prices` dict; subtitle shows "daily cache · HH:MM UTC" with `⚠ Xh Ym old` warning when cache is >60 min stale.
- **IWM/DIA spot price in Zero Gamma panel** — when GEX computation fails/unavailable, live spot price from systems panel is now displayed ("Spot: 287.75  (GEX unavailable)") instead of nothing. Uses `Text.append(..., style=...)` correctly (not inline Rich markup which renders as literal text).
- **`build_layout()` live price extraction** — extracts QQQ/IWM/DIA close prices from systems data and passes them to `build_index_gex_panel()` each cycle.

---

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
