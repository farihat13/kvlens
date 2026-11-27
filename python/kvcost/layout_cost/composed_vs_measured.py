"""Composed against measured: the one comparer of a BUILT mixed layout's measured counts with its cost composed from
the uniform captures (compose.py's rule: each part's rows from the uniform capture with that part's setting, the plan
picked per query). Every reader of "does a built mix equal its composed cost" calls `compare`: `kvcost check-mix`, the
gate tests/gates/check_mix_vs_built.py and the study experiments/mix_benefit/composed_vs_measured.py (which writes the
paper's composition check from its rows).

What is compared, per built mix (`Mix`), on the captures alone (no store is opened):
  - the plan pick of every read query: the built store's own pick (its capture under the plan rule) against the
    composed pick (compose.query_pick over the uniform parts); a different name is an exact tie only when both plans
    cost the same on the built store in every raw channel on every param, else a difference;
  - the candidate plans usable on each side: the same set;
  - every (query, param, plan, step, store part) row of the picked plan and of the other candidates, reported and
    held-out params, in every raw channel (S, G, N, P, D, their misses, each byte lane);
  - per workload, the sums of those rows, and the composed sum against compose.cost_mix (compose's own code path).
Any difference outside the miss-key bytes is a finding. Chunked at any chunk size: a spec's chunk sizes other than
the grid's (`settings`' own) are read from the spec (`sizes_of`), each composed from that size's Chunked/Chunked capture
in the grid's place.

THE MISS-KEY RULE (`MISS_RATIO`, `miss_gap_ok`), one for every caller. The key a missed seek or get lands on belongs to
whatever part sits next to it in the store, so its size can differ between a mixed store and the uniform store a part
was composed from (a Split per-edge key after a Condensed list). The miss COUNTS are compared exactly (S_miss, G_miss,
N_miss); the miss-key BYTES of each (mix, workload) cell, for the reads and for the writes' own reads apart, must be
within 0.5x-2x of the composed (meta's keys are 10-19 B), and 0 on both sides or on neither. Rows keep their miss-key
bytes reported apart (by_row's all_miss_key_bytes), never bounded one by one: a row's neighbour can be a key of another
size, the sum over a workload cannot drift far. Chosen as the stricter of the two rules before (the bound, against
"reported apart, unbounded"); on the paper's built mixes the measured / composed miss-key bytes are 1.0000006x-1.096x
(SF3 Interactive and BI, JOB; paper-results' studies/latency-mixes-*/.../check-composed-vs-measured), well inside it.

Exit codes of the callers: 0 equal, 1 differs (or 0 rows compared), 2 refused (`Refused`: the inputs cannot be compared
at all: no built mix, a spec that is not its run record's, a chunk size with no Chunked/Chunked capture).
"""
import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
from typing import NamedTuple

from kvevaluator.capture import capture_layout
from kvevaluator.capture.step_record import (HIT_BYTE_LANES as HIT_LANES, MISS_BYTE_LANES as MISS_LANES,
                                             WRITE_BYTE_LANES as WRITE_LANES)
from kvevaluator.layouts import cell_id
from kvevaluator.paths import ROOT
from kvevaluator.output_tree import HELD_OUT, capture_in, layout_capture, writes_layout_capture
from kvevaluator.plan_choice.pick_record import RECORD_WORKLOAD
from kvevaluator.weights import DEFAULT as WEIGHTS_DEFAULT
from kvcost.layout_cost import compose, mixes
from kvcost.layout_cost import cost_models as CM
from kvcost.layout_cost.picks import PlanRule, choose_cell
from kvcost.layout_cost.settings import size_name, size_options

COUNTS = ("S", "G", "N", "P", "D")
ROW_CHANNELS = COUNTS + ("S_miss", "G_miss", "N_miss") + HIT_LANES + MISS_LANES + WRITE_LANES
PLANS = ("the picked plan", "other candidate plans")
# three row-level groups beside the single channels: a row is equal in a group when it is equal in every channel of it
ROW_GROUPS = {"all_op_counts": COUNTS + ("S_miss", "G_miss", "N_miss"), "all_bytes_without_miss_keys": HIT_LANES + WRITE_LANES,
              "all_miss_key_bytes": MISS_LANES}
# the workload channels a miss-key byte is part of: they may differ, within the miss-key rule
MISS_AFFECTED = {"read_seek_miss_key_bytes", "read_next_miss_key_bytes", "read_get_miss_key_bytes", "read_miss_key_bytes",
                 "writes_own_read_miss_key_bytes", "bytes", "ops_plus_bytes_per_4kb"}
MISS_TOTALS = ("read_miss_key_bytes", "writes_own_read_miss_key_bytes")   # the two lanes the miss-key rule bounds
MISS_RATIO = 2                  # measured miss-key bytes within [1/MISS_RATIO, MISS_RATIO] x composed (the module doc)
PLANTS = ("count", "pick", "one-sided")


class Refused(Exception):
    """the inputs cannot be compared (not a difference): what is missing, and how to make it"""


class Mix(NamedTuple):
    """one built mix to compare: its set's name (its spec dir's), the captures/<dataset>/<scale> dir it was measured
    in, its layout name and its spec file"""
    set_name: str
    root: str
    name: str
    spec: str


@dataclass
class Result:
    """compare's rows (the study writes them as CSVs) and its findings"""
    by_workload: list = field(default_factory=list)
    by_row: list = field(default_factory=list)
    mixes: list = field(default_factory=list)
    picks: list = field(default_factory=list)
    findings: list = field(default_factory=list)   # anything but a miss-key difference within the rule
    notes: list = field(default_factory=list)      # what the run said along the way, in order
    grouped_skipped: list = field(default_factory=list)
    caught: dict = field(default_factory=dict)     # mix -> its findings (the --swap control reads it)
    planted: bool = False
    miss_ratios: list = field(default_factory=list)   # (mix, workload id, lane, measured / composed) where they differ

    @property
    def cells(self):
        return len({(r["mix_set"], r["mix"], r["workload_id"]) for r in self.by_workload})

    def rows(self, params=None, plans=PLANS[0]):
        return sum(r["rows_compared"] for r in self.by_row if r["channel"] == "S" and r["plans"] == plans
                   and params in (None, r["params"]))

    @property
    def ok(self):
        return not self.findings and self.cells and self.rows()

    def counts_line(self, sf):
        n, h = self.rows(), self.rows("held-out")
        return (f"{len(self.mixes)} built mixes, {self.cells} (mix, workload) cells, {n:,} (query, param, plan, step, part) "
                f"rows of the picked plans compared ({n - h:,} on the reported params, {h:,} on the held-out params), and "
                f"{self.rows(plans=PLANS[1]):,} rows of the other candidate plans (sf{sf})")

    def miss_line(self):
        if not self.miss_ratios:
            return "miss-key bytes: equal in every cell"
        r = [x[3] for x in self.miss_ratios if x[3] is not None]
        return (f"miss-key bytes: {len(self.miss_ratios)} (cell, lane) differ, measured / composed "
                + (f"{min(r):.7f}x-{max(r):.7f}x" if r else "one-sided") + f" (the rule: {1 / MISS_RATIO:.1f}x-{MISS_RATIO}x)")


def miss_gap_ok(measured, composed):
    """THE miss-key rule (the module doc): equal, or both above 0 and measured / composed within [1/MISS_RATIO,
    MISS_RATIO]"""
    if measured == composed:
        return True
    if measured <= 0 or composed <= 0:
        return False
    return Fraction(1, MISS_RATIO) <= Fraction(measured) / Fraction(composed) <= MISS_RATIO


def packs(obj):
    """every chunk size (a component's owners.pack, in bytes) a spec names, anywhere in it"""
    if isinstance(obj, dict):
        own = {obj["pack"]} if isinstance(obj.get("pack"), int) else set()
        return own.union(*(packs(v) for v in obj.values()))
    if isinstance(obj, list):
        return set().union(*(packs(v) for v in obj))
    return set()


def sizes_of(specs, settings):
    """the chunk size names (4k) the specs use beyond the settings' own Chunked (the grid's 64 KB, or the caller's):
    each is composed from that size's Chunked/Chunked"""
    own = set().union(*(packs(json.loads(k)) for kind in settings.values() for k in kind))
    out = set()
    for spec in specs:
        for p in packs(spec) - own:
            if p % 1024:
                raise Refused(f"a part Chunked at {p} bytes: a chunk size is a whole number of KB, its Chunked/Chunked "
                              f"named <N>k")
            out.add(size_name(p))
    return sorted(out, key=lambda s: int(s[:-1]))


def row_vec(r):
    """a parts row's raw channels as integers"""
    return {c: int(float(r.get(c) or 0)) for c in ROW_CHANNELS}


_ROWS = {}


def part_rows(path):
    """{(query, plan, param): {(step_idx, step, part): vec}} of one parts file (a write has the one plan 'write')"""
    if path not in _ROWS:
        out = defaultdict(dict)
        write = capture_layout.is_writes_file(path)          # a writes capture's file: either shape, the plan 'write'
        for r in (capture_layout.write_rows(path)[2] if write else capture_layout.read_rows(path)):
            k, s = (r["query"], "write" if write else r["body"], r["param_idx"]), (int(r["step_idx"]), r["step"], r["part"])
            if s in out[k]:
                raise Refused(f"{path}: two rows for {k} {s}")
            out[k][s] = row_vec(r)
        _ROWS[path] = out
    return _ROWS[path]


def parts_file(root, fam, kind, cell, dataset, held=False):
    """a layout's per-part rows: the reported params, or (held=True, the reads) the held-out params; found as compose.load
    finds them (output_tree.layout_capture: the layout measured itself, else composed)"""
    if kind == "read":
        return layout_capture(root, fam, cell, HELD_OUT if held else None).path_of("parts", cell)
    return writes_layout_capture(root, dataset, cell).path_of("parts", cell)


def workload_channels(reads, writes):
    """the named channels of one workload total, from its read items' and its write items' summed raw channels"""
    # weighted ops (CM.FORMULAS' wops), kept exact: the default weights are stored x100 as whole numbers
    wops = lambda v: sum(Fraction(int(WEIGHTS_DEFAULT[c]), int(WEIGHTS_DEFAULT["G"])) * v[c] for c in ("G", "S", "N"))
    out = {}
    for c in ("S", "G", "N"):
        out[f"read_{c}"] = reads[c]
    out["read_weighted_ops"] = wops(reads)
    for c in HIT_LANES:
        out[f"read_{c}_bytes"] = reads[c]
    out["read_bytes_without_miss_keys"] = sum(reads[c] for c in HIT_LANES)
    for c in MISS_LANES:
        out[f"read_{c}_bytes"] = reads[c]
    out["read_miss_key_bytes"] = sum(reads[c] for c in MISS_LANES)
    out["write_P"], out["write_D"] = writes["P"], writes["D"]
    out["write_ops"] = writes["P"] + writes["D"]
    out["write_key_bytes"], out["write_value_bytes"] = writes["write_key"], writes["write_value"]
    out["bytes_written"] = writes["write_key"] + writes["write_value"]
    for c in ("S", "G", "N"):
        out[f"writes_own_read_{c}"] = writes[c]
    out["writes_own_read_weighted_ops"] = wops(writes)
    out["writes_own_read_bytes_without_miss_keys"] = sum(writes[c] for c in HIT_LANES)
    out["writes_own_read_miss_key_bytes"] = sum(writes[c] for c in MISS_LANES)
    # the two totals a workload is judged in (compose.currencies), then the one derived number
    out["ops"] = out["read_weighted_ops"] + out["writes_own_read_weighted_ops"] + out["write_ops"]
    out["bytes_without_miss_keys"] = (out["read_bytes_without_miss_keys"] + out["writes_own_read_bytes_without_miss_keys"]
                                      + out["bytes_written"])
    out["bytes"] = out["bytes_without_miss_keys"] + out["read_miss_key_bytes"] + out["writes_own_read_miss_key_bytes"]
    out["ops_plus_bytes_per_4kb"] = CM.wops_bytes4k(out["ops"], out["bytes"])
    return out


def mixes_in(spec_dir, root, families, dataset):
    """[Mix] of every spec of `spec_dir` measured under `root` (a parts file of one of `families`), by name"""
    out = []
    for f in sorted(os.listdir(spec_dir)):
        name = f[:-5]
        if f.endswith(".json") and any(os.path.exists(parts_file(root, fam, kind, name, dataset)) for fam, kind in families):
            out.append(Mix(os.path.basename(os.path.normpath(spec_dir)), os.path.abspath(root), name,
                           os.path.join(os.path.abspath(spec_dir), f)))
    return out


def drift_findings(mixes_, families):
    """each built mix's capture against HEAD's candidate set (grid_capture.candidate_drift, both directions): a capture
    that predates a plan file, or that ran a body HEAD now refuses on this mix, is a finding"""
    from kvevaluator.measure.grid_capture import candidate_drift, load_runs
    from kvevaluator.plan_choice.candidates import capture_plan_pipeline, capture_tree
    out = []
    for m in mixes_:
        for fam, kind in families:
            cap = capture_in(m.root, fam)
            if kind != "read" or not os.path.exists(cap.path_of("counts", m.name)):
                continue
            drift = candidate_drift(fam, m.spec, load_runs(cap.path, m.name), capture_tree(cap.path, m.name),
                                    pipeline=capture_plan_pipeline(cap.path, m.name))
            for what in ("unmeasured", "refused"):
                for q, names in sorted(drift[what].items()):
                    out.append(f"{m.set_name}/{m.name} {fam} {q}: drift, {what}: {names} (the capture against HEAD's "
                               f"candidate set)")
    return out


def compare(grid, mixes_, *, sf, dataset, families, workloads, criterion=None, rule=None, settings=None,
            by_direction=False, swap=None, plant=None, held_out_rule_for_unrecorded=False, required=(), part_ok=False,
            queries=None):
    """compare every built mix of `mixes_` with its cost composed from `grid` (a captures/<dataset>/<scale> dir of the
    uniform layouts) -> Result. `families`: ((family, read|write), ...) composed; `workloads`: {name: workload} summed
    (a workload is summed when the mix has every family it reads; one of `required` must be, else a finding; with
    `part_ok` a required one is summed on the families it has). `rule`: the plan rule of both sides (default the pick
    record's). `settings`: the components a spec is matched against (mixes.grid_settings; a caller's own for another
    grid chunk size). `by_direction`: every mix on per-direction units. `swap` out|in: the direction control (that
    direction's lists composed at the other direction's layout). `plant`: a control (PLANTS) that must make a finding:
    one SEEK added to one measured row, one built pick replaced by another candidate, one measured row removed.
    `held_out_rule_for_unrecorded`: a mix with no row in the pick record (never built at the deciding scale) is
    compared with both sides deciding by this capture's held-out rule, said in the notes. `queries`: these queries
    only, in the rows and the workload sums (a slice, measured so on the mix and the uniform layouts); default every
    query of `grid`."""
    grid = os.path.abspath(grid)
    fams = dict(families)
    if queries is not None:
        workloads = {n: dict(w, items=[it for it in w["items"] if it[1] in queries]) for n, w in workloads.items()}
    rule = rule or PlanRule()
    res = Result()
    if not mixes_:
        raise Refused(f"no built mixes to compare in {grid}")
    record = rule.load_record()
    specs = {}
    for m in mixes_:                    # the spec a mix was measured with is its run record's; it must be its file's
        recs = [capture_in(m.root, f).record(m.name) for f, k in families if k == "read"]
        recs = [r for r in recs if os.path.exists(r)]     # the run record of a read family the mix was measured on
        if not recs:
            raise Refused(f"{m.set_name}/{m.name}: no read family's run record under {m.root}")
        rec = json.load(open(recs[0]))
        if rec["spec"] != json.load(open(m.spec)):
            raise Refused(f"{m.set_name}/{m.name}: the run record's spec is not {m.spec}")
        specs[m] = rec
    settings = settings or mixes.grid_settings()
    sizes = sizes_of([r["spec"] for r in specs.values()], settings)
    if sizes:
        rfam = next(f for f, k in families if k == "read")
        missing = [s for s in sizes if not os.path.exists(parts_file(grid, rfam, "read", size_options([s], grid)[0][2], dataset))]
        if missing:
            raise Refused(f"a mix has a part Chunked at {', '.join(missing)}: its Chunked/Chunked is not measured in "
                          f"{grid} (kvcost measure --dataset {dataset}={sf} "
                          + " ".join(f"Ch{s}-Ch{s}" for s in missing) + ")")
        res.notes.append(f"[sizes] Chunked at {', '.join(sizes)} besides the grid's, from the mixes' specs: each from its "
                         f"Chunked/Chunked in {grid}")
        settings = {k: dict(v) for k, v in settings.items()}
        for kind, comps in mixes.grid_settings([f"chunked_c{s}" for s in sizes]).items():
            for k, oid in comps.items():
                settings[kind].setdefault(k, oid)
    sized = tuple(size_options(sizes, grid))
    composer = compose.Composer(criterion=criterion, families=tuple(fams.items()), plan_rule=rule, chunk_view="measured",
                                sizes=sized)
    sized_src = {oid: (os.path.abspath(sroot), scell) for oid, sroot, scell in compose.chunk_sizes(grid, composer)}
    loaded = {}
    bad = res.findings
    for m in mixes_:
        set_name, root, name, spec_path = m
        rec = specs[m]
        spec = rec["spec"]
        if mixes.retired_grouped(spec, name):      # no assignment to compose
            res.grouped_skipped.append(f"{set_name}/{name}")
            continue
        if swap and "adjacency_by_etype_dir" not in spec:
            res.notes.append(f"{set_name}/{name}: no per-direction layout -- not part of the --swap control")
            continue
        n_bad = len(bad)
        per_dir = by_direction or "adjacency_by_etype_dir" in spec
        if per_dir not in loaded:
            loaded[per_dir] = compose.load(grid, by_dir=per_dir, dataset=dataset, composer=composer)
        data = loaded[per_dir]
        mfams = [f for f in sorted(fams) if os.path.exists(parts_file(root, f, fams[f], name, dataset))]
        assign = mixes.assignment(spec, compose.grid_units(data), settings, name, swap=swap)
        # a held-out param can read a part no reported param of its query reads (IC12 at SF30: a Person's and a Tag's
        # props): the row comparison walks those parts too. The pick and the sums keep the reported parts, as compose does
        held_units = {key: {u for per in d.get("cost_h", {}).values() for u in per if u is not None} for key, d in data.items()}
        every_unit = sorted(set(compose.grid_units(data)) | set().union(*held_units.values()), key=str)
        assign_rows = mixes.assignment(spec, every_unit, settings, name, swap=swap)
        built = {}
        for fam in mfams:
            if fams[fam] == "read":
                rdir = capture_in(root, fam).path
                # the record's family by the mix's spec (the pick record), the other read families as the rule says
                unrecorded = (fam == RECORD_WORKLOAD and held_out_rule_for_unrecorded and str(record.decided_at) != str(sf)
                              and not record.has_cell(RECORD_WORKLOAD, name))
                if unrecorded:              # never built at the deciding scale: this capture's held-out rule, on both sides
                    res.notes.append(f"[unrecorded] {set_name}/{name}: no row in the pick record (decided at "
                                     f"sf{record.decided_at}); both sides decide by sf{sf}'s held-out rule: a check of "
                                     f"composition, not a reported pick")
                fam_rule = rule.but(in_sample=True) if unrecorded else rule
                built[fam] = (choose_cell(rdir, name, criterion, rule=fam_rule, spec_path=spec_path) if fam == RECORD_WORKLOAD
                              else choose_cell(rdir, name, criterion, rule=fam_rule))
                if unrecorded:
                    rules = {d.get("rule") for d in built[fam].values()}
                    if not all(str(r).startswith("held-out") for r in rules):
                        raise Refused(f"{set_name}/{name}: an unrecorded mix must be decided by the held-out rule, got "
                                      f"{sorted(map(str, rules))}")
        comp_qp, meas_qp = {}, {}               # (family, query, param) -> Counter of raw channels
        picks = pick_diff = pick_ties = cand_sets = cand_plans = cand_diff = 0
        for fam in mfams:
            write = fams[fam] == "write"
            mine = part_rows(parts_file(root, fam, fams[fam], name, dataset))
            # the row comparison runs on the reported params and, where the mix has them, on the held-out params
            # (the same plan; the workload sums below use the reported params only, as every cost does)
            held_file = parts_file(root, fam, fams[fam], name, dataset, held=True)
            mine_of = {"reported": mine, **({"held-out": part_rows(held_file)} if not write and os.path.exists(held_file) else {})}
            # ... and on every candidate plan, not only the picked one (PLANS: the pick's rows are the cells' cost;
            # the other candidates' rows are what the plan rule reads when it compares plans)
            keys = [(k, pl) for k in mine_of for pl in PLANS]
            stat = {k: {c: Counter() for c in ROW_CHANNELS + tuple(ROW_GROUPS)} for k in keys}
            one_side = {k: Counter() for k in keys}
            for (f2, q), d in sorted((k, v) for k, v in data.items() if k[0] == fam and (queries is None or k[1] in queries)):
                units = {u for per in d["cost"].values() for u in per if u is not None}
                params = compose.params_of(d, None)
                best, usable, _ = compose.query_pick(d, params, assign, units, mode="store")
                if best is None:
                    bad.append(f"{set_name}/{name} {fam} {q}: no usable plan composed")
                    continue
                mplan = "write" if write else built[fam].get(q, {}).get("body")
                total = lambda plan, i: sum((Counter(v) for v in mine.get((q, plan, i), {}).values()), Counter())
                if plant == "pick" and not res.planted and not write and mplan == best:
                    # another candidate the built store ran, of another cost on some param: it must be reported as a pick that differs
                    alt = next((v for v in sorted(usable) if v != best and any(mine.get((q, v, i)) and total(v, i) != total(best, i)
                                                                               for i in params)), None)
                    if alt:
                        mplan, res.planted = alt, True
                        res.notes.append(f"[control] the built store's pick of {set_name}/{name} {fam} {q} replaced by "
                                         f"{alt!r} (it picked {best!r})")
                if not write:
                    picks += 1
                    if mplan != best:
                        # another name: an exact tie (the two plans cost the same on the built store in every raw
                        # channel, on every param; the store keeps its planner's body, compose the first by name) is
                        # counted apart and compared on the composed plan's rows; anything else is a finding
                        if mplan is None or not all(mine.get((q, best, i)) and total(mplan, i) == total(best, i) for i in params):
                            pick_diff += 1
                            bad.append(f"{set_name}/{name} {fam} {q}: the built store's pick {mplan!r}, the composed {best!r}")
                            continue
                        pick_ties += 1
                        mplan = best
                playouts = {compose.validity_layout(assign[u], d["composer"]) for u in units if u[0] == "props"} or set(compose.LAYOUTS)
                alayouts = {compose.validity_layout(assign[u], d["composer"]) for u in units if u[0] == "adj"} or set(compose.LAYOUTS)
                first = cell_id(min(playouts), min(alayouts))

                def source(u):                  # compose._composed's rule: where a unit's rows are read from
                    if u is None:
                        return grid, first
                    if assign_rows[u] in sized_src:     # Chunked at its size: that size's Chunked/Chunked capture
                        return sized_src[assign_rows[u]]
                    if u[0] == "props":
                        return grid, cell_id(assign_rows[u], min(alayouts))
                    return grid, cell_id(min(playouts), assign_rows[u])
                others = []
                if not write:                   # the candidate plans usable on each side must be the same set
                    usable_m = set(built[fam][q]["costs"]) | set(built[fam][q].get("measure_only", {}))
                    cand_sets += 1
                    cand_plans += len(usable)
                    if set(usable) != usable_m:
                        cand_diff += 1
                        bad.append(f"{set_name}/{name} {fam} {q}: candidate plans differ: composed only "
                                   f"{sorted(set(usable) - usable_m)}, built only {sorted(usable_m - set(usable))}")
                    others = sorted((set(usable) & usable_m) - {best, built[fam][q]["body"]})
                    res.picks.append({"scale": f"sf{sf}", "mix_set": set_name, "mix": name, "family": fam, "query": q,
                                      "composed_pick": best, "built_pick": built[fam][q]["body"],
                                      "candidates_composed": " ".join(sorted(usable)), "candidates_built": " ".join(sorted(usable_m)),
                                      "candidate_sets_equal": int(set(usable) == usable_m)})
                for plans, cplan, mpl, pk, i in (
                        [(pl, cp, mp, "reported", i) for pl, cp, mp in [(PLANS[0], best, mplan)] + [(PLANS[1], v, v) for v in others]
                         for i in params]
                        + [(pl, cp, mp, "held-out", k[2]) for pl, cp, mp in [(PLANS[0], best, mplan)] + [(PLANS[1], v, v) for v in others]
                           for k in sorted(mine_of.get("held-out", {})) if k[:2] == (q, mp)]):
                    held = pk == "held-out"
                    pk = (pk, plans)
                    crow = {}
                    for u in sorted(units | held_units[(f2, q)] if held else units, key=str) + [None]:
                        sroot, scell = source(u)
                        for s, v in part_rows(parts_file(sroot, fam, fams[fam], scell, dataset, held)).get((q, cplan, i), {}).items():
                            if compose.unit_of(s[2], per_dir) == u:
                                crow[s] = v
                    mrow = dict(mine_of[pk[0]].get((q, mpl, i), {}))
                    if plant == "count" and not res.planted and mrow:
                        s0 = sorted(mrow)[0]
                        mrow[s0] = dict(mrow[s0], S=mrow[s0]["S"] + 1)
                        res.planted = True
                        res.notes.append(f"[control] one SEEK added to {set_name}/{name} {fam} {q} param {i} step {s0}")
                    if plant == "one-sided" and not res.planted and mrow:
                        s0 = sorted(mrow)[0]
                        del mrow[s0]
                        res.planted = True
                        res.notes.append(f"[control] one measured row removed: {set_name}/{name} {fam} {q} param {i} step {s0}")
                    for s in sorted(set(crow) | set(mrow)):
                        if s not in crow or s not in mrow:
                            one_side[pk]["measured only" if s in mrow else "composed only"] += 1
                            continue
                        for c in ROW_CHANNELS:
                            stat[pk][c]["rows"] += 1
                            stat[pk][c]["equal"] += crow[s][c] == mrow[s][c]
                        for g, chans in ROW_GROUPS.items():
                            stat[pk][g]["rows"] += 1
                            stat[pk][g]["equal"] += all(crow[s][c] == mrow[s][c] for c in chans)
                    if not held and plans == PLANS[0]:
                        comp_qp[(fam, q, i)] = sum((Counter(v) for v in crow.values()), Counter())
                        meas_qp[(fam, q, i)] = sum((Counter(v) for v in mrow.values()), Counter())
            for pk in keys:
                if write and pk[1] != PLANS[0]:
                    continue                    # a write has one plan
                for c in ROW_CHANNELS + tuple(ROW_GROUPS):
                    if not write and c in ("P", "D") + WRITE_LANES:
                        continue                # a read family has no P, D or written bytes
                    n, e = stat[pk][c]["rows"], stat[pk][c]["equal"]
                    res.by_row.append({"scale": f"sf{sf}", "mix_set": set_name, "mix": name, "family": fam, "params": pk[0],
                                       "plans": pk[1], "channel": c, "rows_compared": n, "rows_equal": e, "rows_differ": n - e,
                                       "rows_measured_only": one_side[pk]["measured only"],
                                       "rows_composed_only": one_side[pk]["composed only"]})
                    if c not in MISS_LANES + ("all_miss_key_bytes",) and n != e:
                        bad.append(f"{set_name}/{name} {fam} ({pk[0]} params, {pk[1]}): {n - e} of {n} rows differ in {c}")
                if sum(one_side[pk].values()):
                    bad.append(f"{set_name}/{name} {fam} ({pk[0]} params, {pk[1]}): rows on one side only: {dict(one_side[pk])}")
                if not stat[pk]["S"]["rows"] and pk[1] == PLANS[0]:
                    bad.append(f"{set_name}/{name} {fam} ({pk[0]} params): 0 rows compared")
        covered = []
        for wname, w in workloads.items():
            need = sorted({it[0] for it in w["items"]})
            got = [f for f in need if f in mfams]
            if got != need:
                if wname in required and got and part_ok:     # the picked workload on the families captured
                    w = dict(w, items=[it for it in w["items"] if it[0] in mfams])
                    res.notes.append(f"{set_name}/{name} {wname}: summed on its {'+'.join(got)} part (the families captured)")
                elif wname in required:
                    bad.append(f"{set_name}/{name} {wname}: required, and no capture of "
                               f"{', '.join(f for f in need if f not in mfams)}")
                    continue
                else:
                    continue
            missing = sorted({(fam, q) for fam, q, _, params in w["items"] for i in compose.params_of(data[(fam, q)], params)
                              if (fam, q, i) not in comp_qp})
            if missing:                         # a query with a finding above (a pick that differs): the workload is not summed
                bad.append(f"{set_name}/{name} {wname}: not summed, no compared rows for {missing}")
                continue
            covered.append(w["id"])
            tot = {"composed": {"read": Counter(), "write": Counter()}, "measured": {"read": Counter(), "write": Counter()}}
            for fam, q, often, params in w["items"]:
                idx = compose.params_of(data[(fam, q)], params)
                k = Fraction(often) / len(idx)
                kind = fams[fam]
                for side, src in (("composed", comp_qp), ("measured", meas_qp)):
                    for i in idx:
                        for c, v in src[(fam, q, i)].items():
                            tot[side][kind][c] += k * v
            cc = workload_channels(tot["composed"]["read"], tot["composed"]["write"])
            mm = workload_channels(tot["measured"]["read"], tot["measured"]["write"])
            # the composed sum against compose's own code path: compose.cost_mix
            pred = compose.cost_mix(w, data, mixes.assignment(spec, compose.workload_units(w, data), settings, name, swap=swap),
                                    mode="store")
            for mine_, theirs in ((cc["read_weighted_ops"], pred["read_ops"]), (cc["bytes_written"], pred["write_bytes"]),
                                  (cc["read_bytes_without_miss_keys"] + cc["read_miss_key_bytes"], pred["read_bytes"])):
                if abs(float(mine_) - theirs) > 1e-6 * max(1.0, abs(theirs)):
                    bad.append(f"{set_name}/{name} {wname}: the rows' composed total {float(mine_)} is not "
                               f"compose.cost_mix's {theirs}")
            for ch in cc:
                c, me = cc[ch], mm[ch]
                res.by_workload.append({"scale": f"sf{sf}", "mix_set": set_name, "mix": name, "workload_id": w["id"],
                                        "workload": wname, "channel": ch, "composed": repr(float(c)), "measured": repr(float(me)),
                                        "difference": repr(float(me - c)), "ratio": repr(float(me / c)) if c else "",
                                        "exact": int(c == me)})
                if c != me and ch not in MISS_AFFECTED:
                    bad.append(f"{set_name}/{name} {wname}: {ch} composed {float(c)} measured {float(me)}")
            for ch in MISS_TOTALS:              # the miss-key rule, on the cell's reads and its writes' own reads
                c, me = cc[ch], mm[ch]
                if c != me:
                    res.miss_ratios.append((name, w["id"], ch, float(me / c) if c else None))
                if not miss_gap_ok(me, c):
                    bad.append(f"{set_name}/{name} {wname}: {ch} measured {float(me):,.0f}, composed {float(c):,.0f}: outside "
                               f"the miss-key rule ({1 / MISS_RATIO:.1f}x-{MISS_RATIO}x, 0 on both sides or neither)")
        res.mixes.append({"scale": f"sf{sf}", "mix_set": set_name, "mix": name, "spec": os.path.relpath(spec_path, ROOT),
                          "per_direction": int(per_dir), "families": " ".join(mfams), "workloads": " ".join(covered),
                          "n_workloads": len(covered), "picks_compared": picks, "picks_same_plan": picks - pick_diff - pick_ties,
                          "picks_exact_tie_other_name": pick_ties, "picks_differ": pick_diff,
                          "candidate_sets_compared": cand_sets, "candidate_sets_differ": cand_diff, "candidate_plans": cand_plans,
                          "binary": rec["binary"]["binary_git_sha"][:9], "binary_dirty": rec["binary"]["binary_dirty"]})
        res.caught[name] = len(bad) - n_bad
        res.notes.append(f"{set_name}/{name}: families {' '.join(mfams)}; {len(covered)} workloads; {picks} plan picks "
                         f"compared: {picks - pick_diff - pick_ties} the same plan, {pick_ties} an exact tie under another "
                         f"name, {pick_diff} differ")
    if plant and not res.planted:
        bad.append(f"the control --plant {plant} found nothing to plant")
    return res
