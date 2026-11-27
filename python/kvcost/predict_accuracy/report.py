"""Summarize `kvevaluator predict --accuracy` rows against the accuracy bar -> ratios.csv, summary.md beside them.

    python3 -m kvcost.predict_accuracy.report <dir with steps.csv> [<binary sha + dirty flag, for the header>]

The bar: a step passes when its prediction is within 10% of measured on S, G, N and the value
lanes (get and scan value); a step under 1% of its query's wops is noted, not failed; per query, the layout predicted
cheapest by wops is the one measured cheapest. A step measured at 0 that a level predicts above 0 is judged too.
Also the ratios on wops and bytes (cost_models' "bytes": the 7 read lanes, miss keys included; the hit keys and
values alone are kept as hit_bytes), for each level present (levels.json beside steps.csv: cpp/kvevaluator/src/predict/
terms.h kLevels), and the worst steps named. The truth rows (the exact gate's check on the same steps) are not a level: kvcost.predict_accuracy.
predict_formulas reads them.
Where the error comes from (each level against the one before it, per operator and per workload total):
kvcost.predict_accuracy.predict_formulas. steps.csv carries the lanes only; each
row is costed here (kvcost.predict_accuracy.predicted).
"""
import csv
import json
import math
import os
import sys
import warnings

import numpy as np

from kvcost.layout_cost import compose
from kvcost.predict_accuracy import predicted as PD
from kvevaluator import stale

BAR = 0.10                                      # within 10% of measured
BAR_LANES = ["S", "G", "N", "get_value", "scan_value"]
NOTED_SHARE = 0.01                              # a step under 1% of its query's wops: noted, not failed
CLEAR = 1.12                                    # bar 2: a winner at least 1.12x cheaper than the next layout is clear
TIE = 1e-9                                      # bar 2: layouts within this relative gap measure the same: a tie
def levels_of(d):
    """{level: its record (name, card, anchor, meaning, ...)} in the pass's order: levels.json beside steps.csv (cpp/kvevaluator/src/predict/terms.h kLevels,
    written by `kvevaluator predict --levels-out`: run/predict-accuracy.sh writes it in both of its modes)"""
    path = os.path.join(d, "levels.json")
    if not os.path.isfile(path):
        sys.exit(f"report: no {path}: run/predict-accuracy.sh writes it beside steps.csv (kvevaluator predict --levels-out); "
                 "re-run it")
    with open(path) as f:
        return {lv["name"]: lv for lv in json.load(f)}


def abs_log(x):
    """|log ratio|; a ratio of 0 or infinity (one side measured or predicted 0) is infinitely far"""
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.abs(np.log(np.asarray(x, dtype=float)))


def costs_of(r):
    """a step row's wops, bytes (7 lanes) and hit_bytes (bytes less the miss keys, kept raw), from its lanes; a row
    with empty byte lanes has no bytes, not 0"""
    v = PD.vector(r)
    unknown = any(r.get(b) == "" for b in compose.REPORT_BYTE_LANES)
    nan = float("nan")
    return {"wops": PD.cost(v, "wops"), "bytes": nan if unknown else PD.cost(v, "bytes"),
            "hit_bytes": nan if unknown else PD.hit_bytes(v)}


def on_bar(pred, meas):
    """the bar on one lane: within 10% of measured; a lane measured 0 must be predicted 0"""
    return abs(pred - meas) <= BAR * meas if meas > 0 else abs(pred) < 1e-9




# --- a small column table (no pandas: the artifact's core environment has numpy only) ---------------------------------
KEYS = ["cell", "query", "param", "step", "op"]


def _num(v):
    return float(v) if v != "" else float("nan")


def _key_value(v):
    """a key column's value as read: an integer when it is one (param), else the text"""
    try:
        return int(v)
    except ValueError:
        return v


def nansum(x):
    """a sum that skips NaN; 0 when nothing is left"""
    x = np.asarray(x, dtype=float)
    return float(x[~np.isnan(x)].sum())


def _stat(fn, x, *a):
    """fn over x with NaN skipped; NaN when nothing is left"""
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    return float(fn(x, *a)) if len(x) else float("nan")


def median(x):
    return _stat(np.median, x)


def quantile(x, q):
    return _stat(np.quantile, x, q)


def group_ids(cols):
    """the group key of every row (a tuple over the given key lists)"""
    return list(zip(*cols))


def group_sum(keys, x):
    """per row, the NaN-skipping sum of x over the rows with the same key"""
    tot = {}
    for k, v in zip(keys, x):
        if not math.isnan(v):
            tot[k] = tot.get(k, 0.0) + v
        else:
            tot.setdefault(k, 0.0)
    return np.array([tot[k] for k in keys])


def groups(keys):
    """{key: row indexes}, keys in sorted order"""
    out = {}
    for i, k in enumerate(keys):
        out.setdefault(k, []).append(i)
    return dict(sorted(out.items()))


def take(t, idx):
    """the rows idx of a column table"""
    idx = list(idx)
    return {c: ([v[i] for i in idx] if isinstance(v, list) else v[idx]) for c, v in t.items()}


def nrows(t):
    return len(next(iter(t.values())))


def order_by(t, cols, descending=()):
    """row indexes sorted by cols (NaN last, as pandas; a stable sort)"""
    def key(i):
        out = []
        for c in cols:
            v = t[c][i]
            if isinstance(v, float) and math.isnan(v):
                out += [1, 0]
            else:
                out += [0, (-v if c in descending else v)]
        return out
    return sorted(range(nrows(t)), key=key)


def fmt6(v):
    """a float as pandas' to_csv(float_format="%.6g") writes it: NaN empty"""
    return "" if math.isnan(v) else "%.6g" % v


def main(argv=None):
    warnings.filterwarnings("ignore", category=RuntimeWarning)       # 0/0 and empty medians give NaN, as intended
    args = argv or sys.argv[1:]
    d = args[0]
    binary = args[1] if len(args) > 1 else "not given"
    LEVELS = levels_of(d)
    with open(os.path.join(d, "steps.csv")) as f:
        rows = [r for r in csv.DictReader(f) if r["source"] != "truth"]   # truth: the gate's check, not a level
    # the pivot's column order; chunks: the run's charged chunks (measured) and each level's closed form
    values = sorted(BAR_LANES + ["wops", "bytes", "hit_bytes", "rows"] + (["chunks"] if rows and "chunks" in rows[0] else []))
    seen, piv = set(), {}
    for r in rows:
        k = tuple(_key_value(r[c]) for c in KEYS)
        if (k, r["source"]) in seen:
            n = len(rows) - len({(tuple(x[c] for c in KEYS), x["source"]) for x in rows})
            sys.exit(f"report: {n} duplicate step rows in steps.csv, e.g. {dict(r)}")
        seen.add((k, r["source"]))
        r.update(costs_of(r))
        piv.setdefault(k, {})[r["source"]] = {v: _num(r[v]) if isinstance(r[v], str) else float(r[v]) for v in values}
    # the sources in the pivot's order (its columns' order): the levels in LEVELS' order, then measured (an unknown
    # source after them, by name)
    order = [*LEVELS, "measured"]
    sources = sorted({r["source"] for r in rows}, key=lambda s: (order.index(s) if s in order else len(order), s))
    # the levels present: every level from the accuracy check, the statistics levels from predict --plans over captures
    levels = [lv for lv in LEVELS if lv in sources]
    # predict mode's own prediction, at this pass's body (the sampled levels are kvcost predict's)
    proper = [lv for lv, r in LEVELS.items() if not r["card"] and not r["anchor"] and not r["sampled"]]
    if not set(proper) & set(levels):
        sys.exit(f"report: no {' or '.join(proper)} rows (predict mode's own prediction) in steps.csv")
    keys = sorted(piv)
    w = {c: [k[i] for k in keys] for i, c in enumerate(KEYS)}
    for v in values:
        for src in sources:
            w[f"{v}_{src}"] = np.array([piv[k].get(src, {}).get(v, float("nan")) for k in keys])
    # a step with a cost on either side: measured, or predicted by some level (a step measured at 0 is judged too)
    cost = w["wops_measured"] > 0
    for lv in levels:
        cost |= np.nan_to_num(w[f"wops_{lv}"], nan=0.0) > 0
    w = take(w, np.flatnonzero(cost))
    qkey = group_ids([w["cell"], w["query"], w["param"]])
    # a step with no B row was not estimated (the estimate does not cover it yet): listed, kept off both bars
    w["query_share"] = w["wops_measured"] / group_sum(qkey, w["wops_measured"])
    unest = take(w, np.flatnonzero(np.isnan(w["wops_predicted"])))
    incomplete = set(zip(unest["cell"], unest["query"], unest["param"]))
    w = take(w, np.flatnonzero(~np.isnan(w["wops_predicted"])))
    qkey = group_ids([w["cell"], w["query"], w["param"]])
    with np.errstate(divide="ignore", invalid="ignore"):
        for lv in levels:
            w[f"wops_ratio_{lv}"] = w[f"wops_{lv}"] / w["wops_measured"]
            w[f"bytes_ratio_{lv}"] = w[f"bytes_{lv}"] / w["bytes_measured"]
            w[f"rows_ratio_{lv}"] = w[f"rows_{lv}"] / w["rows_measured"]
            # the step's share of its query, per level: from the larger of measured and predicted, so a step measured
            # at 0 and predicted above it is judged, not noted
            big = np.maximum(w["wops_measured"], np.nan_to_num(w[f"wops_{lv}"], nan=0.0))
            w[f"share_{lv}"] = big / group_sum(qkey, big)
    n = nrows(w)
    for lv in levels:
        fails = [[l for l in BAR_LANES if not on_bar(w[f"{l}_{lv}"][i], w[f"{l}_measured"][i])] for i in range(n)]
        w[f"bar_{lv}"] = ["pass" if not f else ("noted" if sh < NOTED_SHARE else "fail")
                          for f, sh in zip(fails, w[f"share_{lv}"])]
        w[f"bar_lanes_{lv}"] = [" ".join(f) for f in fails]
    with open(os.path.join(d, "ratios.csv"), "w", newline="") as f:
        out_csv = csv.writer(f, lineterminator="\n")
        out_csv.writerow(list(w))
        for i in range(n):
            out_csv.writerow([fmt6(v[i]) if isinstance(v, np.ndarray) else v[i] for v in w.values()])

    def col(c):
        return w[c] if isinstance(w[c], np.ndarray) else np.array(w[c])

    out = ["> Generated by "
           "`python3 -m kvcost.predict_accuracy.report`; ratios are predicted / measured, per KV step with a cost.", ""]
    out.append(f"Binary: {binary}. {n} KV steps with a cost (measured, or predicted). The levels: "
               + "; ".join(f"{lv} = {LEVELS[lv]['meaning']}" for lv in levels)
               + ". \"within 20%\" = a ratio between 1/1.2 and 1.2. Bytes = bytes read on the 7 lanes (the key bytes "
               "of lookups that missed included; cost_models' \"bytes\").")
    out.append("")
    out.append("**Bar 1, per step** (within 10% on " + ", ".join(BAR_LANES) + "; a step under 1% of its query's wops is "
               "noted, not failed):")
    out.append("")
    out.append("| level | steps judged | pass | fail | noted (under 1%, off the bar) | share of the judged steps' measured wops "
               "that pass |")
    out.append("|---|---|---|---|---|---|")
    meas = w["wops_measured"]
    # the headline numbers again as data (summary.json), for a reader comparing two runs (compare_params)
    headline = {"binary": binary, "steps": n, "bar1": {}, "bar2": {}, "ratios": {}}
    for lv in levels:
        b = col(f"bar_{lv}")
        j = w[f"share_{lv}"] >= NOTED_SHARE
        wshare = nansum(meas[j & (b == "pass")]) / nansum(meas[j])
        headline["bar1"][lv] = {"judged": int(j.sum()), "pass": int((j & (b == "pass")).sum()),
                                "fail": int((b == "fail").sum()), "noted": int((b == "noted").sum()),
                                "wops_share_pass": float(wshare)}
        out.append(f"| {lv} | {j.sum()} | {(j & (b == 'pass')).sum()} | {(b == 'fail').sum()} | {(b == 'noted').sum()} | "
                   f"{wshare:.0%} |")
    out.append("")
    zero = meas == 0
    out.append("Steps measured at 0 that a level predicts above 0 (judged above; their predicted wops): "
               + "; ".join(f"{lv} {(zero & (w[f'wops_{lv}'] > 0)).sum()} ({nansum(w[f'wops_{lv}'][zero]):.0f} wops)"
                           for lv in levels) + ".")
    out.append("")
    for lv in levels:
        f = take(w, np.flatnonzero(col(f"bar_{lv}") == "fail"))
        f["query_share"] = f[f"share_{lv}"]
        out.append(f"Failing steps, level {lv}, by their share of the query's wops ({nrows(f)}):")
        out.append("")
        out.append("| cell | query | param | step | share of query wops | lanes off the bar | wops ratio | bytes ratio | "
                   "rows ratio |")
        out.append("|---|---|---|---|---|---|---|---|---|")
        for i in order_by(f, ["query_share", "cell", "query", "param", "step"], descending=("query_share",))[:12]:
            out.append(f"| {f['cell'][i]} | {f['query'][i]} | {f['param'][i]} | {f['step'][i]} | {f['query_share'][i]:.0%} | "
                       f"{f[f'bar_lanes_{lv}'][i]} | {f[f'wops_ratio_{lv}'][i]:.2f} | {f[f'bytes_ratio_{lv}'][i]:.2f} | "
                       f"{f[f'rows_ratio_{lv}'][i]:.2f} |")
        out.append("")
    # the cause of each failure: the rows the level assumed were off the bar (a cardinality error), or they were on
    # it and the cost per row was off (a layout statistic: bytes per entry, present share, chunks)
    out.append("Failing steps by cause: **rows** = the output rows the level assumed are off by more than 10%; "
               "**per row** = the rows are within 10% and the cost per row is off (summed share of the query's wops "
               "in brackets):")
    out.append("")
    out.append("| level | operator | cell | rows | per row |")
    out.append("|---|---|---|---|---|")
    cause_totals = []
    for lv in levels:
        f = take(w, np.flatnonzero(col(f"bar_{lv}") == "fail"))
        r = f[f"rows_ratio_{lv}"]
        with np.errstate(invalid="ignore"):
            is_rows = ~(np.abs(r - 1) <= BAR)
            fin = r[is_rows & np.isfinite(r) & (r > 0)]
        # the row error's size: the median of max(r, 1/r) over the "rows" failures whose ratio is finite and above 0
        cause_totals.append((lv, nrows(f), int(is_rows.sum()), int((~is_rows).sum()),
                             float(np.median(np.maximum(fin, 1 / fin))) if len(fin) else float("nan"),
                             int(is_rows.sum() - len(fin))))
        share = f[f"share_{lv}"]
        cause = ["rows" if x else "per row" for x in is_rows]
        agg = {}                                   # (op, cell, cause) -> [count of shares, NaN-skipping sum]
        for i, k in enumerate(zip(f["op"], f["cell"], cause)):
            a = agg.setdefault(k, [0, 0.0])
            if not math.isnan(share[i]):
                a[0] += 1
                a[1] += share[i]
        tot = {}
        for (op, cell, _c), (_n, sm) in sorted(agg.items()):
            tot[(op, cell)] = tot.get((op, cell), 0.0) + sm
        # descending by the summed share; ties keep the (op, cell) order
        for op, cell in sorted(tot, key=lambda k: -tot[k])[:10]:
            def fmt(c):
                a = agg.get((op, cell, c))
                return f"{a[0]} ({a[1]:.1f})" if a else "0"
            out.append(f"| {lv} | {op} | {cell} | {fmt('rows')} | {fmt('per row')} |")
    out.append("")
    out.append("Failing steps by cause, all operators; the row error = the median of max(ratio, 1/ratio) of the rows the "
               "level assumed over measured, over the \"rows\" failures (a ratio of 0 or infinity left out, counted):")
    out.append("")
    out.append("| level | failing | rows | per row | median row error | rows failures with a ratio of 0 or infinity |")
    out.append("|---|---|---|---|---|---|")
    for lv, nf, nr, npr, med, inf in cause_totals:
        out.append(f"| {lv} | {nf} | {nr} | {npr} | {med:.2f}x | {inf} |")
    out.append("")
    out.append(f"Not estimated yet: {nrows(unest)} steps (in {len(incomplete)} (cell, query, param)); by operator, with "
               "their summed share of their queries' wops:")
    out.append("")
    out.append("| operator | steps | mean share of the query's wops | largest |")
    out.append("|---|---|---|---|")
    for op, idx in groups(unest["op"]).items():
        qs = unest["query_share"][idx]
        out.append(f"| {op} | {len(idx)} | {_stat(np.mean, qs):.1%} | {_stat(np.max, qs):.1%} |")
    out.append("")
    # bar 2 only where every KV step of the (query, param) was estimated, in every cell
    cells = sorted(set(w["cell"]))
    full = take(w, [i for i, (q, p) in enumerate(zip(w["query"], w["param"]))
                    if all((c, q, p) not in incomplete for c in cells)])
    sums = ["wops_measured"] + [f"wops_{lv}" for lv in levels]

    def summed(t, by, cols):
        """one row per group of `by` (sorted): the key columns and the NaN-skipping sums of cols"""
        g = groups(group_ids([t[c] for c in by]))
        res = {c: [k[i] for k in g] for i, c in enumerate(by)}
        for c in cols:
            res[c] = np.array([nansum(t[c][idx]) for idx in g.values()])
        return res

    qp = summed(full, ["query", "param", "cell"], sums)
    out.append("**Bar 2, per query:** the layout predicted cheapest (summed wops of the KV steps) is the one measured "
               "cheapest; only (query, param) whose every step was estimated in every cell.")
    out.append("")

    # each (query, param) by its measured margin: the second-cheapest layout's wops over the cheapest's. A tie (the
    # cheapest two within TIE) has no right answer and is its own row, not "right" by cell-name order
    def margin_class(m):
        m = sorted(m)
        if len(m) < 2 or m[0] <= 0:
            return "tie" if len(m) >= 2 and m[1] <= 0 else "clear"
        return "tie" if m[1] - m[0] <= TIE * m[0] else ("clear" if m[1] / m[0] >= CLEAR else "near-tie")

    def judged(t, by, lv):
        cls = {"clear": [0, 0], "near-tie": [0, 0], "tie": [0, 0]}
        misses = []
        for key, idx in groups(group_ids([t[c] for c in by])).items():
            g = take(t, idx)
            c = margin_class(list(g["wops_measured"]))
            cls[c][1] += 1
            if c == "tie":
                continue
            pick = order_by(g, [f"wops_{lv}", "cell"])[0]
            best = order_by(g, ["wops_measured", "cell"])[0]
            ok = g["cell"][pick] == g["cell"][best]
            cls[c][0] += ok
            if not ok:
                misses.append((key if len(by) > 1 else key[0], g["wops_measured"][pick] / g["wops_measured"][best] - 1))
        return cls, misses

    out.append(f"Each (query, param) by its measured margin (the second-cheapest layout over the cheapest): **clear** = "
               f"at least {CLEAR}x, **near-tie** = under {CLEAR}x, **tie** = the cheapest two measure the same (no "
               "right answer; not scored).")
    out.append("")
    out.append("| level | clear: right / n | near-tie: right / n | ties | per query (params summed), right / n | "
               "the misses: measured wops of the named layout over the cheapest |")
    out.append("|---|---|---|---|---|---|")
    for lv in levels:
        cls, misses = judged(qp, ["query", "param"], lv)
        qs = summed(qp, ["query", "cell"], ["wops_measured", f"wops_{lv}"])
        qcls, qmiss = judged(qs, ["query"], lv)
        qright = qcls["clear"][0] + qcls["near-tie"][0]
        qn = qcls["clear"][1] + qcls["near-tie"][1]
        miss_txt = "; ".join(f"{k[0]} p{k[1]} +{x:.1%}" for k, x in misses) or "none"
        headline["bar2"][lv] = {"clear": [int(x) for x in cls["clear"]], "near_tie": [int(x) for x in cls["near-tie"]],
                                "ties": int(cls["tie"][1]), "per_query": [int(qright), int(qn)],
                                "query_misses": [str(k) for k, _ in qmiss]}
        qmiss_txt = ", ".join(k for k, _ in qmiss)
        out.append(f"| {lv} | {cls['clear'][0]}/{cls['clear'][1]} | {cls['near-tie'][0]}/{cls['near-tie'][1]} | "
                   f"{cls['tie'][1]} | {qright}/{qn}{' (misses ' + qmiss_txt + ')' if qmiss_txt else ''} | {miss_txt} |")
    out.append("")
    out.append("The ratios, predicted / measured:")
    out.append("")
    out.append("| level | metric | median | p10 | p90 | within 20% |")
    out.append("|---|---|---|---|---|---|")
    for lv in levels:
        for m in ("wops", "bytes"):
            r = w[f"{m}_ratio_{lv}"][w[f"{m}_measured"] > 0]   # measured above 0
            within = (abs_log(r) <= math.log(1.2)).mean()
            headline["ratios"].setdefault(lv, {})[m] = {"median": float(median(r)), "p10": float(quantile(r, .1)),
                                                        "p90": float(quantile(r, .9)), "within_20": float(within),
                                                        "n": int(len(r))}
            out.append(f"| {lv} | {m} | {median(r):.2f} | {quantile(r, .1):.2f} | {quantile(r, .9):.2f} | {within:.0%} |")
    out.append("")
    out.append("Per step, median over params [min .. max]:")
    out.append("")
    out.append("| cell | query | step | op | mean measured wops | " + " | ".join(f"wops {lv}" for lv in levels) + " | "
               + " | ".join(f"bytes {lv}" for lv in levels) + " |")
    out.append("|---|---|---|---|---|" + "---|" * (2 * len(levels)))
    for (cell, q, step, op), idx in groups(group_ids([w["cell"], w["query"], w["step"], w["op"]])).items():
        def rng(c):
            x = w[c][idx]
            return f"{median(x):.2f} [{_stat(np.min, x):.2f} .. {_stat(np.max, x):.2f}]"
        out.append(f"| {cell} | {q} | {step} | {op} | {_stat(np.mean, meas[idx]):.1f} | "
                   + " | ".join(rng(f"wops_ratio_{lv}") for lv in levels) + " | "
                   + " | ".join(rng(f"bytes_ratio_{lv}") for lv in levels) + " |")
    out.append("")
    for lv in levels:
        w["k"] = abs_log(w[f"wops_ratio_{lv}"])
        worst = order_by(w, ["k", "cell", "query", "param", "step"], descending=("k",))[:6]
        del w["k"]
        out.append(f"Worst steps, level {lv} (by wops):")
        out.append("")
        out.append("| cell | query | param | step | measured wops | predicted wops | wops ratio | bytes ratio |")
        out.append("|---|---|---|---|---|---|---|---|")
        for i in worst:
            out.append(f"| {w['cell'][i]} | {w['query'][i]} | {w['param'][i]} | {w['step'][i]} | {meas[i]:.1f} | "
                       f"{w[f'wops_{lv}'][i]:.1f} | {w[f'wops_ratio_{lv}'][i]:.2f} | {w[f'bytes_ratio_{lv}'][i]:.2f} |")
        out.append("")
    q = summed(w, ["query", "param", "cell"], [c for c in w if c.startswith(("wops_", "bytes_")) and "ratio" not in c])
    out.append("Ranking of the cells per (query, param), by the summed KV steps:")
    out.append("")
    out.append("| level | metric | best cell right | full order right |")
    out.append("|---|---|---|---|")
    for lv in levels:
        for m, c in (("wops", "wops"), ("bytes", "bytes")):
            best = order = n_qp = 0
            for idx in groups(group_ids([q["query"], q["param"]])).values():
                g = take(q, idx)
                by_meas = [g["cell"][i] for i in order_by(g, [f"{c}_measured", "cell"])]
                by_pred = [g["cell"][i] for i in order_by(g, [f"{c}_{lv}", "cell"])]
                n_qp += 1
                best += by_pred[0] == by_meas[0]
                order += by_pred == by_meas
            out.append(f"| {lv} | {m} | {best}/{n_qp} | {order}/{n_qp} |")
    with open(os.path.join(d, "summary.md"), "w") as f:
        f.write("\n".join(out) + "\n")
    with open(os.path.join(d, "summary.json"), "w") as f:   # + the moved inputs (kvevaluator.stale) of this run and its driver
        json.dump(stale.fold(headline, d), f, indent=1, sort_keys=True)
        f.write("\n")
    print("\n".join(out))


if __name__ == "__main__":
    main()
