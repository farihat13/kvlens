"""The pick of each grid cell's plan per query, from a capture's variant sweep. choose_cell applies the plan rule
(candidates.choose under the named criterion over the reported rows, the held-out rows beside them) and, when the
capture's scale is not the deciding one, the pick record's decision (inputs/picks/picks.csv, pick_record.load). The
rule itself (`choose`, `weighted`) lives in python/kvevaluator/plan_choice/candidates.py and is re-exported here.
"""
import glob
import json
import os
import sys
from dataclasses import KW_ONLY, dataclass, field, fields, replace
from kvevaluator import stale
from kvevaluator.paths import ROOT
from kvevaluator import workloads as _W
from kvevaluator.plan_choice.pick_record import RECORD_WORKLOAD

EXP = str(ROOT)
from kvevaluator.plan_choice.candidates import choose, weighted  # noqa: F401  (the shared rule: choose = the pick under the optimizer's criterion; weighted = the ops currency)
from kvevaluator.measure import grid_capture  # (grid_capture.PICKS_FILE: PlanRule's default record)
from kvevaluator.measure.grid_capture import (capture_pipeline_of, capture_sf, capture_tree_of, capture_workload,
                                              check_record_bodies, load_held_out, load_runs, spec_sha,
                                              spec_sha_of)
from kvevaluator.output_tree import Capture


DECIDING_SF = "3"       # the plan rule: the plan choice is decided at the largest reported scale (SF3 now; SF30 held)


def layout_store(rec, out_dir, cell):
    """THE identity of a priced capture of `cell`: the record store whose layout it is, or None. A
    capture measured under its own name is that store (store_key(cell)). A composed cell (compose_cells' run record says
    composed_from) is the store whose spec file has the same sha as its run record's spec: its own name first, else the
    one record store with that spec, else None (a layout no store has, e.g. Split props with 1 KB Chunked lists). A
    diagonal copied from a size set (named_as_the_grids_cell_from) is the store it was measured as. Every reader and the
    writer of the record go through here (bound_store, record_anchor, grid_pick_record.small_scale)."""
    from kvevaluator.plan_choice.pick_record import store_key
    path = Capture(out_dir).record(cell)
    r = {}
    if os.path.exists(path):
        with open(path) as f:
            r = json.load(f)
    if r.get("named_as_the_grids_cell_from"):
        return store_key(os.path.basename(r["named_as_the_grids_cell_from"])[:-len(".json")])
    if not r.get("composed_from"):
        # measured under a name the record does not hold (a built mix, mix_<sha>): the one record store with its spec,
        # so a layout gets its row whatever it was called (one layout, one store)
        own = store_key(cell)
        if rec.has_cell(RECORD_WORKLOAD, own) or not r.get("spec"):
            return own
        stores = _stores_of_spec(rec).get(spec_sha_of(r["spec"]), [])
        return stores[0] if len(stores) == 1 else own
    if not r.get("spec"):
        raise SystemExit(f"{cell}: a composed run record with no spec ({path}): its layout cannot be named -- compose it again")
    stores = _stores_of_spec(rec).get(spec_sha_of(r["spec"]), [])
    own = store_key(cell)
    if own in stores:
        return own
    if len(stores) > 1:
        raise SystemExit(f"{cell}: its composed spec is the layout of {len(stores)} record stores ({stores}): one layout, one "
                         f"store -- rewrite the record")
    return stores[0] if stores else None


def _stores_of_spec(rec):
    """{spec sha: [the record's stores whose spec file (inputs/specs/<set>/<store>.json) has it]}, sorted"""
    key = tuple(rec.stores(RECORD_WORKLOAD))
    if key not in _SPEC_STORES:
        out = {}
        for path in sorted(glob.glob(os.path.join(EXP, "inputs", "specs", "*", "*.json"))):
            name = os.path.basename(path)[:-len(".json")]
            if name in key:
                try:
                    out.setdefault(spec_sha(path), set()).add(name)
                except (ValueError, KeyError):
                    continue
        _SPEC_STORES[key] = {k: sorted(v) for k, v in out.items()}
    return _SPEC_STORES[key]


_SPEC_STORES = {}


def checked_at(rec, sf):
    """the stores the record says were checked at scale `sf` (pick_stores.json's bound scales: the stores the scale
    exception was decided on there, grid_pick_record.py --small-scale --record)"""
    return rec.bound_at(sf)


def bound_store(rec, sf, store):
    """THE binding rule: the record row of `store` (a layout_store identity) binds at scale `sf` only
    when `sf` is not the deciding scale and the record checked `store` at `sf` (checked_at); else None (the held-out rule,
    no anchor). One rule for a cell (recorded_picks, record_anchor) and a store-mode option (compose.recorded_for_option)."""
    if not rec or sf is None or store is None or str(sf) == str(rec.decided_at):
        return None
    return store if store in checked_at(rec, sf) else None


def bound_picks(rec, store, sf):
    """({query: the record's pin}, {query: this scale's own pick}) of a store bound at `sf` (bound_store): what it hands
    choose. A carried row (v1's selection, decided on no capture of ours) pins nothing (None: this scale takes the
    cheapest, as the deciding scale does, record_anchor) and is no own pick. One rule for a cell (recorded_picks) and a
    store-mode option (compose.recorded_for_option)"""
    from kvevaluator.plan_choice.pick_record import is_carried
    return ({q: None if is_carried(d) else d["body"] for q, d in rec.cell(RECORD_WORKLOAD, store).items()},
            {q: d["body"] for q, d in rec.exceptions(RECORD_WORKLOAD, sf, store).items() if not is_carried(d)})


def recorded_picks(out_dir, cell, rule, spec_path=None, criterion=None):
    """({query: variant}, {query: this scale's own variant}, {query: stale kind of a recorded body that moved}, [stale kinds
    of the whole cell]) recorded for this cell (inputs/picks/picks.csv, grid_pick_record.py) when
    the capture's scale is NOT the deciding one (bound_picks); (None, None) at the deciding scale or with no record at all. A record
    row for a cell this capture does not hold is a silent nothing only at the deciding scale; at another scale a cell
    with no record row is picked on its own capture, in-sample, and marked (pick_in_sample). Staleness: a mix entry whose
    spec sha differs from the spec file's is refused (a re-solved mix under the same name does not inherit an old pick); a
    (cell, query) whose recorded body moved since is marked (grid_capture.check_record_bodies)."""
    sf = capture_sf(out_dir, cell)
    if not rule.has_record():
        if sf != DECIDING_SF and rule.in_sample is not True and out_dir not in _NO_RECORD_SAID:
            _NO_RECORD_SAID.add(out_dir)
            print(f"[choose_cell] NO RECORD: {rule.record_name()} does not exist, so the picks at sf{sf} are the held-out "
                  f"rule's alone, not the sf{DECIDING_SF} decision's (the plan rule). Write it at the deciding scale: "
                  f"python -m kvcost.layout_cost.grid_pick_record --sf {DECIDING_SF}", file=sys.stderr)
        return None, None, {}, []
    rec = rule.load_record()
    if sf == str(rec.decided_at):
        return None, None, {}, []
    from kvevaluator.plan_choice.candidates import MARGIN, resolve_criterion
    want = resolve_criterion(criterion)[0] if criterion else rec.criterion
    if rec.criterion != want or float(MARGIN if rec.margin is None else rec.margin) != float(MARGIN):
        raise SystemExit(f"{cell}: the record was decided under {rec.criterion} with margin {rec.margin}, this caller prices "
                         f"under {want} with margin {MARGIN}: a record binds its rule; rewrite it or price under its rule")
    ident = layout_store(rec, out_dir, cell)
    if ident is not None and not rec.has_cell(RECORD_WORKLOAD, ident):
        # a cell the record does not hold (an added or grouped set, a slice): picked on this capture alone; said under
        # --in-sample, marked otherwise (the record covers every cell a published number prices)
        if rule.in_sample is True:
            if (out_dir, cell) not in _NO_ROW_SAID:
                _NO_ROW_SAID.add((out_dir, cell))
                print(f"[choose_cell] {cell}: no row in {rule.record_name()} (decided at sf{rec.decided_at}); "
                      f"priced by this capture alone (--in-sample), not by the sf{rec.decided_at} decision", file=sys.stderr)
        else:
            stale.note("pick_in_sample", f"{ident}: no row in {rule.record_name()} (decided at sf{rec.decided_at}): "
                       f"picked on its own capture at sf{sf} (own capture); grid_pick_record.py at the deciding scale writes the row")
            return None, None, {}, ["pick_in_sample"]
        return None, None, {}, []
    store = bound_store(rec, sf, ident)
    if store is None:     # not this scale's checked store (bound_store, THE rule): the held-out rule on its rows, no row binds
        if (out_dir, cell) not in _NOT_BOUND_SAID:
            _NOT_BOUND_SAID.add((out_dir, cell))
            what = f"the layout of {ident}" if ident else "a layout no record store has"
            print(f"[choose_cell] {cell}: {what}, not checked at sf{sf} (the record's bound scales, pick_stores.json): the held-out rule "
                  f"on its rows, no record row and no anchor (picks.bound_store)", file=sys.stderr)
        return None, None, {}, []
    ent = rec.cell(RECORD_WORKLOAD, store)
    if store in rec.spec_sha:          # a mix: the capture's run record carries the spec it was measured with
        with open(Capture(out_dir).record(cell)) as f:
            r = json.load(f)
        have = spec_sha(spec_path) if spec_path is not None else spec_sha_of(r["spec"]) if r.get("spec") else None
        if have is None:
            raise SystemExit(f"{cell}: the record ties this mix to a spec sha but the capture's run record holds no spec: the sha "
                             f"cannot be checked -- re-measure the mix")
        if rec.spec_sha[store] != have:
            raise SystemExit(f"{cell}: the record's spec sha {rec.spec_sha[store]} differs from this capture's {have}: a "
                             f"re-solved mix under the same name -- rewrite the record at the deciding scale")
    moved = check_record_bodies(store, ent)
    pinned, own = bound_picks(rec, store, sf)
    return pinned, own, moved, []


_NO_RECORD_SAID = set()
_NOT_BOUND_SAID = set()
_NO_ROW_SAID = set()
_SAID = set()

# by policy: no held-out set exists, all params are reported; said where it is priced. The microbench: 3-5 params.
# BI: its param files hold 5 rows per family (workloads/bi/genparams.py: five named strata per generator), so rows
# beyond 5 do not exist (no held-out rows); a BI pick less than 10% cheaper than the planner's body is marked, never read
# as a finding. LDBC's default for PlanRule.in_sample_default; pricing never tests a family's name against it.
# LinkBench, in-sample in the workloads table too, is priced in-sample through its kvcost adapter (in_sample=True: the
# pick record's pins apply there), not by this default. The two together are the table's in-sample set.
IN_SAMPLE_WORKLOADS = ("microbench", "bi", "job")
IN_SAMPLE_BY_ADAPTER = ("linkbench",)
if set(IN_SAMPLE_WORKLOADS) | set(IN_SAMPLE_BY_ADAPTER) != set(_W.names(pick_params="in-sample")):
    raise ImportError(f"picks: IN_SAMPLE_WORKLOADS {IN_SAMPLE_WORKLOADS} + IN_SAMPLE_BY_ADAPTER {IN_SAMPLE_BY_ADAPTER} are "
                      f"not the workloads table's in-sample set {_W.names(pick_params='in-sample')}: place the new workload")


@dataclass(frozen=True)
class PlanRule:
    """THE plan rule a pricing applies, in one value (no process-wide switch): Composer carries it, choose_cell takes it.
    PlanRule() is the default rule (the record, its anchor and its pin).
    record: the pick record's path (default: grid_capture.PICKS_FILE when the rule is made); None = no record, each
      capture by its own rows (the scale checks).
    anchor: the record's incumbent anchors a cell's pick (record_anchor). pin: the record's row binds at a scale other
      than the deciding one (recorded_picks).
    in_sample: None = from the rows: a capture with held-out rows takes the held-out rule; one without is priced
      in-sample when its family is in `in_sample_default`, else refused. True = any capture without held-out rows is
      priced in-sample (a slice, a smoke: a driver's --in-sample), said on stderr.
    historical: a reader of a COMMITTED snapshot, read as captured: no anchor, in-sample, a missing measure_only column
      read as 0, HEAD's later candidates not required (drift warned). Never the default, never for a working capture.
      Only the studies' readers set it (combined_other_benchmarks, grid_latency --historical).
    held_out: False = the held-out rows are not read: every capture is picked in-sample, on its reported rows
      (--pick-on in-sample on a dataset with a held-out set; pick_on_rule). Named in the repr only when False, so the
      keys a rule made before it had this field stay the same.
    tie_digits: None = plan scores compared exactly (measured counts are whole); N = at N significant digits, the first
      name in sorted order winning a tie (a predicted capture's float costs, as predicted.comparable compares them).
    Every field is a str, bool, tuple or None, so its repr is the same in every process (the mix cost cache keys it)."""
    _: KW_ONLY
    record: object = field(default_factory=lambda: grid_capture.PICKS_FILE)
    anchor: bool = True
    pin: bool = True
    in_sample: object = None
    in_sample_default: tuple = IN_SAMPLE_WORKLOADS
    historical: bool = False
    tie_digits: object = None
    held_out: bool = True

    def __post_init__(self):
        if self.in_sample not in (None, True):
            raise ValueError(f"PlanRule.in_sample: None (from the rows) or True, not {self.in_sample!r}")
        object.__setattr__(self, "in_sample_default", tuple(self.in_sample_default))
        if self.historical or not self.held_out:   # read as captured, or the held-out rows not read: in-sample
            object.__setattr__(self, "in_sample", True)

    def __repr__(self):
        """the dataclass repr, held_out named only when False (a cost cache key holds the repr)"""
        names = [f.name for f in fields(self) if f.name != "held_out" or not self.held_out]
        return f"PlanRule({', '.join(f'{n}={getattr(self, n)!r}' for n in names)})"

    def picks_in_sample(self, family, has_held_out):
        """THE test: is a capture of `family` priced in-sample? Never with held-out rows; without them, by the rule"""
        return not has_held_out and (self.in_sample is True or family in self.in_sample_default)

    def has_record(self):
        return self.record is not None and os.path.exists(self.record)

    def load_record(self):
        """the record as a reader sees it (pick_record.load: a Picks)"""
        from kvevaluator.plan_choice import pick_record
        return pick_record.load(self.record)

    def record_name(self):
        return os.path.relpath(self.record) if self.record else "no pick record"

    def but(self, **changes):
        """a copy with some fields changed"""
        return replace(self, **changes)


def pick_on_rule(pick_on, held_out_set=True, **more):
    """the PlanRule of --pick-on (benchmarks.pick_on_of): held-out, the default rule (in-sample from the rows); in-sample,
    every capture picked on its reported rows, and where the dataset has a held-out set (held_out_set) its rows not read"""
    if pick_on == "in-sample":
        return PlanRule(in_sample=True, held_out=not held_out_set, **more)
    return PlanRule(**more)


class NoAnchor(dict):
    """every query: "" (no anchor: the cheapest), for a decision made afresh (grid_pick_record.decide, its own incumbent rule)"""

    def __contains__(self, q):
        return True

    def __getitem__(self, q):
        return ""


def record_anchor(workload, cell, rule, out_dir=None):
    """{query: the anchor of a decision on this store}: the record's incumbent, "" for a carried entry (v1's selection,
    decided on no capture of ours: never an anchor; the cheapest then); a query the record does not name is absent.
    None of it without the record (rule.record None) or for a historical capture (read as captured: its own selector row).
    LDBC's cells (the record's top level) go through the one rule with `out_dir`: the incumbent of the capture's
    layout_store; at a scale other than the deciding one only when that store is bound there (bound_store), else no
    anchor at all (NoAnchor: the cheapest, as a composed option has)."""
    from kvevaluator.plan_choice import pick_record
    if rule.historical or not rule.has_record():
        return {}
    rec = pick_record.load(rule.record)
    store = pick_record.store_key(cell)
    if workload == RECORD_WORKLOAD and out_dir is not None:
        store = layout_store(rec, out_dir, cell)
        sf = capture_sf(out_dir, cell)
        if str(sf) != str(rec.decided_at):
            store = bound_store(rec, sf, store)
            if store is None:
                return NoAnchor()
    entries = rec.cell(workload, store) if store is not None else {}
    return {q: pick_record.anchor_of(e) or "" for q, e in entries.items()}


def choose_cell(out_dir, cell, criterion=None, *, rule, workload=None, tree=None, spec_path=None, pinned=True, drift="fail",
                anchor=None):
    """THE pick of one cell of a capture under `rule` (PlanRule): candidates.choose over the cell's reported rows, the
    held-out rows beside them (the plan rule; a capture without a held-out set is priced in-sample only where
    rule.picks_in_sample says so, else refused), and, with rule.pin, the recorded decision with its per-scale exception
    when this scale is not the deciding one. Every pricer, check and figure goes through here. `anchor` ({query: body or
    ""}, NoAnchor()): in place of the record's incumbents (record_anchor; none under rule.anchor False)."""
    hr = load_held_out(out_dir, cell) if rule.held_out else None
    wl = capture_workload(out_dir, cell)
    workload = workload or wl
    tree = tree or capture_tree_of(out_dir, cell)
    pipeline = capture_pipeline_of(out_dir, cell)
    if anchor is None:     # the record's incumbent per query (a carried entry: none); no anchor: the cheapest
        anchor = record_anchor(workload, cell, rule, out_dir) if rule.anchor else NoAnchor()
    in_sample = []        # a capture with no held-out rows that the rule would not price in-sample: priced so, marked
    if not rule.picks_in_sample(wl, has_held_out=hr is not None) and hr is None:
        cap = Capture(out_dir)
        why = (f"the {wl} capture {out_dir} has no held-out set (its family has none)" if not cap.has_held_out() else
               f"no held-out capture under {cap.held_out().path} (grid_bodies --params 11,12,13,14,15 --out "
               f"{cap.held_out().path} measures it)")
        stale.note("pick_in_sample", f"{cell}: {why}: picked in-sample, on the reported params")
        in_sample = ["pick_in_sample"]
        rule = rule.but(in_sample=True)
    if hr is None and wl in rule.in_sample_default:        # in-sample by the rule's default (no held-out set exists)
        if out_dir not in _SAID:
            _SAID.add(out_dir)
            print(f"[choose_cell] {os.path.relpath(out_dir)}: the {wl} workload is priced IN-SAMPLE by "
                  f"policy (every param is reported; no held-out set exists)", file=sys.stderr)
        return _no_drift(choose(load_runs(out_dir, cell), criterion, workload, tree, anchor=anchor, pipeline=pipeline,
                                historical=rule.historical, left_out=predicted_left_out(out_dir, cell)), cell, drift,
                         in_sample)
    if hr is None and out_dir not in _SAID and not in_sample:   # in-sample by rule.in_sample True (the driver's flag)
        _SAID.add(out_dir)
        print(f"[choose_cell] {os.path.relpath(out_dir)}: " + (
              "HISTORICAL capture (--historical): no measure_only column read as 0, in-sample, as captured; the record, the "
              "per-scale exception and HEAD's later candidates are NOT applied" if rule.historical else
              f"priced IN-SAMPLE (--in-sample): no held-out set is read. A pick recorded at sf{DECIDING_SF} still applies "
              "where the record has a row (the plan rule); a cell or query with no row is priced by this capture alone"),
              file=sys.stderr)
    if rule.historical:
        drift = "warn"
    rec, scales, moved, cell_kinds = (recorded_picks(out_dir, cell, rule, spec_path, criterion) if pinned and rule.pin
                                      else (None, None, {}, []))
    chosen = choose(load_runs(out_dir, cell), criterion, workload, tree, held_out=hr, pinned=rec, scale_picks=scales,
                    anchor=anchor, pipeline=pipeline, historical=rule.historical, left_out=predicted_left_out(out_dir, cell))
    for q, kind in moved.items():
        if q in chosen:
            chosen[q].setdefault("stale", []).append(kind)
    return _no_drift(chosen, cell, drift, in_sample + cell_kinds)


def predicted_left_out(out_dir, cell):
    """the bodies a predicted capture's requests left out on purpose ({query: {body: why}}: measure-only, or not legal on
    the layout; its record's bodies_left_out, kvcost predict), which a measured capture runs; None for a measured
    capture. A predicted capture of the picked bodies only is refused: it does not hold a layout's candidates"""
    p = Capture(out_dir).record(cell)
    rec = json.load(open(p)) if os.path.exists(p) else {}
    if rec.get("measured_as") != "predicted":
        return None
    every_body(rec, cell)
    return rec.get("bodies_left_out", {})


def every_body(rec, cell):
    """refuse a predicted cell's record `rec` unless it holds every body (kvcost predict --bodies all): a capture of the
    picked bodies only is no layout's candidates, so no reader may pick or price a layout from it"""
    if rec.get("bodies") != "all":
        raise SystemExit(f"{cell}: a predicted capture of {rec.get('bodies', 'unknown')} bodies (not every body): it cannot "
                         f"be priced as a layout's candidates (kvcost predict --bodies all)")


def _no_drift(chosen, cell, mode="fail", marks=()):
    """a capture that predates a candidate of HEAD is never priced silently: its picks are among the bodies it ran, and
    each such query is marked (candidates_unmeasured); mode "warn" (a HISTORICAL copy priced on purpose: the strict
    compare's old side) says it in a historical copy's words. `marks`: kinds every query of the cell carries (an
    in-sample pick). Each chosen query's `stale`: its kinds, sorted"""
    drift = {q: d["unmeasured"] for q, d in chosen.items() if d.get("unmeasured")}
    if drift and mode == "fail":
        stale.note("candidates_unmeasured", f"{cell}: the capture never ran candidates HEAD has ({drift}): picked among "
                   f"the bodies it ran; re-measuring the cell clears it")
    elif drift:
        print(f"[choose_cell] {cell}: a historical copy: {sum(len(v) for v in drift.values())} candidates HEAD has were never run "
              f"here ({sorted(drift)}); its picks are among the bodies it ran", file=sys.stderr)
    for q, d in chosen.items():
        kinds = set(d.get("stale", [])) | set(marks) | ({"candidates_unmeasured"} if q in drift else set())
        if kinds:
            d["stale"] = sorted(kinds)
    return chosen
