#!/usr/bin/env python3
"""A local search for a mix per type, for a model or a workload the exact search cannot take (search-mix): the exact
bill is not linear in the counts, so the solve without the cost cache refuses it, and JOB's cost cache would reach about 4e18 entries.

First improvement: start from the best uniform layout, change one part's setting at a time (parts in sorted order,
settings in DIAGONALS' order), keep a change as soon as the score drops, and sweep again until a whole sweep keeps
none. The result is a local optimum, NOT proven: the row says so. The score is the frequency-weighted sum, the sum
form: a benchmark with no frequencies of its own (BI, JOB) is given as its equal-share frequency file
(query_weighting.EQUAL_SHARE_DIR), as for any workload.

    python -m kvcost.layout_search.local_search --dataset imdb --sf full --workload inputs/workloads/imdb/equal-share/j01.json \\
        --grid <the dataset's captures/ place> --size 1k ... --model bill_exact --chunk-view measured --out <file>.json
writes {"workload", "workload_file", "start", "score", "sum", "queries", "form", "model", "sweeps", "assignment",
"wall_s", "inputs"} (score = sum here; the assignment as a search-mix row's: results.row_assignment; inputs: the key of
what was read and asked, search.inputs_key with the model). A file there with the same inputs is kept and nothing is
searched; --fresh searches again.
"""
import argparse
import hashlib
import json
import math
import os
import time

from kvcost.layout_cost import query_weighting as QW
from kvcost.layout_search import results as R
from kvevaluator.layouts import cell_id
from kvevaluator.layouts import DIAGONALS

MOVE_TOL = 1e-12   # a change is kept only when it lowers the score by more than this share of it


def first_improvement(units, start, score, settings=DIAGONALS, log=None):
    """(assign, its score, sweeps): from `start` ({part: setting}), each part in sorted order tries each other setting in
    `settings`' order and keeps the first that lowers score(assign) by more than MOVE_TOL of it; sweeps until a sweep
    keeps none. score: {part: setting} -> float"""
    assign, best = dict(start), score(start)
    sweep, moved = 0, True
    while moved:
        moved, sweep = False, sweep + 1
        for u in sorted(units):
            for s in settings:
                if s == assign[u]:
                    continue
                trial = {**assign, u: s}
                v = score(trial)
                if v < best * (1 - MOVE_TOL):
                    assign, best, moved = trial, v, True
                    if log:
                        log(f"  sweep {sweep}: {'|'.join(u)} -> {s}: {best:.5f}")
    return assign, best, sweep


def scorer(w, data, model):
    """(score, the workload's parts): score({part: setting}) = the fsum over items of frequency x cost in `model`"""
    from kvcost.layout_cost import compose
    units = compose.workload_units(w, data)
    often = [f for _, _, f, _ in w["items"]]

    def score(assign):
        return math.fsum(f * c for f, (_, _, c) in zip(often, QW.assign_rows(w, data, assign, model)))
    return score, units


def workload_of(a):
    """main's workload: an id or a file (an equal-share file for BI and JOB); refuses another form or a --ref"""
    from kvcost.layout_cost import benchmarks
    from kvevaluator.capture import workload_file as WF
    if a.form != "sum" or a.ref is not None:
        raise SystemExit("local_search: an equal share is --form sum on the workload's equal-share frequency file "
                         "(--workload inputs/workloads/<dataset>/equal-share/<id>.json); no other form or --ref")
    if a.workload in QW.EQUAL_SHARE_ONLY:
        raise SystemExit(f"local_search: {a.workload} defines no frequencies: give its equal-share file "
                         f"(--workload {QW.equal_share_id(a.workload)})")
    path = WF.resolve(a.workload, a.dataset, a.sf)
    _, w = benchmarks.read_workload(path)
    if w.get("equal_share"):                                    # an equal-share file: the tracked file's checks (model,
        QW.equal_share_given(path, QW.equal_share_base(w["id"]))   # chunk view, scale), and the given file is that file
    return w


def main(argv=None):
    from kvcost.layout_cost import benchmarks, compose
    from kvcost.layout_cost import cost_models as CM
    from kvcost.layout_cost.settings import parse_sizes
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dataset", required=True, choices=benchmarks.DATASETS)
    ap.add_argument("--sf", required=True)
    ap.add_argument("--workload", required=True, help="the workload id (i01; an equal-share file's: j01-equal-share) or a "
                    "workload file")
    ap.add_argument("--grid", required=True, help="the dataset's captures/<dataset>/<scale> place (each layout read "
                    "there by output_tree.layout_capture)")
    ap.add_argument("--size", action="append", default=[], metavar="NAME")
    ap.add_argument("--model", required=True, choices=("wops_bytes4k", "bill_exact"))
    ap.add_argument("--chunk-view", required=True, choices=("half", "measured"))
    ap.add_argument("--form", default="sum", help="sum (the only form: frequency x cost)")
    ap.add_argument("--ref", help="refused: an equal share is the sum over the workload's equal-share file")
    benchmarks.add_params_arg(ap)
    ap.add_argument("--in-sample", action="store_true", help="cost a capture with no held-out set in-sample (LinkBench)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--fresh", action="store_true", help="search again when --out holds a result of the same inputs")
    a = ap.parse_args(argv)
    if a.model == "bill_exact" and a.chunk_view != "measured":
        raise SystemExit("bill_exact needs --chunk-view measured (a write's last chunk as the capture found it)")
    sizes = parse_sizes(a.size, a.grid)
    w = workload_of(a)
    composer = compose.Composer(criterion=None, sizes=sizes, plan_rule=compose.PlanRule(in_sample=a.in_sample or None),
                                families=benchmarks.families_read(a.dataset, [w]), param_set=a.param_set,
                                chunk_view=compose.resolve_chunk_view(a.chunk_view), bill=CM.needs_bill(a.model))
    data = compose.load(a.grid, False, a.dataset, composer=composer)
    from kvcost.layout_search import search as S
    inputs = hashlib.sha256(json.dumps([S.inputs_key(data, w), a.model]).encode()).hexdigest()[:32]
    if not a.fresh and os.path.exists(a.out):
        with open(a.out) as f:
            if json.load(f).get("inputs") == inputs:
                print(f"local_search: already answered: {a.out} was searched on the same inputs; nothing searched "
                      f"(--fresh searches again)")
                return 0
    score, units = scorer(w, data, a.model)
    settings = [s for s in DIAGONALS if s in {"split", "condensed", "chunked"} | {o for o, _, _ in sizes}]
    uniform = {cell_id(p, q): {u: (p if u[0] == "props" else q) for u in units} for q in settings for p in settings}
    scores = {c: score(x) for c, x in uniform.items()}
    start = min(scores, key=lambda c: (scores[c], c))
    print(f"local_search: {a.workload}, {len(units)} parts, {len(w['items'])} queries, {len(settings)} settings; "
          f"start {start} {scores[start]:.5f}", flush=True)
    t0 = time.time()
    assign, best, sweeps = first_improvement(units, uniform[start], score, settings, log=lambda m: print(m, flush=True))
    n = len(w["items"])
    out = {"workload": w["id"], "workload_file": R.workload_file(a.workload),
           "start": start, "score": best, "sum": best,
           "queries": n, "form": "sum", "model": a.model, "sweeps": sweeps,
           "assignment": R.row_assignment(assign), "wall_s": round(time.time() - t0, 3), "inputs": inputs}
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
        f.write("\n")
    print(f"local_search: done after {sweeps} sweeps, {time.time() - t0:.0f} s: {best:.5f} (start "
          f"{scores[start]:.5f}) -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
