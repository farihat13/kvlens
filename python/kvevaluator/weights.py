"""The op-weight vector: the one home for what a SEEK / GET / NEXT is worth in a weighted total.

KVCost stays an (S, G, N) vector where it is measured; every weighted collapse in the analysis goes through here. The
unweighted totals (S + G, S + G + N) are separate questions and do not. The notation order is G : S : N (the dict is
keyed by name; only parse() and label() care about order). DEFAULT 1 : 1.1 : 0.01 leans to the cache-pressure end
(SEEK and GET dear, NEXT ~0.009 of a SEEK); it is stored as 100 : 110 : 1 so sums and the ties a pick rule tests with ==
stay exact integers. The vector is a modelling choice, not a measured constant:
a weighted total names its vector, and a crossover weight (where two formats tie) is the regime-free claim.

Override per run with --op-weights G:S:N (passed to parse()); it reaches both the plan pick and the reported cost. There
is no environment setting: an export must not change what a run derives.
"""

DEFAULT = {"G": 100.0, "S": 110.0, "N": 1.0}   # G:S:N = 1:1.1:0.01, stored x100 so sums and ties are exact
# wops_bytes4k counts this many bytes read as one GET: one 4 KiB block read (the stores' RocksDB block size)
BYTES_PER_GET = 4096


def parse(spec=None):
    """The weight vector of a run: spec "G:S:N" (three positive numbers, e.g. "1:30:0.01"; only the ratio matters), or
    DEFAULT when spec is empty. A spec with DEFAULT's ratio returns DEFAULT itself, so a run that names the default
    costs exactly as one that does not."""
    if not spec:
        return dict(DEFAULT)
    parts = spec.split(":")
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        vals = []
    if len(vals) != 3 or min(vals) <= 0:
        raise ValueError(f"weights must be 'G:S:N', three positive numbers (e.g. 1:1.1:0.01), got {spec!r}")
    w = {"G": vals[0], "S": vals[1], "N": vals[2]}
    return dict(DEFAULT) if is_default(w) else w


def is_default(w):
    """does `w` have DEFAULT's ratio, at G = 1 and to the 10 digits a run records (text)? 3:3.3:0.03 does, though
    3.3 / 3 is not 1.1 in floats"""
    return w is None or text(w) == text(DEFAULT)


def text(w):
    """the vector as a run records it, at G = 1: '1:1.1:0.01' for DEFAULT (the --op-weights form, read back by parse)"""
    g = per_get(w)
    return ":".join(f"{g[k]:.10g}" for k in ("G", "S", "N"))


def weighted(S, G, N, w=None):
    """Collapse one (S, G, N) into a scalar under a weight vector, at the stored scale (for comparing: argmin, ties)."""
    w = w or DEFAULT
    return w["S"] * S + w["G"] * G + w["N"] * N


def wops(S, G, N, w=None):
    """Weighted ops in GET units (G = 1): the number a report shows. weighted() / w["G"], so ties stay exact."""
    w = w or DEFAULT
    return weighted(S, G, N, w) / w["G"]


def per_get(w=None):
    """The vector scaled to G = 1, for per-op stacks and legends (1 : 1.1 : 0.01 for DEFAULT)."""
    w = w or DEFAULT
    return {k: w[k] / w["G"] for k in ("S", "G", "N")}


# The MEASURED bracket the findings quote, as data (one home for the paper, the op-weight sensitivity study and
# the mix): each vector with where it comes from. Constants with provenance, not a derivation from an untracked capture.
BRACKET = {
    "warm": {"G": 15.0, "S": 20.0, "N": 1.0,
             "source": "live NebulaGraph warm per-op means (G ~3.2 / S ~4.3 / N ~0.2 us): "
                       "the KV-op latency study, section 2b"},
    "cold_leaning": {**DEFAULT,
                     "source": "the analysis default: SEEK and GET dear, NEXT ~0.009 of a SEEK (this docstring; 2b's "
                               "cache-pressure stretch, no measured cold vector exists)"},
    "evaluator_warm": {"G": 1.0, "S": 1.1, "N": 0.03, "N_max": 0.25,
                       "source": "our evaluator's warm per-op latency, sf0.1 (N/S 0.03-0.25 by query)"},
}
# N/S across the measured regimes: warm ~0.05 (2b) down to ~0.002 under cache pressure (2b's stretch). A claim that
# holds for every N weight in this range is regime-independent; one that flips inside it is not.
NEXT_RANGE = (0.002, 0.05)
# S/G measured WARM: our evaluator's per-op latency (sf0.1) 1.06, live NebulaGraph (2b) 1.33.
# Cold is unmeasured (a cold SEEK and a cold GET both pay a block read; no ratio recorded). The sensitivity study
# sweeps 0.75-2.0 around it (SEEK_SWEEP) and labels a flip inside SEEK_RANGE as regime-dependent.
SEEK_RANGE = (1.06, 1.33)
SEEK_SWEEP = (0.75, 2.0)


def crossover(a, b, w, axis="N"):
    """The weight on `axis` (S, G or N) at which two (S, G, N) sums tie, the other two weights held at `w`; None
    when they never tie for a positive weight (equal counts on the axis, or the tie is at a negative weight).
    weighted(a) == weighted(b)  <=>  w_axis * (a_axis - b_axis) = -(sum over the other axes of w_o * (a_o - b_o))."""
    d = {o: float(a[o]) - float(b[o]) for o in ("S", "G", "N")}
    if d[axis] == 0:
        return None
    other = sum(w[o] * d[o] for o in ("S", "G", "N") if o != axis)
    wstar = -other / d[axis]
    return wstar if wstar > 0 else None


def human(v):
    """Weighted ops in GET units for an axis or a bar label ('0.25', '12.5', '3.4K'): up to two decimals below
    1000 (a NEXT is 0.01 of a GET, and a small panel ticks at 0.5), then K / M / B."""
    v = float(v)
    if abs(v) < 1000:
        return f"{v:.2f}".rstrip("0").rstrip(".")
    for suffix in ("K", "M", "B"):
        v /= 1000.0
        if abs(v) < 1000:
            return f"{v:.1f}{suffix}"
    return f"{v:.1f}T"


def label(w):
    """A short human label for a vector at G = 1, for report headers ('G:S:N = 1:1.1:0.01')."""
    g = per_get(w)

    def fmt(x):
        return f"{x:.4g}"
    return f"G:S:N = {fmt(g['G'])}:{fmt(g['S'])}:{fmt(g['N'])}"
