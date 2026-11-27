"""Small statistics for the analysis scripts -- rank correlation, a log-space fit, and the two
summary helpers that make the sound aggregation the easy one: `rho_by_group` (a correlation per group, never a lone pooled one) and
`summarize` (one statistic, chosen on purpose and labelled). Pure Python (no numpy/scipy
dependency for a two-line formula), tie-aware, deterministic.

    from kvcost.plotting.stats import spearman, log_fit, rho_by_group, summarize
"""
import math
import statistics
import warnings

from kvcost.layout_cost import compare
from kvcost.plotting.formatting import geomean


def spearman(xs, ys):
    """Spearman's rank correlation (Pearson on the ranks, so ties are handled; the one definition:
    kvcost.layout_cost.compare.spearman). None when fewer than 3 pairs or a constant series."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    r = compare.spearman(xs, ys)
    return None if math.isnan(r) else r


def log_fit(xs, ys):
    """Least squares of log10(y) on log10(x): (slope, intercept) for a line on a log-log
    axis, y = 10**intercept * x**slope. Positive values only; None with fewer than 2 points."""
    pts = [(math.log10(x), math.log10(y)) for x, y in zip(xs, ys) if x > 0 and y > 0]
    if len(pts) < 2:
        return None
    mx = sum(p[0] for p in pts) / len(pts)
    my = sum(p[1] for p in pts) / len(pts)
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    if not sxx:
        return None
    slope = sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx
    return slope, my - slope * mx


POOLED = "pooled"        # the key rho_by_group files the across-groups rho under (only when asked)


def _col(spec):
    """A column spec is a dict key or a callable over one point."""
    return spec if callable(spec) else (lambda p: p[spec])


def rho_by_group(points, group_key, x, y, pooled=False, min_n=5):
    """Spearman rho of y on x, one per group: {group: (rho, n)}. The guard against the
    size-confound -- correlating cost vs latency across a format's QUERIES mostly measures
    query size (ic9 is big in every currency, so every currency scores ~0.95), and the honest
    unit is one group (a run: one query+param, compared across formats). So:
      * group_key=None means one pooled rho over everything; that raises unless pooled=True;
      * pooled=True ADDS the across-groups rho under the POOLED key next to the per-group
        ones, so a pooled number never ships alone;
      * a group with fewer than min_n points warns: over 3 formats rho can only be
        {-1, -0.5, 0.5, 1}, so a mean over such rhos is a vote count, not a correlation.
    group_key / x / y: a dict key or a callable over one point. rho is None where spearman
    can't compute it (under 3 points or a constant series); n is the group's point count."""
    pts = list(points)
    fx, fy = _col(x), _col(y)
    if group_key is None:
        if not pooled:
            raise ValueError("rho_by_group: no group_key = one rho pooled over everything, the "
                             "size-confound; pass a group_key (and pooled=True if the pooled "
                             "number is wanted next to the per-group ones)")
        groups = {POOLED: pts}
    else:
        gk = _col(group_key)
        groups = {}
        for p in pts:
            groups.setdefault(gk(p), []).append(p)
        if POOLED in groups:
            raise ValueError(f"rho_by_group: a group is named {POOLED!r}, the pooled key")
    out = {}
    for g, mine in groups.items():
        if len(mine) < min_n:
            warnings.warn(f"rho_by_group: group {g!r} has {len(mine)} points (< {min_n}), so its "
                          f"rho is quantized (few distinct values); read a mean over such rhos as "
                          f"a vote share, not a correlation", stacklevel=2)
        out[g] = (spearman([fx(p) for p in mine], [fy(p) for p in mine]), len(mine))
    if pooled and group_key is not None:
        out[POOLED] = (spearman([fx(p) for p in pts], [fy(p) for p in pts]), len(pts))
    return out


SUMMARIES = ("mean", "median", "geomean", "sum")


def summarize(values, how, unit="params"):
    """One summary statistic, chosen on purpose and labelled: (stat, n, suffix), e.g.
    (12.5, 5, "(median over 5 params)"). `how` is one of SUMMARIES. None/NaN entries are
    dropped and n counts what remains; geomean also drops non-positive entries (it lives on a
    log scale) and n counts only those. stat is None when nothing is left. Put the suffix in
    the axis label so mean-vs-median is a visible choice: LDBC params are heavy-tailed (the
    ic5/ic9 hub params), so the mean is the hub and the median is the typical param."""
    if how not in SUMMARIES:
        raise ValueError(f"summarize: how={how!r}, expected one of {SUMMARIES}")
    vals = [float(v) for v in values if v is not None and v == v]
    if how == "geomean":
        vals = [v for v in vals if v > 0]
    n = len(vals)
    if n == 0:
        stat = None
    elif how == "mean":
        stat = sum(vals) / n
    elif how == "median":
        stat = statistics.median(vals)
    elif how == "geomean":
        stat = geomean(vals)
    else:
        stat = sum(vals)
    return stat, n, f"({how} over {n} {unit})"
