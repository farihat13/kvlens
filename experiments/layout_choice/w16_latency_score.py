#!/usr/bin/env python3
"""The pre-registered scorer of the I01 mix-latency experiment: per byte limit B, is the built mix faster than the
best single layout that fits B (and than the reference layout, Condensed/Split), per cache regime? Every word is printed by the
rule coded here (RULE below); the pre-registration text (not shipped) was diffed against this file before
the freeze. Analysis only: opens no store.

  modes
    predict --picks P --root <layout-choice dir>        the predicted ORDER per B from the COUNTS (ops, bytes, the steady
                                                        proxy ops + est_blocks), from the workload's breakdown.csv (the
                                                        counts' mean over params; est_blocks at query level)
    noise   --picks P --latency-root R --regime D --cells a,b,c --out A [--suffix -repeat] [--writes-only]
                                                        the repeat cells (D vs D<suffix>): drift, per-request gaps, the
                                                        per-CLASS total gap per copy pair -> tau per class (the frozen
                                                        rule) -> A/noise-<D>.json (--writes-only: the write capture's
                                                        repeat -> A/noise-writes.json, the IU class)
    score   --picks P --latency-root R --regime D --noise A/noise-<D>.json [--noise-writes A/noise-writes.json] --out A [--all-arms]
                                                        the words per B, the totals -> A/w16_latency.csv, A/w16_words.csv
    smoke   like noise + score on SEEN data with --stand-in stem=cell,... (a measured cell plays each mix / single),
            --tau for a fixed band, --no-gate (a snapshot has no gate.csv); never into a snapshot dir
    engine  --latency-root R --regime D --noise A/noise-<D>.json --out A   the engine-format arms against their cells:
            I02 without IC3 (the registered class: the format's day-bucket date index makes IC3 another query), I02, IC, IS;
            the two falsifiers (the word, the steady hit-rate gap >= 0.1) -> A/engine_arms.csv
    ic10-plan --cells C,.. --spec-dir D --out F    the ic10-bodies stage's plan (ic10_plan): the stores among C whose
                                                        recorded IC10 pick is the scan, each with its point body (the
                                                        planner's own) and its scan body -> F (store,point,scan)
    ic10    --latency-root R --out A                  IC10's two bodies (the ic10-bodies stage, warm): per store and body
                                                        the median over params 1-5 of the per-param median over the measured
                                                        iterations, both copies' iterations pooled (warm.json wall_ns_all);
                                                        scan / point; held when within IC10_BAND on EVERY store the record
                                                        picks the scan on among the stage's stores (the expected nine: the
                                                        stage's plan, R/ic10-bodies/bodies.csv; a store missing = rc 2)
                                                        -> A/ic10_bodies.csv
    predictions                                         the PREDICTIONS table as the text states it (the diff against the
                                                        frozen pre-registration is read on this print)
    passes  --latency-root R --regime D --noise A/noise-<D>.json --out A   AFTER the words: per store the steady regime's
                                                        pass-to-pass wall spread (cpplatency's steady-passes.csv: the measured
                                                        passes' wall_ns_sum, max / min - 1) against the noise floor, the premise
                                                        of the steady floor (5-10% in the 9-pass record); a store above the floor
                                                        is named beside its word rows (column pass_spread in w16_words.csv and
                                                        engine_arms.csv). Changes no word -> A/pass_spread.csv

Every word row carries `kind` (prediction | descriptive | unregistered), `allowed` (the registered words) and `verdict`
(held | FALSIFIED | descriptive | a FINDING outside the stated pair | reported | no verdict); score exits 2 when a
registered prediction row has no capture.

The quantity per (arm, regime): I01 latency per 100,000 updates = sum over queries of n_q x the per-query MEDIAN over
its params of the __plan__ wall (reads from the regime's run; IU1-8 from the write capture's apply wall, the one write
regime: warm, noflush), plus I02 (reads only) and the IC / IS / IU sub-totals; the mean over params beside the median.
Steady adds the kv-step and device rungs (per request the median over measured passes, from the raw steady.json). The
counts beside them (ops, bytes, est_blocks) are summed from the same steps.csv rows (the same executions), under the
median over params (as the latency) and under the mean (as the counts' prediction).
A word needs: the variant gate equal on every (query, param, regime) row of both arms (grid_latency's gate.csv), the same
(query, param) coverage on both arms, writes on both arms or on neither; else "no verdict (...)", counted.
"""
import argparse
import csv
import glob
import json
import math
import os
import re
import statistics as st
import sys
from kvevaluator.layouts import cell_parts, is_cell
from kvevaluator.paths import ROOT
from kvevaluator.plan_choice.pick_record import RECORD_WORKLOAD

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvcost.layout_cost.workloads import ldbc as I01
from kvcost.latency.features import BLOCK, BYTE_LANES, GET_LANES, SCAN_LANES, f
from kvcost.layout_cost import cost_models as CM  # (the cost models, one definition)
from kvcost.latency.steps import load_writes, raw_medians
from kvevaluator.measure.latency_raw_files import raw_files
from kvcost.latency.i01 import qclass, weights
from kvevaluator.measure.capture_protocol import check_same
from kvevaluator.capture import capture_layout

# ---- THE RULE (frozen with the pre-registration) --------------------------------------------------------------------
TAU_MULT = 2.0                                    # tau[class] = max(TAU_MULT x the repeat copies' class-total gap, FLOOR[regime])
FLOOR = {"steady": 0.10, "cold": 0.10, "warm": 0.05, "writes": 0.05}   # see the prereg: the 9-pass order effect (steady); warm uncited
DRIFT_BOUND = 0.10                                # a repeat cell whose median end/start drift is outside 1 +- this voids the regime
MIN_COMMON = 90                                   # a repeat copy must share at least this many of the 95 read requests
MIN_COMMON_WRITES = 20
TIE_1PCT = 0.01                                   # the counts' own tie band when predicting an order
WORDS = ("mix faster", "no measurable difference", "mix slower")
REPEAT_CELLS = ("props_condensed_adj_split", "w16_b1", "w16_b3")   # the noise reference: measured twice, at the chain's start and end (the driver's REPEAT_CELLS: the reference single is the chain's first cell)
REPEAT_SUFFIX = "-repeat"
# the magnitude reading at B = 1 / 2 (IC): carry = (measured other / mix) / (the proxy's other / mix on ops + est_blocks of the same
# executions, median over params), at the plan, kv-step and device rungs; the bands cover every value
CARRY_BANDS = ((2.0, "the proxy UNDERSTATES (measured > 2x the proxy)"), (0.5, "the proxy carries (within 2x)"),
               (0.25, "the proxy overstates 2-4x"), (0.0, "the proxy overstates > 4x"))
CLASSES = ("I01", "I02", "IC", "IS", "IU")
CLASSES_EXTRA = ("IC_noIC6IC9", "IC_noIC12IC5", "W17_noIC3")   # the caveat columns: IC without the two "plan to change" queries
                                                               # (prereg item 7), IC without the two chunk-cache queries (the
                                                               # steady regime's limits), I02 without IC3 (the engine arm)
# ---- THE PREDICTIONS (the frozen text c6edd99d6, "The predictions, each with what falsifies it" + "What would count
# against us"): (B, comparator role, class, regime family or "*", the allowed words, kind). A prediction is held when the
# word is allowed and FALSIFIED when it is another verdict word; a descriptive row is reported, a word outside its stated
# pair is a FINDING, never a falsification; a (B, role, class) not listed is "unregistered: reported".
_F, _N, _S = "mix faster", "no measurable difference", "mix slower"
# The regimes field: "*" = every regime family, else the tuple of families the text registers. The readings of the frozen
# text that this table fixes were recorded beside the pre-registration (a-h), one line each.
_SW = ("steady", "warm")
PREDICTIONS = (
    [(B, role, "IC", "*", (_F,), "prediction") for B in ("0.5", "1") for role in ("best_single", "reference")]          # (e): cold counts
    + [(B, role, "I01", ("steady",), (_F,), "prediction") for B in ("0.5", "1") for role in ("best_single", "reference")]   # (a)
    + [(B, role, "I01", ("warm", "cold"), (_F,), "descriptive") for B in ("0.5", "1") for role in ("best_single", "reference")]
    + [("2", "best_single", "IC", "*", (_F, _N), "descriptive"), ("2", "reference", "IC", ("steady",), (_F,), "prediction")]   # (c)
    + [(B, "best_single", cls, "*", (_N,), "prediction") for B in ("3", "5") for cls in CLASSES]
    + [(B, "reference", "IC", "*", (_F,), "descriptive") for B in ("3", "5")]                                               # (b)
    + [(B, "reference", "IS", _SW, (_S, _N), "descriptive") for B in ("3", "5")]                                            # (f)
    + [("0.5", role, "IS", _SW, (_N, _S), "descriptive") for role in ("best_single", "reference")]
    + [("1", "best_single", "IS", _SW, (_F, _N), "descriptive"), ("1", "reference", "IS", _SW, (_N, _S), "descriptive"),
       ("2", "best_single", "IS", _SW, (_N, _S), "descriptive"), ("2", "reference", "IS", _SW, (_S, _N), "descriptive")]
)
CARRY_MIN_DEVICE = 0.5            # (d) B = 0.5 / 1, IC, steady, where "mix faster" is held: carry >= 0.5 at the device rung, else FALSIFIED
CARRY_SCOPE = {("0.5", "IC", "steady"), ("1", "IC", "steady")}
READ_QUERIES, NPARAM = 19, 5      # the I01 read query set x the reported params: a capture with fewer (query, param) requests is "no verdict (coverage)"
ENGINE_PREDICTIONS = {("dgraph", "props_split_adj_condensed"): ("W17_noIC3", ("steady", "warm"), (_N,)),   # the word, steady and warm
                      # the NebulaGraph arm (an added arm, recorded beside the pre-registration): I02 whole (its date
                      # index is exact, IC3 comparable); the word from its counts capture on this binary, ops ratio format / cell
                      # 1.298 under the ops^0.48 band [0.8, 1.25]
                      ("nebula", "props_condensed_adj_split"): ("I02", ("steady",), (_F,))}   # steady only: warm not run
ENGINE_HIT_RATE_GAP = 0.1         # steady: a hit-rate gap >= this between the arm and its cell falsifies too
IC10_BAND = (0.90, 1.10)          # the ic10-bodies stage: scan / point within this on EVERY store where the scan is picked
IC10_REGIME = "warm-p0"
IC10_COPIES = ("", "-copy2")
IC10_SCAN = "ic10_fof_from_person_run"   # the scan body's name prefix (the ic10-bodies stage times the stores that pick it)
IC10_PLAN = "bodies.csv"                 # the stage's plan under <latency root>/ic10-bodies/: store, point, scan



def _wl(root, sub):
    """a latency unit's Interactive dir: <root>/<sub>/interactive"""
    return os.path.join(root, sub, "interactive")

def registered(B, role, cls, fam):
    """(allowed words, kind) of the registered row for this (B, comparator, class) in this regime family, or (None, "unregistered")"""
    for b, r, c, f_, allowed, kind in PREDICTIONS:
        if b == B and r == role and c == cls and (f_ == "*" or fam in f_):
            return allowed, kind
    return None, "unregistered"


def verdict_of(w, allowed, kind):
    if w not in WORDS:
        return w                                                   # "no verdict (...)" stays what it is
    if kind == "prediction":
        return "held" if w in allowed else "FALSIFIED"
    if kind == "descriptive":
        return "descriptive" if w in allowed else "a FINDING (outside the stated pair)"
    return "reported"
# the byte limit's reference cell is the picks file's own "reference" (Co-Sp in a picks file in Co-Sp units)
# the engine-format arms: each engine format timed on its own store with its
# registry pins, next to the cell nearest to its layout; the first registered run times dgraph, the nebula arm is added
# later on the same root (its label counting and vid-first keys first). Words read "cell faster / slower" vs the format.
ENGINE_PAIRS = (("dgraph", "props_split_adj_condensed"), ("nebula", "props_condensed_adj_split"),
                ("dgraph-c512k", "props_split_adj_condensed"))   # an added arm: the Dgraph format at its 512 KB posting
                                                                 # split, the reporting rule; unregistered (reported
                                                                 # beside the registered unbounded arm; added-arms note, section 2)
ENGINE_FORMATS = tuple(f for f, _ in ENGINE_PAIRS)
ENGINE_WORDS = {"mix faster": "cell faster", "no measurable difference": "no measurable difference", "mix slower": "cell slower"}
OPS_TIME_EXPONENT = 0.48          # the SF30-derived rule: plan wall ~ ops^0.48; an ops ratio inside [0.8, 1.25] predicts no measurable difference
OPS_BAND = (0.8, 1.25)


def carry_word(carry):
    if carry != carry:
        return "no carry (missing rung)"
    for i, (lo, w) in enumerate(CARRY_BANDS):
        if (carry > lo) if i == 0 else (carry >= lo):      # the text: "> 2" understates; "within 2x" is 0.5 <= carry <= 2
            return w
    return CARRY_BANDS[-1][1]


def word(mix, other, tau):
    """RULE: faster if mix < other x (1 - tau); slower if mix > other x (1 + tau); else no measurable difference.
    The band is on the OTHER side's total, so it is not symmetric in ratio terms: "faster" needs other / mix > 1 / (1 - tau)
    (1.111 at tau 0.10), "slower" needs other / mix < 1 / (1 + tau) (0.909)."""
    if other <= 0 or mix <= 0 or other != other or mix != mix:
        return "no verdict (zero total)"
    if mix < other * (1 - tau):
        return WORDS[0]
    if mix > other * (1 + tau):
        return WORDS[2]
    return WORDS[1]


def order_word(mix, other, tie=TIE_1PCT):
    if abs(mix - other) <= tie * max(mix, other):
        return "tie (within 1%)"
    return "mix fewer" if mix < other else "single fewer"


def in_class(q, cls):
    if cls == "IC_noIC6IC9":
        return qclass(q) == "IC" and q not in ("ic6", "ic9")
    if cls == "IC_noIC12IC5":
        return qclass(q) == "IC" and q not in ("ic12", "ic5")
    if cls == "W17_noIC3":
        return qclass(q) != "IU" and q != "ic3"
    return cls == "I01" or (cls == "I02" and qclass(q) != "IU") or qclass(q) == cls


def regime_family(regime):
    return "steady" if regime.startswith("steady") else "cold" if regime.startswith("cold") else "warm"


# ---- predict: the order from the counts -------------------------------------------------------------------------------
def option_of(root_w16, assignment):
    """options.csv: the first option whose assignment equals the pick's (the breakdown.csv key)"""
    for r in capture_layout.reader(open(os.path.join(root_w16, "options.csv"))):
        if r["assignment"] == assignment:
            return r["option"]
    raise SystemExit(f"no option in {root_w16}/options.csv with assignment {assignment[:60]}...")


def breakdown_totals(root_w16, option, n):
    """per query the W16-weighted ops / bytes / est_blocks from breakdown.csv (the counts' MEAN over params; est_blocks
    by the per-op ceil at QUERY level, an approximation of the proxy's per-step formula); the totals checked by the caller"""
    per = {}
    for r in capture_layout.reader(open(os.path.join(root_w16, "breakdown.csv"))):
        if r["option"] != option:
            continue
        q = r["query"]
        x = per.setdefault(q, {"ops": 0.0, "bytes": 0.0, "S": 0.0, "G": 0.0, "get_b": 0.0, "scan_b": 0.0, "n": n.get(q, 0)})
        x["ops"] += float(r["read_ops"]) + float(r["write_ops"]) + float(r["write_rmw_ops"])
        x["bytes"] += float(r["read_bytes"]) + float(r["write_bytes"]) + float(r["write_rmw_bytes"])
        x["S"] += float(r["S"]); x["G"] += float(r["G"])
        x["get_b"] += float(r["get_key"]) + float(r["get_value"]); x["scan_b"] += float(r["scan_key"]) + float(r["scan_value"])
    for q, x in per.items():
        S, G = x["S"], x["G"]
        x["est_blocks"] = (G * math.ceil(x["get_b"] / G / BLOCK) if G else 0) + (S * math.ceil(x["scan_b"] / S / BLOCK) if S else 0)
        x["ops_blocks"] = x["ops"] + x["est_blocks"]
    tot = {k: sum(x[k] * x["n"] for x in per.values()) for k in ("ops", "bytes", "est_blocks", "ops_blocks")}
    for cls in ("IC", "IS", "IU"):
        for k in ("ops", "bytes", "ops_blocks"):
            tot[f"{k}_{cls}"] = sum(x[k] * x["n"] for q, x in per.items() if qclass(q) == cls)
    return tot, per


def mix_is(rec):
    """the fallback marker of a B's mix (the words file carries it: a reader must not take the fallback for the rule's pick)"""
    r = rec.get("rule_pick_not_buildable")
    if not r:
        return "the rule's pick"
    return (f"FALLBACK: {rec['pick']['kind']}; the rule's pick is {r['kind']} ({r['ops_vs_ref']:.4f}x ref ops, "
            f"{1 / r['fallback_ops_x']:.2f}x the fallback's ops), not buildable on this binary")


def predict(a):
    picks = json.load(open(a.picks))
    REF = picks["reference"]                   # the cell B multiplies, as the picks file was picked
    root_w16 = os.path.join(a.root, "i01-rw-ldbc-mix")
    n = weights()
    rows = []
    print(f"predicted order per B from {root_w16} (the counts' mean over params; est_blocks at query level)")
    for B, rec in picks["picks"].items():
        comps = {"mix": option_of(root_w16, rec["pick"]["assignment"]), "best_single": rec["best_single_within_B"]["cell"],
                 "reference": REF}
        if rec.get("near_miss_single"):
            comps["near_miss_single"] = rec["near_miss_single"]["cell"]
        tots = {}
        for role, opt in comps.items():
            tot, _ = breakdown_totals(root_w16, opt, n)
            want = {"mix": rec["pick"], "best_single": rec["best_single_within_B"], "reference": rec["reference"],
                    "near_miss_single": rec.get("near_miss_single")}[role]
            if abs(tot["ops"] - want["ops"]) > 1e-6 * want["ops"] or abs(tot["bytes"] - want["bytes"]) > 1e-6 * want["bytes"]:
                raise SystemExit(f"B={B} {role} {opt}: breakdown totals {tot['ops']:.1f} / {tot['bytes']:.1f} != picks.json "
                                 f"{want['ops']:.1f} / {want['bytes']:.1f} (the currency is not what the picks used)")
            tots[role] = tot
            rows.append({"B": B, "role": role, "option": opt, "mix_is": mix_is(rec) if role == "mix" else "",
                         **{k: round(v, 3) for k, v in tot.items()}})
        for other in ("best_single", "reference"):
            m, o = tots["mix"], tots[other]
            print(f"  B={B:<4} mix vs {other:12s} {comps[other]:32s} ops {order_word(m['ops'], o['ops']):13s} ({o['ops'] / m['ops']:7.2f}x)  "
                  f"bytes {order_word(m['bytes'], o['bytes']):13s} ({o['bytes'] / m['bytes']:5.2f}x)  "
                  f"ops+est_blocks {order_word(m['ops_blocks'], o['ops_blocks']):13s} ({o['ops_blocks'] / m['ops_blocks']:7.3f}x)  "
                  f"IS: ops {order_word(m['ops_IS'], o['ops_IS']):13s} ({o['ops_IS'] / m['ops_IS']:5.2f}x) "
                  f"ops+blocks {order_word(m['ops_blocks_IS'], o['ops_blocks_IS']):13s} ({o['ops_blocks_IS'] / m['ops_blocks_IS']:5.3f}x) "
                  f"bytes {order_word(m['bytes_IS'], o['bytes_IS']):13s}")
        print(f"         shares of the mix's ops: IC {m['ops_IC'] / m['ops']:.4f} IS {m['ops_IS'] / m['ops']:.4f} IU {m['ops_IU'] / m['ops']:.5f}")
    out = a.picks[:-len("-picks.json")] + "-predicted.csv"
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    # the same order as a markdown table, pasted into the pre-registration (never typed there)
    md = ["| B | vs | ops | bytes | ops + est_blocks | IC ops + est_blocks | IS ops | IS bytes | IS ops + est_blocks |", "|---|---|---|---|---|---|---|---|---|"]
    by = {(r["B"], r["role"]): r for r in rows}
    for B in picks["picks"]:
        m = by[(B, "mix")]
        for role, label in (("best_single", f"best single ({by[(B, 'best_single')]['option']})"), ("reference", "reference")):
            o = by[(B, role)]
            cell = lambda k: f"{o[k] / m[k]:.3g}" + (" (tie)" if order_word(m[k], o[k]) == "tie (within 1%)" else " mix fewer" if m[k] < o[k] else " single fewer")
            md.append(f"| {B} | {label} | {cell('ops')} | {cell('bytes')} | {cell('ops_blocks')} | {cell('ops_blocks_IC')} | {cell('ops_IS')} | {cell('bytes_IS')} | {cell('ops_blocks_IS')} |")
    mdp = a.picks[:-len("-picks.json")] + "-predicted.md"
    with open(mdp, "w") as fh:
        fh.write("Ratios are other / mix (> 1 = the mix has fewer); the counts' mean over params; est_blocks at query level. Generated by "
                 "`w16_latency_score.py predict`, pasted into the pre-registration.\n\n" + "\n".join(md) + "\n")
    print(f"{len(rows)} rows -> {out}; the table -> {mdp}")


# ---- the latency captures ------------------------------------------------------------------------------------------------
def load_reads(path, arm, rungs=False):
    """steps.csv -> {(query, param): {"plan": wall_ns, "kv", "device", counts...}} from the __plan__ row and the kv steps
    (features summed as the latency proxy does). A (query, param) seen twice (two regimes or accumulated runs in one file)
    stops the run. The steady rungs are NaN where the raw files are missing (counted, reported)."""
    out, steps, dup = {}, {}, []
    for r in capture_layout.reader(open(path)):
        k = (r["query"], r["param_idx"])
        if r["step"] == "__plan__":
            if k in out:
                dup.append(k)
            wall = f(r, "wall_ns")
            out[k] = {"plan": wall if wall > 0 else float("nan"), "kv": float("nan"), "device": float("nan")}   # a 0 wall = missing, never a value
            continue
        x = steps.setdefault(k, {"S": 0.0, "G": 0.0, "N": 0.0, "bytes": 0.0, "est_blocks": 0.0})
        S, G = f(r, "S"), f(r, "G")
        get_b, scan_b = sum(f(r, c) for c in GET_LANES), sum(f(r, c) for c in SCAN_LANES)
        x["S"] += S; x["G"] += G; x["N"] += f(r, "N"); x["bytes"] += sum(f(r, c) for c in BYTE_LANES)
        x["est_blocks"] += (G * math.ceil(get_b / G / BLOCK) if G else 0) + (S * math.ceil(scan_b / S / BLOCK) if S else 0)
    if dup:
        raise SystemExit(f"{path}: {len(dup)} (query, param) requests appear twice (e.g. {dup[0]}): one regime per file")
    raw_root = os.path.join(os.path.dirname(path), arm)
    missing = 0
    for k, v in out.items():
        v.update(steps.get(k, {}))
        v["ops"] = CM.op_weight(v.get("S", 0), v.get("G", 0), v.get("N", 0))
        if rungs:
            med = raw_medians(os.path.dirname(path), arm, k[0], k[1]) if os.path.isdir(raw_root) else None
            if med:
                v["kv"], v["device"] = med[0], med[1]
            else:
                missing += 1
    if rungs and missing:
        print(f"  {arm}: the kv / device rungs are missing on {missing} of {len(out)} requests (no raw steady.json): NaN there")
    zero = sum(1 for v in out.values() if v["plan"] != v["plan"])
    if zero:
        print(f"  {arm}: {zero} requests have no plan wall (0 or empty): NaN, so their query's total is NaN")
    return out


def class_totals(reads, writes, n, rung="plan", agg=st.median):
    """{class: total per 100,000 updates} = sum_q n_q x agg over the query's own params (median by default; the mean
    beside it); NaN if any param's value is NaN (a missing rung)"""
    per_q = {}
    for src in (reads or {}, writes or {}):
        for (q, _), v in src.items():
            if q in n:
                per_q.setdefault(q, []).append(v[rung])
    val = {q: (float("nan") if any(x != x for x in v) else agg(v)) for q, v in per_q.items()}
    return {cls: sum(n[q] * val[q] for q in val if in_class(q, cls)) for cls in CLASSES + CLASSES_EXTRA}


def sub_roots(a, regime=None):
    """the capture's roots below the latency root: "" (one root, a snapshot) and every directory the driver wrote a spec
    set into (grid, mixes, added, a re-picked mix set, ...); with a regime, those that hold <sub>/interactive/<regime>/"""
    subs = [""] + sorted(d for d in os.listdir(a.latency_root) if os.path.isdir(_wl(a.latency_root, d)))
    return [s for s in subs if regime is None or os.path.isdir(os.path.join(_wl(a.latency_root, s), regime))]


def check_protocol(a):
    """the captures this run reads must have timed their requests the same way (measure/capture_protocol): the regime's
    reads, its copies and the writes, or (ic10) both bodies' captures. --allow-mixed-protocol reads a mixed set anyway
    and writes each capture's protocol to <out>/capture_protocol.json (removed again by a later run of one protocol)"""
    if a.mode == "ic10":
        dirs = sorted(os.path.dirname(p) for p in glob.glob(os.path.join(a.latency_root, "ic10-bodies", "*", "*", "interactive", "*", "*", "steps.csv")))
    else:
        dirs = sorted(os.path.dirname(p) for sub in sub_roots(a)
                      for p in glob.glob(os.path.join(_wl(a.latency_root, sub), a.regime + "*", "*", "steps.csv")))
        if a.writes_regime and a.writes_root:
            dirs += sorted(os.path.dirname(p) for p in glob.glob(os.path.join(a.writes_root, a.writes_regime + "*", "*", "steps.csv")))
    try:   # this script keeps check_same's refusal: its rules were fixed before its data was seen
        labels = check_same(dirs, f"{a.latency_root} {a.regime or a.mode}", allow_mixed=a.allow_mixed_protocol)
    except SystemExit as e:
        raise SystemExit(f"{e}\npre-registered: the protocol rule is frozen") from None
    print(f"capture protocol: {len(dirs)} captures checked" + (", MIXED (--allow-mixed-protocol)" if labels else ", one protocol"))
    path = os.path.join(a.out, "capture_protocol.json")
    if labels:
        os.makedirs(a.out, exist_ok=True)
        with open(path, "w") as f:
            json.dump({os.path.relpath(c, a.latency_root): v for c, v in labels.items()}, f, indent=1, sort_keys=True)
    elif os.path.exists(path):
        os.remove(path)


def arm_dirs(a, arm, regime):
    """(the reads steps.csv, the writes steps.csv or None) of a store: the reads under <root>/interactive/<regime>/<arm>/ (one
    root, a snapshot) or <root>/{grid,mixes}/interactive/<regime>/<arm>/ (the driver's two grid_latency roots); the writes under
    <writes-root>/<writes regime>/<arm>/"""
    cands = [os.path.join(_wl(a.latency_root, sub), regime, arm, "steps.csv") for sub in sub_roots(a)]
    r = next((c for c in cands if os.path.exists(c)), cands[0])
    w = os.path.join(a.writes_root, a.writes_regime, arm, "steps.csv") if a.writes_regime and a.writes_root else None
    return r, (w if w and os.path.exists(w) else None)


FORMAT_LABEL = {"dgraph": "Dgraph format", "nebula": "NebulaGraph format", "dgraph-c512k": "Dgraph format (512 KB split)",
                "pertype_csr-c64k": "Per-type CSR (64 KB chunks)"}


def label_of(stem, picks=None):
    """a store's name by what it is: a cell by its settings (Split/Condensed = Split props, Condensed adjacency), a mix by
    its byte limit(s) (I01 mix B=3,5), an engine format as a format; else the stem"""
    if stem in FORMAT_LABEL:
        return FORMAT_LABEL[stem]
    if is_cell(stem):                       # read by kvevaluator.layouts.cell_parts, the one parser
        p, pk, a, ak = cell_parts(stem)
        sizes = {k for k in (pk, ak) if k}
        if len(sizes) == 2:                 # two Chunked sizes: each side names its own
            return f"{p.capitalize()} {pk} KB/{a.capitalize()} {ak} KB"
        return f"{p.capitalize()}/{a.capitalize()}" + (f", {sizes.pop()} KB chunks" if sizes else "")
    if picks:
        Bs = [B for B, r in picks["picks"].items() if r["spec"][:-5] == stem]
        if Bs:
            return f"I01 mix B={','.join(Bs)}"
    m = re.fullmatch(r"w16_b(\d+)_picks_(\d{4})(\d\d)(\d\d)", stem)      # a mix re-picked on a later day's counts
    if m:
        return f"I01 mix B={'0.5' if m.group(1) == '05' else m.group(1)} (re-picked {m.group(2)}-{m.group(3)}-{m.group(4)})"
    return stem


def all_arm_stems(a, regime):
    """every arm measured under the latency root for this regime (the chain copies, not the repeats): meta-<stem> -> stem,
    an engine format's dir -> its name"""
    stems = []
    for sub in sub_roots(a, regime):                         # e.g. added/: the stores added after the first results
        d = os.path.join(_wl(a.latency_root, sub), regime)
        for arm in sorted(os.listdir(d)):
            if os.path.exists(os.path.join(d, arm, "steps.csv")):
                stems.append(arm[5:] if arm.startswith("meta-") else arm)
                if not arm.startswith("meta-"):
                    OWN_NAME_ARMS.add(arm)                   # a named format's store: its dir is its name
    return stems


OWN_NAME_ARMS = set(ENGINE_FORMATS)


def arm_of(a, stem):
    """the store arm of a spec stem: meta-<stem>, an engine format's own name, or the stand-in (smoke: 'stem=cell' ->
    meta-<cell>, 'stem=arm:<name>' -> name)"""
    if stem in OWN_NAME_ARMS:
        return stem
    if a.stand_in:
        m = dict(kv.split("=", 1) for kv in a.stand_in.split(","))
        if stem in m:
            return m[stem][4:] if m[stem].startswith("arm:") else f"meta-{m[stem]}"
    return f"meta-{stem}"


def gate_of(a, stem, regime):
    """the variant gate (grid_latency's gate.csv) for this cell and regime: (rows compared, rows differing); None = no gate"""
    rows = []
    for sub in sub_roots(a):
        p = os.path.join(a.latency_root, sub, "gate.csv")
        if os.path.exists(p):
            rows += [r for r in capture_layout.reader(open(p)) if r["cell"] == stem and r["regime"] == regime]
    if not rows:
        return None
    return len(rows), sum(1 for r in rows if r["equal"] != "1")


def arm_totals(a, stem, regime, n):
    arm = arm_of(a, stem)
    rp, wp = arm_dirs(a, arm, regime)
    if not os.path.exists(rp):
        return None, arm
    reads = load_reads(rp, arm, rungs=regime_family(regime) == "steady")
    writes = load_writes(wp) if wp else None
    t = {}
    for cls, v in class_totals(reads, writes, n).items():
        t[f"{cls}_median"] = v
    for cls, v in class_totals(reads, writes, n, agg=st.mean).items():
        t[f"{cls}_mean"] = v
    if regime_family(regime) == "steady":
        for rung in ("kv", "device"):                     # the reads' rungs only (an IU row has no rung): I02 / IC / IS
            for cls, v in class_totals(reads, None, n, rung).items():
                if cls != "I01":
                    t[f"{cls}_median_{rung}"] = v
    for k in ("ops", "bytes", "est_blocks"):      # the counts of the same executions, per query over ITS params
        for agg, name in ((st.median, "median"), (st.mean, "mean")):
            ct = class_totals(reads, writes, n, k, agg)
            t[f"counts_{k}_{name}"], t[f"counts_{k}_{name}_IS"], t[f"counts_{k}_{name}_IC"] = ct["I01"], ct["IS"], ct["IC"]
    for name in ("median", "mean"):
        for cls in ("", "_IS", "_IC"):
            t[f"counts_ops_blocks_{name}{cls}"] = t[f"counts_ops_{name}{cls}"] + t[f"counts_est_blocks_{name}{cls}"]
    t["requests"] = len(reads); t["iu_requests"] = len(writes or {})
    want_q = {q for q in n if qclass(q) != "IU"}
    have = {k[0] for k in reads}
    short = [q for q in sorted(want_q) if q not in have]
    t["_coverage"] = "" if len(reads) == READ_QUERIES * NPARAM and not short else \
        f"{len(reads)} of {READ_QUERIES * NPARAM} read requests" + (f", missing queries {' '.join(short)}" if short else "")
    t["writes_file"] = "yes" if wp else "NONE"
    g = gate_of(a, stem, regime)
    t["gate"] = "none" if g is None else f"{g[0] - g[1]}/{g[0]} equal"
    t["_keys"] = (frozenset(reads), frozenset(writes or {}))
    pq = {}
    for (q, _), v in reads.items():
        pq.setdefault(q, []).append(v["plan"])
    t["_per_query"] = {q: (float("nan") if any(x != x for x in v) else st.median(v)) for q, v in pq.items()}
    t["_gate_bad"] = None if g is None else (g[1] if g[0] == len(reads) else f"{g[0]} gate rows for {len(reads)} requests")
    return t, arm


def taus_of(a, regime):
    """({class: tau}, {class: the drifted repeat cells that void it}) from the noise files: the reads' file sets I02 / IC /
    IS (and I01), the writes' file sets IU (and I01, which holds the IU share); None where no reference exists"""
    tau = {c: None for c in CLASSES}
    void = {c: [] for c in CLASSES}

    def check(nz, path, want_regime, fam, classes):
        """a noise file must be the RULE's: the registered cells (no stand-in), suffix, floor, mult; tau recomputed from its gaps"""
        if nz.get("regime") != want_regime:
            raise SystemExit(f"{path} was measured for {nz.get('regime')!r}, not {want_regime!r}")
        want = [f"meta-{c}" for c in REPEAT_CELLS]
        if not a.no_gate and (sorted(nz.get("cells", [])) != sorted(want) or nz.get("stand_in")):
            raise SystemExit(f"{path}: the repeat cells are {nz.get('cells')} (stand-in {nz.get('stand_in')}); the RULE's are {want}")
        if nz.get("suffix") != REPEAT_SUFFIX or nz.get("floor") != FLOOR[fam] or nz.get("mult") != TAU_MULT:
            raise SystemExit(f"{path}: suffix / floor / mult {nz.get('suffix')} / {nz.get('floor')} / {nz.get('mult')} are not the RULE's "
                             f"{REPEAT_SUFFIX} / {FLOOR[fam]} / {TAU_MULT}")
        for c in classes:
            again = max(TAU_MULT * max(nz["class_total_gap"][c]), FLOOR[fam])
            if abs(again - nz["tau"][c]) > 1e-9:
                raise SystemExit(f"{path}: tau[{c}] {nz['tau'][c]} is not the RULE's value {again} from its own gaps")

    if a.noise:
        nz = json.load(open(a.noise))
        check(nz, a.noise, regime, regime_family(regime), ("I01", "I02", "IC", "IS"))
        for c in ("I01", "I02", "IC", "IS"):
            tau[c] = nz["tau"][c]
            void[c] += [f"reads repeat {v}" for v in nz.get("void", [])]
    if a.noise_writes:
        nz = json.load(open(a.noise_writes))
        check(nz, a.noise_writes, "writes", "writes", ("IU",))
        if nz.get("writes_regime") != a.writes_regime:
            raise SystemExit(f"{a.noise_writes} was measured on {nz.get('writes_regime')!r}, not {a.writes_regime!r}")
        tau["IU"] = nz["tau"]["IU"]
        for c in ("I01", "IU"):
            void[c] += [f"writes repeat {v}" for v in nz.get("void", [])]
    if a.tau is not None:                       # the smoke's fixed band (score refuses it)
        tau = {c: a.tau for c in CLASSES}
    return tau, void


def score(a):
    picks = json.load(open(a.picks))
    REF = picks["reference"]                   # the cell B multiplies, as the picks file was picked
    n = weights()
    out_dir = a.out
    os.makedirs(out_dir, exist_ok=True)
    arms, rows, words, missing_registered, per_q = {}, [], [], [], []
    smoke_mark = f"SMOKE: stand-ins {a.stand_in}" if a.stand_in else ""
    stems = {REF} | {r["best_single_within_B"]["cell"] for r in picks["picks"].values()} | \
            {r["spec"][:-5] for r in picks["picks"].values()} | \
            {r["near_miss_single"]["cell"] for r in picks["picks"].values() if r.get("near_miss_single")}
    for stem in sorted(stems):
        t, arm = arm_totals(a, stem, a.regime, n)
        if t is None:
            print(f"  {arm}: no {a.regime} capture -> every comparison with it is 'no verdict (missing capture)'")
            continue
        arms[stem] = t
        rows.append({"arm": arm, "label": label_of(stem, picks), "regime": a.regime,
                     **{k: (round(v, 3) if isinstance(v, float) else v) for k, v in t.items() if not k.startswith("_")}})
    if a.all_arms:                              # the figures' rows: every measured store, the engine formats included (no word reads them)
        extra = [st for st in all_arm_stems(a, a.regime) if st not in arms]
        for stem in extra:
            t, arm = arm_totals(a, stem, a.regime, n)
            if t is None:
                continue
            arms[stem] = t
            rows.append({"arm": arm, "label": label_of(stem, picks), "regime": a.regime,
                         **{k: (round(v, 3) if isinstance(v, float) else v) for k, v in t.items() if not k.startswith("_")}})
        print(f"  --all-arms: {len(extra)} more arms loaded for the figures: {' '.join(arm_of(a, st) for st in extra)}")
    per_store = [{"arm": arm_of(a, st), "label": label_of(st, picks), "regime": a.regime, "query": q, "class": qclass(q),
                  "median_ns": v} for st, t in sorted(arms.items()) for q, v in sorted(t["_per_query"].items())]
    tau, void = taus_of(a, a.regime)
    print(f"{a.regime}: {len(arms)} arms loaded; tau per class " + ", ".join(f"{c} {tau[c] if tau[c] is None else round(tau[c], 3)}" for c in CLASSES)
          + "".join(f"; {c} VOID (drift: {v})" for c, v in void.items() if v))
    for B, rec in picks["picks"].items():
        mix = rec["spec"][:-5]
        for other_role, cell in (("best_single", rec["best_single_within_B"]["cell"]), ("reference", REF)):
            same = other_role == "reference" and cell == rec["best_single_within_B"]["cell"]
            for cls in CLASSES:
                key = f"{cls}_median"
                m = o = float("nan"); reason = None
                if mix not in arms or cell not in arms:
                    reason = "missing capture"
                else:
                    tm, to = arms[mix], arms[cell]
                    m, o = tm[key], to[key]
                    if not a.no_gate and (tm["_gate_bad"] is None or to["_gate_bad"] is None):
                        reason = "no gate"
                    elif tm["_gate_bad"] or to["_gate_bad"]:
                        reason = "gate: the latency run's counts differ from the counts capture"
                    if reason is None and (tm["_coverage"] or to["_coverage"]):
                        reason = "coverage: " + "; ".join(f"{x} {t_['_coverage']}" for x, t_ in (("mix", tm), ("other", to)) if t_["_coverage"])
                    if reason is None and (tm["_keys"][0] != to["_keys"][0] or tm["_keys"][1] != to["_keys"][1]):
                        d = len(tm["_keys"][0] ^ to["_keys"][0]) + len(tm["_keys"][1] ^ to["_keys"][1])
                        reason = f"coverage: {d} (query, param) requests on one side only"
                    if reason is None and cls in ("I01", "IU") and (tm["writes_file"] == "NONE" or to["writes_file"] == "NONE"):
                        reason = "writes missing" + ("" if tm["writes_file"] == to["writes_file"] else " on one side")
                    if reason is None and void[cls]:
                        reason = "drift: " + "; ".join(void[cls]) + " moved beyond the bound"
                    if reason is None and tau[cls] is None:
                        reason = "no noise reference for this class"
                    if reason is None and cls == "I01" and tau["IU"] is None:
                        reason = "no writes noise reference (I01 holds the IU share)"
                w = f"no verdict ({reason})" if reason else word(m, o, tau[cls])
                allowed, kind = registered(B, other_role, cls, regime_family(a.regime))
                verdict = verdict_of(w, allowed, kind)
                if kind == "prediction" and reason == "missing capture":
                    missing_registered.append((B, other_role, cls))
                caveat = {}
                if cls == "IC" and not reason:                      # the caveat columns: the same word rule on IC without the named queries
                    tm, to = arms[mix], arms[cell]
                    for extra in ("IC_noIC6IC9", "IC_noIC12IC5"):   # (g): the totals and ratios; the word is NOT registered
                        caveat[f"word_{extra}"] = word(tm[f"{extra}_median"], to[f"{extra}_median"], tau[cls]) + " (not registered)"
                        caveat[f"other_over_mix_{extra}"] = to[f"{extra}_median"] / tm[f"{extra}_median"] if tm[f"{extra}_median"] else float("nan")
                        caveat[f"mix_ns_{extra}"], caveat[f"other_ns_{extra}"] = tm[f"{extra}_median"], to[f"{extra}_median"]
                carry = {}
                if cls == "IC" and mix in arms and cell in arms:
                    tm, to = arms[mix], arms[cell]
                    proxy = to["counts_ops_blocks_median_IC"] / tm["counts_ops_blocks_median_IC"] if tm["counts_ops_blocks_median_IC"] else float("nan")
                    carry["proxy_ratio_IC"] = proxy
                    for rung, key2 in (("plan", "IC_median"), ("kv", "IC_median_kv"), ("device", "IC_median_device")):
                        meas = (to.get(key2, float("nan")) / tm.get(key2, float("nan"))) if tm.get(key2) else float("nan")
                        cr = meas / proxy if proxy == proxy and proxy else float("nan")
                        carry[f"measured_ratio_{rung}"] = meas; carry[f"carry_{rung}"] = cr; carry[f"carry_word_{rung}"] = carry_word(cr)
                    if (B, cls, regime_family(a.regime)) in CARRY_SCOPE and kind == "prediction" and w == _F:   # (d): B = 0.5 / 1 IC steady, "mix faster" held
                        cd = carry.get("carry_device", float("nan"))
                        carry["carry_verdict"] = "carry no verdict (no device rung)" if cd != cd else ("carry held" if cd >= CARRY_MIN_DEVICE else "carry FALSIFIED (device rung below 0.5)")
                words.append({"B": B, "regime": a.regime, "class": cls, "mix": f"meta-{mix}", "mix_is": mix_is(rec),
                              "other_role": other_role, "other": f"meta-{cell}", "mix_ns": m, "other_ns": o,
                              "other_over_mix": (o / m if m and m == m else float("nan")), "tau": tau[cls], "word": w,
                              "kind": kind, "allowed": "|".join(allowed or ()), "verdict": verdict,
                              **{k: caveat.get(k, "") for k in ("word_IC_noIC6IC9", "other_over_mix_IC_noIC6IC9", "mix_ns_IC_noIC6IC9", "other_ns_IC_noIC6IC9",
                                                                 "word_IC_noIC12IC5", "other_over_mix_IC_noIC12IC5", "mix_ns_IC_noIC12IC5", "other_ns_IC_noIC12IC5")},
                              "carry_verdict": carry.get("carry_verdict", ""), "smoke": smoke_mark,
                              "counted_once": "no (B = 5 shares B = 3's stores)" if rec.get("shares_spec_with") else
                                              ("no (the best single IS the reference)" if same else
                                               ("no (one write measurement, counted in the steady file)" if cls == "IU" and regime_family(a.regime) != "steady" else "yes")),
                              **{k: carry.get(k, "") for k in ("proxy_ratio_IC", "measured_ratio_plan", "carry_plan", "carry_word_plan",
                                                               "measured_ratio_kv", "carry_kv", "carry_word_kv", "measured_ratio_device",
                                                               "carry_device", "carry_word_device")}})
                if cls in ("I01", "IC", "IS"):
                    print(f"  B={B:<4} vs {other_role:12s} {cell:32s} {cls:3s} {w:26s} (other / mix = {o / m if m and m == m else float('nan'):7.3f})"
                          f"  {kind}: {verdict}" + (f" [allowed: {' | '.join(allowed)}]" if allowed else "")
                          + ("  [= the reference: one comparison]" if same else "")
                          + (f"  {carry['carry_verdict']}" if carry.get("carry_verdict") else ""))
                    if caveat:
                        print(f"         IC without IC6 / IC9: other / mix {caveat['other_over_mix_IC_noIC6IC9']:.3f} ({caveat['word_IC_noIC6IC9']}); "
                              f"without IC12 / IC5 (the chunk-cache caveat): {caveat['other_over_mix_IC_noIC12IC5']:.3f} ({caveat['word_IC_noIC12IC5']})")
                if carry:
                    print(f"         IC magnitude: proxy other/mix {carry['proxy_ratio_IC']:.3f}; measured plan {carry['measured_ratio_plan']:.3f} -> carry "
                          f"{carry['carry_plan']:.3f} ({carry['carry_word_plan']}); kv {carry['carry_kv']:.3f} ({carry['carry_word_kv']}); "
                          f"device {carry['carry_device']:.3f} ({carry['carry_word_device']})")
            if mix in arms and cell in arms:   # (h) the per-query rows: the caveat queries on every pair, every read query at B = 3 / 5
                tm, to = arms[mix], arms[cell]
                for q in sorted(tm["_per_query"]):
                    if q in ("ic12", "ic5") or B in ("3", "5"):
                        mq, oq = tm["_per_query"][q], to["_per_query"].get(q, float("nan"))
                        per_q.append({"B": B, "regime": a.regime, "mix": f"meta-{mix}", "other_role": other_role, "other": f"meta-{cell}", "query": q,
                                      "mix_ns": mq, "other_ns": oq, "other_over_mix": oq / mq if mq and mq == mq else float("nan"),
                                      "why": "the chunk-cache caveat (IC12 / IC5 hold up to 84 MB outside the budget on Chunked adjacency)" if q in ("ic12", "ic5") else "B = 3's findings: per query, no verdict"})
            if mix in arms and cell in arms:   # the measured counts' order under the latency's aggregator (the median), for the record
                tm, to = arms[mix], arms[cell]
                print(f"         counts (median over params) of these executions: ops {order_word(tm['counts_ops_median'], to['counts_ops_median'])} "
                      f"({to['counts_ops_median'] / tm['counts_ops_median'] if tm['counts_ops_median'] else float('nan'):.3f}x), "
                      f"ops+est_blocks {order_word(tm['counts_ops_blocks_median'], to['counts_ops_blocks_median'])} "
                      f"({to['counts_ops_blocks_median'] / tm['counts_ops_blocks_median'] if tm['counts_ops_blocks_median'] else float('nan'):.3f}x), "
                      f"IS ops+est_blocks {order_word(tm['counts_ops_median_IS'] + tm['counts_est_blocks_median_IS'], to['counts_ops_median_IS'] + to['counts_est_blocks_median_IS'])}")
    with open(os.path.join(out_dir, "w16_latency.csv"), "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=sorted({k for r in rows for k in r}, key=lambda k: (k not in ("arm", "label", "regime"), k)))
        wr.writeheader(); wr.writerows(rows)
    with open(os.path.join(out_dir, "w16_per_store_query.csv"), "w", newline="") as fh:   # per store and query: the median over its params
        wr = csv.DictWriter(fh, fieldnames=["arm", "label", "regime", "query", "class", "median_ns"]); wr.writeheader(); wr.writerows(per_store)
    with open(os.path.join(out_dir, "w16_words.csv"), "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(words[0])); wr.writeheader(); wr.writerows(words)
    if per_q:                                   # (h) printed tables, no verdict
        with open(os.path.join(out_dir, "w16_per_query.csv"), "w", newline="") as fh:
            wr = csv.DictWriter(fh, fieldnames=list(per_q[0])); wr.writeheader(); wr.writerows(per_q)
        print(f"per-query rows (no verdict; the caveat queries IC12 / IC5 on every pair, every read query at B = 3 / 5): {len(per_q)} -> w16_per_query.csv")
        for r in per_q:
            if r["query"] in ("ic12", "ic5"):
                print(f"    B={r['B']:<4} vs {r['other_role']:12s} {r['query']:5s} other / mix {r['other_over_mix']:7.3f}  ({r['why'][:28]})")
    limits = []                                 # (h) the built bytes against the limit: the measured executions' bytes, mix / reference, vs B
    for B, rec in picks["picks"].items():
        mix = rec["spec"][:-5]
        if mix in arms and REF in arms and arms[REF]["counts_bytes_mean"]:
            ratio = arms[mix]["counts_bytes_mean"] / arms[REF]["counts_bytes_mean"]
            limits.append({"B": B, "mix": f"meta-{mix}", "mix_bytes": arms[mix]["counts_bytes_mean"], "reference_bytes": arms[REF]["counts_bytes_mean"],
                           "mix_over_reference": ratio, "fits": "yes" if ratio <= float(B) else "NO (over the limit on the measured executions)"})
    if limits:
        with open(os.path.join(out_dir, "w16_bytes_vs_limit.csv"), "w", newline="") as fh:
            wr = csv.DictWriter(fh, fieldnames=list(limits[0])); wr.writeheader(); wr.writerows(limits)
        print("the built bytes against the limit (the measured executions' total bytes per 100,000 updates, mean over params): "
              + "; ".join(f"B={r['B']} {r['mix_over_reference']:.3f}x the reference ({r['fits']})" for r in limits) + " -> w16_bytes_vs_limit.csv")
    verdicts = [w for w in words if w["word"] in WORDS]
    indep = [w for w in verdicts if w["counted_once"] == "yes" and w["class"] in ("IC", "IS", "IU")]
    print(f"{len(words)} words ({len(verdicts)} with a verdict, {len(words) - len(verdicts)} no verdict; "
          f"{len(indep)} independent IC / IS / IU verdicts) -> {out_dir}/w16_words.csv; {len(rows)} arm rows -> w16_latency.csv")
    preds = [w for w in words if w["kind"] == "prediction" and w["counted_once"] == "yes"]   # distinct stores and measurements
    held = [w for w in preds if w["verdict"] == "held"]; fals = [w for w in preds if w["verdict"] == "FALSIFIED"]
    findings = [w for w in words if w["verdict"].startswith("a FINDING")]
    cf = [w for w in words if w["carry_verdict"].startswith("carry FALSIFIED")]
    print(f"PREDICTIONS ({regime_family(a.regime)}{'; ' + smoke_mark if smoke_mark else ''}): {len(preds)} registered rows over distinct stores and measurements: {len(held)} held, {len(fals)} FALSIFIED, "
          f"{len(preds) - len(held) - len(fals)} no verdict; descriptive findings outside the stated pair: {len(findings)}; "
          f"carry FALSIFIED: {len(cf)}; registered rows with NO CAPTURE: {len(set(missing_registered))}")
    for w in fals + findings + cf:
        print(f"    {w['verdict'] if w['verdict'] != 'held' else w['carry_verdict']}: B={w['B']} vs {w['other_role']} {w['other']} {w['class']} -> {w['word']} (other / mix {w['other_over_mix']:.3f})")
    if not verdicts:
        print("SCORE COVERED 0 PAIRS: a failure, not a pass"); return 1
    if missing_registered:
        print("REGISTERED ROWS WITHOUT A CAPTURE (a failure, not a pass): " + ", ".join(f"B={b} {r} {c}" for b, r, c in sorted(set(missing_registered)))); return 2
    return 0


def hit_rate(a, arm, regime):
    """the mean block-cache hit rate of the steady regime's MEASURED passes (cpplatency's steady-passes.csv), or NaN"""
    for sub in sub_roots(a):
        p = os.path.join(_wl(a.latency_root, sub), regime, arm, "steady-passes.csv")
        if os.path.exists(p):
            rows = [r for r in capture_layout.reader(open(p)) if r["phase"] == "measured" and r["arm"] == arm]
            return st.mean(float(r["hit_rate"]) for r in rows) if rows else float("nan")
    return float("nan")


def passes(a):
    """AFTER the words: per store the pass-to-pass wall spread of the steady regime's measured passes against the noise floor;
    a store above the floor is named beside its word rows (changes no word) -> pass_spread.csv"""
    floor = json.load(open(a.noise))["floor"] if a.noise else FLOOR[regime_family(a.regime)]
    rows = []
    for sub in sub_roots(a):
        for suffix in ("", a.suffix):
            d = os.path.join(_wl(a.latency_root, sub), a.regime + suffix)
            if not os.path.isdir(d):
                continue
            for arm in sorted(os.listdir(d)):
                pth = os.path.join(d, arm, "steady-passes.csv")
                if not os.path.exists(pth):
                    continue
                prs = [r for r in capture_layout.reader(open(pth)) if r["arm"] == arm]
                warm = [r for r in prs if r["phase"] == "warm"]; meas = [r for r in prs if r["phase"] == "measured"]
                walls = [int(r["wall_ns_sum"]) for r in meas]; hr = [float(r["hit_rate"]) for r in meas]
                whr = [float(r["hit_rate"]) for r in warm]
                spread = (max(walls) / min(walls) - 1) if walls and min(walls) > 0 else float("nan")
                rows.append({"arm": arm, "copy": "repeat" if suffix else "chain", "regime": a.regime, "warm_passes": len(warm),
                             "converged": "yes" if len(whr) >= 2 and abs(whr[-1] - whr[-2]) < 0.005 else "no (not converged: recorded, the cell kept)",
                             "measured_passes": len(meas), "hit_rate_measured": " ".join(f"{x:.4f}" for x in hr),
                             "hit_rate_spread": (max(hr) - min(hr)) if hr else float("nan"),
                             "wall_sum_s_measured": " ".join(f"{w / 1e9:.1f}" for w in walls), "wall_spread": spread, "floor": floor,
                             "above_floor": "yes" if spread > floor else "no"})
    if not rows:
        print(f"passes: no steady-passes.csv under {a.latency_root} for {a.regime} (a regime without passes): nothing to check"); return 0
    print(f"pass-to-pass wall spread per store ({a.regime}; the measured passes' wall sums over the 95 cells, max / min - 1) against the floor {floor} "
          f"(the floor's premise: the per-pass wall spread was 5-10% in the 9-pass record); the hit rate per measured pass beside it:")
    for r in rows:
        print(f"  {r['arm']:36s} {r['copy']:6s} warm {r['warm_passes']} ({r['converged'][:3]}) measured {r['measured_passes']}: hit {r['hit_rate_measured']} "
              f"(spread {r['hit_rate_spread']:.4f}); wall s {r['wall_sum_s_measured']} -> spread {r['wall_spread']:.3f} "
              f"{'ABOVE the floor' if r['above_floor'] == 'yes' else 'within the floor'}")
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "pass_spread.csv"), "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(rows[0])); wr.writeheader(); wr.writerows(rows)
    above = {r["arm"]: r["wall_spread"] for r in rows if r["copy"] == "chain" and r["above_floor"] == "yes"}
    print(f"{len(rows)} stores -> {a.out}/pass_spread.csv; above the floor: {len(above)}" + (": " + " ".join(sorted(above)) if above else ""))
    # beside the words: the rows whose stores are above the floor get the fact in a column; every other row an empty one
    for name, cols in (("w16_words.csv", (("mix", "mix"), ("other_role", "other"))), ("engine_arms.csv", (("cell", "cell"), ("format", "format")))):
        wp = os.path.join(a.out, name)
        if not os.path.exists(wp):
            continue
        words = list(capture_layout.reader(open(wp)))
        n = 0
        for w in words:
            notes = []
            for role_col, arm_col in cols:
                arm = w.get(arm_col, "")
                arm_full = arm if arm in above else f"meta-{arm}" if f"meta-{arm}" in above else None
                if arm_full:
                    notes.append(f"{w.get(role_col, role_col) if role_col != arm_col else role_col} {arm_full}: pass spread {above[arm_full]:.3f} above the floor {floor}")
            w["pass_spread"] = "; ".join(notes); n += bool(notes)
        with open(wp, "w", newline="") as fh:
            wr = csv.DictWriter(fh, fieldnames=list(words[0]) if words else ["pass_spread"]); wr.writeheader(); wr.writerows(words)
        print(f"  {name}: column pass_spread added, {n} of {len(words)} rows name a store above the floor (no word changed)")
    return 0


def engine(a):
    """the engine-format arms against their nearest cells: per pair and class (I02 = every read, IC, IS) the cell's total over
    the format's, the word under the class's tau (the same noise rule), both arms' steady hit rates, and the counts' ops ratio
    beside it; -> <out>/engine_arms.csv"""
    n = weights()
    pairs = [tuple(p.split("=", 1)) for p in a.pairs.split(",")] if a.pairs else list(ENGINE_PAIRS)
    tau, void = taus_of(a, a.regime)
    os.makedirs(a.out, exist_ok=True)
    rows = []
    not_timed = set(a.not_timed.split(",")) if a.not_timed else set()
    unknown = not_timed - {f for f, _ in pairs}
    if unknown:
        raise SystemExit(f"--not-timed {sorted(unknown)}: no such format among the pairs {[f for f, _ in pairs]}")
    for fmt, cell in pairs:
        tc, _ = arm_totals(a, cell, a.regime, n)
        tf, _ = arm_totals(a, fmt, a.regime, n)
        if tc is None or tf is None:
            # --not-timed names a format this run left out on purpose (e.g. a re-measure that times the Dgraph format at
            # its 512 KB split only): its row says so and is no failure; any other missing capture still is
            w0 = "not timed in this run" if tf is None and tc is not None and fmt in not_timed else "no verdict (missing capture)"
            print(f"  {fmt} vs {cell}: no {a.regime} capture on {'the cell' if tc is None else 'the format'} -> {w0}")
            reg0 = ENGINE_PREDICTIONS.get((fmt, cell))
            kind0 = "prediction" if reg0 and regime_family(a.regime) in reg0[1] else "unregistered"
            rows.append({"format": fmt, "cell": cell, "regime": a.regime, "class": reg0[0] if reg0 else "", "word": w0,
                         "kind": kind0, "allowed": "|".join(ENGINE_WORDS[x] for x in reg0[2]) if reg0 else "", "verdict": w0})
            continue
        hr_c, hr_f = hit_rate(a, f"meta-{cell}", a.regime), hit_rate(a, fmt, a.regime)
        reg = ENGINE_PREDICTIONS.get((fmt, cell))
        for cls in ("W17_noIC3", "I02", "IC", "IS"):
            c, f = tc[f"{cls}_median"], tf[f"{cls}_median"]
            reason = None
            if not a.no_gate and (tc["_gate_bad"] is None or tf["_gate_bad"] is None):
                reason = "no gate"
            elif tc["_gate_bad"] or tf["_gate_bad"]:
                reason = "gate: the latency run's counts differ from the counts capture"
            if reason is None and (tc["_coverage"] or tf["_coverage"]):
                reason = "coverage: " + "; ".join(f"{x} {t_['_coverage']}" for x, t_ in (("cell", tc), ("format", tf)) if t_["_coverage"])
            if reason is None and tc["_keys"][0] != tf["_keys"][0]:
                reason = f"coverage: {len(tc['_keys'][0] ^ tf['_keys'][0])} requests on one side only"
            vcls = "I02" if cls == "W17_noIC3" else cls            # the registered class is voided by I02's repeat drift
            if reason is None and void.get(vcls):
                reason = "drift: " + "; ".join(void[vcls])
            tau_cls = tau.get("I02") if cls == "W17_noIC3" else tau.get(cls)   # the same band as I02: the repeat's I02 gap
            if reason is None and tau_cls is None:
                reason = "no noise reference for this class"
            wm = None if reason else word(c, f, tau_cls)               # the word in the mix vocabulary (the RULE's)
            w = f"no verdict ({reason})" if reason else ENGINE_WORDS[wm]   # printed as cell faster / slower
            kind, allowed, verdict = "unregistered", None, "reported"
            if reg and cls == reg[0] and regime_family(a.regime) in reg[1]:
                kind, allowed = "prediction", tuple(ENGINE_WORDS[x] for x in reg[2])
                # the verdict is read in the RULE's vocabulary: verdict_of knows "mix faster", not "cell faster" (a row whose
                # registered word is "cell faster" prints held / FALSIFIED, not its word)
                verdict = verdict_of(wm, reg[2], kind) if wm is not None else w
                if regime_family(a.regime) == "steady":          # the second falsifier, evaluated whatever the word
                    gap = abs(hr_c - hr_f) if hr_c == hr_c and hr_f == hr_f else float("nan")
                    if gap != gap:
                        verdict = "no verdict (no hit rate: steady-passes.csv missing or empty on one side)"
                    elif gap >= ENGINE_HIT_RATE_GAP:
                        verdict = f"FALSIFIED (hit-rate gap {gap:.3f} >= {ENGINE_HIT_RATE_GAP}" + ("; the word too" if verdict == "FALSIFIED" else "") + ")"
            elif cls == "I02":
                kind, verdict = "descriptive", "descriptive (I02 with IC3: the day-bucket index makes IC3 another query)"
            key = "counts_ops_median" + ("" if cls in ("I02", "W17_noIC3") else "_" + cls)   # an engine arm has no writes: its I01 counts are its reads'
            ops_c, ops_f = tc[key], tf[key]
            rows.append({"format": fmt, "cell": cell, "regime": a.regime, "class": cls, "cell_ns": c, "format_ns": f,
                         "cell_over_format": c / f if f else float("nan"), "tau": tau_cls, "word": w,
                         "kind": kind, "allowed": "|".join(allowed or ()), "verdict": verdict,
                         "hit_rate_cell": hr_c, "hit_rate_format": hr_f, "ops_format_over_cell": ops_f / ops_c if ops_c else float("nan"),
                         "gate_cell": tc["gate"], "gate_format": tf["gate"]})
            print(f"  {fmt:7s} vs {cell:28s} {cls:10s} {w:26s} cell/format {c / f if f else float('nan'):6.3f}  hit rate cell {hr_c:.3f} format {hr_f:.3f}"
                  f"  counts ops format/cell {ops_f / ops_c if ops_c else float('nan'):6.3f}  {kind}: {verdict}")
    with open(os.path.join(a.out, "engine_arms.csv"), "w", newline="") as fh:
        for r in rows:
            r["format_label"], r["cell_label"] = label_of(r["format"]), label_of(r["cell"])
        wr = csv.DictWriter(fh, fieldnames=sorted({k for r in rows for k in r}, key=lambda k: (k not in ("format", "cell", "regime", "class"), k)))
        wr.writeheader(); wr.writerows(rows)
    verdicts = [r for r in rows if r.get("word") in ENGINE_WORDS.values()]
    print(f"{len(rows)} rows ({len(verdicts)} with a verdict) -> {a.out}/engine_arms.csv")
    preds = [r for r in rows if r.get("kind") == "prediction"]
    print(f"PREDICTIONS (engine arms, {regime_family(a.regime)}): {len(preds)} registered rows: "
          + ", ".join(f"{r['format']} vs {r['cell']} {r['class']} -> {r['verdict']}" for r in preds))
    if not verdicts:
        print("ENGINE COVERED 0 PAIRS: a failure, not a pass"); return 1
    if any(r.get("kind") == "prediction" and r["word"].startswith("no verdict (missing") for r in rows):
        print("A REGISTERED ENGINE ROW WITHOUT A CAPTURE: a failure, not a pass"); return 2
    return 0


def ic10_plan(cells, spec_dirs):
    """[(store, point body, scan body)] of the ic10-bodies stage: the stores among `cells` whose recorded IC10 pick (the
    deciding scale's row, pick_record.load) is the scan; the point body is the planner's own body on that store, v1's
    selector over its spec file (cells.cell_selection, at the deciding scale: the anchor the record's ratio is taken
    against). The spec file: <spec dir>/<store>.json, else the grid's. Refused when a store has no IC10 row or no spec"""
    from kvevaluator.layouts import GRID
    from kvevaluator.plan_choice import pick_record
    from kvevaluator.plan_choice.cells import cell_selection
    rec = pick_record.load()
    out = []
    for c in cells:
        row = rec.entry(RECORD_WORKLOAD, c, "ic10")
        if row is None:
            raise SystemExit(f"{c}: no ic10 row in the record ({os.path.relpath(rec.path, EXP)})")
        if not row["body"].startswith(IC10_SCAN):
            continue
        specs = [p for p in [os.path.join(d, f"{c}.json") for d in spec_dirs] + [os.path.join(EXP, GRID, f"{c}.json")]
                 if os.path.exists(p)]
        if not specs:
            raise SystemExit(f"{c}: no spec file in {spec_dirs} or the grid: its planner's body cannot be named")
        point = cell_selection(RECORD_WORKLOAD, specs[0], str(rec.decided_at), ["ic10"]).get("ic10")
        if not point:
            raise SystemExit(f"{c}: the selector takes no IC10 body on {specs[0]}")
        if any(ch in b for b in (point, row["body"]) for ch in ", \t"):
            raise SystemExit(f"{c}: an IC10 body name with a comma or a space ({point!r}, {row['body']!r}): the stage splits on them")
        out.append((c, point, row["body"]))
    return out


def ic10(a):
    """the ic10-bodies stage (warm, IC10 alone, two bodies, two copies): per store and body the median over params of the
    per-param median over the measured iterations, both copies' iterations pooled (warm.json's totals.wall_ns_all; the
    steps.csv wall is the median run's only); scan / point; held when inside IC10_BAND on EVERY store the record picks the
    scan on among the stage's stores (the expected set: the stage's plan, ic10_plan's bodies.csv; absent or incomplete
    = rc 2) -> <out>/ic10_bodies.csv"""
    root = os.path.join(a.latency_root, "ic10-bodies")
    if not os.path.isdir(root):
        print(f"ic10: no {root}: the ic10-bodies stage has not run"); return 2
    plan = os.path.join(root, IC10_PLAN)
    if not os.path.exists(plan):
        print(f"ic10: no {plan}: the ic10-bodies stage wrote no plan (its stores are unknown)"); return 2
    with open(plan, newline="") as fh:
        expected = sorted(r["store"] for r in csv.DictReader(fh))   # the stores the stage times: the record's scan picks
    walls = {}                                                     # (store, body) -> {param: [every measured iteration's wall, both copies]}
    for body in ("point", "scan"):
        for sub in ("grid", "mixes"):
            for copy in IC10_COPIES:
                for out in sorted(glob.glob(os.path.join(_wl(os.path.join(root, body), sub), IC10_REGIME + copy, "meta-*"))):
                    arm = os.path.basename(out)                        # a cpplatency --out and its one arm, meta-<store>
                    for idx, path in raw_files(out, arm, "warm").get("ic10", {}).items():
                        all_ = (json.load(open(path)).get("totals") or {}).get("wall_ns_all") or []
                        walls.setdefault((arm[5:], body), {}).setdefault(idx, []).extend(all_)
    stores = sorted({s for s, _ in walls})
    os.makedirs(a.out, exist_ok=True)
    rows, bad, incomplete = [], [], []
    for store in stores:
        med = {}
        for body in ("point", "scan"):
            per = walls.get((store, body), {})
            if len(per) != 5 or any(len(v) < 2 * len(IC10_COPIES) or any(x <= 0 for x in v) for v in per.values()):
                incomplete.append((store, body)); med[body] = float("nan"); continue
            med[body] = st.median(st.median(v) for v in per.values())   # per param the median over the pooled iterations, then over params
        ratio = med["scan"] / med["point"] if med["point"] == med["point"] and med["point"] else float("nan")
        v = "no verdict (incomplete capture)" if ratio != ratio else ("held" if IC10_BAND[0] <= ratio <= IC10_BAND[1] else "FALSIFIED")
        if v == "FALSIFIED":
            bad.append(store)
        rows.append({"store": store, "point_ns": med["point"], "scan_ns": med["scan"], "scan_over_point": ratio, "band_lo": IC10_BAND[0], "band_hi": IC10_BAND[1], "verdict": v})
        print(f"  {store:32s} point {med['point'] / 1e6 if med['point'] == med['point'] else float('nan'):9.2f} ms  scan {med['scan'] / 1e6 if med['scan'] == med['scan'] else float('nan'):9.2f} ms  scan / point {ratio:6.3f}  {v}")
    with open(os.path.join(a.out, "ic10_bodies.csv"), "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["store"]); wr.writeheader(); wr.writerows(rows)
    absent = [c for c in expected if c not in stores]; extra = [c for c in stores if c not in expected]
    print(f"PREDICTIONS (ic10-bodies): {len(stores)} stores found, {len(expected)} expected from the stage's plan ({', '.join(expected)}); "
          f"the claim 'scan / point within {IC10_BAND[0]}-{IC10_BAND[1]} on every store' is "
          + ("FALSIFIED on " + ", ".join(bad) if bad else ("no verdict (" + "; ".join(x for x in [("absent: " + ", ".join(absent)) if absent else "", ("incomplete: " + ", ".join(f"{s}/{b}" for s, b in incomplete)) if incomplete else "", ("not in the stage's plan: " + ", ".join(extra)) if extra else ""] if x) + ")" if absent or incomplete or extra else "held"))
          + f" -> {a.out}/ic10_bodies.csv")
    if not stores:
        print("IC10 COVERED 0 STORES: a failure, not a pass"); return 1
    return 2 if absent or incomplete or extra else 0


def predictions(a):
    """the PREDICTIONS table as the pre-registration states it: the diff against the frozen text is read on this print"""
    print("| B | comparator | class | regimes | allowed words | kind |\n|---|---|---|---|---|---|")
    for B, role, cls, fam, allowed, kind in PREDICTIONS:
        print(f"| {B} | {role} | {cls} | {'every' if fam == '*' else fam} | {' / '.join(allowed)} | {kind} |")
    print(f"\ncarry: where 'mix faster' is a held prediction (IC), carry at the device rung >= {CARRY_MIN_DEVICE} or 'carry FALSIFIED'")
    for (fmt, cell), (cls, fams, allowed) in ENGINE_PREDICTIONS.items():
        print(f"engine arm {fmt} vs {cell}: {cls}, {' and '.join(fams)}: {' / '.join(ENGINE_WORDS[x] for x in allowed)} (prediction); "
              f"steady hit-rate gap >= {ENGINE_HIT_RATE_GAP} falsifies too" + ("; I02 with IC3 descriptive" if cls == "W17_noIC3" else ""))
    print(f"ic10-bodies ({IC10_REGIME}, copies {len(IC10_COPIES)}): scan / point within {IC10_BAND[0]}-{IC10_BAND[1]} on every store where the scan is picked (prediction)")
    print(f"tie band: tau[class] = max({TAU_MULT} x the repeat gap, floor {FLOOR}); drift bound {DRIFT_BOUND}; repeat cells {REPEAT_CELLS}; classes {CLASSES} + caveat columns {CLASSES_EXTRA}")
    return 0


def noise(a):
    """the frozen noise rule on the repeat cells: per cell the start copy (regime) vs the end copy (regime + suffix); the
    per-CLASS total gap per copy pair -> tau per class; the per-request drift and gaps for the record"""
    n = weights()
    fam = "writes" if a.writes_only else regime_family(a.regime)
    classes = ("IU",) if a.writes_only else ("I01", "I02", "IC", "IS")
    gaps, drifts, cls_gap, names = [], [], {c: [] for c in classes}, []
    for stem in a.cells.split(","):
        arm = arm_of(a, stem)
        if a.writes_only:
            rp = os.path.join(a.writes_root, a.writes_regime, arm, "steps.csv")
            rp2 = os.path.join(a.writes_root, a.writes_regime + a.suffix, arm, "steps.csv")
        else:
            rp, _ = arm_dirs(a, arm, a.regime)
            rp2, _ = arm_dirs(a, arm, a.regime + a.suffix)
        if not (os.path.exists(rp) and os.path.exists(rp2)):
            print(f"  {arm}: a repeat copy is missing ({rp2 if os.path.exists(rp) else rp}) -> NO NOISE REFERENCE"); return 2
        da = load_writes(rp) if a.writes_only else load_reads(rp, arm)
        db = load_writes(rp2) if a.writes_only else load_reads(rp2, arm)
        common = [k for k in da if k in db and da[k]["plan"] > 0 and db[k]["plan"] > 0]
        if len(common) < (MIN_COMMON_WRITES if a.writes_only else MIN_COMMON):
            print(f"  {arm}: only {len(common)} common requests -> NO NOISE REFERENCE"); return 2
        drift = st.median([db[k]["plan"] / da[k]["plan"] for k in common])
        g = sorted(abs(da[k]["plan"] - db[k]["plan"] / drift) / max(da[k]["plan"], db[k]["plan"] / drift) for k in common)
        q90 = g[int(0.9 * (len(g) - 1))]
        ta = class_totals(None if a.writes_only else da, da if a.writes_only else None, n)
        tb = class_totals(None if a.writes_only else db, db if a.writes_only else None, n)
        for c in classes:
            cls_gap[c].append(abs(ta[c] - tb[c]) / max(ta[c], tb[c]) if max(ta[c], tb[c]) > 0 else 0.0)
        gaps.append(q90); drifts.append(drift); names.append(arm)
        print(f"  {arm}: {len(common)} requests; drift (median end / start) {drift:.3f}; de-trended per-request gap median "
              f"{st.median(g):.3f} q90 {q90:.3f} max {g[-1]:.3f}; class-total gaps " + ", ".join(f"{c} {cls_gap[c][-1]:.4f}" for c in classes))
    void = [nm for nm, d in zip(names, drifts) if abs(d - 1) > DRIFT_BOUND]
    tau = {c: max(TAU_MULT * max(cls_gap[c]), FLOOR[fam]) for c in classes}
    print(f"  RULE: tau[class] = max({TAU_MULT} x the max class-total gap, floor {FLOOR[fam]}): "
          + ", ".join(f"{c} {tau[c]:.3f} (gap {max(cls_gap[c]):.4f})" for c in classes) + f"; per-request q90 {max(gaps):.3f} (the record)")
    print(f"  DRIFT CHECK (bound {DRIFT_BOUND}): " + (f"VOID -- {', '.join(void)}" if void else "pass: " + ", ".join(f"{nm} {d:.3f}" for nm, d in zip(names, drifts))))
    out = {"regime": "writes" if a.writes_only else a.regime, "writes_regime": a.writes_regime if a.writes_only else None,
           "suffix": a.suffix, "cells": names, "stand_in": a.stand_in, "drift": drifts,
           "q90_request_gap": gaps, "class_total_gap": cls_gap, "tau": tau, "floor": FLOOR[fam], "mult": TAU_MULT, "void": void}
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "noise-writes.json" if a.writes_only else f"noise-{a.regime}.json")
    json.dump(out, open(path, "w"), indent=1)
    print(f"  -> {path}" + ("  VERDICT_VOID=drift" if void else ""))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("mode", choices=["predict", "noise", "score", "smoke", "engine", "ic10-plan", "ic10", "predictions", "passes"])
    ap.add_argument("--spec-dir", action="append", default=[], help="ic10-plan: a dir of the stores' spec files (repeatable; "
                    "the grid's last)")
    ap.add_argument("--pairs", default=None, help="engine: format=cell,... (default ENGINE_PAIRS)")
    ap.add_argument("--not-timed", default=None, help="engine: formats this run did not time, comma list: their rows say 'not timed "
                    "in this run' and are no failure (a format NOT named here and without a capture still exits 2)")
    ap.add_argument("--picks", default=os.path.join(EXP, "inputs", "specs", "mixes_w16-picks.json"))
    ap.add_argument("--root", default=None, help="predict: the layout-choice dir (a snapshot's wops_bytes4k)")
    ap.add_argument("--latency-root", default=None, help="the latency root: {grid,mixes}/interactive/<regime>/<arm>/ (+ gate.csv per root)")
    ap.add_argument("--writes-root", default=None, help="the writes root: <writes-root>/<writes regime>/<arm>/steps.csv (default <latency-root>/writes)")
    ap.add_argument("--regime", default=None, help="the reads regime dir name, e.g. steady-307mb-p2 | warm-p0 | cold-p2 (a dated root: cold-p3)")
    ap.add_argument("--writes-regime", default="durable-p1-noflush", help="the writes regime dir (IU1-8 at the apply wall); '' = none")
    ap.add_argument("--suffix", default="-repeat", help="noise: the end copy's dir suffix")
    ap.add_argument("--cells", default=None, help="noise: the repeat cells' spec stems, comma list")
    ap.add_argument("--other-repeat-cells", action="store_true",
                    help="noise: --cells are another study's repeats (store_config's re-time), not REPEAT_CELLS; the caller labels the file")
    ap.add_argument("--writes-only", action="store_true", help="noise: the writes capture's repeat (the IU class)")
    ap.add_argument("--noise", default=None, help="score: the reads noise file (tau per class, drift void)")
    ap.add_argument("--noise-writes", default=None, help="score: the writes noise file (the IU tau)")
    ap.add_argument("--tau", type=float, default=None, help="smoke only: one fixed band for every class")
    ap.add_argument("--no-gate", action="store_true", help="smoke only: a snapshot without gate.csv")
    ap.add_argument("--stand-in", default=None, help="smoke: stem=cell,... (or stem=arm:<name>) a measured arm that plays each mix / single")
    ap.add_argument("--out", required=False, help="the analysis dir (required for noise / score / smoke; never under a snapshot)")
    ap.add_argument("--all-arms", action="store_true", help="score: every measured arm's totals row (the figures), not only the pairs'")
    ap.add_argument("--allow-mixed-protocol", action="store_true",
                    help="read captures timed under different request protocols (measure/capture_protocol) instead of refusing; "
                         "each capture's protocol goes to <out>/capture_protocol.json")
    a = ap.parse_args(argv)
    if a.writes_regime == "":
        a.writes_regime = None
    if a.writes_root is None and a.latency_root:
        a.writes_root = os.path.join(a.latency_root, "writes")
    if a.mode not in ("predict", "predictions"):
        if not a.out:
            raise SystemExit(f"{a.mode} needs --out (the analysis dir; a snapshot is never written into)")
        d = os.path.abspath(a.out)
        while d != os.path.dirname(d):
            if os.path.exists(os.path.join(d, "snapshot-meta.json")):
                raise SystemExit(f"--out {a.out} lies under the snapshot {d}: a snapshot is read, never written into")
            d = os.path.dirname(d)
    if a.latency_root and (a.mode == "ic10" or (a.mode in ("noise", "score", "smoke", "engine", "passes") and a.regime)):
        check_protocol(a)
    if a.mode == "predictions":
        return predictions(a)
    if a.mode == "predict":
        return predict(a)
    if a.mode == "ic10-plan":
        if not a.cells:
            raise SystemExit("ic10-plan needs --cells (the stage's stores)")
        plan = ic10_plan(a.cells.split(","), a.spec_dir)
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", newline="") as fh:
            wr = csv.writer(fh, lineterminator="\n"); wr.writerow(["store", "point", "scan"]); wr.writerows(plan)
        print(f"ic10-plan: {len(plan)} of {len(a.cells.split(','))} stores pick the scan -> {a.out}")
        return 0
    if a.mode == "ic10":
        if not a.latency_root:
            raise SystemExit("ic10 needs --latency-root (the run's root; its ic10-bodies/ subtree)")
        return ic10(a)
    if a.mode == "passes":
        if not a.latency_root or not a.regime:
            raise SystemExit("passes needs --latency-root and --regime (and --noise for the floor; --out = the analysis dir of the words)")
        return passes(a)
    if a.mode == "engine":
        if a.tau is not None or a.no_gate or a.stand_in or a.pairs:
            raise SystemExit("engine takes the registered rule and pairs only: --tau / --no-gate / --stand-in / --pairs are refused (as score)")
        if not a.noise:
            raise SystemExit("engine needs --noise (the repeat measured FIRST)")
        return engine(a)
    if a.mode == "noise":
        if a.stand_in:
            raise SystemExit("noise takes the registered repeat cells only: --stand-in is the smoke's")
        if a.cells is None:
            a.cells = ",".join(REPEAT_CELLS)
        elif not a.other_repeat_cells and sorted(a.cells.split(",")) != sorted(REPEAT_CELLS):
            raise SystemExit(f"noise: --cells {a.cells} are not the RULE's repeat cells {REPEAT_CELLS}")
        return noise(a)
    if a.mode == "score":
        if a.tau is not None or a.no_gate or a.stand_in:
            raise SystemExit("score takes the registered rule only: --tau / --no-gate / --stand-in are the smoke's")
        if not a.noise:
            raise SystemExit("score needs --noise (the repeat measured FIRST)")
        return score(a)
    print("SMOKE on seen data: the stand-ins play the mixes; the writes come from --writes-root if given")
    rc = 0
    if a.cells and a.tau is None and not a.noise:
        rc = noise(a)
        a.noise = os.path.join(a.out, f"noise-{a.regime}.json") if rc == 0 else None
        if a.writes_root and a.writes_regime and os.path.isdir(os.path.join(a.writes_root, a.writes_regime + a.suffix)):
            a.writes_only = True
            rc2 = noise(a)
            a.writes_only = False
            a.noise_writes = os.path.join(a.out, "noise-writes.json") if rc2 == 0 else None
    return score(a) or rc


if __name__ == "__main__":
    sys.exit(main())
