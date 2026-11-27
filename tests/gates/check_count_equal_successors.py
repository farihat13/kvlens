#!/usr/bin/env python3
"""The count-equal successors (the body rows of the reuse ledger, inputs/adoptions.json: kvevaluator.adoptions; read by
candidates.successor_sha): each pair says a body was rewritten so that its KV reads are the same as before, so a count captured under the old body is
the new body's count. A pair enters only with its proof, and this checker is the only writer of body rows.

The proof of a pair (the old body at a pushed commit, old_tree -> the new body at a pushed commit, new_tree; both
trees recorded, and every row is proved at those two trees, whether or not its new body is still today's):
  1. structure: the KV read steps (capabilities.KV_READ_OPS) of the two bodies are the same, step for step (name, op
     and every argument), and so is every step they read from, transitively ("closed": the reads cannot differ);
     when a KV read step reads from a rewritten step, the rows it gets must be shown equal by a run ("measured");
  2. a "measured" pair carries its run evidence (old vs new on stores, the binary, its commit and clean flag), given
     with --evidence, and the body shas the two sides' runs report (--ran-old / --ran-new, which must be the pair's);
     the sf0.1 old-vs-new comparison is the round's C++ gates on the new body.
  3. the two bodies have the same plan-level fields (bind, let, params), and the old sha is no candidate today;
  4. the chain: a new body that is not today's must be the old side of a later pair, and the chain ends at a candidate.

    python3 tests/gates/check_count_equal_successors.py                 # --check: re-prove every pair (no store)
    python3 tests/gates/check_count_equal_successors.py --add --family bi --query bi17 --old-tree <sha> \\
        --new-tree <sha> --old-body hand [--new-body hand] [--evidence "<text>" --ran-old <sha> --ran-new <sha> \\
        --old-binary <path> --new-binary <path>]
    python3 tests/gates/check_count_equal_successors.py --rewrite-record   # re-sha the pick record and the aliases
"""
import argparse
import json
import os
import subprocess
import sys

from kvevaluator import adoptions
from kvevaluator.paths import ROOT
from kvevaluator.plan_choice import candidates as C
from kvevaluator.plans.ir.bundle import body_sha, expand_fragments, plan_level
from kvevaluator.plans.ir.capabilities import KV_READ_OPS

RECORDS = [os.path.join(ROOT, "inputs", "picks", "picks.csv"),      # the pick record (its table; the three files)
           os.path.join(ROOT, "inputs", "picks", "candidate_aliases.json")]


def _git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()


def _plan_file(workload, q, tree=None):
    rel = os.path.relpath(os.path.join(C.plans_dir(workload), f"{q}.json"), ROOT)
    return json.loads(_git("show", f"{tree}:{rel}")) if tree else json.load(open(os.path.join(ROOT, rel)))


def _steps(ir, body):
    plan = ir if "steps" in ir else ir["plans"][body]
    return expand_fragments(dict(plan), ir.get("fragments", [])).get("steps", [])


def _refs(x, out):
    """the step names `x` (a step or an argument) reads, {"ref": name} at any depth"""
    if isinstance(x, dict):
        if len(x) == 1 and "ref" in x and isinstance(x["ref"], str):
            out.add(x["ref"])
        for v in x.values():
            _refs(v, out)
    elif isinstance(x, list):
        for v in x:
            _refs(v, out)
    return out


def _closure(steps, names):
    """the steps `names` read from, transitively, with the named steps themselves"""
    by = {s["name"]: s for s in steps}
    seen, todo = set(), list(names)
    while todo:
        n = todo.pop()
        if n in seen or n not in by:
            continue
        seen.add(n)
        todo.extend(_refs({k: v for k, v in by[n].items() if k != "name"}, set()))
    return seen


def _old_side(workload, q, old_tree, old_body):
    """(the old IR, its body or None, its sha) at the old tree, or a failure string"""
    try:
        old_ir = _plan_file(workload, q, old_tree)
    except subprocess.CalledProcessError:
        return None, None, None, f"no {q}.json at {old_tree[:10]}"
    if "plans" in old_ir and old_body not in old_ir["plans"]:
        return None, None, None, f"{q}.json at {old_tree[:10]} has no body {old_body!r}"
    ob = old_body if "plans" in old_ir else None
    return old_ir, ob, body_sha(old_ir, ob), None


def prove(workload, q, old_tree, old_body, new_tree, new_body):
    """(old sha, new sha, proof dict, failure or None): the structural half of a pair's proof, each body at its tree"""
    old_ir, ob, o_sha, why = _old_side(workload, q, old_tree, old_body)
    if why:
        return None, None, {}, why
    new_ir, nb, n_sha, why = _old_side(workload, q, new_tree, new_body)
    if why:
        return o_sha, None, {}, why
    old, new = _steps(old_ir, ob), _steps(new_ir, nb)
    o_kv, n_kv = [s for s in old if s["op"] in KV_READ_OPS], [s for s in new if s["op"] in KV_READ_OPS]
    proof = {"kv_read_steps": len(n_kv)}
    if o_sha == n_sha:
        return o_sha, n_sha, proof, "the two bodies are the same body"
    if plan_level(old_ir) != plan_level(new_ir):
        return o_sha, n_sha, proof, "the plan-level fields (bind, let, params) differ"
    if o_kv != n_kv:
        diff = [s["name"] for s in o_kv if s not in n_kv] + [s["name"] for s in n_kv if s not in o_kv]
        return o_sha, n_sha, proof, f"the KV read steps differ: {sorted(set(diff))}"
    o_by, n_by = {s["name"]: s for s in old}, {s["name"]: s for s in new}
    kv = [s["name"] for s in n_kv]
    changed = sorted(n for n in _closure(new, kv) | _closure(old, kv) if o_by.get(n) != n_by.get(n))
    proof["kv_reads_from_rewritten"] = changed
    proof["verdict"] = "measured" if changed else "closed"
    return o_sha, n_sha, proof, None


def _load():
    """the body rows as the pairs this checker proves"""
    return {"pairs": adoptions.body_pairs()}


def _write(t):
    adoptions.set_body_pairs(t["pairs"])


def _pushed(tree):
    return bool(_git("branch", "-r", "--contains", tree))


def _binary(path):
    """{binary_git_sha, binary_dirty} of an evaluator binary that ran one side of the evidence, read from the binary"""
    v = json.loads(subprocess.run([path, "--version"], capture_output=True, text=True, check=True).stdout)
    return {"binary_git_sha": v.get("binary_git_sha"), "binary_dirty": v.get("binary_dirty")}


def add(a):
    if not (a.old_tree and a.new_tree):
        raise SystemExit("[successors] REFUSED: --add needs --old-tree and --new-tree (two pushed commits)")
    t = _load()
    tree, new_tree = _git("rev-parse", a.old_tree), _git("rev-parse", a.new_tree)
    for side, t_ in (("old", tree), ("new", new_tree)):
        if not _pushed(t_):
            raise SystemExit(f"[successors] REFUSED: the {side} tree {t_[:10]} is on no remote branch (a pair's two "
                             f"trees are pushed commits)")
    o_sha, n_sha, proof, bad = prove(a.workload, a.query, tree, a.old_body, new_tree, a.new_body or a.old_body)
    if bad:
        raise SystemExit(f"[successors] REFUSED {a.workload} {a.query} {a.old_body}: {bad}")
    if o_sha in C.candidate_bodies(a.workload, a.query).values():
        raise SystemExit(f"[successors] REFUSED: the old body {o_sha} is still a candidate of {a.query} today")
    if proof["verdict"] == "measured" and not a.evidence:
        raise SystemExit(f"[successors] REFUSED {a.workload} {a.query}: its KV reads read from rewritten steps "
                         f"{proof['kv_reads_from_rewritten']}: give the run's old-vs-new evidence (--evidence)")
    if a.evidence and not (a.old_binary and a.new_binary):
        raise SystemExit("[successors] REFUSED: --evidence needs --old-binary and --new-binary (the binaries that ran it)")
    bins = {side: _binary(b) for side, b in (("old", a.old_binary), ("new", a.new_binary))} if a.evidence else {}
    if any(b["binary_dirty"] is not False for b in bins.values()):
        raise SystemExit(f"[successors] REFUSED: a dirty or unstamped binary ran the evidence: {bins}")
    if a.evidence and (a.ran_old, a.ran_new) != (o_sha, n_sha):
        raise SystemExit(f"[successors] REFUSED {a.workload} {a.query}: the evidence ran {a.ran_old} -> {a.ran_new}, "
                         f"the pair is {o_sha} -> {n_sha} (--ran-old / --ran-new: the body shas the runs report)")
    if n_sha not in C.candidate_bodies(a.workload, a.query).values():
        raise SystemExit(f"[successors] REFUSED: {n_sha} is no candidate of {a.query} today")
    if any((p["workload"], p["query"], p["old_body_sha"]) == (a.workload, a.query, o_sha) for p in t["pairs"]):
        raise SystemExit(f"[successors] REFUSED: {a.workload} {a.query} {o_sha} is listed already")
    if a.evidence:
        proof["evidence"] = a.evidence
        proof["ran"] = {"old_body_sha": a.ran_old, "new_body_sha": a.ran_new,
                        "old_binary": bins["old"], "new_binary": bins["new"]}
    t["pairs"].append({"workload": a.workload, "query": a.query, "old_tree": tree, "old_body": a.old_body,
                       "old_body_sha": o_sha, "new_tree": new_tree, "new_body": a.new_body or a.old_body,
                       "new_body_sha": n_sha, "proof": proof})
    _write(t)
    C._successors.cache_clear()
    C.successor_sha(a.workload, a.query, o_sha)       # the table still loads: no cycle, no old sha twice
    print(f"[successors] added {a.workload} {a.query} {a.old_body} {o_sha} -> {n_sha} ({proof['verdict']}, "
          f"{proof['kv_read_steps']} KV read steps)")
    return 0


def check_pair(p):
    """the failures of one table row: both trees pushed; the old body and the new body at their trees, with the table's
    shas; the proof between them (always: a chain step is proved like any other pair); the old body no candidate today;
    a new body that is not today's must go on through a later pair, and the chain must end at a candidate; a measured
    pair's evidence names its two shas"""
    k = f"{p['workload']} {p['query']} {p['old_body']} {p['old_body_sha']}"
    if not p.get("new_tree"):
        return [f"{k}: no new_tree (a pair is proved at its two recorded trees)"]
    bad = [f"{k}: {side} tree {p[side + '_tree'][:10]} is on no remote branch"
           for side in ("old", "new") if not _pushed(p[side + "_tree"])]
    o_sha, n_sha, proof, why = prove(p["workload"], p["query"], p["old_tree"], p["old_body"], p["new_tree"],
                                     p["new_body"])
    if why:
        return bad + [f"{k}: {why}"]
    if o_sha != p["old_body_sha"]:
        bad.append(f"{k}: the old body at {p['old_tree'][:10]} is {o_sha}, the table says {p['old_body_sha']}")
    if n_sha != p["new_body_sha"]:
        bad.append(f"{k}: the new body at {p['new_tree'][:10]} is {n_sha}, the table says {p['new_body_sha']}")
    if proof["verdict"] != p["proof"].get("verdict"):
        bad.append(f"{k}: proof {proof['verdict']}, the table's {p['proof'].get('verdict')}")
    heads = C.candidate_bodies(p["workload"], p["query"])
    if p["old_body_sha"] in heads.values():
        bad.append(f"{k}: the old body is still a candidate today")
    if p["new_body_sha"] not in heads.values() and \
            C.successor_sha(p["workload"], p["query"], p["new_body_sha"]) == p["new_body_sha"]:
        bad.append(f"{k}: the new body {p['new_body']!r} ({p['new_body_sha']}) is no candidate today and no later pair "
                   f"goes on from it")
    if p["proof"].get("verdict") == "measured":
        ran = p["proof"].get("ran") or {}
        if not p["proof"].get("evidence") or (ran.get("old_body_sha"), ran.get("new_body_sha")) != (
                p["old_body_sha"], p["new_body_sha"]):
            bad.append(f"{k}: a measured pair without evidence of these two bodies (ran {ran})")
    end = C.successor_sha(p["workload"], p["query"], p["old_body_sha"])
    if end not in heads.values():
        bad.append(f"{k}: the chain ends at {end}, no candidate of {p['query']} today")
    return bad


# the controls, each a row the check must refuse: a forged pair (an old sha no file holds, a new body name that is gone,
# a new sha of another body); a chain step with other KV reads (both sides are
# real bodies at the old tree, and the table goes on from its new side to a candidate: only proving the step itself
# refuses it); and a pair whose KV reads differ
OLD_TREE, NEW_TREE = "59454909f16926be028f0ec9ec4e039dfbdc3ebd", "11be2f8b7a973e784c9487ffcea35294e8b4dbd4"
FORGED = {"workload": "bi", "query": "bi3", "old_tree": OLD_TREE, "old_body": "hand+probe", "old_body_sha": "deadbeef0000",
          "new_tree": NEW_TREE, "new_body": "no-such-body", "new_body_sha": "c12411072604",
          "proof": {"verdict": "closed", "kv_read_steps": 0}}
CHAINED = {"workload": "bi", "query": "bi3", "old_tree": OLD_TREE, "old_body": "hand~2bb1f0", "old_body_sha": "2bb1f0ebfdd8",
           "new_tree": OLD_TREE, "new_body": "hand~8cc107", "new_body_sha": "8cc107c87de6",
           "proof": {"verdict": "closed", "kv_read_steps": 0}}


def check():
    t = _load()
    pairs, bad = t["pairs"], []
    try:
        C._successors.cache_clear()
        C._successors()
    except ValueError as e:
        bad.append(str(e))
    for p in pairs:
        bad += check_pair(p)
    controls = 0
    if check_pair(FORGED):
        controls += 1
    else:
        bad.append("the control: a forged pair (an old sha no file holds, a gone new body) was not refused")
    why = check_pair(CHAINED)
    if any("the KV read steps differ" in x for x in why):
        controls += 1
    else:
        bad.append(f"the control: a chain step with other KV reads was not refused ({why})")
    _, _, _, why = prove("bi", "bi3", OLD_TREE, "hand~2bb1f0", NEW_TREE, "hand~c12411")
    if (why or "").startswith("the KV read steps differ"):
        controls += 1
    else:
        bad.append(f"the control pair with other KV reads was not refused ({why})")
    rk, rbad = reuse_keys(pairs)
    bad += rbad
    controls += 1 if not any(x.startswith("the control") for x in rbad) else 0
    for x in bad:
        print(f"  FAIL {x}")
    print(f"[successors] {len(pairs)} pairs checked, {rk} reuse keys matched, and {controls} of 4 controls refused: "
          f"{'OK' if not bad else f'FAIL ({len(bad)})'}")
    return 1 if bad else 0


def _end_body(p):
    """(the plan file's IR, the body name) of the end of a pair's chain today"""
    end = C.successor_sha(p["workload"], p["query"], p["old_body_sha"])
    name = {s: n for n, s in C.candidate_bodies(p["workload"], p["query"]).items()}.get(end)
    if name is None:
        return None, None
    stem, body = C.resolve_body(p["workload"], p["query"], name)
    return _plan_file(p["workload"], stem), body


def reuse_keys(pairs):
    """(matched, failures): an SF30 row of an old body is reused as the run of the end of its chain
    (grid_bodies.reuse_matches): a run key built from each pair's old sha matches, with the old sha named; the control,
    a key from a sha outside the table, does not"""
    from kvevaluator.measure.grid_bodies import reuse_matches, row_run_key
    bad, params, n = [], {"p": 1}, 0
    for p in pairs:
        ir, body = _end_body(p)
        if ir is None:
            continue                                  # check_pair says so
        key = C.run_key(ir, body)
        old_key = row_run_key(json.dumps([p["old_body_sha"], json.loads(key)[1]], sort_keys=True), params)
        got = reuse_matches(p["workload"], p["query"], {"run_key": old_key, "body_sha": p["old_body_sha"]}, key, params)
        if got == (True, p["old_body_sha"]):
            n += 1
        else:
            bad.append(f"{p['workload']} {p['query']} {p['old_body']}: a row of the old body is not reused ({got})")
    if pairs:
        ir, body = _end_body(pairs[0])
        if ir is not None:
            key = C.run_key(ir, body)
            stray = row_run_key(json.dumps(["0" * 12, json.loads(key)[1]], sort_keys=True), params)
            if reuse_matches(pairs[0]["workload"], pairs[0]["query"], {"run_key": stray, "body_sha": "0" * 12}, key, params)[0]:
                bad.append("the control: a row of a body outside the table was reused")
    return n, bad


WORKLOADS = ("interactive", "bi", "microbench", "job", "writes", "linkbench")


def rewrite_record():
    """re-sha the pick record and the alias table through the table, scoped by (workload, query): in an entry of a
    pair's query, an old sha takes the end of its chain and the old body's name (body, cheapest, an alias's now, a head
    candidate's key) today's name of that end. The table is checked first. Written by their own writers
    (pick_record.dump; the alias file at indent 2), so the layout is kept"""
    from kvevaluator.plan_choice import pick_record
    if check():
        raise SystemExit("[successors] the table does not check: nothing rewritten")
    t = _load()
    by = {}
    for p in t["pairs"]:
        end = C.successor_sha(p["workload"], p["query"], p["old_body_sha"])
        now = {s: n for n, s in C.candidate_bodies(p["workload"], p["query"]).items()}[end]
        by.setdefault((p["workload"], p["query"]), {})[p["old_body_sha"]] = (end, p["old_body"], now)
    n = 0

    def names(w, q):
        return {old: now for _e, old, now in by.get((w, q), {}).values()}

    def walk(x, w, q):
        nonlocal n
        if isinstance(x, list):
            for v in x:
                walk(v, w, q)
            return
        if not isinstance(x, dict):
            return
        m = by.get((w, q), {})
        if q is not None and x.get("body_sha") in m:
            end, old, now = m[x["body_sha"]]
            x["body_sha"] = end
            n += 1
            for f in ("body", "cheapest", "now"):
                if x.get(f) == old and old != now:
                    x[f] = now
                    n += 1
        for k in list(x):
            v = x[k]
            if q is not None and isinstance(v, str) and v in m and k in names(w, q):   # {name: sha}: a head candidate
                end, old, now = m[v]
                del x[k]
                x[now] = end
                n += 1
                continue
            nw = k if k in WORKLOADS else w
            nq = k if any(k == pq for (pw, pq) in by if pw == nw) else (None if k in WORKLOADS else q)
            walk(v, nw, nq)

    for path in RECORDS:
        is_record = path.endswith("picks.csv")
        d = pick_record.load_full(path) if is_record else json.load(open(path))
        before = n
        walk(d, "interactive", None)                 # the pick record's top level is LDBC's (interactive)
        if is_record:
            pick_record.dump(d, path)
        else:
            with open(path, "w") as f:
                f.write(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
        print(f"[successors] {os.path.relpath(path, ROOT)}: {n - before} values rewritten")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--add", action="store_true")
    ap.add_argument("--rewrite-record", action="store_true")
    ap.add_argument("--family", dest="workload")
    ap.add_argument("--query")
    ap.add_argument("--old-tree")
    ap.add_argument("--new-tree", help="with --add: a pushed commit that holds the new body")
    ap.add_argument("--old-body")
    ap.add_argument("--new-body")
    ap.add_argument("--evidence")
    ap.add_argument("--ran-old", help="with --evidence: the body sha the old side's runs report")
    ap.add_argument("--ran-new", help="with --evidence: the body sha the new side's runs report")
    ap.add_argument("--old-binary", help="with --evidence: the binary that ran the old side (its stamp is recorded)")
    ap.add_argument("--new-binary", help="with --evidence: the binary that ran the new side")
    a = ap.parse_args()
    if a.add:
        return add(a)
    if a.rewrite_record:
        return rewrite_record()
    return check()


if __name__ == "__main__":
    sys.exit(main())
