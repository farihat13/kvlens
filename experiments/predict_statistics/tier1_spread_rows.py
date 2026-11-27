#!/usr/bin/env python3
"""The spread rows from the four reports: per (queries, currency, statistics) I02's pick x over the
measured best, Sp-Co, Co-Co, median, worst, gap share (The spread, I02 row) and Spearman of the 49 (Headline).
Owner: experiments/predict_statistics (run-predict-statistics-results.sh, stage tier1).
  tier1_spread_rows.py <reports dir>   (holding tier2-q19, tier1-q19, tier2-q12, tier1-q12)"""
import os
import sys


def table(lines, head):
    i = next(n for n, l in enumerate(lines) if l.startswith(head))
    rows = []
    for l in lines[i + 1:]:
        if l.startswith("## "):
            break
        if l.startswith("|") and not l.startswith("|---"):
            rows.append([c.strip() for c in l.strip().strip("|").split("|")])
    return rows[0], rows[1:]


def main():
    root = sys.argv[1]
    print("| queries | currency | statistics | pick | Sp-Co | Co-Co | median | worst | gap share | Spearman | measured best |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for n in ("19", "12"):
        for cur in ("wops_bytes4k", "wops"):
            for side, label in (("tier2", "tiers 1+2"), ("tier1", "tier 1 + flat")):
                lines = open(os.path.join(root, f"{side}-q{n}", "layout_choice", "summary.md")).read().splitlines()
                h, hr = table(lines, "## Headline")
                sp = [r for r in hr if r[0] == cur][0][h.index("I02: Spearman of the 49")]
                h2, sr = table(lines, "## The spread")
                r = [r for r in sr if r[0] == cur and r[1] == "I02"][0]
                g = dict(zip(h2, r))
                print(f"| {n} | {cur} | {label} | {g['pick']} | {g['Sp-Co']} | {g['Co-Co']} | {g['median']} | {g['worst']} | "
                      f"{g['gap share']} | {sp} | {g['measured best']} |")


if __name__ == "__main__":
    main()
