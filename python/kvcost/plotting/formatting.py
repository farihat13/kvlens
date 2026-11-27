"""Human-readable formatting utilities."""

from __future__ import annotations


def bytes_to_human(num_bytes: int | float) -> str:
    """Convert bytes to human-readable string (e.g. ``1.5 MB``)."""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PB"


def number_to_human(num: int | float) -> str:
    """Convert large numbers to human-readable string (e.g. ``1.5M``)."""
    value = float(num)
    if abs(value) < 1000:
        # .0f (round-half-even), not int() truncation: matches the plot labels' historical .0f
        return f"{value:.0f}"
    for suffix in ("", "K", "M", "B", "T"):
        if abs(value) < 1000:
            return f"{value:.1f}{suffix}"
        value /= 1000.0
    return f"{value:.1f}P"


def us_to_human(us: float) -> str:
    """Microseconds to a human latency label: ``820us`` / ``3.4ms`` / ``1.2s``."""
    if us >= 1_000_000:
        return f"{us / 1_000_000:.1f}s"
    if us >= 1_000:
        return f"{us / 1_000:.1f}ms"
    return f"{us:.0f}us"


def geomean(xs) -> float | None:
    """Geometric mean over the POSITIVE entries (ratios live on a log scale, so the
    arithmetic mean over-weights the big side); None if nothing positive."""
    import math
    pos = [x for x in xs if x > 0]
    if not pos:
        return None
    return math.exp(sum(math.log(x) for x in pos) / len(pos))


def short_bytes(v):
    """a byte count in DECIMAL units, 3 significant digits (labels; decimal_bytes is for decade ticks)"""
    for unit, d in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if v >= d:
            return f"{v / d:.3g} {unit}"
    return f"{v:.3g} B"


def decimal_bytes(v):
    """decade ticks need DECIMAL units (the lib's bytes_to_human is binary: 1e7 B would read '9.5 MB')"""
    for unit, d in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if v >= d:
            return f"{v / d:g} {unit}"
    return f"{v:g} B"


def percent(share):
    """a share (0-1) as a percent label: 29%, 0.6%, <0.1% (never a bare 0%)"""
    pct = 100 * share
    return f"{pct:.0f}%" if pct >= 10 else f"{pct:.1f}%" if pct >= 0.05 else "<0.1%"


def params_phrase(params):
    """a caption's words for the params a per-run cost is the mean over: "1-5" -> "the mean over params 1-5", "1" -> "its
    one param"; anything else (LinkBench: "the mean over its requests in the log") is the phrase itself"""
    lo, sep, hi = params.partition("-")
    if not (lo.isdigit() and (not sep or hi.isdigit())):
        return params
    return f"the mean over params {params}" if hi and hi != lo else "its one param"
