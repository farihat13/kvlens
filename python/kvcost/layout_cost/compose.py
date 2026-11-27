#!/usr/bin/env python3
"""Mixed layouts, priced without building them. "Per type": vertex props get a layout per vertex type (per vid group:
Post and Comment share one, as meta requires), adjacency a layout per edge type; "per type and direction"
(--by-direction): adjacency per edge type and direction. Each choice is one of the grid's three settings (split /
condensed / chunked), or Chunked at another chunk size (Composer.sizes).

How: the grid captures every step split by store part (parts/: props:<label>, adj:<group>:<etype>:<dir>,
eprops:<group>:<etype>, index:...). A part's cost depends only on its own component's layout (check_grid's
decomposition), so a mixed layout's cost = each part's cost taken from a measured cell whose layout for that part's
component is the one the mix assigns it. A query keeps one plan across its parts: a plan is usable in a mix iff it
was valid in every measured cell pairing a props layout and an adjacency layout that the mix gives the parts this
query touches; the query then takes the usable plan the shared rule picks (query_pick). Index parts are the same in
every cell.

The measured cells: load_reads (a cell's read queries at the plan the shared rule picks), load_writes, load_measured,
and a workload's totals on one cell (item_cost: each item = how often x its mean over its params).

    python3 -m kvcost.layout_cost.compose --sf 0.1 [--by-direction] [--pick-by wops_bytes4k|wops|<spec>]
        checks that each uniform assignment reproduces the measured cell's totals exactly, then builds each
        workload's mix cost cache
"""
import argparse
import json
import math
import multiprocessing
import os
import sys
from collections import defaultdict
from dataclasses import KW_ONLY, dataclass, replace
from kvevaluator.paths import ROOT
from kvevaluator.layouts import cell_id
from kvevaluator import contract  # (the last chunk's assumed fill: inputs/contract/capabilities.json)

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvevaluator.measure import grid_capture
from kvcost.layout_cost import picks
from kvcost.layout_cost.picks import PlanRule  # (THE plan rule; Composer carries it)
from kvcost.layout_cost import settings  # (the settings and the cells: one list)
from kvevaluator.capture import step_record  # (the KV-pair histograms' text format, the write units' columns)
from kvevaluator.plan_choice import candidates as C  # (pick_body: THE plan rule)
from kvevaluator.schema.vids import group_of  # (Post + Comment share the Message vid group)
from kvcost.layout_cost.mixes import spec_of  # (an assignment's spec)
from kvcost.layout_cost.cost_models import currencies  # noqa: F401  (the two totals: one definition)
from kvcost.layout_cost import benchmarks

LAYOUTS = settings.SETTINGS
CELLS = settings.CELLS
# the four uniform layouts every figure shows by name. A cell is named properties/adjacency only: an engine's name is
# used only for a NAMED format
KNOWN = {"props_split_adj_condensed": "Split/Condensed", "props_condensed_adj_split": "Condensed/Split",
         "props_condensed_adj_condensed": "All-Condensed", "props_chunked_adj_chunked": "All-Chunked"}
# the palette key of the colour a cell takes from its ADJACENCY setting (the convention of mix_benefit/figure_common.ADJ)
ADJ_COLOR_KEY = {"condensed": "all_condensed", "split": "nebula", "chunked": "pertype_csr"}


from kvevaluator.capture.step_record import HIT_BYTE_LANES, MISS_BYTE_LANES as MISS_LANES
from kvevaluator.capture import capture_layout
from kvevaluator.output_tree import HELD_OUT, Capture, Tree, layout_capture, writes_layout_capture
from kvevaluator import weights as WEIGHTS  # (the op-weight vector: one home)
from kvevaluator.plan_choice.optimizer import criteria as CRITERIA
# the bytes a read query (and a write's own read-modify-write) is REPORTED at: every lane the store returned, the
# miss keys included (the paper's "bytes read"); the hit-only total is read_bytes - miss_key_bytes, derived where a
# view wants it. ONE definition for the reads and the writes' own reads; the plan PICK sums the same 7 (criteria.PICK_BYTE_LANES).
REPORT_BYTE_LANES = HIT_BYTE_LANES + MISS_LANES


def read_cost(r, w=None):
    """read_ops under the vector `w` (None: weights.DEFAULT); read_bytes = REPORT_BYTE_LANES summed (the capture's read_bytes column is that same 7-lane sum); miss_key_bytes
    beside it: the keys a seek/next/get lands on when it misses -- their size is whatever key sits next door in the
    store, so it can differ between stores with the same layout of the part"""
    return {"read_ops": picks.weighted(r, w), "read_bytes": sum(float(r[b] or 0) for b in REPORT_BYTE_LANES),
            "miss_key_bytes": sum(float(r[m] or 0) for m in MISS_LANES)}


RULES = {}      # (root, cell) -> the pick rule(s) the cell's picks came from (load_reads), written beside the criterion
STALE = {}      # (root, cell) -> the stale kinds of the cell's picks (kvevaluator.stale; picks.choose_cell marks them)


def load_reads(root, cell, criterion=None, *, rule):
    """query -> {param_idx: {read_ops, read_bytes, miss_key_bytes}} at the plan the shared rule picks for the cell
    (picks.choose_cell under `criterion` and `rule`, a PlanRule; a query with no valid plan is left out)"""
    chosen = picks.choose_cell(root, cell, criterion, rule=rule)
    RULES[(root, cell)] = "; ".join(sorted({c["rule"] for c in chosen.values() if c.get("rule")}))   # for workload_costs.csv
    STALE[(root, cell)] = sorted({k for c in chosen.values() for k in c.get("stale", [])})
    runs = grid_capture.load_runs(root, cell)        # the reported rows: the cost of the pick comes from these
    by = defaultdict(lambda: defaultdict(dict))     # query -> variant -> param -> row
    for r in runs:
        by[r["query"]][r["body"]][r["param_idx"]] = r
    return {q: {i: read_cost(r) for i, r in by[q][c["body"]].items()} for q, c in chosen.items() if c["body"]}


# The chunk view: what a write is charged for a touch of a family's LAST chunk.
# An insert of a new vertex lands in its family's last chunk, and pays for that chunk's whole read and write. How full
# the last chunk is depends on where the earlier chunk edges happened to fall, not on the layout: a 0.27% shift in chunk
# edges (a54442cc) took W11's Chunked Message props from 109 to 32 MB at SF3. A run makes too few inserts to average
# this out (5 per query, ~0.6% of a 64 KB chunk). Over time a last chunk is anywhere from empty to full, so the half
# view charges each last-chunk touch at chunk target x LAST_CHUNK_FILL (one half: inputs/contract/capabilities.json, the
# fill a sampled tally takes for a sampled family's last chunk too). The measured bytes stay in the capture (the
# "measured" view).
# The numbers: README.md, section "The chunk view".
CHUNK_VIEWS = ("half", "measured")    # the first is the default
LAST_CHUNK_FILL = contract.last_chunk_fill()   # the half view's share of the chunk target


def default_chunk_view(model=None):
    """the chunk view a cost under `model` takes when none is named: measured for the bill models (bill_exact bills
    each measured request rounded up alone, the capture's exact unit columns, Composer(bill="sized"); bill_lower and
    bill_upper come from the same rows, so the three take one view), half for every other model"""
    from kvcost.layout_cost.cost_models import needs_bill
    return "measured" if needs_bill(model) else CHUNK_VIEWS[0]


def resolve_chunk_view(view, model=None):
    """the chunk view a CLI's --chunk-view gives (None when not named) under `model`: the named view, else the model's
    default (default_chunk_view)"""
    return view or default_chunk_view(model)
LAST_CHUNK = tuple(step_record.LAST_CHUNK)   # the write capture's last-chunk columns (Counts::LastChunk)


def chunk_view(r, view, cell):
    """a write row (a counts, step or part row) under the chunk view `view`: "measured" as captured; "half" charges each
    last-chunk touch the family's chunk target x LAST_CHUNK_FILL (a half) in place of the chunk's bytes, on the read (scan_value) and on the
    write's base (write_value keeps the new row's own bytes). Keys and S / G / N stay measured. Under "half", a row
    without the last-chunk columns is refused on every cell: a cell's name does not say whether it stores a component
    Chunked (a mix, Per-type CSR)"""
    if view not in CHUNK_VIEWS:
        raise ValueError(f"chunk view {view!r}: one of {CHUNK_VIEWS}")
    if view == "measured":
        return r
    if any(r.get(c) in (None, "") for c in LAST_CHUNK):
        raise SystemExit(f"{cell} {r.get('query')} p{r.get('param_idx')}: the writes capture has no last-chunk columns "
                         f"(an older capture shape): the half chunk view cannot be applied; re-measure it, or cost it "
                         f"under --chunk-view measured")
    f = lambda c: float(r[c])
    return {**r, "scan_value": float(r["scan_value"] or 0) - f("last_chunk_read_bytes")
            + f("last_chunk_read_target_bytes") * LAST_CHUNK_FILL,
            "write_value": float(r["write_value"] or 0) - f("last_chunk_write_base_bytes")
            + f("last_chunk_write_target_bytes") * LAST_CHUNK_FILL}


def write_vector(r, w=None):
    """a write's measures from its lanes, named as a capture names them (a counts or step row: P, D, write_key,
    write_value, the read lanes): write_ops = PUTs + DELs, write_bytes = the bytes they write, and its own reads (the
    read-modify-write) as write cost, never read_ops / read_bytes: write_rmw_ops (weighted, as read_cost), write_rmw_bytes
    (REPORT_BYTE_LANES, the miss keys included) and write_rmw_miss_key_bytes beside. The one definition of a write's
    cost: the measured writes capture (load_writes), the predicted one (predict_accuracy.predicted)
    and the write accuracy report read it. `w`: the op-weight vector of write_rmw_ops (None: weights.DEFAULT)"""
    rc = read_cost(r, w)
    return {"write_bytes": float(r["write_key"] or 0) + float(r["write_value"] or 0),
            "write_ops": float(r["P"] or 0) + float(r["D"] or 0),
            "write_rmw_ops": rc["read_ops"], "write_rmw_bytes": rc["read_bytes"],
            "write_rmw_miss_key_bytes": rc["miss_key_bytes"]}


def load_writes(root, cell, view, dataset=None, param_set=None):
    """query -> {param_idx: write_vector} of a measured writes capture (capture_layout.write_rows: either shape), under
    the chunk view `view` (chunk_view; no default: a caller costing beside a Composer passes its chunk_view); `dataset`
    and `param_set` as output_tree.writes_in (LinkBench's writes: its param set's rows)"""
    out = defaultdict(dict)
    cap = writes_layout_capture(root, dataset or benchmarks.DATASETS[0], cell, param_set)
    for r in capture_layout.write_rows(cap.path_of("counts", cell), cell=cell)[2]:
        out[r["query"]][r["param_idx"]] = write_vector(chunk_view(r, view, cell))
    return out


add_pick_by_arg = benchmarks.add_pick_by_arg     # the one --pick-by flag (benchmarks: the rule is an output identity axis)
criterion_name = benchmarks.criterion_name


class Unmeasured(Exception):
    """a workload item has no valid measurement in some cell: the workload is not priced (never partially)"""


def item_cost(data, family, query, params):
    """the item's mean over its params, per measure"""
    per = data[family].get(query)
    if per is None:
        raise Unmeasured(f"{query} ({family}) has no valid plan in this cell's capture")
    idx = [str(i) for i in params] if params else sorted(per, key=int)
    missing = [i for i in idx if i not in per]
    if missing:
        raise Unmeasured(f"{query} ({family}): params {missing} not measured")
    keys = per[idx[0]].keys()
    return {k: sum(per[i][k] for i in idx) / len(idx) for k in keys}


def cost_or_refuse(workload, data, what):
    """price(), a capture lacking an item refused in one line (what: the capture or cell, named in the message)"""
    try:
        return workload_cost(workload, data)
    except Unmeasured as e:
        raise SystemExit(f"{what}: workload {workload.get('id', '?')}: {e} -- REFUSED: the capture does not hold every "
                         f"param the workload prices (re-measure it on the workload's params)") from None


MEASURES = ("read_ops", "read_bytes", "miss_key_bytes", "write_bytes", "write_ops", "write_rmw_ops", "write_rmw_bytes",
            "write_rmw_miss_key_bytes")


def workload_cost(workload, data):
    tot = dict.fromkeys(MEASURES, 0.0)
    for family, query, often, params in workload["items"]:
        for k, v in item_cost(data, family, query, params).items():
            tot[k] += often * v
    return tot


def pareto(points):
    """cells no other cell beats: none is <= on every axis and < on one (axes = read_ops, read_bytes,
    write_bytes; an axis that is 0 everywhere, e.g. write_bytes of a read-only workload, drops out)"""
    return [c for c, p in points.items()
            if not any(all(b <= a for a, b in zip(p, q)) and any(b < a for a, b in zip(p, q))
                       for c2, q in points.items() if c2 != c)]


def load_measured(dataset, root, criterion=None, families=None, *, rule, param_set=None, view):
    """{cell: {family: measured rows}} of a suite's grid capture: each read family at the plan the shared rule picks
    under `criterion` and `rule` (a PlanRule), each write family from its flat writes CSV under the chunk view `view`; families =
    Composer.families' form (None: the suite's); `param_set`: the read captures' param set (output_tree.params_dir)"""
    fams = dict(families) if families is not None else dict(benchmarks.FAMILIES[dataset])
    return {cell: {fam: (load_writes(root, cell, view) if fams[fam] == "write"
                         else load_reads(layout_capture(root, fam, cell, param_set).path, cell, criterion, rule=rule))
                   for fam in fams} for cell in CELLS}


READ_BYTE_LANES = REPORT_BYTE_LANES     # the reported bytes, compose.py's definition (the pick sums the same lanes)
# the RAW per-part channels, kept beside the measures so any view (op mix, byte lanes) is drawn from the saved CSVs
# without re-deriving; for a write query the read lanes are its read-modify-write
CHANNELS = (*step_record.BASE_OPS, *step_record.WRITE_OPS, *step_record.READ_BYTE_LANES, *step_record.WRITE_BYTE_LANES)


# the bill's units (DynamoDB's rule per request), computed per step row and part in cost_vec, so they
# ADD like every channel. Off unless Composer(bill="bounds" | "sized") (cost_models.needs_bill): no other stage carries
# them. The bounds come from requests and bytes alone (any capture, predicted too); the exact units are each request
# rounded up alone, which the C++ counts per request (a measured capture's *_fetch_4k and *_units_1k columns), so a
# bounds load carries no exact channel at all
BILL_BOUNDS = ("bill_read_lower", "bill_read_upper", "bill_write_lower", "bill_write_upper")
BILL_SIZED = ("bill_read_exact", "bill_write_exact")
BILL = BILL_BOUNDS + BILL_SIZED
BILL_SETTINGS = ("", "bounds", "sized")
READ_UNIT, WRITE_UNIT = 4096.0, 1024.0
# the steady latency proxy's block estimate (python/kvcost/latency/features.py), per step row and part: each GET and
# each SEEK lands on >= 1 block of 4 KB, a big value spans more. est_blocks: reads only (a write query counts 0, as in
# w16_pass_through). est_blocks_rw: the same for a read; a write query's own reads (its read lanes) by the same rule,
# plus its PUTs, each >= 1 block: P x ceil(written bytes / P / 4 KB) (the rows hold no per-PUT size: the row's mean per
# PUT). The ceil is per row, so both are computed in cost_vec and ADD like a channel. Off unless Composer(est_blocks=True)
EST_BLOCKS, BLOCK = ("est_blocks", "est_blocks_rw"), 4096.0


WEIGHTINGS = ("sum", "geomean", "additive")


@dataclass(frozen=True)
class Composer:
    """How a load prices, in one object: load() requires it, and the data and every query of it carry it (data.composer,
    d["composer"]), so a query is always priced under the settings it was loaded with. Every field is a str, bool or
    tuple, so its repr is the same in every process (the mix cost cache keys it).
    criterion: the plan pick rule (--pick-by; None = criteria.DEFAULT_CRITERION).
    sizes: ((option id, capture root, cell), ...): Chunked at another chunk size as a setting of its own, per unit, priced
      from that size's Chunked/Chunked capture (settings.size_options). The grid's own size (64 KB), "chunked", is costed
      the same way from the grid's Chunked/Chunked (chunk_sizes).
    sized_drift_ok: the queries whose same-name body may have another body_sha in a size capture than in the grid
      (checked equal by the caller); any other such query is refused.
    bill: the bill's units carried as channels: "" none, "bounds" (lower and upper), "sized" (also exact).
    est_blocks: carry the steady proxy's block estimate as channels.
    bind_per_type_record: False only in a check's control (a per type record row not bound to its per-direction form).
    families: ((family, kind), ...) the load reads, in order; None = the suite's own (benchmarks.FAMILIES).
    plan_rule: the plan rule (picks.PlanRule). Every pick of the load and of its queries reads it from here, never from a
      process-wide switch.
    param_set: the param set whose reported rows the read captures hold (None: the default set); the plan rule still
      reads the default set's held-out rows.
    weighting: how a workload's queries combine into its total: "sum" (the weighted sum), "geomean", "additive".
    chunk_view: what a write's touch of a family's last chunk is charged (chunk_view): "half" (the default) or "measured".
    weights: the op-weight vector (G, S, N) of read_ops and write_rmw_ops (default weights.DEFAULT). Another vector must
      reach the pick too: the criterion carries it and the pick record is off (record None). pick_rule_under() makes
      both; a Composer that has one without the other is refused.
    Every field is a keyword; criterion and plan_rule have no default, so each caller states the rule it prices under."""
    _: KW_ONLY
    criterion: object
    plan_rule: PlanRule
    weighting: str = "sum"
    weights: tuple = tuple(WEIGHTS.DEFAULT[k] for k in "GSN")
    sizes: tuple = ()
    sized_drift_ok: tuple = ()
    bill: str = ""
    est_blocks: bool = False
    bind_per_type_record: bool = True
    families: object = None
    param_set: object = None
    chunk_view: str = CHUNK_VIEWS[0]

    def __post_init__(self):
        if not isinstance(self.plan_rule, PlanRule):
            raise TypeError(f"Composer.plan_rule must be a picks.PlanRule, not {type(self.plan_rule).__name__}")
        if self.bill not in BILL_SETTINGS:
            raise ValueError(f"Composer.bill {self.bill!r}: one of {BILL_SETTINGS} (cost_models.needs_bill)")
        if self.chunk_view not in CHUNK_VIEWS:
            raise ValueError(f"Composer.chunk_view {self.chunk_view!r}: one of {CHUNK_VIEWS}")
        if self.bill == "sized" and self.chunk_view != "measured":   # each request's size comes from the histograms
            raise ValueError("Composer.bill 'sized' (bill_exact) bills each measured request rounded up alone (the "
                             "capture's exact unit columns): it is costed under the measured chunk view only (--chunk-view measured)")
        if self.weighting not in WEIGHTINGS:
            raise ValueError(f"Composer.weighting {self.weighting!r}: one of {WEIGHTINGS}")
        w = tuple(float(x) for x in self.weights)
        if len(w) != 3 or min(w) <= 0:
            raise ValueError(f"Composer.weights {self.weights!r}: (G, S, N), three positive numbers")
        if WEIGHTS.is_default(self.w):
            w = tuple(WEIGHTS.DEFAULT[k] for k in "GSN")      # one key for the default, however it was spelled
        object.__setattr__(self, "weights", w)
        if not WEIGHTS.is_default(self.w):
            if self.plan_rule.record is not None:
                raise ValueError(f"Composer.weights {WEIGHTS.text(self.w)}: the pick record was decided under the default "
                                 f"vector, so the plan rule must have no record (compose.pick_rule_under)")
            if CRITERIA.with_weights(self.criterion, self.w) != self.criterion:
                raise ValueError(f"Composer.weights {WEIGHTS.text(self.w)}: the criterion {self.criterion!r} picks under "
                                 f"another vector (compose.pick_rule_under)")
        object.__setattr__(self, "sizes", tuple(tuple(x) for x in self.sizes))
        object.__setattr__(self, "sized_drift_ok", tuple(sorted(set(self.sized_drift_ok))))
        if self.families is not None:
            fams = self.families.items() if isinstance(self.families, dict) else self.families
            object.__setattr__(self, "families", tuple((str(f), str(k)) for f, k in fams))

    @property
    def w(self):
        """the weights as weights.py's dict ({"G", "S", "N"})"""
        return dict(zip("GSN", self.weights))

    def families_of(self, dataset):
        """{family: "read" | "write"} the load of `suite` reads (a copy)"""
        return dict(self.families) if self.families is not None else dict(benchmarks.FAMILIES[dataset])

    @property
    def keys(self):
        """the channels and measures of every cost vector"""
        bill = {"": (), "bounds": BILL_BOUNDS, "sized": BILL}[self.bill]
        return MEASURES + CHANNELS + bill + (EST_BLOCKS if self.est_blocks else ())

    def but(self, **changes):
        """a copy with some settings changed"""
        return replace(self, **changes)

    @classmethod
    def from_args(cls, a, **more):
        """the settings a CLI's flags give (--pick-by, --in-sample: the plan rule's, --params), plus `more`"""
        more.setdefault("plan_rule", PlanRule(in_sample=True if getattr(a, "in_sample", False) else None))
        more.setdefault("param_set", getattr(a, "param_set", None))
        more.setdefault("chunk_view", resolve_chunk_view(getattr(a, "chunk_view", None), getattr(a, "model", None)))
        return cls(criterion=getattr(a, "criterion", None), **more)


_WEIGHTS_SAID = set()


def pick_rule_under(criterion, plan_rule, w):
    """(criterion, plan rule) that pick under the vector `w` (kvcost --op-weights): at the default, both as given; else the
    criterion carries `w` (criteria.with_weights) and the plan rule drops the pick record. Every row of the record is a
    cost pick under the default vector (realism decisions are the candidates' measure-only bodies and the planner's
    answer, which `choose` applies under any vector), so under another vector every cell picks afresh."""
    if WEIGHTS.is_default(w):
        return criterion, plan_rule
    if WEIGHTS.text(w) not in _WEIGHTS_SAID:          # said once per process (cost-layouts sets up once per output)
        _WEIGHTS_SAID.add(WEIGHTS.text(w))
        print(f"[kvcost] --op-weights {WEIGHTS.text(w)}: plans picked afresh under this vector; the pick record (decided under "
              f"{WEIGHTS.text(WEIGHTS.DEFAULT)}) and its anchors and pins are off", file=sys.stderr)
    return CRITERIA.with_weights(criterion, w), plan_rule.but(record=None, anchor=False, pin=False)


# BI's capture (10-12) ran bi10's body before 10-18 (since then the anchor's by-id read names its label, Person); its
# counts are equal at today's head (the 10-19 BI count check: 22,740 step rows, 0 differ outside used_value). The pick
# is unaffected (bi10 has one body). Only this drift is accepted, only on BI's capture (a caller passes it as
# Composer.sized_drift_ok for BI's sizes); any other stops the run
BI_DRIFT_OK = frozenset({"bi10"})


def put_blocks(v):
    """one write row's PUTs in 4 KB blocks, each >= 1: P x ceil(written bytes / P / 4 KB)"""
    p = v["P"]
    return p * math.ceil((v["write_key"] + v["write_value"]) / p / BLOCK) if p else 0.0


def est_blocks(v):
    """one read row's block estimate from its channels: G x ceil(get bytes / G / 4 KB) + S x ceil(scan bytes / S / 4 KB)"""
    g, s = v["G"], v["S"]
    return ((g * math.ceil((v["get_key"] + v["get_value"]) / g / BLOCK) if g else 0.0)
            + (s * math.ceil((v["scan_key"] + v["scan_value"]) / s / BLOCK) if s else 0.0))


def zero(composer):
    return dict.fromkeys(composer.keys, 0.0)


def axes(cell):
    p, a = cell[len("props_"):].split("_adj_")
    return p, a


# Chunked at every chunk size is costed one way: a unit set to it takes its rows from that size's
# Chunked/Chunked capture, keyed by the option id. The grid's own size (64 KB) keeps the id "chunked"; the other sizes
# are Composer.sizes (chunked_c4k, ...). The other cells give the Split and Condensed parts, the index and plan validity.
GRID_CHUNKED = "chunked"
CH_CH = "props_chunked_adj_chunked"


def chunk_sizes(root, composer):
    """[(option id, capture root, cell)] of every chunk size a load reads: the grid's own first, then Composer.sizes"""
    return [(GRID_CHUNKED, root, CH_CH)] + list(composer.sizes)


def is_sized(layout, composer):
    """Chunked at some chunk size: the grid's own ("chunked") or one of Composer.sizes' option ids"""
    return layout == GRID_CHUNKED or any(layout == x[0] for x in composer.sizes)


def validity_layout(layout, composer):
    """the layout whose plan validity a unit's layout has: Chunked at any size has the grid's Chunked's"""
    if is_sized(layout, composer):    # the chunk size changes no capability below coarse_chunks (512 KiB; load checks the runs)
        return GRID_CHUNKED
    if layout not in LAYOUTS:        # e.g. a size option priced on a load without sizes: no rows hold it, never a silent 0
        raise SystemExit(f"{layout}: neither one of the grid's three settings nor a chunk size this load holds "
                         f"(Composer.sizes): it cannot be priced")
    return layout


def options_of(data, u):
    """a unit's options: the grid's three settings (its Chunked at 64 KB), then Chunked at each other chunk size
    (Composer.sizes; only the unit's own sizes when set_unit_sizes lists it)"""
    sized = tuple(getattr(data, "sized", ()))
    keep = getattr(data, "unit_sizes", {}).get(u)
    if keep is not None:
        sized = tuple(s for s in sized if s in keep)
    return LAYOUTS + sized


def set_unit_sizes(data, unit_sizes):
    """restrict the chunk sizes some units may take: {unit: sized option ids}. A unit not listed keeps every size. Refuses a
    size that is not one of data's sized options and a unit that no query of data has (a misspelled key would filter nothing)"""
    known = {u for d in data.values() if isinstance(d, dict) for per in d.get("cost", {}).values() for u in per if u is not None}
    bad_u = sorted((u for u in unit_sizes if u not in known), key=str)
    bad_s = sorted({s for v in unit_sizes.values() for s in v} - set(getattr(data, "sized", ())))
    if bad_u or bad_s:
        raise SystemExit(f"set_unit_sizes: units no query has {bad_u[:5]}; sizes that are not options {bad_s}")
    data.unit_sizes = {u: frozenset(v) for u, v in unit_sizes.items()}
    print(f"[compose] unit sizes: {len(unit_sizes)} of {len(known)} units restricted; "
          f"{sum(len(getattr(data, 'sized', ())) - len(v) for v in data.unit_sizes.values())} (unit, size) options removed")


def unit_of(part, by_dir):
    """store part -> the layout unit that decides its cost: ('props', label) | ('adj', etype[, dir]) | None (index)"""
    k = part.split(":")
    if k[0] == "props":   # per vid GROUP: meta requires a group's labels to share a props layout (one fan-out)
        return ("props", group_of(k[1]))
    if k[0] == "adj":
        return ("adj", k[2], k[3]) if by_dir else ("adj", k[2])
    if k[0] == "eprops":   # edge props live with the edge type's lists; stored once, at the out end
        return ("adj", k[2], "out") if by_dir else ("adj", k[2])
    if k[0] == "index":
        return None
    raise SystemExit(f"unknown part {part!r}")


def cost_vec(r, write, composer):
    """a part's cost, split as compose.py splits a query's: a write's own reads (read-modify-write) are WRITE
    cost (write_rmw_ops), never read_ops / read_bytes"""
    v = zero(composer)
    for c in CHANNELS:        # the grid shape's lane names (a writes file: through capture_layout.write_rows)
        v[c] = float(r.get(c) or 0)
    if write:
        v.update(write_vector({**r, **{l: v[l] for l in CHANNELS}}, composer.w))
    else:
        v["read_ops"] = picks.weighted(r, composer.w)
        v["read_bytes"] = sum(float(r.get(b) or 0) for b in READ_BYTE_LANES)
        v["miss_key_bytes"] = sum(float(r.get(b) or 0) for b in MISS_LANES)
    if composer.bill:
        v.update(bill_units(r, v, sized=composer.bill == "sized"))
    if composer.est_blocks:
        v["est_blocks"] = 0.0 if write else est_blocks(v)
        v["est_blocks_rw"] = est_blocks(v) + (put_blocks(v) if write else 0.0)
    return v


def bill_units(r, v, sized=True):
    """one step row's billed units (a row = one step, one part, one param). Reads: `lower` = max(requests, bytes / 4 KB)
    and `upper` = requests + bytes / 4 KB per kind (GETs; range reads, a NEXT is free); `exact` = the capture's
    get_fetch_4k + range_fetch_4k (each request rounded up alone) + 1 per request that returned nothing (the
    histograms' bucket 0: the service still charges it). Writes, in 1 KB units: `lower` / `upper` the same two forms
    on the PUTs, a DELETE 1 unit; `exact` = the capture's put_units_1k + del_units_1k (each PUT and DELETE rounded up
    alone, at least 1; a DELETE of unknown size 1: cpp/kvevaluator/src/store/size_hist.h, the one rule). sized=False:
    the bounds only (BILL_BOUNDS), from the counts and bytes, which a predicted row has; the exact units need a measured
    capture's columns, and a row without them is refused by name"""
    g_bytes, s_bytes = v["get_key"] + v["get_value"], v["scan_key"] + v["scan_value"]
    w_bytes = v["write_key"] + v["write_value"]
    p_lower, p_upper = max(v["P"], w_bytes / WRITE_UNIT), v["P"] + w_bytes / WRITE_UNIT
    out = {"bill_read_lower": max(v["G"], g_bytes / READ_UNIT) + max(v["S"], s_bytes / READ_UNIT),
           "bill_read_upper": v["G"] + v["S"] + (g_bytes + s_bytes) / READ_UNIT,
           "bill_write_lower": p_lower + v["D"], "bill_write_upper": p_upper + v["D"]}
    if not sized:
        return out
    if r.get("get_fetch_4k") in (None, "") or r.get("get_kvpair_hist") in (None, ""):
        raise SystemExit(f"{r.get('query')} {r.get('step')}: the capture has no exact Fetch columns or item histograms "
                         f"(a predicted capture, an older binary, or run.emit.kvpair_stats off): bill_exact cannot be "
                         f"computed from it; bill_lower and bill_upper can")
    wrote = v["P"] + v["D"] > 0       # a read row (no PUT, no DELETE) has no write units, and a read file no such column
    if wrote and any(r.get(c) in (None, "") for c in step_record.WRITE_UNITS):
        raise SystemExit(f"{r.get('query')} {r.get('step')}: the writes capture has no {' / '.join(step_record.WRITE_UNITS)} "
                         f"columns (captured before the binary counted them, or predicted): bill_exact cannot be "
                         f"computed from it; bill_lower and bill_upper can")
    empty = sum(int(n) for h in ("get_kvpair_hist", "range_read_hist") for k, n in step_record._pairs(r[h]) if k == "0")
    out.update({"bill_read_exact": float(r["get_fetch_4k"]) + float(r["range_fetch_4k"]) + empty,
                "bill_write_exact": sum(float(r[c]) for c in step_record.WRITE_UNITS) if wrote else 0.0})
    return out


def add(a, b, k=1.0):
    for m in a:              # a vector holds its pricing's keys (zero)
        a[m] += k * b[m]


class GridData(defaultdict):
    """load()'s result: (family, query) -> {"valid", "cost"}"""


def load(root, by_dir, dataset="ldbc", *, composer):
    """(family, query) -> {"valid": {cell: set(variants)}, "cost": {(cell or chunk size, variant, param): {unit: vec}}},
    over the load's families (pricing.families_of(suite)): a read family from its grid_bodies capture, a write family
    from its kvstats per-part rows. Each cell's parts are keyed by the cell; each chunk size's Chunked/Chunked parts, the
    grid's 64 KB included, by its option id (chunk_sizes), where _composed reads a Chunked unit. Priced under
    `pricing` (Composer), which the data and each query carry"""
    if not isinstance(composer, Composer):
        raise TypeError(f"load: pricing must be a Composer, not {type(composer).__name__}")
    vec = lambda: zero(composer)
    missing = defaultdict(set)      # option id -> the families its size has no capture of (query_pick refuses them)
    data = GridData(lambda: {"valid": {}, "cost": defaultdict(lambda: defaultdict(vec)), "composer": composer,
                             "sized_missing": missing})
    fams = composer.families_of(dataset)
    data.read_dirs, data.sf, data.record, data.composer, data.sized_missing = {}, None, None, composer, missing
    data.families = fams
    for cell in CELLS:
        for fam, kind in fams.items():
            if kind == "read":
                rdir = layout_capture(root, fam, cell, composer.param_set).path   # measured itself, else composed
                data.read_dirs.setdefault(fam, {})[cell] = rdir
                if data.sf is None:
                    data.sf = grid_capture.capture_sf(rdir, cell)
                    if composer.plan_rule.has_record():
                        data.record = composer.plan_rule.load_record()
                    elif (data.sf != picks.DECIDING_SF and composer.plan_rule.in_sample is not True
                          and WEIGHTS.is_default(composer.w)):      # another vector turns the record off on purpose
                        print(f"[compose] NO RECORD at sf{data.sf}: composed options take the held-out rule alone, not the "
                              f"sf{picks.DECIDING_SF} decision's (grid_pick_record.py --sf {picks.DECIDING_SF} writes it)",
                              file=sys.stderr)
                chosen = picks.choose_cell(rdir, cell, rule=composer.plan_rule)
                for q, c in chosen.items():
                    # usable = priced + measure-only (a measure-only body is usable on the cell, never PICKED: the pick
                    # excludes it in query_cost by the MIX's own assignment, not per source cell -- per cell, a mix that
                    # combines a Split and a lists adjacency would get an empty intersection)
                    data[(fam, q)]["valid"][cell] = set(c["costs"]) | set(c.get("measure_only", {}))
                    if "measure_only_registry" not in data[(fam, q)]:     # the registry's flagged twins of q (every store)
                        from kvevaluator.plan_choice.candidates import measure_only_bodies, plan_files
                        dq = data[(fam, q)]
                        dq["measure_only_registry"] = measure_only_bodies(fam, q)
                        dq["fam"], dq["q"] = fam, q
                        dq["read_dirs"], dq["sf"], dq["record"] = data.read_dirs[fam], data.sf, data.record
                        ir = json.load(open(plan_files(fam, q)[0]))        # does any body carry the cell-dependent flag?
                        dq["measure_only_where"] = (any(m.get("measure_only_where") for m in (ir.get("bodies") or {}).values())
                                                    or any(v.get("measure_only_where") for v in ir.get("variants", [])))
                with open(Capture(rdir).path_of("parts", cell)) as f:
                    for r in capture_layout.reader(f):
                        add(data[(fam, r["query"])]["cost"][(cell, r["body"], r["param_idx"])]
                            [unit_of(r["part"], by_dir)], cost_vec(r, write=False, composer=composer))
                # the HELD-OUT parts (grid_capture.HELD_OUT): the per-option CHOICE is made on them (the plan rule, python/kvevaluator/plan_choice/candidates.py, one
                # rule for every priced thing, built or not); the cost stays the reported parts'
                hp = layout_capture(root, fam, cell, HELD_OUT).path_of("parts", cell)
                if composer.plan_rule.held_out and os.path.exists(hp):     # held_out False: not read (--pick-on in-sample)
                    with open(hp) as f:
                        for r in capture_layout.reader(f):
                            add(data[(fam, r["query"])].setdefault("cost_h", defaultdict(lambda: defaultdict(vec)))
                                [(cell, r["body"], r["param_idx"])][unit_of(r["part"], by_dir)], cost_vec(r, write=False, composer=composer))
                    hr = grid_capture.load_held_out(rdir, cell)   # the held-out capture beside rdir
                    for q, c in C.choose(hr, historical=composer.plan_rule.historical).items():   # the candidates valid on the held-out set (ran ok, the planner's answer)
                        data[(fam, q)].setdefault("valid_h", {})[cell] = set(c["costs"]) | set(c.get("measure_only", {}))
                        data[(fam, q)].setdefault("planner_h", {})[cell] = c["planner"]
            else:
                wcap = writes_layout_capture(root, dataset, cell, composer.param_set)
                # a predicted write request with a step it could not price has no part rows for that step: refused, never
                # priced from its other steps (a measured write row says ok 1: a failed write stops its cell)
                failed = sorted(f"{r['query']}#{r['param_idx']}"
                                for r in capture_layout.write_rows(wcap.path_of("counts", cell), cell=cell)[2] if str(r["ok"]) == "0")
                if failed:
                    raise SystemExit(f"compose: {cell}'s writes capture has {len(failed)} request(s) not priced: "
                                     f"{', '.join(failed[:5])} ({wcap.path_of('counts', cell)})")
                for r in capture_layout.write_rows(wcap.path_of("parts", cell), cell=cell)[2]:
                    r = chunk_view(r, composer.chunk_view, cell)
                    d = data[(fam, r["query"])]
                    d.setdefault("fam", fam); d.setdefault("q", r["query"])
                    d["valid"].setdefault(cell, {"write"})
                    add(d["cost"][(cell, "write", r["param_idx"])][unit_of(r["part"], by_dir)],
                        cost_vec(r, write=True, composer=composer))
    # every chunk size's Chunked parts, from its Chunked/Chunked capture. A size takes the grid Chunked's plan validity,
    # so its runs must be the grid's Chunked/Chunked runs row for row; the grid's own size is that reference
    data.sized, data.sized_checked, said = tuple(x[0] for x in composer.sizes), 0, set()
    for oid, sroot, scell in chunk_sizes(root, composer):
        for fam, kind in sorted(fams.items()):
            if kind == "read":
                load_size_reads(data, root, by_dir, fam, oid, sroot, scell, said)
            else:
                load_size_writes(data, by_dir, dataset, fam, oid, sroot, scell)
    if composer.sizes:
        check_size_parts(data)
    return data


def _shas(rows, side, workload):
    """(query, variant) -> its body_sha, as today names it (a body rewritten count-equal since the capture: its
    successor, candidates.successor_sha); None when the rows carry no body_sha (an older capture). Two shas under one
    variant are refused"""
    from kvevaluator.plan_choice.candidates import successor_sha
    if not rows or not all(r.get("body_sha") for r in rows):
        return None
    out = {}
    for r in rows:
        k = (r["query"], r["body"])
        sha = successor_sha(workload, r["query"], r["body_sha"])
        if out.setdefault(k, sha) != sha:
            raise SystemExit(f"{side}: {k[0]} {k[1]} ran two bodies ({out[k]}, {sha})")
    return out


def size_names(oid, fam, mine, theirs, drift_ok, said):
    """(query, variant) of the size capture -> the grid's name for the same body: a name the grid has with the same body_sha
    is kept; a name the grid lacks takes the one grid variant with its body_sha (printed once per (option, query, name) in
    `said`); a name the grid has with another body_sha is kept only for a query in drift_ok, else refused. Without
    body_sha on either side, every name is kept"""
    a, b = _shas(mine, oid, fam), _shas(theirs, "the grid", fam)
    if a is None or b is None:
        return {}
    by_sha = defaultdict(set)
    for (q, v), s in b.items():
        by_sha[(q, s)].add(v)
    names = {}
    for (q, v), s in sorted(a.items()):
        if (q, v) in b:
            if b[(q, v)] != s and q not in drift_ok:
                raise SystemExit(f"{oid}: {fam} {q} {v} ran body {s}, the grid's {b[(q, v)]} under the same name: the counts "
                                 f"may differ (STOP; a query checked equal is passed in Composer.sized_drift_ok, now {sorted(drift_ok)})")
            if b[(q, v)] != s and (oid, q, v) not in said:
                said.add((oid, q, v))
                print(f"[compose] {oid}: {fam} {q} {v}: body {s} joined by name to the grid's {b[(q, v)]} (drift_ok)", file=sys.stderr)
            names[(q, v)] = v
            continue
        g = sorted(x for x in by_sha.get((q, s), ()) if (q, x) not in a)   # a grid name the size capture also ran is its own
        if len(g) == 1:
            names[(q, v)] = g[0]
            if (oid, q, v) not in said:
                said.add((oid, q, v))
                print(f"[compose] {oid}: {fam} {q} {v} -> the grid's {g[0]} (renamed, same body {s})", file=sys.stderr)
        # no grid name, or two, with this body: the name is kept and the runs check below refuses it by name
    return names


def read_sized_parts(data, fam, key, oid, path, by_dir, name, ok):
    """one size option's parts file into data[(fam, query)][key][(oid, body, param)][unit]. A run that is ok is keyed by its
    index row too: a body that read the index and found nothing past it (JOB's job2c at the 1% sample) has no Chunked
    part, a true 0, said once per parts file; a run with no row at all, or not ok, stays unkeyed and is refused where it
    is costed (_composed: a missing part is never costed 0)"""
    vec = lambda: zero(data.composer)
    index_only = set()
    with open(path) as f:
        for r in capture_layout.reader(f):
            u = unit_of(r["part"], by_dir)
            run = (r["query"], name(r), r["param_idx"])
            cost = data[(fam, r["query"])].setdefault(key, defaultdict(lambda: defaultdict(vec)))
            if u is not None:
                add(cost[(oid, *run[1:])][u], cost_vec(r, write=False, composer=data.composer))
            elif ok.get(run) == "1":
                cost.setdefault((oid, *run[1:]), defaultdict(vec))
                index_only.add(run)
    zero_runs = [(q, v, i) for q, v, i in sorted(index_only)
                 if not any(u is not None for u in data[(fam, q)][key][(oid, v, i)])]
    if zero_runs:
        q, v, i = zero_runs[0]
        print(f"[compose] {oid}: {len(zero_runs)} {fam} runs ({key}) read the index and nothing past it (each run is ok): "
              f"their Chunked parts cost 0; first: {q} {v} param {i}", file=sys.stderr)


SIZE_RUN_FIELDS = ("ok", "planner_pick", "measure_only", "result_hash")   # what a size's runs share with the grid's Ch64k-Ch64k


def load_size_writes(data, by_dir, dataset, fam, oid, sroot, scell):
    """one chunk size's write parts of `fam` into data, keyed by its option id; its runs must have the grid
    Chunked/Chunked's params. No capture: recorded in data.sized_missing"""
    composer = data.composer
    path = writes_layout_capture(sroot, dataset, scell, composer.param_set).path_of("parts", scell)
    if not os.path.exists(path):
        data.sized_missing[oid].add(fam)
        return
    ran = defaultdict(set)
    for r in capture_layout.write_rows(path, cell=scell)[2]:
        r = chunk_view(r, composer.chunk_view, scell)
        ran[r["query"]].add(r["param_idx"])
        u = unit_of(r["part"], by_dir)
        if u is not None:
            add(data[(fam, r["query"])]["cost"][(oid, "write", r["param_idx"])][u], cost_vec(r, write=True, composer=composer))
    for q, ps in sorted(ran.items()):
        grid_ps = {k[2] for k in data[(fam, q)]["cost"] if k[0] == CH_CH}
        if ps != grid_ps:
            raise SystemExit(f"{oid}: write {q} ran params {sorted(ps, key=int)}, the grid's {CH_CH} {sorted(grid_ps, key=int)}")


def load_size_reads(data, root, by_dir, fam, oid, sroot, scell, said):
    """one chunk size's read parts of `fam` into data, keyed by its option id: every unit's rows, reported and held-out.
    Another size's runs must be the grid's Chunked/Chunked runs row for row (SIZE_RUN_FIELDS): a difference stops the
    load and names the query. The two join by (query, body_sha, param): a body renamed between the captures takes the
    grid's name (size_names), in its runs and its parts. No capture: recorded in data.sized_missing (refused when a
    query of it is priced on that size). A run that is ok and read only the index has no Chunked part: a true 0
    (read_sized_parts)."""
    composer = data.composer
    sdir = layout_capture(sroot, fam, scell, composer.param_set).path
    rdir = layout_capture(root, fam, CH_CH, composer.param_set).path
    if not os.path.exists(Capture(sdir).path_of("parts", scell)):
        data.sized_missing[oid].add(fam)
        return
    held = os.path.exists(layout_capture(root, fam, CH_CH, HELD_OUT).path_of("parts", CH_CH))
    for sub, key in ((False, "cost"),) + ((True, "cost_h"),) * held:
        mine = grid_capture.load_held_out(sdir, scell) if sub else grid_capture.load_runs(sdir, scell)
        if mine is None:
            raise SystemExit(f"{oid}: the grid has a held-out set and {os.path.relpath(sdir)} has none for {scell}: "
                             f"its options cannot be picked under the rule (the plan rule)")
        # the grid's own size is the reference: nothing to compare, no body to rename
        names = {} if oid == GRID_CHUNKED else size_runs_checked(data, fam, oid, sub, mine, rdir, said)
        name = lambda r: names.get((r["query"], r["body"]), r["body"])
        ok = {(r["query"], name(r), r["param_idx"]): r.get("ok", "") for r in mine}   # the run's ok, by the grid's body name
        read_sized_parts(data, fam, key, oid, layout_capture(sroot, fam, scell, HELD_OUT if sub else composer.param_set)
                         .path_of("parts", scell), by_dir, name, ok)


def size_runs_checked(data, fam, oid, sub, mine, rdir, said):
    """another size's runs `mine` (held-out if `sub`) against the grid's Chunked/Chunked runs in `rdir`, row for row in
    SIZE_RUN_FIELDS (a difference stops the load); returns size_names' renames"""
    theirs = grid_capture.load_held_out(rdir, CH_CH) if sub else grid_capture.load_runs(rdir, CH_CH)
    names = size_names(oid, fam, mine, theirs, data.composer.sized_drift_ok, said)
    name = lambda r: names.get((r["query"], r["body"]), r["body"])
    rows = lambda rs, nm: {(r["query"], nm(r), r["param_idx"]): tuple(r.get(c, "") for c in SIZE_RUN_FIELDS) for r in rs}
    a_, b_ = rows(mine, name), rows(theirs, lambda r: r["body"])
    diff = sorted(k for k in set(a_) | set(b_) if a_.get(k) != b_.get(k))
    if diff:
        k = diff[0]
        raise SystemExit(f"{oid}: {len(diff)} {fam} {'held-out' if sub else 'reported'} runs differ from the grid's {CH_CH} "
                         f"in {SIZE_RUN_FIELDS} ({len({x[0] for x in diff})} queries), first: query {k[0]}, plan {k[1]}, "
                         f"param {k[2]}: {a_.get(k)} against {b_.get(k)}. A size option takes Chunked's plan validity, so "
                         f"the two must agree: STOP, a decision is needed for this query")
    data.sized_checked += len(a_)
    return names


def check_size_parts(data):
    """every (plan, param, unit) the grid's Chunked has rows for must have rows at each other size, and no other: a
    missing row would be priced 0 (_composed skips a missing vector)"""
    keys = 0
    for oid in data.sized:
        for (fam, q), d in sorted(data.items()):
            if fam in data.sized_missing[oid]:
                continue
            for key in ("cost", "cost_h"):
                rows = lambda src: {(k[1], k[2], u) for k, per in d.get(key, {}).items() if k[0] == src for u in per if u is not None}
                mine, theirs = rows(oid), rows(GRID_CHUNKED)
                keys += len(theirs)
                if mine != theirs:
                    x = sorted(mine ^ theirs, key=str)[0]
                    raise SystemExit(f"{oid}: {fam} {q} ({key}): {len(mine ^ theirs)} (plan, param, unit) rows are in one of this size's "
                                     f"capture and the grid's {CH_CH} only, first: {x} ({'this size' if x in mine else CH_CH} only)")
    print(f"[compose] chunk sizes as options: {', '.join(data.sized)}; {data.sized_checked:,} runs equal to the grid's {CH_CH} in "
          f"{SIZE_RUN_FIELDS}, {keys:,} (plan, param, unit) part rows present on both" + "".join(f"; {o}: no capture of {sorted(m)}" for o, m in sorted(data.sized_missing.items())), file=sys.stderr)


def caps_assign(assign, units, composer):
    """the query's units with each layout as the capabilities see it: a size option is Chunked (its own chunk size
    changes no capability; one spec per distinct capability set, not per size)"""
    return {u: (GRID_CHUNKED if is_sized(assign[u], composer) else assign[u]) for u in units if u in assign}


def _composed(d, cost_key, params, assign, units, pairs, playouts, alayouts, variants):
    """{variant: (total vec, {unit: vec})} composed from the parts under `cost_key` ("cost" = the reported params, "cost_h"
    = the held-out params), mean over `params`"""
    tots, per_units = {}, {}
    for v in sorted(variants):
        tot = zero(d["composer"])
        per_unit = defaultdict(lambda: zero(d["composer"]))
        for i in params:
            for u in sorted(units) + [None]:            # a fixed order: float sums must not follow the hash seed
                if u is None:
                    src = pairs[0]                           # the index: identical in every cell
                elif is_sized(assign[u], d["composer"]):     # Chunked at its size: that size's Chunked/Chunked rows
                    src = assign[u]
                elif u[0] == "props":
                    src = cell_id(assign[u], min(alayouts))
                else:
                    src = cell_id(min(playouts), assign[u])
                rows = d.get(cost_key, {}).get((src, v, i))
                # a missing part is never priced 0: a usable plan with no
                # part rows at all in its source, or a size option with no row where the grid's Chunked/Chunked has one
                # (data.sized_missing is the load's own), stops here, whatever the load did
                if rows is None:
                    raise SystemExit(f"{d.get('fam')} {d.get('q')}: {src} has no {cost_key} part rows for plan {v}, param {i}: "
                                     f"it would be priced 0")
                vec = rows.get(u)
                if vec is None and u is not None and is_sized(src, d["composer"]) and d.get(cost_key, {}).get((CH_CH, v, i), {}).get(u):
                    raise SystemExit(f"{d.get('fam')} {d.get('q')}: {src} has no {cost_key} row for plan {v}, param {i}, unit {u}, "
                                     f"where {CH_CH} has one: it would be priced 0")
                if vec:
                    add(tot, vec, 1.0 / len(params))
                    add(per_unit[u], vec, 1.0 / len(params))
        tots[v], per_units[v] = tot, dict(per_unit)
    return tots, per_units


def _scalar(t):
    return {"S": t["S"], "G": t["G"], "N": t["N"], "total_bytes": sum(t[l] for l in C.READ_LANES)}


MODES = ("option", "store")   # how an assignment is priced: see query_pick


def _option(d, assign, units):
    """the cells a query's parts are costed from under `assign`: (props layouts, adjacency layouts, size options, the
    (props, adj) cells they pair into, the plans valid in every one of those cells)"""
    composer = d["composer"]
    playouts = {validity_layout(assign[u], composer) for u in units if u[0] == "props"} or set(LAYOUTS)
    alayouts = {validity_layout(assign[u], composer) for u in units if u[0] == "adj"} or set(LAYOUTS)
    sized = {assign[u] for u in units if is_sized(assign[u], composer)}
    for oid in sorted(sized):
        if d.get("fam") in d["sized_missing"].get(oid, ()):
            raise SystemExit(f"{d.get('fam')} {d.get('q')}: {oid} has no capture of this family: it cannot be priced on that size")
    # sorted: a set's order follows the hash seed, and which cell a part is priced from must not change per run
    pairs = [cell_id(p, a) for p in sorted(playouts) for a in sorted(alayouts)]
    variants = set.intersection(*(d["valid"].get(c, set()) for c in pairs))
    return playouts, alayouts, sized, pairs, variants


def _require_held_out(d, pairs):
    missing = [c for c in pairs if c not in d.get("valid_h", {})]
    if missing:                                       # a grid with a held-out set must have it for every cell it prices from
        raise SystemExit(f"{d.get('fam')} {d.get('q')}: no held-out rows for {missing} (grid_bodies --params 11,12,13,14,15 "
                         f"--out the held-out capture (<set>/<workload>/held-out) for every cell, or none)")


def _held_out_vectors(d, assign, units, pairs, playouts, alayouts, among):
    """{plan: vector composed from the HELD-OUT parts} for the plans in `among` usable on the held-out params of every cell"""
    usable_h = set.intersection(*(d["valid_h"][c] for c in pairs))
    params_h = sorted({k[2] for k in d["cost_h"]}, key=int)
    tots_h, _pu = _composed(d, "cost_h", params_h, assign, units, pairs, playouts, alayouts, usable_h & set(among))
    return tots_h


def query_candidates(d, params, assign, units):
    """every plan of a query usable under `assign`, as query_pick sees them before it picks: (reported {plan: vector}
    (mean over `params`), {plan: {unit: vector}}, held-out {plan: vector} for the plans usable on both sets and not
    measure-only ({} when the grid has no held-out parts), the measure-only plans). No usable plan: ({}, {}, {}, set())."""
    playouts, alayouts, _sized, pairs, variants = _option(d, assign, units)
    tots, per_units = _composed(d, "cost", params, assign, units, pairs, playouts, alayouts, variants)
    if not tots:
        return {}, {}, {}, set()
    mo = measure_only_for_assignment(d, assign, units)
    tots_h = {}
    if d.get("cost_h"):
        _require_held_out(d, pairs)
        tots_h = _held_out_vectors(d, assign, units, pairs, playouts, alayouts, set(tots) - mo)
    return tots, per_units, tots_h, mo


def query_pick(d, params, assign, units, mode="option"):
    """the query's mean cost over `params` under `assign` (unit -> layout), at the usable plan the shared rule picks;
    (variant, {variant: vec}, {variant: {unit: vec}}), (None, None, None) if no plan is usable. Props units take their cost from a cell with that props layout, adjacency
    units from a cell with that adjacency layout; a plan is usable iff valid in every (props, adj) pair the mix puts
    together for this query.
    THE RULE (the plan rule, one rule for every priced thing, built or not): with held-out parts in the grid (`cost_h`,
    compose.load), the plan is candidates.held_out_pick over the plans usable on BOTH sets, scored on the vectors
    composed from the HELD-OUT parts, with no anchor and no pin (a uniform option too, in option mode; a built mix's
    record row pins it in store mode); the COST is the vector composed from the
    reported parts of that plan. Without held-out parts: the in-sample argmin (candidates.pick_body). Ties: the first
    usable plan in sorted order. query_cost returns the cost of that plan.
    `mode`: "option" (the default: a candidate in a search, the cost cache and every re-price of a solved point) never binds
    a recorded store's row by the WHOLE assignment, so a query's pick depends on its own units only and a solve equals
    its re-price; "store" (a measured or recorded store priced as itself: check_mix_vs_built,
    composed_vs_measured, a built mix's predicted point) binds the row of the store whose assignment this is, where
    picks.bound_store says (recorded_for_option). An option uniform on the query's units is that cell (choose_cell): in
    both modes at Split and Condensed, in store mode also with the grid's Chunked (64 KB)."""
    if mode not in MODES:
        raise ValueError(f"mode {mode!r}: one of {MODES}")
    composer = d["composer"]
    criterion = composer.criterion
    playouts, alayouts, sized, pairs, variants = _option(d, assign, units)
    tots, per_units = _composed(d, "cost", params, assign, units, pairs, playouts, alayouts, variants)
    if not tots:
        return None, None, None
    # the pick excludes a measure-only body: a twin flagged in the registry (TWIN_MEASURE_ONLY, every store), and -- once
    # the cell-dependent flag lands (measure_only_where) -- the bodies measure-only on the MIX's own
    # assignment (its spec synthesized from `assign`); a body that is the only usable one stays priced (never None here)
    mo = measure_only_for_assignment(d, assign, units)
    pool = {v: t for v, t in tots.items() if v not in mo}
    if not pool:
        raise SystemExit(f"{d.get('fam')} {d.get('q')}: every usable plan on this option is measure-only ({sorted(tots)}): a "
                         f"measure-only body is never priced as the pick")
    if not d.get("cost_h"):
        if set(tots) == {"write"}:                                # a write family has ONE plan: nothing to choose
            return "write", tots, per_units
        if len(tots) == 1:                                        # ONE usable plan on this option (LinkBench: one body per
            return next(iter(tots)), tots, per_units              # request): nothing to choose, so no held-out set is needed
        # in-sample by the rule's default families (the microbench: no held-out set exists). Not picks_in_sample: a
        # composed option of a capture without held-out rows under in_sample=True stops here
        if d.get("fam") in composer.plan_rule.in_sample_default:
            # no anchor: the cheapest; float costs (a predicted capture) tie at tie_digits significant digits
            best, _ = C.pick_body({v: _scalar(t) for v, t in pool.items()}, criterion,
                                  tie_digits=composer.plan_rule.tie_digits)
            return best, tots, per_units
        raise SystemExit(f"{d.get('fam')} {d.get('q')}: the grid has no held-out parts: a composed option cannot be priced under "
                         f"the plan rule (kvcost measure, held-out params included, for every cell)")
    _require_held_out(d, pairs)
    # THE SAME decision a cell gets (candidates.decide): a uniform option IS a cell -- its pick is the cell's (picks.choose_cell
    # .choose_cell: the held-out rule, the record, the per-scale exception); any other option has no anchor, the
    # vectors composed from the held-out parts for
    # the choice, the record's row when the option is a built mix (by its assignment), and the reported parts for the cost
    # a query's pick depends only on the units it reads (exact_search's premise): every capability a body requires is
    # checked on the label or edge type it names (dump_planir's `on`; the plan-file lint holds it), so an option uniform
    # on this query's units IS the cell for this query. Option mode picks Chunked at every size, 64 KB too, on the
    # held-out vectors (equal to the 64 KB cells' own picks on every query of the LDBC mixes and BI at SF3, JOB and
    # LinkBench); store mode keeps a grid cell's own pick
    other_sizes = sized - {GRID_CHUNKED}
    uniform = len(playouts) == 1 and len(alayouts) == 1 and not (other_sizes if mode == "store" else sized)
    if uniform:
        cell = pairs[0]
        # one rule for every priced query: in option mode a uniform query is chosen as a spanning
        # one is, on this scale's held-out rows with no anchor and no pin; store mode (a measured store priced as
        # itself) keeps the record's anchored, pinned row, which is what that store runs
        rule = composer.plan_rule.but(anchor=False, pin=False) if mode == "option" else composer.plan_rule
        rdir = d["read_dirs"][cell]       # the cell's capture: measured itself, else composed (layout_capture)
        key = (rdir, cell, criterion, rule, mode)   # all it reads
        chosen = _CELL_PICK.get(key)
        if chosen is None:
            chosen = _CELL_PICK[key] = picks.choose_cell(rdir, cell, criterion, rule=rule)
        best = chosen[d["q"]]["body"]
        if best not in tots:
            raise SystemExit(f"{d['fam']} {d['q']}: the cell's pick {best!r} is not among the composed plans of {cell} ({sorted(tots)})")
        return best, tots, per_units
    tots_h = _held_out_vectors(d, assign, units, pairs, playouts, alayouts, set(pool))
    if not tots_h:
        raise SystemExit(f"{d.get('fam')} {d.get('q')}: no plan usable on both the reported and the held-out params for {assign}")
    planner = ""          # a composed option has no record entry: no anchor, the cheapest (the record's row when a built mix)
    scores = C.pick_body({v: _scalar(t) for v, t in pool.items()}, criterion)[1]
    scores_h = C.pick_body({v: _scalar(t) for v, t in tots_h.items()}, criterion)[1]
    pinned, own = recorded_for_option(d, assign, units, other_sizes) if mode == "store" else (None, None)
    best, _rule, _why = C.decide(d["q"], scores, scores_h, planner, pinned=pinned, scale_picks=own)
    return best, tots, per_units


def query_cost(d, params, assign, units, by_unit=False, mode="option"):
    """the query's mean cost vector over `params` under `assign` at the plan query_pick chooses; None if no plan is
    usable. by_unit=True returns (total, {unit: vec}) of that plan (None = the index)."""
    best, tots, per_units = query_pick(d, params, assign, units, mode)
    if best is None:
        return (None, None) if by_unit else None
    return (tots[best], per_units[best]) if by_unit else tots[best]


_CELL_PICK = {}     # (read dir, cell, criterion, the plan rule, mode) -> choose_cell's result (a uniform
                    # option is priced at its own cell's pick)
def recorded_for_option(d, assign, units, sized=()):
    """STORE mode only (query_pick): (the recorded picks, this scale's own picks) of the store whose layout this whole
    assignment is -- a built mix whose recorded assignment EQUALS the option's whole normalised assignment (the same unit
    keys, directions included, the same layouts), or a uniform cell by its name -- bound where
    picks.bound_store says (the record checked that store at this scale; never at the deciding scale). A per-direction
    option with both directions of every edge type at one layout is the store of a per type assignment, so its per type
    form is compared too (the record holds a per type mix). Chunked at another size (`sized`, Composer.sizes) names
    no store (no store-mode caller prices sizes). (None, None) otherwise, said once per option: the held-out rule on its rows."""
    rec, sf = d.get("record"), d.get("sf")
    if not rec or sf is None or str(sf) == str(rec.decided_at):
        return None, None
    mine = {f"{u[0]}:{':'.join(u[1:])}": l for u, l in sorted(assign.items())}
    if not mine:
        return None, None
    props = {l for u, l in assign.items() if u[0] == "props"}; adj = {l for u, l in assign.items() if u[0] == "adj"}
    cell = cell_id(next(iter(props)), next(iter(adj))) if len(props) == 1 and len(adj) == 1 and not sized else None
    forms = [mine] + ([per_type] if d["composer"].bind_per_type_record and (per_type := per_type_form(assign)) else [])
    rows = [m for m, a in sorted(rec.assignment.items()) if a in forms]
    if len(rows) > 1:                                        # one store has one assignment: two rows with it is no record
        raise SystemExit(f"pick_stores.json names two stores with one assignment ({rows}): a malformed record, rewrite it "
                         f"(grid_pick_record.py at the deciding scale)")
    hit = cell if rec.has_cell(picks.RECORD_WORKLOAD, cell) else (rows[0] if rows else None)
    store = picks.bound_store(rec, sf, hit)
    if store is None:
        key = tuple(sorted(mine.items()))
        if key not in _UNRECORDED:
            _UNRECORDED.add(key)
            # said once per process, never in a pool worker: a cost cache fill meets one per assignment (3.5 M lines per
            # sf0.1 check); cost_cache.problems says it once for its workers
            if multiprocessing.parent_process() is None and (hit is not None or len(_UNRECORDED) == 1):
                what = f"{hit}, not checked at sf{sf}" if hit else f"an option that is no recorded store ({len(mine)} units)"
                print(f"[compose] {what}: the held-out rule on its own capabilities, no record row (picks.bound_store; "
                      f"said once{' per store' if hit else ''})", file=sys.stderr)
        return None, None
    return store_mode_picks(rec, store, sf)


def store_mode_picks(rec, store, sf):
    """({query: the record's body}, {query: this scale's own body}) a bound store hands store mode: every row as
    recorded, carried ones included (unlike picks.bound_picks, which pins nothing for a carried row)"""
    return ({q: x["body"] for q, x in rec.cell(picks.RECORD_WORKLOAD, store).items()},
            {q: x["body"] for q, x in rec.exceptions(picks.RECORD_WORKLOAD, sf, store).items()})


_UNRECORDED = set()


def per_type_form(assign):
    """a per-direction assignment in the record's per type keys ({"adj:knows": layout}), when both directions of every
    edge type have one layout; None when a type's directions differ or the assignment is per type already"""
    by_type, directed = {}, False
    for u, l in assign.items():
        directed = directed or (u[0] == "adj" and len(u) == 3)
        by_type.setdefault(u[:2], set()).add(l)
    if not directed or any(len(ls) > 1 for ls in by_type.values()):
        return None
    return {f"{u[0]}:{u[1]}": next(iter(ls)) for u, ls in sorted(by_type.items())}


_MO_CELL = {}     # (fam, q, the assignment's layouts) -> the bodies measure-only on that mix (cached: the search calls this per assignment)


def measure_only_for_assignment(d, assign, units):
    """the variants of this query that may not be PICKED for a mix: the registry's measure-only twins (every store), and the
    bodies measure-only on the MIX's own layout (a bundle variant's `measure_only_where`, grid_capture.measure_only_on_cell
    on the spec synthesized from the assignment: a per-direction assignment has no spec, then only the registry's apply)"""
    mo = set(d.get("measure_only_registry", ()))
    if not d.get("measure_only_where"):
        return mo
    assign = caps_assign(assign, units, d["composer"])
    key = (d["fam"], d["q"], tuple(sorted((u, assign[u]) for u in units if u in assign)))   # the query's units
    if key not in _MO_CELL:
        import tempfile
        from kvevaluator.measure.grid_capture import measure_only_on_cell
        try:
            spec, _ = spec_of({u: assign[u] for u in units if u in assign})
        except ValueError:                                   # per direction: no buildable spec on this binary
            _MO_CELL[key] = set()
        else:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump(spec, f)
            try:
                _MO_CELL[key] = measure_only_on_cell(d["fam"], d["q"], f.name)
            finally:
                os.unlink(f.name)
    return mo | _MO_CELL[key]


def grid_units(data):
    """the unit universe of a grid capture: every unit any part of any cell names (reads and writes), sorted -- the SAME
    set a record's assignment (grid_pick_record.mix_identity) and a composed option (check_mix_vs_built) are normalised over, so the
    record binds by exact equality"""
    return sorted({u for dd in data.values() for per in dd["cost"].values() for u in per if u is not None})


def workload_units(w, data):
    units = set()
    for family, q, _, _ in w["items"]:
        for per in data[(family, q)]["cost"].values():
            units |= {u for u in per if u is not None}
    return sorted(units)


def params_of(d, params):
    have = sorted({k[2] for k in d["cost"]}, key=int)
    return [str(i) for i in params] if params else have


def cost_mix(w, data, assign, mode="option"):
    """a workload's total cost vector under `assign`, None if a query has no usable plan; `mode`: query_pick's"""
    tot = zero(data.composer)
    for family, q, often, params in w["items"]:
        d = data[(family, q)]
        units = {u for per in d["cost"].values() for u in per if u is not None}
        c = query_cost(d, params_of(d, params), assign, units, mode=mode)
        if c is None:
            return None
        add(tot, c, often)
    return tot


def space_of(data, units):
    """(the number of assignments, '3^25 x 4 x 5')"""
    sizes = [len(options_of(data, u)) for u in units]
    n = 1
    for k in sizes:
        n *= k
    txt = " x ".join(f"{k}^{sizes.count(k)}" if sizes.count(k) > 1 else f"{k}" for k in sorted(set(sizes)))
    return n, txt or "1"


def main():
    from kvcost.layout_search import search as cost_cache
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True, help="the suite's axis value: LDBC's scale factor, LinkBench's scale tag")
    ap.add_argument("--dataset", default="ldbc", choices=benchmarks.DATASETS)
    ap.add_argument("--grid", default=None)
    ap.add_argument("--by-direction", action="store_true", help="adjacency per (edge type, direction)")
    benchmarks.add_workload_arg(ap)
    add_pick_by_arg(ap)
    benchmarks.add_params_arg(ap)
    benchmarks.add_chunk_view_arg(ap)
    cost_cache.add_cache_args(ap)      # --fresh: each query's costs are kept and reused across runs
    a = ap.parse_args()
    cost_cache.apply_cache_args(a)
    root = a.grid or str(Tree().place_dir("captures", a.dataset, a.sf))
    data = load(root, a.by_direction, a.dataset, composer=Composer.from_args(a))
    measured = load_measured(a.dataset, root, a.criterion, data.families, rule=data.composer.plan_rule,
                             param_set=data.composer.param_set, view=data.composer.chunk_view)
    bad = 0
    for name, w in benchmarks.select(a.dataset, a.sf, a.workload).items():
        units = workload_units(w, data)
        # 1. the check: every uniform assignment reproduces the measured cell exactly
        uni = {}
        for cell in CELLS:
            p, ad = axes(cell)
            got = cost_mix(w, data, {u: (p if u[0] == "props" else ad) for u in units}, mode="store")
            want = cost_or_refuse(w, measured[cell], cell)
            diff = [m for m in MEASURES if got is None or abs(got[m] - want[m]) > 1e-6 * max(1.0, abs(want[m]))]
            if diff:
                bad += 1
                print(f"  CHECK FAIL {name} {cell}: {diff} got {got and {m: round(got[m], 2) for m in diff}} "
                      f"want {{{', '.join(f'{m}: {want[m]:.2f}' for m in diff)}}}")
            uni[cell] = want
        # 2. the mix cost cache of this option set: built once per driver run, handed on to sweep and workload_story
        # (the exact solves on them are workload_story's: solving here too only repeated them into the log)
        pb = cost_cache.problem(w, data, units)
        n, txt = space_of(data, units)
        print(f"\n== {name} ({len(units)} parts, {txt} = {n:,} assignments; cost cache {pb.cost_cache_seconds:.1f}s)")
    print(f"uniform check (the 9 cells): "
          f"{'PASS' if not bad else f'{bad} FAIL'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
