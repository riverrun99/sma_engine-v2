"""
Market Overlay — Terminal UI
============================
Combines:
  1. The System (TraderBJones): SMA10/50 + EMA9/30/50 on 30m SPY
  2. Zero Gamma Engine: SPX/SPY GEX zero-crossing level
  3. Triangulation Engine: top signals across all engines
  4. Macro read from triangulation groups

Run: python3 overlay.py
Refreshes every 60 seconds. GEX takes ~90s on first load.
"""

import time
import os
import sys
import json
import traceback
from datetime import datetime, timezone

# Load Webull credentials from parent .env
_env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

# Local modules
sys.path.insert(0, os.path.dirname(__file__))
import the_system
import gamma_engine
import flashalpha_gex
import index_gex
import sheets_sync
import systems_panel

# Triangulation readers — import from _triangulation_staging (read-only)
_tri_path = os.path.join(os.path.dirname(__file__), "..", "_triangulation_staging")
sys.path.insert(0, _tri_path)
try:
    from triangulator import (
        read_original, read_normalized, read_v3,
        read_discovery, read_confluence, read_trades,
        read_backtest, read_snapshot,
        triangulate, analyze_macro,
        DIR_STYLE, DIR_ICON, STATE_STYLE, CONV_STYLE,
    )
    TRI_AVAILABLE = True
except Exception as _e:
    TRI_AVAILABLE = False
    _TRI_ERR = str(_e)


REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "60"))
TRI_TOP_N       = int(os.environ.get("TRI_TOP_N", "15"))

console = Console()


# ─────────────────────────────────────────────────────────────────────────────
# Color helpers
# ─────────────────────────────────────────────────────────────────────────────

def state_color(state):
    return "green" if state == "UP" else "red"

def signal_color(signal):
    s = signal.upper()
    if "ENTER LONG"  in s: return "bright_green"
    if "ENTER SHORT" in s: return "bright_red"
    if "EXIT"        in s: return "yellow"
    return "white"

def dist_color(pct):
    if abs(pct) < 0.5: return "bright_yellow"
    if pct < 0:        return "cyan"
    return "white"

def regime_color(regime):
    return "green" if "ABOVE" in regime else "red"


# ─────────────────────────────────────────────────────────────────────────────
# System panel
# ─────────────────────────────────────────────────────────────────────────────

def build_system_panel(data):
    if "error" in data:
        return Panel(f"[red]{data['error']}[/red]", title="THE SYSTEM", border_style="red")

    state, vehicle = data["state"], data["vehicle"]
    sig, reason    = data["entry_signal"], data.get("entry_reason", "")
    flip           = data.get("trend_flip", False)
    caution        = data.get("caution", False)
    invalid        = data.get("invalid_cross", False)
    ob_os          = data.get("ob_os", "NEUTRAL")
    sc             = state_color(state)

    t = Text()
    t.append("SYSTEM STATE: ", style="bold")
    t.append(f"  {state}  ", style=f"bold {sc} on {'dark_green' if state=='UP' else 'dark_red'}")
    t.append("   Vehicle: ", style="dim")
    t.append(vehicle, style=f"bold {'green' if state=='UP' else 'red'}")
    if flip:
        t.append("  ⚡ FLIP!", style="bold magenta")
    t.append("\n\n")

    # SMAs
    sma50_dir = data.get("sma50_dir", "")
    sma50_dir_sym = ("↑" if sma50_dir == "rising" else "↓" if sma50_dir == "falling" else "→")
    sma50_dir_col = ("green" if sma50_dir == "rising" else "red" if sma50_dir == "falling" else "dim")
    t.append(f"SPY Close : {data['close']:.2f}   ")
    # Oversold/Overbought condition
    ob_col = ("bright_green" if "OVERBOUGHT" in ob_os else
              "bright_red"   if "OVERSOLD"   in ob_os else "dim")
    t.append(f"[{ob_col}]{ob_os}[/{ob_col}]")
    t.append(f"  ({data['dist_sma50_pct']:+.2f}% vs SMA50)\n", style="dim")

    t.append(f"SMA10 : {data['sma10']:.3f}   ", style=sc)
    t.append(f"SMA50 : {data['sma50']:.3f} [{sma50_dir_col}]{sma50_dir_sym}[/{sma50_dir_col}]")
    t.append(f"   Spread: {data['sma_spread_pct']:+.2f}%\n", style="dim")

    if data.get("sma200"):
        s200_col = "green" if data["close"] > data["sma200"] else "red"
        t.append(f"SMA200: {data['sma200']:.3f}   ", style=s200_col)
        ctx = data.get("sma200_context", "")
        t.append(f"{ctx}\n", style="dim")
    t.append("\n")

    # EMA table (9/21/50 per document)
    tbl = Table(box=box.SIMPLE, show_header=True, header_style="bold dim", padding=(0,1))
    tbl.add_column("EMA",  style="dim", width=6)
    tbl.add_column("Level", width=9)
    tbl.add_column("Dist%", width=8)
    for label, level_key, dist_key in [
        ("EMA9",  "ema9",  "dist_ema9_pct"),
        ("EMA21", "ema21", None),
        ("EMA50", "ema50", "dist_ema50_pct"),
    ]:
        level = data.get(level_key, 0)
        dist  = (data.get(dist_key) if dist_key else
                 (data["close"] - level) / level * 100 if level else 0)
        dc = dist_color(dist)
        tbl.add_row(label, f"{level:.3f}", f"[{dc}]{dist:+.2f}%[/{dc}]")

    # Signal text (after table)
    t2 = Text()
    sig_col = signal_color(sig)
    if invalid:
        sig_col = "yellow"
    t2.append("Signal: ", style="bold")
    t2.append(f"{sig}\n", style=f"bold {sig_col}")
    if reason:
        t2.append(f"        {reason}\n", style="dim")
    if caution:
        t2.append("        ⚠ CAUTION: SMA50 still sloping down\n", style="yellow")
    if invalid:
        t2.append("        ✗ INVALID: Price below SMAs after cross\n", style="yellow")

    # Choppy market warning
    if data.get("choppy"):
        t2.append("\n[yellow]⚠ CHOPPY — SMA10/50 nearly equal. Sit on cash until definitive trend.[/yellow]\n")

    # NASDAQ leading indicator
    ndx = data.get("nasdaq", {})
    if ndx and "error" not in ndx:
        t2.append("\n")
        ndx_state   = ndx.get("state", "")
        ndx_col     = "green" if ndx_state == "UP" else "red"
        ndx_dir     = ndx.get("sma50_dir", "")
        ndx_dir_sym = "↑" if ndx_dir == "rising" else "↓" if ndx_dir == "falling" else "→"
        ndx_dir_col = "green" if ndx_dir == "rising" else "red" if ndx_dir == "falling" else "dim"
        gap         = ndx.get("gap_pct", 0)
        gap_col     = "green" if gap > 0 else "red" if gap < 0 else "dim"
        ndx_dist    = ndx.get("dist_sma50", 0)

        t2.append("NASDAQ (QQQ): ", style="bold dim")
        t2.append(f"{ndx_state} ", style=f"bold {ndx_col}")
        t2.append(f"SMA50 {ndx_dir_sym}  ", style=ndx_dir_col)
        t2.append(f"Day: ", style="dim")
        t2.append(f"{gap:+.2f}%  ", style=gap_col)
        t2.append(f"vs SMA50: {ndx_dist:+.2f}%\n", style="dim")

        spy_gap = (data["close"] - data.get("open", data["close"])) / data.get("open", data["close"]) * 100 if data.get("open") else 0
        rel = gap - spy_gap
        if abs(rel) > 0.2:
            rel_label = "OUTPERFORMING SPY" if rel > 0 else "UNDERPERFORMING SPY"
            rel_col   = "green" if rel > 0 else "red"
            t2.append(f"             ")
            t2.append(f"{rel_label} ({rel:+.2f}%)\n", style=rel_col)

    t2.append(f"\n[dim]{data['timestamp']}[/dim]")

    src = data.get("source", "Webull")
    src_style = "dim" if src == "Webull" else "yellow"
    return Panel(Group(t, tbl, t2), title="[bold]⚙  THE SYSTEM[/bold]", border_style=sc,
                 subtitle=f"[{src_style}]SPY 30m · {src}[/{src_style}]")


# ─────────────────────────────────────────────────────────────────────────────
# Gamma panel
# ─────────────────────────────────────────────────────────────────────────────

def build_gamma_panel(data):
    if "error" in data:
        return Panel(f"[red]{data['error']}[/red]", title="ZERO GAMMA", border_style="red")

    spot, zero = data["spot"], data["zero_gamma"]
    dist       = data["dist_from_zero_pct"]
    regime     = data["regime"]
    rc         = regime_color(regime)

    t = Text()
    t.append("REGIME: ", style="bold")
    t.append(f"  {regime}  ", style=f"bold {rc} on {'dark_green' if 'ABOVE' in regime else 'dark_red'}")
    t.append(f"\n{data['regime_note']}\n\n", style="dim")

    t.append(f"SPY Spot   : {spot:.2f}\n")
    t.append(f"Zero Gamma : {zero:.2f}\n", style=f"bold {rc}")
    dc = dist_color(dist)
    t.append("Distance   : ", style="dim")
    t.append(f"{dist:+.2f}%", style=f"bold {dc}")
    t.append(f"  ({'above' if dist > 0 else 'below'} zero)\n\n", style="dim")

    if data.get("call_wall"):
        t.append(f"Call Wall  : {data['call_wall']:.2f}\n", style="green")
    if data.get("put_wall"):
        t.append(f"Put Wall   : {data['put_wall']:.2f}\n", style="red")

    if abs(dist) <= 5:
        bw  = 38
        mid = bw // 2
        pos = max(0, min(bw-1, int(mid + (dist / 5) * (bw // 2))))
        bar = list("─" * bw)
        bar[mid] = "│"
        bar[pos] = "●"
        t.append(f"\n[{''.join(bar)}]\n", style=rc)
        t.append("Put wall         Zero         Call wall\n", style="dim")

    if data.get("stale"):
        t.append(f"\n[yellow]⚠ {data['stale_note']}[/yellow]\n")
    t.append(f"\n[dim]{data['expirations_used']} expiries · {data['strikes_analyzed']} strikes · {data['timestamp']}[/dim]")

    border = "yellow" if data.get("stale") else rc
    return Panel(t, title="[bold]γ  ZERO GAMMA[/bold]", border_style=border,
                 subtitle="[dim]tikitrade.com · SPX gamma[/dim]")


# ─────────────────────────────────────────────────────────────────────────────
# Index GEX panel (QQQ / IWM / DIA)
# ─────────────────────────────────────────────────────────────────────────────

def build_index_gex_panel(idx_gex: dict, live_prices: dict = None):
    t = Text()
    tickers = ["QQQ", "IWM", "DIA"]
    labels  = {"QQQ": "QQQ Nasdaq", "IWM": "IWM Russell", "DIA": "DIA Dow"}
    any_data = False
    live_prices = live_prices or {}

    for ticker in tickers:
        r = idx_gex.get(ticker, {})
        if not r or r.get("error"):
            lp = live_prices.get(ticker)
            if lp:
                # Show live price from systems panel even if GEX calc failed
                t.append(f"{labels[ticker]:<14}", style="bold")
                t.append(f"  Spot: {lp:,.2f}", style="dim")
                t.append("  (GEX unavailable)\n", style="dim yellow")
            else:
                err = r.get("error", "loading...") if r else "loading..."
                t.append(f"{labels[ticker]:<14}", style="dim")
                t.append(f"  [dim]{err}[/dim]\n")
            continue

        any_data = True
        spot = r.get("spot", 0)
        zg   = r.get("zero_gamma", 0)
        dist = r.get("dist_pct", 0)
        reg  = r.get("regime", "")
        rc   = "green" if "ABOVE" in reg else "red"
        cw   = r.get("call_wall")
        pw   = r.get("put_wall")

        t.append(f"{labels[ticker]:<14}", style="bold")
        t.append(f"  [{rc}]{'▲' if 'ABOVE' in reg else '▼'} {reg.replace(' ZERO GAMMA','')}[/{rc}]")
        t.append(f"  {dist:+.2f}%\n", style="dim")
        t.append(f"  Spot: {spot:.2f}  ZeroGamma: {zg:.2f}", style="dim")
        if cw:
            t.append(f"  CW: {cw:.0f}", style="green")
        if pw:
            t.append(f"  PW: {pw:.0f}", style="red")
        t.append("\n")

    if not any_data:
        subtitle = "[dim]yfinance · computing first run (~2 min)[/dim]"
    else:
        ts = next((v.get("timestamp","") for v in idx_gex.values() if v.get("timestamp")), "")
        # Show age warning if data is stale (daily cache)
        age_note = ""
        try:
            ts_clean = ts.replace(" UTC", "").strip()
            ts_dt    = datetime.fromisoformat(ts_clean).replace(tzinfo=timezone.utc)
            age_min  = (datetime.now(timezone.utc) - ts_dt).total_seconds() / 60
            if age_min > 60:
                age_note = f" [yellow]⚠ {int(age_min//60)}h{int(age_min%60)}m old[/yellow]"
        except Exception:
            pass
        subtitle = f"[dim]yfinance · daily cache · {ts}[/dim]{age_note}"

    return Panel(t, title="[bold]γ  INDEX ZERO GAMMA[/bold]",
                 border_style="cyan", subtitle=subtitle)


# ─────────────────────────────────────────────────────────────────────────────
# Synthesis panel
# ─────────────────────────────────────────────────────────────────────────────

def build_synthesis_panel(sys_data, gex_data):
    if "error" in sys_data or "error" in gex_data:
        return Panel("[dim]Waiting for data...[/dim]", title="SYNTHESIS", border_style="dim")

    system_up  = sys_data["state"] == "UP"
    above_zero = "ABOVE" in gex_data["regime"]
    sig        = sys_data["entry_signal"]
    dist       = gex_data["dist_from_zero_pct"]

    if   system_up and above_zero:
        align, ac = "ALIGNED BULLISH",  "bright_green"
        note      = "System UP + above zero gamma — favorable long environment"
    elif not system_up and not above_zero:
        align, ac = "ALIGNED BEARISH",  "bright_red"
        note      = "System DOWN + below zero gamma — favorable short environment"
    elif system_up and not above_zero:
        align, ac = "CAUTION — CONFLICT", "yellow"
        note      = "System UP but below zero gamma — elevated vol risk"
    else:
        align, ac = "CAUTION — CONFLICT", "yellow"
        note      = "System DOWN but above zero gamma — possible short squeeze"

    # Override note when EXIT fires — the structural note contradicts the action
    if "EXIT SHORT" in sig:
        note = "Price crossed above SMA50 — exit SPXU, stand aside. Await SMA10/50 bullish cross for UPRO entry."
    elif "EXIT LONG" in sig:
        note = "Price crossed below SMA50 — exit UPRO, stand aside. Await SMA10/50 bearish cross for SPXU entry."

    if "ENTER LONG"  in sig and system_up and above_zero:
        action, ac2 = "✅ HIGH CONVICTION LONG — all aligned",  "bright_green"
    elif "ENTER SHORT" in sig and not system_up and not above_zero:
        action, ac2 = "✅ HIGH CONVICTION SHORT — all aligned", "bright_red"
    elif "EXIT" in sig:
        action, ac2 = f"⚠️  {sig}",                             "yellow"
    elif "HOLD" in sig:
        action, ac2 = f"⏳ {sys_data['entry_reason']}",         "white"
    else:
        action, ac2 = f"⚠️  {sig} (check GEX alignment)",       "yellow"

    t = Text()
    t.append("Alignment : ", style="bold")
    t.append(f" {align} \n", style=f"bold {ac}")
    t.append(f"{note}\n\n", style="dim")
    t.append("Action    : ", style="bold")
    t.append(f"{action}\n", style=f"bold {ac2}")
    t.append(f"\nVehicle: ", style="dim")
    t.append(sys_data["vehicle"], style=f"bold {'green' if system_up else 'red'}")
    t.append(f"  │  GEX dist: {dist:+.2f}%", style="dim")

    return Panel(t, title="[bold]⚡ SYNTHESIS[/bold]", border_style=ac, padding=(0,1))


# ─────────────────────────────────────────────────────────────────────────────
# Triangulation panels
# ─────────────────────────────────────────────────────────────────────────────

def build_macro_panel(macro):
    if macro is None:
        return Panel("[dim]No triangulation data[/dim]", title="MACRO", border_style="dim")

    tbl = Table(box=None, padding=(0,2), show_header=False, expand=True)
    tbl.add_column("Group",     style="bold", width=14)
    tbl.add_column("Dir",       width=14)
    tbl.add_column("Bear",      justify="right", width=7)
    tbl.add_column("Bull",      justify="right", width=7)

    for group, (direction, bear, bull) in macro.items():
        icon  = DIR_ICON.get(direction, "")
        style = DIR_STYLE.get(direction, "white")
        tbl.add_row(
            group,
            Text(f"{icon} {direction.upper()}", style=style),
            Text(f"{bear:.1f}", style="red"   if bear > 0 else "dim"),
            Text(f"{bull:.1f}", style="green" if bull > 0 else "dim"),
        )

    return Panel(tbl, title="[bold cyan]▼▲ MACRO[/bold cyan]", border_style="cyan")


def build_tri_signals_panel(signals, top_n, stock_gex=None):
    if signals is None:
        return Panel("[dim]No triangulation data[/dim]", title="SIGNALS", border_style="dim")

    stock_gex = stock_gex or {}
    has_gex   = bool(stock_gex)

    tbl = Table(box=box.SIMPLE_HEAD, padding=(0,1), show_header=True,
                header_style="bold white", expand=True)
    tbl.add_column("#",      width=3,  style="dim")
    tbl.add_column("Ticker", width=7,  style="bold")
    tbl.add_column("Score",  width=6)
    tbl.add_column("Eng",    width=5)
    tbl.add_column("V3",     width=8)
    tbl.add_column("Norm",   width=6)
    tbl.add_column("Orig",   width=6)
    tbl.add_column("Disc",   width=10)
    tbl.add_column("Entry",  width=8)
    if has_gex:
        tbl.add_column("γ",  width=4)   # gamma regime badge

    shown = [s for s in signals if s.score > 0][:top_n]
    for i, sig in enumerate(shown, 1):
        sc = ("bright_green bold" if sig.score >= 7 else
              "green"             if sig.score >= 5 else
              "yellow"            if sig.score >= 3 else "dim")

        eng_s = f"{sig.engines_confirmed}/3"
        eng_c = ("bright_green" if sig.engines_confirmed == 3 else
                 "green"        if sig.engines_confirmed == 2 else "dim")

        v3_s  = sig.v3_state or "—"
        v3_c  = STATE_STYLE.get(sig.v3_state, "dim")

        norm_s = sig.norm_conv or "—"
        norm_c = CONV_STYLE.get(sig.norm_conv, "dim")

        orig_s = sig.orig_conv or "—"

        if sig.disc_tf:
            disc_s = f"{sig.disc_tf} {sig.disc_sma}"
            disc_c = ("bright_green" if sig.disc_tf in ("1w","1d") else
                      "green"        if sig.disc_tf in ("4h","2h") else "yellow")
        else:
            disc_s, disc_c = "—", "dim"

        entry_s = f"${sig.trade_entry:.2f}" if sig.trade_entry else "—"

        row = [
            str(i),
            sig.ticker,
            Text(f"{sig.score:.1f}", style=sc),
            Text(eng_s,  style=eng_c),
            Text(v3_s,   style=v3_c),
            Text(norm_s, style=norm_c),
            orig_s,
            Text(disc_s, style=disc_c),
            entry_s,
        ]
        if has_gex:
            badge = flashalpha_gex.regime_badge(stock_gex.get(sig.ticker.upper()))
            row.append(badge)
        tbl.add_row(*row)

    total = len([s for s in signals if s.score > 0])
    return Panel(tbl,
                 title=f"[bold yellow]✦ TRIANGULATED SIGNALS — TOP {top_n}[/bold yellow]",
                 subtitle=f"[dim]{total} active tickers[/dim]",
                 border_style="yellow")


# ─────────────────────────────────────────────────────────────────────────────
# Plain-English narrative
# ─────────────────────────────────────────────────────────────────────────────

def build_narrative(sys_data, gex_data, signals=None, idx_gex=None) -> Panel:
    """Generate a plain-English market read — no API call, pure logic."""
    if "error" in sys_data:
        return Panel("[dim]Waiting for data...[/dim]", title="📋 MARKET READ", border_style="dim")

    parts = []

    # ── The System state ──────────────────────────────────────────────────────
    state   = sys_data.get("state", "")
    vehicle = sys_data.get("vehicle", "")
    sig     = sys_data.get("entry_signal", "")
    sma50d  = sys_data.get("sma50_dir", "flat")
    choppy  = sys_data.get("choppy", False)

    if choppy:
        parts.append("The System is [yellow]choppy[/yellow] — SMA10 and SMA50 are nearly equal, no clear trend. Sit on cash and wait for a definitive move.")
    elif "ENTER LONG" in sig:
        parts.append(f"The System is [bold green]LONG[/bold green] ({vehicle}). {sys_data.get('entry_reason', '')}.")
    elif "ENTER SHORT" in sig:
        parts.append(f"The System is [bold red]SHORT[/bold red] ({vehicle}). {sys_data.get('entry_reason', '')}.")
    elif "GO TO CASH" in sig:
        parts.append("The System just crossed [red]bearish[/red] but SMA50 isn't sloping down yet — go to cash, wait for confirmation before shorting.")
    elif "EXIT" in sig:
        parts.append(f"The System signals [yellow]EXIT[/yellow]: {sys_data.get('entry_reason', '')}.")
    elif state == "UP":
        parts.append(f"The System is [green]UP[/green] (holding [bold]{vehicle}[/bold]). SMA50 is {sma50d}.")
    else:
        parts.append(f"The System is [red]DOWN[/red] (holding [bold]{vehicle}[/bold]). SMA50 is {sma50d}.")

    if sys_data.get("caution"):
        parts.append("[yellow]⚠ Caution: SMA50 still sloping down — reduce size or wait.[/yellow]")

    # ── SMA200 context ────────────────────────────────────────────────────────
    ctx200 = sys_data.get("sma200_context")
    if ctx200:
        parts.append(f"SPY is {ctx200.lower()}.")

    # ── Zero Gamma ────────────────────────────────────────────────────────────
    if "error" not in gex_data:
        regime = gex_data.get("regime", "")
        dist   = gex_data.get("dist_from_zero_pct", 0)
        zg     = gex_data.get("zero_gamma", 0)
        if "ABOVE" in regime:
            parts.append(f"SPX is [green]above zero gamma[/green] ({zg:.0f}, {dist:+.2f}%) — dealers are long gamma, dampening volatility and providing a bullish tailwind.")
        elif "BELOW" in regime:
            parts.append(f"SPX is [red]below zero gamma[/red] ({zg:.0f}, {dist:+.2f}%) — dealers are short gamma, amplifying moves. Expect larger swings.")
        else:
            parts.append(f"SPX is [yellow]at zero gamma[/yellow] ({zg:.0f}) — key inflection point, volatility may pick up.")

    # ── NASDAQ leading indicator ──────────────────────────────────────────────
    ndx = sys_data.get("nasdaq", {})
    if ndx and "error" not in ndx:
        ndx_state = ndx.get("state", "")
        ndx_dir   = ndx.get("sma50_dir", "")
        gap       = ndx.get("gap_pct", 0)
        ndx_col   = "green" if ndx_state == "UP" else "red"
        if ndx_state != state:
            parts.append(f"[yellow]⚠ NASDAQ divergence[/yellow]: QQQ is [{ndx_col}]{ndx_state}[/{ndx_col}] while SPY is {state} — watch closely, NASDAQ leads.")
        else:
            dir_word = "higher" if gap > 0 else "lower"
            parts.append(f"NASDAQ is confirming: QQQ [{ndx_col}]{ndx_state}[/{ndx_col}] (MA structure), trading {dir_word} by {abs(gap):.2f}% today.")

    # ── Top signal ────────────────────────────────────────────────────────────
    if signals:
        top = [s for s in signals if s.score > 0]
        if top:
            s1 = top[0]
            sc_word = ("strong" if s1.score >= 7 else "moderate" if s1.score >= 5 else "weak")
            parts.append(f"Top triangulated signal: [bold]{s1.ticker}[/bold] (score {s1.score:.1f}, {s1.engines_confirmed}/3 engines) — {sc_word} confluence.")

    # ── Index GEX summary ─────────────────────────────────────────────────────
    if idx_gex:
        regimes = []
        for ticker, data in idx_gex.items():
            if "error" not in data:
                r = data.get("regime", "")
                col = "green" if "ABOVE" in r else "red" if "BELOW" in r else "yellow"
                regimes.append(f"[{col}]{ticker}[/{col}]")
        if regimes:
            parts.append(f"Index GEX: {', '.join(regimes)} — {'all bullish gamma' if all('green' in r for r in regimes) else 'mixed gamma environment'}.")

    # ── Assemble ──────────────────────────────────────────────────────────────
    t = Text()
    for i, p in enumerate(parts):
        t.append_text(Text.from_markup(p))
        if i < len(parts) - 1:
            t.append("  ")

    border = "green" if state == "UP" else "red"
    if choppy:
        border = "yellow"
    return Panel(t, title="[bold]📋 MARKET READ[/bold]",
                 border_style=border, padding=(0,1))


def write_snapshot(sys_data, gex_data, signals=None, macro=None, idx_gex=None):
    """Write JSON snapshot + self-contained HTML dashboard."""
    try:
        snap = {
            "updated": datetime.now(timezone.utc).isoformat(),
            "system":  sys_data,
            "gex":     gex_data,
            "idx_gex": idx_gex or {},
            "signals": [
                {
                    "ticker":    s.ticker,
                    "score":     s.score,
                    "engines":   s.engines_confirmed,
                    "v3_state":  s.v3_state,
                    "norm_conv": s.norm_conv,
                    "disc_tf":   s.disc_tf,
                    "entry":     s.trade_entry,
                }
                for s in (signals or []) if s.score > 0
            ][:20],
            "macro": {k: list(v) for k, v in (macro or {}).items()},
        }
        base = os.path.dirname(__file__)
        with open(os.path.join(base, "latest_snapshot.json"), "w") as f:
            json.dump(snap, f, default=str)
        _write_dashboard_html(snap, base)
    except Exception:
        pass


def _write_dashboard_html(snap: dict, base: str):
    """Write dashboard.html with data embedded — open in any browser, hit refresh."""
    sys_d  = snap.get("system", {})
    gex_d  = snap.get("gex", {})
    sigs   = snap.get("signals", [])
    idx_d  = snap.get("idx_gex", {})
    updated = snap.get("updated", "")

    state   = sys_d.get("state", "—")
    vehicle = sys_d.get("vehicle", "—")
    sig     = sys_d.get("entry_signal", "—")
    reason  = sys_d.get("entry_reason", "")
    choppy  = sys_d.get("choppy", False)
    sma50d  = sys_d.get("sma50_dir", "flat")
    ob_os   = sys_d.get("ob_os", "—")
    ctx200  = sys_d.get("sma200_context", "")
    caution = sys_d.get("caution", False)
    ndx     = sys_d.get("nasdaq") or {}

    regime  = gex_d.get("regime", "—") if "error" not in gex_d else "—"
    zg      = gex_d.get("zero_gamma", 0)
    dist    = gex_d.get("dist_from_zero_pct", 0)
    cwall   = gex_d.get("call_wall", "—")
    pwall   = gex_d.get("put_wall", "—")

    # ── Narrative (rule-based, same logic as terminal panel) ──────────────────
    narr_parts = []
    if "error" not in sys_d:
        if choppy:
            narr_parts.append("⚠ The System is choppy — SMA10 and SMA50 are nearly equal with no clear trend. Sit on cash until a definitive directional move develops.")
        elif "ENTER LONG"  in sig: narr_parts.append(f"✅ The System is LONG ({vehicle}). {reason}.")
        elif "ENTER SHORT" in sig: narr_parts.append(f"🔴 The System is SHORT ({vehicle}). {reason}.")
        elif "GO TO CASH"  in sig: narr_parts.append("⚠ Bearish cross detected but SMA50 isn't sloping down yet — go to cash and wait for confirmation before shorting.")
        elif "EXIT"        in sig: narr_parts.append(f"⚠ EXIT signal: {reason}.")
        elif state == "UP":        narr_parts.append(f"The System is UP (holding {vehicle}). SMA50 is {sma50d}.")
        else:                      narr_parts.append(f"The System is DOWN (holding {vehicle}). SMA50 is {sma50d}.")
        if caution:       narr_parts.append("⚠ Caution: SMA50 still sloping down — reduce size or wait for confirmation.")
        if ctx200:        narr_parts.append(f"SPY is {ctx200.lower()}.")
    if "error" not in gex_d:
        if "ABOVE" in regime: narr_parts.append(f"SPX is above zero gamma ({zg:.0f}, {dist:+.2f}%) — dealers long gamma, volatility dampened, bullish tailwind.")
        elif "BELOW" in regime: narr_parts.append(f"SPX is below zero gamma ({zg:.0f}, {dist:+.2f}%) — dealers short gamma, moves amplified. Expect wider swings.")
        else: narr_parts.append(f"SPX is near zero gamma ({zg:.0f}) — key inflection, volatility may expand.")
    if ndx and "error" not in ndx:
        ndx_state = ndx.get("state", "")
        gap = ndx.get("gap_pct", 0)
        dir_word = "higher" if gap > 0 else "lower"
        if ndx_state != state: narr_parts.append(f"⚠ NASDAQ divergence: QQQ is {ndx_state} while SPY is {state} — NASDAQ leads, watch closely.")
        else: narr_parts.append(f"NASDAQ confirming: QQQ {ndx_state}, trading {dir_word} {abs(gap):.2f}% today.")
    if sigs:
        s1 = sigs[0]
        sc_word = "strong" if s1["score"] >= 7 else "moderate" if s1["score"] >= 5 else "weak"
        narr_parts.append(f"Top signal: {s1['ticker']} (score {s1['score']:.1f}, {s1['engines']}/3 engines) — {sc_word} confluence.")
    narrative = "  ".join(narr_parts) or "Waiting for data..."

    # ── Colour helpers ────────────────────────────────────────────────────────
    state_col  = "#16a34a" if state == "UP" else "#dc2626"
    border_col = "#d97706" if choppy else state_col
    reg_col    = "#16a34a" if "ABOVE" in regime else "#dc2626" if "BELOW" in regime else "#d97706"

    def pct_col(v):
        try:
            return "#16a34a" if float(v) > 0 else "#dc2626" if float(v) < 0 else "#888"
        except Exception:
            return "#888"

    # ── Signal rows ───────────────────────────────────────────────────────────
    sig_rows = ""
    for s in sigs[:12]:
        bar = min(100, s["score"] / 10 * 100)
        bcol = "#16a34a" if s["score"] >= 7 else "#65a30d" if s["score"] >= 5 else "#d97706"
        v3bg = "#dcfce7" if s.get("v3_state") == "BULL" else "#fee2e2" if s.get("v3_state") == "BEAR" else "#f1f5f9"
        v3fg = "#15803d" if s.get("v3_state") == "BULL" else "#b91c1c" if s.get("v3_state") == "BEAR" else "#64748b"
        entry = f"${float(s['entry']):.2f}" if s.get("entry") else "—"
        sig_rows += f"""
        <div class="sig-row">
          <span class="ticker">{s['ticker']}</span>
          <div class="bar-bg"><div class="bar" style="width:{bar:.0f}%;background:{bcol};"></div></div>
          <span class="score">{s['score']:.1f}</span>
          <span class="eng">{s['engines']}/3</span>
          <span class="badge" style="background:{v3bg};color:{v3fg};">{s.get('v3_state') or '—'}</span>
          <span class="disc">{s.get('disc_tf') or '—'}</span>
          <span class="entry">{entry}</span>
        </div>"""

    # ── Index GEX chips ───────────────────────────────────────────────────────
    idx_chips = ""
    for t, data in idx_d.items():
        if "error" in data:
            idx_chips += f'<div class="idx-chip"><div class="idx-name">{t}</div><div style="color:#888">—</div></div>'
        else:
            r = data.get("regime", "")
            rc = "#16a34a" if "ABOVE" in r else "#dc2626" if "BELOW" in r else "#d97706"
            dp = float(data.get("dist_pct") or data.get("dist_from_zero_pct") or 0)
            lbl = "▲ ABOVE γ0" if "ABOVE" in r else "▼ BELOW γ0" if "BELOW" in r else "≈ AT γ0"
            idx_chips += f'<div class="idx-chip"><div class="idx-name">{t}</div><div style="color:{rc};font-weight:600;font-size:11px;">{lbl}</div><div style="color:#888;font-size:10px;">{dp:+.2f}%</div></div>'

    # ── NASDAQ section ────────────────────────────────────────────────────────
    ndx_html = ""
    if ndx and "error" not in ndx:
        nc = "#16a34a" if ndx.get("state") == "UP" else "#dc2626"
        ndx_html = f"""
        <div style="margin-top:10px;padding-top:10px;border-top:1px solid #e5e7eb;">
          <div class="section-label">NASDAQ (QQQ)</div>
          <div class="kv-row"><span>State</span><span style="color:{nc};font-weight:700;">{ndx.get('state','—')}</span></div>
          <div class="kv-row"><span>Day %</span><span style="color:{pct_col(ndx.get('gap_pct',0))};">{float(ndx.get('gap_pct',0)):+.2f}%</span></div>
          <div class="kv-row"><span>vs SMA50</span><span style="color:{pct_col(ndx.get('dist_sma50',0))};">{float(ndx.get('dist_sma50',0)):+.2f}%</span></div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Market Dashboard</title>
<style>
:root{{color-scheme:light}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f8f9fa;color:#1a1a2e;padding:20px;font-size:13px;max-width:900px;margin:0 auto}}
h1{{font-size:18px;font-weight:700;margin-bottom:2px}}
.ts{{font-size:11px;color:#aaa;margin-bottom:16px}}
.narr{{background:#fff;border-radius:10px;padding:16px;margin-bottom:14px;border-left:4px solid {border_col};box-shadow:0 1px 4px rgba(0,0,0,.07);line-height:1.65;font-size:13px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}}
.card{{background:#fff;border-radius:10px;padding:14px;box-shadow:0 1px 4px rgba(0,0,0,.07);border:1px solid #e8eaed}}
.section-label{{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.7px;color:#888;margin-bottom:8px}}
.big{{font-size:24px;font-weight:700;margin-bottom:8px}}
.badge{{display:inline-block;padding:2px 7px;border-radius:10px;font-size:10px;font-weight:600}}
.kv-row{{display:flex;justify-content:space-between;align-items:center;padding:3px 0;border-bottom:1px solid #f1f5f9;font-size:12px}}
.kv-row:last-child{{border-bottom:none}}
.kv-row span:first-child{{color:#666}}
.idx-row{{display:flex;gap:20px;flex-wrap:wrap}}
.idx-chip{{text-align:center}}
.idx-name{{font-size:10px;color:#888;margin-bottom:2px}}
.sig-row{{display:flex;align-items:center;gap:6px;padding:5px 0;border-bottom:1px solid #f1f5f9;font-size:12px}}
.sig-row:last-child{{border-bottom:none}}
.ticker{{font-weight:700;width:46px}}
.bar-bg{{flex:1;height:5px;background:#e5e7eb;border-radius:3px}}
.bar{{height:5px;border-radius:3px}}
.score{{width:28px;text-align:right;color:#444}}
.eng{{width:26px;text-align:right;color:#888;font-size:10px}}
.disc{{width:28px;text-align:right;color:#888;font-size:10px}}
.entry{{width:46px;text-align:right;color:#444;font-size:10px}}
</style></head><body>
<h1>⚡ Market Dashboard</h1>
<div class="ts">Last updated: {updated} · Auto-refreshes every 60s</div>

<div class="narr">{narrative}</div>

<div class="grid">
  <div class="card">
    <div class="section-label">The System (SPY 30m)</div>
    <div class="big" style="color:{border_col};">{'CHOPPY' if choppy else state}</div>
    <span class="badge" style="background:{'#dcfce7' if state=='UP' else '#fee2e2'};color:{'#15803d' if state=='UP' else '#b91c1c'};margin-bottom:10px;display:inline-block;">{vehicle}</span>
    <div class="kv-row"><span>Signal</span><span style="font-weight:600;">{sig}</span></div>
    <div class="kv-row"><span>SPY</span><span>${sys_d.get('close', '—')}</span></div>
    <div class="kv-row"><span>SMA10</span><span>{sys_d.get('sma10', '—')}</span></div>
    <div class="kv-row"><span>SMA50</span><span>{sys_d.get('sma50', '—')} ({sma50d})</span></div>
    <div class="kv-row"><span>SMA200</span><span>{sys_d.get('sma200', '—')}</span></div>
    <div class="kv-row"><span>Condition</span><span>{ob_os}</span></div>
    {ndx_html}
  </div>
  <div class="card">
    <div class="section-label">Zero Gamma (SPX)</div>
    <div class="big" style="color:{reg_col};">{regime.replace('ZERO GAMMA','γ0')}</div>
    <div class="kv-row"><span>SPX Spot</span><span>{gex_d.get('spot','—')}</span></div>
    <div class="kv-row"><span>Zero Gamma</span><span style="font-weight:600;color:{reg_col};">{zg:.0f}</span></div>
    <div class="kv-row"><span>Distance</span><span style="color:{pct_col(dist)};">{dist:+.3f}%</span></div>
    <div class="kv-row"><span>Call Wall</span><span>{cwall}</span></div>
    <div class="kv-row"><span>Put Wall</span><span>{pwall}</span></div>
    <div class="kv-row"><span>Data As Of</span><span style="color:#888;font-size:11px;">{gex_d.get('data_as_of','—')}</span></div>
  </div>
</div>

<div class="card" style="margin-bottom:12px;">
  <div class="section-label">Index GEX Regime</div>
  <div class="idx-row">{idx_chips or '<span style="color:#888">No index GEX data yet</span>'}</div>
</div>

<div class="card">
  <div class="section-label">Top Triangulated Signals</div>
  {sig_rows or '<div style="color:#888;padding:8px 0;">No signals yet — run full engine first</div>'}
</div>
</body></html>"""

    dash_path = os.path.join(base, "dashboard.html")
    with open(dash_path, "w") as f:
        f.write(html)


# ─────────────────────────────────────────────────────────────────────────────
# Full layout
# ─────────────────────────────────────────────────────────────────────────────

def build_layout(sys_data, gex_data, signals, macro, stock_gex=None, idx_gex=None, systems_data=None):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Header
    hdr = Text(justify="center")
    hdr.append("⚡ MARKET OVERLAY  ", style="bold white")
    hdr.append(f"{now}", style="dim white")
    hdr.append(f"  │  refresh {REFRESH_SECONDS}s", style="dim")
    header_panel = Panel(hdr, style="bold blue", padding=(0,1))

    layout = Layout()
    layout.split_column(
        Layout(name="header",    size=3),
        Layout(name="top",       ratio=5),
        Layout(name="synthesis", size=6),
        Layout(name="narrative", size=5),
        Layout(name="systems",   size=20),
        Layout(name="bottom",    ratio=4),
    )

    layout["top"].split_row(
        Layout(name="system",    ratio=1),
        Layout(name="gex_right", ratio=1),
    )
    layout["gex_right"].split_column(
        Layout(name="gamma",     ratio=1),
        Layout(name="idx_gamma", ratio=1),
    )
    layout["bottom"].split_row(
        Layout(name="macro",    ratio=2),
        Layout(name="signals",  ratio=3),
    )

    layout["header"].update(header_panel)
    layout["system"].update(build_system_panel(sys_data))
    layout["gamma"].update(build_gamma_panel(gex_data))
    # Extract live proxy prices from systems_data for index_gex panel
    _live_px = {}
    if systems_data:
        _ixic = systems_data.get("ixic", {})
        if _ixic.get("close"):
            _live_px["QQQ"] = _ixic["close"]
        _iwm = systems_data.get("iwm", {})
        if _iwm.get("close"):
            _live_px["IWM"] = _iwm["close"]
        _dji = systems_data.get("dji_15m", {})
        if _dji.get("close"):
            _live_px["DIA"] = _dji["close"]
    layout["idx_gamma"].update(build_index_gex_panel(idx_gex or {}, live_prices=_live_px))
    layout["synthesis"].update(build_synthesis_panel(sys_data, gex_data))
    layout["narrative"].update(build_narrative(sys_data, gex_data, signals, idx_gex))
    layout["systems"].update(systems_panel.build_systems_panel(systems_data))
    layout["macro"].update(build_macro_panel(macro))
    layout["signals"].update(build_tri_signals_panel(signals, TRI_TOP_N, stock_gex))

    return layout


# ─────────────────────────────────────────────────────────────────────────────
# Data fetch
# ─────────────────────────────────────────────────────────────────────────────

def fetch_triangulation():
    if not TRI_AVAILABLE:
        return None, None
    try:
        orig  = read_original()
        norm  = read_normalized()
        v3    = read_v3()
        disc  = read_discovery()
        conf  = read_confluence()
        tr    = read_trades()
        bt    = read_backtest()
        snap  = read_snapshot()
        sigs  = triangulate(orig, norm, v3, disc, conf, tr, bt, snap)
        macro = analyze_macro(sigs)
        return sigs, macro
    except Exception:
        return None, None


def fetch_all():
    try:
        sys_data = the_system.analyze()
    except Exception:
        sys_data = {"error": traceback.format_exc(limit=1)}

    time.sleep(3)

    # Pass Webull spot price into GEX so it doesn't need to fetch it separately
    spot_hint = sys_data.get("close") if "error" not in sys_data else None
    try:
        gex_data = gamma_engine.fetch_gex(spot_hint=spot_hint)
    except Exception:
        gex_data = {"error": traceback.format_exc(limit=1)}

    signals, macro = fetch_triangulation()  # pure file reads, instant

    # FlashAlpha single-stock GEX for top 5 signals (cached daily, 5-call budget)
    stock_gex = {}
    if signals:
        top_tickers = [s.ticker for s in signals[:flashalpha_gex.MAX_TICKERS]
                       if s.ticker]
        try:
            stock_gex = flashalpha_gex.fetch_gex_for_tickers(top_tickers)
        except Exception:
            stock_gex = {}

    # Index GEX — QQQ, IWM, DIA (cached daily)
    idx_gex = {}
    try:
        ndx_data = sys_data.get("nasdaq", {})
        spot_hints = {"QQQ": ndx_data.get("close")} if ndx_data and "error" not in ndx_data else {}
        idx_gex = index_gex.fetch_all(spot_hints=spot_hints)
    except Exception:
        idx_gex = {}

    # Sync all output categories to Google Sheets (non-blocking, errors swallowed)
    try:
        sheets_sync.sync_all(sys_data=sys_data, gex_data=gex_data)
    except Exception:
        pass

    # All index systems (cached 5 min, safe to call every cycle)
    all_systems = {}
    try:
        all_systems = systems_panel.fetch_all_systems()
    except Exception:
        all_systems = {}

    # Write JSON snapshot for live artifact dashboard
    write_snapshot(sys_data, gex_data, signals, macro, idx_gex)

    return sys_data, gex_data, signals, macro, stock_gex, idx_gex, all_systems


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    console.print("[bold blue]Starting Market Overlay...[/bold blue]")
    if not TRI_AVAILABLE:
        console.print(f"[yellow]Triangulation unavailable: {_TRI_ERR}[/yellow]")
    console.print(f"[dim]Refresh: {REFRESH_SECONDS}s  |  Ctrl+C to exit[/dim]\n")

    sys_data = {"error": "Loading..."}
    gex_data = {"error": "Loading..."}
    signals, macro, stock_gex, idx_gex, all_systems = None, None, {}, {}, {}
    last_fetch = 0

    with Live(build_layout(sys_data, gex_data, signals, macro, stock_gex, idx_gex, all_systems),
              refresh_per_second=1,
              console=console,
              screen=True) as live:
        try:
            while True:
                now = time.time()
                if now - last_fetch >= REFRESH_SECONDS:
                    sys_data, gex_data, signals, macro, stock_gex, idx_gex, all_systems = fetch_all()
                    last_fetch = now
                live.update(build_layout(sys_data, gex_data, signals, macro, stock_gex, idx_gex, all_systems))
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    console.print("\n[dim]Overlay stopped.[/dim]")


if __name__ == "__main__":
    main()
