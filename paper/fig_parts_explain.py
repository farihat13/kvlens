#!/usr/bin/env python3
"""Paper figures: why a mix can pay, part by part, on two small workloads (full width each).

  figures/layout-choice/parts-w14.pdf     W14: two inserts and one read, no byte limit
  figures/layout-choice/parts-w02.pdf     W02: three reads, bytes limited to 2 x those of Sp-Co

Top to bottom: which query reads or writes each part (vertex properties, then adjacency lists); each part's total
weighted ops and bytes under the three settings (the only numbers inside the grid); the nine uniform layouts, one row
each, a cell shaded by the setting the layout gives the part, with the row's total ops and bytes; the mixes the same
way, with their gain over the uniform layout that carries the same mark. A marked row says what it is cheapest in:
fewest ops, fewest bytes, or fewest ops within the byte limit. Weighted ops = G + 1.1 S + 0.01 N + P + D; bytes =
read + written, a write's own reads included. The index (no setting to choose) is not drawn; the totals include it.
Counts are deterministic: no error bars.

Reads LAYOUT_CHOICE_SF3_UNGROUPED's <workload>/breakdown.csv (per option, query and part), touch.csv (which query
touches which part) and options.csv (totals).

    uv run python paper/fig_parts_explain.py
"""
import csv
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

from kvcost.plotting.style import apply_house_style
from kvevaluator.layouts import cell_id, name_of

from common import FIGURES, LAYOUT_CHOICE_SF3_UNGROUPED

CODE = ("split", "condensed", "chunked")      # the three settings: the nine uniform layouts are their pairs
SHADE = {"split": "#f1f1f1", "condensed": "#b9b9b9", "chunked": "#5f5f5f"}     # the options map's three greys
MIX = "#d81b60"
REFERENCE = "props_split_adj_condensed"         # Sp-Co: the byte limit is B x this layout's bytes, as everywhere in the paper
QUERY = {"nhop2": "friends of friends", "hop2_post_likers": "likers of posts", "entry_eq_scan": "name scan",
         "iu6": "add a Post", "iu7": "add a Comment"}
# output stem, snapshot folder, title, mixes [(option id, row label)], byte limit B or None, parts in column order
PANELS = [
    ("parts-w14", "w14-rw-hubs-vs-leaves", "W14: two inserts, one read; no byte limit",
     [("per_type_fewest_ops", "mix, fewest ops"), ("per_type_fewest_bytes", "mix, fewest bytes")], None,
     ["Message props", "knows lists", "hasCreator lists", "hasTag lists", "isLocatedIn lists", "containerOf lists", "replyOf lists"]),
    ("parts-w02", "w02-r-analytics", "W02: three reads; byte limit = 2 x bytes of Sp-Co",
     [("per_type_fewest_ops", "mix, fewest ops"), ("per_type_B2", "mix, fewest ops in limit"), ("per_type_fewest_bytes", "mix, fewest bytes")], 2.0,
     ["Person props", "likes lists", "knows lists", "hasCreator lists"]),
]
LEFT, GAP, TOT = 3.5, 0.25, 4.6          # in cell widths: the label column, the gap between the two groups, the totals column
QH, NH, LH = 0.5, 0.95, 0.5              # row heights: a query row, a numbers row, a layout row
U, V, FW = 0.46, 0.27, 7.0               # inches per cell width and per unit of height; the figure's width


def num(v):
    return f"{v / 1e6:.1f}M" if v >= 1e6 else f"{v / 1e3:.0f}K" if v >= 1e4 else f"{v / 1e3:.1f}K" if v >= 1e3 else f"{v:.0f}"


def size(b):
    return (f"{b / 1e9:.1f} GB" if b >= 1e9 else f"{b / 1e6:.0f} MB" if b >= 1e7 else f"{b / 1e6:.1f} MB" if b >= 1e6
            else f"{b / 1e3:.0f} KB")


def load(folder):
    """({(option, part): [ops, bytes, setting]}, {(query, how often): {part: reads | writes}}, {option: (ops, bytes)})"""
    d = LAYOUT_CHOICE_SF3_UNGROUPED / folder
    cost = defaultdict(lambda: [0.0, 0.0, None])
    for r in csv.DictReader(open(d / "breakdown.csv")):
        c = cost[(r["option"], r["part"])]
        c[0] += float(r["how_often"]) * (float(r["read_ops"]) + float(r["write_ops"]) + float(r["write_rmw_ops"]))
        c[1] += float(r["how_often"]) * sum(float(r[k]) for k in ("read_bytes", "miss_key_bytes", "write_bytes", "write_rmw_bytes",
                                                                 "write_rmw_miss_key_bytes"))
        c[2] = r["layout"]
    touch = defaultdict(dict)
    for r in csv.DictReader(open(d / "touch.csv")):
        touch[(r["query"], r["how_often"])][r["part"].replace(" in-lists", " lists").replace(" out-lists", " lists")] = r["kind"]
    total = {r["option"]: (float(r["total_ops"]), float(r["total_bytes"])) for r in csv.DictReader(open(d / "options.csv"))}
    return cost, touch, total


def height(nq, nmix):
    return 1.5 + 0.9 + nq * QH + 0.15 + 3 * NH + 0.45 + 9 * LH + 0.25 + nmix * LH + 0.15


def draw(stem, folder, title, mixes, limit, parts):
    cost, touch, total = load(folder)
    uni = [(c, name_of(c)) for c in (cell_id(p, a) for p in CODE for a in CODE)]
    cap = limit * total[REFERENCE][1] if limit else None
    fits = lambda o: cap is None or total[o][1] <= cap * 1.0001
    ok = [o for o, _ in uni if fits(o)]
    any_ops = min((o for o, _ in uni), key=lambda o: total[o][0])               # fewest ops with no limit
    few_ops, few_bytes = min(ok, key=lambda o: total[o][0]), min(ok, key=lambda o: total[o][1])
    nprops = sum(p.endswith("props") for p in parts)
    X = [j + (GAP if j >= nprops else 0) for j in range(len(parts))]
    xt = X[-1] + 1 + 0.3
    H = height(len(touch), len(mixes))
    fig = plt.figure(figsize=(FW, H * V))
    ax = fig.add_axes([0.0, 0.0, (len(parts) + LEFT + GAP + 0.3 + TOT) * U / FW, 1.0])
    ax.set_xlim(-LEFT, xt + TOT)
    ax.set_ylim(-H, 0)
    ax.axis("off")
    ax.text(-LEFT, -0.1, title, fontsize=7.5, va="top")
    y = -1.5
    for lo, hi, text in ((0, nprops - 1, "vertex properties" if nprops > 1 else "vertex props"), (nprops, len(parts) - 1, "adjacency lists")):
        ax.text((X[lo] + X[hi] + 1) / 2, y + 0.07, text, ha="center", va="bottom", fontsize=6.0, color="0.3")
        ax.plot([X[lo] + 0.05, X[hi] + 0.95], [y] * 2, color="0.6", lw=0.5)
    y -= 0.9
    for j, part in enumerate(parts):
        ax.text(X[j] + 0.5, y + 0.12, part.split()[0], ha="center", va="bottom", fontsize=5.2)
    for (q, n), t in touch.items():                         # which query reads / writes each part
        y -= QH
        ax.text(-0.1, y + QH / 2, f"{QUERY[q]} ×{n}", ha="right", va="center", fontsize=5.8)
        for j, part in enumerate(parts):
            if part in t:
                w = t[part] == "writes"
                ax.add_patch(Rectangle((X[j] + 0.1, y + 0.06), 0.8, QH - 0.12, fc="#333333" if w else "white", ec="#333333", lw=0.6))
                ax.text(X[j] + 0.5, y + QH / 2, "write" if w else "read", ha="center", va="center", fontsize=5.2,
                        color="white" if w else "black")
    y -= 0.15
    for s in CODE:                                          # the numbers: a part's ops and bytes under each setting
        y -= NH
        ax.add_patch(Rectangle((-1.15, y + NH / 2 - 0.2), 0.4, 0.4, fc=SHADE[s], ec="0.4", lw=0.4))
        ax.text(-1.3, y + NH / 2, s.capitalize(), ha="right", va="center", fontsize=6.0)
        for j, part in enumerate(parts):
            opt = next(o for o, _ in uni if (o, part) in cost and cost[(o, part)][2] == s)
            o, b, _ = cost[(opt, part)]
            ax.add_patch(Rectangle((X[j], y), 1, NH, fc="white", ec="0.65", lw=0.4))
            ax.text(X[j] + 0.5, y + NH / 2, f"{num(o)}\n{size(b)}", ha="center", va="center", fontsize=5.6, linespacing=1.05)
    ax.text(xt, y + 1.5 * NH, "weighted ops, bytes\nper part and setting", fontsize=5.6, va="center", color="0.3", linespacing=1.0)
    y -= 0.45
    ax.text(-0.1, y + 0.1, "uniform", ha="right", va="bottom", fontsize=5.6, color="0.3")
    ax.text(xt, y + 0.1, "total" + (f" (limit {size(cap)})" if cap else ""), fontsize=5.6, va="bottom", color="0.3")

    def layout_row(y, opt, label, color, note):
        ax.text(-0.1, y + LH / 2, label, ha="right", va="center", fontsize=5.8, color=color, fontweight="bold" if note else "normal")
        for j, part in enumerate(parts):
            ax.add_patch(Rectangle((X[j] + 0.03, y + 0.04), 0.94, LH - 0.08, fc=SHADE[cost[(opt, part)][2]], ec="0.4", lw=0.3))
        o, b = total[opt]
        over = not fits(opt)
        ax.text(xt, y + LH / 2, f"{num(o)} ops, {size(b)}" + (", over limit" if over else "") + (f"  ◀ {note}" if note else ""),
                va="center", fontsize=5.6, color="0.6" if over and not note else color, fontweight="bold" if note else "normal")

    marks = [("fewest ops", any_ops), ("fewest bytes", few_bytes)] + ([("fewest ops in limit", few_ops)] if cap else [])
    for opt, label in uni:                                  # the nine uniform layouts
        y -= LH
        layout_row(y, opt, label, "black", ", ".join(t for t, o in marks if o == opt))
    y -= 0.25
    for opt, label in mixes:                                # the mixes, each against the uniform layout with its mark
        y -= LH
        k = 1 if "bytes" in label else 0
        base = few_bytes if k else few_ops if "limit" in label or not cap else any_ops
        gain = total[base][k] / total[opt][k]
        same = all(cost[(opt, q)][2] == cost[(base, q)][2] for q in parts)
        layout_row(y, opt, label, MIX, f"{gain:.2f}× fewer {('ops', 'bytes')[k]}" if gain > 1.005
                   else ("= " if same else "no gain over ") + dict(uni)[base])
        print(f"{stem}: {label}: {total[opt][0]:,.0f} ops, {total[opt][1]:,.0f} bytes; {gain:.3f}x against {dict(uni)[base]}")
    out = FIGURES / f"layout-choice/{stem}.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    fig.savefig(out.with_suffix(".png"), dpi=300)
    plt.close(fig)
    print(f"wrote {out}")


def main():
    apply_house_style(extralarge=1)
    for p in PANELS:
        draw(*p)


if __name__ == "__main__":
    main()
