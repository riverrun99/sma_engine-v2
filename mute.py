#!/usr/bin/env python3
"""
mute.py — batch add tickers to muted_tickers.txt

Usage:
    # Paste a raw list (duplicates fine) and pipe it in:
    pbpaste | python3 mute.py

    # Or pass tickers directly as arguments:
    python3 mute.py ESPR CCRN VTIP STIP HYG

    # Unmute (remove from file):
    python3 mute.py --unmute ESPR CCRN

Reads stdin if no arguments given (for piping).
Deduplicates automatically. No restart needed — takes effect next cycle.
"""

import sys
import os
import re

MUTED_PATH = os.path.join(os.path.dirname(__file__), "muted_tickers.txt")


def parse_tickers(text: str) -> list[str]:
    """Extract unique uppercase ticker symbols from any blob of text."""
    # Split on whitespace, commas, pipes, newlines — handle any paste format
    tokens = re.split(r"[\s,|]+", text.strip())
    seen = set()
    tickers = []
    for t in tokens:
        t = t.strip().upper()
        # Basic ticker sanity check: 1-6 letters/digits, optional hyphen (BRK-B)
        if re.match(r"^[A-Z][A-Z0-9\-]{0,5}$", t) and t not in seen:
            tickers.append(t)
            seen.add(t)
    return tickers


def load_existing(path: str) -> set[str]:
    """Load tickers already in the mute file."""
    existing = set()
    try:
        with open(path) as f:
            for line in f:
                line = line.split("#")[0].strip().upper()
                if line:
                    existing.add(line)
    except FileNotFoundError:
        pass
    return existing


def append_muted(tickers: list[str], path: str) -> tuple[list[str], list[str]]:
    """Append new tickers to muted file. Returns (added, already_muted)."""
    existing = load_existing(path)
    to_add   = [t for t in tickers if t not in existing]
    already  = [t for t in tickers if t in existing]

    if to_add:
        with open(path, "a") as f:
            f.write("\n")
            for t in to_add:
                f.write(f"{t}\n")

    return to_add, already


def remove_muted(tickers: list[str], path: str) -> list[str]:
    """Remove tickers from muted file. Returns list of removed tickers."""
    remove_set = set(t.upper() for t in tickers)
    removed = []
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []

    new_lines = []
    for line in lines:
        ticker = line.split("#")[0].strip().upper()
        if ticker and ticker in remove_set:
            removed.append(ticker)
        else:
            new_lines.append(line)

    with open(path, "w") as f:
        f.writelines(new_lines)

    return removed


def main():
    args = sys.argv[1:]

    # -- Unmute mode --
    if args and args[0] == "--unmute":
        tickers_to_unmute = args[1:]
        if not tickers_to_unmute:
            print("Usage: python3 mute.py --unmute TICKER1 TICKER2 ...")
            sys.exit(1)
        removed = remove_muted(tickers_to_unmute, MUTED_PATH)
        if removed:
            print(f"Unmuted {len(removed)}: {', '.join(sorted(removed))}")
        else:
            print("None of those tickers were in the mute list.")
        return

    # -- Mute mode: args or stdin --
    if args:
        raw = " ".join(args)
    elif not sys.stdin.isatty():
        raw = sys.stdin.read()
    else:
        print("Usage:")
        print("  pbpaste | python3 mute.py")
        print("  python3 mute.py ESPR CCRN VTIP")
        print("  python3 mute.py --unmute ESPR")
        sys.exit(0)

    tickers = parse_tickers(raw)
    if not tickers:
        print("No valid tickers found in input.")
        sys.exit(1)

    added, already = append_muted(tickers, MUTED_PATH)

    if added:
        print(f"Muted {len(added)} ticker(s): {', '.join(sorted(added))}")
    if already:
        print(f"Already muted ({len(already)}): {', '.join(sorted(already))}")
    if not added and not already:
        print("Nothing to mute.")


if __name__ == "__main__":
    main()
