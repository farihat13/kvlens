#!/usr/bin/env python3
# released from gs-paper at 6f1420b (evidence/latency-steady-ldbc/tab_latency_selection.py); edit it there, then run evidence/release_draw.py
"""Paper table: does the op-byte cost pick a fast layout? (the tabular only; the draft writes the caption and label)

  tables/latency-selection.tex   the tabular for the paper
  tables/latency-selection.md    the same rows, for a reader

One row per workload (LDBC Interactive, LDBC BI, JOB), over the same 12 uniform layouts and the same points as
latency-vs-cost-linear.pdf: the fastest layout by steady latency, the pick (the layout with the fewest op-byte cost,
wops + bytes / 4,096), the selection regret (the pick's latency over the fastest's) and Spearman's rho of the 12 (cost
against latency). A workload with no layout timed gives a row of dashes.

Reads only its tables, as latency-vs-cost.pdf (load() of fig_latency_vs_cost.py).

    python tab_latency_selection.py --data <dir of the tables> --out <dir>   # -> <out>/tables/latency-selection.{tex,md}
"""
import fig_latency_vs_cost as L
from common import args
from paper_style import cell_parts

WORKLOADS = ("Interactive", "BI", "JOB")
MD_HEAD = ("Steady latency of the 12 uniform layouts: the fastest, the layout with the lowest op-byte cost (wops + "
           "bytes / 4,096), its latency over the fastest's (regret), and Spearman's rho of the 12 (cost against latency).")


def short(cell):
    """a layout's short name, props then adjacency: Sp-Co, Ch1k-Ch4k"""
    name = lambda layout, kb: f"Ch{kb}k" if layout == "chunked" else {"split": "Sp", "condensed": "Co"}[layout]  # noqa: E731
    return "-".join(name(layout, kb) for layout, kb in cell_parts(cell))


def main():
    data, out_dir = args(__doc__)
    pts = L.load(data)
    rows = []                                      # (workload, fastest, pick, regret, rho); None when not timed
    for w in WORKLOADS:
        layouts = [q for q in pts[w] if q[0] == "layout"]
        if not layouts:
            rows.append((w, None, None, None, None))
            print(f"{w}: no layout timed in this run: a row of dashes")
            continue
        p, cheapest, fastest = L.to_best(layouts, False)
        regret = next(q[3] for q in p if q[1] == cheapest)
        rho = L.spearman([q[2] for q in p], [q[3] for q in p])
        rows.append((w, short(fastest), short(cheapest), regret, rho))
        print(f"{w}: {len(p)} layouts, fastest {short(fastest)}, pick {short(cheapest)}, regret {regret:.4f}x, "
              f"Spearman {rho:.4f}")
    tex = [r"\begin{tabular}{@{}lllrr@{}}", r"\toprule",
           r"Workload & Fastest & Lowest op-byte cost & Regret & $\rho$ \\", r"\midrule"]
    md = [MD_HEAD, "", "| Workload | Fastest | Lowest op-byte cost | Regret | rho |", "|---|---|---|---|---|"]
    for w, fast, pick, regret, rho in rows:
        if fast is None:
            tex.append(rf"{w} & -- & -- & -- & -- \\")
            md.append(f"| {w} | - | - | - | - |")
        else:
            tex.append(rf"{w} & {fast} & {pick} & {regret:.2f}$\times$ & {rho:.2f} \\")
            md.append(f"| {w} | {fast} | {pick} | {regret:.2f}x | {rho:.2f} |")
    tex += [r"\bottomrule", r"\end{tabular}"]
    out = out_dir / "tables" / "latency-selection.tex"
    out.parent.mkdir(parents=True, exist_ok=True)
    for path, text in ((out, tex), (out.with_suffix(".md"), md)):
        path.write_text("\n".join(text) + "\n")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
