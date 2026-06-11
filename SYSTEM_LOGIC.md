# The System Logic

## Overview

Equity vehicles utilize every SMA outfit. The primary influence that enables wealth firms to adjust equities higher and lower are called "Systems." Three singular baseline SMA functions are defined for the S&P 500, NASDAQ, and Dow Jones Industrial — each applied to specific timeframes. These are midterm trend-based algorithms for institutional buying and selling protocols.

None of the SMA outfits are specific to any vehicle. The outfits below are proprietary protocols for their respective indices.

---

## The Three Systems

### 1. S&P 500 — The System
- **Index:** SPX
- **Timeframe:** 30-minute chart
- **Outfit:** 10 / 50 / 200
- **Positive:** MA10 > MA50
- **Negative:** MA10 < MA50
- **Major level:** MA200 — key determinant of recovery or downturn

### 2. NASDAQ
- **Index:** IXIC (Composite)
- **Timeframe:** 20-minute chart (30-minute during underperformance)
- **Outfit:** 20 / 100 / 250
- **Positive:** MA20 > MA100
- **Negative:** MA20 < MA100
- **Major level:** MA250

The bifurcation between 20m and 30m timeframes reflects the NASDAQ's first proprietary SMA integer (20) and alternates based on performance regime.

### 3. Dow Jones Industrial
- **Index:** DJI
- **Timeframes:** 15-minute and 1-hour charts
- **Outfit:** 30 / 60 / 90 / 300 / 600 / 900
- **Positive:** MA90 > MA300
- **Negative:** MA90 < MA300
- **Major level:** MA900

The two timeframes reflect the U.S. institutional hour session (390 minutes). Both timeframes are used — 15m for active operations, 1H for structural confirmation.

---

## Heightened Volatility Rules

During periods of rising VIX, the cross-based logic shifts to a close-based logic.

| System | Normal rule | High-VIX rule |
|--------|-------------|---------------|
| SPX 30m | MA10 vs MA50 | Candle close above/below MA50 |
| NASDAQ 20m | MA20 vs MA100 | Candle close above/below MA100 |
| DJI 15m/1H | MA90 vs MA300 | Candle close above/below MA300 |

A rising VIX activates these shifted parameters. The MA cross relationship remains structural context; the candle close becomes the active signal.

---

## VIX Confirmation Framework

VIX is tracked using the SVIX outfit: **26 / 52 / 116 / 211 / 422 / 844**

| Timeframe | Purpose |
|-----------|---------|
| 1-minute | Real-time execution — watch alongside index candle close vs key MA |
| 10-minute | Intraday regime confirmation — full bull stack = high-vol rules active |
| 1-hour | Regime trigger — MA26 crossing above MA422 = official regime shift |

**SVIX daily (inverse VIX):** The MA cluster around 20 (MA116, MA211, MA422 converging) is the structural support level. SVIX holding 20 = systems can stabilize. SVIX losing 20 = sustained VIX elevation, high-vol rules remain active.

---

## Major Level Confluence

When all three systems reach their long-term MA simultaneously, this serves as a key determinant of market outcomes — indicating either recovery or downturn.

| System | Major level |
|--------|-------------|
| SPX | MA200 |
| NASDAQ | MA250 |
| DJI | MA900 |

---

## Crash Condition

Crashes only occur when the SPX system is negative (MA10 < MA50). In this state, large-scale liquidation events occur with no firm stepping in before major institutions (JPM, BofA, Wells Fargo, BlackRock, Citadel) unless providing liquidity.

---

## Broader Market Extensions

The 16/31/63/125/250/500 outfit (the "500 outfit") applied to broad market ETFs on the 2-hour chart:

| Vehicle | Key MA | Notes |
|---------|--------|-------|
| IWM (Russell 2000) | MA250 | Structural positive/negative line |
| IWV (Russell 3000) | MA250 | Structural positive/negative line |

**Positive:** MA16 > MA250  
**Negative:** MA16 < MA250  
**High-VIX:** Candle close vs MA250

---

## "Fill the Bucket" Dynamic

The system's effectiveness is not limited to a single protocol. Precision Buying Algorithms and Parameter Limitations create a push-and-pull dynamic between equity vehicles. When one system is negative, capital rotates — creating the "fill the bucket" game between vehicles across the universe.

The strongest read comes when all three primary systems align:
- All positive → institutional accumulation, firms actively buying
- All negative → institutional distribution, no firm steps in ahead of the majors
- Mixed → rotation in progress, watch lagging systems for confirmation

---

*Reference: TraderBJones framework. Internal use only.*
