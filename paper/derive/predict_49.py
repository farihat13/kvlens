#!/usr/bin/env python3
"""Derive predict-49's tables: predict mode at tier 1 (no store; its tallies counted on the paper's sample of the
owners, kvcost.predict_accuracy.uniform_49_tallies.PAPER_SAMPLE) against the measured cost on the 49 uniform layouts, for
I01 (SF3), BI's B01 (17 queries, SF3) and JOB's J01 (113 queries, IMDb full), in wops and wops_bytes4k.

I01 sums its queries by their frequencies. B01 and J01 sum by their equal-share file (SF3's and IMDb full's at every
scale), the same weights for both models and both sides; checked at the file's own scale: the
measured Sp-Co combined total is the query count within SHARE_TOL. A
check line also prints the predicted side under weights from its own Sp-Co costs, not in the tables (tier 1 costs a few
queries far above their measured cost on Sp-Co, so those weights all but drop them).

Reads one output tree (--from; the paper's runs by default): summary/<dataset>/<scale>/predict-statistics/<workload>/
<the sample's set>/: I01's layouts.csv, B01's and J01's layout_choice/by_query.csv (kvcost predict-accuracy --dataset D=S
--workload W uniform-49 --count-source <the set> writes them); --set names another predicted set's dirs (the same
figure for predictions made another way, for example predicted-tier1, from the full tallies; it also writes predict-49-set.csv, the set and --note, which the draw prints
above the panels so the figure cannot pass for the paper's), --i01, --b01, --j01 name other dirs. A workload whose dataset the run leaves out is skipped and named; one whose dataset is in the run and whose
report the tree does not hold is refused (exit 1, nothing written), naming the file and the command that makes it.

Writes build/data/predict-49-i01.csv (per model and layout, the predicted and measured totals as the I01 run holds
them), predict-49-b01.csv and predict-49-j01.csv (per model and layout, the predicted and measured equal-share sums)
and predict-49-picks.csv (per workload and model: predict's pick, its measured score over the measured best, the
measured best, Spearman's rho of the 49).

    uv run python paper/derive/predict_49.py [--from output --dataset ldbc=0.1 --tables DIR]
"""
import argparse
import csv
import math
from pathlib import Path

from kvcost.layout_cost import query_weighting as QW
from kvevaluator import sizes
from kvcost.plotting.stats import spearman
from kvcost.predict_accuracy import uniform_49_tallies as U
from kvcost.predict_accuracy.layouts import short
import snapshots
from snapshots import add_tree_args, scale_in, tree_of, write_table

MODELS = ("wops", "wops_bytes4k")
SHARE_TOL = 1e-5        # the measured Sp-Co combined total against the query count, relative (by_query.csv's 6 digits)
REF = "props_split_adj_condensed"        # Sp-Co: each equal-share query's cost on it is that query's 1
COLUMNS = ["workload", "model", "cell", "layout", "predicted", "measured"]
NOTE = "predict-49-set"        # the table that names another predicted set than the paper's (--set), with its note
PICK_COLUMNS = ["workload", "model", "form", "pick", "pick_over_measured_best", "measured_best", "spearman"]


def cost(r, side, model):
    """one by_query row's cost on one side in `model` (wops_bytes4k = wops + bytes / 4096)"""
    wops = float(r[f"{side}_wops"])
    return wops if model == "wops" else wops + float(r[f"{side}_bytes"]) / 4096


def i01_rows(i01_dir):
    """the 49 layouts per model, the run's totals as its text (i01_dir: I01's tier-1 pick dir)"""
    with open(i01_dir / "layouts.csv") as fh:
        rows = [{"workload": "i01", "model": r["model"], "cell": r["cell"], "layout": short(r["cell"]),
                 "predicted": r["predicted"], "measured": r["measured"]} for r in csv.DictReader(fh) if r["model"] in MODELS]
    return rows


def equal_share_rows(workload, path, run):
    """the 49 layouts per model, each side's sum over `workload`'s equal-share frequencies (`path`: its tier-1 dir); and
    the check: the predicted side under weights from its own (predicted) combined Sp-Co costs. When the run (--scale) is
    at the scale the equal-share file was made at (its "equal_share" dataset and scale), the measured Sp-Co total must
    be the query count"""
    name = workload.upper()
    with open(path / "by_query.csv") as fh:
        by_query = list(csv.DictReader(fh))
    if not by_query or any(float(r["weight"]) != 1 for r in by_query):   # the number: a writer may print 1 or 1.0
        raise SystemExit(f"{name}: a query with a weight other than 1 (each query runs once)")
    freq = QW.equal_share_frequencies(workload)
    made_at = QW.equal_share(workload)["equal_share"]
    own_scale = sizes.scale(run, made_at["dataset"]) == str(made_at["scale"])
    per = {(m, side): {} for m in MODELS for side in ("predicted", "measured")}
    for r in by_query:
        for m, side in per:
            per[(m, side)].setdefault(r["query"], {})[r["cell"]] = cost(r, side, m)
    meas_ref = {q: c[REF] for q, c in per[("wops_bytes4k", "measured")].items()}
    if set(meas_ref) != set(freq):
        raise SystemExit(f"{name}: by_query.csv's queries differ from the equal-share file's ({sorted(set(meas_ref) ^ set(freq))})")
    total = math.fsum(freq[q] * meas_ref[q] for q in freq)
    if own_scale and abs(total - len(freq)) > SHARE_TOL * len(freq):
        raise SystemExit(f"{name}: the measured Sp-Co combined total is {total!r}, not {len(freq)} (the equal-share file)")
    pred_weight = {q: 1 / c[REF] for q, c in per[("wops_bytes4k", "predicted")].items()}
    rows, check = [], {}
    for m in MODELS:
        pred, meas = per[(m, "predicted")], per[(m, "measured")]
        measured = QW.aggregate(meas, "sum", weight=freq)
        predicted = QW.aggregate(pred, "sum", weight=freq)
        check[m] = QW.aggregate(pred, "sum", weight=pred_weight)
        rows += [{"workload": workload, "model": m, "cell": c, "layout": short(c), "predicted": predicted[c],
                  "measured": measured[c]} for c in measured]
    print(f"{workload}: {len(freq)} queries x {len(measured)} layouts x {len(MODELS)} models; the measured Sp-Co combined "
          f"total {total:.6f}" + (f" (the query count within {SHARE_TOL})" if own_scale else
                                  " (another scale than the equal-share file's: not the query count, not checked)"))
    return rows, check


def pick_row(rows, workload, model, form, predicted=None):
    """predict's pick against the measured best of the 49 (`predicted` {cell: score} replaces the rows' predicted side)"""
    rs = [r for r in rows if r["workload"] == workload and r["model"] == model]
    if len(rs) != 49:
        raise SystemExit(f"{workload} {model}: {len(rs)} layouts, not 49")
    meas = {r["cell"]: float(r["measured"]) for r in rs}
    pred = predicted or {r["cell"]: float(r["predicted"]) for r in rs}
    pick, best = QW.best(pred), QW.best(meas)
    cells = sorted(meas)
    return {"workload": workload, "model": model, "form": form, "pick": short(pick),
            "pick_over_measured_best": meas[pick] / meas[best], "measured_best": short(best),
            "spearman": spearman([pred[c] for c in cells], [meas[c] for c in cells])}


def main():
    ap = argparse.ArgumentParser(description="predict-49's tables, from one tree's prediction runs or the dirs named")
    add_tree_args(ap)
    ap.add_argument("--i01", type=Path, default=None, help="I01's tier-1 pick dir (layouts.csv; default the tree's)")
    ap.add_argument("--b01", type=Path, default=None, help="BI B01's layout_choice dir (by_query.csv; default the tree's)")
    ap.add_argument("--j01", type=Path, default=None, help="JOB J01's layout_choice dir (by_query.csv; default the tree's)")
    ap.add_argument("--set", dest="set_name", default=U.sampled_set(U.PAPER_SAMPLE),
                    help="the predicted set whose report is read (default the paper's: tier 1 from the tallies counted "
                         "on the paper's sample of the owners; another: kvcost predict --save-as, kvcost "
                         "predict-accuracy --count-source)")
    ap.add_argument("--note", default=None,
                    help="with --set: how that set's predictions were made, drawn above the panels (default: the set's name)")
    a = ap.parse_args()
    tree, set_name = tree_of(a.tree), a.set_name
    default = ap.get_default("set_name")
    if a.note and set_name == default:
        ap.error(f"--note names another predicted set's predictions: give --set (not {default})")
    note = (a.tables or snapshots.TABLES_DIR) / f"{NOTE}.csv"
    if set_name == default and note.is_file():       # before any table is written: the dir holds another set's tables
        raise SystemExit(f"predict_49: {note} is another predicted set's ({set_name} writes none): the tables of two sets "
                         f"in one dir; give --tables another dir")
    ldbc, imdb = scale_in(a, "ldbc"), scale_in(a, "imdb")
    place = lambda ds, sc, w: tree.summary(ds, sc, "predict-statistics", w, set_name)    # predict-accuracy uniform-49's
    dirs = {"i01": a.i01 or (ldbc and place("ldbc", ldbc, "i01")),
            "b01": a.b01 or (ldbc and place("ldbc", ldbc, "b01") / "layout_choice"),
            "j01": a.j01 or (imdb and place("imdb", imdb, "j01") / "layout_choice")}
    held, absent = {}, []
    for w, d in dirs.items():
        if not d:
            print(f"{w}: its dataset is not in this run (--scale)")
            continue
        f = d / ("layouts.csv" if w == "i01" else "by_query.csv")
        if f.is_file():
            held[w] = d
        else:
            absent.append(f"{w}: {f}")
    if absent:       # a dataset of the run without its report: never tables with a workload silently left out
        how = ("just results predict-49" if set_name == default else
               f"kvcost predict ... --save-as {set_name}, then kvcost predict-accuracy ... --count-source {set_name} "
               f"(the full tallies' set: just results predict-49-full-tallies)")
        raise SystemExit(f"predict_49: no report of the predicted set {set_name} in this tree, nothing written:\n  "
                         + "\n  ".join(absent) + f"\n  make it: {how} (with the run's --dataset)")
    i01 = i01_rows(held["i01"]) if "i01" in held else []
    b01, b01_check = equal_share_rows("b01", held["b01"], a.run) if "b01" in held else ([], {})
    j01, j01_check = equal_share_rows("j01", held["j01"], a.run) if "j01" in held else ([], {})
    picks = [pick_row(i01, "i01", m, "sum") for m in MODELS if "i01" in held]
    picks += [pick_row(rows, w, m, "spco-share") for w, rows in (("b01", b01), ("j01", j01)) for m in MODELS if w in held]
    for p in picks:
        print(f"{p['workload']} {p['model']} ({p['form']}): pick {p['pick']}, {p['pick_over_measured_best']:.4f}x the "
              f"measured best ({p['measured_best']}); Spearman {p['spearman']:.4f}")
    for w, rows, check in (("b01", b01, b01_check), ("j01", j01, j01_check)):
        for m in MODELS if w in held else ():     # the check, not written: the predicted side weighted by its own (predicted) Sp-Co costs
            p = pick_row(rows, w, m, "spco-share", predicted=check[m])
            print(f"check, {w} {m}, predicted side under predicted Sp-Co weights: pick {p['pick']}, "
                  f"{p['pick_over_measured_best']:.4f}x the measured best ({p['measured_best']}); Spearman {p['spearman']:.4f}")
    for w, rows in (("i01", i01), ("b01", b01), ("j01", j01)):
        if w in held:
            write_table(f"predict-49-{w}", COLUMNS, rows, key=("model", "cell"), out_dir=a.tables)
    write_table("predict-49-picks", PICK_COLUMNS, picks, key=("workload", "model"), out_dir=a.tables)
    # another predicted set than the paper's: the figure says so (the draw prints this line above its panels)
    if set_name != default:
        write_table(NOTE, ["set", "note"], [{"set": set_name, "note": a.note or set_name}], key=None, out_dir=a.tables)


if __name__ == "__main__":
    main()
