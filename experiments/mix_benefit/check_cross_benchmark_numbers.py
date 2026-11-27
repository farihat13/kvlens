#!/usr/bin/env python3
"""The cross-benchmark results doc's inputs, tables and numbers, re-derived with no code shared with the pipeline.

Part 0 (the loader): each family's per-query cost, recomputed from the captures themselves -- I01's breakdown.csv (its
parts summed), BI's capture at bi_picks.csv's pick, LinkBench's reads (the planner's body) and writes -- against the
currencies rebuilt from costs.csv's raw channels and against its weight (IMDb / JOB: its capture at job_picks.csv's pick, as BI); costs.csv's own currency columns (what the
tables read) against that rebuild; the query sets against the expected ones; LDBC's body column against pick_margins.csv.
Part 1 (the tables): every ratio of cross_benchmark_frequencies.csv and cross_benchmark_regret.csv. wops, bytes and
wops_bytes4k are rebuilt per row from costs.csv's raw channels (S G N P D, the read lanes, write_bytes) and summed,
geomeaned or averaged over Sp-Co (additive) here, not through python/kvcost/layout_cost/query_weighting.py. A best layout within TIE of another is printed as a tie.
Part 2 (the doc): each number the doc states (NUMBERS), recomputed from part 1's totals, and found in the doc as a token.
--perturb FAMILY,QUERY,CELL,CHANNEL,FACTOR multiplies one raw channel of costs.csv first: the negative control.

    python3 experiments/mix_benefit/check_cross_benchmark_numbers.py --root <analysis root> --ldbc <breakdown.csv>
        --bi <bi_picks.csv> --bi-capture <its capture> --linkbench <captures/linkbench/<scale> dir>
        [--job <job_picks.csv> --job-capture <its capture>] [--doc <doc.md>] [--pending FAMILY]
Not recomputed here: the random mixes (their draws are read from random_mix_robustness.csv). Reads CSVs only; about 11 s."""
import argparse
import csv
import math
import os
import re
import sys
from collections import defaultdict
from kvevaluator.paths import ROOT
from kvevaluator.layouts import SETTINGS, cell_id
from kvevaluator.output_tree import Capture, capture_in, writes_in
from kvevaluator.capture import capture_layout
from kvcost.layout_cost import benchmarks, compose, cost_models as CM

MODELS = ("wops", "bytes", "wops_bytes4k")
from kvevaluator.capture.step_record import READ_BYTE_LANES  # (the lanes, one definition)
LANES = READ_BYTE_LANES
CELLS = [cell_id(p, a) for p in SETTINGS for a in SETTINGS]
TIE = 1.01                     # a layout within 1% of the best is named beside it (per_query_disagreement's tie rule)
# the tables are written at 4 decimals (ratios); costs.csv at 6 (each query): the recompute must agree to that
ABS_TOL, REL_TOL = 1e-4, 1e-5

# the expected query sets: a query lost on every layout must fail, not re-normalise the weights silently
I01 = [f"ic{i}" for i in range(1, 13)] + [f"is{i}" for i in range(1, 8)] + [f"iu{i}" for i in range(1, 9)]
BI = [f"bi{i}" for i in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 18)]     # bi15, bi19, bi20: no plan
LINKBENCH = ["addlink_link1", "addlink_link2", "addnode", "countlink_link1", "countlink_link2", "deletelink_link1",
             "deletelink_link2", "deletenode", "getlink_link1", "getlink_link2", "getlinklist_link1", "getlinklist_link2",
             "getnode", "updatelink_link1", "updatelink_link2", "updatenode"]
JOB_PLANS = os.path.join(str(ROOT), "python", "kvevaluator", "workloads", "job", "plans")
JOB = sorted(f[:-len(".json")] for f in os.listdir(JOB_PLANS) if f.endswith(".json"))      # one plan file per query
assert len(JOB) == 113, f"{JOB_PLANS}: {len(JOB)} plan files, JOB has 113 queries"
EXPECTED = {"LDBC, all of I01": I01, "LDBC IC": I01[:12], "LDBC IS": I01[12:19], "LDBC IU": I01[19:], "BI": BI,
            "IMDb / JOB": JOB, "LinkBench": LINKBENCH}
LB_WRITE = ("add", "update", "delete")      # a LinkBench request type is a write by its name

# (benchmark, frequencies) of cross_benchmark_frequencies.csv -> (family, how a query counts, how the queries add up)
FREQ_COLUMNS = {
    ("LDBC Interactive", "LDBC frequencies"): ("LDBC, all of I01", "file", "sum"),
    ("LDBC Interactive", "once each"): ("LDBC, all of I01", "one", "sum"),
    ("LDBC BI", "equal contribution"): ("BI", "one", "geomean"),
    ("LDBC BI", "once each"): ("BI", "one", "sum"),
    ("IMDb / JOB", "equal contribution"): ("IMDb / JOB", "one", "geomean"),
    ("IMDb / JOB", "once each"): ("IMDb / JOB", "one", "sum"),
    ("LinkBench", "its log's frequencies"): ("LinkBench", "file", "sum"),
    ("LinkBench", "once each"): ("LinkBench", "one", "sum"),
}
# benchmark column of cross_benchmark_regret.csv -> (families, how a query counts, how the queries add up)
REGRET_COLUMNS = {
    "I01 LDBC mix": (("LDBC, all of I01",), "file", "sum"),
    "I02 LDBC reads": (("LDBC IC", "LDBC IS"), "file", "sum"),
    "BI": (("BI",), "one", "sum"),
    "BI, equal contribution": (("BI",), "one", "geomean"),
    "BI, additive (Sp-Co reference)": (("BI",), "one", "additive"),
    "IMDb / JOB": (("IMDb / JOB",), "one", "sum"),
    "IMDb / JOB, equal contribution": (("IMDb / JOB",), "one", "geomean"),
    "IMDb / JOB, additive (Sp-Co reference)": (("IMDb / JOB",), "one", "additive"),
    "LinkBench L01 (the log's mix)": (("LinkBench",), "file", "sum"),
    "LinkBench L02 (reads)": (("LinkBench",), "reads", "sum"),
    "LinkBench L03 (writes)": (("LinkBench",), "writes", "sum"),
    "LinkBench L04 (3x writes)": (("LinkBench",), "3x writes", "sum"),
}

# Part 2: (what the doc says, a function of (T, a), the doc's number as written). T = {(column, model): {cell: total}} from
# part 1; a = the arguments (the root, for the files the tables do not hold). Written the way the doc writes it.
CO_CH, CH_CH, CO_CO, CH_CO, SP_CH, CO_SP = ("props_condensed_adj_chunked", "props_chunked_adj_chunked", "props_condensed_adj_condensed",
                                            "props_chunked_adj_condensed", "props_split_adj_chunked", "props_condensed_adj_split")
SP_CO = "props_split_adj_condensed"     # the additive form's reference layout
HEADLINE = ("I01 LDBC mix", "BI, equal contribution", "IMDb / JOB, equal contribution", "LinkBench L01 (the log's mix)")  # one per benchmark
W16_COL, LB_COL = HEADLINE[0], HEADLINE[3]
# BI's and JOB's columns under each way of counting their queries (the same form for both)
FORMS = {"geomean": ("BI, equal contribution", "IMDb / JOB, equal contribution"), "once": ("BI", "IMDb / JOB"),
         "additive": ("BI, additive (Sp-Co reference)", "IMDb / JOB, additive (Sp-Co reference)")}
W4K = "wops_bytes4k"


def ratio(T, col, m, cell):
    t = T[(col, m)]
    return t[cell] / min(t.values())


def x(v):
    return f"{v:.2f}x"


def worst(T, m, cell):
    return max(ratio(T, b, m, cell) for b in HEADLINE)


def named_kind(rows):
    """(the named limits' cap_kind prefix, B = 1 in bytes) of a front file, read here (no shared code): reference_x<B>
    is B x Sp-Co's bytes; nebula_x<B>, in a file in Co-Sp units, B x Co-Sp's (B = 0.5 named only there)"""
    kinds = {k for r in rows for k in ("reference_x", "nebula_x") if r["cap_kind"].startswith(k)}
    if len(kinds) != 1:
        raise SystemExit(f"a front with named-limit kinds {sorted(kinds)} (want one)")
    kind = kinds.pop()
    return kind, next(float(r["cap_bytes"]) for r in rows if r["cap_kind"] == f"{kind}1")


def at_b(a, wdir, b, option_set="per type and direction"):
    """the best uniform that fits / the mix at the named limit B (in the file's own unit)"""
    rows = list(capture_layout.reader(open(os.path.join(a.root, "equal-bytes-front", wdir, "equal_bytes_front.csv"))))
    kind, _ = named_kind(rows)
    single = {r["cap_kind"]: float(r["total_ops"]) for r in rows if r["option_set"] == "single"}
    mix = next(r for r in rows if r["option_set"] == option_set and r["option"] == "fewest_ops_within_cap" and r["cap_kind"] == f"{kind}{b:g}")
    return single[mix["cap_kind"]] / float(mix["total_ops"])


def front(a, wdir, option_set="per type and direction"):
    """(B = 0.5, B = 1, peak, B at the peak) of an option set's line (default per type and direction): the best uniform
    that fits / the mix; B in the file's own unit (B = 0.5 is None in a file)"""
    rows = list(capture_layout.reader(open(os.path.join(a.root, "equal-bytes-front", wdir, "equal_bytes_front.csv"))))
    kind, ref = named_kind(rows)
    single = {r["cap_kind"]: float(r["total_ops"]) for r in rows if r["option_set"] == "single"}
    mix = [r for r in rows if r["option_set"] == option_set and r["option"] == "fewest_ops_within_cap"]
    at = {r["cap_kind"]: single[r["cap_kind"]] / float(r["total_ops"]) for r in mix if r["cap_kind"] in single}
    peak = max(mix, key=lambda r: float(r["ops_single_over_mix"] or 0))
    return at.get(f"{kind}0.5"), at[f"{kind}1"], float(peak["ops_single_over_mix"]), float(peak["cap_bytes"]) / ref


BI_ADD, JOB_ADD = FORMS["additive"]
SHORT = {"split": "Sp", "condensed": "Co", "chunked": "Ch"}


def lab(c):
    p, adj = c[len("props_"):].split("_adj_")
    return f"{SHORT[p]}-{SHORT[adj]}"


def smallest_worst(T, m, form):
    """the smallest worst case over I01, BI and JOB counted by form, and LinkBench: 'R.RRx (layout)'"""
    cols = (W16_COL, *FORMS[form], LB_COL)
    best = min(CELLS, key=lambda c: (max(ratio(T, b, m, c) for b in cols), c))
    return f"{x(max(ratio(T, b, m, best) for b in cols))} ({lab(best)})"


def form_row(T, name, form):
    """one row of section 1's table: the BI and JOB form, then the smallest worst case under each model"""
    return f"| {name} | " + " | ".join(smallest_worst(T, m, form) for m in (W4K, "bytes", "wops")) + " |"


def form_cell(T, col, m):
    """one cell of section 3's table: 'best; worst R.RRx' (ties by name)"""
    t = T[(col, m)]
    best, worst_ = min(CELLS, key=lambda c: (t[c], c)), max(CELLS, key=lambda c: (t[c], c))
    return f"{lab(best)}; {lab(worst_)} {t[worst_] / t[best]:.2f}x"


def form_table_row(T, bench, m, name):
    """one row of section 3's table: a benchmark and a model, then once each / additive / geomean"""
    cols = {"BI": FORMS, "IMDb / JOB": FORMS}[bench]
    i = 0 if bench == "BI" else 1
    return f"| {bench} | {name} | " + " | ".join(form_cell(T, cols[f][i], m) for f in ("once", "additive", "geomean")) + " |"


def per_query_row(a, fam, name, form):
    """one row of section 2's table (ops + bytes / 4 KB), from costs.csv's raw channels: the benchmark's best uniform layout
    (form: its frequencies' sum, or the geomean over queries), the queries it is cheapest on (within TIE), the queries
    over 1.5x their own best (the costliest named), their share of the cost on that layout (geomean: each query once),
    and the gain of a layout per query (total over total; geomean: the geomean of the ratios)"""
    cost, weight, _ = raw_costs(os.path.join(a.root, "per-query-disagreement", "costs.csv"), a.perturb_t)
    Q = {q: {c: v[W4K] for c, v in cs.items()} for q, cs in cost[fam].items()}
    w = {q: 1.0 if form == "geomean" else weight[fam][q] for q in Q}
    if form == "geomean":
        score = {c: math.fsum(math.log(Q[q][c]) for q in Q) for c in CELLS}
    else:
        score = {c: math.fsum(w[q] * Q[q][c] for q in Q) for c in CELLS}
    best = min(CELLS, key=lambda c: (score[c], c))
    r = {q: Q[q][best] / min(Q[q].values()) for q in Q}
    over = sorted((q for q in Q if r[q] > 1.5), key=lambda q: (-r[q], q))
    share = math.fsum(w[q] * Q[q][best] for q in over) / math.fsum(w[q] * Q[q][best] for q in Q)
    if form == "geomean":
        gain = math.exp(math.fsum(math.log(r[q]) for q in Q) / len(Q))
    else:
        gain = math.fsum(w[q] * Q[q][best] for q in Q) / math.fsum(w[q] * min(Q[q].values()) for q in Q)
    cheapest = sum(1 for q in Q if Q[q][best] <= min(Q[q].values()) * TIE)
    top = f" (up to {r[over[0]]:.2f}x, {over[0]})" if over else ""
    return f"| {name} | {lab(best)} | {cheapest} of {len(Q)} | {len(over)}{top} | {100 * share:.1f}% | {gain:.2f}x less |"


def random_mix(a, cell, model=W4K, shares_on="props_split_adj_condensed", bench="BI", mixes="Dir(1) x 1000"):
    """(wins, median, worst) of a layout over a benchmark's Dir(1) x 1000 random mixes"""
    r = next(r for r in capture_layout.reader(open(os.path.join(a.root, "random-mix", "random_mix_robustness.csv")))
             if r["benchmark"] == bench and r["mixes"] == mixes and r["shares_on"] == shares_on
             and r["model"] == model and r["cell"] == cell)
    return int(r["wins"]), float(r["ratio_median"]), float(r["ratio_max"])


def random_mix_text(a, cell, bench="BI"):
    w, med, mx = random_mix(a, cell, bench=bench)
    return f"{lab(cell)} is the best on {w:,} of 1,000 (median {med:.2f}x, worst {mx:.2f}x)"


def one_mix(a, layout):
    rows = capture_layout.reader(open(os.path.join(a.root, "one-mix-per-dataset", "one_mix_layouts.csv")))
    return next(float(r["max_r_w"]) for r in rows if r["option_set"] == "per type and direction" and r["workloads"].startswith("18")
                and r["layout"] == layout)


NUMBERS = [
    ("1: the smallest worst case over the four benchmarks, ops + bytes / 4 KB", lambda T, a: x(min(worst(T, W4K, c) for c in CELLS)), "5.25x"),
    ("1: Co-Ch (I01's best) on LinkBench", lambda T, a: x(ratio(T, LB_COL, W4K, CO_CH)), "14.47x"),
    ("1: Ch-Ch (BI's and JOB's best) on LinkBench", lambda T, a: x(ratio(T, LB_COL, W4K, CH_CH)), "17.58x"),
    ("1: JOB's best, ops + bytes / 4 KB (geomean and once each)", lambda T, a: "/".join(sorted({lab(min(CELLS, key=lambda c: (T[(col, W4K)][c], c))) for col in FORMS["geomean"][1:] + FORMS["once"][1:]})), "Ch-Ch"),
    ("1: Co-Co (LinkBench's best) on BI", lambda T, a: x(ratio(T, HEADLINE[1], W4K, CO_CO)), "5.48x"),
    ("1: Ch-Ch's worst case under wops", lambda T, a: x(worst(T, "wops", CH_CH)), "1.04x"),
    ("1: Sp-Co's worst case under bytes", lambda T, a: x(worst(T, "bytes", SP_CO)), "1.02x"),
    ("1: Co-Co's worst case under bytes, set by JOB", lambda T, a: x(worst(T, "bytes", CO_CO)), "1.37x"),
    ("1: I01 wops, Co-Co over Ch-Ch", lambda T, a: x(ratio(T, HEADLINE[0], "wops", CO_CO) / ratio(T, HEADLINE[0], "wops", CH_CH)), "13.72x"),
    ("1: LinkBench bytes, Co-Ch over its best", lambda T, a: x(ratio(T, LB_COL, "bytes", CO_CH)), "78.49x"),
    ("1: LinkBench wops, Co-Co over Co-Ch (the tie)", lambda T, a: f"{100 * (T[(LB_COL, 'wops')][CO_CO] / T[(LB_COL, 'wops')][CO_CH] - 1):.3f}%", "0.004%"),
    # B x Sp-Co's bytes (Co-Sp's before, when these read 3.45x, 1.96x at B = 0.5, 7.58x at B = 1.66)
    ("1: I01 at B = 1", lambda T, a: x(front(a, "i01-rw-ldbc-mix")[1]), "1.34x"),
    ("1: I01 at B = 2", lambda T, a: x(at_b(a, "i01-rw-ldbc-mix", 2)), "1.87x"),
    ("1: I01 at B = 5", lambda T, a: x(at_b(a, "i01-rw-ldbc-mix", 5)), "5.03x"),
    ("1: I01's peak", lambda T, a: x(front(a, "i01-rw-ldbc-mix")[2]), "7.58x"),
    ("1: I01's peak at B", lambda T, a: f"B = {front(a, 'i01-rw-ldbc-mix')[3]:.2f}", "B = 6.88"),
    ("1: I02 at B = 1", lambda T, a: x(front(a, "i02-r-ldbc-mix-reads")[1]), "1.09x"),
    ("1: I02's peak", lambda T, a: x(front(a, "i02-r-ldbc-mix-reads")[2]), "7.68x"),
    ("1: the one mix's worst case", lambda T, a: x(one_mix(a, "one mix, worst case")), "3.66x"),
    ("title, 1: Co-Co's worst case over the one mix's", lambda T, a: x(one_mix(a, "Condensed/Condensed") / one_mix(a, "one mix, worst case")), "3.44x"),
    ("1: Co-Co's worst case over the 18", lambda T, a: x(one_mix(a, "Condensed/Condensed")), "12.58x"),
    ("3: BI once each, wops, Sp-Sp", lambda T, a: x(ratio(T, "BI", "wops", "props_split_adj_split")), "103.01x"),
    ("3: BI geomean, wops, Sp-Sp", lambda T, a: x(ratio(T, HEADLINE[1], "wops", "props_split_adj_split")), "66.21x"),
    ("3: BI once each, bytes, Sp-Ch", lambda T, a: x(ratio(T, "BI", "bytes", SP_CH)), "2.01x"),
    ("3: BI geomean, bytes, Sp-Ch", lambda T, a: x(ratio(T, HEADLINE[1], "bytes", SP_CH)), "4.90x"),
    ("3: BI once each, ops + bytes / 4 KB, Sp-Sp", lambda T, a: x(ratio(T, "BI", W4K, "props_split_adj_split")), "14.41x"),
    ("3: BI geomean, ops + bytes / 4 KB, Sp-Sp", lambda T, a: x(ratio(T, HEADLINE[1], W4K, "props_split_adj_split")), "7.02x"),
    ("3: BI geomean, ops + bytes / 4 KB, Co-Ch over Ch-Ch", lambda T, a: x(ratio(T, HEADLINE[1], W4K, CO_CH)), "1.11x"),
    # section 1's table: one row per way of counting BI's queries (BI and JOB are reported both ways), the whole row as one token
    ("1: the smallest worst case, BI and JOB by geomean", lambda T, a: form_row(T, "geomean over queries (each query equal)", "geomean"),
     "| geomean over queries (each query equal) | 5.25x (Ch-Co) | 1.02x (Sp-Co) | 1.04x (Ch-Ch) |"),
    ("1: the smallest worst case, BI and JOB once each", lambda T, a: form_row(T, "each query once (the paper's text today)", "once"),
     "| each query once (the paper's text today) | 12.11x (Ch-Co) | 1.29x (Co-Co) | 1.04x (Ch-Ch) |"),
    ("1: the smallest worst case, BI and JOB additive", lambda T, a: form_row(T, "additive: each query's cost over Sp-Co's, averaged", "additive"),
     "| additive: each query's cost over Sp-Co's, averaged | 3.18x (Co-Co) | 1.02x (Sp-Co) | 1.04x (Ch-Ch) |"),
    # section 3's table, a whole row as one token: once each / additive / geomean, the best layout; the worst and its ratio
    ("3: table, BI wops", lambda T, a: form_table_row(T, "BI", "wops", "wops"), "| BI | wops | Ch-Ch; Sp-Sp 103.01x | Ch-Ch; Sp-Sp 31.04x | Ch-Ch; Sp-Sp 66.21x |"),
    ("3: table, BI bytes", lambda T, a: form_table_row(T, "BI", "bytes", "bytes"), "| BI | bytes | Ch-Co; Sp-Ch 2.01x | Sp-Co; Ch-Ch 19.82x | Co-Co; Sp-Ch 4.90x |"),
    ("3: table, BI ops + bytes / 4 KB", lambda T, a: form_table_row(T, "BI", W4K, "ops + bytes / 4 KB"), "| BI | ops + bytes / 4 KB | Ch-Ch; Sp-Sp 14.41x | Co-Ch; Sp-Sp 3.91x | Ch-Ch; Sp-Sp 7.02x |"),
    ("3: table, JOB wops", lambda T, a: form_table_row(T, "IMDb / JOB", "wops", "wops"), "| IMDb / JOB | wops | Ch-Ch; Sp-Sp 33.11x | Ch-Ch; Sp-Sp 7.50x | Ch-Ch; Sp-Sp 18.06x |"),
    ("3: table, JOB bytes", lambda T, a: form_table_row(T, "IMDb / JOB", "bytes", "bytes"), "| IMDb / JOB | bytes | Co-Co; Ch-Ch 2.81x | Sp-Co; Ch-Ch 149.66x | Sp-Co; Ch-Ch 25.65x |"),
    ("3: table, JOB ops + bytes / 4 KB", lambda T, a: form_table_row(T, "IMDb / JOB", W4K, "ops + bytes / 4 KB"), "| IMDb / JOB | ops + bytes / 4 KB | Ch-Ch; Sp-Sp 4.70x | Co-Ch; Ch-Sp 1.79x | Ch-Ch; Sp-Sp 1.84x |"),
    ("3: JOB geomean, ops + bytes / 4 KB, Co-Ch over Ch-Ch", lambda T, a: x(ratio(T, FORMS["geomean"][1], W4K, CO_CH)), "1.02x"),
    ("3: JOB additive, ops + bytes / 4 KB, Ch-Ch over Co-Ch", lambda T, a: x(ratio(T, JOB_ADD, W4K, CH_CH)), "1.77x"),
    ("3: JOB additive, ops + bytes / 4 KB, Co-Co over Co-Ch", lambda T, a: f"{100 * (T[(JOB_ADD, W4K)][CO_CO] / T[(JOB_ADD, W4K)][CO_CH] - 1):.1f}%", "0.2%"),
    ("3: JOB, Ch-Ch over Sp-Co per query: cheaper on, and the queries over 6x", lambda T, a: (lambda r: "cheaper than Sp-Co on {} of {} queries, but costs {:.1f}-{:.1f}x Sp-Co's on {}".format(
        sum(v < 1 for v in r), len(r), min(v for v in r if v > 6), max(r), sum(v > 6 for v in r)))(
        [(lambda cs: cs[CH_CH][W4K] / cs[SP_CO][W4K])(cs) for cs in raw_costs(os.path.join(a.root, "per-query-disagreement", "costs.csv"), a.perturb_t)[0]["IMDb / JOB"].values()]),
     "cheaper than Sp-Co on 87 of 113 queries, but costs 6.1-12.4x Sp-Co's on 9"),
    ("3: JOB random mixes, Co-Ch", lambda T, a: random_mix_text(a, CO_CH, "IMDb / JOB"), "Co-Ch is the best on 558 of 1,000 (median 1.00x, worst 1.36x)"),
    ("3: JOB random mixes, Co-Co", lambda T, a: random_mix_text(a, CO_CO, "IMDb / JOB"), "Co-Co is the best on 442 of 1,000 (median 1.01x, worst 1.27x)"),
    ("3: JOB random mixes, Ch-Ch", lambda T, a: random_mix_text(a, CH_CH, "IMDb / JOB"), "Ch-Ch is the best on 0 of 1,000 (median 1.79x, worst 2.79x)"),
    ("3: JOB random mixes, shares on Ch-Ch", lambda T, a: "{:,} of 1,000".format(random_mix(a, CH_CH, shares_on=CH_CH, bench="IMDb / JOB", mixes="Dir(1) x 1000, shares on Ch-Ch")[0]), "1,000 of 1,000"),
    ("3: BI additive, ops + bytes / 4 KB, Ch-Ch over Co-Ch", lambda T, a: x(ratio(T, BI_ADD, W4K, CH_CH)), "1.10x"),
    ("3: BI random mixes, Co-Ch", lambda T, a: random_mix_text(a, CO_CH), "Co-Ch is the best on 666 of 1,000 (median 1.00x, worst 1.46x)"),
    ("3: BI random mixes, Ch-Ch", lambda T, a: random_mix_text(a, CH_CH), "Ch-Ch is the best on 334 of 1,000 (median 1.07x, worst 1.90x)"),
    # section 2's table, a whole row as one token (ops + bytes / 4 KB)
    ("2: table, I01", lambda T, a: per_query_row(a, "LDBC, all of I01", "LDBC Interactive (I01)", "sum"),
     "| LDBC Interactive (I01) | Co-Ch | 7 of 27 | 17 (up to 32.08x, iu3) | 26.9% | 1.23x less |"),
    ("2: table, BI", lambda T, a: per_query_row(a, "BI", "LDBC BI", "geomean"), "| LDBC BI | Ch-Ch | 14 of 17 | 1 (up to 2.95x, bi8) | 2.8% | 1.09x less |"),
    ("2: table, JOB", lambda T, a: per_query_row(a, "IMDb / JOB", "IMDb / JOB", "geomean"),
     "| IMDb / JOB | Ch-Ch | 48 of 113 | 38 (up to 12.50x, job6c) | 10.2% | 1.62x less |"),
    ("2: table, LinkBench", lambda T, a: per_query_row(a, "LinkBench", "LinkBench", "sum"),
     "| LinkBench | Co-Co | 10 of 16 | 2 (up to 2.17x, addlink_link2) | 19.8% | 1.13x less |"),
    ("3: JOB random mixes, wops, Ch-Ch's wins", lambda T, a: "{:,} of 1,000".format(random_mix(a, CH_CH, model="wops", bench="IMDb / JOB")[0]), "1,000 of 1,000"),
    ("3: JOB random mixes, bytes, Co-Co's wins", lambda T, a: "{:,} of 1,000".format(random_mix(a, CO_CO, model="bytes", bench="IMDb / JOB")[0]), "995 of 1,000"),
]

# Each number is looked up in the doc inside the words around it, so a token is found exactly once (a bare "1.02x" or
# "Ch-Ch" occurs many times, and a wrong statement elsewhere would pass). CONTEXT wraps an entry's value in its sentence;
# AGAIN checks a number the doc states a second time (section 4's table), each in its own words.
CONTEXT = {
    "1: the smallest worst case over the four benchmarks, ops + bytes / 4 KB": "every uniform layout costs at least {} the best on one of the four benchmarks",
    "1: Ch-Ch's worst case under wops": "under wops, Ch-Ch is within {} on all four",
    "1: Sp-Co's worst case under bytes": "under bytes, Sp-Co is within {}",
    "1: Co-Co's worst case under bytes, set by JOB": "Co-Co, within 1.01x on the other three, costs {} on JOB",
    "1: I01 at B = 1": "needs {} fewer wops than the best uniform layout",
    "1: I01 at B = 2": "at B = 2, {}",
    "1: I01 at B = 5": "at B = 5, {}",
    "1: I02 at B = 1": "reads only): {} at B = 1",
    "1: I02's peak": "peak {} at B = 7.27",
    "1: the one mix's worst case": "costs at most {} a workload's own best uniform",
    "title, 1: Co-Co's worst case over the one mix's": "one mixed layout cuts the worst case {}",
    "1: Co-Co's worst case over the 18": "Co-Co, reaches {}",
    "3: BI geomean, ops + bytes / 4 KB, Co-Ch over Ch-Ch": "Co-Ch is {} Ch-Ch under the geomean, and Ch-Ch is 1.10x",
    "3: JOB geomean, ops + bytes / 4 KB, Co-Ch over Ch-Ch": "JOB: Co-Ch is {} Ch-Ch under the geomean",
    "3: JOB additive, ops + bytes / 4 KB, Ch-Ch over Co-Ch": "Ch-Ch is {} Co-Ch under the additive form (where Co-Co is within",
    "3: JOB random mixes, shares on Ch-Ch": "With the shares drawn on Ch-Ch, Ch-Ch is the best on {}.",
}
DROP = {"1: I01's peak", "1: I01's peak at B", "1: JOB's best, ops + bytes / 4 KB (geomean and once each)",
        "1: Co-Ch (I01's best) on LinkBench", "1: Ch-Ch (BI's and JOB's best) on LinkBench", "1: Co-Co (LinkBench's best) on BI",
        "3: JOB random mixes, wops, Ch-Ch's wins", "3: JOB random mixes, bytes, Co-Co's wins"}       # replaced by AGAIN's rows
NAMES = {W16_COL: "LDBC Interactive", FORMS["geomean"][0]: "LDBC BI", FORMS["geomean"][1]: "IMDb / JOB", LB_COL: "LinkBench"}


def best_row(T, col):
    """one row of section 1's second table: a benchmark's best layout and its worst ratio on another benchmark"""
    b = min(CELLS, key=lambda c: (T[(col, W4K)][c], c))
    other = max((o for o in HEADLINE if o != col), key=lambda o: ratio(T, o, W4K, b))
    return f"| {NAMES[col]} | {lab(b)} | {x(ratio(T, other, W4K, b))} on {NAMES[other]} |"


def peak(a, wdir):
    f = front(a, wdir)
    return f"{x(f[2])} at B = {f[3]:.2f}"


AGAIN = [
    ("1: Ch-Co's worst, on BI", lambda T, a: f"Ch-Co, still costs {x(ratio(T, FORMS['geomean'][0], W4K, CH_CO))} the best on LDBC BI"),
    ("1: the table of each benchmark's best, I01", lambda T, a: best_row(T, W16_COL)),
    ("1: the table of each benchmark's best, BI", lambda T, a: best_row(T, FORMS["geomean"][0])),
    ("1: the table of each benchmark's best, JOB", lambda T, a: best_row(T, FORMS["geomean"][1])),
    ("1: the table of each benchmark's best, LinkBench", lambda T, a: best_row(T, LB_COL)),
    ("1: I01's peak", lambda T, a: f"the gap peaks at {peak(a, 'i01-rw-ldbc-mix')}"),
    ("3: BI random mixes, wops and bytes", lambda T, a: "wops, Ch-Ch is the best uniform layout on {:,} of 1,000; under bytes, Co-Co on {:,}".format(
        random_mix(a, CH_CH, model="wops")[0], random_mix(a, CO_CO, model="bytes")[0])),
    ("3: JOB random mixes, wops and bytes", lambda T, a: "under wops, Ch-Ch is the best on {:,} of 1,000; under bytes, Co-Co on {:,} of 1,000".format(
        random_mix(a, CH_CH, model="wops", bench="IMDb / JOB")[0], random_mix(a, CO_CO, model="bytes", bench="IMDb / JOB")[0])),
    ("3: JOB random mixes, bytes, Sp-Co (the forms' pick)", lambda T, a: (lambda w, med, mx: f"Sp-Co, which wins {w} (median {med:.2f}x)")(
        *random_mix(a, SP_CO, model="bytes", bench="IMDb / JOB"))),
    ("4: table, three benchmarks", lambda T, a: "| 5.53x (Ch-Co) | {} | BI's picks |".format(
        (lambda cols: (lambda b: f"{x(max(ratio(T, c, W4K, b) for c in cols))} ({lab(b)})")(
            min(CELLS, key=lambda c: (max(ratio(T, k, W4K, c) for k in cols), c))))((W16_COL, FORMS["geomean"][0], LB_COL)))),
    # history, B x Co-Sp's bytes (the 10-13 and 10-24 snapshots, before B's unit moved): not recomputed from
    # this snapshot, whose fronts are in Sp-Co's bytes; the 10-24 values were recomputed from that snapshot
    ("4: table, I01 at B = 1", lambda T, a: "| I01 mix over best uniform, B = 1 (Co-Sp's bytes) | 4.92x | 3.45x |"),
    ("4: table, I01 peak", lambda T, a: "| I01 peak (B in Co-Sp's bytes) | 12.12x at B = 1.78 | 7.58x at B = 1.66 |"),
    ("4: table, I02 peak", lambda T, a: f"| I02 peak | 12.28x | {x(front(a, 'i02-r-ldbc-mix-reads')[2])} |"),
    ("4: table, the one mix", lambda T, a: f"| one mix (n = 18), worst case | 3.86x | {x(one_mix(a, 'one mix, worst case'))} |"),
    ("4: table, its best uniform layout", lambda T, a: f"| its best uniform layout, worst case | 15.76x (Ch-Sp) | {x(one_mix(a, 'Condensed/Condensed'))} (Co-Co) |"),
    ("1: the one mix, a smaller worst case", lambda T, a: f"a {x(one_mix(a, 'Condensed/Condensed') / one_mix(a, 'one mix, worst case'))} smaller worst case"),
]

_names = {w for w, _, _ in NUMBERS}
assert set(CONTEXT) <= _names and DROP <= _names, sorted((set(CONTEXT) | DROP) - _names)
NUMBERS = ([(w, (lambda f, fn: lambda T, a: f.format(fn(T, a)))(CONTEXT[w], fn), CONTEXT[w].format(want)) if w in CONTEXT else (w, fn, want)
            for w, fn, want in NUMBERS if w not in DROP]
           + [(w, fn, None) for w, fn in AGAIN])     # None: the doc must hold the recomputed text itself, once

def wops_of(r):
    return CM.op_weight(float(r["S"]), float(r["G"]), float(r["N"]))


PICKS_CHECKED = []     # (family, cell, query, is the pick the cheapest body), filled by loader_costs


def loader_costs(a):
    """part 0: (family, query) -> cell -> (wops, bytes) and -> weight, from the captures (never from costs.csv)"""
    cost, weight = defaultdict(dict), {}
    for r in capture_layout.reader(open(a.ldbc)):                      # I01: per (option, query, part), the mean of one run
        if r["option"] in CELLS:
            k = ("LDBC, all of I01", r["query"])
            w, b = cost[k].get(r["option"], (0.0, 0.0))
            parts_w = CM.op_weight(float(r["S"]), float(r["G"]), float(r["N"])) + float(r["P"]) + float(r["D"])
            parts_b = math.fsum(float(r[x]) for x in LANES) + float(r["write_key"]) + float(r["write_value"])
            cost[k][r["option"]] = (w + parts_w, b + parts_b)
            weight[k] = float(r["how_often"])
    for fam, path, capture in (("BI", a.bi, a.bi_capture), ("IMDb / JOB", a.job, a.job_capture)):    # BI and JOB: the picked body's rows, every param
        if not path:
            continue
        runs = defaultdict(lambda: [0.0, 0.0, 0])
        picks = list(capture_layout.reader(open(path)))
        for cell in sorted({r["cell"] for r in picks}):
            for r in capture_layout.reader(open(Capture(capture).path_of("counts", cell))):
                if r["ok"] == "1":
                    x = runs[(cell, r["query"], r["body"])]
                    x[0] += wops_of(r); x[1] += math.fsum(float(r[k]) for k in LANES); x[2] += 1
        for r in picks:
            w, b, n = runs[(r["cell"], r["query"], r["pick"])]
            cost[(fam, r["query"])][r["cell"]] = (w / n, b / n) if n else (math.nan, math.nan)
            weight[(fam, r["query"])] = 1.0
            # the pick: the cheapest body under ops + bytes / 4 KB among those that ran ok on every param (in-sample)
            mine = {v: x for (c, q, v), x in runs.items() if (c, q) == (r["cell"], r["query"])}
            full = max(x[2] for x in mine.values())
            score = {v: CM.wops_bytes4k(x[0], x[1]) / x[2] for v, x in mine.items() if x[2] == full}
            low = min(score.values())
            PICKS_CHECKED.append((fam, r["cell"], r["query"], r["pick"] in score and score[r["pick"]] <= low * (1 + 1e-9)))
    view = compose.resolve_chunk_view(a.chunk_view)
    for cell in CELLS:                                         # LinkBench: the mean per request, reads and writes
        acc = defaultdict(lambda: [0.0, 0.0, 0])
        for r in capture_layout.reader(open(capture_in(a.linkbench, "linkbench", a.linkbench_params).path_of("counts", cell))):
            if r.get("planner_pick", "1") == "1":  # one body per request type (a capture with a planner_pick column flags it 1)
                if r["ok"] != "1":
                    sys.exit(f"LinkBench {cell} {r['query']} param {r['param_idx']}: the request's run failed")
                x = acc[r["query"]]
                x[0] += wops_of(r); x[1] += math.fsum(float(r[k]) for k in LANES); x[2] += 1
        for r in capture_layout.write_rows(writes_in(a.linkbench, "linkbench", a.linkbench_params).path_of("counts", cell), cell=cell)[2]:
            r = compose.chunk_view(r, view, cell)              # the chunk view per_query_disagreement costs it under
            f = lambda k: float(r[k] or 0)
            x = acc[r["query"]]
            # every read lane of a write (the GET key and value, the scan stream, the misses), and the bytes it writes
            x[0] += wops_of(r) + f("P") + f("D")
            x[1] += math.fsum(f(k) for k in LANES + ("write_key", "write_value")); x[2] += 1
        for q, (w, b, n) in acc.items():
            cost[("LinkBench", q)][cell] = (w / n, b / n)
            if weight.setdefault(("LinkBench", q), float(n)) != n:
                sys.exit(f"LinkBench {q}: {n} requests on {cell}, {weight[('LinkBench', q)]:.0f} on another cell")
    return cost, weight


def raw_costs(path, perturb):
    """family -> query -> cell -> {model: cost}, the currencies rebuilt from costs.csv's raw channels; and the rows"""
    cost, weight, rows = defaultdict(lambda: defaultdict(dict)), defaultdict(dict), []
    hit = 0
    for r in capture_layout.reader(open(path)):
        rows.append(r)
        v = {k: float(r[k]) for k in ("S", "G", "N", "P", "D", "write_bytes") + LANES}
        if perturb and (r["family"], r["query"], r["cell"]) == perturb[:3]:
            v[perturb[3]] *= perturb[4]
            hit += 1
        wops = CM.op_weight(v["S"], v["G"], v["N"]) + v["P"] + v["D"]
        nbytes = math.fsum(v[k] for k in LANES) + v["write_bytes"]
        cost[r["family"]][r["query"]][r["cell"]] = {"wops": wops, "bytes": nbytes, "wops_bytes4k": CM.wops_bytes4k(wops, nbytes)}
        weight[r["family"]][r["query"]] = float(r["weight"])
    if perturb and not hit:
        sys.exit(f"--perturb {perturb[:3]}: no such row in {path}")
    return cost, weight, rows


def count_of(how, family, q, weight):
    w = weight[family][q]
    is_write = family == "LinkBench" and q.startswith(LB_WRITE)
    return {"file": w, "one": 1.0, "reads": None if is_write else w, "writes": w if is_write else None,
            "3x writes": 3 * w if is_write else w}[how]


def totals(cost, weight, families, how, form, model):
    """cell -> the column's total (sum), geomean, or mean cost over Sp-Co's (additive)"""
    qs = [(f, q) for f in families for q in sorted(cost[f]) if count_of(how, f, q, weight) is not None]
    out = {}
    for c in CELLS:
        if form == "sum":
            out[c] = math.fsum(count_of(how, f, q, weight) * cost[f][q][c][model] for f, q in qs)
        elif form == "additive":
            out[c] = math.fsum(cost[f][q][c][model] / cost[f][q][SP_CO][model] for f, q in qs) / len(qs)
        elif form == "geomean":
            out[c] = math.exp(math.fsum(math.log(cost[f][q][c][model]) for f, q in qs) / len(qs))
        else:
            raise SystemExit(f"unknown form {form}")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", required=True, help="the analysis root: per-query-disagreement/, joint-mix/, cross-benchmark-frequencies/, pick-margins/")
    ap.add_argument("--ldbc", required=True, help="I01's breakdown.csv (workload_story.py)")
    ap.add_argument("--bi", required=True, help="bi_picks.csv")
    ap.add_argument("--bi-capture", required=True, help="the BI capture dir --bi was picked on")
    ap.add_argument("--linkbench", required=True, help="LinkBench's captures: a captures/linkbench/<scale> dir")
    ap.add_argument("--linkbench-params", default=None, help="the LinkBench param set read (default: LinkBench's one "
                    "default, output_tree.default_param_set: log, the whole request log)")
    ap.add_argument("--job", default="", help="job_picks.csv (needed unless --pending 'IMDb / JOB')")
    ap.add_argument("--job-capture", default="", help="the JOB capture dir --job was picked on")
    ap.add_argument("--doc", default="", help="the results doc; part 2 needs it")
    ap.add_argument("--pending", action="append", default=[], metavar="FAMILY", help="a family not captured yet: its columns are skipped")
    benchmarks.add_chunk_view_arg(ap)
    ap.add_argument("--perturb", default="", help="FAMILY,QUERY,CELL,CHANNEL,FACTOR (the negative control)")
    a = ap.parse_args()
    perturb = None
    if a.perturb:
        f, q, c, ch, x = a.perturb.rsplit(",", 4)       # a family name may hold a comma ("LDBC, all of I01")
        perturb = (f, q, c, ch, float(x))
    a.perturb_t = perturb                               # part 2's per-query entries re-read costs.csv with it too
    cost, weight, rows = raw_costs(os.path.join(a.root, "per-query-disagreement", "costs.csv"), perturb)
    for f in a.pending:
        if cost.get(f):
            sys.exit(f"{f}: --pending names it, but costs.csv has {len(cost[f])} of its queries")
    if "IMDb / JOB" not in a.pending and not a.job:
        sys.exit("IMDb / JOB: give --job <job_picks.csv> (its runs/ are the loader's input) or --pending 'IMDb / JOB'")
    checked, bad, said = 0, 0, set()

    def fail(msg, key=None):
        nonlocal bad
        if key is not None and key in said:              # one line (and one count) per defect, not per row it touches
            return
        said.add(key)
        bad += 1
        if bad <= 25:
            print("FAIL", msg)

    def compare(what, got, want, tol=None):
        nonlocal checked
        checked += 1
        if not abs(got - want) <= (tol if tol is not None else max(ABS_TOL, REL_TOL * abs(want))):
            fail(f"{what}: recomputed {got:.6f}, the file {want:.6f}")

    # part 0: the loader
    for fam, qs in EXPECTED.items():
        if fam in a.pending:
            continue
        if sorted(cost[fam]) != sorted(qs):
            fail(f"{fam}: queries {sorted(cost[fam])} != expected {sorted(qs)}")
        for q in cost[fam]:
            if sorted(cost[fam][q]) != sorted(CELLS):
                fail(f"{fam} {q}: on {len(cost[fam][q])} of the 9 layouts")
    lcost, lweight = loader_costs(a)
    for (fam, q), per in sorted(lcost.items()):
        if q not in cost[fam]:
            continue                                     # reported by the query-set test
        compare(f"loader {fam} {q} weight", weight[fam][q], lweight[(fam, q)], 0)
        for c, (w, b) in per.items():
            r = cost[fam][q][c]
            compare(f"loader {fam} {q} {c} wops", r["wops"], w, max(1e-6, 1e-7 * w))
            compare(f"loader {fam} {q} {c} bytes", r["bytes"], b, max(1e-6, 1e-7 * b))
    for fam, cell, q, ok in PICKS_CHECKED:
        checked += 1
        if not ok:
            fail(f"{fam} {cell} {q}: the pick is not the cheapest body under ops + bytes / 4 KB")
    fams = sorted({f for f, *_ in PICKS_CHECKED})
    print(f"   picks: {len(PICKS_CHECKED)} ({', '.join(f'{f} {sum(1 for x in PICKS_CHECKED if x[0] == f)}' for f in fams)}) "
          f"checked against their runs, {sum(1 for x in PICKS_CHECKED if not x[3])} not the cheapest")
    picks = {(r["option"], r["query"]): r["pick"] for r in capture_layout.reader(open(os.path.join(a.root, "pick-margins", "pick_margins.csv")))}
    for r in rows:
        if r["family"] in ("LDBC IC", "LDBC IS"):
            checked += 1
            if picks.get((r["cell"], r["query"])) != r["body"]:
                fail(f"LDBC {r['query']} {r['cell']}: body {r['body']}, pick_margins.csv {picks.get((r['cell'], r['query']))}")
    # the currency columns steps 3-5 read: equal to the raw channels' rebuild (6 decimals in the file)
    for r in rows:
        for m in MODELS:
            if (r["family"], r["query"], r["cell"]) != (perturb or (None,) * 3)[:3]:
                v = cost[r["family"]][r["query"]][r["cell"]][m]
                compare(f"costs.csv {r['family']} {r['query']} {r['cell']} {m} column", v, float(r[m]), max(2e-6, 1e-7 * v))
    n0 = checked

    # part 1a: cross_benchmark_frequencies.csv, every (column, cell, model): the total's ratio to the best and the equal share
    T, seen = {}, set()
    for r in capture_layout.reader(open(os.path.join(a.root, "cross-benchmark-frequencies", "cross_benchmark_frequencies.csv"))):
        key = (r["benchmark"], r["frequencies"])
        family, how, form = FREQ_COLUMNS[key]
        seen.add(key)
        for m in MODELS:
            t = T.setdefault((key, m), totals(cost, weight, (family,), how, form, m))
            best = min(CELLS, key=lambda c: (t[c], c))
            compare(f"frequencies {key} {r['cell']} {m} ratio", t[r["cell"]] / t[best], float(r[f"{m}_ratio"]))
            share = math.exp(math.fsum(math.log(cost[family][q][r["cell"]][m] / cost[family][q][best][m]) for q in cost[family]) / len(cost[family]))
            compare(f"frequencies {key} {r['cell']} {m} equal share", share, float(r[f"{m}_equal_share"]))
            if r[f"{m}_best_cell"] != best:
                fail(f"frequencies {key} {m}: best {best}, the file {r[f'{m}_best_cell']}", ("best", key, m))
    want_cols = {k for k, v in FREQ_COLUMNS.items() if v[0] not in a.pending}
    if seen != want_cols:
        fail(f"frequencies: columns {sorted(seen)} != expected {sorted(want_cols)}")
    n1 = checked - n0

    # part 1b: cross_benchmark_regret.csv, every (column, cell, model) ratio
    seen = set()
    for r in capture_layout.reader(open(os.path.join(a.root, "joint-mix", "cross_benchmark_regret.csv"))):
        families, how, form = REGRET_COLUMNS[r["benchmark"]]
        seen.add(r["benchmark"])
        t = T.setdefault((r["benchmark"], r["model"]), totals(cost, weight, families, how, form, r["model"]))
        best = min(CELLS, key=lambda c: (t[c], c))
        compare(f"regret {r['benchmark']} {r['cell']} {r['model']}", t[r["cell"]] / t[best], float(r["ratio"]))
        if r["best_single"] != best:
            fail(f"regret {r['benchmark']} {r['model']}: best {best}, the file {r['best_single']}", ("best", r["benchmark"], r["model"]))
    want_cols = {k for k, v in REGRET_COLUMNS.items() if not set(v[0]) <= set(a.pending)}
    if seen != want_cols:
        fail(f"regret: columns {sorted(seen)} != expected {sorted(want_cols)}")
    n2 = checked - n0 - n1

    # the ties a reader must be told: a best layout with another within TIE (the doc names both)
    for (col, m), t in sorted(T.items(), key=str):
        best = min(CELLS, key=lambda c: (t[c], c))
        near = [c for c in CELLS if c != best and t[c] <= t[best] * TIE]
        if near:
            print(f"   tie: {col} {m}: best {best}, within {100 * (TIE - 1):.0f}%: "
                  + ", ".join(f"{c} +{100 * (t[c] / t[best] - 1):.3f}%" for c in near))

    # part 2: the doc's numbers, each found in the doc as a token (not inside a longer number). They cover all four
    # benchmarks: with a family pending (the 10-24 snapshot alone), parts 0 and 1 run and part 2 is skipped
    if a.pending:
        if a.doc:
            fail("--doc with --pending: the doc's numbers cover every benchmark")
        print(f"   part 2 skipped: {', '.join(a.pending)} pending, and the doc's numbers cover every benchmark")
        print(f"[check_cross_benchmark_numbers] {len(rows)} (query, cell) rows; loader {n0} values, frequencies {n1}, regret {n2}; "
              f"{bad} differ; pending: {', '.join(a.pending)}{'; PERTURBED ' + a.perturb if a.perturb else ''}")
        print("RESULT:", "PASS" if checked and not bad else "FAIL")
        return 1 if bad or not checked else 0
    doc = open(a.doc).read() if a.doc else ""
    if a.doc and not NUMBERS:
        fail("--doc given, but NUMBERS is empty: nothing of the doc was checked")
    for what, fn, want in NUMBERS:
        got = fn(T, a)
        want = got if want is None else want
        checked += 1
        hits = len(re.findall(rf"(?<![\w.]){re.escape(want)}(?![\w]|\.\d)", doc))
        if got != want or (a.doc and hits != 1):
            fail(f"doc: {what}: recomputed {got}, the doc says {want}" + ("" if hits == 1 or not a.doc else f" (found {hits} times in the doc, not once)"))
    if NUMBERS and not a.doc:
        print("   (no --doc: the numbers were recomputed but not looked up in the doc)")

    print(f"[check_cross_benchmark_numbers] {len(rows)} (query, cell) rows; loader {n0} values, frequencies {n1}, regret {n2}, "
          f"doc numbers {len(NUMBERS)}; {bad} differ{'; pending: ' + ', '.join(a.pending) if a.pending else ''}"
          f"{'; PERTURBED ' + a.perturb if a.perturb else ''}")
    print("RESULT:", "PASS" if checked and not bad else "FAIL")
    return 1 if bad or not checked else 0


if __name__ == "__main__":
    sys.exit(main())
