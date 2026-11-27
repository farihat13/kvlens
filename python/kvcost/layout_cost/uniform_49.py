"""The 49 uniform layouts of a workload: every (properties, adjacency) pair over Split, Condensed and Chunked at each chunk
size, composed per part, each measured one checked against its own capture. Analysis only: opens no store.
`kvcost cost-layouts --dataset D --workload W Sp-Sp Sp-Co ... Ch256k-Ch256k` runs it (python/kvcost/cli/search.py); the derives import it.

A uniform layout gives every properties part one setting and every adjacency part one setting (props P / adjacency A).
With the grid's chunk size and the other sizes, P and A range over Sp, Co and Chunked at each size: 7 x 7 = 49 layouts
for four extra sizes, of which the grid and each size's Chunked/Chunked capture measure only some (at SF3: 17). The rest
are composed: each part from the capture its setting names (compose.Composer.sizes); each query's plan is
compose.query_pick's (decided on the held-out params), its cost on the reported ones. Totals:
    total ops   = read weighted ops (G + 1.1 S + 0.01 N) + the writes' own reads (same weights) + PUTs + DELs
    total bytes = bytes read + the bytes the writes read first + bytes written

The check: every layout a capture MEASURED (its counts file is there, not composed) is costed from that capture alone
(compose.workload_cost on its own reads and writes) and must equal its composed cost in every measure outside miss-key
bytes (a missed probe lands on a neighbour part's key); it fails on a difference or when 0 measured layouts were
compared. The control (plant=True) adds one SEEK's worth of ops to one measured layout's cost: it must fail.

Writes, under the output tree's summary/<dataset>/<scale>/uniform-49/<workload>[-<param set>][-<criterion>]/:
    uniform_layouts_by_size.csv   a row per layout: workload, cell, props, adjacency, measured_as, total_ops,
                                  total_bytes, every raw lane (compose.MEASURES, compose.CHANNELS)
    query_costs.csv               a row per (query, layout): the picked plan's cost per run (wops, bytes, wops_bytes4k
                                  and the raw channels), the workload's items in order, the layouts sorted
    query_parts.csv               a row per (query, layout, part: adj, props, index): that plan's cost per run on the
                                  part (every measure and raw channel), the census's per-part costs
    README.md                     what was read and the check's verdict
A chunk view other than half adds _<view> to each name (uniform_layouts_by_size_measured.csv), beside the half view's.
With bill="sized" (cost-layouts --cost-model bill_exact, the measured view) every file also keeps the bill's unit lanes
(compose.BILL: exact, lower and upper, read and write): the paper's bill tables read them.
"""
import json
import os
import time

from kvcost.layout_cost import benchmarks, cost_models
from kvcost.layout_cost import compose
from kvcost.layout_cost.compose import load_reads, load_writes, workload_cost
from kvcost.layout_cost.settings import parse_sizes, size_name
from kvevaluator.layouts import CHUNK_SIZES_KB, is_cell
from kvevaluator.output_tree import capture_in, layout_capture

RAW = list(compose.MEASURES) + list(compose.CHANNELS)
MISS_MEASURES = ("miss_key_bytes", "write_rmw_miss_key_bytes")

# the measured side of the check takes the plan rule a layout composed from its parts takes (option mode, decided
# no anchor, no pin), so each measured layout is compared at the plans its composition chose
OPTION_RULE = compose.PlanRule().but(anchor=False, pin=False)

PART_KINDS = ("adj", "props", "index")   # a unit's kind: ("adj", <type>), ("props", <type>), None = the index


def bill_lanes(composer):
    """the bill's unit lanes a composer's vectors carry (none without Composer.bill), in the vector's order"""
    return [k for k in compose.zero(composer) if k not in RAW]


def first_read(families):
    """the family whose capture names the grid's chunk size and its measured cells"""
    return next(f for f, k in families if k == "read")


def grid_size_of(place, family, param_set=None):
    """the chunk size of the layouts' `chunked` setting, from the Chunked/Chunked run record's spec (its capture by
    output_tree.layout_capture)"""
    cell = "props_chunked_adj_chunked"
    with open(layout_capture(place, family, cell, param_set).record(cell)) as f:
        return size_name(json.load(f)["spec"]["adjacency"]["owners"]["pack"])


def other_sizes(measured, names=None):
    """parse_sizes' [(option, place, cell)] of the chunk sizes other than the grid's (default every one but 64 KB), each
    from its Chunked/Chunked in the layouts' place"""
    names = names or [f"{k}k" for k in CHUNK_SIZES_KB if k != 64]
    return parse_sizes(names, measured)


def settings_of(sizes, gsize):
    """the 7 settings in order: Split, Condensed, then Chunked by chunk size, the grid's own size as `chunked`"""
    kb = lambda o: int(o[len("chunked_c"):-1])  # chunked_c4k -> 4
    own = int(gsize[:-1])
    return (["split", "condensed"] + [o for o, _, _ in sizes if kb(o) < own] + ["chunked"]
            + [o for o, _, _ in sizes if kb(o) > own])


def measured_cells(root, family, param_set=None):
    """[(capture root, cell, cell)]: the uniform layouts `family`'s capture measured (a counts file and a run record not
    composed by compose_cells.py); named formats and mixes filed beside them are left out, and so is a counts file with
    no run record (the paper's latency runs kept only the counts of the bodies they timed: no parts to cost from)"""
    cap = capture_in(root, family, param_set)
    out = []
    for c in filter(is_cell, cap.stores()):
        if not os.path.exists(cap.record(c)):
            continue
        with open(cap.record(c)) as f:
            if not json.load(f).get("composed_from"):
                out.append((root, c, c))
    return out


def load_49(place, sizes, dataset, sf, wid, chunk_view, bill="", param_set=None, plan_rule=None, criterion=None):
    """(w, data, settings): one workload's load for the 49 uniform layouts, the one way to build it. place: the
    dataset's captures/<dataset>/<scale> dir (each layout read from it by output_tree.layout_capture); sizes: parse_sizes' [(option, root, cell)] of the other chunk sizes; wid: a workload
    id, or a workload file (an equal-share file: query_weighting.equal_share_path); plan_rule: default PlanRule()"""
    if str(wid).endswith(".json"):
        _, w = benchmarks.read_workload(wid)
    else:
        (_, w), = benchmarks.select(dataset, sf, wid).items()
    families = benchmarks.families_read(dataset, [w])
    composer = compose.Composer(criterion=criterion, sizes=sizes, bill=bill, plan_rule=plan_rule or compose.PlanRule(),
                                families=families, chunk_view=chunk_view, param_set=param_set)
    settings = settings_of(sizes, grid_size_of(str(place), first_read(families), param_set))
    return w, compose.load(str(place), False, dataset, composer=composer), settings


def query_row(where, body, vec, extra):
    """one query_costs row: `where` (family, query, layout, props, adjacency), the picked plan and its cost per run
    `vec` (wops, bytes, wops_bytes4k, the raw channels, and the `extra` lanes)"""
    ch = {k: float(vec[k]) for k in compose.CHANNELS}
    c = cost_models.of_channels(ch)
    return {**where, "body": body, "wops": repr(c["wops"]), "bytes": repr(c["bytes"]),
            "wops_bytes4k": repr(c["wops_bytes4k"]), **{k: repr(v) for k, v in ch.items()},
            **{k: repr(float(vec[k])) for k in extra}}


def part_rows(where, body, by_unit, composer, extra):
    """the query_parts rows of one picked plan: its per-unit vectors (`by_unit`) summed per part kind (PART_KINDS: a
    unit's kind, None = the index)"""
    rows = []
    for kind in PART_KINDS:
        v = compose.zero(composer)
        for u, vec in sorted(by_unit.items(), key=lambda x: str(x[0])):
            if (u[0] if u is not None else "index") == kind:
                compose.add(v, vec)
        rows.append({**where, "body": body, "part": kind, **{k: repr(float(v[k])) for k in RAW + extra}})
    return rows


def check(w, families, composed, measured, plant, view, dataset=None, param_set=None, rule=OPTION_RULE):
    """(compared, findings): each measured layout's own cost against its composition"""
    bad, planted = [], False
    for root, c, cell in measured:
        if cell not in composed:
            bad.append(f"{cell} (measured as {c} under {root}): not among the composed layouts")
            continue
        meas = workload_cost(w, own_cost(root, c, families, view, dataset, param_set, rule))
        if plant and not planted:
            meas["read_ops"] += 1.1
            planted = True
            print(f"[control] one SEEK's ops added to {cell}'s measured cost")
        t = composed[cell][2]
        diffs = [m for m in compose.MEASURES if m not in MISS_MEASURES and abs(t[m] - meas[m]) > 1e-9 * max(1.0, abs(meas[m]))]
        miss = sum(t[m] - meas[m] for m in MISS_MEASURES)
        print(f"   {cell}: measured as {c} ({os.path.basename(root)}): measures that differ outside miss keys: "
              f"{diffs or 'none'}; miss-key bytes composed - measured {miss:+,.0f}")
        bad += [f"{cell}: {m} measured {meas[m]} vs composed {t[m]}" for m in diffs]
    return len(measured), bad


def own_cost(root, c, families, view, dataset=None, param_set=None, rule=OPTION_RULE):
    """{family: query -> param -> vector} of one measured cell, costed from its own capture alone: a read family at the
    plan the option rule picks, a write family from its writes capture (both at `param_set`'s rows)"""
    return {f: (load_writes(root, c, view, dataset=dataset, param_set=param_set) if k == "write"
                else load_reads(layout_capture(root, f, c, param_set).path, c, rule=rule))
            for f, k in families}


def cost_all(w, data, entries, per_query, per_part):
    """{layout: totals} of each entry (a dict: layout, assign {unit: setting} over the grid's units), every query at the
    plan compose.query_pick picks under the entry's assignment, its cost per run appended to per_query and per_part
    (query_row's and part_rows' fields, `where` from the entry)"""
    extra = bill_lanes(data.composer)
    out = {}
    for e in entries:
        tot = compose.zero(data.composer)
        for family, q, often, params in w["items"]:          # compose.cost_mix, each query's pick kept
            d = data[(family, q)]
            qu = {u for per in d["cost"].values() for u in per if u is not None}
            body, tots, units_of = compose.query_pick(d, compose.params_of(d, params), e["assign"], qu)
            if body is None:
                raise SystemExit(f"{e['name']}: {q} has no plan valid on it")
            compose.add(tot, tots[body], often)
            where = {"family": family, "query": q, "layout": e["layout"], **e["where"]}
            per_query.append(query_row(where, body, tots[body], extra))
            per_part += part_rows(where, body, units_of[body], data.composer, extra)
        out[e["layout"]] = tot
    return out


def cost_rows(w, data, entries, view, measured_as):
    """(totals, layout rows, query rows, part rows) of `entries` (entries_of's) costed on `data` in chunk view `view`:
    cost_all's, with each layout's layouts.csv row (the frequency-weighted totals and the currencies) and the query
    and part rows' view and kind. measured_as: layout -> measured, composed or predicted, as the row says it"""
    from kvcost.layout_cost import layout_costs as LC
    per_query, per_part = [], []
    tots = cost_all(w, data, entries, per_query, per_part)
    extra = bill_lanes(data.composer)
    rows = []
    for e in entries:
        tt = tots[e["layout"]]
        cur = compose.currencies(tt)
        rows.append({"workload": w["id"], "layout": e["layout"], "chunk_view": view, "kind": e["kind"],
                     "name": e["name"], **e["where"], "spec_sha": LC.spec_sha(e["spec"]),
                     "measured_as": measured_as(e["layout"]),
                     "total_ops": round(cur["ops"], 3), "total_bytes": round(cur["bytes"], 1),
                     **{k: round(tt[k], 3) for k in RAW + extra}})
    for r in per_query + per_part:
        r.update(chunk_view=view, kind=next(e["kind"] for e in entries if e["layout"] == r["layout"]))
    return tots, rows, per_query, per_part


def entries_of(layouts, units, sizes):
    """the costed entries of resolved layouts ([(kind, key, name, spec, assign or None)]: kvcost.cli.search's), each
    with its assignment over `units` (a uniform layout: props units its props setting, the rest its adjacency's)"""
    from kvcost.layout_cost import mixes
    from kvevaluator import layouts as L
    out = []
    for kind, key, name, spec, assign in layouts:
        if kind == "uniform":
            p, a = L.cell_settings(key)
            assign = {u: (p if u[0] == "props" else a) for u in units}
            where = {"props": p, "adjacency": a}
        else:
            if callable(assign):                 # a --mix string: its parts set, the rest from its base
                assign = assign(units, sizes)
            elif assign is None:
                assign = mixes.assignment(spec, units, mixes.grid_settings(sizes), name)
            if key is None:                      # named by its content, as a built mix is
                try:
                    spec = mixes.spec_of(assign)[0]
                    key = L.mix_name(spec)
                except SystemExit:
                    import hashlib
                    key = "mix_" + hashlib.sha256(mixes.assignment_text(assign).encode()).hexdigest()[:10]
            left = [u for u in units if u not in assign]
            if left:
                raise SystemExit(f"{name}: no setting for {', '.join(mixes.unit_name(u) for u in left[:4])}")
            where = {"props": "", "adjacency": ""}
        out.append({"layout": key, "kind": kind, "name": name, "spec": spec, "assign": assign, "where": where})
    return out


def sizes_held(place, family, param_set=None):
    """the chunk sizes besides the grid's whose Chunked/Chunked the place holds (measured itself), by size: ['1k', ...]"""
    from kvevaluator.layouts import OTHER_CHUNK_SIZES
    from kvcost.layout_cost.settings import sized_cell
    return [k for k in OTHER_CHUNK_SIZES if os.path.exists(layout_capture(place, family, sized_cell(k), param_set)
                                                            .record(sized_cell(k)))]


def cost_layouts(src, dataset, sf, workload, layouts, *, plan_rule, param_set=None, criterion=None, plant=False, weights=None,
          views=None, by_dir=False):
    """cost `layouts` (entries_of's input) for `workload` (an id or a workload file) from tree `src`'s captures, in each
    chunk view (the bill's lanes on the measured view), each measured uniform layout checked against its own capture.
    by_dir: the adjacency parts per direction (a mix that sets a direction's lists).
    -> (w, {view: (layout rows, query rows, part rows)}, {stage: seconds}, check lines, rc: 0 when every check passes)"""
    from kvcost.layout_cost import layout_costs as LC
    from kvcost.layout_search import search as cost_cache
    if str(workload).endswith(".json"):
        _, w = benchmarks.read_workload(workload)
    else:
        (_, w), = benchmarks.select(dataset, sf, workload).items()
    families = benchmarks.families_read(dataset, [w])
    grid = str(src.place_dir("captures", dataset, sf))         # each layout by output_tree.layout_capture
    cost_cache.set_cache(None)
    sizes = other_sizes(grid, sizes_held(grid, first_read(families), param_set))
    measured = {c: (root, mc) for root, mc, c in measured_cells(grid, first_read(families), param_set)}
    keys = {key for kind, key, *_ in layouts if kind == "uniform"}
    out, times, lines, rc = {}, {"load": 0.0, "pick_and_price": 0.0, "check": 0.0}, [], 0
    for view in views or LC.VIEWS:
        t = time.time()
        bill = "sized" if view == LC.BILL_VIEW else ""
        composer = compose.Composer(criterion=criterion, sizes=sizes, plan_rule=plan_rule, families=families,
                                    chunk_view=view, param_set=param_set, bill=bill,
                                    **({} if weights is None else {"weights": weights}))
        try:
            data = compose.load(grid, by_dir, dataset, composer=composer)
        except (SystemExit, KeyError) as e:      # no exact unit columns (a capture older than them): the view without
            if not bill:                         # the bill's lanes, said
                raise
            lines.append(f"{view} view: the bill's exact lanes left blank ({e})")
            composer = composer.but(bill="")
            data = compose.load(grid, by_dir, dataset, composer=composer)
        times["load"] += time.time() - t
        t = time.time()
        units = compose.grid_units(data)
        entries = entries_of(layouts, units, data.sized)
        tots, rows, per_query, per_part = cost_rows(
            w, data, entries, view, lambda layout: "measured" if layout in measured else "composed")
        times["pick_and_price"] += time.time() - t
        t = time.time()
        uni = {k: (e["where"]["props"], e["where"]["adjacency"], tots[k]) for k in keys for e in entries
               if e["layout"] == k}
        mine = [(measured[c][0], measured[c][1], c) for c in sorted(measured) if c in keys]
        compared, bad = check(w, families, uni, mine, plant and view == LC.VIEWS[0], composer.chunk_view, dataset,
                              param_set, OPTION_RULE.but(in_sample=plan_rule.in_sample, held_out=plan_rule.held_out))
        times["check"] += time.time() - t
        ok = not bad and (compared > 0 or not mine)
        lines.append(f"{view} view: {len(entries)} layouts, {compared} measured compared with their composition, "
                     f"{len(bad)} differ outside miss-key bytes: {'PASS' if ok else 'FAIL'}"
                     + (" [control: must FAIL]" if plant and view == LC.VIEWS[0] else ""))
        lines += [f"   DIFF {x}" for x in bad[:10]]
        rc = rc or (0 if ok else 1)
        out[view] = (rows, per_query, per_part)
    return w, out, LC.stage_times(**times), lines, rc
