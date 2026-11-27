#!/usr/bin/env python3
"""The numbers in a workload's "why" text, computed from its own priced data (so each scale's README gets its own).

A why text names each number as {name}; its "why_numbers" entry defines it from the data workload_story.py writes:
    ("run", measure, query, part, option)     one run of `query` on `part` (None = every part), as breakdown.csv
    ("runs", measure, query, part, option)    the same x the query's how_often (the workload's share)
    ("total", measure, option)                the workload total, as options.csv
    ("ratio", ref_a, ref_b)                   a / b, printed as "N.NNx"
    ("share", ref_a, ref_b)                   a / b, printed as a percentage
    ("sum", ref, ...) / ("diff", ref_a, ref_b)   for a share's numerator
measure = a breakdown / options column, or "ops" / "bytes" (compose.currencies); option = a cell or option name, or
"best_single_ops" / "best_single_bytes" (the cheapest of the 9 single layouts at this scale). A plain number left in the text
must be listed in "why_scale_free" with the reason it holds at every scale; check_why_numbers.py enforces both.

    python3 experiments/layout_choice/why_numbers.py --snapshot <layout-choice dir>/wops_bytes4k   # print
    ... --rewrite   # replace each w*/README.md's "**Why:**" line with the text rendered from that folder's own CSVs
"""
import argparse
import csv
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
TOKEN = re.compile(r"[A-Za-z_]*\d[\w.,]*")    # a token holding a digit; one that starts with a letter is a name (nhop2, p10)


def currencies(t):
    """compose.currencies on a row (kept here so this module reads CSVs without compose's grid loader)"""
    return {"ops": t["read_ops"] + t["write_rmw_ops"] + t["write_ops"],
            "bytes": t["read_bytes"] + t["write_rmw_bytes"] + t["write_bytes"]}


def is_bytes(measure):
    return "bytes" in measure or measure.endswith(("_key", "_value"))


def fmt_num(v):
    return f"{v:,.0f}" if v >= 10 else f"{v:,.1f}"


def fmt_bytes(v):
    for unit, d in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if v >= d:
            return f"{v / d:,.1f} {unit}"
    return f"{v:,.0f} B"


def fmt_ratio(v):
    return f"{v:,.0f}x" if v >= 10 else f"{v:.2f}x"


def fmt_share(v):
    return f"{100 * v:.2g}%" if v < 0.1 else f"{100 * v:.0f}%"


class Data:
    """a workload's priced data: totals[option] and runs[(option, query, part)] (per run) + how_often[query]"""

    def __init__(self, totals, runs, how_often, singles):
        self.totals, self.runs, self.how_often = totals, runs, how_often
        # the aliases: the cheapest single layout in each currency
        self.alias = {f"best_single_{c}": min(singles, key=lambda o, c=c: currencies(totals[o])[c]) for c in ("ops", "bytes")}
        # a snapshot with grouped props solves holds them beside the others: there a text's per_type_<q> is
        # its "no grouping" solve, the one the grid's three settings give (the search offers no grouped options)
        self.alias.update({o.replace("_no_grouping_", "_"): o for o in totals if "_no_grouping_" in o})

    @classmethod
    def from_dir(cls, d):
        """read a workload's options.csv and breakdown.csv (a working dir or a snapshot)"""
        num = lambda r: {k: float(v) for k, v in r.items() if v not in ("", None) and _isnum(v)}
        rows = list(csv.DictReader(open(os.path.join(d, "options.csv"))))
        totals = {r["option"]: num(r) for r in rows}
        singles = [r["option"] for r in rows if r["kind"] == "single"]
        runs, often = {}, {}
        for r in csv.DictReader(open(os.path.join(d, "breakdown.csv"))):
            runs[(r["option"], r["query"], r["part"])] = num(r)
            often[r["query"]] = float(r["how_often"])
        return cls(totals, runs, often, singles)

    def value(self, ref):
        kind = ref[0]
        if kind in ("ratio", "share"):
            return self.value(ref[1]) / self.value(ref[2])
        if kind == "sum":
            return sum(self.value(r) for r in ref[1:])
        if kind == "diff":
            return self.value(ref[1]) - self.value(ref[2])
        if kind == "total":
            _, m, option = ref
            return _measure(self.totals[self.alias.get(option, option)], m)
        _, m, q, part, option = ref
        option = self.alias.get(option, option)
        rows = [v for (o, qq, p), v in self.runs.items() if o == option and qq == q and (part is None or p == part)]
        if not rows:
            raise KeyError(f"no breakdown row for {ref}")
        v = sum(_measure(r, m) for r in rows)
        return v * self.how_often[q] if kind == "runs" else v

    def text(self, ref):
        v = self.value(ref)
        if ref[0] == "ratio":
            return fmt_ratio(v)
        if ref[0] == "share":
            return fmt_share(v)
        return fmt_bytes(v) if is_bytes(ref[1]) else fmt_num(v)


def _isnum(s):
    try:
        float(s)
        return True
    except ValueError:
        return False


def _measure(row, m):
    return currencies(row)[m] if m in ("ops", "bytes") else row[m]


def render(w, data):
    """the workload's why text with every {name} filled from `data` (KeyError on an undefined name)"""
    defs = w.get("why_numbers", {})
    return PLACEHOLDER.sub(lambda m: data.text(defs[m.group(1)]), w["why"])


def problems(w):
    """what is wrong with a why text: a plain number not declared scale-free, a {name} with no definition, a
    definition or scale-free entry the text no longer uses"""
    out, why = [], w.get("why", "")
    defs, free = w.get("why_numbers", {}), w.get("why_scale_free", {})
    names = set(PLACEHOLDER.findall(why))
    plain = [re.match(r"[\d.,]*\d", t).group(0) for t in TOKEN.findall(PLACEHOLDER.sub("", why)) if t[0].isdigit()]
    out += [f"plain number {n!r} not in why_scale_free (compute it, or say why it holds at every scale)"
            for n in plain if n not in free]
    out += [f"{{{n}}} has no why_numbers entry" for n in sorted(names - set(defs))]
    out += [f"why_numbers {n!r} is not used in the text" for n in sorted(set(defs) - names)]
    out += [f"why_scale_free {n!r} is not in the text" for n in sorted(set(free) - set(plain))]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--snapshot", required=True, help="a layout-choice output dir holding w*/options.csv")
    ap.add_argument("--rewrite", action="store_true", help="rewrite each README's Why line in place (text only)")
    a = ap.parse_args()
    from kvcost.layout_cost import benchmarks
    changed = 0
    for name, w in benchmarks.workloads("ldbc", "3").items():
        if not w.get("why"):
            continue
        (d,) = glob.glob(os.path.join(a.snapshot, f"{w['id']}-*"))
        why = render(w, Data.from_dir(d))
        if not a.rewrite:
            print(f"{w['id']}: {why}\n")
            continue
        path = os.path.join(d, "README.md")
        lines = open(path).read().split("\n")
        (i,) = [k for k, ln in enumerate(lines) if ln.startswith("**Why:** ")]
        if lines[i] != f"**Why:** {why}":
            lines[i] = f"**Why:** {why}"
            with open(path, "w") as f:
                f.write("\n".join(lines))
            changed += 1
    if a.rewrite:
        print(f"{changed} README Why lines rewritten under {a.snapshot}")


if __name__ == "__main__":
    main()
