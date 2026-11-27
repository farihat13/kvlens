#!/usr/bin/env python3
"""Why a part takes its chunk size in the mix that chunk_size_front.py solves: per part and byte limit, the setting the
exact mix gives it, what the part costs there, what the workload would cost with ONLY that part on each other setting,
and, per part and chunk size, how many owners a chunk read serves. Then two rules, stated here BEFORE the fit is read,
are scored against the solver's choice. Analysis only: opens no store.

  The rules (fixed before the first run):
    exchange  Start from Condensed/Condensed (the uniform layout with the fewest bytes). Repeat: among the changes of ONE
              part's setting that lower the workload's ops and keep its bytes within the limit, apply the one with the
              most ops saved per byte added (a change that adds no bytes first, the largest saving). Stop when none is
              left. The rule predicts the setting of every part at every limit.
    monotone  A part's chunk never shrinks when the byte limit grows (Split or Condensed < 4 KB < 16 KB < 64 KB).
  A rule "predicts" a (part, limit) cell when its setting equals the exact mix's. Above about 80% it is a usable rule;
  below, the choice is the solver's output and no simple rule.

  Per part and size ("intrinsic", from that size's Chunked/Chunked capture at the plan the all-Chunked layout of that
  size picks; I01's frequencies, reported params):
    owners per chunk read = rows into the steps that read the part / SEEKs on the part (one SEEK = one chunk read)
    bytes per owner       = the part's bytes read / those rows
    used share            = used bytes / bytes read, over the steps that read this part alone

    python3 experiments/layout_choice/chunk_size_choice_by_part.py --sf 3 --grid <nine cells' captures/<dataset>/<scale>> \\
        --size 4k --size 16k --front <dir of chunk_size_front.csv> [--workload i01] [--unit-set "per type"]
        -> <front dir>/part_settings.csv, part_sizes_intrinsic.csv, why_a_part_takes_its_size.md
"""
import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
from kvcost.layout_cost import compose
from kvcost.layout_cost import benchmarks
from kvcost.layout_cost.settings import size_options
from kvcost.layout_cost.mixes import unit_of_name
from kvcost.layout_cost.compose import add_pick_by_arg
from kvcost.layout_cost.mixes import unit_name
from kvevaluator.measure import grid_capture
from kvevaluator.output_tree import capture_in
from kvcost.layout_search.front import b_kind, b_of
from kvevaluator.capture import capture_layout

BS = ()               # the front's named limits B (1, 2, 3, 5; 0.5, 1, 2, 3, 5 before), read in main
from kvevaluator.capture.step_record import READ_BYTE_LANES  # (the lanes, one definition)
BYTE_LANES = READ_BYTE_LANES
RULE_EXCHANGE = ("Start from Condensed/Condensed. Repeat: among the changes of one part's setting that lower the workload's ops and "
                 "keep its bytes within the limit, apply the one with the most ops saved per byte added (a change that adds no "
                 "bytes first). Stop when none is left.")
RULE_MONOTONE = "A part's chunk never shrinks when the byte limit grows (Split or Condensed < 4 KB < 16 KB < 64 KB)."


class Coster:
    """the workload's (ops, bytes) under an assignment, each query priced once per assignment of its own parts"""

    def __init__(self, w, data):
        self.items = []
        for fam, q, often, params in w["items"]:
            d = data[(fam, q)]
            units = sorted({u for per in d["cost"].values() for u in per if u is not None}, key=str)
            self.items.append((d, compose.params_of(d, params), units, often))
        self.cache = {}

    def query(self, k, assign, by_unit=False):
        d, ps, units, often = self.items[k]
        key = (k, tuple(assign[u] for u in units))
        if key not in self.cache:
            tot, per = compose.query_cost(d, ps, assign, set(units), by_unit=True)
            self.cache[key] = (compose.currencies(tot), {u: compose.currencies(v) for u, v in per.items()})
        return self.cache[key] if by_unit else self.cache[key][0]

    def total(self, assign):
        o = b = 0.0
        for k, (_, _, _, often) in enumerate(self.items):
            c = self.query(k, assign)
            o += often * c["ops"]
            b += often * c["bytes"]
        return o, b

    def by_unit(self, assign):
        out = defaultdict(lambda: [0.0, 0.0])
        for k, (_, _, _, often) in enumerate(self.items):
            for u, c in self.query(k, assign, True)[1].items():
                out[u][0] += often * c["ops"]
                out[u][1] += often * c["bytes"]
        return out


def exchange_rule(coster, units, options, cap):
    """the rule's assignment within `cap` bytes, and the steps it took"""
    assign = {u: "condensed" for u in units}
    ops, byt = coster.total(assign)
    steps = 0
    while True:
        best = None
        for u in units:
            for o in options:
                if o == assign[u]:
                    continue
                o2, b2 = coster.total({**assign, u: o})
                if o2 >= ops * (1 - 1e-12) or b2 > cap * (1 + 1e-9):
                    continue
                key = (0, -(ops - o2)) if b2 <= byt else (1, -(ops - o2) / (b2 - byt))
                if best is None or key < best[0]:
                    best = (key, u, o, o2, b2)
        if best is None:
            return assign, ops, byt, steps
        _, u, o, ops, byt = best
        assign[u] = o
        steps += 1


def rows_of(path):
    out = defaultdict(list)
    with open(path) as f:
        for r in capture_layout.reader(f):
            out[(r["query"], r["body"], r["param_idx"])].append(r)
    return out


def intrinsic(w, data, units, oid, root, cell, by_dir):
    """{unit: sums} at one chunk size, from its Chunked/Chunked capture, at the all-Chunked layout's plan"""
    cap = capture_in(root, "interactive")
    parts, steps = rows_of(cap.path_of("parts", cell)), rows_of(cap.path_of("steps", cell))
    assign = {u: oid for u in units}
    tot = defaultdict(lambda: defaultdict(float))
    for fam, q, often, params in w["items"]:
        if fam != "interactive":
            continue
        d = data[(fam, q)]
        qunits = {u for per in d["cost"].values() for u in per if u is not None}
        ps = compose.params_of(d, params)
        best = compose.query_pick(d, ps, assign, qunits)[0]
        for i in ps:
            step = {int(r["step_idx"]): r for r in steps.get((q, best, str(i)), [])}
            units_of_step, seen, wgt = defaultdict(set), set(), often / len(ps)
            pr = parts.get((q, best, str(i)), [])
            for r in pr:
                u = compose.unit_of(r["part"], by_dir)
                if u is not None:
                    units_of_step[int(r["step_idx"])].add(u)
            for r in pr:
                u, k = compose.unit_of(r["part"], by_dir), int(r["step_idx"])
                if u is None:
                    continue
                t = tot[u]
                t["S"] += wgt * float(r["S"])
                byt = sum(float(r.get(c) or 0) for c in BYTE_LANES)
                t["bytes"] += wgt * byt
                if (k, u) not in seen and k in step:
                    seen.add((k, u))
                    t["owners"] += wgt * float(step[k]["rows_in"] or 0)
                    if len(units_of_step[k]) == 1:
                        t["used"] += wgt * (float(step[k].get("used_key") or 0) + float(step[k].get("used_value") or 0))
                        t["alone_bytes"] += wgt * sum(float(step[k].get(c) or 0) for c in BYTE_LANES)
    return tot


def label(setting, gsize):
    return {"chunked": f"Chunked {gsize[:-1]} KB", "split": "Split", "condensed": "Condensed"}.get(
        setting, f"Chunked {setting[len('chunked_c'):-1]} KB")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", default="3")
    ap.add_argument("--grid", required=True)
    ap.add_argument("--size", action="append", default=[], metavar="NAME", help="Chunked at another chunk size (4k), its Chunked/Chunked in --grid's place; repeatable")
    ap.add_argument("--front", required=True, help="the workload's dir of chunk_size_front.py (chunk_size_front.csv); the tables are written there")
    ap.add_argument("--workload", default="i01")
    ap.add_argument("--unit-set", default="per type", choices=("per type", "per type and direction"))
    add_pick_by_arg(ap)
    a = ap.parse_args()
    t0 = time.time()
    sizes = size_options(a.size, a.grid)
    composer = compose.Composer(criterion=a.criterion, sizes=sizes, plan_rule=compose.PlanRule())
    by_dir = a.unit_set == "per type and direction"
    gpack = json.load(open(os.path.join(grid_capture.GRID, "props_chunked_adj_chunked.json")))["adjacency"]["owners"]["pack"]
    gsize = f"{gpack // 1024}k"
    data = compose.load(a.grid, by_dir, composer=composer)
    (name, w), = benchmarks.select("ldbc", a.sf, a.workload).items()
    units = compose.workload_units(w, data)
    ladder = ["chunked_c%s" % s for s in sorted([x[0][len("chunked_c"):] for x in sizes] + [gsize], key=lambda s: int(s[:-1]))]
    opt = lambda s: "chunked" if s == f"chunked_c{gsize}" else s  # (an output name -> compose's option)
    options = ["split", "condensed"] + [opt(s) for s in ladder]
    rank = {"split": 0, "condensed": 0, **{opt(s): i + 1 for i, s in enumerate(ladder)}}
    coster = Coster(w, data)
    # the exact mix per limit, from the front
    with open(os.path.join(a.front, "chunk_size_front.csv")) as f:
        front = {r["cap_kind"]: r for r in capture_layout.reader(f) if r["line"] == "any size" and r["unit_set"] == a.unit_set}
    global BS
    BS = tuple(f"{b:g}" for b in sorted({b_of(k) for k in front} - {None}))
    kind = b_kind(front.values())          # reference_x (B x Sp-Co's bytes), or nebula_x in a front in Co-Sp units
    exact, caps = {}, {}
    for b in BS:
        r = front[f"{kind}{b}"]
        caps[b] = float(r["cap_bytes"])
        exact[b] = {unit_of_name(x.rsplit("=", 1)[0]): opt(x.rsplit("=", 1)[1]) for x in r["assignment"].split("; ")}
        o, by = coster.total(exact[b])
        assert abs(o - float(r["total_ops"])) <= 1e-6 * o and abs(by - float(r["total_bytes"])) <= 1e-6 * by, (b, o, r["total_ops"])
    # the intrinsic numbers per size
    roots = {f"chunked_c{gsize}": ("chunked", a.grid, "props_chunked_adj_chunked"), **{oid: (oid, root, cell) for oid, root, cell in sizes}}
    intr_rows, intr = [], {}
    for s in ladder:
        oid, root, cell = roots[s]
        t = intrinsic(w, data, units, oid, root, cell, by_dir)
        for u in units:
            v = t.get(u, {})
            if not v.get("S"):
                continue
            row = {"workload_id": w["id"], "part": unit_name(u), "chunk_size": label(opt(s), gsize), "owners_probed": round(v["owners"], 1),
                   "chunk_reads_S": round(v["S"], 1), "bytes_read": round(v["bytes"], 1),
                   "owners_per_chunk_read": round(v["owners"] / v["S"], 3), "bytes_per_owner": round(v["bytes"] / v["owners"], 1) if v["owners"] else "",
                   "used_share_steps_on_this_part_alone": round(v["used"] / v["alone_bytes"], 4) if v.get("alone_bytes") else ""}
            intr_rows.append(row)
            intr[(u, opt(s))] = row
    # the two rules
    rule = {b: exchange_rule(coster, units, options, caps[b]) for b in BS}
    rows, hits, cells, misses = [], 0, 0, []
    for b in BS:
        mix_ops, mix_bytes = coster.total(exact[b])
        per = coster.by_unit(exact[b])
        for u in units:
            chosen, pred = exact[b][u], rule[b][0][u]
            cells += 1
            hits += pred == chosen
            row = {"workload_id": w["id"], "unit_set": a.unit_set, "B": b, "part": unit_name(u), "setting": label(chosen, gsize),
                   "part_share_of_ops": round(per[u][0] / mix_ops, 4), "part_share_of_bytes": round(per[u][1] / mix_bytes, 4),
                   "exchange_rule_setting": label(pred, gsize), "exchange_rule_right": int(pred == chosen)}
            for o in options:                    # the workload with ONLY this part on another setting
                o2, b2 = coster.total({**exact[b], u: o})
                tag = label(o, gsize).lower().replace(" ", "_")
                row[f"ops_x_if_{tag}"] = round(o2 / mix_ops, 4)
                row[f"bytes_x_if_{tag}"] = round(b2 / mix_bytes, 4)
                row[f"fits_if_{tag}"] = int(b2 <= caps[b] * (1 + 1e-9))
            rows.append(row)
            if pred != chosen:
                misses.append((b, u, chosen, pred))
    shrink = [(u, b1, b2) for u in units for b1, b2 in zip(BS, BS[1:]) if rank[exact[b2][u]] < rank[exact[b1][u]]]
    pairs = len(units) * (len(BS) - 1)
    with open(os.path.join(a.front, "part_settings.csv"), "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]))
        wr.writeheader()
        wr.writerows(rows)
    with open(os.path.join(a.front, "part_sizes_intrinsic.csv"), "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(intr_rows[0]))
        wr.writeheader()
        wr.writerows(intr_rows)
    # the text
    out = [f"# Why a part takes its chunk size: {w['id'].upper()}, mix {a.unit_set}, SF{a.sf}", "",
           "> Generated by `experiments/layout_choice/chunk_size_choice_by_part.py`; working "
           "output. Ops = weighted ops of the whole workload; B = the byte limit over the front's B = 1 (Sp-Co's bytes; Co-Sp's in a front in Co-Sp units).", "",
           "## The two rules, written before the fit was read", "",
           f"- **exchange:** {RULE_EXCHANGE}", f"- **monotone:** {RULE_MONOTONE}", "",
           "## How well they predict the exact mix", "",
           "| rule | (part, limit) cells | predicted | share |", "|---|---|---|---|",
           f"| exchange | {cells} | {hits} | {100 * hits / cells:.0f}% |",
           f"| monotone | {pairs} (part, pair of neighbouring limits) | {pairs - len(shrink)} | {100 * (pairs - len(shrink)) / pairs:.0f}% |", "",
           "| B | exact mix: ops (M), bytes (GB) | exchange rule: ops (M), bytes (GB) | rule's ops over the exact mix's | cells the rule gets right |",
           "|---|---|---|---|---|"]
    for b in BS:
        eo, eb = coster.total(exact[b])
        _, ro, rb, _ = rule[b]
        right = sum(rule[b][0][u] == exact[b][u] for u in units)
        out.append(f"| {b} | {eo / 1e6:.1f}, {eb / 1e9:.1f} | {ro / 1e6:.1f}, {rb / 1e9:.1f} | {ro / eo:.3f}x | {right} of {len(units)} |")
    out += ["", "Cells the exchange rule misses (limit, part: the exact mix's setting, the rule's):", ""]
    out += [f"- B = {b}, {unit_name(u)}: {label(c, gsize)}, rule {label(p, gsize)}" for b, u, c, p in misses] or ["- none"]
    out += ["", "Parts whose chunk shrinks as the limit grows: " + ("; ".join(f"{unit_name(u)} from B = {b1} ({label(exact[b1][u], gsize)}) to "
            f"B = {b2} ({label(exact[b2][u], gsize)})" for u, b1, b2 in shrink) or "none") + ".", "",
            "## Per part: the setting at each limit, and what a chunk read serves at each size", "",
            "Owners per chunk read / bytes per owner, at the plan of the all-Chunked layout of that size. A part with no number is not read on "
            "a Chunked setting by this workload's reads (writes only, or never read).", "",
            "| part | " + " | ".join(f"B = {b}" for b in BS) + " | " + " | ".join(f"{label(opt(s), gsize)[8:]}: owners per chunk read, bytes per owner" for s in ladder)
            + " | share of ops, of bytes at B = 1 |", "|---|" + "---|" * len(BS) + "---|" * len(ladder) + "---|"]
    per1, (o1, b1_) = coster.by_unit(exact["1"]), coster.total(exact["1"])
    for u in sorted(units, key=lambda u: -per1[u][0]):
        cellsz = []
        for s in ladder:
            r = intr.get((u, opt(s)))
            cellsz.append(f"{r['owners_per_chunk_read']:.2f}, {float(r['bytes_per_owner']):,.0f} B" if r and r["bytes_per_owner"] != "" else "")
        out.append(f"| {unit_name(u)} | " + " | ".join(label(exact[b][u], gsize) for b in BS) + " | " + " | ".join(cellsz)
                   + f" | {100 * per1[u][0] / o1:.1f}%, {100 * per1[u][1] / b1_:.1f}% |")
    out += ["", "`part_settings.csv` has, per part and limit, the workload's ops and bytes (as multiples of the exact mix's) with only that part "
            "on each other setting, and whether that still fits the limit: the trade the solver made.", ""]
    with open(os.path.join(a.front, "why_a_part_takes_its_size.md"), "w") as f:
        f.write("\n".join(out))
    print(f"{name}: exchange rule {hits} of {cells} cells ({100 * hits / cells:.0f}%); monotone {pairs - len(shrink)} of {pairs}; "
          f"{len(coster.cache):,} query pricings, {time.time() - t0:.0f} s -> {a.front}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
