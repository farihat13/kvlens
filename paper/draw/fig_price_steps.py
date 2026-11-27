#!/usr/bin/env python3
# released from gs-paper at 4b5c722 (evidence/price-steps/fig_price_steps.py); edit it there, then run evidence/release_draw.py
"""Paper figure: how a layout is priced, plan step by plan step (full width).

  figures/layout-choice/price-steps.pdf

One read of the microbenchmark (who liked a person's posts; one parameter, one plan) at SF30: its three plan steps,
the part each step reads, and each step's KV operations as measured in the three uniform layouts Sp-Sp, Co-Co and
Ch64k-Ch64k. A layout's cost for the query is one cell per step, chosen by the setting the layout gives the step's part.
Two layouts are priced that way: the uniform Co-Ch64k, whose price is checked here against its own measured steps (the
script stops if they differ), and one mix. Weighted ops = 1.1 SEEK + GET + 0.01 NEXT. Counts are deterministic.

main.tex shows the TikZ version (figures/layout-choice/price-steps.tex); this one is not in the paper.

Reads only its table, price-steps.csv (the artifact's paper/derive/price_steps.py makes it from the SF30 grid
sf30/meta-grid-20261002, and checks Co-Ch64k): the query's step rows in Sp-Sp, Co-Co, Ch64k-Ch64k and Co-Ch64k.

    python fig_price_steps.py --data <dir of price-steps.csv> --out <dir>
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle

from common import args, read_table
from paper_style import apply_house_style, cell_id, cell_name

CODE = ("split", "condensed", "chunked")       # the three uniform layouts the table shows, one setting on both sides
# plan step id, what it does, the part it reads, whether that part is vertex properties
STEPS = [("ms", "read a person's\nmessage list", "hasCreator lists", False), ("po", "keep the Posts", "Message props", True),
         ("lk", "read each post's\nlikers", "likes lists", False)]
MIX, UNIFORM = "#d81b60", "0.25"      # the mix colour of the options map; a uniform layout in dark grey
# the two layouts priced in the figure: name, colour, the setting each gives the three steps' parts
PRICED = [("uniform Co-Ch64k", UNIFORM, ("chunked", "condensed", "chunked")),
          ("mix: hasCreator Sp, Message Co, likes Ch64k", MIX, ("split", "condensed", "chunked"))]


def steps_of(table, props, adj):
    """{plan step: (S, G, N)} of the query in the uniform layout props x adj"""
    return {r["step"]: tuple(int(r[k]) for k in "SGN") for r in table if r["layout"] == cell_id(props, adj)}


def wops(c):
    return 1.1 * c[0] + c[1] + 0.01 * c[2]


def counts(c):
    return " + ".join(f"{v:,} {k}" for v, k in zip(c, ("SEEK", "GET", "NEXT")) if v)


def short(v):
    return f"{v:,.0f}" if v >= 100 else f"{v:.1f}"


def main():
    data, out_dir = args(__doc__)
    table = read_table(data, "price-steps")
    apply_house_style(extralarge=1)
    cell = {s: steps_of(table, s, s) for s in CODE}               # the three measured uniform layouts
    price = lambda pick: [wops(cell[s][step[0]]) for s, step in zip(pick, STEPS)]
    # the check the figure states: Co-Ch64k priced from the three equals Co-Ch64k as built
    built = sum(wops(c) for st, c in steps_of(table, "condensed", "chunked").items() if st in {s[0] for s in STEPS})
    assert abs(sum(price(PRICED[0][2])) - built) < 1e-6, (sum(price(PRICED[0][2])), built)

    fig = plt.figure(figsize=(7.0, 2.6))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 37)
    ax.axis("off")
    x0, cw, rh, ytab = 13.5, 19.5, 4.6, 23.2
    for y, text in ((32.0, "plan step"), (27.2, "part it reads"), (24.6, "measured in")):
        ax.text(0.3, y, text, fontsize=6, va="center", color="0.3")
    for j, (_, what, part, _) in enumerate(STEPS):
        x = x0 + j * cw
        ax.add_patch(FancyBboxPatch((x + 0.8, 29.6), cw - 3.6, 4.8, boxstyle="round,pad=0.2,rounding_size=0.8",
                                    fc="#f3f3f3", ec="0.3", lw=0.6))
        ax.text(x + cw / 2 - 1, 32.0, what, ha="center", va="center", fontsize=5.8, linespacing=0.95)
        if j < len(STEPS) - 1:
            ax.annotate("", (x + cw + 0.6, 32.0), (x + cw - 2.6, 32.0), arrowprops=dict(arrowstyle="->", lw=0.7, color="0.3"))
        ax.text(x + cw / 2 - 1, 27.2, part, ha="center", va="center", fontsize=6, fontweight="bold")
    for i, s in enumerate(CODE):
        y = ytab - (i + 1) * rh
        ax.text(x0 - 0.8, y + rh / 2, cell_name(cell_id(s, s)), ha="right", va="center", fontsize=6)
        for j, (step, _, _, _) in enumerate(STEPS):
            c = cell[s][step]
            ax.add_patch(Rectangle((x0 + j * cw, y), cw - 2, rh, fc="white", ec="0.7", lw=0.4))
            ax.text(x0 + j * cw + cw / 2 - 1, y + rh / 2, f"{counts(c)}\n= {short(wops(c))} ops", ha="center", va="center",
                    fontsize=5.4, linespacing=1.05)
    xs = x0 + len(STEPS) * cw
    ax.text(xs, 24.6, "a layout's cost for this query:\none cell per step, by the setting\nthe layout gives the step's part",
            fontsize=5.8, va="center", color="0.3", linespacing=1.0)
    for k, (name, color, pick) in enumerate(PRICED):
        inset = 0.45 * k                                           # the second outline sits inside the first
        for j, s in enumerate(pick):
            i = list(CODE).index(s)
            ax.add_patch(Rectangle((x0 + j * cw + inset, ytab - (i + 1) * rh + 0.6 * inset), cw - 2 - 2 * inset,
                                   rh - 1.2 * inset, fc="none", ec=color, lw=1.3, zorder=5))
        v = price(pick)
        y = 18.6 - k * 8.0
        ax.text(xs, y, name, fontsize=5.8, va="center", color=color, fontweight="bold")
        ax.text(xs, y - 2.4, " + ".join(short(a) for a in v) + f" = {sum(v):,.1f} ops", fontsize=5.8, va="center", color=color)
        if k == 0:
            ax.text(xs, y - 4.4, f"built and measured: {built:,.1f}", fontsize=5.4, va="center", color="0.3")
        print(f"{name}: {' + '.join(f'{a:.2f}' for a in v)} = {sum(v):.2f}" + (f"; built: {built:.2f}" if k == 0 else ""))
    ax.text(0.3, 3.4, "A workload's cost: this sum for every query, times how often the query runs (mean over its parameters). "
            "Bytes add the same way.", fontsize=5.8, va="center", color="0.3")
    ax.text(0.3, 1.2, "Three uniform layouts give every step under every setting; the other six are built as a check, "
            "and to know which plans a mix may use.", fontsize=5.8, va="center", color="0.3")
    OUT = out_dir / "figures/layout-choice/price-steps.pdf"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT)
    fig.savefig(OUT.with_suffix(".png"), dpi=300)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
