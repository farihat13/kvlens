#!/usr/bin/env python3
"""What each mix on I01's complete per-type front is made of (SF3): six figures -> <out>/plots/*.{png,pdf}.

    python experiments/mix_benefit/plot_w16_front_makeup.py [--front CSV] [--out DIR]

Reads <out>/complete_front.json and uniform_layouts.json (w16_complete_front.py). Drawing only, about 1 min.
  wops_bars_abc        three mixes' wops, stacked by part (A = fewest bytes, B = fewest wops within Sp-Co's bytes,
                       C = fewest wops): how to read the next one
  wops_bars_all        the same bar at every mix of the front, filled
  bytes_bars_abc       three mixes' bytes, stacked by part (horizontal)
  bytes_bars_all       the same at every mix
  front_over_barcode   the front, and below it the setting each type gets at each byte limit
  hull_exchange_rates  the front's lower convex hull: each big move, the types that switch and the wops saved per GB
"""
import argparse
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import PolyCollection
from matplotlib.colors import to_hex, to_rgb
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter, NullFormatter

import w16_front_common as F
from w16_front_common import LISTS, PROPS, SETTINGS, SHORT
import figure_common  # noqa: F401  (puts lib/ on the path)
from kvcost.layout_search.front import uniform_name
from kvcost.plotting.style import ADJ_COLOR, CHUNK_SHADE, DEFAULT_CHUNK_KB

PT = 10
MIX, UNIFORM = "#d81b60", "0.35"               # the mix line and the best uniform line, as in the other front figures


def tint(c, f=0.55):
    """c mixed with white: f = 0 is c, 1 is white"""
    return to_hex([x + (1 - x) * f for x in to_rgb(c)])


# one hue per setting, the cell theme of kvcost.plotting.style (Split purple, Condensed teal, Chunked red at 64 KB);
# an edge part takes the hue, a vertex part a light tint of it
HUE = {**ADJ_COLOR, "chunked": CHUNK_SHADE[DEFAULT_CHUNK_KB]}
COLOR = {s: (tint(HUE[s]), HUE[s]) for s in SETTINGS}
INDEX = "#bdbdbd"
UNITS = [("index", None)] + [("props", p) for p in PROPS] + [("adj", lst) for lst in LISTS]   # stacking order, bottom up
ROWS = UNITS[1:]
WOPS = "wops = GET + 1.1 SEEK + 0.01 NEXT on reads; a write adds its own reads, PUTs and DELETEs. bytes = read + written."


def rects(ax, x0, x1, y0, y1, colors, zorder=3):
    """many filled rectangles as one collection: one ax.bar patch per rectangle made the figures take 2 min"""
    verts, cs = [], []
    for a, b, c, d, col in zip(x0, x1, y0, y1, colors):
        if b > a and d > c:
            verts.append(((a, c), (a, d), (b, d), (b, c)))
            cs.append(col)
    ax.add_collection(PolyCollection(verts, facecolors=cs, edgecolors="none", linewidths=0, zorder=zorder))


def key(u):
    return "None" if u[0] == "index" else str(u)


def setting(m, u):
    return None if u[0] == "index" else m["assign"][f"{u[0]}|{u[1]}"]


def color(m, u):
    s = setting(m, u)
    return INDEX if s is None else COLOR[s][0 if u[0] == "props" else 1]


def ink(u):
    """a part name's colour: white on the dark edge colours, black on the light vertex and index ones"""
    return "white" if u[0] == "adj" else "black"


def share(m, u, cur):
    v = m["units"].get(key(u))
    return v[cur] if v else 0.0


class Fig:
    def __init__(self, mixes, nine, ref, out, unit):
        self.mixes, self.nine, self.ref, self.out, self.unit = mixes, nine, ref, out, unit   # unit: B = 1's cell (Sp-Co)
        for m in mixes:
            m["B"] = m["bytes"] / ref
        self.A, self.C = mixes[0], mixes[-1]
        self.Bm = [m for m in mixes if m["bytes"] <= ref * (1 + 1e-9)][-1]
        self.ABC = (("A", self.A), ("B", self.Bm), ("C", self.C))
        self.xmax = max([m["B"] for m in mixes] + [b for b, _ in nine.values()]) * 1.13
        self.ytop = self.A["ops"] * 1.08                     # linear wops axes: the front's highest mix
        self.ylog = (self.C["ops"] * 0.52, self.A["ops"] * 1.6)

    def front_xy(self):
        xs, ys = [], []
        for i, m in enumerate(self.mixes):
            nxt = self.mixes[i + 1]["B"] if i + 1 < len(self.mixes) else self.xmax
            xs += [m["B"], nxt]
            ys += [m["ops"], m["ops"]]
        return xs, ys

    def uniform_xy(self):
        pts = sorted(self.nine.values())
        xs, ys, best = [], [], float("inf")
        for i, (b, o) in enumerate(pts):
            best = min(best, o)
            nxt = pts[i + 1][0] if i + 1 < len(pts) else self.xmax
            xs += [b, nxt]
            ys += [best, best]
        return xs, ys

    def legend(self, fig, with_index=True, ncol=7):
        hs = [Patch(fc=INDEX, label="index")] if with_index else []
        for s in SETTINGS:
            hs += [Patch(fc=COLOR[s][0], label=f"{SHORT[s]} vertex"), Patch(fc=COLOR[s][1], label=f"{SHORT[s]} edge")]
        hs += [Line2D([], [], color=MIX, lw=1.8, label="mix front"),
               Line2D([], [], color=UNIFORM, lw=1.4, ls="--", label="best uniform")]
        fig.legend(handles=hs, loc="upper center", ncol=ncol, frameon=False, fontsize=PT - 2, handlelength=1.0,
                   columnspacing=0.8, handletextpad=0.3)

    def base(self, ax, logx=True, logy=True):
        ux, uy = self.uniform_xy()
        fx, fy = self.front_xy()
        ax.plot(ux, uy, color=UNIFORM, lw=1.4, ls="--", zorder=5)
        ax.plot(fx, fy, color=MIX, lw=1.8, zorder=6)
        for b, o in self.nine.values():
            ax.plot([b], [o], "o", ms=4, mfc="white", mec="0.15", mew=0.8, zorder=7)
        if logx:
            ax.set_xscale("log")
            ax.set_xticks([0.25, 0.5, 1, 2])
            ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
            ax.xaxis.set_minor_formatter(NullFormatter())
        if logy:
            ax.set_yscale("log")
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v / 1e9:g}B" if v >= 1e9 else f"{v / 1e6:g}M"))
        ax.set_xlabel(f"byte limit B (x the bytes of {self.unit})")
        ax.set_ylabel(r"$\mathit{wops}$")
        ax.grid(alpha=0.3, lw=0.5)

    def mark(self, ax, letter, m, dx=5, dy=5):
        ax.plot([m["B"]], [m["ops"]], "o", color=MIX, ms=6, zorder=9)
        ax.annotate(letter, (m["B"], m["ops"]), textcoords="offset points", xytext=(dx, dy), fontsize=PT + 1,
                    fontweight="bold", color=MIX, zorder=10)

    def vbar(self, ax, m, x, w):
        """a stacked bar from 0 to the mix's wops at x: index, vertex types, edge types, bottom up; big parts named"""
        y = 0.0
        for u in UNITS:
            v = share(m, u, "ops")
            if v <= 0:
                continue
            ax.bar(x, v, width=w, bottom=y, color=color(m, u), edgecolor="white", lw=0.4, zorder=3)
            if v > 0.045 * self.ytop:
                ax.text(x, y + v / 2, u[1] or "index", ha="center", va="center", fontsize=PT - 3, zorder=4, color=ink(u))
            y += v

    def hbar(self, ax, m, y, h):
        """a stacked bar from 0 to the mix's bytes (x B) at height y; big parts named"""
        x, tot = 0.0, m["bytes"] / self.ref
        for u in UNITS:
            v = share(m, u, "bytes") / self.ref
            if v <= 0:
                continue
            ax.barh(y, v, height=h, left=x, color=color(m, u), edgecolor="white", lw=0.4, zorder=3)
            if v > 0.013 * self.xmax * len(u[1] or "index") and x + v < tot - 0.1:
                ax.text(x + v / 2, y, u[1] or "index", ha="center", va="center", fontsize=PT - 3, zorder=4, color=ink(u))
            x += v

    def caption(self, fig, text, y=0.01):
        fig.text(0.01, y, text, fontsize=6.5, color="0.3", ha="left", va="bottom", wrap=True)

    def save(self, fig, name):
        d = os.path.join(self.out, "plots")
        os.makedirs(d, exist_ok=True)
        for ext in ("pdf", "png"):
            fig.savefig(os.path.join(d, f"{name}.{ext}"), dpi=200)
        plt.close(fig)
        print(f"wrote {d}/{name}.png")

    # ---- the figures ----
    def wops_bars(self, every):
        fig, ax = plt.subplots(figsize=(5.6, 3.9))
        self.base(ax, logy=False)
        if every:
            ms = self.mixes
            xs = [m["B"] for m in ms]
            ws = [(ms[i + 1]["B"] if i + 1 < len(ms) else self.xmax) - m["B"] for i, m in enumerate(ms)]
            y = [0.0] * len(ms)
            for u in UNITS:                                # one call per part: its segment in every mix
                v = [share(m, u, "ops") for m in ms]
                rects(ax, xs, [a + b for a, b in zip(xs, ws)], y, [a + b for a, b in zip(y, v)], [color(m, u) for m in ms])
                y = [a + b for a, b in zip(y, v)]
        else:
            for _, m in self.ABC:
                self.vbar(ax, m, m["B"], m["B"] * 0.22)
        for letter, m in self.ABC:
            self.mark(ax, letter, m, dx=-14 if letter != "C" else 6, dy=4)
        ax.set_ylim(0, self.ytop)
        ax.set_xlim(self.A["B"] * 0.85, self.xmax)
        self.legend(fig)
        what = (f"every one of the {len(self.mixes):,} mixes of the front, each filled to the next" if every else
                f"three mixes: A = fewest bytes, B = fewest wops within {self.unit}'s bytes (B = 1), C = fewest wops")
        self.caption(fig, f"I01, SF3, a layout per type. Bars: the wops of {what}, stacked by part (linear y, so a "
                          f"segment's height is its share). Circles: the nine uniform layouts. {WOPS}")
        fig.subplots_adjust(left=0.12, right=0.97, top=0.86, bottom=0.21)
        self.save(fig, "wops_bars_all" if every else "wops_bars_abc")

    def bytes_bars(self, every):
        fig, ax = plt.subplots(figsize=(5.6, 4.1))
        self.base(ax, logx=False)
        if every:
            ms = self.mixes
            ys = [m["ops"] for m in ms]
            hs = [(ms[i - 1]["ops"] if i > 0 else m["ops"] * 1.03) - m["ops"] for i, m in enumerate(ms)]
            x = [0.0] * len(ms)
            for u in UNITS:
                v = [share(m, u, "bytes") / self.ref for m in ms]
                rects(ax, x, [a + b for a, b in zip(x, v)], ys, [a + b for a, b in zip(ys, hs)], [color(m, u) for m in ms])
                x = [a + b for a, b in zip(x, v)]
        else:
            for _, m in self.ABC:
                self.hbar(ax, m, m["ops"], m["ops"] * 0.28)
        for letter, m in self.ABC:
            self.mark(ax, letter, m, dx=6, dy=6)
        ax.set_xlim(0, self.xmax)
        ax.set_ylim(*self.ylog)
        self.legend(fig)
        what = (f"every one of the {len(self.mixes):,} mixes of the front" if every else
                f"three mixes: A = fewest bytes, B = fewest wops within {self.unit}'s bytes (B = 1), C = fewest wops")
        self.caption(fig, f"I01, SF3, a layout per type. Bars: the bytes of {what}, stacked by part (linear x from 0, "
                          f"so a segment's length is its share), drawn at the mix's wops. Circles: the nine uniform "
                          f"layouts. {WOPS}")
        fig.subplots_adjust(left=0.12, right=0.97, top=0.86, bottom=0.25)   # a four-line caption
        self.save(fig, "bytes_bars_all" if every else "bytes_bars_abc")

    def barcode_rows(self):
        """the types with at least 1% of some mix's wops or bytes: vertex types first, then by their largest wops share"""
        rows = [u for u in ROWS if any(share(m, u, c) / m[c] >= 0.01 for m in self.mixes for c in ("ops", "bytes"))]
        weight = {u: max(share(m, u, "ops") / m["ops"] for m in self.mixes) for u in rows}
        hidden = [u[1] for u in ROWS if u not in rows]
        return sorted(rows, key=lambda u: (u[0] != "props", -weight[u])), hidden

    def front_over_barcode(self):
        fig, (ax, bx) = plt.subplots(2, 1, figsize=(5.6, 5.4), sharex=True,
                                     gridspec_kw={"height_ratios": [1.0, 0.9], "hspace": 0.06})
        self.base(ax)
        ax.set_xlabel("")
        for letter, m in self.ABC:
            self.mark(ax, letter, m)
        ax.set_xlim(self.A["B"] * 0.97, self.xmax)
        rows, hidden = self.barcode_rows()
        nv = sum(u[0] == "props" for u in rows)
        print(f"barcode rows {len(rows)} of {len(ROWS)}, hidden (under 1% everywhere): {hidden}")
        ms = self.mixes
        bxs = [m["B"] for m in ms]
        bws = [(ms[i + 1]["B"] if i + 1 < len(ms) else self.xmax) - m["B"] for i, m in enumerate(ms)]
        for j, u in enumerate(rows):
            rects(bx, bxs, [a + b for a, b in zip(bxs, bws)], [j - 0.45] * len(ms), [j + 0.45] * len(ms),
                  [color(m, u) for m in ms])
        bx.set_yticks(range(len(rows)), [u[1] for u in rows], fontsize=PT - 3)
        bx.axhline(nv - 0.5, color="0.2", lw=0.8)
        bx.set_ylim(len(rows) - 0.5, -0.5)
        for _, m in self.ABC:
            bx.axvline(m["B"], color=MIX, lw=0.8, ls=":")
        bx.set_xlabel(f"byte limit B (x the bytes of {self.unit})")
        for txt, yy, va in (("vertex types", 1 - nv / len(rows) / 2, "center"),
                            ("edge types", 1 - (nv + 0.5) / len(rows) - 0.03, "top")):
            bx.text(0.995, yy, txt, transform=bx.transAxes, ha="right", va=va, fontsize=PT - 3, color="0.2",
                    bbox=dict(fc="white", ec="none", pad=1))
        self.legend(fig, with_index=False, ncol=4)
        self.caption(fig, "I01, SF3, a layout per type. Top: the fewest wops within each byte limit (mix front) and the "
                          "best uniform layout that fits. Bottom: the setting the front's mix gives each type at that "
                          f"limit; types under 1% of every mix's wops and bytes are left out ({', '.join(hidden)}). "
                          + WOPS)
        fig.subplots_adjust(left=0.17, right=0.97, top=0.89, bottom=0.17)
        self.save(fig, "front_over_barcode")

    def hull_exchange_rates(self):
        hull = []                      # the lower-left convex hull: the mixes that minimize wops + lambda x bytes
        for p in self.mixes:
            while len(hull) >= 2:
                (x1, y1), (x2, y2) = (hull[-2]["bytes"], hull[-2]["ops"]), (hull[-1]["bytes"], hull[-1]["ops"])
                if (x2 - x1) * (p["ops"] - y1) - (y2 - y1) * (p["bytes"] - x1) <= 0:
                    hull.pop()
                else:
                    break
            hull.append(p)
        print(f"hull: {len(hull)} of {len(self.mixes)} front mixes")
        fig, ax = plt.subplots(figsize=(5.6, 4.1))
        self.base(ax)
        ax.plot([m["B"] for m in hull], [m["ops"] for m in hull], "o-", color="0.1", ms=3.5, lw=0.9, zorder=8)
        last_x, big = None, 10e6 * self.C["ops"] / 28.6e6     # label moves that save more than ~35% of C's wops
        for i in range(1, len(hull)):
            p, m = hull[i - 1], hull[i]
            ch = sorted((u for u in ROWS if setting(p, u) != setting(m, u)),
                        key=lambda u: -abs(share(p, u, "ops") - share(m, u, "ops")))
            rate = (p["ops"] - m["ops"]) / ((m["bytes"] - p["bytes"]) / 1e9)
            print(f"  B {p['B']:.3f} -> {m['B']:.3f}: {p['ops'] / 1e6:7.1f}M -> {m['ops'] / 1e6:7.1f}M wops, "
                  f"{rate / 1e3:8.1f}K wops saved per GB: "
                  + ", ".join(f"{u[1]}{' props' if u[0] == 'props' else ''} {SHORT[setting(p, u)]}->{SHORT[setting(m, u)]}"
                              for u in ch))
            if p["ops"] - m["ops"] < big:
                continue
            txt = ", ".join(f"{u[1]}{' props' if u[0] == 'props' else ''}→{SHORT[setting(m, u)]}" for u in ch[:2]) + \
                (f"\n{rate / 1e6:.1f}M wops/GB" if rate >= 1e6 else f"\n{rate / 1e3:.0f}K wops/GB")
            near = last_x is not None and m["B"] / last_x < 1.15   # right after another label: put it below the line
            last_x = m["B"]
            xm, ym = (p["B"] * m["B"]) ** 0.5, (p["ops"] * m["ops"]) ** 0.5
            ax.annotate(txt, (xm, ym), textcoords="offset points", xytext=(6, -26) if near else (4, 2),
                        fontsize=PT - 3.5, color="0.1", zorder=10, bbox=dict(fc="white", ec="none", pad=0.5, alpha=0.85))
        ax.set_xlim(self.A["B"] * 0.95, self.xmax)
        fig.legend(handles=[Line2D([], [], color=MIX, lw=1.8, label="mix front"),
                            Line2D([], [], color="0.1", lw=0.9, marker="o", ms=3.5, label="its lower convex hull"),
                            Line2D([], [], color=UNIFORM, lw=1.4, ls="--", label="best uniform")],
                   loc="upper center", ncol=3, frameon=False, fontsize=PT - 2)
        self.caption(fig, "I01, SF3, a layout per type. The hull's mixes are those that minimize wops + lambda x bytes "
                          "for some price lambda; each big move is labelled with the two types whose switch matters "
                          "most and the wops it saves per extra GB. " + WOPS)
        fig.subplots_adjust(left=0.12, right=0.97, top=0.90, bottom=0.19)
        self.save(fig, "hull_exchange_rates")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    F.add_input_args(ap)
    a = ap.parse_args()
    out = F.abspath(a.out)
    mixes = F.read_json(out, "complete_front.json")
    nine = {uniform_name(u["assign"]): (u["bytes"], u["ops"]) for u in F.read_json(out, "uniform_layouts.json")}
    ref, unit = F.ref_bytes(a.front)
    nine = {n: (b / ref, o) for n, (b, o) in nine.items()}
    plt.rcParams.update({"font.size": PT, "font.family": "sans-serif", "mathtext.fontset": "dejavusans"})
    f = Fig(mixes, nine, ref, out, unit)
    f.wops_bars(False)
    f.wops_bars(True)
    f.bytes_bars(False)
    f.bytes_bars(True)
    f.front_over_barcode()
    f.hull_exchange_rates()


if __name__ == "__main__":
    main()
