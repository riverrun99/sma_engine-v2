# SMA Outfit Detection Engine

Continuously scans ~1,700 tickers across 10 timeframes and 41 SMA outfits.
For every combination it detects price-to-SMA contact, scores each level by
the cumulative time price has spent there (measured in deciseconds), and ranks
signals by multi-layer convergence. The methodology is based on the time-series
rank analysis framework developed by Raul (@UnfairMarket).

---

## How it works

### SMA outfits

An "outfit" is a specific set of simple moving average periods — for example
[16, 32, 64, 128, 256, 512] (the Base-2 outfit) or [25, 50, 100, 200, 400, 800]
(the standard sequence). The engine tests 41 different outfits simultaneously.
For each outfit it computes all the SMAs on a price series and checks whether
any candle makes contact with one of those SMA values. A contact is called a hit.

Hit detection has three modes:

- **exact** — open, high, low, or close must equal the SMA value at 2 decimal
  places. Most precise. Flags clear, unambiguous confluences only.
- **wick** — the SMA must fall within the candle's full [low, high] range, or
  within a configurable dollar tolerance of open or close.
- **both** — both exact and wick checks run. Maximum sensitivity.

### Decisecond scoring

The core ranking unit is the **decisecond** — one-tenth of a second. Every hit
is weighted by how long its candle lasts:

| Timeframe | Deciseconds per candle |
|---|---|
| 1 minute | 600 |
| 5 minutes | 3,000 |
| 15 minutes | 9,000 |
| 1 hour | 36,000 |
| 4 hours | 144,000 |
| 1 day | 864,000 |
| 1 week | 6,048,000 |
| 1 month | 25,920,000 |

A single daily candle hit outscores 1,440 one-minute hits. This means the
leaderboard reflects where price has genuinely spent time at a level, not which
ticker produces the most intraday noise. Short timeframes (1m, 5m) are filtered
out of rankings by default via `ENGINE_MIN_TF_MINUTES`.

### Key variable

Within each outfit, the SMA period that has accumulated the most deciseconds is
the **key variable**. It is the specific MA level that price keeps returning to.
Entry price is pinned to the key variable's current SMA value. Exit logic is a
close below (long) or above (short) that level.

### Cumulative persistence

Deciseconds accumulate across cycles and sessions in InfluxDB. A level visited
repeatedly over several days scores higher than one hit heavily in a single
cycle. The query window defaults to 7 days. This is how Raul's XLE signal built
from March 24–30: the same SMA level accumulated deciseconds across hundreds of
cycles before the convergence score made it the clear top signal.

The first week of engine operation is a warm-up period. Decisecond history grows
with each cycle. Convergence scores become more reliable as history accumulates.

### Four-layer convergence

Each signal candidate is evaluated on four independent detection layers:

| Layer | Label | Description |
|---|---|---|
| (a) | `ohlc_detection` | Active hit exists on the primary timeframe |
| (b) | `time_series` | This level's decisecond score is disproportionately high relative to sibling outfits on the same ticker/timeframe |
| (c) | `parm_price` | The key variable's SMA price matches a level appearing in multiple timeframes for the same ticker |
| (d) | `candle_close` | The most recent candle closed at or near the key variable level |

The convergence score is reported as `n/4`. A 4/4 signal has all four layers
confirmed simultaneously.

### Multi-condition gating

The final ranking uses a gated score rather than raw deciseconds:

```
gated_score = decisecond_score × (1 + convergence_layers)
```

A signal with 3/4 convergence and a moderate decisecond score beats a signal
with 1/4 convergence and a higher raw score. This prevents a numerically large
but unconfirmed level from overriding a well-confirmed weaker one.

The top 50 candidates by raw decisecond score are re-ranked by gated score. The
winner is the cycle's output signal.

### Hash map structure

The engine maintains a hash map keyed by `[ticker / timeframe / outfit]`. Every
hit updates the entry: incrementing hit count, adding deciseconds to the running
total for that SMA period, and updating the key variable. This is the same
structure Raul describes in his methodology — each cell accumulates time-series
evidence independently.

### Market regime

In parallel, the engine fits a Hidden Markov Model (HMM) to SPY, UVXY, and
SMH daily data. The HMM identifies which of 3 hidden states the market is
currently in — roughly bull, bear, or transitional. This regime label is shown
alongside signals as context.

### System monitor

8 major market systems are evaluated each cycle by comparing fast and slow SMAs:

| System | Proxy | Timeframe | Positive when |
|---|---|---|---|
| S&P 500 | SPY | 30m | MA10 > MA50 |
| NASDAQ | QQQ | 30m | MA20 > MA100 |
| Dow Jones | DIA | 1h | MA90 > MA300 |
| VIX | UVXY | 1h | MA52 < MA106 |
| SVIX | SVXY | 1h | MA52 > MA106 |
| Russell 2000 | IWM | 1d | MA10 > MA50 |
| Russell 3000 | IWV | 1d | MA19 > MA600 |
| Semiconductors | SMH | 1d | MA50 > MA100 |

All three major indices positive = bull structure. VIX negative + SVIX positive
= falling volatility. System states appear in the terminal dashboard, Grafana,
and alongside every signal.

### Output

Each cycle writes:
- The top-ranked signal with: ticker, timeframe, outfit, entry price (key
  variable SMA), hit count, decisecond score, convergence score (n/4), key
  variable period, and all four convergence layer states
- A ranked leaderboard of the top N combinations (default 500)
- Local files: `output/signals_current.xlsx` (snapshot) and
  `output/signals_log.csv` (running history)
- All data to InfluxDB for Grafana dashboards
- Optionally, a Google Sheet for phone access

---

## What you need

- A computer (Mac, Windows, or Linux) that stays on while the engine runs
- Docker Desktop installed (free)
- A Webull developer account for market data (free, 1-2 business days to approve)
- About 30 minutes for first-time setup

---

## Quick start

If you already have Docker and Webull API credentials:

```bash
git clone https://github.com/riverrun99/sma_engine
cd sma_engine
cp .env.example .env
# Edit .env — add your Webull credentials, set ENGINE_SOURCE=webull
docker compose up -d
```

Open `http://localhost:3001`, log in with `admin` / `element47`. Dashboards
populate after the first scan cycle completes.

---

## Installation

### Step 1: Install Docker Desktop

**Mac:**
1. Go to `https://www.docker.com/products/docker-desktop/`
2. Download for Mac — choose Apple Silicon (M1/M2/M3/M4) or Intel
3. Open the downloaded `.dmg`, drag Docker to Applications
4. Open Docker from Applications and approve the helper tools prompt
5. Wait for the whale icon in the menu bar to stop animating

**Windows:**
1. Go to `https://www.docker.com/products/docker-desktop/`
2. Download for Windows and run the installer
3. When asked to enable WSL 2, say yes
4. Restart when prompted

Verify:
```bash
docker --version
```

### Step 2: Get Webull API credentials

1. Go to `https://developer.webull.com/`
2. Apply for access — request **market data permissions only**
3. Wait for approval email (1-2 business days)
4. Generate App Key and App Secret in the developer portal

### Step 3: Clone the repository

```bash
git clone https://github.com/riverrun99/sma_engine
cd sma_engine
```

### Step 4: Configure credentials

```bash
cp .env.example .env
```

Open `.env` and fill in:

```
ENGINE_SOURCE=webull
WEBULL_APP_KEY=your_app_key_here
WEBULL_APP_SECRET=your_app_secret_here
```

Leave `ENGINE_SOURCE=mock` to run on synthetic data while waiting for approval.

### Step 5: Start the stack

```bash
docker compose up -d
```

The first run downloads InfluxDB and Grafana and builds the engine image
(5–15 minutes). After that, starting is instant.

```
[+] Running 3/3
 ✔ Container e47_influxdb    Started
 ✔ Container e47_grafana     Started
 ✔ Container e47_engine      Started
```

### Step 6: View the output

**Grafana:** `http://localhost:3001` — login `admin` / `element47`

Three dashboards load automatically under the **SMA Engine** folder:
- **Live Signal** — top signal spotlight, leaderboard, hit and score history
- **Hit Heatmap** — hit distribution by outfit and timeframe
- **Regime & Systems** — live system states, HMM regime timeline

**Local files:** written to `output/` after each cycle:
- `output/signals_current.xlsx` — current snapshot, overwritten each cycle
- `output/signals_log.csv` — full history, one row appended per cycle

---

## Daily use

```bash
# Start everything
docker compose up -d

# Stop everything
docker compose down

# Rebuild and restart engine only (after editing .py files)
docker compose up -d --build engine

# Watch live logs
docker compose logs -f engine

# Check container status
docker compose ps
docker stats --no-stream

# Attach to the live terminal dashboard
docker attach e47_engine
# Detach without stopping: Ctrl+P then Ctrl+Q
```

Code changes (`.py` files) take effect after:
```bash
docker compose up -d --build engine
```

InfluxDB data is stored in a named Docker volume and persists across all
restarts. Only `docker compose down -v` deletes it. Data older than 90 days
auto-expires.

---

## Universe tiers

| Tier | Tickers | Contents |
|---|---|---|
| `tier1` | 143 | All major index ETFs, every leveraged pair (2x/3x bull/bear) for S&P/Nasdaq/Dow/Russell/semis, all 11 SPDR sectors + sub-sectors, precious metals, energy commodities, vol products, bonds/rates, crypto ETFs, China. This is the streaming tier. |
| `tier2` | 40 | Individual mega-cap stocks: AAPL, MSFT, NVDA, GOOGL, META, TSLA, AMD, JPM, XOM, COIN, and others |
| `all` | ~1,705 unique | Tier 1+2 + extended individual equities: S&P 500 components, mid-caps, healthcare, financials, industrials, REITs, international ADRs |

Set via `ENGINE_UNIVERSE=tier1 / tier2 / all` in `.env`.

TIER_1 is designed to always be scanned first and covers the instruments Raul
monitors as primary signals. If streaming is enabled, TIER_1 tickers receive
real-time tick data.

---

## Filtering results

After the first few cycles certain tickers may dominate rankings. Common noise
sources:

- **Bond/fixed-income ETFs** — very low volatility, prices sit on SMA levels
  for hundreds of candles, producing high decisecond scores that reflect
  stability rather than active confluence. Most are pre-muted.
- **Low-float biotech/pharma** — range-bound for weeks. Same levels get hit
  continuously without price going anywhere.
- **Leveraged inverse ETFs on stable assets** — currency and bond inverse ETFs
  (EUO, EPV, DUG) behave like the above when their underlying is range-bound.

Filtering is iterative — mute the first wave of noise, let the next cycle run,
see what surfaces underneath, repeat. Two or three passes typically brings
the top results to consistently liquid, tradeable names.

### Muting tickers

**Option 1 — edit the file directly:**

Open `muted_tickers.txt` and add tickers one per line:

```
EUO     # currency inverse ETF, range-bound
ERNA    # small biotech, 300+ hits on 2h — noise
```

**Option 2 — command line (run from the project folder):**

```bash
python3 mute.py EUO ERNA DUG EPV
```

`muted_tickers.txt` is hot-reloaded every cycle — no restart needed. The
engine logs confirm what was muted at the start of each cycle.

### Unmuting

```bash
python3 mute.py --unmute EUO ERNA
```

Or delete the lines from `muted_tickers.txt` directly.

### Adding tickers temporarily

Open `custom_tickers.txt` and add tickers one per line:

```
RDDT    # Reddit, watching post-IPO
HOOD    # Robinhood, potential setup
```

Hot-reloaded every cycle. Use this for tickers you want to track without
permanently adding them to the universe.

### Adding tickers permanently

Edit `UNIVERSE_TIER_3` in `engine.py` and add your tickers to the list. They
will be included in every future scan. Rebuild the engine container after:

```bash
docker compose up -d --build engine
```

To add tickers to the priority streaming tier (TIER_1), add them to
`UNIVERSE_TIER_1` instead. TIER_1 tickers are scanned first and, when
streaming is enabled, receive real-time tick data.

---

## Google Sheets setup (optional)

Lets you view signals on your phone via the Google Sheets app.

1. Go to `https://console.cloud.google.com/` → New Project
2. APIs & Services → Library → "Google Sheets API" → Enable
3. APIs & Services → Credentials → Create Credentials → Service account → Create
4. Click the service account → Keys → Add Key → JSON → save to `credentials/sheets.json`
5. Create a blank Google Sheet, copy the Sheet ID from the URL
6. Share the sheet with the `client_email` from `credentials/sheets.json`
7. Add to `.env`:

```
GOOGLE_SHEETS_CREDENTIALS_PATH=./credentials/sheets.json
GOOGLE_SHEET_ID=your_sheet_id_here
```

```bash
docker compose up -d --build engine
```

After the next cycle the sheet has two tabs: **Current** (overwritten) and
**Log** (appended).

---

## Remote access via Tailscale

Tailscale creates an encrypted private network so your phone can reach Grafana
from anywhere without port forwarding.

1. Install Tailscale on the host: `https://tailscale.com/download`
2. Install Tailscale on your phone (App Store / Play Store)
3. Log in with the same account on both
4. Open Grafana on your phone: `http://<tailscale-ip>:3001`
5. For terminal access install **Termius**, connect via SSH to the Tailscale IP,
   then `docker attach e47_engine`

---

## Configuration reference

All settings live in `.env`. Changes take effect after
`docker compose up -d --build engine`.

### Data source

| Variable | Values | Default |
|---|---|---|
| `ENGINE_SOURCE` | `webull` / `mock` | `webull` |

### Universe

| Variable | Values | Default |
|---|---|---|
| `ENGINE_UNIVERSE` | `tier1` / `tier2` / `all` | `all` |

### Scan timing

| Variable | Default | Description |
|---|---|---|
| `ENGINE_INTERVAL_SECONDS` | `300` | Seconds between cycles. With the full universe, cycles take 25–40 min so this setting is effectively a minimum — the engine runs back-to-back. |
| `ENGINE_REGIME_EVERY` | `12` | Refit HMM every N cycles. |

### Timeframes

| Variable | Default |
|---|---|
| `ENGINE_TIMEFRAMES` | `1m,5m,15m,30m,1h,2h,4h,1d,1w,1mo` |

### Microterm filter

| Variable | Default | Description |
|---|---|---|
| `ENGINE_MIN_TF_MINUTES` | `15` | Timeframes below this threshold are excluded from rankings. 15 drops 1m and 5m. Set to 0 to disable. |

1m and 5m hits are still detected and written to InfluxDB — they just cannot
surface to the top of the leaderboard. This prevents intraday noise from
overwhelming higher-timeframe structural signals.

### Hit detection

| Variable | Default | Description |
|---|---|---|
| `HIT_MODE` | `exact` | `exact`, `wick`, or `both` |
| `HIT_TOLERANCE` | `0.0` | Dollar tolerance for open/close proximity in `wick`/`both` modes |

### Lookback

| Variable | Default | Max |
|---|---|---|
| `ENGINE_LOOKBACK` | `130` | `999` |

How many recent candles are scored per cycle.

| Lookback | 30m candles covers | 1d candles covers |
|---|---|---|
| `130` | ~10 days | ~6 months |
| `500` | ~37 days | ~2 years |
| `999` | ~75 days | ~4 years |

### Incremental fetch

| Variable | Default | Description |
|---|---|---|
| `ENGINE_REFRESH_BARS` | `20` | On cycle 2+, only this many recent bars are fetched per (ticker, timeframe) pair and merged into the cache. Set to 0 to always fetch full history. |

### Sub-minute streaming

| Variable | Default | Description |
|---|---|---|
| `STREAM_ENABLED` | `false` | Enable MQTT tick streaming |
| `STREAM_TIMEFRAMES` | `1s,5s,15s,30s` | Sub-minute candle sizes built from ticks |

When enabled, the engine subscribes TIER_1 tickers (143 symbols) to Webull's
real-time tick feed. Ticks aggregate into OHLCV candles for each stream
timeframe and are scanned alongside REST candles every cycle.

To enable:
```
STREAM_ENABLED=true
```

### Cumulative deciseconds

| Variable | Default | Description |
|---|---|---|
| `INFLUX_URL` | `http://influxdb:8086` | InfluxDB endpoint |
| `INFLUX_BUCKET` | `sma_engine` | Bucket name |

Each cycle queries InfluxDB for the cumulative sum of deciseconds per
[ticker / timeframe / outfit / SMA period] over the last 7 days. This is
the cross-session time-series persistence layer. The first cycle writes to
Influx; from cycle 2 onward cumulative history blends into scoring.

### Output

| Variable | Default | Description |
|---|---|---|
| `ENGINE_TOP_N` | `500` | Ranked combinations written per cycle |
| `TERMINAL_UI` | `true` | Rich live dashboard in terminal |
| `LOCAL_OUTPUT_DIR` | `./output` | Where xlsx and csv files are written |

---

## Cycle time

With the full `all` universe (~1,705 unique tickers × 10 timeframes × 41 outfits):

| Phase | Cycle 1 (cold, no cache) | Cycle 2+ (warm cache) |
|---|---|---|
| Prefetch | ~30–50 min | ~25–40 min |
| Scan + rank | included | included |
| **Total** | **~30–50 min** | **~25–40 min** |

Cycle 1 fetches full bar history for any (ticker, timeframe) pair not already in
the disk cache. The cache persists across restarts via the `engine_cache` Docker
volume — so a restart resumes warm, not cold.

Cycle 2+ fetches only `ENGINE_REFRESH_BARS` recent bars per pair (default 20),
dramatically reducing API load. Total cycle time is dominated by the Webull
rate limit (~10 req/sec across ~18,000 pairs), not computation.

The engine runs back-to-back cycles with no idle time when cycle duration
exceeds `ENGINE_INTERVAL_SECONDS`.

---

## Signal reliability over time

| Days running | What to trust |
|---|---|
| Day 1 | Decisecond ranking and microterm filter are valid. Convergence scores are early-stage — treat with skepticism. |
| Days 2–4 | Cumulative InfluxDB history starts separating persistent levels from one-off hits. Time-series layer becomes meaningful. |
| Days 5–7 | Full convergence scoring reliable. Levels that have accumulated deciseconds across multiple sessions are the real signals. |
| Week 2+ | Leaderboard stabilizes. New entries represent genuinely fresh confluence, not recency bias. |

---

## Troubleshooting

**"no configuration file provided: not found"**
Run `cd ~/Developer/sma_engine` first.

**Engine exits with code 137**
Out-of-memory kill. Docker Desktop → Settings → Resources → raise Memory to
at least 8 GB → `docker compose restart engine`.

**Grafana shows "No data"**
Set the time picker to **Last 24 hours** or **Last 7 days**.

**"InfluxDB persistence disabled" in logs**
InfluxDB wasn't healthy when engine started. Run `docker compose restart engine`.

**"401 Unauthorized" in logs**
Wrong Webull credentials. Check `WEBULL_APP_KEY` and `WEBULL_APP_SECRET` in `.env`.

**Rate limit warnings**
Normal with large universes. Engine backs off and continues.

**Streaming not connecting**
Check that your Webull account has streaming permissions. Engine logs show
`[stream] Connected` on success.

---

## Repository structure

```
engine.py              Core: 41 outfits, 8 systems, hit detection, decisecond
                       scoring, convergence, ~1,705-ticker universe
daemon.py              Long-running scan loop (container entry point)
stream_client.py       MQTT sub-minute tick streaming + candle aggregation
persistence.py         InfluxDB writer: candles, hits, signals, top-N,
                       system states, regimes + cumulative deciseconds query
local_writer.py        Writes signals_current.xlsx and signals_log.csv
sheets_writer.py       Google Sheets writer (disabled without credentials)
terminal_ui.py         Rich live terminal dashboard
regime.py              Hidden Markov Model market regime detection
significance.py        Permutation significance tests + Benjamini-Hochberg FDR
backtest.py            Walk-forward + combinatorial purged cross-validation
async_fetch.py         Concurrent Webull fetcher with rate limiter
run_pipeline.py        Single-cycle CLI runner (alternative to the daemon)
mute.py                Batch mute/unmute tickers via command line
tests.py               Test suite
docker-compose.yml     Stack: InfluxDB + Grafana + engine
Dockerfile             Engine container definition
requirements.txt       Python dependencies
.env                   All configuration (gitignored)
.env.example           Template with placeholder values (safe to commit)
muted_tickers.txt      Tickers excluded each cycle (hot-reloaded, gitignored)
custom_tickers.txt     Extra tickers added each cycle (hot-reloaded)
grafana/               Provisioned datasource + 3 dashboards (auto-loaded)
output/                Engine output files (gitignored)
credentials/           Google service account JSON (gitignored)
```

---

## Known limitations

**Statistical sample size** — convergence disproportionality (time_series layer)
is calculated across sibling outfits. In practice 10–20 outfits produce hits for
any given (ticker, timeframe) pair, which is a marginal sample for z-score
reliability. Scores are directionally correct but not statistically tight,
especially in the first week before cumulative history builds.

**Cross-TF parm matching** — the parm_price convergence layer currently checks
whether current price is near the key variable's SMA value. A fuller
implementation would verify the same price level appears as a parameter across
multiple timeframes for the same ticker. This is a known gap versus Raul's full
methodology.

**Single lookback across timeframes** — `ENGINE_LOOKBACK` applies uniformly.
130 candles = 10 days on a 30m chart but 6 months on a daily chart.

**Index and spot symbols** — SPX, IXIC, DJI, VIX, TNX, DXY, HSI, DAX,
BTCUSD, ETHUSD, XAUUSD are included in TIER_1. Webull may not return OHLC
bar data for all of them. The engine skips silently on fetch failure.

**Data quality** — Webull's developer API is retail-grade. Minor OHLC
discrepancies can cause hits to appear or disappear versus primary exchange data.

**Transaction costs** — backtests do not model fees or slippage.

---

## Disclaimer

This software detects patterns in historical price data. It does not predict
future prices. Nothing it outputs is financial advice. Trading involves risk
of loss. You are solely responsible for any decisions you make based on its
output.
