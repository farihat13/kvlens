#!/usr/bin/env python3
"""BI on a Chunked cell at another chunk size gives the same answers: every run ok, the same bodies as the base cell,
and per (query, body, param) the same result_hash and rows as the base (the grid's Chunked/Chunked at 64 KB). Runs are
matched by body sha (a body renamed with the same content still matches); a query whose one unmatched body has another
sha on each side (an edit that changed the body) is matched by name and listed. Exit 1 on any difference, an unmatched
run, or a count other than --expect-runs / --expect-bodies. Opens no store.

    python3 experiments/meta/check_bi_chunk_answers.py --capture output/captures/ldbc/sf3/bi/reported \\
        --cell props_chunked_c16k_adj_chunked_c16k --base <another capture>/bi/reported [--base-cell props_chunked_adj_chunked] \\
        [--expect-runs 280 --expect-bodies 56]
"""
import argparse
import json
import os
import sys
from kvevaluator import workloads
from kvevaluator.capture import capture_layout
from kvevaluator.output_tree import Capture
from kvevaluator.plan_choice import candidates


def load(root, cell):
    with open(Capture(root).path_of("counts", cell)) as f:
        rows = list(capture_layout.reader(f))
    with open(Capture(root).record(cell)) as f:
        rec = json.load(f)
    return rows, rec


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--capture", required=True)
    ap.add_argument("--cell", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--base-cell", default="props_chunked_adj_chunked")
    ap.add_argument("--expect-runs", type=int)
    ap.add_argument("--expect-bodies", type=int)
    a = ap.parse_args()
    # a body rewritten count-equal since a capture is matched as its successor (candidates.successor_sha); the
    # workload is the capture's (a capture dir is <...>/<workload>/<params>)
    wl = Capture(os.path.abspath(a.capture)).workload()
    if wl not in workloads.names():
        raise SystemExit(f"[bi-chunk-answers] REFUSED: {a.capture}: its parent dir {wl!r} is no workload "
                         f"({', '.join(workloads.names())}); give a <...>/<workload>/<params> capture dir")
    new, rec = load(a.capture, a.cell)
    old, brec = load(a.base, a.base_cell)
    new, old = ([{**r, "body_sha": candidates.successor_sha(wl, r["query"], r["body_sha"])} for r in rows]
                for rows in (new, old))
    b = rec.get("binary", {})
    print(f"[bi-chunk-answers] {a.cell} ({a.capture}) against {a.base_cell} ({a.base}); binary "
          f"{b.get('binary_git_sha', '?')[:10]} dirty {b.get('binary_dirty')}; tree {rec.get('tree', {}).get('git_sha', '?')[:10]}")
    bad = []
    notok = [r for r in new if r["ok"] != "1"]
    if notok:
        bad.append(f"{len(notok)} runs not ok, e.g. {notok[0]['query']} {notok[0]['body']} p{notok[0]['param_idx']}")
    # (query, body sha) -> variant name, per side
    nb = {(r["query"], r["body_sha"]): r["body"] for r in new}
    ob = {(r["query"], r["body_sha"]): r["body"] for r in old}
    pair = {k: k for k in nb if k in ob}            # new body -> base body
    renamed = sorted((k[0], ob[k], nb[k]) for k in pair if ob[k] != nb[k])
    changed = []
    for q in sorted({k[0] for k in nb} | {k[0] for k in ob}):
        n_left = [k for k in nb if k[0] == q and k not in pair]
        o_left = [k for k in ob if k[0] == q and k not in set(pair.values())]
        if len(n_left) == 1 and len(o_left) == 1 and nb[n_left[0]] == ob[o_left[0]]:
            pair[n_left[0]] = o_left[0]             # one body changed under the same name
            changed.append((q, nb[n_left[0]], o_left[0][1], n_left[0][1]))
        elif n_left or o_left:
            bad.append(f"{q}: unmatched bodies, only here {[nb[k] for k in n_left]}, only in the base {[ob[k] for k in o_left]}")
    O = {(r["query"], r["body_sha"], r["param_idx"]): r for r in old}
    compared = 0
    for r in new:
        k = pair.get((r["query"], r["body_sha"]))
        if k is None:
            continue
        o = O.get((k[0], k[1], r["param_idx"]))
        if o is None:
            bad.append(f"{r['query']} {r['body']} p{r['param_idx']}: no base run")
            continue
        compared += 1
        for col in ("result_hash", "rows"):
            if r[col] != o[col]:
                bad.append(f"{r['query']} {r['body']} p{r['param_idx']}: {col} {o[col]} -> {r[col]}")
    bodies = len(nb)
    if a.expect_runs is not None and len(new) != a.expect_runs:
        bad.append(f"{len(new)} runs, expected {a.expect_runs}")
    if a.expect_bodies is not None and bodies != a.expect_bodies:
        bad.append(f"{bodies} bodies, expected {a.expect_bodies}")
    if compared == 0:
        bad.append("0 runs compared")
    print(f"  runs {len(new)} ({len(new) - len(notok)} ok), bodies {bodies}; compared {compared} runs on result_hash and rows")
    for q, base_name, name in renamed:
        print(f"  renamed, same body: {q} {base_name} -> {name}")
    for q, name, s_old, s_new in changed:
        print(f"  changed body, same name: {q} {name} {s_old} -> {s_new}")
    for x in bad[:20]:
        print(f"  FAIL {x}")
    print(f"[bi-chunk-answers] {'OK' if not bad else f'FAIL ({len(bad)})'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
