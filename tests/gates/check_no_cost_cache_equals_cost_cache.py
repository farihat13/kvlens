#!/usr/bin/env python3
"""kvcost search-mix without the mix cost cache (its default: python/kvcost/layout_search/bodies.py) gives the answers
of search-mix --cost-cache (the solve on the mix cost cache): each question is run both ways on the same grid, and the
two rows must hold the same totals (wops, bytes, the model's) and the same score, to 1e-9 relative, both proven. An
assignment may differ on a tie only (the totals then agree; counted).

    python3 tests/gates/check_no_cost_cache_equals_cost_cache.py --dataset ldbc --sf 3 --from <grid> --workload <id or json> \\
        [--settings Sp,Co,Ch64k ...] [--by-direction no|yes|both] [--cost-model wops_bytes4k ...]
        [--combine-queries sum ...] [--byte-limit 2x ...]
Every combination of the lists is one question (--byte-limit only under the sum, and not on bytes; by direction only
on the grid's three settings, as search-mix takes it; not a question search-mix solves on the cost cache by itself,
cli/search.py cost_cache_use: the fewest bytes, a byte limit, a bill model). Prints each question and the count compared; exit 1 on a
difference, an unproven row, a failed run, or 0 questions compared.
"""
import argparse
import itertools
import json
import os
import subprocess
import sys
import tempfile

from kvevaluator.output_tree import Tree, own_tree, tree_of
from kvevaluator.paths import ROOT
from kvcost.cli import search as SR
from kvcost.layout_search import results as R

REL = 1e-9


def close(a, b):
    return abs(a - b) <= REL * max(1.0, abs(b))


def run(a, out, workload, extra):
    """search-mix on the tree whose captures place --from names, its row written into the output tree `out`"""
    cmd = [sys.executable, "-m", "kvcost.cli.cli", "search-mix", "--dataset", f"{a.dataset}={a.sf}", "--from",
           str(tree_of(a.from_dir)), "--workload", workload, *extra]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=own_tree(out))
    if r.returncode != 0:
        print(r.stdout[-1500:], r.stderr[-1500:])
        return None
    wid = json.load(open(workload))["id"] if workload.endswith(".json") else workload
    stem = R.stem(*SR.settings_option_set(extra[extra.index("--settings") + 1], "--by-direction" in extra))
    with open(Tree(out).summary(a.dataset, a.sf, "mix-search", wid, f"{stem}.json")) as f:
        rows = json.load(f)["rows"]
    return rows[-1] if len(rows) == 1 else None          # one question per out dir


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--sf", required=True)
    ap.add_argument("--from", dest="from_dir", required=True, help="a grid capture root")
    ap.add_argument("--workload", action="append", required=True, help="an id or a workload file; repeat")
    ap.add_argument("--settings", action="append", default=None,
                    help="the settings a part may take, as search-mix takes them (default: Sp,Co,Ch64k); repeat")
    ap.add_argument("--by-direction", choices=("no", "yes", "both"), default="both",
                    help="each settings list with a part per type (no), per direction (yes), or both (default)")
    ap.add_argument("--cost-model", action="append", default=None, help="default: wops_bytes4k")
    ap.add_argument("--combine-queries", action="append", default=None, help="default: sum")
    ap.add_argument("--byte-limit", action="append", default=[], help="byte limits, 2x or 68MB (sum only); repeat")
    a = ap.parse_args()
    a.from_dir = os.path.abspath(a.from_dir)
    tmp = tempfile.mkdtemp(prefix="no-cost-cache-", dir=os.environ.get("TMPDIR"))
    n, bad, ties = 0, [], 0
    dirs = {"no": [False], "yes": [True], "both": [False, True]}[a.by_direction]
    for wl, settings, by_dir, model, form, within in itertools.product(
            a.workload, a.settings or ["Sp,Co,Ch64k"], dirs, a.cost_model or ["wops_bytes4k"],
            a.combine_queries or ["sum"], [None] + a.byte_limit):
        if within is not None and (form != "sum" or model == "bytes"):
            continue
        if by_dir and SR.settings_option_set(settings)[1]:      # search-mix takes a part per direction on Sp,Co,Ch64k only
            continue
        if SR._needs_cost_cache(argparse.Namespace(front=False, no_walk=False, model=model, trace=None, limits=None,
                                                   jobs=1, byte_limit=within)):
            print(f"  skip {model}{'' if within is None else ' within ' + within}: search-mix solves it on the cost "
                  f"cache by itself (cost_cache_use)")
            continue
        extra = (["--settings", settings, *(["--by-direction"] if by_dir else []), "--cost-model", model,
                  "--combine-queries", form] + ([] if within is None else ["--byte-limit", within]))
        what = (f"{os.path.basename(wl)} {settings}{' by direction' if by_dir else ''} {model} {form}"
                + ("" if within is None else f" within {within}"))
        tag = f"q{n + len(bad)}"
        tab = run(a, os.path.join(tmp, tag, "cost-cache"), wl, extra + ["--cost-cache"])
        free = run(a, os.path.join(tmp, tag, "free"), wl, extra)
        if tab is None or free is None:
            bad.append(f"{what}: a run failed")
            continue
        if free.get("solver") != "no-cost-cache" or tab.get("solver") == "no-cost-cache":
            bad.append(f"{what}: the rows' solvers are {tab.get('solver')!r} and {free.get('solver')!r}")
            continue
        ok = tab["proven"] and free["proven"] and tab["fits"] == free["fits"]
        ok = ok and all(close(free["totals"][k], tab["totals"][k]) for k in tab["totals"])
        if tab["score"]:
            ok = ok and close(free["score"]["value"], tab["score"]["value"]) and \
                free["score"].get("additive_reference") == tab["score"].get("additive_reference")
        n += 1
        if not ok:
            bad.append(f"{what}: cost cache {tab['totals']} {tab['score']} proven {tab['proven']}; no cost cache {free['totals']} "
                       f"{free['score']} proven {free['proven']}")
        ties += ok and tab["assignment"] != free["assignment"]
        print(f"  {'ok  ' if ok else 'DIFF'} {what}: {free['nodes']} nodes without the cost cache, {tab['nodes']} with it"
              f"{'; another assignment at the same totals' if ok and tab['assignment'] != free['assignment'] else ''}")
    for line in bad:
        print("  DIFFER", line)
    good = not bad and n > 0
    print(f"check_no_cost_cache_equals_cost_cache {'OK' if good else 'FAIL'}: {n} questions compared, {len(bad)} differ or failed, "
          f"{ties} tie(s) at another assignment; rows under {tmp}")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
