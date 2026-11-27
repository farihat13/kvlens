#!/usr/bin/env python3
"""The write latency table of one steady writes capture (run-w16-mix-latency.sh --stage writes --write-cache steady): per
store and write query, the median over params of the apply wall after the steady read warm-up (B) and in the four-leg run
under the steady settings (A), and each store's IU total at I01's counts over the reference store's. Opens no store.

    python3 experiments/layout_choice/write_latency_table.py --b <writes>/after-steady-warmup-<MB>mb-p1-noflush \
        --a <writes>/steady-settings-<MB>mb-4leg-p1-noflush --out <dir> [--ref props_split_adj_condensed]

The rule is the scorer's (w16_latency_score.class_totals): per query the median over its params of the `__plan__` wall
(the apply leg; one wall per param, as an applied write runs once), the IU total = the sum over IU1-8 of I01's count
per 100,000 updates x that median; the mean over params beside it. The primitives (w_*) have no I01 count: per query only.
Output: <out>/write_latency_by_query.csv, <out>/write_latency_totals.csv, and the tables as Markdown on stdout.
"""
import argparse
import csv
import json
import os
import statistics as st

from kvcost.latency.steps import load_writes
from kvcost.latency.i01 import weights
from w16_latency_score import class_totals   # the IU total: one rule, the scorer's (this script's dir comes first on the path)


def load_all_writes(path):
    """load_writes for every write query (load_writes keeps the IU queries only): {(query, param): apply wall_ns}, and
    the shadow control's wall (A only; NaN in B, which runs the apply leg alone)"""
    out = {}
    for r in csv.DictReader(open(path)):
        if r["step"] == "__plan__":
            wall, shadow = float(r["wall_ns"] or 0), float(r["shadow_wall_ns"] or "nan")
            out[(r["query"], r["param_idx"])] = {"plan": wall if wall > 0 else float("nan"), "shadow": shadow}
    iu = load_writes(path)
    for k, v in iu.items():
        if v["plan"] != out[k]["plan"] and v["plan"] == v["plan"]:
            raise SystemExit(f"{path}: {k} read two ways: {v['plan']} vs {out[k]['plan']}")
    return out


def stores(regime_dir):
    return sorted(d[len("meta-"):] for d in os.listdir(regime_dir)
                  if d.startswith("meta-") and os.path.exists(os.path.join(regime_dir, d, "steps.csv")))


def per_query(w, key="plan", agg=st.median):
    by_q = {}
    for (q, _), v in w.items():
        by_q.setdefault(q, []).append(v[key])
    return {q: (float("nan") if any(x != x for x in v) else agg(v)) for q, v in by_q.items()}, {q: len(v) for q, v in by_q.items()}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--b", required=True, help="the writes after the steady read warm-up (apply leg only): meta-<store>/steps.csv")
    ap.add_argument("--a", required=True, help="the four-leg run under the steady settings, the same stores and sequence")
    ap.add_argument("--ref", default="props_split_adj_condensed", help="the store the totals are divided by")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    sb, sa = stores(a.b), stores(a.a)
    if sb != sa or a.ref not in sb:
        raise SystemExit(f"stores differ between B ({len(sb)}) and A ({len(sa)}), or no {a.ref}")
    n = weights()
    rows, tot = [], {}
    for s in sb:
        wb = load_all_writes(os.path.join(a.b, f"meta-{s}", "steps.csv"))
        wa = load_all_writes(os.path.join(a.a, f"meta-{s}", "steps.csv"))
        if set(wb) != set(wa):
            raise SystemExit(f"{s}: B and A cover different (query, param) requests")
        mb, cnt = per_query(wb); ma, _ = per_query(wa); msh, _ = per_query(wa, "shadow")
        eb, _ = per_query(wb, agg=st.mean); ea, _ = per_query(wa, agg=st.mean)
        for q in sorted(mb):
            rows.append({"store": s, "query": q, "params": cnt[q], "w16_count": n.get(q, ""),
                         "b_median_wall_ns": mb[q], "a_median_wall_ns": ma[q], "a_median_shadow_wall_ns": msh[q],
                         "b_mean_wall_ns": eb[q], "a_mean_wall_ns": ea[q]})
        meta = json.load(open(os.path.join(a.b, f"meta-{s}", "steps.csv.meta.json")))
        warm = meta["warm_reads"][f"meta-{s}"]; same = meta["same_counts"]
        prov = meta["provenance"][f"meta-{s}"]
        iu = {k: v for k, v in wb.items() if k[0].startswith("iu")}
        iua = {k: v for k, v in wa.items() if k[0].startswith("iu")}
        tot[s] = {"store": s, "iu_requests": len(iu),
                  "b_iu_total_ns": class_totals(None, iu, n)["IU"], "a_iu_total_ns": class_totals(None, iua, n)["IU"],
                  "b_iu_total_mean_ns": class_totals(None, iu, n, agg=st.mean)["IU"],
                  "a_iu_total_mean_ns": class_totals(None, iua, n, agg=st.mean)["IU"],
                  "warm_passes": warm["passes"], "settled_at": warm["steady_at"], "capture_settled_at": warm["capture_steady_at"],
                  "same_counts_cells": same["cells_compared"], "same_counts_differ": same["differ"],
                  "binary_git_sha": prov["binary_git_sha"][:10], "binary_dirty": prov["binary_dirty"]}
    ref = tot[a.ref]
    for t in tot.values():
        for k in ("b_iu_total_ns", "a_iu_total_ns", "b_iu_total_mean_ns", "a_iu_total_mean_ns"):
            t[k.replace("_ns", "_over_ref")] = t[k] / ref[k]
    os.makedirs(a.out, exist_ok=True)
    for name, data in (("write_latency_by_query.csv", rows), ("write_latency_totals.csv", list(tot.values()))):
        with open(os.path.join(a.out, name), "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(data[0]))
            w.writeheader()
            w.writerows(data)
        print(f"wrote {os.path.join(a.out, name)} ({len(data)} rows)")
    order = sorted(tot, key=lambda s: tot[s]["b_iu_total_over_ref"])
    print(f"\nIU total at I01's counts (seconds per 100,000 updates; median over params), over {a.ref}'s:\n")
    print("| store | B: after the read warm-up | B / ref | A: four legs | A / ref | B mean / ref | warm-up passes, settled at |")
    print("|---|---|---|---|---|---|---|")
    for s in order:
        t = tot[s]
        print(f"| `{s}` | {t['b_iu_total_ns'] / 1e9:.3f} | {t['b_iu_total_over_ref']:.3f} | {t['a_iu_total_ns'] / 1e9:.3f} | "
              f"{t['a_iu_total_over_ref']:.3f} | {t['b_iu_total_mean_over_ref']:.3f} | {t['warm_passes']}, {t['settled_at']} |")
    by = {(r["store"], r["query"]): r for r in rows}
    queries = sorted({r["query"] for r in rows}, key=lambda q: (not q.startswith("iu"), len(q), q))
    for title, qs in (("IU1-8", [q for q in queries if q.startswith("iu")]), ("the primitives", [q for q in queries if not q.startswith("iu")])):
        print(f"\n{title}: median over params of the apply wall, B / A, in microseconds:\n")
        print("| store | " + " | ".join(qs) + " |")
        print("|---|" + "---|" * len(qs))
        for s in order:
            print(f"| `{s}` | " + " | ".join(f"{by[(s, q)]['b_median_wall_ns'] / 1e3:.0f} / {by[(s, q)]['a_median_wall_ns'] / 1e3:.0f}" for q in qs) + " |")
    print(f"\n{len(tot)} stores, {len(rows)} (store, query) rows, {sum(t['iu_requests'] for t in tot.values())} IU requests")
    print("WRITE_TABLE_DONE")


if __name__ == "__main__":
    main()
