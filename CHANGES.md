# Changelog — SMA Engine Updates

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
