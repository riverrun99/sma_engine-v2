# Changelog — SMA Engine Updates

---

## 2026-06-10 — Systems Panel, Forward Return Tracker, System Logic Doc

All new code in new files only. Zero changes to original engine files.

### `market_overlay/systems_panel.py` — New File
Live state display for all TraderBJones index systems, pulled via yfinance (5-min cache).
- **SPX 30m [10/50/200]** — MA10 vs MA50, vehicle UPRO/SPXU
- **IXIC 30m [20/100/250]** — MA20 vs MA100, vehicle TQQQ/SQQQ
- **DJI 15m [90/300]** — active operations timeframe, vehicle UDOW/SDOW
- **DJI 1H [90/300/900]** — structural confirmation timeframe
- **IWM 2H [16/250/500]** — 1h bars resampled to 2h, MA16 vs MA250
- **IWV 2H [16/250/500]** — Russell 3000 broad market read
- **SOX 30m [16/256/512]** — Base-2/NVDA outfit, vehicles SOXL/SOXS
- **VIX 1H [26/422]** — MA26 > MA422 flags HIGH-VOL regime active
- **SVIX 1D [116/211/422]** — cluster convergence ~20 = structural support
- Under HIGH-VOL, panel flags the key MA level each system shifts to (close-based rule)
- Alignment summary: ALL POSITIVE / ALL NEGATIVE / MIXED with fill-the-bucket note
- Standalone: `python3 market_overlay/systems_panel.py`

### `market_overlay/overlay.py` — Updated
- Imported `systems_panel` module
- Added `size=20` layout row between Narrative and bottom panels
- `fetch_all()` calls `systems_panel.fetch_all_systems()` each cycle (cached, non-blocking)
- `build_layout()` accepts and renders `systems_data` parameter

### `SYSTEM_LOGIC.md` — New File
Complete reference document for the TraderBJones system logic framework.
- Three primary systems: SPX [10/50/200], NASDAQ [20/100/250], DJI [30/60/90/300/600/900]
- Heightened volatility rules: MA cross → candle close vs key MA during rising VIX
- VIX confirmation framework [26/52/116/211/422/844] with 1m/10m/1H timeframe roles
- SVIX daily cluster at ~20 as structural support indicator
- Major level confluence: SPX MA200 + NASDAQ MA250 + DJI MA900
- Crash condition: only when SPX system negative (MA10 < MA50)
- Broader market extensions: IWM/IWV 2H [16/31/63/125/250/500]
- Fill-the-bucket dynamic: cross-system capital rotation

### `signal_tracker_v2.py` — New File
Standalone forward return tracker. Read-only from existing output. Writes only to `output/signal_tracking/`.
- Ingests top-N signals from latest v3 xlsx (grade, score, entry price) + high-confidence trades
- Logs first detection only per ticker/timeframe/outfit — never overwrites
- Fills forward closing prices at **+1d / +3d / +5d / +10d / +20d** trading days via yfinance
- Once a window is filled it is frozen — never re-fetched
- Prunes signals older than N days (default 60)
- Terminal report: per-signal forward returns with win rate and average by window
- Usage: `python3 signal_tracker_v2.py [--top N] [--report] [--csv] [--prune N]`
- Log: `output/signal_tracking/signal_log.json`

### `muted_tickers.txt` — Updated
- Added CZR (flat, no setup — 2026-06-10)

### `custom_tickers.txt` — Updated
- Added UUUU (uranium miner)

---

## 2026-06-09 — Market Overlay, Zero Gamma, The System, Live Dashboard

All new code lives in `market_overlay/`. Zero changes to original engine files.

### `market_overlay/the_system.py` — New File
TraderBJones "The System" implementation on SPY 30m data.
- Indicators: SMA10, SMA50, SMA200, EMA9, EMA21, EMA50
- State: UP (UPRO) when SMA10 > SMA50, DOWN (SPXU) otherwise
- Entry types: CROSS (SMA10/50 + EMA9/50 confirmation), BOUNCE (extreme oversold + reclaim SMA10)
- Two-step bearish logic: GO TO CASH when SMA50 not yet sloping down; ENTER SHORT when SMA50 falling
- Choppy detection: SMA10/50 spread < 0.3% → sit on cash
- NASDAQ leading indicator: fetches QQQ 30m, reports state/direction/relative performance vs SPY
- Fallback to yfinance if Webull unavailable

### `market_overlay/gamma_engine.py` — New File
SPX zero gamma via Tikitrade free data (no API key, no rate limiting).
- Scrapes `tikitrade.com/gamma` HTML at 9:30 AM ET
- Parses: Zero Gamma, Call/Put Walls, Max Pain, Expected Move, Vanna Inflection, Basis Shift
- Module-level stale-data cache for resilience between refreshes
- Replaced yfinance options chain approach entirely — eliminates rate limiting

### `market_overlay/flashalpha_gex.py` — New File
Single-stock GEX for top triangulated signals via FlashAlpha free tier.
- Free tier: 5 calls/day. Daily disk cache (`.gex_cache.json`) preserves budget.
- Returns regime badge: positive γ or negative γ per ticker
- Silent no-op if `FLASHALPHA_API_KEY` not set in `.env`

### `market_overlay/index_gex.py` — New File
Index-level zero gamma for QQQ, IWM, DIA from yfinance options chains.
- Black-Scholes gamma × OI, cumulative zero-crossing with linear interpolation
- Daily cache — hits yfinance once per day only
- Spot price hints passed from The System to avoid duplicate fetches

### `market_overlay/sheets_sync.py` — New File
Syncs all engine output categories to Google Sheets.
- Tabs written: Discovery, Confluence, Backtest, Trades, Normalized, V3, Triangulation, Overlay
- Does NOT touch original `sheets_writer.py` or Current/Log tabs
- Reads from `GOOGLE_SHEETS_CREDENTIALS_PATH` and `GOOGLE_SHEET_ID` in `.env`
- Graceful no-op if credentials not configured
- Standalone: `python3 market_overlay/sheets_sync.py`

### `market_overlay/overlay.py` — New File
Terminal UI combining all data sources into a live Rich dashboard.
- Panels: The System, Zero Gamma (SPX), Index GEX (QQQ/IWM/DIA), Synthesis, Market Read, Macro, Signals
- Plain-English "Market Read" panel — rule-based narrative, zero API cost, updates every refresh
- Writes `latest_snapshot.json` and `dashboard.html` each cycle
- Syncs all output categories to Google Sheets each cycle
- FlashAlpha GEX badges on top 5 triangulated signals
- Refresh: every 60 seconds

### `market_overlay/dashboard.html` — Generated File (gitignored)
Self-contained HTML dashboard regenerated each overlay cycle.
- Open in any browser — auto-refreshes every 60 seconds
- Plain-English narrative, System state, Zero Gamma, index GEX, top signals
- No API calls, no external dependencies

### `.env` — Updated
- Added `FLASHALPHA_API_KEY=` entry for FlashAlpha free-tier key

### `.gitignore` — Updated
- Added exclusions for `market_overlay/dashboard.html`, `latest_snapshot.json`, `.gex_cache.json`

### `CHANGELOG.md` — New File
Full architectural overview with security notes. See `CHANGELOG.md`.

---

## 2026-06-04 — Performance & Stability Overhaul

### engine.py

**Multiprocessing scan (GIL bypass)**
- Replaced `threading.Thread` workers in `scan()` with `multiprocessing.Pool`
- Each worker gets its own process with its own GIL — true parallel CPU execution
- Cache is sliced per worker (only tickers in that chunk) to minimize pickling overhead
- Result: scan went from single-core (~6 hrs) to multi-core parallel

**Vectorized `detect_hits`**
- Replaced Python `for i in range(start, len(df))` inner loop with numpy array comparisons
- `np.where(arr == sma_w)` evaluates all 999 candles simultaneously in C
- Identical results, ~10-50x faster per combo
- Scan time dropped from ~6 hours → ~3 minutes for 467k combinations

**Worker SIGTERM isolation**
- Added `_scan_worker_init()` initializer to `multiprocessing.Pool`
- Workers inherit parent signal handlers via fork — caused double SIGTERM logging
- Workers now ignore SIGTERM/SIGINT; only parent handles shutdown

**Progress logging**
- Replaced thread-based progress logger with `imap_unordered`
- Each worker logs on completion: `scan N/N workers done (X%) — Xs elapsed, ~Ys remaining`

**Module-level worker function**
- Added `_scan_worker_fn()` at module level (required for multiprocessing pickling)

---

### daemon.py

**Output write order — local files first**
- Previously: InfluxDB writes → local file writes
- Now: signal/top_n computed → local files written → InfluxDB writes
- InfluxDB timeouts no longer block `signals_current.xlsx` and `signals_log.csv`

**Cumulative deciseconds query — non-fatal**
- Wrapped `query_cumulative_deciseconds()` in try/except
- Timeout returns empty dict; engine continues with current-cycle deciseconds only

**InfluxDB writes wrapped**
- All InfluxDB write calls wrapped in single try/except block
- InfluxDB failure is logged as warning, never crashes the cycle

---

### persistence.py

**InfluxDB client timeout**
- Increased from default (~10s) to 60,000ms (60 seconds)
- Fixes `query_cumulative_deciseconds` timeout on larger datasets

---

### docker-compose.yml

**Stop grace period**
- Added `stop_grace_period: 120s` to engine service
- Engine now has 2 minutes to complete output writing after SIGTERM

**Missing env vars added**
- Added pass-through for: `ENGINE_SCAN_WORKERS`, `ENGINE_REFRESH_BARS`, `ENGINE_MIN_TF_MINUTES`, `HIT_MODE`, `HIT_TOLERANCE`, `STREAM_ENABLED`, `STREAM_TIMEFRAMES`, `ENGINE_BACKTEST_EVERY`, `TERMINAL_UI`

---

### .env

| Setting | Before | After | Reason |
|---|---|---|---|
| `ENGINE_LOOKBACK` | `130` | `999` | Max history per cycle |
| `ENGINE_SCAN_WORKERS` | `12` (not passed) | `3` | Memory safety with lookback=999 |
| `ENGINE_TIMEFRAMES` | `1m,5m,...,1mo` | `5m,15m,...,1mo` | 1m removed (no signal value) |
| `ENGINE_MIN_TF_MINUTES` | `15` | `5` | Allow 5m signals in rankings |
| `ENGINE_TOP_N` | `50` | `2000` | Full leaderboard |
| `Docker CPUs` | `4` | `8` | More cores for scan workers |

---

### muted_tickers.txt

Added ~220 tickers across:
- Small/regional banks, biotech small/mid, bonds/rates ETFs
- Factor/smart-beta ETFs, micro-cap biotech, small REITs
- Small financials, low-signal healthcare, small industrials
- Low-signal tech/software, misc small caps

**Active tickers: 1,266** (down from 1,489)

---

### Results

- Scan time: **~3-4 minutes** (was 6+ hours)
- Memory stable at ~5-6 GB with 3 workers + lookback=999
- Output writes reliably every cycle
- InfluxDB history: 183,393 decisecond keys loaded successfully
- Top signal (2026-06-04): **MKC / 1mo / 33 Outfit / entry 51.82 / 1,067 hits**

---

## 2026-06-02 (Session 2)

### Discovery Engine — New File: `discovery_engine.py`
- Built a second, parallel engine for early signal detection
- Scans candle cache for tickers where price touches a significant SMA for the **first time in N bars** (default: 100 bars absence)
- Surfaces structural "first touch" setups before they accumulate enough hits to rank in the main engine
- Example signal type: LAC touching MA420 at $3.96 — the structural low the main engine missed
- All SMA periods from all outfits scanned; results sorted by SMA period (longest first)
- Flags direction: `from_above` (support tap) vs `from_below` (resistance tap)
- Output: terminal print + timestamped CSV saved to `output/discovery/discovery_YYYY-MM-DD_HH-MM-SS.csv`
- Does NOT modify any existing engine files
- Usage: `docker exec e47_engine python /app/discovery_engine.py`
- Options: `--absence N`, `--min-period N`, `--timeframes 1d,1w`, `--tolerance 0.002`
- First run surfaced 84 unique daily/weekly/monthly signals — only 2 overlapped with main ranker output

### Engine Config Changes
- `ENGINE_LOOKBACK` changed from 999 → 50 (scores only the 50 most recent bars for hits; SMA calculations still use full history)
- `RYCEY` (Rolls-Royce ADR) added to `custom_tickers.txt` — UK small modular reactor developer, nuclear theme

### Muted Tickers (additional)
- KBWY, JFIN, OCFC, FULT, RSKD, KITT, GFAI, WTIU, WTI, APLD — muted as noise or parabolic/extended
- MGYR, HBAN, HBANM, MFIN, VMBS, OCFC, AGG, INTL, PINE, FULT — additional noise mutes

### Custom Tickers — Major Universe Expansion
Added 200+ tickers across 8 batches covering:
- **Watchlist ETFs:** NAT, OWL, VIXM, VIXY, ZIM, SCO, FRO, TBT, FAZ, EXC, BAC, BNO, KIE, XLE, ZROZ, WFC, TLT, SHEL, BX, JPM, GLD, YCL, UUP, TMF, XLF, FXY, MDLZ, SHY, JNK, LQD, SPY, GUSH, SVXY, BND, TQQQ, TIP, PLTR, LABU, COIN, BITO, MSTU, JBLU, SOXS, MARA, KOLD, SDOW, SPDN, CPB, CWEB, KWEB, NVDD, SCHD
- **Discovery batch:** MZZ, DRN, ICAGY, SMR, TSLL, SILJ, AAPU, ENPH, ORCX, IYR, FAS, MSTR, ORCL, TSLA, BTBT, MYY, PSLV, UMDD, CONI, ICSH, BABA, Q, AAPL, ARM
- **Energy/commodities:** SOXL, XOP, UCO, USO, BOIL, ETR, GEV, SO, DUK, GEVO, RIVN, VNQ, VZ, JEPQ, SVOL, T, AGG, JEPI, PFE, MSOS, AI, SOFI, LYFT, NKE
- **Energy majors/midstream:** EP, XOM, OXY, MPLX, HESM, CVX, COP, IYE, AM, EOG, WMB, VST, ERX, ENB, SLB, OKLO, LIN, PAA
- **Industrials/automation:** DE, VIS, ALB, SUPL, DHR, HON, EMR, RTX, ROK, TER, CGNX, ZBH, SIEGY, SBGSY, DNZOY, SEKEY, KNNGF, YASKY, KYCCF
- **Mega cap tech/semis:** NVDA, MSFT, GOOG, META, AMZN, AAPL, TSM, AVGO, ASML, AMD, KLAC, QCOM, MU, MCHP, ADI, TXN, NXPI, SMCI, MRVL, HPE, DELL, SNOW, RBLX, RDDT, ADBE, NFLX, CRWV, NBIS
- **Healthcare/pharma:** LLY, UNH, ABBV, MRK, AMGN, REGN, MRNA, GILD, AZN, SYK, ABT, CI, TMO, HALO, MDT, DHR, VHT, IYH, HIMS, CVS
- **Financials:** GS, MS, JPM, BAC, WFC, C, USB, PNC, RF, BLK, BRK-B, AXP, V, MA, PYPL, SCHW, COF, IBKR, HSBC, SAN, RY, MUFG, BNS, TFC, BCS, NU, NDAQ, TOST, ARKF, FIS, XYZ, LDI, IVZ, KBE, IAK, SYF, VFH
- **China/EM:** TCEHY, DIDIY, JD, BIDU, MCHI, FXI, PDD, EDU, PONY, BEKE, CWEB, KWEB
- **Cannabis/psychedelics:** MSOS, DFTX, CMPS, ATAI, ENVB, GTBIF, GRWG, GHRS, LFLH
- **Real estate:** VNQI, REET, URE, VNQ, ZG, COMP, IYR
- **Nuclear cluster:** SMR, SMUP, SMU, OKLO, RYCEY (full SMR discovery stack)
- **Japanese/global:** FANUY, NTDOF, KONMY, NTDOY, YASKY, KYCCF, DNZOY, SEKEY, SIEGY, SBGSY, ENGIY, IBDRY, SOBKY, MUFG

### Output Folders
- `output/charts/` — created for daily scan chart screenshots
- `output/discovery_charts/` — created for discovery engine chart screenshots

### Key Analysis & Observations (2026-06-02)
- **SMR +20%** confirmed nuclear/power demand theme; URAA + VSTL flagged by main engine as downstream plays
- **Discovery engine first run:** Surfaced BWXT (MA250 tap, nuclear), UTSL (MA464 bounce +5.69%), ISRG (weekly MA200), ETSY (MA884), PENN (MA884), RIOT/MARA (crypto miners)
- **Top setups identified:** LAC (breakout, MA420 base), EOLS (SMA coil), VSTL (nuclear/Vistra), BWXT (MA250 tap), UTSL (MA464 bounce), SNAP (coiled), RIG (pullback to support)
- **Bear ETFs kept in universe** — confirmed valid as market sentiment overlay, not noise
- Only 2 of 84 discovery signals overlapped with main ranker — engines are surfacing complementary, non-overlapping information

## 2026-06-01 / 2026-06-02

### Muted Tickers
- Added 150+ tickers to `muted_tickers.txt` across several sessions
- Categories muted: bond ETFs (HYG, BNDW, DBA, SOYB), tiny REITs (PDM, DEI, FBRT, NTST, OLP, GOOD, CHCT, SQFT, BRT, LAND), biotech microcaps (BCAB, FATE, CODX, GNSS, SABS, SPRO, GNTA, NKTX, ARDX, ACRS, TCRT, SVRA, TBPH, ELAB, CLAR), small community banks (RVSB, FNWB, ALEC, PROV, KELYA, SHBI), China/EM small caps (NIO, WB, SEA, ITUB, EWH), noise ETFs (SZK, MSFD, SCC, SMDD, METD, AMDD, CMTG, MSFO, SPDN, TRTX, GGLS, AAPD), illiquid micro caps (IVDA, XOMA, EVMO, SPOK, ZNTL, TTEC)

### Custom Tickers
- Added 88 tickers to `custom_tickers.txt` from personal watchlists
- Includes: leveraged ETFs (TYO, UBT, UPW, UBOT, URAA, WTIU, XOMX, TSMX), watchlist names (AMC, KITT, GFAI, APLD, SYM, VSTL, WTI), reinsurance ADRs (SSREY, HVRRY), composites/aerospace (HXL, TDY), nuclear/energy (CCJ, CEG, LEU, BWXT, PEG), and many others

### Engine Config
- `ENGINE_TOP_N` set to 2000 (was 50)
- `ENGINE_TIMEFRAMES` confirmed: 1m, 5m, 15m, 30m, 1h, 2h, 4h, 1d, 1w, 1mo
- `ENGINE_LOOKBACK` = 130 bars
- `ENGINE_BACKTEST_EVERY` = 288 cycles (auto-backtest every 24 hours)

### local_writer.py — New Features
- **Performance tab** in `signals_current.xlsx`: tracks price change since each ticker's first appearance in ranked output. Persisted via `output/price_tracker.json`.
- **Ranked log** (`output/ranked_log.csv`): append-only, one row per ranked entry per cycle. Full historical record of every ranked signal.
- **Snapshots** (`output/snapshots/snapshot_YYYY-MM-DD_HH-MM-SS.csv`): one timestamped file per scan cycle listing every unique ticker with timeframe, outfit, hits, convergence, score.

### backtest.py
- Fixed `ZeroDivisionError` in `evaluate_signal` when candle open price is zero

### run_backtest.py — New File
- Standalone backtest runner: reads current `signals_current.xlsx`, loads candle cache, runs walk-forward backtest on top 100 signals
- Prints summary table: ticker, timeframe, outfit (SMA periods), trades, win rate, avg return, Sharpe
- Saves timestamped CSV to `output/backtest_YYYY-MM-DD_HH-MM-SS.csv`
- Usage: `docker exec e47_engine python /app/run_backtest.py`
- Options: `--method cpcv`, `--horizon N`, `--top N`

### daemon.py
- Wired `ENGINE_BACKTEST_EVERY` env var to auto-run backtest every N cycles
- Added `backtest_every` logic in main loop

### README.md
- Updated placeholder URLs from YOUR_USERNAME to riverrun99
