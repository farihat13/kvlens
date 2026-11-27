#!/usr/bin/env python3
"""Derive uniform-and-mixed-i01's table: I01 at SF3 (64 KB chunks), the nine uniform layouts and the three mixes the
figure draws, each fewest under one model (wops, bytes, combined), with their summed costs.

Reads the nine from LAYOUT_CHOICE_SF3_UNGROUPED's options.csv and SUM_OPTIMUM_SF3's sum_optimum.csv (checked to be one
grid: the nine cost the same in both), and the mixes from MIX_SEARCH_SF3's I01 rows. Writes
build/data/uniform-and-mixed-i01.csv: 12 rows (source = the file, option), every column of either file (blank where a
file has none). The three mixes are written in those files' shape: options.csv's for the fewest wops and bytes
(beats_per_type_by_1pct: at least 1% fewer than the per-type answer to the same question), sum_optimum.csv's for the
fewest combined, values rounded as those files are. wops = total_ops (G + 1.1 S + 0.01 N + P + D, reads and writes);
bytes = total_bytes (read, misses included, + written + read by writes).

    uv run python paper/derive/uniform_and_mixed_i01.py
"""
import csv

from kvcost.layout_cost import cost_models as CM
from kvevaluator.layouts import SETTINGS, cell_id
from kvcost.layout_cost.compose import CHANNELS, MEASURES, currencies
from snapshots import PAPER, LAYOUT_CHOICE_SF3_UNGROUPED, SUM_OPTIMUM_SF3, assignment_text, mix_search_row, write_table

OPTIONS = LAYOUT_CHOICE_SF3_UNGROUPED / "i01-rw-ldbc-mix/options.csv"
MIXED = SUM_OPTIMUM_SF3 / "i01-rw-ldbc-mix/sum_optimum.csv"
CELLS = [cell_id(p, a) for a in SETTINGS for p in SETTINGS]
MIXES = ("per_type_direction_fewest_ops", "per_type_direction_fewest_bytes")   # options.csv's names for the two mixes
QUESTION = {"per_type_direction_fewest_ops": ("wops", "fewest ops", "ops"),       # model, question, the lane compared
            "per_type_direction_fewest_bytes": ("bytes", "fewest bytes", "bytes")}
BEAT = 0.99          # per type and direction "beats" per type when it is at least 1% cheaper (workload_story.BEAT)


def option_row(option):
    """one of the two mixes as an options.csv row (workload_story.py's columns and rounding)"""
    model, question, lane = QUESTION[option]
    r = mix_search_row(PAPER, "3", "i01", "per-type-and-direction", question="fewest", model=model, limit=None, weighting="sum")
    per_type = mix_search_row(PAPER, "3", "i01", "per-type", question="fewest", model=model, limit=None, weighting="sum")
    c, pc = currencies(r["lanes"]), currencies(per_type["lanes"])
    return {"option": option, "option_set": "per type and direction", "question": question,
            "label": f"per type and direction: {question}", "kind": "per type and direction", "fits": "",
            "beats_per_type_by_1pct": "yes" if c[lane] < BEAT * pc[lane] else "no",
            "assignment": assignment_text(r["assignment"]), "built": "",
            **{m: round(r["lanes"][m], 3) for m in MEASURES + CHANNELS},
            **{f"total_{k}": round(v, 3) for k, v in c.items()}, "nodes": r["nodes"]}


def sum_row(single):
    """the fewest-combined mix as a sum_optimum.csv row (sum_search.py's columns and rounding); single: the nine's rows"""
    r = mix_search_row(PAPER, "3", "i01", "per-type-and-direction", question="fewest", model="wops_bytes4k", limit=None,
                       weighting="sum")
    t, c = r["lanes"], currencies(r["lanes"])
    b1 = min(single, key=lambda k: (float(single[k]["sum"]), k))
    s1, total = float(single[b1]["sum"]), CM.cost(t, "wops_bytes4k")
    return {"workload": "ldbc_mix", "id": "i01", "option_set": "per type and direction", "option": "fewest_sum",
            "sum": round(total, 3), "total_ops": round(c["ops"], 3), "total_bytes": round(c["bytes"], 3),
            **{k: round(t[k], 3) for k in MEASURES + CHANNELS}, "best_single": b1, "best_single_sum": round(s1, 3),
            "sum_single_over_option": round(s1 / total, 4), "nodes": r["nodes"], "assignment": assignment_text(r["assignment"])}


def read(path):
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames), list(reader)


def main():
    opt_cols, opt_rows = read(OPTIONS)
    rows = {r["option"]: r for r in opt_rows}
    missing = [c for c in CELLS if c not in rows]
    if missing:
        raise SystemExit(f"{OPTIONS}: no row for {missing}")
    sum_cols, summ = read(MIXED)
    # the two files are one grid: the nine uniform layouts cost the same in both
    single = {r["option"]: r for r in summ if r["id"] == "i01" and r["option_set"] == "single"}
    differ = [c for c in CELLS if c not in single or any(abs(float(single[c][k]) - float(rows[c][k])) > 1e-6 * float(rows[c][k])
                                                         for k in ("total_ops", "total_bytes"))]
    if differ:
        raise SystemExit(f"{MIXED} and {OPTIONS} disagree on {differ}: not one grid")
    columns = ["source"] + opt_cols + [c for c in sum_cols if c not in opt_cols]
    out = ([{**rows[o], "source": "options.csv"} for o in CELLS] + [{**option_row(o), "source": "options.csv"} for o in MIXES]
           + [{**sum_row(single), "source": "sum_optimum.csv"}])
    write_table("uniform-and-mixed-i01", columns, [{c: r.get(c, "") for c in columns} for r in out],
                key=("source", "option"))


if __name__ == "__main__":
    main()
