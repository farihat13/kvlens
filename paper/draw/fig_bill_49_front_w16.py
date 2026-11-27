#!/usr/bin/env python3
# released from gs-paper at b95936d (evidence/bill-49/fig_bill_49_front_w16.py); edit it there, then run evidence/release_draw.py
"""Paper figure: the DynamoDB front of W16's 49 uniform layouts at SF3 (one column), in the style of
uniform-49-layouts-w16-front-log.pdf.

  figures/evaluation/bill-49-front-w16.pdf

Top: read units against write units of the 49 (DynamoDB's exact form: each read request rounded up alone to 4 KB, each
written item to 1 KB), each over the fewest of the 49 (log); marker = the properties setting, colour = the adjacency
setting; filled = on the 49's lower-left front (the solid step), open = beaten on both axes by another layout. The
dashed lines are equal bills at r = 1, 5, 33.7 and 100 (r = the write price in read units; DynamoDB's is 5; Ch4k-Ch4k is the cheapest up to r = 33.7, where Ch4k-Sp ties it, and Ch4k-Sp above, the first Split adjacency to win), each through the
layout with the fewest bill at that r: a layout under a line is cheaper at that r.
Below: the front's adjacency and properties setting from each of its points to the next, in the settings' colours.
Deterministic counts: no error bars.

Reads only its table: bill-49-i01.csv (its form-exact, option-set-uniform rows: the 49).

    python fig_bill_49_front_w16.py --data <dir of the tables> --out <dir>
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator  # noqa: E402

from common import args, read_table, use_paper_font  # noqa: E402
from paper_style import ADJ_COLOR, CHUNK_SHADE, COLUMN_IN, cell_id, cell_parts, cell_style  # noqa: E402

OUT = "figures/evaluation/bill-49-front-w16.pdf"
R = 5.0                                          # DynamoDB's write price, in read units
RLINES = ((1, "0.15"), (5, "0.15"), (33.7, "0.55"), (100, "0.55"))   # equal-bill lines: r and its grey (r = 5 is DynamoDB's price;
# at 33.7 Ch4k-Sp ties Ch4k-Ch4k; at 100 Ch4k-Sp is the cheapest)
LABEL_Y = {1: 1.6, 5: 1.6, 33.7: 10.0, 100: 1.6}   # each line's label height, clear of the points and of the other labels
PT, PT_SMALL = 7, 6                              # print size at one column
FRONT = "0.2"                                    # the 49's front, as in uniform-49-layouts-w16-front-log
SETTINGS = ("split", "condensed", "chunked_c1k", "chunked_c4k", "chunked_c16k", "chunked", "chunked_c256k")
SHORT = {"split": "Sp", "condensed": "Co", "chunked_c1k": "Ch1k", "chunked_c4k": "Ch4k", "chunked_c16k": "Ch16k",
         "chunked": "Ch64k", "chunked_c256k": "Ch256k"}
HUE = {"split": ADJ_COLOR["split"], "condensed": ADJ_COLOR["condensed"], "chunked_c1k": CHUNK_SHADE[1],
       "chunked_c4k": CHUNK_SHADE[4], "chunked_c16k": CHUNK_SHADE[16], "chunked": CHUNK_SHADE[64],
       "chunked_c256k": CHUNK_SHADE[256]}


def setting(layout, kb):
    return layout if layout != "chunked" else ("chunked" if kb == 64 else f"chunked_c{kb}k")


def short(c):
    return "-".join(SHORT[setting(l, kb)] for l, kb in cell_parts(c))


def front(pts):
    """the lower-left front of (x, y, cell) points, by x"""
    out, best = [], float("inf")
    for x, y, c in sorted(pts):
        if y < best:
            out.append((x, y, c))
            best = y
    return out


def times(v, _=None):
    return f"{v:g}x"


def main():
    data, out_dir = args(__doc__)
    rows = [r for r in read_table(data, "bill-49-i01") if r["form"] == "exact" and r["option_set"] == "uniform"]
    assert len(rows) == 49, len(rows)
    rr, rw = min(float(r["read_units"]) for r in rows), min(float(r["write_units"]) for r in rows)
    pts = [(float(r["read_units"]) / rr, float(r["write_units"]) / rw, r["cell"]) for r in rows]
    bills = {r: {x["cell"]: float(x["read_units"]) + r * float(x["write_units"]) for x in rows} for r, _ in RLINES}
    bill = bills[R]
    fr = front(pts)
    on = {c for _, _, c in fr}
    best = min(bill, key=bill.get)
    star = next((x, y) for x, y, c in pts if c == best)
    x0, x1 = 1 / 1.1, max(x for x, _, _ in pts) * 1.15
    y0, y1 = 1 / 1.3, max(y for _, y, _ in pts) * 1.3

    use_paper_font()
    plt.rcParams.update({"font.size": PT})
    fig = plt.figure(figsize=(COLUMN_IN, 2.75))
    gs = fig.add_gridspec(2, 1, height_ratios=(1, 0.16), hspace=0.06, left=0.15, right=0.985, top=0.89, bottom=0.12)
    ax, bx = fig.add_subplot(gs[0]), fig.add_subplot(gs[1])
    for x, y, c in pts:
        st = cell_style(c)
        ax.scatter([x], [y], s=14, marker=st["marker"], facecolor=st["color"] if c in on else "none",
                   edgecolor="black" if c in on else st["color"], linewidth=0.4 if c in on else 0.7,
                   zorder=4 if c in on else 2)
    ax.step([p[0] for p in fr] + [x1], [p[1] for p in fr] + [fr[-1][1]], where="post", color=FRONT, lw=1.0, zorder=3)
    # equal bill at each r through that r's cheapest: read + r x write = its bill, in each axis's units
    xs = [x0 * (x1 / x0) ** (i / 400) for i in range(401)]
    for r, grey in RLINES:
        b = min(bills[r].values())
        line = [(x, (b - x * rr) / (r * rw)) for x in xs]
        line = [(x, y) for x, y in line if y > y0 / 2] + [((b - r * rw * y0 / 2) / rr, y0 / 2)]   # to the foot
        ax.plot([x for x, _ in line], [y for _, y in line], ls=(0, (4, 2)) if r == R else (0, (2, 2)), lw=0.6,
                color=grey, zorder=2)
        yl = LABEL_Y[r]                                 # the label: beside the line, clear of the points
        xl = (b - r * rw * yl) / rr
        left = r >= 30                                  # the last line runs into the front's step: label it on its left
        ax.text(xl / 1.02 if left else xl * 1.02, yl, f"r = {r:g}", fontsize=PT_SMALL - 1, color=grey,
                ha="right" if left else "left", va="center", zorder=6)
        print(f"r = {r:g}: fewest bill {short(min(bills[r], key=bills[r].get))}")
    for a in (ax, bx):
        a.set_xscale("log")
        a.set_xlim(x0, x1)
        a.xaxis.set_major_locator(FixedLocator([t for t in (1, 1.5, 2, 3, 5) if t <= x1]))
        a.xaxis.set_major_formatter(FuncFormatter(times))
        a.xaxis.set_minor_locator(NullLocator())
        a.tick_params(labelsize=PT_SMALL, pad=1, length=2, width=0.5)
        for s in a.spines.values():
            s.set_linewidth(0.5)
    ax.set_yscale("log")
    ax.set_ylim(y0, y1)
    ax.yaxis.set_major_locator(FixedLocator([t for t in (1, 2, 5, 10, 20, 50, 100) if t <= y1]))
    ax.yaxis.set_major_formatter(FuncFormatter(times))
    ax.yaxis.set_minor_locator(NullLocator())
    ax.set_ylabel("x the fewest write units (log)", labelpad=1, fontsize=PT)
    ax.tick_params(labelbottom=False)
    ax.grid(alpha=0.25, lw=0.4, zorder=0)

    # below: the front's adjacency and properties setting from each point to the next
    nxt = [fr[i + 1][0] if i + 1 < len(fr) else x1 for i in range(len(fr))]
    for j, kind in enumerate(("props", "adj")):
        for (x, _, c), e in zip(fr, nxt):
            (p, pk), (a_, ak) = cell_parts(c)
            s = setting(a_, ak) if kind == "adj" else setting(p, pk)
            bx.axvspan(x, e, ymin=j / 2 + 0.04, ymax=(j + 1) / 2 - 0.04, color=HUE[s], lw=0)
    bx.set_yticks([0.25, 0.75], ["properties", "adjacency"], fontsize=PT_SMALL - 0.5)
    bx.set_ylim(0, 1)
    bx.tick_params(axis="y", length=0)
    bx.set_xlabel("x the fewest read units (log)", labelpad=1, fontsize=PT)

    kw = dict(frameon=False, fontsize=PT_SMALL - 0.5, handlelength=0.9, columnspacing=0.4, handletextpad=0.3,
              borderaxespad=0, borderpad=0)
    colours = [Patch(fc=HUE[s], label=SHORT[s]) for s in SETTINGS]
    colours.append(Line2D([], [], color="0.3", lw=0.6, ls=(0, (4, 2)), label="equal bill"))
    first = fig.legend(handles=colours, loc="upper center", ncol=8, bbox_to_anchor=(0.55, 0.995), **kw)
    marks = [Line2D([], [], ls="", marker="", label="props:")]
    marks += [Line2D([], [], ls="", marker=cell_style(cell_id(s, "split"))["marker"], ms=3.4, mfc="0.45", mec="black",
                     mew=0.4, label=SHORT[s]) for s in SETTINGS]
    marks += [Line2D([], [], ls="", marker="o", ms=3.4, mfc="none", mec="0.45", mew=0.7, label="open: beaten"),
              Line2D([], [], color=FRONT, lw=1.0, ls="-", label="front")]
    fig.legend(handles=marks, loc="upper center", ncol=10, bbox_to_anchor=(0.55, 0.945), **{**kw, "columnspacing": 0.25})
    fig.add_artist(first)
    out = out_dir / OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    print(f"the front of the 49: {len(fr)} layouts, {', '.join(short(c) for _, _, c in fr)}")
    print(f"  fewest bill at r = {R:g}: {short(best)}, {star[0]:.2f}x read, {star[1]:.2f}x write units, "
          f"{'on' if best in on else 'off'} the front")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
