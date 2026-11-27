"""Join two predicted captures (`kvcost predict`: levels predicted, the population statistics, and predicted_anchor, with the anchor
statistics too) to a measured capture's step rows, for the body the pick record names per (cell, query) at the
capture's scale (inputs/picks/picks.csv, through grid_capture.record_cell_entries: that scale's own pick where
the record has one). The measured runs are found by the record's body_sha, the body's identity, not by its name: a
capture made before a body was renamed still joins. When the capture holds that body under two names, their counts must
agree (they are the same body), and the first name in sort order is used. A prediction whose body_sha differs from the
record's is refused. The rows carry the lanes only; report.py prices them (cost_models). Runs no plan, opens no store.
"""
import functools
import json
import os

from kvcost.predict_accuracy import predicted as PD
from kvevaluator.capture import capture_layout, step_record
from kvevaluator.measure import grid_capture, workload_plans
from kvevaluator.output_tree import Capture, capture_in
from kvevaluator.plan_choice import candidates

# the lanes the accuracy report compares: a read's counts, its 7 lanes and the read bytes it used (the C++ gate's order)
LANES = [*step_record.READ_OPS, *step_record.READ_BYTE_LANES, *step_record.USED_BYTE_LANES[:2]]
COLUMNS = ["cell", "query", "param", "body_sha", "step", "op", "source"] + LANES + ["rows"]


@functools.lru_cache(maxsize=None)
def levels():
    """the levels of cpp/kvevaluator/src/predict/terms.h kLevels, as `kvevaluator predict --levels-out` writes them"""
    import subprocess
    import tempfile
    from kvevaluator.capture.binary import binary_path
    with tempfile.TemporaryDirectory() as t:
        path = os.path.join(t, "levels.json")
        subprocess.run([binary_path(), "predict", "--levels-out", path], check=True)
        with open(path) as f:
            return tuple(json.load(f, object_hook=lambda d: tuple(sorted(d.items()))))


def statistics_levels():
    """(the population level's name, the anchor level's): the two levels a predicted capture is (no truth card)"""
    stat = [dict(lv) for lv in levels() if not dict(lv)["card"] and not dict(lv)["sampled"]]
    plain = [lv["name"] for lv in stat if not lv["anchor"]]
    anchor = [lv["name"] for lv in stat if lv["anchor"]]
    if len(plain) != 1 or len(anchor) != 1:
        raise SystemExit(f"join: kLevels names {len(plain)} population and {len(anchor)} anchor statistics levels, not 1 each")
    return plain[0], anchor[0]


def measured_capture(path, workload, param_set=None):
    """a measured capture: its dir (…/<workload>/<params>), or a place dir holding <workload>/<params>
    (output_tree.capture_in). `param_set`: the set the predictions bind (the predicted capture's, Capture.param_set); a
    capture of another set is refused"""
    below = capture_in(path, workload, param_set)
    cap = below if os.path.isdir(below.path) else Capture(path)
    if cap.param_set() != param_set:
        raise SystemExit(f"predict-accuracy: the measured capture {cap.path} holds param set "
                         f"{cap.param_set() or 'the default'}, the predictions "
                         f"{param_set or 'the default'}: they would bind different params -- REFUSED")
    return cap


def predicted_steps(cap_dir, cell, queries, workload):
    """{(query, param, body, step): row} of a predicted capture's step rows, and {(query, param, body): body_sha}, each
    body as today names it (a body rewritten count-equal since the capture: its successor, candidates.successor_sha)"""
    shas, now = {}, {}
    for r in PD.rows(cap_dir, "counts", cell):
        if r["query"] in queries:
            name = candidates.current_name(workload, r["query"], r["body"], r["body_sha"])
            now[(r["query"], r["body"])] = name
            shas[(r["query"], r["param_idx"], name)] = candidates.successor_sha(workload, r["query"], r["body_sha"])
    steps = {(r["query"], r["param_idx"], now.get((r["query"], r["body"]), r["body"]), r["step"]): r
             for r in PD.rows(cap_dir, "steps", cell) if r["query"] in queries}
    return steps, shas


def join(sf, workload, caps, measured, cells, queries, lo, hi):
    """(the step rows, sorted; a count line). caps: {the population level's name: its predicted capture dir[, the anchor
    level's: dir]} (statistics_levels); measured: a Capture.
    Exits naming the cause when the capture misses a (cell, query, param) asked, or nothing joined."""
    queries = set(queries)
    out, mismatched, picked_n, joined, unpredicted = [], [], 0, 0, 0
    for cell in cells:
        entries = grid_capture.record_cell_entries(cell, sorted(queries), scale=sf, workload=workload)
        pred = {lv: predicted_steps(d, cell, queries, workload) for lv, d in caps.items()}
        found = {}   # (query, param) -> {the capture's name for the record's body: its counts}
        with open(measured.path_of("counts", cell)) as f:
            for r in capture_layout.reader(f):
                if (r["query"] in queries and lo <= int(r["param_idx"]) <= hi
                        and candidates.successor_sha(workload, r["query"], r["body_sha"]) == entries[r["query"]]["body_sha"]):
                    found.setdefault((r["query"], r["param_idx"]), {})[r["body"]] = tuple(r[l] for l in LANES[:12])
        picks = {}   # (query, param) -> (the capture's name, the record's name, the sha)
        for k, names in found.items():
            if len(set(names.values())) > 1:
                raise SystemExit(f"predict-accuracy: {cell} {k[0]} p{k[1]}: the capture's bodies with the record's "
                                 f"body_sha count differently: {sorted(names)}")
            picks[k] = (sorted(names)[0], entries[k[0]]["body"], entries[k[0]]["body_sha"])
        picked_n += len(picks)
        with open(measured.path_of("steps", cell)) as f:
            for r in capture_layout.reader(f):
                k = (r["query"], r["param_idx"])
                if r["kind"] != "kv" or k not in picks or r["body"] != picks[k][0]:
                    continue
                _, variant, sha = picks[k]
                out.append([cell, r["query"], r["param_idx"], sha, r["step"], r["op"], "measured"]
                           + [float(r[l]) for l in LANES] + [float(r["rows"])])
                for lv in (lv for lv in statistics_levels() if lv in pred):   # the anchor level only when given
                    steps, shas = pred[lv]
                    p = steps.get((r["query"], r["param_idx"], variant, r["step"]))
                    if p is None:
                        unpredicted += lv == statistics_levels()[0]
                        continue
                    if shas[(r["query"], r["param_idx"], variant)] != sha:
                        mismatched.append((cell, r["query"], r["param_idx"], variant, shas[(r["query"], r["param_idx"], variant)], sha))
                        continue
                    out.append([cell, r["query"], r["param_idx"], sha, r["step"], r["op"], lv]
                               + [float(p[l]) for l in LANES] + [p["rows"]])
                    joined += lv == statistics_levels()[0]
    if mismatched:   # left out of the join, counted in its line and marked (kvevaluator.stale)
        from kvevaluator import stale
        stale.note("predictions_skipped", f"{len(mismatched)} predicted (cell, query, param, variant) steps name another "
                   f"body than the capture's and are left out, e.g. {mismatched[0]}")
    # per query, its param rows in lo..hi (a query with no parameter has one row)
    param_set = measured.param_set()
    nrows = {q: max(0, min(hi, len(workload_plans.query_params(workload, sf, q, hi, param_set))) - lo + 1)
             for q in queries}
    want = len(cells) * sum(nrows.values())
    line = (f"predict-accuracy: {picked_n} picked (cell, query, param) of {want} expected; {joined} KV steps joined, "
            f"{unpredicted} with no prediction, {len(mismatched)} skipped (a prediction of another body)")
    if picked_n != want or joined == 0:
        raise SystemExit(line + f"\npredict-accuracy: the capture {measured.path} does not cover every (cell, query, "
                         "param) asked, or nothing joined")
    out.sort(key=lambda x: (x[0], x[1], int(x[2]), x[4], x[6]))
    return out, line


def joined(sf, family, predicted, predicted_anchor, measured, cells, queries, param_rows):
    """([steps.csv rows], its line): a predicted capture (and its anchor level's, or None) joined to the measured capture
    `measured` (its dir or a place dir) at the predicted capture's param set; cells None: every layout predicted"""
    lo, hi = (int(x) for x in param_rows.split("-"))
    param_set = Capture(predicted).param_set()
    plain, anchor = statistics_levels()
    caps = {plain: predicted, **({anchor: predicted_anchor} if predicted_anchor else {})}
    return join(sf, family, caps, measured_capture(measured, family, param_set),
                cells if cells else sorted(PD.cells(predicted)), list(queries), lo, hi)


def write_steps(out, rows):
    """<out>/steps.csv: the joined step rows (COLUMNS), and levels.json beside it (the levels report.py reads:
    `kvevaluator predict --levels-out`, cpp/kvevaluator/src/predict/terms.h kLevels)"""
    import csv
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "steps.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        w.writerows(rows)
    with open(os.path.join(out, "levels.json"), "w") as f:
        json.dump([dict(lv) for lv in levels()], f, indent=2)
        f.write("\n")


def main(argv=None):
    """the join of captures named by path: python -m kvcost.predict_accuracy.join --dataset D=S --family F --queries a,b
    --predicted P [--predicted-anchor PA] --measured M [--param-rows 1-5] --out D LAYOUTS -> D/steps.csv; then
    kvcost predict-accuracy --per-step D reports it"""
    import argparse
    from kvevaluator import layouts as L
    from kvevaluator import workloads
    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("--dataset", required=True, metavar="DATASET=SCALE")
    ap.add_argument("--family", required=True, choices=workloads.names())
    ap.add_argument("--queries", required=True)
    ap.add_argument("--predicted", required=True, help="the predicted capture of the population statistics")
    ap.add_argument("--predicted-anchor", help="the predicted capture with the anchor statistics")
    ap.add_argument("--measured", required=True, help="a measured capture: its dir, or a place dir holding <family>/reported")
    ap.add_argument("--param-rows", default="1-5")
    ap.add_argument("--out", required=True)
    ap.add_argument("layouts", nargs="+", help="the layouts joined (names or cell ids)")
    a = ap.parse_args(argv)
    sf = a.dataset.split("=", 1)[1]
    rows, line = joined(sf, a.family, a.predicted, a.predicted_anchor, a.measured, [L.cell_of(x) for x in a.layouts],
                        a.queries.split(","), a.param_rows)
    print(line)
    write_steps(a.out, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
