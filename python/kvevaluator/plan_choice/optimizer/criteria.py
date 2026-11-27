"""Criteria over per-candidate cost vectors: a pick is a derived argmin under a declared criterion, never a stored fact.
The vectors come from a capture's run rows (candidates.choose), so any criterion is re-derivable from the captures.

Criterion specs (strings parsed here; every constant is the caller's explicit declaration):
  ops                  dominance on the S / G / N sums, the weight vector only on the Pareto frontier (the default)
  bytes                argmin total_bytes
  lane:<field>         argmin one recorded field (e.g. lane:scan_key_bytes)
  scalar:<f=w,...>     argmin of a declared weighted sum over recorded fields (the exchange rate stated by the caller)
  weighted:ops=<w>,bytes=<w>[,G=..,S=..,N=..][,norm=max|minmax]
                       argmin of a normalized blend of weighted ops and total_bytes, each channel scaled over a
                       reference set of the candidates (default all). norm=max: x / max; norm=minmax: (x - min) /
                       (max - min), a channel all candidates tie on contributes 0. Unit-free.

budget_pick(candidates, ref, budget) is the constrained rule: the fewest `objective` among the candidates whose `cap`
is within `budget` x the reference candidate's (e.g. fewest ops within B x NebulaGraph's bytes).

The C++-vs-Python differential checks ops only, so a loser candidate's byte lanes have one source (the C++ counts).
"""

from kvevaluator import weights
from kvevaluator.capture.step_record import BASE_OPS, HIT_BYTE_LANES, READ_BYTE_LANES, WRITE_BYTE_LANES, WRITE_OPS



def dominates(a, b):
    """a dominates b on the (S,G,N) sums: <= everywhere, < somewhere."""
    le = all(a[ch] <= b[ch] for ch in ("S", "G", "N"))
    return le and any(a[ch] < b[ch] for ch in ("S", "G", "N"))


def tie_break(tied, prefer):
    """An EXACT tie (equal scalars, the equality the callers already test; never a tolerance) carries no cost
    information, so the pick keeps the RECORDED choice when it is among the tied (`prefer`: the tracked picks
    file's label for the cell) -- a re-search then never flips a pin on a tie -- else catalog order.
    Returns (pick, reason); the reason names which branch decided."""
    if prefer is not None and prefer in tied:
        return prefer, "tied_kept_recorded"
    return tied[0], "tied_default_order"


def pick_rule(agg, w, prefer=None):
    """The OPS criterion (pick, reason, frontier, crossover_wN). Dominance on the
    SUMS first (the reason says the currency: a per-param winner can still
    differ); the vector only on the frontier. crossover_wN = the positive N
    weight NEAREST the default at which the pick ties a frontier rival (S/G
    weights held). With >2 frontier members every pair is considered. None = no
    rival reachable by moving wN alone (exact ties are labeled
    tied_kept_recorded / tied_default_order (tie_break), not weighted)."""
    frontier = [r for r in agg if not any(dominates(agg[o], agg[r]) for o in agg if o != r)]
    if len(frontier) == 1:
        return frontier[0], "dominant_on_sums", frontier, None
    key = lambda r: weights.weighted(agg[r]["S"], agg[r]["G"], agg[r]["N"], w)
    ranked = sorted(frontier, key=key)
    if key(ranked[0]) == key(ranked[1]):     # exact tie on the scalar
        tied = [r for r in ranked if key(r) == key(ranked[0])]
        pick, reason = tie_break(tied, prefer)
        return pick, reason, frontier, None
    pick, a = ranked[0], agg[ranked[0]]
    ties = []
    for r in frontier:
        if r == pick:
            continue
        b = agg[r]
        dn = a["N"] - b["N"]
        if dn == 0:
            continue                         # this rival is not reachable via wN
        t = -(w["S"] * (a["S"] - b["S"]) + w["G"] * (a["G"] - b["G"])) / dn
        if t > 0:
            ties.append(t)
    cross = round(min(ties, key=lambda t: abs(t - w["N"])), 4) if ties else None
    return pick, "weighted", frontier, cross


VALID_FIELDS = frozenset({*BASE_OPS, *WRITE_OPS, *(f"{lane}_bytes" for lane in (*HIT_BYTE_LANES, *WRITE_BYTE_LANES)),
                          "total_bytes"})


class Criterion:
    """A parsed criterion: pick(vectors) -> (label, reason, frontier, crossover)
    and score(vector) -> the scalar it minimizes (the report's ratio basis).
    scores(vectors[, ref]) -> {label: scalar} for a whole candidate set: the
    per-vector score for every criterion but `weighted`, whose scalar is only
    defined relative to the set (normalized by the reference candidates).
    HERMETIC by construction: 'ops' carries its OWN vector (the module default,
    or an explicit 'ops:G=..,S=..,N=..' declaration); another vector comes only
    as a spec (with_weights), never from the environment."""

    def __init__(self, spec, pick, score, w=None, scores=None):
        self.spec, self._pick, self.score, self.w = spec, pick, score, w
        self._scores = scores

    def pick(self, vectors, ref=None, prefer=None):
        """prefer: the cell's recorded label, kept on an EXACT tie (tie_break)"""
        return self._pick(vectors, prefer) if self._scores is None else _argmin_scores(
            self.scores(vectors, ref), self.spec, prefer)

    def scores(self, vectors, ref=None):
        if self._scores is not None:
            return self._scores(vectors, ref)
        return {l: self.score(v) for l, v in vectors.items()}


def wops_bytes4k_spec(w=None):
    """wops_bytes4k's spec under a weight vector (None: weights.DEFAULT), with G = 1 and weights.BYTES_PER_GET: the one
    definition"""
    w = w or weights.DEFAULT
    return f"scalar:G=1,S={w['S'] / w['G']!r},N={w['N'] / w['G']!r},total_bytes={1 / weights.BYTES_PER_GET!r}"


def with_weights(name_or_spec, w):
    """the criterion that picks under the vector `w` (kvcost --op-weights): the named rule itself at the default vector;
    else wops_bytes4k and wops as specs that carry `w`. bytes and a lane read no op weight and stay. A raw spec states
    its own weights, so it is refused beside a non-default vector."""
    name, spec = resolve_criterion(name_or_spec)
    if weights.is_default(w):
        return name_or_spec
    g = weights.per_get(w)
    made = {"wops_bytes4k": wops_bytes4k_spec(w), "wops": f"ops:G=1,S={g['S']!r},N={g['N']!r}"}
    if name in made:
        return made[name]
    if spec in made.values() or spec == "bytes" or spec.startswith("lane:"):    # already under `w`, or no op weight
        return name_or_spec
    raise SystemExit(f"--pick-by {name_or_spec!r} states its own weights: drop --op-weights, or name the vector in the spec")


# NAMED criteria: the ones a pin set is emitted under, named in this one place. A CLI's --pick-by takes a name
# (or a raw spec); the emit records name + resolved spec in the picks file; dump_planir stamps it into the bundles.
NAMED = {
    # today's rule, byte-identical: dominance on the S/G/N sums, the vector 1 : 1.1 : 0.01 on the Pareto frontier
    "wops": "ops",
    # the op-weight sensitivity study's decision:
    # weighted ops + bytes read as 4 KiB block reads, one block priced as one GET (the stores' RocksDB block size)
    "wops_bytes4k": wops_bytes4k_spec(),          # scalar:G=1,S=1.1,N=0.01,total_bytes=0.000244140625
}
DEFAULT_CRITERION = "wops_bytes4k"


def resolve_criterion(name_or_spec):
    """(name, spec): a NAMED criterion's name and its spec; a raw spec is its own name"""
    if name_or_spec is None:
        name_or_spec = DEFAULT_CRITERION
    return (name_or_spec, NAMED[name_or_spec]) if name_or_spec in NAMED else (name_or_spec, name_or_spec)


def parse_criterion(spec):
    spec = NAMED.get(spec, spec)                 # a name resolves to its spec; the Criterion carries the spec
    if spec == "ops" or spec.startswith("ops:"):
        if spec == "ops":
            w = dict(weights.DEFAULT)            # the declared default, NOT parse(None)
        else:
            w = {}
            for part in spec[len("ops:"):].split(","):
                f, _, wt = part.partition("=")
                if f.strip() not in ("G", "S", "N"):
                    raise ValueError(f"ops vector names G/S/N only, got {f.strip()!r}")
                w[f.strip()] = float(wt)
        return Criterion(spec, lambda vs, prefer=None: pick_rule(vs, w, prefer),
                         lambda v: weights.weighted(v["S"], v["G"], v["N"], w), w=w)
    if spec == "bytes":
        return _argmin_of(lambda v: v["total_bytes"], spec)
    if spec.startswith("lane:"):
        field = spec[len("lane:"):]
        if field not in VALID_FIELDS:
            raise ValueError(f"unknown lane {field!r} (fields: {sorted(VALID_FIELDS)})")
        return _argmin_of(lambda v, f=field: v[f], spec)
    if spec.startswith("scalar:"):
        terms = {}
        for part in spec[len("scalar:"):].split(","):
            f, _, wt = part.partition("=")
            if f.strip() not in VALID_FIELDS:
                raise ValueError(f"unknown field {f.strip()!r} in scalar criterion")
            terms[f.strip()] = float(wt)
        return _argmin_of(lambda v, t=terms: sum(v[f] * wt for f, wt in t.items()), spec)
    if spec.startswith("weighted:"):
        parts, w, norm = {}, dict(weights.DEFAULT), "max"
        for part in spec[len("weighted:"):].split(","):
            f, _, wt = part.partition("=")
            f = f.strip()
            if f in ("ops", "bytes"):
                parts[f] = float(wt)
            elif f in ("G", "S", "N"):
                w[f] = float(wt)
            elif f == "norm":
                if wt.strip() not in ("max", "minmax"):
                    raise ValueError(f"weighted criterion norm= is max or minmax, got {wt.strip()!r}")
                norm = wt.strip()
            else:
                raise ValueError(f"weighted criterion names ops/bytes (+ G/S/N, norm), got {f!r}")
        if set(parts) != {"ops", "bytes"}:
            raise ValueError(f"weighted criterion needs both ops= and bytes=, got {sorted(parts)}")
        wops = lambda v: weights.weighted(v["S"], v["G"], v["N"], w)

        def _scale(x, xs):
            """One channel's value scaled over the reference values xs, per `norm`."""
            if norm == "minmax":
                lo, hi = min(xs), max(xs)
                return (x - lo) / (hi - lo) if hi > lo else 0.0   # all tie: no preference
            return x / (max(xs) or 1.0)

        def _scores(vectors, ref=None):
            refs = [vectors[l] for l in (ref or vectors)]
            ro = [wops(v) for v in refs]
            rb = [v["total_bytes"] for v in refs]
            return {l: parts["ops"] * _scale(wops(v), ro) + parts["bytes"] * _scale(v["total_bytes"], rb)
                    for l, v in vectors.items()}
        # score(v) alone is undefined (needs the set): scores() is the entry
        return Criterion(spec, None, None, w=w, scores=_scores)
    raise ValueError(f"unknown criterion spec {spec!r}")


def _argmin_scores(scores, spec, prefer=None):
    best = min(scores.values())
    tied = [l for l, x in scores.items() if x == best]
    if len(tied) > 1:
        pick, reason = tie_break(tied, prefer)
        return pick, reason, tied, None
    return tied[0], f"argmin:{spec}", tied, None


PICK_FIELDS = ("S", "G", "N", "total_bytes")   # the vector candidates.vector builds and every pick scores
# total_bytes sums the 7 read lanes the reports count (step_record.READ_BYTE_LANES: get / scan key and value, and the
# seek / next / get miss keys), so the pick's wops_bytes4k is the reported one on a read query
PICK_BYTE_LANES = READ_BYTE_LANES


def linear_score(name_or_spec=None):
    """The criterion's scalar as per-field weights `{S, G, N, total_bytes: w}` when its `score` is a non-negative
    LINEAR function of the pick vector (the `ops` and `scalar:` forms: `wops`, `wops_bytes4k`), else None. Read off
    the criterion's own score function (unit vectors, then a probe for linearity), never re-typed: a pricer that sums
    per-part scores -- the grouping search's branch and bound over per-label tables -- needs exactly this, since a
    per-part lower bound is a bound of the sum only for a linear, monotone score. None = that pricer must refuse."""
    _, spec = resolve_criterion(name_or_spec)
    crit = parse_criterion(spec)
    zero = {f: 0.0 for f in PICK_FIELDS}
    try:
        base = float(crit.score(zero))
        w = {f: float(crit.score({**zero, f: 1.0})) - base for f in PICK_FIELDS}
        probe = {"S": 3.0, "G": 5.0, "N": 7000.0, "total_bytes": 123456.0}
        got = float(crit.score(probe))
    except (KeyError, TypeError, ValueError):
        return None
    want = sum(w[f] * probe[f] for f in PICK_FIELDS)
    if base != 0.0 or abs(got - want) > 1e-9 * max(1.0, abs(want)) or any(x < 0 for x in w.values()):
        return None
    return w


def _argmin_of(score, spec):
    def _pick(vectors, prefer=None):
        return _argmin_scores({l: score(v) for l, v in vectors.items()}, spec, prefer)
    return Criterion(spec, _pick, score)


def budget_pick(candidates, ref, budget, objective="ops", cap="bytes"):
    """(label, fits, tied): the fewest `objective` among candidates {label: {field: value}} whose `cap` is
    <= budget x candidates[ref][cap]. No candidate under the cap: fits=False and the pick is the lowest-`cap`
    one. Ties break on the other field, then on the candidates' order (the caller's preference); tied = every
    label with the pick's exact (objective, cap)."""
    if ref not in candidates:
        raise KeyError(f"reference {ref!r} is not a candidate")
    if budget <= 0:
        raise ValueError(f"budget must be > 0, got {budget}")
    limit = budget * candidates[ref][cap]
    fit = [l for l, v in candidates.items() if v[cap] <= limit]
    rank = {l: i for i, l in enumerate(candidates)}
    if fit:
        key = lambda l: (candidates[l][objective], candidates[l][cap], rank[l])
        pick = min(fit, key=key)
    else:
        key = lambda l: (candidates[l][cap], candidates[l][objective], rank[l])
        pick = min(candidates, key=key)
    p = candidates[pick]
    tied = [l for l, v in candidates.items() if v[objective] == p[objective] and v[cap] == p[cap]]
    return pick, bool(fit), tied
