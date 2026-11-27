#!/usr/bin/env python3
"""Numbers for the mixed-layout results at SF3: the W04 and W15 byte-budget rows, and W02's and W11's parts figures
(parts-w02, parts-w11). Reads the layout-choice study (experiments/layout_choice/run-layout-choice.sh) in the tree
--from names, at the run's LDBC scale (--dataset): summary/ldbc/<scale>/layout-choice/reported/wops_bytes4k/, its
budget_picks.csv and each workload's options.csv and breakdown.csv.

Keys:
  lc.sf<scale>.budget.<wid>.b<B>                      one table cell: "ops / bytes" vs B's unit (Sp-Co), with a mark
                                                  ($^u$ a single layout, $^*$ per type and direction: priced, not
                                                  built; no mark = a per type mix; "no fit" when nothing fits B)
  lc.w02.part.<person|likes|knows|hascreator>.<sp|co|ch>.<ops|bytes>, lc.w02.mix.<part>.<ops|bytes|setting>,
  lc.w02.uni.<p><a>.<ops|bytes|over>, lc.w02.<limit|best|mix>.*, lc.w02.gain   W02's parts figure (parts-w02) at B = W02_B
  lc.w11.*                                        the same for W11's parts figure (parts-w11) at B = W11_B; its
                                                  columns person, message, knows, written (the five lists only the
                                                  inserts write, summed); lc.w11.index.<ops|bytes> the index's cost,
                                                  the same in every layout; lc.<w02|w11>.<best|mix>.opsfull the exact wops;
                                                  lc.<w02|w11>.gain.short the gain at one decimal

The table holds the rows the budget keys are computed from:
  build/data/layout-choice-numbers-budget.csv      budget_picks.csv (scale)

    uv run python paper/derive/layout_choice.py [--from output --dataset ldbc=0.1]   -> build/numbers/layout_choice.tex and the table
"""
import argparse
import csv
from pathlib import Path

from snapshots import add_tree_args, scale_in, tree_of, write_numbers, write_table
from kvcost.layout_cost.settings import B_UNIT
from kvevaluator.layouts import cell_id, name_of, side_name

SF3_BUDGET_ROWS = ["w04", "w15"]          # the budget rows at SF3
REFERENCE = B_UNIT                       # B's unit: Sp-Co's bytes (settings.B_UNIT)
W02_B = "2"                              # parts-w02's byte limit: at SF3, B = 1 leaves no room (the mix is Ch64k-Co itself); B = 2 and 3 give the same mix
STUDY = None                             # the study's wops_bytes4k dir in the tree (main); parts-w02 is drawn at SF3 (a reader can re-make it on a small machine)
W11_B = "1"                              # parts-w11's byte limit: Sp-Co's own bytes. At SF3 the best uniform within it is
                                         # Co-Sp (Ch64k-Sp prints the same 35K at 4x the bytes), and the mix (knows Ch64k, Message
                                         # Co) is 2.20x fewer wops


def mixing_only(o, name):
    """the per type mix over the grid's three settings only: per_type_no_grouping_<q> in a snapshot that also holds the
    grouped props options' solves (retired), else per_type_<q>, which is that mix"""
    return o.get(name.replace("per_type_", "per_type_no_grouping_", 1)) or o[name]


SHORTER = {"split": "Sp", "condensed": "Co", "chunked": "Ch"}     # a setting's letters in a number key (sp, co, ch)
W02_PARTS = {"Person props": "person", "likes lists": "likes", "knows lists": "knows", "hasCreator lists": "hascreator"}
# W11's figure columns: a column sums its parts (every layout gives them one setting); the index is no choice
W11_COLUMNS = {"person": ["Person props"], "message": ["Message props"], "knows": ["knows lists"],
               "written": ["containerOf lists", "hasCreator lists", "hasTag lists", "isLocatedIn lists", "replyOf lists"]}


def compact(x):
    """weighted operations as parts-w02 prints them: 26, 1.1K, 18K, 827K, 1.0M"""
    x = float(x)
    if x >= 1e6:
        return f"{x / 1e6:.1f}M"
    if x >= 1e4:
        return f"{x / 1e3:.0f}K"
    if x >= 1e3:
        return f"{x / 1e3:.1f}K"
    return f"{x:.0f}"


def shown(text):
    """the value a compact() number shows: "35K" -> 35000.0"""
    scale = {"K": 1e3, "M": 1e6}.get(text[-1], 1.0)
    return float(text.rstrip("KM")) * scale


def compact_bytes(x):
    """bytes as parts-w02 prints them, decimal units, two significant digits under 100: 209 KB, 2.3 MB, 1.7 GB"""
    x = float(x)
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if x >= scale:
            v = x / scale
            return f"{v:.0f} {unit}" if v >= 10 else f"{v:.1f} {unit}"
    return f"{x:.0f} B"


def w02_keys(out):
    """parts-w02's numbers (parts_keys on W02 at W02_B; W02 reads no index)"""
    parts_keys(out, "w02", STUDY / "w02-r-analytics", W02_B, {pk: [part] for part, pk in W02_PARTS.items()})


def w11_keys(out):
    """parts-w11's numbers (parts_keys on W11 at W11_B; its inserts write the index, the same in every layout)"""
    parts_keys(out, "w11", STUDY / "w11-rw-people-analytics-content-firehose", W11_B, W11_COLUMNS, fixed=["index"])


def parts_keys(out, wid, d, b, columns, fixed=()):
    """a parts figure's numbers, lc.<wid>.*: each column (its parts summed) under each setting (from the diagonal layout
    that gives every part that setting), the mix at b x Sp-Co's bytes column by column, the nine uniform totals (over
    the limit or not), the best uniform within the limit and the gain. `fixed`: parts no layout changes (the index),
    summed into lc.<wid>.index.*. Ops and bytes: the workload totals' currencies, frequency-weighted (breakdown.csv)."""
    w = {"G": 1, "S": 1.1, "N": 0.01, "P": 1, "D": 1}
    o = {r["option"]: r for r in csv.DictReader(open(d / "options.csv"))}
    cost = {}
    for r in csv.DictReader(open(d / "breakdown.csv")):
        f = float(r["how_often"])
        c = cost.setdefault((r["option"], r["part"]), [0.0, 0.0])
        c[0] += f * sum(w[k] * float(r[k] or 0) for k in w)
        c[1] += f * (float(r["read_bytes"] or 0) + float(r["write_rmw_bytes"] or 0) + float(r["write_bytes"] or 0))
    drawn = sorted(p for ps in columns.values() for p in ps)
    parts = sorted({p for _o, p in cost})
    if parts != sorted(drawn + list(fixed)):
        raise SystemExit(f"{wid.upper()}'s parts changed: {parts} (the figure draws {drawn}, fixed {list(fixed)})")

    def col(option, ck):
        return [sum(cost[(option, p)][i] for p in columns[ck]) for i in (0, 1)]
    for ck in columns:
        for s, sk in SHORTER.items():
            ops_, bytes_ = col(cell_id(s, s), ck)
            out[f"lc.{wid}.part.{ck}.{sk.lower()}.ops"] = compact(ops_)
            out[f"lc.{wid}.part.{ck}.{sk.lower()}.bytes"] = compact_bytes(bytes_)
    if fixed:
        per = {tuple(sum(cost[(r, p)][i] for p in fixed) for i in (0, 1)) for r in o if any((r, p) in cost for p in fixed)}
        if len(per) != 1:
            raise SystemExit(f"{wid.upper()}: {list(fixed)} cost differently across the options: {sorted(per)}")
        (ops_, bytes_), = per
        out[f"lc.{wid}.index.ops"], out[f"lc.{wid}.index.bytes"] = compact(ops_), compact_bytes(bytes_)
    limit = float(b) * float(o[REFERENCE]["total_bytes"])
    singles = [r for r in o.values() if r["option_set"] == "single"]
    for r in singles:
        key = "".join(SHORTER[x].lower() for x in r["option"][len("props_"):].split("_adj_"))
        out[f"lc.{wid}.uni.{key}.ops"] = compact(r["total_ops"])
        out[f"lc.{wid}.uni.{key}.bytes"] = compact_bytes(r["total_bytes"])
        out[f"lc.{wid}.uni.{key}.over"] = "1" if float(r["total_bytes"]) > limit * (1 + 1e-9) else "0"
    # the best uniform within the limit, as the figure shows it: the fewest wops as printed (compact), then the fewest
    # bytes
    within = [r for r in singles if float(r["total_bytes"]) <= limit * (1 + 1e-9)]
    best = min(within, key=lambda r: (shown(compact(r["total_ops"])), float(r["total_bytes"])))
    mix = mixing_only(o, f"per_type_B{b}")
    setting = dict(kv.split("=") for kv in mix["assignment"].split("; "))
    for ck, ps in columns.items():
        given = {setting[p] for p in ps}
        if len(given) != 1:
            raise SystemExit(f"{wid.upper()}: the mix gives column {ck}'s parts different settings: {given}")
        ops_, bytes_ = col(mix["option"], ck)
        out[f"lc.{wid}.mix.{ck}.ops"], out[f"lc.{wid}.mix.{ck}.bytes"] = compact(ops_), compact_bytes(bytes_)
        out[f"lc.{wid}.mix.{ck}.setting"] = side_name(given.pop())
    out[f"lc.{wid}.limit.name"], out[f"lc.{wid}.limit.bytes"] = name_of(REFERENCE), compact_bytes(limit)
    out[f"lc.{wid}.limit.b"] = b
    out[f"lc.{wid}.best.name"] = name_of(best["option"])
    order = list(SHORTER)                    # the figure's grid: columns = props setting, rows = adjacency setting (0 = Sp)
    bp, ba = best["option"][len("props_"):].split("_adj_")
    out[f"lc.{wid}.best.col"], out[f"lc.{wid}.best.row"] = str(order.index(bp)), str(order.index(ba))
    out[f"lc.{wid}.best.ops"], out[f"lc.{wid}.best.bytes"] = compact(best["total_ops"]), compact_bytes(best["total_bytes"])
    out[f"lc.{wid}.mix.ops"], out[f"lc.{wid}.mix.bytes"] = compact(mix["total_ops"]), compact_bytes(mix["total_bytes"])
    out[f"lc.{wid}.gain"] = f"{float(best['total_ops']) / float(mix['total_ops']):.2f}"
    out[f"lc.{wid}.gain.short"] = f"{float(best['total_ops']) / float(mix['total_ops']):.1f}"     # as the text types it: 2.2
    # the exact counts, for a box where two uniform layouts round to the same compact number (W11: Ch64k-Sp and Co-Sp, 35K)
    out[f"lc.{wid}.best.opsfull"] = f"{float(best['total_ops']):,.0f}"
    out[f"lc.{wid}.mix.opsfull"] = f"{float(mix['total_ops']):,.0f}"
    # the columns and the fixed parts add up to the totals: the figure's rows explain its totals
    for r in singles + [mix]:
        if abs(sum(cost[(r["option"], p)][0] for p in parts) - float(r["total_ops"])) > 1e-6 * float(r["total_ops"]):
            raise SystemExit(f"{wid.upper()} {r['option']}: the parts' ops do not add up to its total")


def main():
    global STUDY
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_tree_args(ap)
    a = ap.parse_args()
    scale = scale_in(a, "ldbc")
    if scale is None:
        raise SystemExit("layout_choice reads the LDBC layout-choice study: the run (--dataset) leaves ldbc out")
    STUDY = Path(tree_of(a.tree).summary("ldbc", scale, "layout-choice")) / "reported" / "wops_bytes4k"
    if not (STUDY / "budget_picks.csv").is_file():
        raise SystemExit(f"no layout-choice study in {STUDY}: experiments/layout_choice/run-layout-choice.sh --sf {scale} "
                         f"makes it (just results parts-w11)")
    out = {}
    # --- one pick per byte budget (the W04 and W15 rows at SF3)
    # mixing only: a budget_picks.csv with grouped props solves also picks with those options (options the search does not offer)
    picks, budgets = {}, set()
    for r in csv.DictReader(open(STUDY / "budget_picks.csv")):
        if r.get("grouped_options", "no") == "no":
            picks[(r["id"], r["budget_B"])] = r
            budgets.add(r["budget_B"])
    for wid in SF3_BUDGET_ROWS:
        for b in sorted(budgets, key=float):
            r = picks[(wid, b)]
            key = f"lc.sf{scale}.budget.{wid}.b{b.replace('.0', '').replace('.', '')}"      # b05, b1, b2, b3
            if r["fits"] != "yes":
                out[key] = "no fit"
                continue
            mark = {"single": "$^{u}$", "per type": "", "per type and direction": "$^{*}$"}[r["pick_kind"]]
            out[key] = f"{float(r['ops_vs_ref']):.2f}\\,/\\,{float(r['bytes_vs_ref']):.2f}{mark}"
    w02_keys(out)
    w11_keys(out)
    write_numbers("layout_choice", out, f"layout choice at SF{scale}: the W04 and W15 budget rows ({STUDY.parent.name}/"
                  f"{STUDY.name}/budget_picks.csv) and W02's and W11's parts figures (options.csv, breakdown.csv)",
                  out_dir=a.tables)
    columns, rows = read(STUDY / "budget_picks.csv", scale=f"SF{scale}")
    write_table("layout-choice-numbers-budget", columns, [{c: r.get(c, "") for c in columns} for r in rows],
                ("scale", "line"), out_dir=a.tables)


def read(path, **tags):
    """(columns, rows) of a snapshot CSV, each row with `tags` and its line in the file (the header is line 1)"""
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        rows = [{**tags, "line": f"{i:05d}", **r} for i, r in enumerate(reader, 2)]
        return list(tags) + ["line"] + list(reader.fieldnames), rows


if __name__ == "__main__":
    main()
