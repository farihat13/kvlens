#!/usr/bin/env python3
"""single-vs-mix-chunk-size.png: the best single layout against the best mixed layout within each byte limit B, once per
chunk size of the Chunked setting (the sizes the report holds), in weighted ops and in ops + bytes / 4 KB (owner: mix_benefit).
Every bar is x the reference layout (--ref, default Sp-Co: python/kvcost/layout_cost/settings.py); B stays in units of the mixes' B unit (cells.B_UNIT, Sp-Co;
Co-Sp in a report in Co-Sp units, told by its questions: search.options_b_unit).

Reads chunk_size_report.py's uniform_cells.csv and mixed_layouts.csv under --root, and the workload's options.csv for
the single layouts with no Chunked side (the same at every chunk size). Analysis only; counts are deterministic.

    python3 experiments/mix_benefit/plot_chunk_size_single_vs_mix.py --root <chunk_size_report.py's --out> \
        --options <analysis dir>/i01-rw-ldbc-mix/options.csv [--workload i01] [--ref CELL] [--note TEXT]

Writes <root>/plots/single-vs-mix-chunk-size.png and <root>/single_vs_mix.csv (the drawn numbers).
"""
import argparse
import csv
import os
import sys
import textwrap

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

from kvcost.plotting.style import apply_house_style
from kvcost.layout_cost import settings as cells  # (the reference layout and the cells' short names)
from kvcost.layout_cost import cost_models as CM  # (the cost models, one definition)
from kvcost.layout_search.search import options_b_unit

SIZES = (("262144", "256 KB", "#66a61e"), ("131072", "128 KB", "#e7298a"), ("65536", "64 KB", "#8c8c8c"), ("16384", "16 KB", "#d95f02"),
         ("4096", "4 KB", "#1b9e77"), ("1024", "1 KB", "#7570b3"))   # study-local: a chunk size has no repo-wide color
BS = ()                               # the named B of mixed_layouts.csv, read from its questions
MEASURES = (("total_ops", "weighted KV ops"), ("ops_plus_bytes_per_4kb", "ops + bytes / 4 KB"))


def read(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", required=True, help="chunk_size_report.py's --out dir")
    ap.add_argument("--options", required=True, help="the workload's options.csv: the single layouts with no Chunked side")
    ap.add_argument("--workload", default="i01")
    ap.add_argument("--ref", default=cells.REF, choices=cells.CELLS, help="the layout every bar is x (default Sp-Co)")
    ap.add_argument("--note", default="", help="the last caption sentence: scale and snapshot")
    a = ap.parse_args()
    apply_house_style()
    global BS
    questions = [r["question"] for r in read(os.path.join(a.root, "mixed_layouts.csv"))]
    BS = tuple(f"{b:g}" for b in sorted({float(q.split("B=")[1]) for q in questions if "B=" in q}))
    REF, BU = cells.name_of(a.ref), cells.name_of(options_b_unit(q.replace("fewest ops within ", "") for q in questions))
    plain = {cells.name_of(o["option"]): o for o in read(a.options) if o["option_set"] == "single" and "chunked" not in o["option"]}
    rows = []
    global SIZES
    present = {r["chunk_bytes"] for r in read(os.path.join(a.root, "uniform_cells.csv"))}
    SIZES = tuple(x for x in SIZES if x[0] in present)              # the sizes the report holds, largest first
    for size, label, _ in SIZES:
        layouts = {cells.short_names(r["cell"]): (float(r["total_ops"]), float(r["total_bytes"])) for r in read(os.path.join(a.root, "uniform_cells.csv"))
                 if r["chunk_bytes"] == size and r["workload_id"] == a.workload}
        for name, o in plain.items():
            mine = (float(o["total_ops"]), float(o["total_bytes"]))
            if name in layouts and abs(layouts[name][0] / mine[0] - 1) > 1e-9:
                raise SystemExit(f"{name}: options.csv and uniform_cells.csv differ ({mine[0]} vs {layouts[name][0]}): not the same grid")
            layouts[name] = mine
        if len(layouts) != 9:
            raise SystemExit(f"{label}: {len(layouts)} single layouts, not 9")
        ref_ops, ref_bytes = layouts[REF]
        b_unit = layouts[BU][1]                                  # B = 1: B_UNIT's bytes, the unit the mixes' limits are in
        ref_sum = CM.wops_bytes4k(ref_ops, ref_bytes)
        mixes = {r["question"]: r for r in read(os.path.join(a.root, "mixed_layouts.csv")) if r["chunk_bytes"] == size and r["workload_id"] == a.workload}
        for b in BS:
            fit = sorted((ops, name, byt) for name, (ops, byt) in layouts.items() if byt <= float(b) * b_unit * (1 + 1e-12))
            if not fit:
                raise SystemExit(f"{label} B={b}: no single layout fits")
            ops, name, byt = fit[0]
            m = mixes[f"fewest ops within B={b}"]
            if m["fits"] != "yes":
                raise SystemExit(f"{label} B={b}: the mixed layout does not fit its limit")
            rows.append({"workload": a.workload, "B": b, "chunk_size": label, "single": name, "single_total_ops": round(ops / ref_ops, 4),
                         "single_bytes": round(byt / ref_bytes, 4), "single_ops_plus_bytes_per_4kb": round(CM.wops_bytes4k(ops, byt) / ref_sum, 4),
                         "mix_total_ops": round(float(m["total_ops"]) / ref_ops, 4), "mix_bytes": round(float(m["total_bytes"]) / ref_bytes, 4),
                         "mix_ops_plus_bytes_per_4kb": round(float(m["ops_plus_bytes_per_4kb"]) / ref_sum, 4),
                         "reference": REF, "reference_ops": ref_ops, "reference_bytes": ref_bytes, "b_unit": BU, "b_unit_bytes": b_unit})
    with open(os.path.join(a.root, "single_vs_mix.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    n = len(SIZES)
    fig, axes = plt.subplots(2, 1, figsize=(max(12, 3.2 * n + 2), 12.4), gridspec_kw={"hspace": 0.62})
    step = 0.92 / n                              # a B group spans < 1 whatever the number of sizes
    bw = step * 0.42                             # one bar of a (single, mix) pair
    for ax, (key, title) in zip(axes, MEASURES):
        ticks = []
        for bi, b in enumerate(BS):
            for ci, (_, label, color) in enumerate(SIZES):
                r = next(x for x in rows if x["B"] == b and x["chunk_size"] == label)
                x0 = bi + (ci - (len(SIZES) - 1) / 2) * step
                s, m = r["single_" + key], r["mix_" + key]
                ax.bar(x0 - bw / 2, s, bw, color=color)
                ax.bar(x0 + bw / 2, m, bw, color=color, hatch="///", edgecolor="white", linewidth=0)
                for x, v in ((x0 - bw / 2, s), (x0 + bw / 2, m)):
                    ax.text(x, v * 1.06, f"{v:.2f}" if v >= 0.1 else f"{v:.3f}", ha="center", va="bottom", fontsize=8, rotation=90)
                ticks.append((x0, r["single"]))
                gain = s / m
                ax.text(x0, 3.4, "=" if abs(gain - 1) < 0.005 else f"{gain:.1f}x" if gain >= 1.05 else f"{gain:.2f}x", ha="center", va="top", fontsize=8,
                        color="black" if gain >= 1.05 else "0.45")
        ax.set_yscale("log")
        lo = min(min(x["single_" + key], x["mix_" + key]) for x in rows)
        ax.set_ylim(lo / 1.5, 3.8)
        ax.set_yticks([t for t in (0.003, 0.01, 0.03, 0.1, 0.3, 1) if t >= lo / 1.5], [f"{t:g}x" for t in (0.003, 0.01, 0.03, 0.1, 0.3, 1) if t >= lo / 1.5])
        ax.minorticks_off()
        ax.axhline(1, color="0.5", lw=0.8, ls=":")
        ax.set_xticks([t[0] for t in ticks], [t[1] for t in ticks], fontsize=8, rotation=40, ha="right", rotation_mode="anchor")
        ax.text(-0.5, 3.4, "mix\nbelow\nsingle:", ha="right", va="top", fontsize=8)
        for bi, b in enumerate(BS):
            ax.text(bi, -0.2, f"B = {b}", transform=ax.get_xaxis_transform(), ha="center", va="top", fontsize=12)
            if bi:
                ax.axvline(bi - 0.5, color="0.8", lw=0.8)
        ax.set_title(f"{title} ({REF} = 1x)", fontsize=12, fontweight="bold")
        ax.tick_params(axis="y", labelsize=10)
        ax.grid(axis="y", alpha=0.3)
        ax.set_axisbelow(True)
    handles = []
    for _, label, color in SIZES:
        handles += [Patch(color=color, label=f"Chunked = {label} chunks: best single layout"),
                    Patch(facecolor=color, hatch="///", edgecolor="white", label=f"Chunked = {label} chunks: best mixed layout")]
    fh = fig.get_figheight()
    fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 0.75 / fh), ncol=min(n, 3), frameon=False, fontsize=10)
    fig.suptitle(f"{a.workload.upper()}: the best single layout against the best mixed layout within a byte limit, with Chunked at " + ", ".join(l for _, l, _ in SIZES) + " chunks",
                 fontsize=13, y=1 - 0.15 / fh)
    text = (cells.NAMES_NOTE + f" Every bar is relative to the reference layout {REF} "
            f"({rows[0]['reference_ops'] / 1e6:,.1f} M weighted ops, {rows[0]['reference_bytes'] / 1e9:,.1f} GB per 100,000 updates). B = the byte limit, in "
            f"units of {BU}'s bytes. Per chunk size: the nine single layouts and the exact mixed layout per type and direction; the best = "
            "fewest weighted ops within the limit (the same pick in both panels). The name under a pair of bars is the best single layout; the top row is the best single layout / the mix, in that panel's measure (= within 0.5%; below 1x the mix, picked on weighted ops, costs more in that measure). Counts only "
            "(deterministic), reads and updates; ops weighted GET : SEEK : NEXT : PUT : DELETE = 1 : 1.1 : 0.01 : 1 : 1. " + a.note)
    fig.text(0.5, 0.62 / fh, "\n".join(textwrap.wrap(text, 215)), ha="center", va="top", fontsize=9)
    fig.subplots_adjust(left=0.07, right=0.99, top=1 - 0.75 / fh, bottom=(2.0 + 0.27 * ((2 * n + 2) // 3)) / fh)
    out = os.path.join(a.root, "plots")
    os.makedirs(out, exist_ok=True)
    fig.savefig(os.path.join(out, "single-vs-mix-chunk-size.png"), dpi=150, bbox_inches="tight")
    print(f"wrote {os.path.join(out, 'single-vs-mix-chunk-size.png')} and single_vs_mix.csv: {len(rows)} rows")


if __name__ == "__main__":
    main()
