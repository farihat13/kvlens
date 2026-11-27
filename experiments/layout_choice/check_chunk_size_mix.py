#!/usr/bin/env python3
"""A BUILT mixed layout whose parts take different chunk sizes, against its composition (compose.Composer.sizes): the
check of chunk_size_front.py's pricing on a real store. Analysis only: opens no store.

  spec   --front <chunk_size_front.csv> --unit-set "per type" --cap-kind reference_x1 --out <spec.json>
         the "any size" mix of that row as the meta spec that builds it (mixes.spec_of: a pack per part)
  check  --sf SF --grid <the nine cells' captures/<dataset>/<scale> dir> --size 4k ... --mix-root
         <the captures/<dataset>/<scale> dir holding the mix> --spec <its tracked spec> [--plant]
         1. picks: per LDBC read query, the built store's pick (picks.choose_cell on its own capture) == the
            composed option's (compose.query_pick)
         2. rows: every (query, param, step, store part) row of the picked plan on the built store == the row of the
            uniform capture that part's setting names (reported and held-out params, and the writes); op counts, byte
            lanes outside miss keys, and miss-key bytes are counted apart (a missed probe lands on a foreign key, whose
            bytes belong to a neighbour part)
         3. totals: per workload, the built store's measured cost == the composed cost, per measure outside miss keys
         exit 1 on any difference outside miss-key bytes, or when 0 rows were compared.
         --plant: the control, one SEEK added to one measured row: must exit 1.
"""
import argparse
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
from kvcost.layout_cost import mixes
from kvevaluator.layouts import cell_id
from kvcost.layout_cost import compose
from kvcost.layout_cost import benchmarks
from kvcost.layout_cost.compose import add_pick_by_arg, load_reads, load_writes, workload_cost
from kvevaluator.measure import grid_capture
from kvevaluator.output_tree import HELD_OUT, capture_in, writes_in
from kvevaluator import workloads
from kvcost.layout_cost import picks as pick_rule
from kvcost.layout_cost.settings import size_options
from kvevaluator.capture import capture_layout

from kvevaluator.capture.step_record import (HIT_BYTE_LANES, MISS_BYTE_LANES, OPS, WRITE_BYTE_LANES)  # (the lanes, one definition)
COUNTS = OPS
HIT = (*HIT_BYTE_LANES, *WRITE_BYTE_LANES)
MISS = MISS_BYTE_LANES
MISS_MEASURES = ("miss_key_bytes", "write_rmw_miss_key_bytes")
HIT_OF = {"read_bytes": "miss_key_bytes", "write_rmw_bytes": "write_rmw_miss_key_bytes"}   # a total -> the miss bytes in it




def write_spec(a):
    grid_pack = json.load(open(os.path.join(grid_capture.GRID, "props_chunked_adj_chunked.json")))["adjacency"]["owners"]["pack"]
    with open(a.front) as f:
        rows = [r for r in capture_layout.reader(f) if r["line"] == "any size" and r["unit_set"] == a.unit_set and r["cap_kind"] == a.cap_kind]
    if len(rows) != 1:
        raise SystemExit(f"{a.front}: {len(rows)} rows for {a.unit_set} / {a.cap_kind}, expected 1")
    assign = {}
    for item in rows[0]["assignment"].split("; "):
        name, setting = item.rsplit("=", 1)
        assign[mixes.unit_of_name(name)] = "chunked" if mixes.sized_pack(setting) == grid_pack else setting
    spec, base = mixes.spec_of(assign)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(spec, f, indent=2, sort_keys=True)
        f.write("\n")
    count = defaultdict(int)
    for l in assign.values():
        count[l] += 1
    print(f"{a.out}: base {base}; {len(assign)} parts: " + ", ".join(f"{k} {v}" for k, v in sorted(count.items()))
          + f"; ops {float(rows[0]['total_ops']):,.0f}, bytes {float(rows[0]['total_bytes']):,.0f} (composed, {rows[0]['workload']})")
    return 0


def part_rows(path, write):
    """{(query, plan, param): {(step_idx, step, part): {channel: int}}}"""
    out = defaultdict(dict)
    rows = capture_layout.write_rows(path)[2] if write else capture_layout.read_rows(path)   # a writes file: either shape
    for r in rows:
        vec = {c: int(float(r.get(c) or 0)) for c in COUNTS + HIT + MISS}
        out[(r["query"], "write" if write else r["body"], r["param_idx"])][(int(r["step_idx"]), r["step"], r["part"])] = vec
    return out


def parts_file(root, fam, cell, held=False):
    if benchmarks.FAMILIES["ldbc"][fam] == "read":
        return capture_in(root, fam, HELD_OUT if held else None).path_of("parts", cell)
    return writes_in(root, "ldbc").path_of("parts", cell)


def check(a):
    sizes = size_options(a.size, a.grid)     # each size's ChK-ChK in the grid's place (one parser)
    rule = pick_rule.PlanRule(in_sample=True if a.in_sample else None)
    composer = compose.Composer(criterion=a.criterion, sizes=sizes, plan_rule=rule)
    spec = json.load(open(a.spec))
    name = os.path.basename(a.spec)[:-5]
    mix_reads = capture_in(a.mix_root, workloads.names(kind="read", dataset="ldbc")[0])
    rec = json.load(open(mix_reads.record(name)))
    if rec["spec"] != spec:
        raise SystemExit(f"{name}: the capture's run record was measured with another spec than {a.spec}")
    by_dir = "adjacency_by_etype_dir" in spec
    data = compose.load(a.grid, by_dir=by_dir, composer=composer)
    settings = mixes.grid_settings([oid for oid, _, _ in sizes])   # + Chunked at each other size, as a component of its own
    held_units = {key: {u for per in d.get("cost_h", {}).values() for u in per if u is not None} for key, d in data.items()}
    every = sorted(set(compose.grid_units(data)) | set().union(*held_units.values()), key=str)
    assign = mixes.assignment(spec, every, settings, name)
    used = defaultdict(int)
    for l in assign.values():
        used[l] += 1
    print(f"{name}: {len(assign)} units: " + ", ".join(f"{k} {v}" for k, v in sorted(used.items())))
    root_of = {oid: (root, cell) for oid, root, cell in compose.chunk_sizes(a.grid, composer)}   # 64 KB's too
    bad, planted = [], False
    built = pick_rule.choose_cell(mix_reads.path, name, a.criterion, rule=composer.plan_rule, spec_path=a.spec)
    picks = pick_diff = 0
    stat = defaultdict(lambda: defaultdict(int))              # (family, params) -> counter
    for fam in sorted(benchmarks.FAMILIES["ldbc"]):
        if not os.path.exists(parts_file(a.mix_root, fam, name)):
            continue
        write = benchmarks.FAMILIES["ldbc"][fam] == "write"
        mine = {"reported": part_rows(parts_file(a.mix_root, fam, name), write)}
        if not write and os.path.exists(parts_file(a.mix_root, fam, name, held=True)):
            mine["held-out"] = part_rows(parts_file(a.mix_root, fam, name, held=True), write)
        cache = {}

        def source(src_root, cell, held):
            k = (src_root, cell, held)
            if k not in cache:
                cache[k] = part_rows(parts_file(src_root, fam, cell, held), write)
            return cache[k]

        for (f2, q), d in sorted((k, v) for k, v in data.items() if k[0] == fam):
            units = {u for per in d["cost"].values() for u in per if u is not None}
            best, _, _ = compose.query_pick(d, compose.params_of(d, None), assign, units)
            if best is None:
                bad.append(f"{fam} {q}: no usable plan composed")
                continue
            if not write:
                picks += 1
                if built.get(q, {}).get("body") != best:
                    pick_diff += 1
                    bad.append(f"{fam} {q}: the built store's pick {built.get(q, {}).get('body')!r}, the composed {best!r}")
                    continue
            playouts = {compose.validity_layout(assign[u], d["composer"]) for u in units if u[0] == "props"} or set(compose.LAYOUTS)
            alayouts = {compose.validity_layout(assign[u], d["composer"]) for u in units if u[0] == "adj"} or set(compose.LAYOUTS)

            def where(u):                      # compose._composed's rule: the capture a unit's rows are read from
                if u is None:
                    return a.grid, cell_id(min(playouts), min(alayouts))
                if compose.is_sized(assign[u], d["composer"]):
                    return root_of[assign[u]]
                if u[0] == "props":
                    return a.grid, cell_id(assign[u], min(alayouts))
                return a.grid, cell_id(min(playouts), assign[u])

            for pk, rows in mine.items():
                held = pk == "held-out"
                for (qq, plan, i), got in sorted(rows.items()):
                    if qq != q or plan != best:
                        continue
                    want = {}
                    parts_here = {s[2] for s in got}
                    for u in {compose.unit_of(p, by_dir) for p in parts_here} | (held_units[(f2, q)] if held else units) | {None}:
                        src = source(*where(u), held).get((q, best, i), {})
                        want.update({s: v for s, v in src.items() if compose.unit_of(s[2], by_dir) == u})
                    for s in sorted(set(got) | set(want)):
                        g, w_ = got.get(s), want.get(s)
                        c = stat[(fam, pk)]
                        c["rows"] += 1
                        if g is not None and a.plant and not planted:
                            g = dict(g, S=g["S"] + 1)
                            planted = True
                            print(f"[control] one SEEK added to the measured row {fam} {q} {best} param {i} {s}")
                        if g is None or w_ is None:
                            c["one side only"] += 1
                            bad.append(f"{fam} {q} {best} param {i} {s}: a row on {'the composition' if g is None else 'the built store'} only")
                            continue
                        ops_eq = all(g[x] == w_[x] for x in COUNTS)
                        hit_eq = all(g[x] == w_[x] for x in HIT)
                        c["equal in op counts"] += ops_eq
                        c["equal in bytes outside miss keys"] += hit_eq
                        c["differ in miss-key bytes"] += any(g[x] != w_[x] for x in MISS)
                        if not (ops_eq and hit_eq):
                            bad.append(f"{fam} {q} {best} param {i} {s}: measured {g} vs composed {w_}")
    total = 0
    for (fam, pk), c in sorted(stat.items()):
        total += c["rows"]
        print(f"   rows {fam} {pk}: {c['rows']:,} compared, {c['equal in op counts']:,} equal in every op count, "
              f"{c['equal in bytes outside miss keys']:,} equal in every byte lane outside miss keys, {c['differ in miss-key bytes']:,} "
              f"differ in miss-key bytes, {c['one side only']:,} on one side only")
    print(f"   picks: {picks} LDBC read queries, {pick_diff} differ")
    measured = {"interactive": load_reads(mix_reads.path, name, a.criterion, rule=data.composer.plan_rule), "microbench": {}, "writes": load_writes(a.mix_root, name, data.composer.chunk_view)}
    cells = 0
    for wname, w in benchmarks.select("ldbc", a.sf, a.workload).items():
        if any(fam == "microbench" for fam, _, _, _ in w["items"]):
            continue                            # the other sizes have no microbench capture
        pred = compose.cost_mix(w, data, mixes.assignment(spec, compose.workload_units(w, data), settings, name))
        meas = workload_cost(w, measured)
        cur_p = compose.currencies(pred)
        cells += 1
        # a byte total that holds its miss-key bytes (compose.py: read_bytes and write_rmw_bytes count every lane the store
        # returned) is compared without them, as the rows are
        hit = lambda v, m: v[m] - v.get(HIT_OF.get(m, ""), 0.0)
        diffs = [m for m in compose.MEASURES if m not in MISS_MEASURES and abs(hit(pred, m) - hit(meas, m)) > 1e-9 * max(1.0, abs(meas[m]))]
        miss = sum(pred[m] - meas[m] for m in MISS_MEASURES if m in pred and m in meas)
        m_ops = meas["read_ops"] + meas["write_rmw_ops"] + meas["write_ops"]
        m_bytes = meas["read_bytes"] + meas["write_rmw_bytes"] + meas["write_bytes"]
        print(f"   {w['id']} {wname}: measured ops {m_ops:,.3f} bytes {m_bytes:,.0f} | composed ops {cur_p['ops']:,.3f} bytes {cur_p['bytes']:,.0f}"
              f" | measures that differ outside miss keys: {diffs or 'none'}; miss-key bytes composed - measured: {miss:+,.0f}")
        for m in diffs:
            bad.append(f"{wname}: {m} measured {meas[m]} vs composed {pred[m]}")
    for x in bad[:10]:
        print("   DIFF", x)
    ok = not bad and total > 0 and cells > 0
    print(f"composed against measured, {name} (sf{a.sf}): {total:,} rows, {len(bad)} findings, {cells} workloads: {'PASS' if ok else 'FAIL'}"
          + (" [control: must FAIL]" if a.plant else ""))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("mode", choices=("spec", "check"))
    ap.add_argument("--front")
    ap.add_argument("--unit-set", default="per type")
    ap.add_argument("--cap-kind", default="reference_x1", help="the row's cap_kind: reference_x<B> = B x Sp-Co's bytes (nebula_x<B>, "
                    "B x Co-Sp's, in a front in Co-Sp units)")
    ap.add_argument("--out")
    ap.add_argument("--sf", default="3")
    ap.add_argument("--grid")
    ap.add_argument("--size", action="append", default=[], metavar="NAME", help="Chunked at another chunk size (4k), its Chunked/Chunked in --grid's place; repeatable")
    ap.add_argument("--mix-root")
    ap.add_argument("--spec")
    ap.add_argument("--plant", action="store_true")
    ap.add_argument("--in-sample", action="store_true", help="below the deciding scale: a mix with no row in the pick record is priced "
                    "by its own capture's held-out rule")
    benchmarks.add_workload_arg(ap)
    add_pick_by_arg(ap)
    a = ap.parse_args()
    return write_spec(a) if a.mode == "spec" else check(a)


if __name__ == "__main__":
    sys.exit(main())
