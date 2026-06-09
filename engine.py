"""
═══════════════════════════════════════════════════════════════════════════════
  SMA OUTFIT DETECTION ENGINE
  Element 47 — single-file, Webull OpenAPI
═══════════════════════════════════════════════════════════════════════════════

  Detects, ranks, and surfaces real-time SMA outfit signals across U.S. public
  equity markets. Implements the documented detection/ranking framework:

    fetch candles → compute SMAs → detect exact OHLC hits → store in hash map
    → apply filters/scores → rank → apply conditionals/offset → output the
    top-ranked signal with outfit, equity, timeframe, entry, risk, protocol.

  Two parallel modules:
    1. Outfit Detection Engine — scans [outfit × equity × timeframe] combos,
       detects hits, ranks them, outputs the top signal(s).
    2. System Monitor — tracks the +/- state of 8 major market systems as
       contextual filters.

  Data adapter: WebullClient (real) | MockClient (synthetic, deterministic).
  Real client requires WEBULL_APP_KEY + WEBULL_APP_SECRET env vars.
  Mock mode runs end-to-end with no credentials — useful for development and
  for verifying the engine logic before Webull approval comes through.

  Build phases (per blueprint Section 10):
    [✓] 1. Data ingestion (Webull adapter + mock)
    [✓] 2. SMA computation (vectorized, 2-decimal precision)
    [✓] 3. Hit detection (exact OHLC equality)
    [✓] 4. Hash map storage (in-memory; InfluxDB swap-in noted)
    [✓] 5. Ranking engine (frequency + lookback filter + freshness)
    [✓] 6. System monitor (8 systems, parallel)
    [✓] 7. Convergence detection (a/b/d layers; c stub for cross-tf)
    [✓] 8. Offset testing (±0.01 post-rank)
    [✓] 9. Discovery mode (toggle, 1-999 brute force, off by default)
    [stub] 10. Decisecond layer (slot reserved; needs tick data feed)

═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import os
import sys
import time
import uuid
import json
import hmac
import base64
import hashlib
import logging
import argparse
import asyncio
import math
import multiprocessing
import random
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from itertools import product
from typing import Optional, Iterable

import numpy as np
import pandas as pd
import requests


# ═══════════════════════════════════════════════════════════════════════════════
# 1. CONSTANTS — outfits, systems, timeframes
# ═══════════════════════════════════════════════════════════════════════════════

# Per blueprint Section 4 — exact periods, exact ordering. No fabrication.
OUTFITS: list[dict] = [
    {"id":  1, "periods": [33, 66, 99, 333, 666, 999],          "name": "AN"},
    {"id":  2, "periods": [28, 56, 112, 224, 448, 976],          "name": "PRC Chair (7)"},
    {"id":  3, "periods": [33, 66, 131, 262, 626, 919],          "name": "Nikola"},
    {"id":  4, "periods": [22, 44, 121, 242, 484, 968],          "name": "22²/484"},
    {"id":  5, "periods": [39, 78, 156, 311, 622, 944],          "name": "TSLA Daily ETF Dual Sequence"},
    {"id":  6, "periods": [29, 58, 116, 232, 464, 928],          "name": "ISMA / Starmer 58th UK PM"},
    {"id":  7, "periods": [30, 60, 90, 300, 600, 900],           "name": "DJI"},
    {"id":  8, "periods": [25, 45, 75, 225, 450, 900],           "name": "Japan 225"},
    {"id":  9, "periods": [28, 56, 112, 224, 448, 896],          "name": "US Speaker of the House (56)"},
    {"id": 10, "periods": [28, 57, 114, 228, 456, 911],          "name": "WTC homage (911)"},
    {"id": 11, "periods": [29, 57, 114, 227, 455, 911],          "name": "US President seat (45)"},
    {"id": 12, "periods": [11, 44, 88, 111, 444, 888],           "name": "AN"},
    {"id": 13, "periods": [28, 55, 111, 221, 442, 884],          "name": "884"},
    {"id": 14, "periods": [27, 54, 108, 216, 432, 864],          "name": "Regression (432)"},
    {"id": 15, "periods": [26, 52, 106, 211, 422, 844],          "name": "SVIX (211)"},
    {"id": 16, "periods": [36, 52, 106, 211, 422, 844],          "name": "Chicago VIX 36/844"},
    {"id": 17, "periods": [27, 53, 105, 210, 420, 840],          "name": "TSLA (420)"},
    {"id": 18, "periods": [26, 51, 102, 205, 409, 818],          "name": "Octane"},
    {"id": 19, "periods": [26, 51, 102, 204, 408, 816],          "name": "Octuple / NVDA-AAPL Area Code"},
    {"id": 20, "periods": [25, 50, 100, 200, 400, 800],          "name": "Alphabet (100)"},
    {"id": 21, "periods": [25, 51, 101, 202, 404, 808],          "name": "Resource missing (404)"},
    {"id": 22, "periods": [22, 55, 77, 222, 555, 777],           "name": "AN"},
    {"id": 23, "periods": [22, 55, 77, 220, 550, 770],           "name": "Palantir"},
    {"id": 24, "periods": [24, 48, 96, 192, 384, 768],           "name": "Türkiye president seat (12)"},
    {"id": 25, "periods": [16, 31, 62, 124, 246, 748],           "name": "Bitcoin (248.666)"},
    {"id": 26, "periods": [24, 47, 94, 188, 376, 752],           "name": "US President seat (47)"},
    {"id": 27, "periods": [23, 46, 92, 184, 368, 736],           "name": "US President seat (46)"},
    {"id": 28, "periods": [23, 46, 92, 183, 366, 732],           "name": "Time (366)"},
    {"id": 29, "periods": [23, 46, 91, 183, 365, 730],           "name": "Time (365)"},
    {"id": 30, "periods": [30, 60, 90, 180, 360, 720],           "name": "180 Reversal (720)"},
    {"id": 31, "periods": [20, 40, 80, 160, 320, 640],           "name": "SDOW Flip"},
    {"id": 32, "periods": [25, 50, 100, 200, 400, 600],          "name": "France president seat (25)"},
    {"id": 33, "periods": [19, 38, 75, 150, 300, 600],           "name": "Russell 3000"},
    {"id": 34, "periods": [18, 36, 72, 144, 288, 576],           "name": "Time (1440)"},
    {"id": 35, "periods": [19, 37, 73, 143, 279, 548],           "name": "Waring's Problem"},
    {"id": 36, "periods": [17, 33, 66, 132, 264, 528],           "name": "33 Outfit"},
    {"id": 37, "periods": [16, 32, 64, 128, 256, 512],           "name": "Base 2 / NVDA"},
    {"id": 38, "periods": [16, 31, 63, 125, 250, 500],           "name": "QPUX/PUtin"},
    {"id": 39, "periods": [20, 100, 250],                        "name": "NAS"},
    {"id": 40, "periods": [10, 50, 200],                         "name": "S&P"},
    {"id": 41, "periods": [16, 32, 65, 160, 320, 650],           "name": "HV"},
]

# Per blueprint Section 5 — 8 systems monitored continuously.
SYSTEMS: list[dict] = [
    {"id": 1, "name": "S&P 500",        "proxy": "SPY",  "tf": "30m", "fast": 10, "slow":  50, "rule": "fast>slow_pos"},
    {"id": 2, "name": "NASDAQ",         "proxy": "QQQ",  "tf": "30m", "fast": 20, "slow": 100, "rule": "fast>slow_pos"},
    {"id": 3, "name": "Dow Jones",      "proxy": "DIA",  "tf": "1h",  "fast": 90, "slow": 300, "rule": "fast>slow_pos"},
    {"id": 4, "name": "VIX",            "proxy": "UVXY", "tf": "1h",  "fast": 52, "slow": 106, "rule": "fast>slow_pos"},
    {"id": 5, "name": "SVIX",           "proxy": "SVXY", "tf": "1h",  "fast": 52, "slow": 106, "rule": "fast>slow_pos"},
    {"id": 6, "name": "Russell 2000",   "proxy": "IWM",  "tf": "1d",  "fast": 10, "slow":  50, "rule": "fast>slow_pos"},
    {"id": 7, "name": "Russell 3000",   "proxy": "IWV",  "tf": "1d",  "fast": 19, "slow": 600, "rule": "fast>slow_pos"},
    {"id": 8, "name": "Semiconductors", "proxy": "SMH",  "tf": "1d",  "fast": 50, "slow": 100, "rule": "fast>slow_pos"},
]

# Per blueprint Section 3 — standard timeframe set.
# Webull timespan codes. Sub-minute (5s/15s/30s) requires tick data — stubbed.
TIMEFRAMES_STANDARD: list[str] = [
    "1m", "2m", "3m", "5m", "10m", "15m", "20m", "30m",
    "1h", "2h", "4h", "1d", "1w", "1mo",
]

WEBULL_TIMESPAN_MAP: dict[str, str] = {
    # Every minute increment 1m–30m
    "1m": "M1",   "2m": "M2",   "3m": "M3",   "4m": "M4",   "5m": "M5",
    "6m": "M6",   "7m": "M7",   "8m": "M8",   "9m": "M9",   "10m": "M10",
    "11m": "M11", "12m": "M12", "13m": "M13", "14m": "M14", "15m": "M15",
    "16m": "M16", "17m": "M17", "18m": "M18", "19m": "M19", "20m": "M20",
    "21m": "M21", "22m": "M22", "23m": "M23", "24m": "M24", "25m": "M25",
    "26m": "M26", "27m": "M27", "28m": "M28", "29m": "M29", "30m": "M30",
    # Hourly+
    "1h": "M60", "2h": "M120", "4h": "M240",
    "1d": "D1", "1w": "W1", "1mo": "MO1",
}

# Universe: ~2k tickers organized in tiers for rate-limit-aware scanning.
# Tier 1 = system proxies + active leveraged ETFs, scanned every cycle on all tfs.
# Tier 2 = broad liquid universe, scanned on 15m+ only.
# Tier 3 = wide universe, scanned on 1h+ only.
# Replace UNIVERSE_TIER_3 with your own list (NYSE/NASDAQ/CBOE) for full 2k coverage.

UNIVERSE_TIER_1: list[str] = [
    # ── Market indices (Webull may return these as read-only; engine skips on fail) ─
    "SPX",                      # S&P 500 Index
    "IXIC",                     # NASDAQ Composite
    "DJI",                      # Dow Jones Industrial Average
    "VIX",                      # CBOE Volatility Index
    "TNX",                      # 10-Year Treasury Yield
    "DXY",                      # US Dollar Index
    "HSI",                      # Hang Seng Index
    "DAX",                      # German DAX

    # ── Spot / crypto pairs ───────────────────────────────────────────────────
    "BTCUSD",                   # Bitcoin spot
    "ETHUSD",                   # Ethereum spot
    "XAUUSD",                   # Gold spot

    # ── Core ETFs ─────────────────────────────────────────────────────────────
    "SPY", "QQQ", "DIA", "IWM", "IWV", "IVV", "VOO", "RSP", "QQQM",

    # ── S&P 500 leveraged ─────────────────────────────────────────────────────
    "UPRO", "SPXU",             # 3x bull/bear (ProShares)
    "SPXL", "SPXS",             # 3x bull/bear (Direxion)
    "SSO", "SDS",               # 2x bull/bear
    "SH", "SPDN",               # 1x inverse

    # ── Nasdaq leveraged ──────────────────────────────────────────────────────
    "TQQQ", "SQQQ",             # 3x bull/bear
    "QLD", "QID",               # 2x bull/bear
    "PSQ",                      # 1x inverse

    # ── Dow leveraged ─────────────────────────────────────────────────────────
    "UDOW", "SDOW",             # 3x bull/bear
    "DDM", "DXD",               # 2x bull/bear
    "DOG",                      # 1x inverse

    # ── Russell 2000 leveraged ────────────────────────────────────────────────
    "TNA", "TZA",               # 3x bull/bear (Direxion)
    "URTY", "SRTY",             # 3x bull/bear (ProShares)
    "UWM", "TWM",               # 2x bull/bear
    "RWM",                      # 1x inverse

    # ── Volatility ────────────────────────────────────────────────────────────
    "UVXY", "SVXY", "VXX", "VIXY", "SVIX",

    # ── Semis ─────────────────────────────────────────────────────────────────
    "SMH", "SOXX",
    "SOXL", "SOXS",             # 3x bull/bear

    # ── SPDR sectors — all 11 + key sub-sectors ───────────────────────────────
    "XLF", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC",
    "XLE",                      # energy
    "XBI",                      # biotech
    "XOP",                      # oil & gas E&P
    "XHB",                      # homebuilders
    "XRT",                      # retail
    "XME",                      # metals & mining
    "KRE", "KBE",               # regional banks / banking

    # ── Sector leveraged ──────────────────────────────────────────────────────
    "FAS", "FAZ",               # financials 3x
    "ERX", "DRIP",              # energy 3x bull/bear
    "GUSH",                     # oil & gas 3x bull
    "TECL", "TECS",             # technology 3x
    "LABU", "LABD",             # biotech 3x
    "FNGU", "FNGD",             # FANG+ 3x
    "DPST",                     # regional banks 3x
    "NUGT", "DUST",             # gold miners 3x
    "JNUG", "JDST",             # junior gold miners 3x
    "NAIL",                     # homebuilders 3x
    "CURE",                     # healthcare 3x
    "DFEN",                     # aerospace & defense 3x
    "BNKU",                     # banks 3x
    "BULZ",                     # mega tech 3x
    "HIBL", "HIBS",             # high beta 3x
    "WEBL", "WEBS",             # internet 3x
    "DRN", "DRV",               # real estate 3x
    "MIDU",                     # mid cap 3x
    "RETL",                     # retail 3x
    "UTSL",                     # utilities 3x
    "TPOR",                     # transports 3x
    "PILL",                     # pharma 3x
    "WANT",                     # consumer discretionary 3x
    "BRZU",                     # Brazil 3x
    "INDL",                     # India 3x

    # ── Single-stock leveraged (high volume) ──────────────────────────────────
    "TSLL", "TSLS",             # Tesla 2x bull/bear
    "NVDL", "NVDS",             # Nvidia 2x bull/bear
    "MSTU", "MSTZ",             # MicroStrategy 2x bull/bear
    "MSTX",                     # MicroStrategy leveraged

    # ── Precious metals ───────────────────────────────────────────────────────
    "GLD", "IAU",               # gold
    "GDX", "GDXJ",              # gold miners
    "SLV", "PSLV",              # silver
    "UGL", "GLL",               # gold 2x bull/bear
    "AGQ", "ZSL",               # silver 2x bull/bear

    # ── Energy / commodities ──────────────────────────────────────────────────
    "USO", "UCO", "SCO",        # crude oil / 2x bull / 2x bear
    "UNG", "BOIL", "KOLD",      # natural gas / 2x bull / 2x bear

    # ── Bonds / rates ─────────────────────────────────────────────────────────
    "TLT",                      # 20yr treasury
    "TMF", "TMV",               # treasury 3x bull/bear
    "TBT",                      # treasury 2x bear
    "ZROZ",                     # zero-coupon long-duration

    # ── Crypto ────────────────────────────────────────────────────────────────
    "MARA",                     # bitcoin miner
    "BITO", "BITI",             # bitcoin futures / short

    # ── China ─────────────────────────────────────────────────────────────────
    "YINN", "YANG",             # China large cap 3x bull/bear
    "FXI", "MCHI", "KWEB",      # China large cap / MSCI / internet
]

UNIVERSE_TIER_2: list[str] = [
    # ── Mega caps ────────────────────────────────────────────────────────────
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "META", "AMZN", "TSLA", "AVGO",
    "AMD", "PLTR", "NFLX", "ORCL", "CRM", "ADBE", "INTC", "QCOM", "MU",
    # ── Financials ────────────────────────────────────────────────────────────
    "JPM", "BAC", "WFC", "GS", "MS", "C", "V", "MA",
    # ── Energy majors ─────────────────────────────────────────────────────────
    "XOM", "CVX", "COP", "OXY", "SLB", "EOG",
    # ── Crypto-adjacent stocks ────────────────────────────────────────────────
    "COIN", "MSTR", "RIOT", "CLSK",
    # ── International broad ───────────────────────────────────────────────────
    "EEM", "EFA", "EWZ", "VWO",
]

UNIVERSE_TIER_3: list[str] = [
    # ── T-Rex / GraniteShares single-stock leveraged (less liquid) ────────────
    "NVDX",                  # Nvidia 2x bull (GraniteShares)
    "AAPB",                  # Apple 2x bull
    "AMZU",                  # Amazon 2x bull
    "CONL",                  # Coinbase 2x bull
    "MSFO",                  # Microsoft 2x bull

    # ── iShares ───────────────────────────────────────────────────────────────
    "IAU",                   # Gold
    "SOXX",                  # Semiconductors
    "IBB",                   # Biotech
    "IGV",                   # Software
    "IYR",                   # Real estate
    "IVV",                   # S&P 500
    "IJR",                   # S&P 600 small cap
    "IWB",                   # Russell 1000
    "IWF",                   # Russell 1000 Growth
    "IWD",                   # Russell 1000 Value
    "IEMG",                  # Emerging markets
    "INDA",                  # India
    "MCHI",                  # China
    "EWJ",                   # Japan
    "EWG",                   # Germany
    "EWU",                   # UK
    "EWC",                   # Canada
    "EWT",                   # Taiwan
    "EWY",                   # South Korea

    # ── Vanguard ──────────────────────────────────────────────────────────────
    "VTI", "VOO", "VGT", "VHT", "VFH", "VDE", "VNQ",
    "VCR", "VIS", "VPU", "VO", "VB", "VEA",

    # ── ARK ───────────────────────────────────────────────────────────────────
    "ARKK", "ARKG", "ARKW", "ARKF", "ARKQ", "ARKX",

    # ── Thematic ──────────────────────────────────────────────────────────────
    "BOTZ",                  # AI & Robotics
    "LIT",                   # Lithium & battery
    "COPX",                  # Copper miners
    "JETS",                  # Airlines
    "TAN",                   # Solar
    "ICLN",                  # Clean energy
    "BLOK",                  # Blockchain
    "CIBR",                  # Cybersecurity
    "KWEB",                  # China internet
    "RSP",                   # Equal weight S&P 500

    # ── Individual equities ───────────────────────────────────────────────────
    # Healthcare
    "UNH", "LLY", "JNJ", "MRK", "PFE", "ABT", "TMO", "DHR",
    # Financials
    "BRK-B", "BLK", "SPGI", "CME", "ICE", "CB", "AXP",
    # Consumer
    "PG", "KO", "PEP", "WMT", "COST", "HD", "MCD", "NKE",
    # Energy E&P
    "DVN", "FANG", "HES", "MRO",
    # Industrials / defense
    "CAT", "DE", "HON", "GE", "RTX", "LMT", "NOC", "BA",
    # Utilities
    "NEE", "DUK", "SO",
    # REITs
    "AMT", "PLD", "O", "EQIX",
    # Semis / hardware
    "TSM", "ASML", "LRCX", "KLAC", "AMAT",
    # Cybersecurity
    "PANW", "CRWD", "ZS", "FTNT",
    # Cloud / SaaS
    "SNOW", "DDOG", "MDB", "NET", "NOW", "WDAY", "INTU",
    # E-commerce / consumer tech
    "SHOP", "UBER", "ABNB", "DASH", "ROKU",
    # Social / streaming
    "SNAP", "SPOT", "PYPL",
    # EV
    "RIVN", "NIO", "LI", "XPEV",
    # China ADRs
    "BIDU", "BABA", "JD", "PDD",
    # Metals / miners
    "FCX", "NUE", "CLF", "WPM",
    # Travel / leisure
    "MAR", "HLT", "CCL", "DAL", "UAL",

    # ── Additional leveraged ETFs ─────────────────────────────────────────────
    "DDM",                   # Dow 2x bull
    "RWM",                   # Russell 2000 inverse
    "SRTY",                  # Russell 2000 3x bear
    "QQQM",                  # Nasdaq 100 (Invesco mini)
    "SPHQ",                  # S&P 500 Quality factor
    "SPLV",                  # S&P 500 Low Volatility
    "IJH",                   # S&P 400 mid cap
    "SCHD", "SCHG", "SCHF",  # Schwab dividend / growth / intl
    "USMV",                  # MSCI USA Min Vol
    "ACWI",                  # All-country world
    "VEU",                   # Vanguard ex-US
    "IWO", "IWN",            # Russell 2000 Growth / Value
    "VOX", "VDC", "VAW",     # Telecom / Consumer staples / Materials Vanguard

    # ── Semis / hardware (S&P 500 additions) ──────────────────────────────────
    "CSCO", "IBM", "TXN", "MU", "MRVL", "ON", "ANET", "ARM",
    "SMCI", "SNPS", "CDNS", "ADSK", "ANSS", "EPAM", "CTSH",
    "ACN", "INFY", "WIT", "ORCL", "SAP", "PLTR",

    # ── Healthcare additions ───────────────────────────────────────────────────
    "ISRG", "REGN", "VRTX", "GILD", "BIIB", "MRNA", "ZTS",
    "BSX", "EW", "MDT", "SYK", "BDX", "IDXX", "IQV", "DXCM",
    "CI", "ELV", "CVS", "HUM", "MOH", "CNC", "MCK", "ABC", "CAH",
    "AMGN", "ALGN", "HOLX", "PODD", "RMD", "TECH", "TFX", "VAR",
    "BAX", "HSIC", "PKI", "XRAY", "VTRS", "ALXN", "BMRN", "EXAS",
    "FATE", "HALO", "INCY", "IONS", "JAZZ", "NBIX", "NKTR", "RARE",
    "RCKT", "SGEN", "SRPT", "TGTX", "XNCR", "ZYME",

    # ── Financials additions ───────────────────────────────────────────────────
    "USB", "TFC", "PNC", "COF", "DFS", "SYF", "AIG", "MET",
    "PRU", "AFL", "ALL", "PGR", "TRV", "HIG", "SCHW", "IBKR",
    "HOOD", "BX", "KKR", "APO", "ARES", "GPN", "FIS", "FISV",
    "SQ", "AFRM", "SOFI", "NDAQ", "MSCI", "MCO",
    "MTB", "HBAN", "CFG", "FITB", "RF", "KEY", "ZION", "CMA",
    "ALLY", "SLM", "CACC", "OMF", "ENVA", "WRLD",
    "RJF", "LPLA", "SSNC", "SEI", "BEN", "IVZ", "WDR",
    "TROW", "AMG", "EV", "APAM", "GS", "MS", "JPM", "BAC", "C", "WFC",

    # ── Consumer discretionary additions ──────────────────────────────────────
    "TJX", "ROST", "DLTR", "DG", "YUM", "QSR", "DPZ", "CMG",
    "F", "GM", "EL", "ULTA", "PM", "MO", "BTI", "STZ",
    "CLX", "CL", "GIS", "K", "HSY", "MDLZ", "MNST", "LOW", "TGT",
    "ETSY", "W", "RH", "BBWI", "GPS", "ANF", "AEO", "URBN",
    "PVH", "RL", "TPR", "CPRI", "VFC", "HBI", "LEVI", "SKX",
    "NKE",  # already in list but including for completeness guard
    "LULU", "DECK", "WWW", "CROX", "ONON",
    "SBUX", "DRI", "EAT", "TXRH", "BJRI", "CAKE",
    "MGM", "WYNN", "LVS", "CZR", "PENN", "DKNG", "BALY",
    "DIS", "NFLX", "PARA", "WBD", "FOXA", "FOX",
    "LYV", "MSGS", "MSGE", "EDR",
    "GPC", "AZO", "ORLY", "AAP",
    "HD", "LOW",   # already present, deduped at scan time
    "WSM", "ARHS", "LOVE", "ETD",
    "BURL", "FIVE", "OLLI",
    "AMZN",  # tier1 but safe dupe
    "EBAY", "VTEX", "VTRU",

    # ── Energy additions ───────────────────────────────────────────────────────
    "PSX", "VLO", "MPC", "WMB", "KMI", "HAL", "BKR",
    "APA", "OVV", "CTRA", "SM", "MTDR", "CHRD", "PDCE",
    "SLB", "FTI", "NOV", "HP", "PTEN", "NE", "DO",
    "MMP", "LNG", "TELL", "NFE",
    "DKL", "PARR", "DINO", "CLMT",
    "RIG", "VAL", "SDRL",
    "REI", "ESTE", "ARIS",

    # ── Industrials additions ──────────────────────────────────────────────────
    "UPS", "FDX", "NSC", "CSX", "UNP", "URI", "CMI", "ITW",
    "PH", "EMR", "ROK", "WM", "RSG", "FAST", "CTAS",
    "GWW", "MSM", "SNA", "SWK", "TT", "IR", "XYL", "XYLEM",
    "OTIS", "CARR", "TDG", "HWM", "SPR", "HII", "GD", "TXT",
    "L3H", "LDOS", "SAIC", "BAH", "DRS", "CACI", "MANT",
    "WAB", "TRN", "GBX", "GATX",
    "EXPD", "CHRW", "XPO", "ODFL", "SAIA", "WERN", "JBHT", "KNX",
    "AAL", "SAVE", "ALGT", "HA",
    "R", "REXL", "ARMK",
    "ABM", "KELYA", "MAN", "RHI",
    "AOS", "GNRC", "REZI", "ALLE",
    "SXC", "GFL", "SRCL", "US",

    # ── Materials additions ────────────────────────────────────────────────────
    "LIN", "APD", "SHW", "PPG", "DOW", "LYB", "CF", "MOS",
    "NEM", "GOLD", "AA", "VMC", "MLM",
    "ALB", "LTHM", "LAC", "SQM", "VALE",
    "RIO", "BHP", "SCCO", "TECK", "HBM",
    "MP", "NOVT", "REX", "CRUS",
    "IP", "PKG", "GPK", "SEE", "SON",
    "ECL", "FMC", "RPM", "HUN", "CC",
    "ATI", "STLD", "RS", "CMC", "MTX",

    # ── REITs additions ────────────────────────────────────────────────────────
    "SPG", "VTR", "WELL", "EQR", "AVB", "PSA", "IRM", "VICI",
    "ARE", "BXP", "KIM", "REG", "FRT", "NNN", "SRC", "STOR",
    "MPW", "OHI", "HR", "DOC",
    "CCI", "SBAC", "AMT",   # towers (AMT already in T3)
    "DLR", "CONE", "QTS",   # data center
    "WPC", "EPRT", "NTST",
    "COLD", "STAG", "REXR", "ELS", "SUI",
    "UDR", "AIR", "NHI", "LTC",
    "GLPI", "GAMING",
    "LAND", "AFAR", "PINE",

    # ── Utilities additions ────────────────────────────────────────────────────
    "AEE", "AEP", "AWK", "CMS", "CNP", "ED", "ETR",
    "EXC", "FE", "LNT", "NI", "EVRG", "XEL", "WEC",
    "ATO", "SRE", "PCG", "PNW", "OGE", "OGS", "NWE",
    "AWR", "YORW", "MSEX", "ARTNA",

    # ── Comm services / media additions ───────────────────────────────────────
    "T", "VZ", "TMUS", "LUMN", "SHEN", "USM",
    "TTWO", "EA", "ATVI", "RBLX", "U",
    "IAC", "ANGI", "ZG", "OPEN", "COMP",
    "YELP", "TRIP", "EXPE", "BKNG",
    "MTCH", "BMBL", "MAN",

    # ── Tech / software additions ──────────────────────────────────────────────
    "TWLO", "HUBS", "BILL", "DOCN", "GTLB", "DOMO", "NCNO",
    "SMAR", "ALTR", "CFLT", "ESTC", "FROG", "PLAN", "PEGA",
    "ALRM", "ARLO", "CEVA", "COHU", "FORM", "ICHR", "IPGP",
    "LITE", "LSCC", "MCHP", "MTSI", "NXPI", "OLED", "POWI",
    "QRVO", "RMBS", "SITM", "SWKS", "WOLF", "XPERI",
    "ACLS", "AEHR", "AOSL", "ASMB", "BRKS", "CCMP", "CLAR",
    "DIOD", "EGAN", "ELAB", "ENPH",   # ENPH in semis/solar
    "ENTG", "EVTC", "EXLS", "EXPO",
    "GKOS", "GRTS", "HQY", "INVA",
    "IPAR", "KFRC", "KINS", "KNSL",
    "KRYS", "LGND", "LQDT", "LSEA",
    "MLAB", "MMSI", "MNKD", "MPWR",
    "MSGE", "MTCH",   # safe dup
    "NABL", "NARI", "NFLX",   # NFLX safe dup
    "NTGR", "NVAX", "OPCH", "OSUR",
    "OTEX", "PCRX", "PDFS", "PLAB",
    "PLUS", "PMTS", "POWI",   # safe dup
    "PRGS", "PRPH", "PSNL", "PTCT",
    "QLYS", "RDVT", "REYN",
    "RGEN", "RIOT", "RLAY", "RNST",
    "RPAY", "RPRX", "RSKD", "RXRX",
    "SAIA",   # safe dup
    "SANM", "SCSC", "SIGI", "SLAB",
    "SLCA", "SMTC", "SNCR", "SNEX",
    "SPSC", "SSYS", "STAA", "STRL",
    "SUPN", "SVRA", "SWAV", "SYBT",
    "TASK", "TCBK", "TCMD", "TGTX",   # safe dup
    "TPIC", "TRMK", "TRTX", "TTEC",
    "TTGT", "TVTX", "TWST", "TZOO",

    # ── Additional S&P 500 / mega cap ─────────────────────────────────────────
    "MMM", "ADP", "PAYX", "VRSK", "IDEX", "IEX", "ROP", "ODFL",
    "AXON", "TASER", "AMETEK", "FBHS", "MAS", "WHR", "HAS", "MAT",
    "CHD", "CPB", "HRL", "SJM", "CAG", "MKC", "TAP", "BG",
    "ADM", "CTVA", "FMC",   # safe dup
    "BALL", "BMS", "CCK", "TRS", "OI",
    "ETN", "HUBB", "LECO", "NDSN", "PNR", "RXO",
    "CFX", "DOV", "FLS", "FWRD", "GNSS", "GRC",
    "AWI", "BLDR", "DOOR", "EXP", "IIIN", "UFPI",
    "GRMN", "COHR", "VIAV", "EXTR", "INFN",
    "WDC", "STX", "NTAP", "PSTG", "PRCT",
    "HPE", "HPQ", "DELL", "NCSX", "XRX",
    "QCOM", "AVGO", "INTC",   # tier1 safe dup
    "STM", "IFNNY", "ASXC",
    "SPXS", "SPXL",   # tier1 safe dup
    "EEM", "VWO", "DEM", "EMXC",
    "IYW", "FDN", "PNQI",   # internet ETFs
    "FINX", "KBWB", "IAI",   # fintech / banking ETFs
    "HLTH", "CURE", "LABD", "LABU",   # biotech levered
    "SOXL", "SOXS",   # semis levered (tier1 safe dup)
    "TECL", "TECS",   # tech levered

    # ── Mid-cap individual names ───────────────────────────────────────────────
    "BRKR", "BIO", "FDS", "MORN", "NATI", "TRMB", "MANH",
    "CSGP", "CBRE", "JLL", "CIGI", "NMRK",
    "BXMT", "ACRE", "RC", "TWO", "NLY", "AGNC", "ARR",
    "MFA", "IVR", "WMC", "EFC", "EARN",
    "LOPE", "PRDO", "STRA", "GHC", "EVLV",
    "DNOW", "PUMP", "NINE", "WTTR",
    "XOMA", "IMVT", "HALO",   # safe dup
    "TBK", "BOKF", "FFIN", "TCBI", "IBOC", "CVBF",
    "PACW", "WAL", "HOME", "BANR", "SBCF",
    "WSFS", "TBBK", "FULT", "UMBF", "WTFC",
    "CBU", "IBTX", "NBTB", "NWBI", "SBSI",
    "HFWA", "BSVN", "CBTX", "CCBG", "CFFN",
    "AMTB", "AUB", "BCAL", "BCOW", "BFST",
    "CCNE", "CFFI", "CLBK", "CNNB", "COLB",
    "CPF", "CTBI", "CZWI", "ECBK", "EGBN",

    # ── International ADRs / large caps ───────────────────────────────────────
    "BP", "SHEL", "TTE", "SNY", "NVS", "AZN", "GSK",
    "ITUB", "BBD", "ERIC", "NOK", "ASML",   # safe dup
    "UL", "BTI",   # safe dup
    "SONY", "TM", "HMC", "NSANY",
    "BABA",   # safe dup
    "TCEHY", "NTES", "WB", "IQ", "VNET",
    "GRAB", "SEA",   # SE safe dup
    "SPOT",   # safe dup
    "NU", "STNE", "PAGS", "CASH",
    "CNHI", "RACE", "LUX", "MONC",
    "SHOP",   # safe dup
    "GLXY", "HUT", "MARA", "CIFR", "CLSK",   # crypto miners
    "COIN",   # Coinbase
    "MSTR",   # MicroStrategy

    # ── Biotech / pharma small-mid cap ────────────────────────────────────────
    "ACAD", "ACMR", "AGIO", "AKBA", "AKRO", "ALEC",
    "ALLO", "ALPN", "ALKS", "ALNY", "ALXO", "AMGN",   # safe dup
    "AMRN", "AMRS", "ANAB", "APLS", "APLT", "ARAV",
    "ARDX", "ARQT", "ARWR", "ARVN", "ASND", "ATRC",
    "AUPH", "AVDL", "AVEO", "AVID", "AVRO", "AXSM",
    "AZTA", "BEAM", "BHVN", "BNGO", "BOLT", "BPMC",
    "BPTH", "BTAI", "CAPR", "CBAY", "CBPO", "CCRN",
    "CDTX", "CERE", "CHRS", "CLVS", "CMPX", "CNST",
    "COKE", "COGT", "COMP",   # safe dup
    "CORT", "CPIX", "CRVL", "CRVS", "CSGP",   # safe dup
    "CSII", "CTMX", "DAWN", "DCPH", "DMTK",
    "DNLI", "DRNA", "EDIT", "EIDX", "EIGR",
    "ENTA", "EPZM", "ERNA", "ESPR", "ETNB",
    "EVGN", "EVLO", "EVMO", "EXEL", "FATE",   # safe dup
    "FGEN", "FOLD", "FREQ", "FRTX", "GALT",
    "GCPW", "GERN", "GHRS", "GLPG", "GLYC",
    "GMTX", "GNTA", "GOSS", "GPCR", "GRPH",
    "GRTS",   # safe dup
    "HALO",   # safe dup

    # ── Additional thematic / factor ETFs ─────────────────────────────────────
    "GBTC", "IBIT", "FBTC", "ARKB",   # Bitcoin ETFs
    "BITW", "AETH", "ETHU",            # crypto ETFs
    "SHNY", "GOAU", "RING", "GDX", "GDXJ",   # gold miners
    "SIL", "SILJ",                     # silver miners
    "PALL", "PPLT",                    # palladium / platinum
    "URA",                             # uranium
    "URNM", "NLR",                     # nuclear / uranium
    "REMX",                            # rare earth
    "MOO",                             # agribusiness
    "WEAT", "CORN", "SOYB",            # grain futures
    "NIB", "BAL", "JO",                # softs
    "DBB", "PDBC", "DJP",              # broad commodities
    "USO", "BNO",                      # crude oil
    "UNG", "BOIL", "KOLD",             # nat gas
    "DBA",                             # agriculture
    "PDBA",                            # diversified ag
    "WOOD",                            # timber
    "SOIL",                            # soil / ag
    "PICK",                            # metals & mining
    "GUNR",                            # global natural resources
    "MSOS", "YOLO", "MJ",              # cannabis
    "BETZ",                            # sports betting
    "HERO",                            # gaming & esports
    "NERD",                            # esports
    "ESPO",                            # video games
    "GAMR",                            # gaming
    "MEME",                            # meme stocks
    "BUZZ",                            # social sentiment
    "SPAK",                            # SPAC
    "XTLN",                            # tele / 5G
    "FIVG",                            # 5G
    "NXTG",                            # next gen connectivity
    "SNSR",                            # IoT
    "SKYY",                            # cloud computing
    "WCLD",                            # cloud
    "BUG",                             # cybersecurity
    "HACK",                            # cybersecurity
    "ROBO",                            # robotics
    "IRBO",                            # AI & robotics
    "THNQ",                            # AI
    "AIQ",                             # AI
    "LRNZ",                            # AI/machine learning
    "DTCR",                            # data center
    "SRVR",                            # data center REITs
    "INDS",                            # industrial REITs
    "HOMZ",                            # housing
    "REZ",                             # residential REITs
    "KBWY",                            # small-cap REITs
    "SMLV",                            # small-cap low vol
    "XSMO",                            # small-cap momentum
    "MOMO",                            # momentum factor
    "QMOM",                            # quantitative momentum
    "VMOT",                            # value momentum
    "DIVA",                            # dividend momentum
    "DIVB", "DVYL", "DHS",             # dividend ETFs
    "DGRW", "REGL", "SDOG",            # dividend growth
    "PFF", "PFFD", "FPE",              # preferred stock
    "HYLD", "JNK", "HYG",             # high yield bond ETFs
    "LQD", "VCIT", "VCSH",            # investment grade bond ETFs
    "TIP", "VTIP", "STIP",            # TIPS
    "MBB", "VMBS",                     # mortgage-backed
    "EMB", "PCY",                      # EM bonds
    "BWX", "BNDX",                     # intl bonds
    "BNDW",                            # global bonds
    "AGG", "BND",                      # total bond

    # ── More S&P 500 names ────────────────────────────────────────────────────
    "MMC", "AON", "WTW", "ERIE", "RLI",
    "CINF", "RE", "RNR", "WRB", "MKL",
    "BRO", "RYAN", "AJG", "MMC",
    "FNF", "FAF", "STC",
    "NWSA", "NWS", "FWONK", "LEN", "PHM", "TOL", "DHI", "MDC",
    "LGIH", "MHO", "TMHC", "NVR", "SSD", "IBP",
    "TREX", "PGTI", "APOG", "ROCK", "CSL",
    "AIT", "GWW",
    "ACCO", "BRC", "CLH", "HNI", "KFY",
    "MWA", "NN", "NVT", "SITE", "TISI",
    "WLDN", "WSO", "AYI", "AAON",
    "FELE", "IIVI", "NOVT", "RBC", "REEF",
    "SPXC", "SPOK", "STGH", "ATKR",
    "AIRC", "AMBC", "APLE", "BRSP", "BRT",
    "CLDT", "CHCT", "CSR", "CTO", "DEI",
    "DHCP", "DLR",   # safe dup
    "ESRT", "EXR", "FCPT", "FSP", "GNL",
    "GMRE", "GOOD", "IIPR", "INDO", "INDT",
    "JBGS", "KRC", "KRG", "LADR", "LXP",
    "MNR", "NXRT", "OLP", "PDM", "PECO",
    "PLYM", "PW", "QTS",   # safe dup
    "RLJ", "SHO", "SKT", "SQFT", "TPVG",
    "UBA", "UNIT", "VER", "VICI",   # safe dup
    "XHR",

    # ── More tech / internet ──────────────────────────────────────────────────
    "ZI", "SEMR", "AMPL", "BRZE", "SPRK",
    "DUOL", "COUR", "CHGG", "ZETA",
    "MAPS", "MAPS", "RELY", "FLYW", "STEP",
    "ONON",   # safe dup
    "MNDY", "TASK",   # safe dup
    "PAYC", "PCTY", "APPF",
    "YEXT", "TTGT",   # safe dup
    "ASAN", "BOX", "DOCU", "DRCT",
    "FRSH", "JAMF", "JFIN", "KLIC",
    "LPSN", "MGNI", "MNTV", "NCLH",
    "NEWR", "NNOX", "NUAN",
    "PAYA", "PRFT", "PROS", "QNST",
    "SLAB",   # safe dup
    "SPSC",   # safe dup
    "SVMK", "TENB", "TTWO",   # safe dup
    "UPWK", "VSAT", "WEX", "XPEL",
    "ZETA", "ZUO",

    # ── More healthcare ───────────────────────────────────────────────────────
    "ARWR",   # safe dup
    "AVXS", "AXNX", "BCAB", "BGNE", "BLUE",
    "CGEN", "CMRX", "CNMD", "CNST",   # safe dup
    "CODX", "CVAC", "CYCN",
    "ELOS", "EOLS", "EPIX", "ESPR",   # safe dup
    "HCAT", "HCKT", "HLI", "HALO",   # safe dup
    "IART", "ICVX", "IHRT",
    "JNCE", "KDMN", "KROS",
    "LBPH", "LEGN", "LNTH",
    "NKTX", "NTLA", "NVAX",   # safe dup
    "ORIC", "ORTX", "PCVX",
    "PHAT", "PMVP", "PRLD",
    "PTGX", "RCUS", "RETA",
    "RIGL", "RLAY",   # safe dup
    "RPTX", "RUBY", "SAGE",
    "SPNE", "SPRO", "STRO",
    "SURF", "SVRA",   # safe dup
    "SWTX", "SYRA", "TBPH",
    "TCRT", "TDIO", "TELA",
    "TICA", "TPVG",   # safe dup
    "TRVI", "TVTX",   # safe dup
    "YMAB", "ZAFG", "ZNTL",

    # ── More financials / fintech ─────────────────────────────────────────────
    "ACRS", "AFIN", "AFMD",
    "BFLY", "BLNK", "BRLT",
    "CARG", "CARV", "CASH",   # safe dup
    "CBAN", "CBFV", "CBNK",
    "CMTG", "COOP", "COVA",
    "CZFS", "DCOM", "DNBA",
    "ESSA", "EVBN", "EVTC",   # safe dup
    "FBIZ", "FBMS", "FBRT",
    "FCNCA", "FFBC", "FFBH",
    "FHB", "FISI", "FITB",   # safe dup
    "FMBH", "FMFG", "FNWB",
    "GCBC", "GFED", "GNTY",
    "HAFC", "HBCP", "HBIO",
    "HIFS", "HMST", "HNRG",
    "INBK", "INDB", "INTL",
    "IPAC", "ISTR", "JRVR",
    "LCNB", "LKFN", "LMST",
    "MBWM", "MCBC", "MFIN",
    "MFNB", "MGYR", "MNSB",
    "MRLN", "MSBI", "MSVB",
    "NBTB",   # safe dup
    "NFBK", "NFLD", "NIDB",
    "NRDS", "NRIM", "NWIN",
    "OCFC", "OFED", "OFLX",
    "OSHC", "OSMT", "OVLY",
    "PBBK", "PBCT", "PBHC",
    "PFIS", "PLBC", "PNFP",
    "PPBI", "PROV", "PVBC",
    "QCRH", "RBCAA", "RCAT",
    "RRBI", "RRST", "RSSS",
    "RVSB", "SBNY", "SFBC",
    "SFBS", "SFST", "SHBI",
    "SMBC", "SMBK", "SMFG",
    "STBA", "TCBX", "TBNK",
    "TCFC", "THFF", "TIPT",
    "TPVG",   # safe dup
    "TRMK",   # safe dup
    "TWFG", "UBCP", "UBFO",
    "UBSI", "UCBI", "UCFC",
    "UNTY", "UONE", "UVSP",
    "VBTX", "VFIN", "VIRT",
    "WABC", "WAFD", "WASH",
    "WMPN", "WNEB", "WSBC",
    "WSFS",   # safe dup
    "XBCR", "XBKS",
    "YOUNK", "ZION",   # safe dup

    # ── VIX instruments ───────────────────────────────────────────────────────
    "UVIX",                  # 2x long VIX short-term futures
    "VIXM",                  # VIX mid-term futures
    "VIXY",                  # ProShares VIX short-term futures

    # ── Single-stock leveraged ETFs (new) ─────────────────────────────────────
    "AAPD",                  # Apple 1x bear (Direxion)
    "AAPU",                  # Apple 2x bull (Direxion)
    "TSLT",                  # Tesla 2x bull (T-Rex)
    "TSLQ",                  # Tesla bear (AXS)
    "NVD",                   # Nvidia 1x bear (GraniteShares)
    "AMDL",                  # AMD 2x bull
    "ARMU",                  # ARM bear
    "MSFL",                  # Microsoft leveraged
    "TSMZ",                  # TSM bear
    "ORCX",                  # Oracle bear
    "AMDD",                  # AMD bear
    "NFXL",                  # Netflix leveraged
    "SMST",                  # SMCI bear
    "GEVX",                  # GE Vernova leveraged
    "PLTD",                  # Palantir bear
    "GGLL",                  # Alphabet 2x bull
    "GGLS",                  # Alphabet bear
    "QPUX",                  # QQQ leveraged
    "QBTZ",                  # Bitcoin/QQQ leveraged

    # ── Crypto-adjacent (non-crypto ETFs) ─────────────────────────────────────
    "BITU",                  # Bitcoin 2x bull (ProShares)
    "SBIT",                  # Bitcoin short (ProShares)
    "ETHD",                  # Ethereum bear
    "EETH",                  # Ethereum 2x bull
    "SETH",                  # Ethereum short
    "CONI",                  # Coinbase inverse

    # ── Sector / inverse ETFs (new) ───────────────────────────────────────────
    "REK",                   # Short real estate (ProShares)
    "SEF",                   # Short financials (ProShares)
    "MZZ",                   # 2x short mid cap (ProShares)
    "URE",                   # Ultra real estate 2x (ProShares)
    "CWEB",                  # 2x China internet (Direxion)
    "EWH",                   # iShares MSCI Hong Kong
    "FTGC",                  # First Trust Global Tactical Commodity
    "UUUU",                  # Energy Fuels (uranium)
    "SMSOX",                 # SMCI options/leveraged
    "NFXL",                  # Netflix leveraged (safe dup guard)

    # ── Individual equities (new) ─────────────────────────────────────────────
    "UPST",                  # Upstart Holdings
    "AI",                    # C3.ai
    "GME",                   # GameStop
    "NVO",                   # Novo Nordisk
    "GEV",                   # GE Vernova
    "SMR",                   # NuScale Power
    "OKLO",                  # Oklo (nuclear)
    "FICO",                  # Fair Isaac (FICO score)
    "AUR",                   # Aurora Innovation (autonomous)
    "IOT",                   # Samsara
    "GEVO",                  # Gevo (sustainable aviation fuel)
    "GEO",                   # GEO Group
    "TSL",                   # Greenlight Capital Re / Tesla-adjacent ETF
    "Q",                     # Quintiles / misc
    "SABS",                  # misc small cap
    "MUD",                   # leveraged Micron / misc
    "MUU",                   # Micron structured product
    "SNDK",                  # SanDisk (legacy; dead ticker filter will catch if delisted)
    "PTIR",                  # misc
    "MYTY",                  # misc
    "XRPT",                  # misc
    "FIG",                   # misc
    "BNC",                   # misc
    "AVS",                   # misc
    "ORCS",                  # misc

    # ── Watchlist additions (from user watchlist, May 2025) ───────────────────

    # Vanguard ETFs (missing from prior tiers)
    "VV",                    # Vanguard Large Cap
    "VTV",                   # Vanguard Value
    "VBR",                   # Vanguard Small Cap Value
    "VIG",                   # Vanguard Dividend Appreciation
    "VXF",                   # Vanguard Extended Market
    "VUG",                   # Vanguard Growth
    "VGK",                   # Vanguard European
    "VYM",                   # Vanguard High Dividend Yield
    "VXUS",                  # Vanguard Total International

    # Broad market / factor ETFs
    "ITOT",                  # iShares Core S&P Total US Market
    "IWP",                   # iShares Russell Mid-Cap Growth
    "IVW",                   # iShares S&P 500 Growth
    "SPMD",                  # SPDR Portfolio S&P 400 Mid Cap
    "SPMO",                  # Invesco S&P 500 Momentum
    "SPYV",                  # SPDR Portfolio S&P 500 Value
    "JEPQ",                  # JPMorgan Nasdaq Equity Premium Income

    # Sector ETFs
    "ITA",                   # iShares U.S. Aerospace & Defense
    "SOXQ",                  # Invesco PHLX Semiconductor
    "XHB",                   # SPDR S&P Homebuilders
    "QTUM",                  # Defiance Quantum ETF
    "FEZ",                   # SPDR Euro STOXX 50

    # Leveraged ETFs
    "ROM",                   # ProShares Ultra Technology 2x
    "BIB",                   # ProShares Ultra Nasdaq Biotech 2x
    "SPUU",                  # Direxion Daily S&P 500 Bull 2x
    "NVDU",                  # ProShares Ultra NVDA 2x
    "EZJ",                   # ProShares Ultra MSCI Japan 2x
    "DIG",                   # ProShares Ultra Oil & Gas 2x
    "KORU",                  # Direxion Daily South Korea Bull 3x

    # Individual equities
    "VST",                   # Vistra Energy (power / nuclear)
    "CEG",                   # Constellation Energy (nuclear)
    "RDDT",                  # Reddit
    "RKLB",                  # Rocket Lab
    "NBIS",                  # Nebius Group (AI cloud)
    "CRCL",                  # Circle Internet Group
    "H",                     # Hyatt Hotels
    "TER",                   # Teradyne (semiconductor test)
    "CGNX",                  # Cognex (machine vision)
    "BWXT",                  # BWX Technologies (nuclear)
    "LEU",                   # Centrus Energy (uranium enrichment)
    "AGX",                   # Argan (power plant construction)
    "RCL",                   # Royal Caribbean

    # Precious metals
    "GLDM",                  # SPDR Gold MiniShares (lower-cost GLD alt)

    # ── Watchlist batch 2 (May 2025) ─────────────────────────────────────────

    # iShares sector ETFs
    "IYE",                   # iShares U.S. Energy
    "IYF",                   # iShares U.S. Financials
    "ITB",                   # iShares U.S. Home Construction
    "IAK",                   # iShares U.S. Insurance
    "IHI",                   # iShares U.S. Medical Devices
    "DYNF",                  # iShares U.S. Equity Factor Rotation Active
    "ESGV",                  # Vanguard ESG U.S. Stock
    "HODL",                  # VanEck Bitcoin ETF
    "BTF",                   # CoinShares Bitcoin and Ether ETF

    # Direxion leveraged (new)
    "EDZ",                   # Direxion Daily EM Bear 3x
    "EDC",                   # Direxion Daily EM Bull 3x
    "MEXX",                  # Direxion Daily Mexico Bull 3x
    "MSFD",                  # Direxion Daily MSFT Bear 1x
    "MSFU",                  # Direxion Daily MSFT Bull 2x
    "QQQD",                  # Direxion Daily Magnificent 7 Bear 1x
    "QQQU",                  # Direxion Daily Magnificent 7 Bull 2x
    "NVDD",                  # Direxion Daily NVDA Bear 1x
    "DUSL",                  # Direxion Daily Industrials Bull 3x
    "ELIS",                  # Direxion Daily LLY Bear 1x
    "ELIL",                  # Direxion Daily LLY Bull 2x
    "METD",                  # Direxion Daily META Bear 1x
    "METU",                  # Direxion Daily META Bull 2x

    # ProShares inverse (new)
    "SMDD",                  # ProShares UltraPro Short MidCap400
    "SCC",                   # ProShares UltraShort Consumer Discretionary
    "SZK",                   # ProShares UltraShort Consumer Staples
    "DUG",                   # ProShares UltraShort Energy
    "EUO",                   # ProShares UltraShort Euro
    "FXP",                   # ProShares UltraShort FTSE China 50
    "EPV",                   # ProShares UltraShort Europe
    "PST",                   # ProShares UltraShort 7-10 Year Treasury

    # Individual equities (new)
    "WEN",                   # Wendy's
    "BULL",                  # Webull Corp
    "UMAC",                  # Unusual Machines (drones)
    "CRWV",                  # CoreWeave (AI cloud)
    "BROS",                  # Dutch Bros
    "ENB",                   # Enbridge
    "ET",                    # Energy Transfer LP
    "COMP",                  # Compass Inc (real estate tech)
]


# ═══════════════════════════════════════════════════════════════════════════════
# 2. DATA ADAPTER — Webull OpenAPI client + Mock client
# ═══════════════════════════════════════════════════════════════════════════════

class DataClient:
    """Abstract data client interface. Both Webull and Mock implement this."""

    def fetch_bars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        """Return a DataFrame with columns [timestamp, open, high, low, close, volume].

        Timestamps in UTC. Prices as floats, rounded to 2 decimals.
        """
        raise NotImplementedError


class WebullClient(DataClient):
    """Real Webull OpenAPI client.

    Uses the official Webull SDK when available. The SDK handles HMAC signing,
    endpoint details, and response wrapping. A manual request path is kept as a
    fallback for environments where the SDK is not installed.

    Reads credentials from env:
      WEBULL_APP_KEY, WEBULL_APP_SECRET, WEBULL_REGION (default 'us')

    Rate limit: 60 calls/minute per app key. We enforce client-side.
    """

    BASE_URL = "https://api.webull.com"
    SDK_TIMESPAN_MAP = {
        # Every minute increment 1m–30m
        "1m": "M1",   "2m": "M2",   "3m": "M3",   "4m": "M4",   "5m": "M5",
        "6m": "M6",   "7m": "M7",   "8m": "M8",   "9m": "M9",   "10m": "M10",
        "11m": "M11", "12m": "M12", "13m": "M13", "14m": "M14", "15m": "M15",
        "16m": "M16", "17m": "M17", "18m": "M18", "19m": "M19", "20m": "M20",
        "21m": "M21", "22m": "M22", "23m": "M23", "24m": "M24", "25m": "M25",
        "26m": "M26", "27m": "M27", "28m": "M28", "29m": "M29", "30m": "M30",
        # Hourly+
        "1h": "M60", "2h": "M120", "4h": "M240",
        "1d": "D", "1w": "W", "1mo": "M",
    }
    ETF_SYMBOLS = {
        "SPY", "QQQ", "DIA", "IWM", "IWV", "SMH", "UVXY", "SVXY",
        "XLE", "ERX", "KOLD", "DRIP", "BOIL", "FAZ", "FAS", "SOXS",
        "SOXL", "SDOW", "TZA", "SPDN", "TMF", "ZROZ", "PSLV", "ZSL",
        "AGQ", "SLV", "HYG", "SJB", "TQQQ", "SQQQ", "UPRO", "SPXU",
        "YINN", "YANG", "GLD", "GDX", "GDXJ", "USO", "UNG", "TLT",
        "IEF", "SHY", "LQD", "JNK", "EEM", "EWZ", "FXI", "EFA", "VWO",
    }

    MAX_WORKERS = 6          # parallel fetch workers
    RATE_LIMIT_SLEEP = 0.7   # seconds between requests per worker

    def __init__(self, app_key: str, app_secret: str, region: str = "us"):
        self.app_key = app_key
        self.app_secret = app_secret
        self.region = region
        self._call_times: list[float] = []
        self._call_times_lock = threading.Lock()
        self._rate_limit = 600  # calls per minute (Webull allows ~600/min)
        self._sdk_data_client = None
        self._sdk_available = False
        self._init_sdk()

    def _init_sdk(self) -> None:
        try:
            from webull.core.client import ApiClient
            from webull.data.data_client import DataClient as SDKDataClient
        except ImportError:
            logging.warning(
                "Official Webull SDK not installed; using manual OpenAPI fallback"
            )
            return

        host = self.BASE_URL.replace("https://", "").replace("http://", "")
        api_client = ApiClient(self.app_key, self.app_secret, self.region)
        api_client.add_endpoint(self.region, host)
        self._sdk_data_client = SDKDataClient(api_client)
        self._sdk_available = True

    def _rate_limit_wait(self) -> None:
        """Thread-safe sliding-window 600/min limiter + per-request sleep."""
        with self._call_times_lock:
            now = time.monotonic()
            self._call_times = [t for t in self._call_times if now - t < 60]
            if len(self._call_times) >= self._rate_limit:
                wait = 60 - (now - self._call_times[0]) + 0.1
                logging.warning(f"Webull rate limit reached, sleeping {wait:.1f}s")
                time.sleep(wait)
            self._call_times.append(time.monotonic())
        time.sleep(self.RATE_LIMIT_SLEEP)  # 0.7s spacing between requests

    def _sign(self, method: str, path: str, query: str, body: str,
              timestamp: str, nonce: str) -> str:
        """HMAC-SHA1 signature per Webull OpenAPI spec.

        Canonical string format follows the documented pattern. Confirm exact
        canonicalization order against the auth docs once credentials arrive
        — the official Python SDK is the source of truth.
        """
        canonical = "\n".join([method, path, query, body, timestamp, nonce])
        digest = hmac.new(
            self.app_secret.encode("utf-8"),
            canonical.encode("utf-8"),
            hashlib.sha1,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def _headers(self, method: str, path: str, query: str = "", body: str = "") -> dict:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        nonce = uuid.uuid4().hex
        signature = self._sign(method, path, query, body, timestamp, nonce)
        return {
            "x-app-key": self.app_key,
            "x-timestamp": timestamp,
            "x-signature-algorithm": "HMAC-SHA1",
            "x-signature-version": "1.0",
            "x-signature-nonce": nonce,
            "x-version": "v2",
            "x-signature": signature,
            "Content-Type": "application/json",
        }

    # Symbols that trade under category=CRYPTO on Webull OpenAPI
    _CRYPTO_SYMBOLS: frozenset = frozenset({
        "BTC-USD", "ETH-USD", "BNB-USD", "SOL-USD", "XRP-USD",
        "DOGE-USD", "ADA-USD", "AVAX-USD", "DOT-USD", "MATIC-USD",
        "LTC-USD", "LINK-USD", "UNI-USD", "ATOM-USD", "XLM-USD",
    })

    def _category_for(self, symbol: str) -> str:
        if symbol in self._CRYPTO_SYMBOLS:
            return "CRYPTO"
        if symbol in self.ETF_SYMBOLS:
            return "US_ETF"
        return "US_STOCK"

    def _parse_bars_payload(self, payload) -> pd.DataFrame:
        bars = None
        if isinstance(payload, dict):
            inner = payload.get("data") or {}
            bars = (inner.get("list") if isinstance(inner, dict) else inner) \
                or payload.get("list") or []
        elif isinstance(payload, list):
            bars = payload

        if not bars:
            return pd.DataFrame()

        rows = []
        for bar in bars:
            ts = bar.get("t") or bar.get("timestamp") or bar.get("time")
            if ts is None:
                continue
            ts_value = pd.to_datetime(ts, utc=True)
            if isinstance(ts, (int, float)) or str(ts).isdigit():
                ts_int = int(ts)
                unit = "ms" if ts_int > 10_000_000_000 else "s"
                ts_value = pd.to_datetime(ts_int, unit=unit, utc=True)
            rows.append({
                "timestamp": ts_value,
                "open": round(float(bar.get("o") or bar.get("open", 0) or 0), 2),
                "high": round(float(bar.get("h") or bar.get("high", 0) or 0), 2),
                "low": round(float(bar.get("l") or bar.get("low", 0) or 0), 2),
                "close": round(float(bar.get("c") or bar.get("close", 0) or 0), 2),
                "volume": int(float(bar.get("v") or bar.get("volume", 0) or 0)),
            })
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)

    def _fetch_bars_sdk(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        timespan = self.SDK_TIMESPAN_MAP.get(timeframe)
        if timespan is None:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        kwargs = dict(count=min(count, 999))
        response = self._sdk_data_client.market_data.get_history_bar(
            symbol,
            self._category_for(symbol),
            timespan,
            **kwargs,
        )
        try:
            payload = response.json()
        except Exception:
            payload = response if isinstance(response, (dict, list)) else {}
        return self._parse_bars_payload(payload)

    def fetch_bars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        self._rate_limit_wait()

        if self._sdk_available:
            try:
                return self._fetch_bars_sdk(symbol, timeframe, count)
            except Exception as e:
                logging.debug(f"Webull SDK fetch failed for {symbol} {timeframe}: {e}")
                return pd.DataFrame()

        timespan = WEBULL_TIMESPAN_MAP.get(timeframe)
        if timespan is None:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        path = "/openapi/market-data/stock/history-bars"
        params = {
            "symbol": symbol,
            "category": self._category_for(symbol),
            "timespan": timespan,
            "count": min(count, 999),
        }
        query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        url = f"{self.BASE_URL}{path}?{query}"

        try:
            resp = requests.get(
                url,
                headers=self._headers("GET", path, query=query),
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logging.debug(f"Webull fetch failed for {symbol} {timeframe}: {e}")
            return pd.DataFrame()

        # Webull returns numeric values as strings to preserve precision.
        rows = []
        for bar in data:
            rows.append({
                "timestamp": pd.to_datetime(int(bar["time"]), unit="ms", utc=True),
                "open":   round(float(bar["open"]), 2),
                "high":   round(float(bar["high"]), 2),
                "low":    round(float(bar["low"]), 2),
                "close":  round(float(bar["close"]), 2),
                "volume": int(bar.get("volume", 0)),
            })
        return pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)


class MockClient(DataClient):
    """Deterministic synthetic data for development.

    Generates plausible OHLC walks seeded by symbol so output is reproducible.
    Engineered to occasionally produce exact OHLC=SMA hits — otherwise the
    detection logic has nothing to fire on at 2-decimal precision.
    """

    def __init__(self, seed_offset: int = 0):
        self._seed_offset = seed_offset

    def fetch_bars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        seed = (hash(symbol) + self._seed_offset) % (2**32)
        rng = np.random.default_rng(seed)

        n = min(count, 999)
        # Per-symbol price scale so XLE != QQQ != BTC
        base_price = 20 + (hash(symbol) % 400)

        # Geometric Brownian-ish walk with mild drift and noise
        returns = rng.normal(loc=0.0001, scale=0.008, size=n)
        prices = base_price * np.exp(np.cumsum(returns))

        # Build OHLC with intra-bar volatility
        opens  = prices.copy()
        closes = prices * (1 + rng.normal(0, 0.003, n))
        highs  = np.maximum(opens, closes) * (1 + np.abs(rng.normal(0, 0.004, n)))
        lows   = np.minimum(opens, closes) * (1 - np.abs(rng.normal(0, 0.004, n)))
        vols   = rng.integers(100_000, 10_000_000, size=n)

        # Snap to 2 decimals — this is what enables exact hit detection
        opens, highs, lows, closes = (np.round(x, 2) for x in (opens, highs, lows, closes))

        # Timestamps: walk back from now at the timeframe interval
        interval_minutes = {
            "1m": 1, "2m": 2, "3m": 3, "5m": 5, "10m": 10, "15m": 15,
            "20m": 20, "30m": 30, "1h": 60, "2h": 120, "4h": 240,
            "1d": 1440, "1w": 10080, "1mo": 43200,
        }.get(timeframe, 5)
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        timestamps = [end - timedelta(minutes=interval_minutes * (n - 1 - i)) for i in range(n)]

        return pd.DataFrame({
            "timestamp": pd.to_datetime(timestamps, utc=True),
            "open": opens, "high": highs, "low": lows, "close": closes,
            "volume": vols,
        })


# ═══════════════════════════════════════════════════════════════════════════════
# 3. SMA COMPUTATION
# ═══════════════════════════════════════════════════════════════════════════════

def compute_smas(closes: np.ndarray, periods: list[int]) -> dict[int, np.ndarray]:
    """Vectorized SMA computation. Returns {period: sma_array}.

    SMA at index i = mean of closes[i-period+1 : i+1], rounded to 2 decimals.
    Indices < period-1 are NaN.
    """
    n = len(closes)
    smas: dict[int, np.ndarray] = {}
    for p in periods:
        if p > n:
            smas[p] = np.full(n, np.nan)
            continue
        # Cumulative-sum trick for O(n) SMA
        cs = np.concatenate([[0], np.cumsum(closes)])
        sma = (cs[p:] - cs[:-p]) / p
        sma = np.concatenate([np.full(p - 1, np.nan), sma])
        smas[p] = np.round(sma, 2)
    return smas


# ═══════════════════════════════════════════════════════════════════════════════
# 4. HIT DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Hit:
    ticker: str
    timeframe: str
    outfit_id: int
    outfit_periods: tuple[int, ...]
    bar_index: int
    timestamp: pd.Timestamp
    ohlc_component: str         # 'O' | 'H' | 'L' | 'C' | 'W'
    sma_period: int
    price: float
    sma_value: float = 0.0      # actual SMA price at time of hit — the "parm" (e.g. MA184@16.79)
    deciseconds: float = 0.0    # candle duration in deciseconds for time-series scoring


def detect_hits(
    df: pd.DataFrame,
    ticker: str,
    timeframe: str,
    outfit: dict,
    lookback: int,
    hit_mode: str = "exact",
    hit_tolerance: float = 0.0,
) -> list[Hit]:
    """
    Detect SMA hits over the last `lookback` bars.

    hit_mode:
      "exact"  — SMA must equal O/H/L/C at 2 decimal places (component: O/H/L/C).
      "wick"   — SMA must fall within candle's [Low, High] range (component: W),
                 plus O/C proximity within hit_tolerance dollars (component: O/C).
      "both"   — Both exact AND wick checks run; all passing conditions produce hits.

    hit_tolerance:
      Dollar distance for Open/Close proximity check in wick/both modes.
      0.0 = disabled. 0.01 = 1-cent tolerance (March engine default).

    Vectorized: inner candle loop replaced with numpy array comparisons so all
    130 bars are evaluated simultaneously in C rather than one at a time in Python.
    Produces identical results to the scalar version, ~10-50x faster per combo.
    """
    if len(df) == 0:
        return []

    closes = df["close"].to_numpy()
    smas = compute_smas(closes, outfit["periods"])

    start = max(0, len(df) - lookback)
    hits: list[Hit] = []

    low_arr    = df["low"].to_numpy()
    high_arr   = df["high"].to_numpy()
    open_arr   = df["open"].to_numpy()
    close_arr  = df["close"].to_numpy()
    timestamps = df["timestamp"].to_numpy()  # extracted once; avoids iloc in loop

    ds           = TF_DECISECONDS.get(timeframe, 600)
    outfit_id    = outfit["id"]
    outfit_perds = tuple(outfit["periods"])

    for period, sma_arr in smas.items():
        # Slice everything to the lookback window once per period
        sma_w   = sma_arr[start:]
        open_w  = open_arr[start:]
        high_w  = high_arr[start:]
        low_w   = low_arr[start:]
        close_w = close_arr[start:]
        ts_w    = timestamps[start:]

        # valid mask: exclude NaN SMA values (insufficient history)
        valid = ~np.isnan(sma_w)

        # ── Exact mode: O/H/L/C == SMA at 2dp ───────────────────────────────
        if hit_mode in ("exact", "both"):
            for comp, arr_w in (("O", open_w), ("H", high_w), ("L", low_w), ("C", close_w)):
                # Single numpy call replaces the entire inner for-loop
                for idx in np.where(valid & (arr_w == sma_w))[0]:
                    hits.append(Hit(
                        ticker=ticker,
                        timeframe=timeframe,
                        outfit_id=outfit_id,
                        outfit_periods=outfit_perds,
                        bar_index=start + int(idx),
                        timestamp=ts_w[idx],
                        ohlc_component=comp,
                        sma_period=period,
                        price=float(arr_w[idx]),
                        sma_value=float(sma_w[idx]),
                        deciseconds=ds,
                    ))

        # ── Wick mode: SMA within candle range + O/C proximity ───────────────
        if hit_mode in ("wick", "both"):
            for idx in np.where(valid & (low_w <= sma_w) & (sma_w <= high_w))[0]:
                hits.append(Hit(
                    ticker=ticker,
                    timeframe=timeframe,
                    outfit_id=outfit_id,
                    outfit_periods=outfit_perds,
                    bar_index=start + int(idx),
                    timestamp=ts_w[idx],
                    ohlc_component="W",
                    sma_period=period,
                    price=float(sma_w[idx]),
                    sma_value=float(sma_w[idx]),
                    deciseconds=ds,
                ))

            if hit_tolerance > 0.0:
                for comp, arr_w in (("O", open_w), ("C", close_w)):
                    for idx in np.where(valid & (np.abs(arr_w - sma_w) <= hit_tolerance))[0]:
                        hits.append(Hit(
                            ticker=ticker,
                            timeframe=timeframe,
                            outfit_id=outfit_id,
                            outfit_periods=outfit_perds,
                            bar_index=start + int(idx),
                            timestamp=ts_w[idx],
                            ohlc_component=comp,
                            sma_period=period,
                            price=float(arr_w[idx]),
                            sma_value=float(sma_w[idx]),
                            deciseconds=ds,
                        ))

    return hits


# ═══════════════════════════════════════════════════════════════════════════════
# 5. HASH MAP STORE — [ticker | tf | outfit] → aggregated stats
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class HashMapEntry:
    ticker: str
    timeframe: str
    outfit_id: int
    outfit_periods: tuple[int, ...]
    hit_count: int = 0
    hits_by_component: dict = field(default_factory=lambda: {"O": 0, "H": 0, "L": 0, "C": 0, "W": 0})
    hits_by_period: dict = field(default_factory=dict)
    last_hit_ts: Optional[pd.Timestamp] = None
    last_hit_price: Optional[float] = None
    hits: list[Hit] = field(default_factory=list)
    # ── Time-series scoring ───────────────────────────────────────────────────
    decisecond_score: float = 0.0           # cumulative deciseconds across all hits
    hits_by_period_ds: dict = field(default_factory=dict)  # deciseconds per SMA period
    key_variable: Optional[int] = None      # SMA period with most deciseconds ("key variable")
    key_variable_price: Optional[float] = None  # SMA price of key variable at last hit

    def add_hit(self, h: Hit) -> None:
        self.hit_count += 1
        # Use get/set pattern so any new component (e.g. 'W') is handled gracefully
        self.hits_by_component[h.ohlc_component] = self.hits_by_component.get(h.ohlc_component, 0) + 1
        self.hits_by_period[h.sma_period] = self.hits_by_period.get(h.sma_period, 0) + 1
        # Accumulate deciseconds for time-series scoring
        self.decisecond_score += h.deciseconds
        self.hits_by_period_ds[h.sma_period] = (
            self.hits_by_period_ds.get(h.sma_period, 0.0) + h.deciseconds
        )
        # Update key variable — whichever SMA period has accumulated the most deciseconds
        self.key_variable = max(self.hits_by_period_ds, key=self.hits_by_period_ds.get)
        if self.last_hit_ts is None or h.timestamp > self.last_hit_ts:
            self.last_hit_ts = h.timestamp
            self.last_hit_price = h.price
        # Track the key variable's current SMA price for parm output
        if h.sma_period == self.key_variable:
            self.key_variable_price = h.sma_value
        self.hits.append(h)

    @property
    def key(self) -> str:
        periods_str = "/".join(str(p) for p in self.outfit_periods)
        return f"{self.ticker}|{self.timeframe}|{periods_str}"


class HashMapStore:
    """In-memory keyed store. Swap for InfluxDB by re-implementing get/put/all."""

    def __init__(self):
        self._store: dict[str, HashMapEntry] = {}

    def _key(self, ticker: str, tf: str, outfit_periods: tuple) -> str:
        periods_str = "/".join(str(p) for p in outfit_periods)
        return f"{ticker}|{tf}|{periods_str}"

    def add_hits(self, hits: list[Hit]) -> None:
        for h in hits:
            k = self._key(h.ticker, h.timeframe, h.outfit_periods)
            if k not in self._store:
                self._store[k] = HashMapEntry(
                    ticker=h.ticker,
                    timeframe=h.timeframe,
                    outfit_id=h.outfit_id,
                    outfit_periods=h.outfit_periods,
                )
            self._store[k].add_hit(h)

    def prune(self, keep: int = 50_000) -> None:
        """Evict lowest-scoring entries to cap memory usage.

        Called periodically during long scans. Keeps the top `keep` entries
        ranked by hit_count (cheap proxy for rank_score during mid-scan pruning).
        """
        if len(self._store) <= keep:
            return
        ranked = sorted(self._store.items(),
                        key=lambda kv: kv[1].hit_count, reverse=True)
        self._store = dict(ranked[:keep])

    def all(self) -> list[HashMapEntry]:
        return list(self._store.values())

    def __len__(self) -> int:
        return len(self._store)


def _scan_worker_init():
    """Ignore SIGTERM/SIGINT in worker processes.

    Workers are forked from the parent and inherit its signal handlers.
    Without this, pool.terminate() sends SIGTERM to workers which triggers
    the parent's handle_signal via inherited handler, causing double logging
    and premature shutdown. Workers should just run their task and exit.
    """
    import signal as _signal
    _signal.signal(_signal.SIGTERM, _signal.SIG_IGN)
    _signal.signal(_signal.SIGINT, _signal.SIG_IGN)


def _scan_worker_fn(args: tuple) -> HashMapStore:
    """Module-level worker for multiprocessing scan.

    Must be module-level (not a nested function or lambda) so that Python's
    multiprocessing can pickle it when spawning worker processes.

    Args:
        args: (ticker_chunk, cache_slice, outfits, all_tfs,
               lookback, hit_mode, hit_tolerance)

    Returns:
        A populated HashMapStore containing all hits for the ticker chunk.
    """
    ticker_chunk, cache_slice, outfits, all_tfs, lookback, hit_mode, hit_tolerance = args
    store = HashMapStore()
    local_scanned = 0
    next_prune = 2_000

    for ticker in ticker_chunk:
        for tf in all_tfs:
            df = cache_slice.get((ticker, tf), pd.DataFrame())
            if len(df) == 0:
                local_scanned += len(outfits)
            else:
                for outfit in outfits:
                    hits = detect_hits(
                        df, ticker, tf, outfit, lookback,
                        hit_mode=hit_mode,
                        hit_tolerance=hit_tolerance,
                    )
                    if hits:
                        store.add_hits(hits)
                    local_scanned += 1
            if local_scanned >= next_prune:
                store.prune(keep=7_000)
                next_prune = local_scanned + 2_000

    return store


# ═══════════════════════════════════════════════════════════════════════════════
# 6. RANKING ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def freshness_score(last_ts: pd.Timestamp, now: pd.Timestamp, tf_minutes: float) -> float:
    """Recent hits weighted higher. Decays over ~50 bars of the timeframe."""
    if last_ts is None:
        return 0.0
    age_minutes = (now - last_ts).total_seconds() / 60
    decay_window = tf_minutes * 50
    return max(0.0, 1.0 - (age_minutes / decay_window))


TF_MINUTES = {
    "1m": 1, "2m": 2, "3m": 3, "5m": 5, "10m": 10, "15m": 15,
    "20m": 20, "30m": 30, "1h": 60, "2h": 120, "4h": 240,
    "1d": 1440, "1w": 10080, "1mo": 43200,
}

# Candle duration in deciseconds (tenths of a second) per timeframe.
# Used for time-series scoring: each hit weighted by how long the candle lasted.
# Higher timeframes accumulate deciseconds ~proportionally, so 1d hits naturally
# outrank 1m hits without any manual weighting factor.
TF_DECISECONDS = {
    "1s": 10,        "5s": 50,        "15s": 150,      "30s": 300,
    "1m": 600,       "2m": 1200,      "3m": 1800,      "5m": 3000,
    "10m": 6000,     "15m": 9000,     "20m": 12000,    "30m": 18000,
    "1h": 36000,     "2h": 72000,     "4h": 144000,
    "1d": 864000,    "1w": 6048000,   "1mo": 25920000,
}


def rank_entries(
    store: HashMapStore,
    now: pd.Timestamp,
    weight_freshness: float = 0.3,
    min_tf_minutes: int = 0,
    cumulative_ds: Optional[dict] = None,
) -> list[tuple[HashMapEntry, float]]:
    """
    Score = base_ds × (1 + weight × freshness).

    base_ds is the cumulative decisecond total from InfluxDB when available
    (blending this cycle + prior cycles over the query window), falling back
    to the in-cycle decisecond_score if Influx is unreachable.

    decisecond_score naturally weights higher timeframes: a single 1d hit
    (864,000 ds) outscores ~1,440 raw 1m hits, so flat low-price stocks
    accumulating 1m noise never top the leaderboard.

    min_tf_minutes: microterm filter — entries on timeframes below this
    threshold are excluded entirely (e.g. 15 drops all 1m/5m entries).
    Default 0 = no filter (all timeframes ranked).
    """
    scored: list[tuple[HashMapEntry, float]] = []
    for e in store.all():
        tf_mins = TF_MINUTES.get(e.timeframe, 5)
        if min_tf_minutes > 0 and tf_mins < min_tf_minutes:
            continue
        f = freshness_score(e.last_hit_ts, now, tf_mins)
        # Use cumulative InfluxDB score if available — this is Raul's key insight:
        # deciseconds accumulate across cycles and sessions, so a level visited
        # repeatedly over days scores higher than one hit heavily in one cycle.
        if cumulative_ds and e.key_variable is not None:
            # Sum all periods' cumulative ds for this entry (ticker/tf/outfit)
            base_ds = sum(
                cumulative_ds.get((e.ticker, e.timeframe, str(e.outfit_id), str(p)), 0.0)
                for p in e.hits_by_period_ds
            )
            # Fall back to in-cycle score if nothing in Influx yet (first run)
            if base_ds == 0.0:
                base_ds = e.decisecond_score
        else:
            base_ds = e.decisecond_score
        score = base_ds * (1 + weight_freshness * f)
        scored.append((e, score))
    return sorted(scored, key=lambda x: x[1], reverse=True)


# ═══════════════════════════════════════════════════════════════════════════════
# 7. CONVERGENCE DETECTION (a/b/c/d layers)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Convergence:
    ohlc_detection: bool = False     # (a) hit frequency on primary tf
    time_series: bool = False         # (b) decisecond — stub
    parm_price: bool = False          # (c) cross-tf parm match — partial
    candle_close: bool = False        # (d) close above/below specific MA on primary tf

    @property
    def score(self) -> str:
        n = sum([self.ohlc_detection, self.time_series, self.parm_price, self.candle_close])
        return f"{n}/4"


def detect_convergence(
    entry: HashMapEntry,
    df: pd.DataFrame,
    store: Optional[HashMapStore] = None,
    cross_tf_store: Optional[HashMapStore] = None,
    cumulative_ds: Optional[dict] = None,
) -> Convergence:
    """
    Four-layer convergence detection:
      (a) ohlc_detection  — entry has hits at all
      (b) time_series     — this entry's decisecond score is disproportionate vs.
                            siblings (same ticker, same TF, different outfit).
                            Uses cumulative InfluxDB scores when available,
                            falls back to in-cycle scores.
      (c) parm_price      — same outfit on a different timeframe points to the
                            same key-variable SMA price (within $0.02).
      (d) candle_close    — last candle close sits within 0.05% of the key
                            variable SMA (most deciseconds, not just most hits).
    """
    c = Convergence()

    # (a) OHLC detection — basic: any hits recorded
    c.ohlc_detection = entry.hit_count > 0

    # (d) Candle close — use key_variable (most deciseconds) not most_hit_period
    kv = entry.key_variable
    if kv and len(df) > 0:
        smas = compute_smas(df["close"].to_numpy(), [kv])
        last_sma = smas[kv][-1]
        last_close = df["close"].iloc[-1]
        if not np.isnan(last_sma) and last_sma > 0:
            if abs(last_close - last_sma) / last_sma < 0.0005:  # 0.05% tolerance
                c.candle_close = True

    # (c) Cross-TF parm match — same outfit, same ticker, different TF,
    #     key_variable_price within $0.02
    ref_price = entry.key_variable_price or entry.last_hit_price
    if cross_tf_store is not None and ref_price is not None:
        for other in cross_tf_store.all():
            other_price = other.key_variable_price or other.last_hit_price
            if (other.ticker == entry.ticker
                    and other.outfit_periods == entry.outfit_periods
                    and other.timeframe != entry.timeframe
                    and other_price is not None
                    and abs(other_price - ref_price) < 0.02):
                c.parm_price = True
                break

    # (b) Time-series disproportionality
    # Get the total decisecond score for an entry from cumulative InfluxDB data
    # (sum across all SMA periods for that ticker/TF/outfit), or fall back to
    # the in-cycle decisecond_score when Influx data isn't available yet.
    def _get_score(e: HashMapEntry) -> float:
        if cumulative_ds and e.hits_by_period_ds:
            total = sum(
                cumulative_ds.get((e.ticker, e.timeframe, str(e.outfit_id), str(p)), 0.0)
                for p in e.hits_by_period_ds
            )
            return total if total > 0.0 else e.decisecond_score
        return e.decisecond_score

    this_score = _get_score(entry)
    if store is not None and this_score > 0:
        # Compare against siblings: same ticker, same TF, different outfit
        sibling_scores = [
            _get_score(e) for e in store.all()
            if e.ticker == entry.ticker
            and e.timeframe == entry.timeframe
            and e.outfit_periods != entry.outfit_periods
            and _get_score(e) > 0
        ]
        if sibling_scores:
            mean_sibling = sum(sibling_scores) / len(sibling_scores)
            if mean_sibling > 0 and this_score >= 2.0 * mean_sibling:
                c.time_series = True
        else:
            # No siblings — any positive score counts
            c.time_series = this_score > 0
    else:
        c.time_series = this_score > 0

    return c


# ═══════════════════════════════════════════════════════════════════════════════
# 8. SYSTEM MONITOR — 8 systems, parallel module
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class SystemState:
    name: str
    proxy: str
    timeframe: str
    state: str  # 'positive' | 'negative' | 'unknown'
    fast_value: Optional[float] = None
    slow_value: Optional[float] = None
    note: str = ""


def evaluate_systems(client: DataClient) -> list[SystemState]:
    """Compute current +/- state for each of the 8 systems."""
    states: list[SystemState] = []
    for sys_def in SYSTEMS:
        try:
            df = client.fetch_bars(sys_def["proxy"], sys_def["tf"], count=max(sys_def["slow"] * 2, 200))
            if len(df) < sys_def["slow"]:
                states.append(SystemState(
                    name=sys_def["name"], proxy=sys_def["proxy"],
                    timeframe=sys_def["tf"], state="unknown",
                    note="insufficient data",
                ))
                continue
            smas = compute_smas(df["close"].to_numpy(), [sys_def["fast"], sys_def["slow"]])
            fast_v = float(smas[sys_def["fast"]][-1])
            slow_v = float(smas[sys_def["slow"]][-1])
            state = "positive" if fast_v > slow_v else "negative"
            states.append(SystemState(
                name=sys_def["name"], proxy=sys_def["proxy"],
                timeframe=sys_def["tf"], state=state,
                fast_value=round(fast_v, 2), slow_value=round(slow_v, 2),
                note=f"MA{sys_def['fast']} {'>' if state == 'positive' else '<'} MA{sys_def['slow']}",
            ))
        except Exception as e:
            states.append(SystemState(
                name=sys_def["name"], proxy=sys_def["proxy"],
                timeframe=sys_def["tf"], state="unknown",
                note=f"error: {e}",
            ))
    return states


# ═══════════════════════════════════════════════════════════════════════════════
# 9. OFFSET TESTING — ±0.01 / ±0.02 variants on top-ranked combos
# ═══════════════════════════════════════════════════════════════════════════════

def best_offset(entry: HashMapEntry, df: pd.DataFrame) -> tuple[float, float]:
    """Try raw, ±0.01, ±0.02 offsets against the most-hit period.
    Returns (best_offset, entry_price)."""
    if not entry.hits_by_period or len(df) == 0:
        return 0.0, entry.last_hit_price or 0.0

    most_hit_period = max(entry.hits_by_period, key=entry.hits_by_period.get)
    smas = compute_smas(df["close"].to_numpy(), [most_hit_period])
    sma_arr = smas[most_hit_period]

    candidates = [-0.02, -0.01, 0.0, 0.01, 0.02]
    best_off, best_count = 0.0, -1
    ohlc = {"O": df["open"].to_numpy(), "H": df["high"].to_numpy(),
            "L": df["low"].to_numpy(), "C": df["close"].to_numpy()}

    for off in candidates:
        target = np.round(sma_arr + off, 2)
        count = 0
        for arr in ohlc.values():
            count += int(np.sum(arr == target))
        if count > best_count:
            best_count = count
            best_off = off

    last_sma = sma_arr[-1]
    entry_price = round(float(last_sma) + best_off, 2) if not np.isnan(last_sma) else (entry.last_hit_price or 0.0)
    return best_off, entry_price


# ═══════════════════════════════════════════════════════════════════════════════
# 10. ENGINE ORCHESTRATION
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class EngineConfig:
    universe: list[str]
    timeframes: list[str] = field(default_factory=lambda: ["5m", "15m", "30m", "1h", "1d"])
    outfits: list[dict] = field(default_factory=lambda: OUTFITS)
    lookback: int = 130
    candle_count: int = 999
    discovery_mode: bool = False
    # ── Hit detection mode ────────────────────────────────────────────────────
    # "exact"  — SMA must exactly equal O/H/L/C at 2 decimal places (default).
    #            Most precise; flags only clear price-level confluences.
    # "wick"   — SMA must fall within the candle's [Low, High] range (wick touch),
    #            OR within hit_tolerance dollars of Open/Close.
    #            More hits; useful for catching near-misses in fast markets.
    # "both"   — Both exact AND wick checks run; either type produces a hit.
    #            Maximum sensitivity.
    hit_mode: str = "exact"
    # ── Tolerance (dollars) for Open/Close proximity in wick/both modes ───────
    # 0.0 = disabled (only wick range check applies).
    # 0.01 = flag O/C within 1 cent of the SMA (matches March engine default).
    hit_tolerance: float = 0.0
    # ── Sub-minute timeframes from streaming (populated by daemon.py) ─────────
    stream_timeframes: list[str] = field(default_factory=list)
    # ── Incremental fetch: bars to pull for already-cached (ticker, tf) pairs ─
    # On cycle 2+, only this many recent bars are fetched and merged into the
    # existing cache instead of re-fetching all 999. Massively reduces API load.
    # 0 = disabled (always fetch full candle_count, legacy behaviour).
    refresh_bars: int = 20
    # ── Microterm filter: minimum timeframe for ranking ───────────────────────
    # Entries on timeframes shorter than this are excluded from ranking entirely.
    # Prevents 1m noise (flat low-price stocks accumulating thousands of hits)
    # from crowding out meaningful higher-timeframe signals.
    # 0 = no filter (all timeframes ranked, legacy behaviour).
    # 15 = ignore 1m/5m — only 15m and above appear in rankings.
    min_tf_minutes: int = 0


class SMAOutfitEngine:
    # Persisted inside the engine_cache Docker volume — survives restarts
    DEAD_TICKERS_PATH = "/cache/dead_tickers.txt"

    def __init__(
        self,
        client: DataClient,
        cfg: EngineConfig,
        stream_client=None,
        initial_cache: dict | None = None,
    ):
        self.client = client
        self.cfg = cfg
        self.store = HashMapStore()
        self.system_states: list[SystemState] = []
        # Seed with persistent cache from daemon so incremental fetch works
        self.candle_cache: dict[tuple[str, str], pd.DataFrame] = dict(initial_cache or {})
        self._dead_tickers: set[str] = self._load_dead_tickers()
        self._stream_client = stream_client  # WebullStreamClient | None

    def _load_dead_tickers(self) -> set[str]:
        """Load persistently known-dead tickers from disk."""
        try:
            with open(self.DEAD_TICKERS_PATH) as f:
                dead = {line.strip() for line in f if line.strip()}
            if dead:
                logging.info(f"  skipping {len(dead)} known-dead tickers (cached)")
            return dead
        except FileNotFoundError:
            return set()

    def _save_dead_tickers(self) -> None:
        """Persist dead-ticker set so restarts skip them immediately."""
        try:
            os.makedirs(os.path.dirname(self.DEAD_TICKERS_PATH), exist_ok=True)
            with open(self.DEAD_TICKERS_PATH, "w") as f:
                for t in sorted(self._dead_tickers):
                    f.write(t + "\n")
        except Exception as e:
            logging.warning(f"Could not save dead tickers: {e}")

    def _fetch_with_cache(self, ticker: str, tf: str) -> pd.DataFrame:
        key = (ticker, tf)
        if key not in self.candle_cache:
            df = self.client.fetch_bars(ticker, tf, self.cfg.candle_count)
            self.candle_cache[key] = df
        return self.candle_cache[key]

    @staticmethod
    def _merge_bars(existing: pd.DataFrame, new_bars: pd.DataFrame, keep: int = 999) -> pd.DataFrame:
        """Merge new bars into an existing DataFrame, dedup by timestamp, keep last N rows."""
        if existing.empty:
            return new_bars
        if new_bars.empty:
            return existing
        combined = pd.concat([existing, new_bars], ignore_index=True)
        combined = combined.drop_duplicates(subset=["timestamp"])
        combined = combined.sort_values("timestamp").reset_index(drop=True)
        return combined.tail(keep).reset_index(drop=True)

    def _prefetch_candles(self) -> None:
        """Parallel candle cache population using ThreadPoolExecutor.

        On first run (or when refresh_bars=0), fetches full candle_count bars
        for every pair. On subsequent cycles, already-cached pairs only pull
        refresh_bars recent bars and merge them in — dramatically reducing API
        load from ~17,000 full fetches down to ~17,000 tiny top-ups.

        Tickers that fail on every timeframe are marked dead and persisted to
        disk so future restarts skip them silently.
        """
        use_incremental = self.cfg.refresh_bars > 0

        # Split into cold (never fetched) and warm (already cached) pairs
        cold_pairs = []
        warm_pairs = []
        for ticker in self.cfg.universe:
            if ticker in self._dead_tickers:
                continue
            for tf in self.cfg.timeframes:
                key = (ticker, tf)
                if key in self.candle_cache and not self.candle_cache[key].empty:
                    if use_incremental:
                        warm_pairs.append(key)
                    # else: already cached + no incremental → skip re-fetch
                else:
                    cold_pairs.append(key)

        total_pairs = len(cold_pairs) + len(warm_pairs)
        if total_pairs == 0:
            return

        max_workers = getattr(self.client, "MAX_WORKERS", 1)

        if use_incremental and warm_pairs:
            logging.info(
                f"  prefetching {len(cold_pairs):,} cold + "
                f"{len(warm_pairs):,} warm (refresh={self.cfg.refresh_bars} bars) pairs "
                f"({max_workers} workers)..."
            )
        else:
            logging.info(
                f"  prefetching {len(cold_pairs):,} candle series "
                f"({max_workers} workers)..."
            )

        def fetch_one(args: tuple) -> tuple:
            ticker, tf = args
            count = self.cfg.refresh_bars if (use_incremental and (ticker, tf) in self.candle_cache) \
                    else self.cfg.candle_count
            df = self.client.fetch_bars(ticker, tf, count)
            return ticker, tf, df

        all_pairs = cold_pairs + warm_pairs
        ticker_attempts: dict[str, int] = {}
        ticker_failures: dict[str, int] = {}
        for ticker, _ in all_pairs:
            ticker_attempts[ticker] = ticker_attempts.get(ticker, 0) + 1

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(fetch_one, pair): pair for pair in all_pairs}
            done = 0
            for future in as_completed(futures):
                try:
                    ticker, tf, df = future.result()
                    key = (ticker, tf)
                    if use_incremental and key in self.candle_cache:
                        # Warm pair — merge new bars into existing cache
                        self.candle_cache[key] = self._merge_bars(
                            self.candle_cache[key], df, keep=self.cfg.candle_count
                        )
                    else:
                        # Cold pair — store fresh
                        self.candle_cache[key] = df
                except Exception as e:
                    ticker, tf = futures[future]
                    if (ticker, tf) not in self.candle_cache:
                        self.candle_cache[(ticker, tf)] = pd.DataFrame()
                    ticker_failures[ticker] = ticker_failures.get(ticker, 0) + 1
                    if ticker_failures[ticker] == 1:
                        logging.warning(f"  bad ticker: {ticker} — {e}")
                done += 1
                if done % 200 == 0:
                    logging.info(f"  prefetched {done:,}/{total_pairs:,}")

        # Tickers that failed on every timeframe are dead — persist them
        new_dead = {t for t, f in ticker_failures.items()
                    if f >= ticker_attempts.get(t, 1)}
        if new_dead:
            self._dead_tickers.update(new_dead)
            self._save_dead_tickers()
            logging.warning(f"  marked {len(new_dead)} dead tickers "
                            f"(won't retry): {', '.join(sorted(new_dead))}")

    def scan(self) -> None:
        """Run full detection pass over universe × timeframes × outfits.

        If a stream_client is attached, sub-minute candles are injected into
        candle_cache before the REST scan runs, and stream_timeframes are
        appended to the scan loop automatically.
        """
        # ── Inject sub-minute candles from stream client ──────────────────────
        active_stream_tfs: list[str] = []
        if self._stream_client is not None and self.cfg.stream_timeframes:
            for ticker in self.cfg.universe:
                for tf in self.cfg.stream_timeframes:
                    df = self._stream_client.get_candles(ticker, tf)
                    if not df.empty:
                        self.candle_cache[(ticker, tf)] = df
                        if tf not in active_stream_tfs:
                            active_stream_tfs.append(tf)
            if active_stream_tfs:
                logging.info(
                    f"  stream: injected candles for "
                    f"{len(active_stream_tfs)} sub-minute tf(s): "
                    f"{active_stream_tfs}"
                )

        self._prefetch_candles()

        # Combine REST timeframes + any active stream timeframes
        all_tfs = self.cfg.timeframes + active_stream_tfs

        total = len(self.cfg.universe) * len(all_tfs) * len(self.cfg.outfits)
        logging.info(f"Scanning {total:,} combinations "
                     f"({len(self.cfg.universe)} tickers × "
                     f"{len(all_tfs)} tfs × "
                     f"{len(self.cfg.outfits)} outfits) "
                     f"[hit_mode={self.cfg.hit_mode}, tol={self.cfg.hit_tolerance}]")

        # ── Parallel scan across tickers (multiprocessing — bypasses the GIL) ──
        n_workers = int(os.environ.get("ENGINE_SCAN_WORKERS", "6"))
        tickers = list(self.cfg.universe)
        chunk_size = math.ceil(len(tickers) / n_workers)
        ticker_chunks = [tickers[i:i + chunk_size] for i in range(0, len(tickers), chunk_size)]

        # Build a cache slice for each worker — only the tickers it needs.
        # This avoids pickling the full cache to every process.
        worker_args = []
        for chunk in ticker_chunks:
            chunk_set = set(chunk)
            cache_slice = {
                (t, tf): df
                for (t, tf), df in self.candle_cache.items()
                if t in chunk_set
            }
            worker_args.append((
                chunk, cache_slice, self.cfg.outfits, all_tfs,
                self.cfg.lookback, self.cfg.hit_mode, self.cfg.hit_tolerance,
            ))

        n_chunks = len(ticker_chunks)
        logging.info(f"  launching {n_chunks} worker processes (ENGINE_SCAN_WORKERS={n_workers})...")
        worker_stores: list[HashMapStore] = []
        scan_start = time.monotonic()
        with multiprocessing.Pool(processes=n_workers, initializer=_scan_worker_init) as pool:
            for i, result in enumerate(pool.imap_unordered(_scan_worker_fn, worker_args), 1):
                worker_stores.append(result)
                elapsed = time.monotonic() - scan_start
                pct = i / n_chunks * 100
                eta = (elapsed / i) * (n_chunks - i) if i < n_chunks else 0
                logging.info(
                    f"  scan {i}/{n_chunks} workers done "
                    f"({pct:.0f}%) — {elapsed:.0f}s elapsed, "
                    f"~{eta:.0f}s remaining"
                )

        # Merge all worker stores into main store
        logging.info(f"  merging {len(worker_stores)} worker stores...")
        for worker_store in worker_stores:
            for entry in worker_store.all():
                k = entry.key
                if k not in self.store._store:
                    self.store._store[k] = entry
                else:
                    existing = self.store._store[k]
                    existing.hit_count += entry.hit_count
                    ts_a = existing.last_hit_ts
                    ts_b = entry.last_hit_ts
                    if ts_a is None:
                        existing.last_hit_ts = ts_b
                    elif ts_b is not None:
                        existing.last_hit_ts = ts_a if ts_a > ts_b else ts_b
        logging.info(f"  merge complete: {len(self.store)} total combos")

    def monitor_systems(self) -> None:
        self.system_states = evaluate_systems(self.client)

    def top_signal(self, cumulative_ds: Optional[dict] = None) -> Optional[dict]:
        """
        Return the top-ranked signal after applying multi-condition gating.

        Ranking uses decisecond_score (blended with cumulative InfluxDB scores
        when provided). The final signal is selected by convergence-weighted score:
            gated_score = decisecond_score × (1 + conv_layers)
        so a 3/4 convergence entry beats a 1/4 entry even with a lower raw score,
        as long as the gap isn't extreme. This prevents a single noisy high-score
        entry from overriding a genuinely confirmed signal.
        """
        ranked = rank_entries(self.store, pd.Timestamp.now(tz="UTC"),
                              min_tf_minutes=self.cfg.min_tf_minutes,
                              cumulative_ds=cumulative_ds)
        if not ranked:
            return None

        # Compute convergence for top-50 candidates, then re-rank by gated score
        candidates = []
        for entry, score in ranked[:50]:
            df = self.candle_cache.get((entry.ticker, entry.timeframe), pd.DataFrame())
            conv = detect_convergence(entry, df,
                                      store=self.store,
                                      cross_tf_store=self.store,
                                      cumulative_ds=cumulative_ds)
            conv_layers = sum([conv.ohlc_detection, conv.time_series,
                               conv.parm_price, conv.candle_close])
            gated = score * (1 + conv_layers)
            candidates.append((entry, score, conv, gated))

        candidates.sort(key=lambda x: x[3], reverse=True)
        entry, score, conv, _ = candidates[0]

        df = self.candle_cache.get((entry.ticker, entry.timeframe), pd.DataFrame())
        offset, entry_price = best_offset(entry, df)

        return {
            "rank": 1,
            "ticker": entry.ticker,
            "timeframe": entry.timeframe,
            "outfit_id": entry.outfit_id,
            "outfit_periods": list(entry.outfit_periods),
            "outfit_name": next((o["name"] for o in OUTFITS if o["id"] == entry.outfit_id), ""),
            "entry_price": entry_price,
            "offset_applied": offset,
            "risk": f"penny break of {entry_price:.2f}",
            "hit_count": entry.hit_count,
            "decisecond_score": round(entry.decisecond_score, 0),
            "key_variable": entry.key_variable,
            "key_variable_price": entry.key_variable_price,
            "hits_by_component": entry.hits_by_component,
            "hits_by_period": entry.hits_by_period,
            "convergence": {
                "ohlc_detection": conv.ohlc_detection,
                "time_series": conv.time_series,
                "parm_price": conv.parm_price,
                "candle_close": conv.candle_close,
                "score": conv.score,
            },
            "rank_score": round(score, 2),
            "lookback_candles": self.cfg.lookback,
            "last_hit_ts": entry.last_hit_ts.isoformat() if entry.last_hit_ts else None,
        }

    def top_n(self, n: int = 10, cumulative_ds: Optional[dict] = None) -> list[dict]:
        ranked = rank_entries(self.store, pd.Timestamp.now(tz="UTC"),
                              min_tf_minutes=self.cfg.min_tf_minutes,
                              cumulative_ds=cumulative_ds)[:n]
        out = []
        for rank, (entry, score) in enumerate(ranked, 1):
            df = self.candle_cache.get((entry.ticker, entry.timeframe), pd.DataFrame())
            conv = detect_convergence(entry, df,
                                      store=self.store,
                                      cross_tf_store=self.store,
                                      cumulative_ds=cumulative_ds)
            offset, entry_price = best_offset(entry, df)
            out.append({
                "rank": rank,
                "ticker": entry.ticker,
                "timeframe": entry.timeframe,
                "outfit_id": entry.outfit_id,
                "outfit_periods": list(entry.outfit_periods),
                "outfit_name": next((o["name"] for o in OUTFITS if o["id"] == entry.outfit_id), ""),
                "entry_price": entry_price,
                "offset": offset,
                "hit_count": entry.hit_count,
                "decisecond_score": round(entry.decisecond_score, 0),
                "key_variable": entry.key_variable,
                "key_variable_price": entry.key_variable_price,
                "convergence": conv.score,
                "rank_score": round(score, 2),
            })
        return out


# ═══════════════════════════════════════════════════════════════════════════════
# 11. OUTPUT — terminal dashboard
# ═══════════════════════════════════════════════════════════════════════════════

def render_dashboard(signal: Optional[dict], systems: list[SystemState], top_n: list[dict]) -> str:
    lines: list[str] = []
    lines.append("═" * 71)
    if signal:
        lines.append(f"  TOP SIGNAL: {signal['ticker']} | "
                     f"{'/'.join(str(p) for p in signal['outfit_periods'])} "
                     f"({signal['outfit_name']}) | {signal['entry_price']:.2f}")
    else:
        lines.append("  TOP SIGNAL: (no hits detected this cycle)")
    lines.append("═" * 71)
    if signal:
        lines.append(f"  Outfit:       {'/'.join(str(p) for p in signal['outfit_periods'])}")
        lines.append(f"  Timeframe:    {signal['timeframe']}")
        lines.append(f"  Entry:        {signal['entry_price']:.2f}")
        lines.append(f"  Offset:       {signal['offset_applied']:+.2f}")
        lines.append(f"  Risk:         {signal['risk']}")
        lines.append(f"  Hit Count:    {signal['hit_count']} ({signal['lookback_candles']} candle lookback)")
        lines.append(f"  Convergence:  {signal['convergence']['score']} "
                     f"(OHLC={signal['convergence']['ohlc_detection']} "
                     f"TS={signal['convergence']['time_series']} "
                     f"Parm={signal['convergence']['parm_price']} "
                     f"Close={signal['convergence']['candle_close']})")
    lines.append("─" * 71)
    lines.append("  SYSTEMS:")
    for s in systems:
        glyph = "✅" if s.state == "positive" else ("❌" if s.state == "negative" else "⬜")
        lines.append(f"    {glyph} {s.name:<15} {s.state.upper():<10} {s.note}")
    lines.append("─" * 71)
    if top_n:
        lines.append("  TOP 10 RANKED:")
        for r in top_n:
            periods_str = "/".join(str(p) for p in r['outfit_periods'])
            lines.append(f"    {r['rank']:>2}. {r['ticker']:<6} {r['timeframe']:<5} "
                         f"{periods_str:<30} hits={r['hit_count']:>3} "
                         f"conv={r['convergence']} score={r['rank_score']}")
    lines.append("═" * 71)
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# 12. CLI
# ═══════════════════════════════════════════════════════════════════════════════

def build_universe(tier: str) -> list[str]:
    if tier == "tier1":
        raw = UNIVERSE_TIER_1
    elif tier == "tier2":
        raw = UNIVERSE_TIER_1 + UNIVERSE_TIER_2
    elif tier == "all":
        raw = UNIVERSE_TIER_1 + UNIVERSE_TIER_2 + UNIVERSE_TIER_3
    else:
        raw = UNIVERSE_TIER_1
    # Deduplicate while preserving order (tier1 takes precedence)
    seen: set = set()
    out: list[str] = []
    for t in raw:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def main():
    parser = argparse.ArgumentParser(description="SMA Outfit Detection Engine")
    parser.add_argument("--source", choices=["mock", "webull"], default="mock",
                        help="Data source. 'webull' requires WEBULL_APP_KEY/WEBULL_APP_SECRET env.")
    parser.add_argument("--universe", choices=["tier1", "tier2", "all"], default="tier1")
    parser.add_argument("--timeframes", nargs="+", default=["5m", "15m", "30m", "1h", "1d"])
    parser.add_argument("--lookback", type=int, default=130)
    parser.add_argument("--discovery", action="store_true", help="Enable discovery mode (1-999 brute force, slow)")
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--json", action="store_true", help="Output top signal as JSON instead of dashboard")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # Silence the Webull SDK's internal loggers — they dump full request blobs
    # on every INVALID_SYMBOL / 4xx error, making logs unreadable.
    for _noisy in ("webull", "webull.core.client", "webull.core.auth",
                   "urllib3", "urllib3.connectionpool"):
        _lg = logging.getLogger(_noisy)
        _lg.setLevel(logging.CRITICAL)
        _lg.propagate = False

    # Wire up data client
    if args.source == "webull":
        app_key = os.environ.get("WEBULL_APP_KEY")
        app_secret = os.environ.get("WEBULL_APP_SECRET")
        if not (app_key and app_secret):
            print("ERROR: WEBULL_APP_KEY and WEBULL_APP_SECRET env vars required for --source webull",
                  file=sys.stderr)
            sys.exit(1)
        client = WebullClient(app_key, app_secret, region=os.environ.get("WEBULL_REGION", "us"))
    else:
        client = MockClient()

    cfg = EngineConfig(
        universe=build_universe(args.universe),
        timeframes=args.timeframes,
        lookback=args.lookback,
        discovery_mode=args.discovery,
    )

    engine = SMAOutfitEngine(client, cfg)
    engine.monitor_systems()
    engine.scan()

    signal = engine.top_signal()
    top_n = engine.top_n(args.top_n)

    if args.json:
        print(json.dumps({"signal": signal, "systems": [asdict(s) for s in engine.system_states],
                          "top_n": top_n}, indent=2, default=str))
    else:
        print(render_dashboard(signal, engine.system_states, top_n))


if __name__ == "__main__":
    main()
