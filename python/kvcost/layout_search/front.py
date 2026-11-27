"""A workload's COMPLETE Pareto front over its parts (every mix no other mix beats in both wops and bytes), and a mix
priced per part. The workload, its loaded data (compose.load: its families and settings are the caller's) and its search
problem (search.problem over compose.workload_units) come from the caller; this module only walks and prices.

How the walk goes: from the fewest-wops mix, down in bytes: the fewest wops within (the last mix's bytes - a hair),
until no mix fits (search.solve, the three settings only: plain=True)."""
import glob
import json
import math
import os
import time

from kvcost.layout_cost import compose
from kvcost.layout_cost.mixes import assignment_label
from kvcost.layout_search.search import solve
from kvcost.layout_cost import mixes
from kvcost.layout_cost.compose import CELLS, LAYOUTS, MEASURES, currencies, load_reads, load_writes, cost_mix
from kvcost.layout_search.search import uniform
from kvcost.layout_cost.settings import B_UNIT, NAMED_BUDGETS, OLD_B_UNIT, name_of   # B's unit (Sp-Co) and the named B
from kvevaluator.output_tree import capture_in
from kvcost.layout_cost import picks


def lower_left_front(points):
    """the lower-left front of [(x, y, payload)] (both axes lower is better), in x order: sorted by (x, y), stable, a
    point kept when its y is below every kept point's; of points equal in (x, y), the first in the given order"""
    out, best = [], float("inf")
    for p in sorted(points, key=lambda p: (p[0], p[1])):
        if p[1] < best:
            out.append(p)
            best = p[1]
    return out


def cost_by_part(data, w, assign, measure=None):
    """(w's totals, {part: totals}) of one mix, each as currencies (ops = wops, bytes), or as measure(vector) (a front's
    axis pair: questions.Axes.measure); part None = the index"""
    measure = measure or compose.currencies
    per, tot = {}, compose.zero(data.composer)
    for fam, q, often, params in w["items"]:
        d = data[(fam, q)]
        us = {u for v in d["cost"].values() for u in v if u is not None}
        t, pu = compose.query_cost(d, compose.params_of(d, params), assign, us, by_unit=True)
        compose.add(tot, t, often)
        for u, vec in pu.items():
            per.setdefault(u, compose.zero(data.composer))
            compose.add(per[u], vec, often)
    return measure(tot), {u: measure(v) for u, v in per.items()}


def mix_record(assign, c, pc, names=("ops", "bytes")):
    """one mix as the JSON files keep it: assignment text, setting per part, totals (its two currencies, `names`),
    totals per part"""
    return {"assignment": assignment_label(assign),
            "assign": {"|".join(u): v for u, v in assign.items()}, names[0]: c[names[0]], names[1]: c[names[1]],
            "units": {str(u): v for u, v in pc.items()}}


def uniform_name(assign):
    """the name of the uniform layout a record's `assign` is ({"props|Person": setting, "adj|knows": setting, ...}: one
    setting for every props part, one for every list part), by the one rule (kvevaluator.layouts.name_of); a reader
    names a stored uniform row by it, never by a name the file holds. ValueError when the assignment is no uniform layout"""
    from kvevaluator.layouts import cell_id
    sides = [{v for k, v in assign.items() if k.startswith(kind + "|")} for kind in ("props", "adj")]
    if any(len(s) != 1 for s in sides):
        raise ValueError(f"not a uniform layout: {sorted(sides[0])} properties settings, {sorted(sides[1])} list settings")
    return name_of(cell_id(*(s.pop() for s in sides)))


def _axes_parts(axes):
    """(the cost's measure, a record's two keys) of a front's axes (questions.Axes); None: currencies, ops and bytes"""
    return (None, ("ops", "bytes")) if axes is None else (axes.measure, axes.names)


def _amount(v, name):
    """a walk's progress amount: bytes in GB, another currency as it is"""
    return f"{v / 1e9:.1f} GB" if name == "bytes" else f"{v:.6g} {name}"


def complete_front(pb, data, w, starts, progress_every=200, max_nodes=None, stats=None, plain=True, lagrange=None,
                   seconds=None, axes=None):
    """(the front's mix records, fewest bytes first; the search nodes) of workload w on problem pb. starts: solver
    starting points (pb.from_assign of known mixes); they only speed the search up. max_nodes: each step's node limit
    (search.solve); stats (a dict) gets "steps" and "unproven": how many steps stopped at the limit (a point of such a
    step is a mix that fits, not proven the fewest wops there, so the walk is not proven complete). plain: the grid's
    three settings only (search.solve); False: every option the problem holds (Chunked at other sizes). lagrange:
    search.lagrange_bounds for every capped step (more lower bounds; the answers are the same). seconds: stop after the
    step that passes this wall time; stats["stopped"] says so (the points found are exact, the front is incomplete).
    axes (questions.Axes): the two currencies pb's "ops" and "bytes" hold (pb.pair) and how a mix's cost vector gives them;
    None: wops and bytes. A record names them axes.names"""
    measure, (nx, ny) = _axes_parts(axes)
    mixes, cap, nodes_all, t1 = [], None, 0, time.time()
    steps = unproven = 0
    while True:
        fits = [s for s in starts if cap is None or pb.total(s)[1] <= cap]
        start = min(fits, key=lambda s: pb.total(s)) if fits else None
        info = {}
        assign, o, b, nodes, secs = solve(pb, "ops", cap, start, plain=plain, lagrange=lagrange, max_nodes=max_nodes,
                                          info=info)
        nodes_all += nodes
        steps += 1
        unproven += not info.get("proven", True)
        if stats is not None:
            stats.update(steps=steps, unproven=unproven, stopped=False)
        if assign is None:
            break
        if mixes and b >= mixes[-1][ny]:
            raise SystemExit(f"no progress: the solve returned {b} {ny} again under cap {cap}")
        c, pc = cost_by_part(data, w, assign, measure)
        assert abs(c[nx] - o) <= 1e-6 * o and abs(c[ny] - b) <= 1e-6 * b, (c, o, b)
        mixes.append(mix_record(assign, c, pc, (nx, ny)))
        if len(mixes) % progress_every == 0:
            print(f"  {len(mixes)} mixes, {_amount(b, ny)}, {time.time() - t1:.0f}s", flush=True)
        # the next cap: just below b once the solver's fit tolerance (cap + 1e-9 x cap) is added, so the next point has
        # fewer bytes; two points closer than ~1e-9 relative are one point at the solver's tolerance (a wider step
        # would skip a point of the front between them)
        cap = b * (1 - 2e-9)
        if seconds is not None and time.time() - t1 > seconds:
            if stats is not None:
                stats["stopped"] = True
            break
    mixes.reverse()                                # fewest bytes first
    return mixes, nodes_all


# --- the limits a workload is solved at, and a built mix's point -------------------------------------------------
CAPS = 32


def caps_of(pb, units, plain, caps=CAPS, reference=B_UNIT):
    """the sweep's limits (the rule, one place): ({cell: (combo, ops, bytes)} of the 9 single layouts,
    {cap_bytes: cap_kind}, whether the problem holds options beyond the grid's three: Chunked at other sizes). `caps`: how many evenly spaced (in log)
    limits; `reference`: the cell the named budgets B are taken against (cap_kind reference_x<B>; default B_UNIT)"""
    singles = {}                                   # cell -> (combo, ops, bytes)
    for cell in CELLS:
        combo = pb.from_assign(uniform(units, *cell[len("props_"):].split("_adj_")))
        singles[cell] = (combo, *pb.total(combo))
    by_ops = min(CELLS, key=lambda c: (singles[c][1], singles[c][2]))
    by_bytes = min(CELLS, key=lambda c: (singles[c][2], singles[c][1]))
    more = (not plain) and any(k > len(LAYOUTS) for k in pb.size)
    lo = solve(pb, "bytes", None, singles[by_bytes][0], plain=True)[2]
    hi = max(solve(pb, "ops", None, singles[by_ops][0], plain=True)[2], singles[by_ops][2])
    if more:                                       # the other options can reach fewer bytes: the caps cover both
        lo = min(lo, solve(pb, "bytes", None, singles[by_bytes][0])[2])
        hi = max(hi, solve(pb, "ops", None, singles[by_ops][0])[2])
    n = caps
    caps = {lo * (hi / lo) ** (i / (n - 1)): "grid" for i in range(n)} if hi > lo else {lo: "grid"}
    caps.update({singles[c][2]: "single" for c in CELLS if lo <= singles[c][2] <= hi})
    caps[math.sqrt(singles[by_bytes][2] * singles[by_ops][2])] = "midpoint"
    for b in NAMED_BUDGETS:                        # the named budgets: B x the reference cell's bytes
        caps[b * singles[reference][2]] = f"{B_KIND}{b:g}"
    return singles, caps, more


BUILT_FIELDS = (["workload", "mix", "spec_dir", "option_set", "measured_ops", "measured_bytes", "composed_ops", "composed_bytes",
                 "front_ops_at_its_bytes", "front_bytes", "measured_over_front_ops", "best_single_within_its_bytes",
                 "best_single_ops", "ops_single_over_measured", "bytes_x_reference"] + [f"measured_{m}" for m in MEASURES] +
                ["binary_git_sha", "binary_dirty", "tree_git_sha", "assignment"])


def spec_and_capture(arg, measured):
    """--built SPEC_DIR[=PLACE_DIR]: (the spec dir, the captures/<dataset>/<scale> dir its mixes were measured in;
    default `measured`, where every layout's captures sit)"""
    spec, _, cap = arg.partition("=")
    return spec, cap or measured


def built_points(name, w, measured, sets, ref, spec_dirs, criterion):
    """(the built points, the mixes left out: [{mix, missing, capture}] for a mix with no capture of a family the
    workload prices, e.g. the I01 mixes have no microbench capture, so a microbench workload leaves them out)"""
    settings, rows, left = mixes.grid_settings(), [], []
    need = sorted({fam for fam, _, _, _ in w["items"]})
    for spec_dir, mix_root in (spec_and_capture(x, measured) for x in spec_dirs):
        for path in sorted(glob.glob(os.path.join(spec_dir, "*.json"))):
            mix = os.path.basename(path)[:-5]
            reads = capture_in(mix_root, picks.RECORD_WORKLOAD)
            rec = reads.record(mix)
            if not os.path.exists(rec):
                print(f"   {mix}: not measured under {mix_root} -- no built point")
                continue
            spec = json.load(open(path))
            by_dir = "adjacency_by_etype_dir" in spec
            if mixes.retired_grouped(spec, mix):      # grouped props: no assignment
                print(f"   {mix}: grouped props, an option the search does not offer -- no built point")
                continue
            data, pb, units, singles = sets[by_dir]
            assign = mixes.assignment(spec, units, settings, mix)
            rule = data.composer.plan_rule
            measured = {"interactive": load_reads(reads.path, mix, criterion, rule=rule), "writes": load_writes(mix_root, mix, data.composer.chunk_view)}
            mb = capture_in(mix_root, "microbench")
            if os.path.exists(mb.path_of("counts", mix)):
                measured["microbench"] = load_reads(mb.path, mix, criterion, rule=rule)
            lacking = [fam for fam in need if fam not in measured]
            if lacking:
                print(f"   {mix}: no {', '.join(lacking)} capture under {mix_root} -- left out of {name}'s built points")
                left.append({"mix": mix, "missing": ", ".join(lacking), "capture": mix_root})
                continue
            # the built store priced as itself (store mode); the front point beside it is an option-mode solve
            meas, comp = compose.cost_or_refuse(w, measured, mix), cost_mix(w, data, assign, mode="store")   # price: the module here
            mc, cc = currencies(meas), currencies(comp)
            fits = [c for c in CELLS if singles[c][2] <= cc["bytes"] * (1 + 1e-9)]
            best = min(fits, key=lambda c: (singles[c][1], singles[c][2])) if fits else None
            # the front at the mix's own composed bytes, on the mix's unit set, the grid's three settings
            _, fops, fbyt, _, _ = solve(pb, "ops", cc["bytes"], pb.from_assign(assign), plain=True)
            b = json.load(open(rec))
            rows.append({"workload": name, "mix": mix, "spec_dir": os.path.basename(os.path.abspath(spec_dir)),
                         "option_set": "per type and direction" if by_dir else "per type",
                         "measured_ops": round(mc["ops"], 3), "measured_bytes": round(mc["bytes"], 3),
                         "composed_ops": round(cc["ops"], 3), "composed_bytes": round(cc["bytes"], 3),
                         "front_ops_at_its_bytes": round(fops, 3), "front_bytes": round(fbyt, 3),
                         "measured_over_front_ops": round(mc["ops"] / fops, 4), "best_single_within_its_bytes": best or "",
                         "best_single_ops": singles[best][1] if best else "",
                         "ops_single_over_measured": round(singles[best][1] / mc["ops"], 4) if best else "",
                         "bytes_x_reference": round(mc["bytes"] / ref, 4), **{f"measured_{m}": round(meas[m], 3) for m in MEASURES},
                         "binary_git_sha": b["binary"]["binary_git_sha"], "binary_dirty": b["binary"]["binary_dirty"],
                         "tree_git_sha": b["tree"]["git_sha"], "assignment": assignment_label(assign)})
    return rows, left


# --- reading a named budget back from a front or sweep file (one place for every reader) --------------------------------
B_KIND = "reference_x"     # cap_kind of a named budget: reference_x<B> = B x B_UNIT's bytes
OLD_B_KIND = "nebula_x"    # the same in a file in Co-Sp units: B x Co-Sp's bytes (cap_x_nebula, bytes_x_nebula)


def b_kind(rows):
    """the cap_kind prefix of a file's named budgets: reference_x, or nebula_x in a file in Co-Sp units"""
    kinds = {k for r in rows for k in (B_KIND, OLD_B_KIND) if r["cap_kind"].startswith(k)}
    if len(kinds) != 1:
        raise SystemExit(f"a front file with {sorted(kinds) or 'no'} named-budget kinds (want one of {B_KIND}, {OLD_B_KIND})")
    return kinds.pop()


def b_of(kind):
    """B of a named budget's cap_kind ('reference_x2' -> 2.0; 'nebula_x0.5' -> 0.5), None for another kind"""
    for k in (B_KIND, OLD_B_KIND):
        if kind.startswith(k):
            return float(kind[len(k):])
    return None


def ref_bytes_of(rows):
    """(B = 1 in bytes, the unit's cell) of a front or sweep file's rows: the cap of its B = 1 rows; the cell is B_UNIT,
    or OLD_B_UNIT (Co-Sp) in a file in Co-Sp units"""
    kind = b_kind(rows)
    caps = {float(r["cap_bytes"]) for r in rows if r["cap_kind"] == f"{kind}1"}
    if len(caps) != 1:
        raise SystemExit(f"a front file with {len(caps)} different {kind}1 caps (want 1)")
    return caps.pop(), (B_UNIT if kind == B_KIND else OLD_B_UNIT)


def unit_text(cell):
    """how a figure or report names B's unit: Sp-Co's bytes, or Co-Sp's bytes (a file in Co-Sp units)"""
    return f"{name_of(cell)}'s bytes" + ("" if cell == B_UNIT else " (a file in Co-Sp units)")


def x_of(row, what):
    """a row's ratio to B = 1: what = 'cap' (cap_x_reference) or 'bytes' (bytes_x_reference); a file before
    names them cap_x_nebula and bytes_x_nebula (in Co-Sp's bytes)"""
    for c in (f"{what}_x_reference", f"{what}_x_nebula"):
        if row.get(c) not in (None, ""):
            return float(row[c])
    raise SystemExit(f"a row with neither {what}_x_reference nor {what}_x_nebula")
