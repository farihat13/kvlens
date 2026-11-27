#!/usr/bin/env python3
"""The one figure of the I01 mix-latency experiment: per byte limit B, the I01 latency per 100,000 updates of the picked
mix, of the best single layout that fits B, and of the picks file's reference cell (B's unit as picked), one panel per measured regime (log y;
the updates' share hatched at the foot of each bar; the pre-registered word under each B), beside the same three in total
weighted ops and in total bytes from the counts (the prediction). Reads the scorer's outputs (w16_latency_score.py) and
the generator's (pick_specs.py); draws nothing it computes itself.

    python3 experiments/layout_choice/plot_w16_mix_latency.py --sf 3 --root <latency root> --picks inputs/specs/mixes_w16-picks.json
    python3 experiments/layout_choice/plot_w16_mix_latency.py --sf 30 --analysis steady-p2=<dir> --picks ... --out <dir>/plots   # a smoke
Output: <root>/analysis/plots/w16_mix_latency.{png,pdf} (or --out).
"""
import argparse
import csv
import json
import math
import os
import sys

import matplotlib
from kvevaluator.paths import ROOT, figure_dir
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvcost.plotting.style import apply_house_style, bar_offsets, db_color, format_color, stack_on_log
from kvcost.plotting.formatting import bytes_to_human, number_to_human
from kvcost.layout_cost.settings import name_of

ROLES = (("mix", "the picked mix"), ("best_single", "best single within B"), ("reference", "the reference layout"))   # the last label: the picks file's cell
REGIME_TITLE = {"steady": "steady (bounded cache)", "warm": "warm", "cold": "cold"}


def role_color(role):
    return {"mix": format_color("meta"), "best_single": "0.55", "reference": format_color("nebula")}[role]   # a Split-adjacency cell's colour (compose.ADJ_COLOR_KEY)


def decade_ticks(ax):
    """human labels on the decades (1, 2, 5 x 10^k) of a log axis and no raw minor labels (the house-style trap)"""
    lo, hi = ax.get_ylim()
    ticks = [m * 10 ** k for k in range(int(math.floor(math.log10(lo))), int(math.ceil(math.log10(hi))) + 1) for m in (1, 2, 5)]
    ax.set_yticks([t for t in ticks if lo <= t <= hi])
    ax.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())


def _wrap(text, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur); cur = w
        else:
            cur = (cur + " " + w).strip()
    return lines + [cur]


def regime_family(name):
    return "steady" if name.startswith("steady") else "cold" if name.startswith("cold") else "warm"


def read_csv(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def arms_of(rec):
    """B's record -> {role: store arm}"""
    return {"mix": "meta-" + rec["spec"][:-5], "best_single": "meta-" + rec["best_single_within_B"]["cell"],
            "reference": "meta-" + rec["reference"]["cell"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True)
    ap.add_argument("--root", default=None, help="the latency root: <root>/analysis/<regime>/{w16_latency,w16_words}.csv")
    ap.add_argument("--analysis", default=None, help="regime=dir,... instead of --root (a smoke on seen data)")
    ap.add_argument("--picks", required=True)
    ap.add_argument("--predicted", default=None, help="the generator's <stem>-predicted.csv (default beside --picks)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--extralarge", type=int, default=1)
    ap.add_argument("--note", default="", help="a line under the title (a first pass: which regimes are not measured yet)")
    a = ap.parse_args()
    apply_house_style(extralarge=a.extralarge)
    picks = json.load(open(a.picks))
    ref_name = name_of(picks["reference"])        # B's unit as the file was picked (Co-Sp in a picks file in Co-Sp units)
    pred = read_csv(a.predicted or a.picks[:-len("-picks.json")] + "-predicted.csv")
    if a.analysis:
        dirs = dict(kv.split("=", 1) for kv in a.analysis.split(","))
    else:
        base = os.path.join(a.root, "analysis")
        dirs = {d: os.path.join(base, d) for d in sorted(os.listdir(base)) if os.path.exists(os.path.join(base, d, "w16_latency.csv"))}
    dirs = {k: v for k, v in sorted(dirs.items(), key=lambda kv: ("steady", "warm", "cold").index(regime_family(kv[0])))}
    if not dirs:
        raise SystemExit("no analysis dir with w16_latency.csv")
    Bs = list(picks["picks"])
    panels = [("latency", r) for r in dirs] + [("ops", None), ("bytes", None)]
    any_no_iu = False
    fig, axes = plt.subplots(1, len(panels), figsize=(3.4 * len(panels) + 1, 5.2), constrained_layout=True)
    width = 0.26
    offs = bar_offsets(3, width)
    for ax, (kind, regime) in zip(axes, panels):
        if kind == "latency":
            lat = {r["arm"]: r for r in read_csv(os.path.join(dirs[regime], "w16_latency.csv"))}
            no_iu = all(float(r["IU_median"] or 0) == 0 for r in lat.values())   # the writes stage not run: the bars are the reads
            any_no_iu = any_no_iu or no_iu
            words = {(w["B"], w["other_role"]): w["word"] for w in read_csv(os.path.join(dirs[regime], "w16_words.csv")) if w["class"] == "I01"}
            vals = {}
            for bi, B in enumerate(Bs):
                for ri, (role, _) in enumerate(ROLES):
                    arm = arms_of(picks["picks"][B])[role]
                    if arm not in lat:
                        continue
                    tot, iu = float(lat[arm]["W16_median"]), float(lat[arm]["IU_median"])
                    vals[(B, role)] = tot
                    x = bi + offs[ri]
                    floor = min(float(r["W16_median"]) for r in lat.values() if float(r["W16_median"]) > 0) / 4
                    # the updates' share at the foot (hatched), the reads above: one color per role, the hatch says IU
                    stack_on_log(ax, x, width, [(iu, role_color(role))], floor, barkw={"hatch": "///", "edgecolor": "white"})
                    base = max(iu, floor)
                    if tot - base > 0:
                        ax.bar(x, tot - base, width, bottom=base, color=role_color(role), edgecolor="white")
            ax.set_yscale("log")
            ax.set_title(f"latency, {REGIME_TITLE[regime_family(regime)]}\n({regime})", pad=14)   # 'reads only' is in the y label
            ax.set_ylabel(("I01 reads only (IU not measured) per 100,000 updates" if no_iu else "I01 latency per 100,000 updates") if ax is axes[0] else "")
            ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: number_to_human(v / 1e9) + " s"))
            decade_ticks(ax)
            for bi, B in enumerate(Bs):
                w = words.get((B, "best_single"), "—")
                short = {"mix faster": "faster", "no measurable difference": "no diff.", "mix slower": "slower"}.get(w, "n/v")
                ax.annotate(short, (bi, 1), xycoords=("data", "axes fraction"), xytext=(0, 2), textcoords="offset points",
                            ha="center", va="bottom", fontsize=6 + a.extralarge, color="0.25")
        else:
            col = "ops" if kind == "ops" else "bytes"
            rows = {(r["B"], r["role"]): float(r[col]) for r in pred}
            for bi, B in enumerate(Bs):
                for ri, (role, _) in enumerate(ROLES):
                    v = rows.get((B, role))
                    if v is None:
                        continue
                    ax.bar(bi + offs[ri], v, width, color=role_color(role), edgecolor="white")
            ax.set_yscale("log")
            ax.set_title(f"{'total weighted ops' if kind == 'ops' else 'total bytes'} (the counts)")
            ax.set_ylabel("weighted ops per 100,000 updates" if kind == "ops" else "bytes per 100,000 updates")
            fmt = number_to_human if kind == "ops" else bytes_to_human       # bound now: a lambda would see the loop's last `kind`
            ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _, fmt=fmt: fmt(v)))
            decade_ticks(ax)
        ax.set_xticks(range(len(Bs)))
        ax.set_xticklabels([str(B) for B in Bs])
        ax.set_xlabel(f"byte limit B (× {ref_name}'s bytes)" if ax is axes[len(axes) // 2] else "B")
        ax.grid(axis="y", alpha=0.3)
    handles = [plt.Rectangle((0, 0), 1, 1, facecolor=role_color(r)) for r, _ in ROLES] + [plt.Rectangle((0, 0), 1, 1, facecolor="0.8", hatch="///", edgecolor="white")]
    labels = [lab if r != "reference" else f"{ref_name} (B's unit in the picks file)" for r, lab in ROLES] + ["the updates' share (IU1-8, apply wall)"]
    if any_no_iu:
        handles, labels = handles[:3], labels[:3]
    fig.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0), ncol=4, frameon=False)
    fig.suptitle(f"I01 (LDBC Interactive, LDBC frequencies), SF{a.sf}: the picked mix against the best single layout that fits each byte limit"
                 + (f"\n{a.note}" if a.note else ""),
                 fontsize=10 + a.extralarge)
    shares = [f"B = {b2} shares B = {b1}'s store" for i, b1 in enumerate(Bs) for b2 in Bs[i + 1:]
              if picks["picks"][b1]["spec"] == picks["picks"][b2]["spec"]]
    caption = ("Latency: added over the queries, the I01 count x the per-query median over params of the plan wall (nondeterministic); "
               + ("the updates IU1-8 are NOT measured in this pass: the latency bars are the reads (I02) and the I01 words are 'no verdict'. "
                  if any_no_iu else "the updates IU1-8 at the write driver's apply wall (warm) in every regime. ")
               + "Counts: deterministic, from the layout-choice snapshot "
               "(ops = G + 1.1 S + 0.01 N + P + D; bytes = read + read before a write + written). The word above each B is the pre-registered "
               "verdict of the mix against the best single layout." + (" " + "; ".join(shares) + "." if shares else "")
               + (" " + a.note if a.note else ""))
    fig.text(0.5, -0.12, "\n".join(_wrap(caption, 170)), ha="center", va="top", fontsize=7 + a.extralarge, color="0.3")
    out = figure_dir(os.path.join(a.root, "analysis", "plots"), a.out)
    os.makedirs(out, exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(os.path.join(out, f"w16_mix_latency.{ext}"), dpi=150, bbox_inches="tight")
    print(f"-> {out}/w16_mix_latency.png ({len(dirs)} regime panels: {', '.join(dirs)})")


if __name__ == "__main__":
    main()
