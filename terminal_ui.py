"""
terminal_ui.py — rich live terminal dashboard.

Beautiful in-terminal display of engine state. Uses the `rich` library for
colored panels, tables, and live updating. This replaces the plain ASCII
dashboard for users who want to watch the engine run in real time.

Usage:
    ui = TerminalUI()
    ui.render(signal, top_n, systems, regime_label, cycle_count, last_scan_time)

The render method prints a fresh dashboard every call. Designed to be called
once per scan cycle from the daemon.

For continuously-updating-in-place display, use ui.live_context() as a
context manager (Rich Live mode). This is more visually impressive but
harder to debug if the engine logs errors — the dashboard occludes them.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.layout import Layout
from rich.align import Align
from rich import box


_console = Console()


def _signal_panel(signal: Optional[dict]) -> Panel:
    """Render the top signal as a panel."""
    if not signal:
        body = Text("(no signal detected this cycle)", style="dim italic")
        return Panel(body, title="[bold]TOP SIGNAL[/bold]",
                     border_style="dim", box=box.ROUNDED)

    periods_str = "/".join(str(p) for p in signal.get("outfit_periods", []))
    conv_score = signal.get("convergence", {}).get("score", "0/4")
    conv_n = int(conv_score.split("/")[0]) if "/" in conv_score else 0
    conv_color = {0: "red", 1: "red", 2: "yellow", 3: "green", 4: "bright_green"}.get(conv_n, "white")

    t = Table(show_header=False, box=None, padding=(0, 1), expand=True)
    t.add_column(style="bold cyan", width=14)
    t.add_column(style="bold white")
    t.add_row("Ticker", f"[bold yellow]{signal.get('ticker', '')}[/bold yellow]")
    t.add_row("Timeframe", signal.get("timeframe", ""))
    t.add_row("Outfit", f"{periods_str}  [dim]({signal.get('outfit_name', '')})[/dim]")
    t.add_row("Entry price", f"[bold green]{signal.get('entry_price', '')}[/bold green]")
    t.add_row("Offset", str(signal.get("offset_applied", "")))
    t.add_row("Hit count", f"[bold]{signal.get('hit_count', '')}[/bold]")
    t.add_row("Convergence", f"[{conv_color}]{conv_score}[/{conv_color}]")
    t.add_row("Risk", signal.get("risk", ""))

    return Panel(
        t,
        title="[bold]▌ TOP SIGNAL[/bold]",
        border_style="bright_cyan",
        box=box.ROUNDED,
    )


def _top_n_table(top_n: list, max_rows: int) -> Panel:
    """Render the top-N leaderboard as a table panel."""
    t = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 1))
    t.add_column("#", justify="right", style="dim", width=3)
    t.add_column("Ticker", style="bold yellow")
    t.add_column("TF", style="cyan", width=5)
    t.add_column("Outfit", style="white")
    t.add_column("Hits", justify="right", style="bold")
    t.add_column("Conv", justify="center")
    t.add_column("Score", justify="right", style="dim")

    if not top_n:
        t.add_row("—", "—", "—", "(no ranked entries)", "—", "—", "—")
    else:
        for entry in top_n[:max_rows]:
            periods_str = "/".join(str(p) for p in entry.get("outfit_periods", []))
            conv = entry.get("convergence", "0/4")
            conv_n = int(conv.split("/")[0]) if "/" in conv else 0
            conv_color = {0: "red", 1: "red", 2: "yellow", 3: "green", 4: "bright_green"}.get(conv_n, "white")
            t.add_row(
                str(entry.get("rank", "")),
                entry.get("ticker", ""),
                entry.get("timeframe", ""),
                periods_str,
                str(entry.get("hit_count", "")),
                f"[{conv_color}]{conv}[/{conv_color}]",
                f"{entry.get('rank_score', 0):.1f}",
            )

    shown = min(len(top_n), max_rows)
    return Panel(t, title=f"[bold]▌ TOP {shown} RANKED[/bold]",
                 border_style="bright_blue", box=box.ROUNDED)


def _systems_panel(systems: list) -> Panel:
    """Render the 8-system grid as a panel."""
    t = Table(box=box.SIMPLE, expand=True, padding=(0, 1))
    t.add_column("System", style="bold")
    t.add_column("State", justify="center")
    t.add_column("Note", style="dim")

    for s in systems:
        if s.state == "positive":
            marker = "[bold bright_green]●  POS[/bold bright_green]"
        elif s.state == "negative":
            marker = "[bold red]●  NEG[/bold red]"
        else:
            marker = "[dim]●  ---[/dim]"
        t.add_row(s.name, marker, s.note)

    return Panel(t, title="[bold]▌ SYSTEM STATES[/bold]",
                 border_style="bright_magenta", box=box.ROUNDED)


def _regime_panel(regime_label: Optional[str]) -> Panel:
    """Render the current regime as a compact panel."""
    if regime_label is None:
        text = Text("(not yet computed)", style="dim italic")
        border = "dim"
    else:
        colors = {"risk-on": "bright_green", "neutral": "yellow",
                  "risk-off": "red"}
        color = colors.get(regime_label, "white")
        text = Text(regime_label.upper(), style=f"bold {color}", justify="center")
        border = color

    return Panel(Align.center(text, vertical="middle"),
                 title="[bold]▌ REGIME[/bold]",
                 border_style=border, box=box.ROUNDED,
                 height=5)


def _header(cycle_count: int, last_scan: Optional[datetime]) -> Panel:
    """Render the header bar with branding and cycle info."""
    last_str = last_scan.strftime("%Y-%m-%d %H:%M:%S UTC") if last_scan else "—"
    txt = Text()
    txt.append("ELEMENT 47 ", style="bold bright_cyan")
    txt.append("· SMA OUTFIT ENGINE", style="bold white")
    txt.append(f"   cycle #{cycle_count}", style="dim")
    txt.append(f"   last scan: {last_str}", style="dim")
    return Panel(txt, box=box.HEAVY, border_style="bright_cyan", padding=(0, 1))


class TerminalUI:
    """Beautiful terminal dashboard for live engine state."""

    def __init__(self, console: Optional[Console] = None, max_rows: Optional[int] = None):
        self.console = console or _console
        self.max_rows = max_rows or int(os.environ.get("TERMINAL_TOP_N", "20"))

    def render(
        self,
        signal: Optional[dict],
        top_n: list,
        systems: list,
        regime_label: Optional[str] = None,
        cycle_count: int = 0,
        last_scan: Optional[datetime] = None,
    ) -> None:
        """Print the full dashboard once."""
        last_scan = last_scan or datetime.now(timezone.utc)
        self.console.clear()
        self.console.print(_header(cycle_count, last_scan))
        self.console.print()

        # Top row: signal (left) + regime (right narrow)
        layout = Layout()
        layout.split_column(
            Layout(name="top", size=14),
            Layout(name="mid"),
            Layout(name="bot"),
        )
        layout["top"].split_row(
            Layout(_signal_panel(signal), ratio=2),
            Layout(_regime_panel(regime_label), ratio=1),
        )
        layout["mid"].update(_top_n_table(top_n, self.max_rows))
        layout["bot"].update(_systems_panel(systems))

        # Total height to give the layout
        self.console.print(layout, height=46)

    def render_simple(
        self,
        signal: Optional[dict],
        top_n: list,
        systems: list,
        regime_label: Optional[str] = None,
        cycle_count: int = 0,
        last_scan: Optional[datetime] = None,
    ) -> None:
        """Simpler vertical render — works better on narrow terminals.

        Use this if the layout-based render looks cramped (mobile SSH, etc).
        """
        last_scan = last_scan or datetime.now(timezone.utc)
        self.console.clear()
        self.console.print(_header(cycle_count, last_scan))
        self.console.print(_signal_panel(signal))
        self.console.print(_regime_panel(regime_label))
        self.console.print(_top_n_table(top_n, self.max_rows))
        self.console.print(_systems_panel(systems))
