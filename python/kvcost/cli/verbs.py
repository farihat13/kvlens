"""kvcost's run commands, one per stage: generate-plans, measure, compose, check-mix, record-picks. measure and compose
write the output tree's captures/ and composed/ (python/kvevaluator/output_tree.py); check-mix builds a mix (its spec in
summary/<dataset>/<scale>/built-mixes/<mix>/, its store in stores/) and measures it into captures/. Every command takes
a workload (--workload: a file, kvevaluator.capture.workload_file, or an id). cost-layouts and search-mix are in
search.py. The C++ binary does every ingest and run (python/kvevaluator/measure/run_measure.py: the calls).

    kvcost generate-plans --workload my.json                          each query's plan file, printed
    kvcost measure        --workload my.json --dataset D=S Sp-Sp Co-Co Ch64k-Ch64k
                                                                      its query sets on these layouts, checked (reused while fresh)
    kvcost compose        --dataset D=S [Sp-Ch4k ...]                 the layouts not measured, from the measured diagonals
    kvcost check-mix      --workload my.json mix:best                 build the found mix, measure it, measured = composed
    kvcost record-picks   --workload my.json                          the query set's pick record, at its deciding scale
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

from kvevaluator import layouts as L
from kvevaluator import workloads
from kvevaluator.measure import run_measure as M
from kvevaluator.output_tree import Tree, capture_in
from kvevaluator.paths import ROOT, refuse_results
from kvcost.cli import runs
from kvcost.cli import workloads as W
from kvcost.layout_cost import benchmarks

DIAGONALS = ("Sp-Sp", "Co-Co", "Ch64k-Ch64k")


# -- generate-plans --------------------------------------------------------------------------------------------------
def _step_text(st):
    """one step as the full listing prints it: name = op(args)"""
    args = ", ".join(f"{k}={json.dumps(v)}" for k, v in st.items() if k not in ("name", "op"))
    return f"{st.get('name', st.get('use', '?')):>8} = {st.get('op', 'use ' + str(st.get('use')))}({args})"


def _own_step(st, name, bodies):
    """a step only some bodies have: 'name = op', and the args that differ where another body has the same step"""
    same = [o for n, b in bodies.items() if n != name for o in b.get("steps", [])
            if o.get("name") == st.get("name") and o.get("op") == st.get("op")]
    text = f"{st.get('name', '?')} = {st.get('op', 'use')}"
    if not same:
        return text
    diff = [k for k in st if k not in ("name", "op") and any(o.get(k) != st[k] for o in same)]
    short = lambda v: (lambda t: t if len(t) <= 72 else t[:69] + "...")(json.dumps(v, separators=(",", ":")))
    return text + ("(" + ", ".join(f"{k}={short(st[k])}" for k in diff) + ")" if diff else "")


def _choice_points(fam, q):
    """the logical plan's choice points as 'step (op)', or [] for a query with hand bodies"""
    from kvevaluator.plan_choice.optimizer import rules
    logical = getattr(workloads.queries_module(fam), "LOGICAL", {}).get(q)
    if logical is None:
        return []
    try:
        return [f"{p.out} ({p.op})" for p in rules.choice_points(logical({}))]
    except Exception:  # -- a builder that needs bound args: its bodies still list
        return []


def _print_plan(fam, q, steps=False, run=None):
    """one query: its question, params and bodies by name (each with the steps only it has); steps=True: every step"""
    path = workloads.plan_path(fam, q)
    if path is None:
        print(f"  {q}: no plan file")
        return 0
    ir = json.load(open(path))
    rec = workloads.query(fam, q)
    print(f"\n{q} ({fam}): {rec.question or '(no question: declare one in its Python, Plan(..., question=...))'}")
    params = ", ".join(f"{k}: {v.get('type', v) if isinstance(v, dict) else v}" for k, v in ir.get("params", {}).items())
    print(f"  params: {params or 'none'}" + (f" (its rows are {rec.params_like}'s)" if rec.params_like else ""))
    bodies = ir.get("plans") or {ir.get("plan", q): {"steps": ir.get("steps", []), "output": ir.get("output")}}
    if len(bodies) > 1:
        points = _choice_points(fam, q)
        print(f"  choice point{'s' if len(points) != 1 else ''}: {', '.join(points) or 'between hand bodies'}; the plan "
              f"rule picks one body per layout (wops_bytes4k on the held-out params), inside cost-layouts and search-mix")
    texts = {n: [json.dumps(st, sort_keys=True) for st in b.get("steps", [])] for n, b in bodies.items()}
    shared = set.intersection(*(set(t) for t in texts.values())) if texts else set()
    width = max(len(n) for n in bodies)
    print(f"  bodies ({len(bodies)}):")
    for name, body in bodies.items():
        own = [st for st, t in zip(body.get("steps", []), texts[name]) if t not in shared]
        what = "" if len(bodies) == 1 else ("; its own steps: " + ", ".join(_own_step(st, name, bodies) for st in own)
                                            if own else "; no step of its own")
        print(f"    {name:<{width}}  {len(body.get('steps', []))} steps{what}")
        if steps:
            for st in body.get("steps", []):
                print("      " + _step_text(st))
            if body.get("output"):
                print(f"      -> {body['output']}")
    print(f"  file: {os.path.relpath(path, str(ROOT))}")
    return len(bodies)


def _with_fields(ir, fields):
    """the IR with `fields` right after `plan` (replacing the IR's own)"""
    if not fields:
        return ir
    out = {}
    for k, v in ir.items():
        if k in fields:
            continue
        out[k] = v
        if k == "plan":
            out.update(fields)
    return out if "plan" in ir else {**fields, **out}


def cmd_generate_plans(a, argv):
    """write each run query's plan file from its builder (plan_choice/generate.py), where it has one; print every plan"""
    run = runs.load(a)
    runs.say_run("generate-plans", run)
    from kvevaluator.plan_choice import generate
    wrote = 0
    for it in run.items:
        fam, q = it.family, it.query
        gen = fam in workloads.names(kind="read", dataset="ldbc")
        reg = getattr(workloads.queries_module(fam), "REGISTRY", {})
        if gen and q in reg:
            d = os.path.join(str(ROOT), "python", "kvevaluator", "workloads", workloads.get(fam).plan_dirs[0])
            path = os.path.join(d, f"{q}.json")
            ir = generate.plan_file(fam, q)
            if os.path.exists(path):         # a field written into the file by hand, and not declared, stays
                own = json.load(open(path))
                kept = {k: own[k] for k in ("question", "params_like") if k in own and k not in ir}
                ir = _with_fields(ir, kept)
                for k, v in kept.items():
                    print(f"  {q}: {k} {v!r} is in its plan file only: declare it in the query's Python, "
                          f"Plan(..., {k}={v!r}), and generate-plans writes it")
            text = json.dumps(ir, indent=2) + "\n"
            if not os.path.exists(path) or open(path).read() != text:
                with open(path, "w") as f:
                    f.write(text)
                wrote += 1
                print(f"  {q}: wrote {os.path.relpath(path, str(ROOT))}")
        elif workloads.plan_path(fam, q) is None:
            raise SystemExit(f"{q} ({fam}): no builder in its family's queries and no plan file in "
                             f"{workloads.get(fam).plan_dirs[0]}: write its function in python/kvevaluator/workloads/"
                             f"{fam}/queries/<file>.py and add it to that file's LOGICAL and SIM (as myq1 in the "
                             f"layout-cost walkthrough), or write a plan file (copy a query's)")
    n = {it.query: _print_plan(it.family, it.query, steps=a.steps) for it in run.items}
    choice = sum(1 for v in n.values() if v > 1)
    print()
    runs.answer("generate-plans", f"{len(n)} queries, {sum(n.values())} bodies, {choice} with a choice point; {wrote} "
                                  f"plan files written" + ("" if a.steps else " (--steps prints every body's steps)"))
    return 0


def require_plan_files(run, verb):
    """refuse, by name, a run query with no plan file (before any store is opened)"""
    missing = [f"{it.query} ({it.family})" for it in run.items if workloads.plan_path(it.family, it.query) is None]
    if missing:
        raise SystemExit(f"{verb}: no plan file for {', '.join(missing)}: kvcost generate-plans --workload "
                         f"{runs.shown(run.path)} writes it from the query's Python")


# -- measure, compose ------------------------------------------------------------------------------------------------
def query_sets_of(a):
    """(dataset, scale, the families measured) of --dataset and --workload (a workload: an id or a file, the families its
    items come from) or --family (one family by its name, kvevaluator.workloads.names()); one of the two"""
    runs.one_pair(a, f"kvcost {a.sub}")
    if bool(a.family) == bool(a.workload):
        raise SystemExit(f"kvcost {a.sub}: give --workload (a workload: an id or a file) or --family (one of "
                         f"{', '.join(workloads.names())}), one of the two")
    if a.family:
        fams = [a.family]
        ds = a.dataset or workloads.get(a.family).dataset
        sc = a.sf
    elif a.workload.endswith(".json") or not (a.dataset and a.sf):
        run = runs.load(a)
        fams, ds, sc = run.families(), run.dataset, run.scale
    else:                                      # a workload id, resolved as the pricing resolves it
        from kvcost.layout_cost import benchmarks
        ws = benchmarks.select(a.dataset, a.sf, a.workload)
        if len(ws) != 1:
            raise SystemExit(f"--workload {a.workload}: names {len(ws)} workloads; name one")
        fams = [f for f, _ in benchmarks.families_read(a.dataset, ws.values())]
        ds, sc = a.dataset, a.sf
    given = f"--family {a.family}" if a.family else f"--workload {a.workload}"
    if a.dataset and a.dataset != ds:
        raise SystemExit(f"{given} runs on {ds}, not {a.dataset}")
    if sc is None:
        raise SystemExit(f"{given}: give the scale, --dataset {ds}=<scale>")
    return ds, str(sc), fams


def _param_set(a, dataset):
    """the param set measured: --params, else the dataset adapter's default (LinkBench: log, the paper's)"""
    if getattr(a, "param_set", None):
        return a.param_set
    if dataset in W.ADAPTERS:
        W.get(dataset).prepare(a)
    return getattr(a, "param_set", None)


def _binary_version():
    """the binary every call runs: its --version (commit, dirty flag), read from the binary itself, and its path"""
    from kvevaluator.paths import cpp_bin
    try:
        ver = json.loads(subprocess.run([cpp_bin(), "--version"], capture_output=True, text=True).stdout)
    except (OSError, ValueError):
        raise SystemExit(f"no binary at {cpp_bin()} (just build)")
    from kvevaluator.paths import recorded_path
    return {**ver, "path": recorded_path(str(cpp_bin()))}


def in_default_tree():
    """whether the output dir is the default one (no KVEVAL_OUTPUT_DIR): a slice of a query set needs another"""
    from kvevaluator.paths import resolve
    return Tree().root.resolve() == resolve(environ={})["output_dir"]


def slice_tree(a):
    """the tree a --queries slice goes to: the output dir, which must not be the default one (a slice of a query set is
    a capture of its own: KVEVAL_OUTPUT_DIR=<another dir>); None without --queries"""
    if not a.queries:
        return None
    if in_default_tree():
        raise SystemExit("--queries: a slice of a query set goes to a tree of its own: KVEVAL_OUTPUT_DIR=<dir> kvcost "
                         "measure ... --queries ...")
    return str(Tree().root)


def cmd_measure(a, argv):
    """measure whole query sets on the layouts named (their measured basis), with the grid check, into captures/"""
    ds, sc, fams = query_sets_of(a)
    reads, write = M.query_sets(ds, fams)
    formats, passes = M.passes(runs.mix_paths(a, a.layouts))
    pset = _param_set(a, ds)
    from kvcost.cli import workloads as W
    ad = W.of(ds, fams)          # the dataset's own counts per query and timeout, unless given
    nparam = a.nparam if a.nparam is not None else W.nparam_of(ad, pset)
    timeout = a.timeout if a.timeout is not None else W.timeout_of(ad)
    in_sample = benchmarks.pick_on_of(a, not getattr(W.get(ad), "IN_SAMPLE", False)) == "in-sample"
    jobs, threads, why = M.measure_at_once(a.jobs, a.P, ds, sc, passes, formats)
    calls = M.calls(ds, sc, reads, write, formats, passes, root=slice_tree(a), nparam=nparam, P=threads, fresh=a.fresh,
                    reuse_stale=a.reuse_stale, param_set=pset, in_sample=in_sample,
                    jobs=jobs, check_only=a.check_only, timeout=timeout, held_out=not in_sample,
                    queries=a.queries)
    if a.no_check:
        calls = [c for c in calls if not c[0].startswith("check ")]
    where = Tree().place_dir("captures", ds, sc)
    print(f"[kvcost measure] {', '.join(fams)} on {ds} at {sc}: {len(calls)} calls "
          f"({', '.join(list(map(repr, passes)) + formats)}), stores {why}, into {where}", flush=True)
    if a.dry_run:
        for _stage, cmd in calls:
            print(" ".join(["PY"] + cmd[1:] if cmd[0] == sys.executable else cmd))
        return 0
    refuse_results(where)
    ver = _binary_version()
    print(f"[kvcost measure] binary {ver['path']} at {(ver.get('binary_git_sha') or '')[:10]} "
          f"dirty={str(ver.get('binary_dirty')).lower()}", flush=True)
    M.mark_captures(calls, M.binary_notes(ver))
    t0 = time.time()
    logs = str(Tree().logs("measure", *where.parts[-2:]))      # each call's every line: logs/measure/<dataset>/<scale>/
    rc, times = M.run_calls(calls, jobs, log_dir=logs, verbose=a.verbose)
    for line in M.say_reuse(times):
        print(f"[kvcost measure] {line}")
    print("[kvcost measure] wall per stage: " + ", ".join(f"{s} {t:.0f}s" + (f" (rc={r})" if r else "")
                                                          for s, t, r, _tally in times) + f"; total {time.time() - t0:.0f}s")
    if rc != 0:
        runs.answer("measure", f"FAIL: {M.failed_calls(times)} ({', '.join(fams)} on {ds} at {sc})")
        return rc
    failed = M.failed_in(M.measured_counts(calls))
    if failed:          # a query with a failed run is never costed from nothing: named here, refused by the cost steps
        print(M.say_failed_runs(f"[kvcost measure] {', '.join(fams)} on {ds} at {sc}", failed,
                                "fix them (the plan, the timeout: --timeout) and measure again"), flush=True)
        runs.answer("measure", f"FAIL: {len(failed)} (layout, query) pairs with failed runs; captures in {where}")
        return 1
    runs.answer("measure", f"DONE ({', '.join(fams)} on {ds} at {sc}, {len(calls)} calls, {time.time() - t0:.0f}s): "
                           f"captures in {where}")
    return 0


def cmd_build_store(a, argv):
    """build each layout's store on the dataset at the scale, asked for: a layout built before in another form is
    built anyway (stores.meta_build_guard); measure builds a missing store itself"""
    runs.one_pair(a, "kvcost build-store")
    ds, sc = (query_sets_of(a)[:2] if a.workload or a.family else (a.dataset, str(a.sf) if a.sf else None))
    if not (ds and sc):
        raise SystemExit("build-store: give --dataset DATASET=SCALE")
    from kvevaluator.measure import grid_capture
    build = not a.dry_run
    if build:
        os.environ["KVEVAL_BUILD_STORE"] = "1"
    formats, passes = M.passes(runs.mix_paths(a, a.layouts))
    fam = workloads.names(kind="read", dataset=ds)[0]   # the dataset's store axis (its read query sets share the stores)
    n = 0
    for f in formats:
        t = time.time()
        path = grid_capture.format_store(sc, fam, f, build=build)
        print(f"  {f}: {path}" + (f" ({time.time() - t:.1f} s)" if build else ""), flush=True)
        n += 1
    for ps in passes:
        for cell in ps.cells or M.spec_cells(ps.spec_dir):
            t = time.time()
            path = grid_capture.meta_store(sc, fam, os.path.join(ps.spec_dir, f"{cell}.json"), build=build)
            print(f"  {runs.layout_name(cell)}: {path}" + (f" ({time.time() - t:.1f} s)" if build else ""), flush=True)
            n += 1
    if not build:
        runs.answer("build-store", f"{n} stores to build on {ds} at {sc}, none built (--dry-run)")
        return 0
    runs.answer("build-store", f"{n} stores ready on {ds} at {sc} (shared by every capture, outside the tree's captures)")
    return 0


def cmd_compose(a, argv):
    """the uniform layouts named (default: every pair of the measured diagonals) that were not measured themselves,
    composed from the measured diagonals into composed/, --jobs layouts at once; composed again on every call
    (seconds), each file kept
    when byte-identical, added when new, moved aside when it changed (compose_cells install()); each composed layout's
    run record holds its wall_s and the measured files it was made from"""
    runs.one_pair(a, "kvcost compose")
    if not (a.dataset and a.sf):
        raise SystemExit("compose: give --dataset DATASET=SCALE")
    ds, sc = a.dataset, str(a.sf)
    cells = [L.cell_of(x) for x in a.layouts] or None
    t0 = time.time()
    cmd, why = M.compose_call(ds, sc, None, _param_set(a, ds), cells, a.jobs)
    if cmd is None:
        runs.answer("compose", f"not composed: {why}")
        return 1
    if a.dry_run:
        print(" ".join(["PY"] + cmd[1:]))
        return 0
    refuse_results(cmd[cmd.index("--out") + 1])
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    lines = r.stdout.splitlines()
    if r.returncode != 0 or a.verbose:
        print(r.stdout + r.stderr, end="")
    if r.returncode != 0:
        runs.answer("compose", f"FAIL: compose_cells exited {r.returncode}: {' '.join(cmd)}")
        return r.returncode
    if not a.verbose:
        _say_composed(lines)
    verdict = lines[-1] if lines else ""
    out = cmd[cmd.index("--out") + 1]
    failed = M.failed_in(glob.glob(os.path.join(out, "*", "*", "counts", "*.csv")))
    if failed:          # a failed measured run carries into every layout composed from it
        print(M.say_failed_runs(f"[kvcost compose] {out}", failed, "measure the failed layouts again, then compose"),
              flush=True)
        runs.answer("compose", f"FAIL: {len(failed)} (layout, query) pairs with failed runs in {out}")
        return 1
    said = (("installed at", "new"), ("byte-identical", "unchanged"), ("added to", "added"))   # compose_cells' last line
    what = next((w for start, w in said if verdict.startswith(start)), "changed")
    took = f" ({time.time() - t0:.1f} s; each layout's wall_s in its run record)"
    runs.answer("compose", {
        "new": f"{out} written: the layouts not measured, composed from the measured diagonals{took}",
        "unchanged": f"{out} unchanged: composed again, byte-identical, file by file, to the one there, so it was "
                     f"kept{took}",
        "added": f"{out}: composed again, new files added (a query set or layouts composed for the first time), every "
                 f"file there byte-identical and kept{took}",
        "changed": f"{out} changed: composed again, some files differ from the ones there ({verdict}){took}"}[what])
    return 0


def _say_composed(lines):
    """compose_cells' output in a few lines: what was composed, what was measured itself (read from captures/, not
    written), the queries not exact, any problem"""
    for l in lines:
        if l.startswith("composing "):        # how many layouts, how many at once and why
            print(f"  {l}")
    own = [l.split(":", 1)[0] for l in lines if "measured itself" in l]
    composed = [l.split(":", 1)[0] for l in lines if ": composed (" in l and "measured itself" not in l]
    print(f"  {len(composed)} composed from their parts ({', '.join(runs.layout_name(c) for c in composed)})"
          + (f"; {len(own)} measured themselves, read from captures/ ({', '.join(runs.layout_name(c) for c in own)})"
             if own else ""))
    not_exact = [l.strip() for l in lines if l.strip().startswith("exact (")
                 and not re.fullmatch(r"exact \(\S+\): (\d+) of \1 queries yes", l.strip())]
    if not_exact:
        print(f"  not exact on every query (composed/.../exact/<layout>.csv says which, and why): {len(not_exact)} lines"
              f"{'' if len(not_exact) <= 6 else ' (-v prints them all)'}")
        for l in not_exact[:6]:
            print(f"    {l}")
    else:
        print("  exact: every query's composed cost equals what that layout would measure (exact/<layout>.csv: "
              "per query, yes or why not)")
    for l in lines:
        if l.startswith("PROBLEM") or "rows differ" in l or l.startswith("--check compared"):
            print(f"  {l}")


# -- check-mix -------------------------------------------------------------------------------------------------------
def cmd_check_mix(a, argv):
    """build the mix, measure the run's query sets on it (kvcost measure's calls, into captures/), and check that what
    it measured equals what was composed: on the slice the tree's uniform layouts were measured on, when they were
    (measured_slice)"""
    t0 = time.time()
    run = runs.load(a)
    runs.say_run("check-mix", run)
    require_plan_files(run, "check-mix")
    spec = os.path.abspath(runs.mix_paths(a, [a.mix])[0])
    if not os.path.exists(spec):
        raise SystemExit(f"check-mix: no {spec} (kvcost search-mix --workload {runs.shown(run.path)} --cost-model ... writes "
                         f"best.json, mix:best)")
    name, full = runs.built_mix(spec)          # named by its content: the store and captures are shared
    mixdir = runs.built_mix_dir(run.dataset, run.scale, name)
    built = os.path.join(mixdir, name + ".json")
    if a.dry_run:
        print(f"  the mix's spec: {runs.rel(built)}{'' if os.path.exists(built) else ' (to write)'}", flush=True)
    elif not os.path.exists(built) or json.load(open(built)) != full:
        os.makedirs(mixdir, exist_ok=True)
        with open(built, "w") as f:
            json.dump(full, f, indent=2, sort_keys=True)
            f.write("\n")
    reads, write = M.query_sets(run.dataset, run.families())
    from kvcost.cli import workloads as W
    pset = _param_set(a, run.dataset)
    ad = runs.adapter_of(run)
    tree = Tree()
    grid = str(tree.place_dir("captures", run.dataset, run.scale))
    queries = measured_slice(grid, reads, pset)
    calls = M.calls(run.dataset, run.scale, reads, write, [], [M.Pass(os.path.realpath(mixdir), [name])],
                    root=str(tree.root) if queries else None, queries=queries,
                    nparam=a.nparam if a.nparam is not None else W.nparam_of(ad, pset), param_set=pset,
                    timeout=W.timeout_of(ad))
    print(f"  measure: the run's query sets ({', '.join(run.families())})"
          f"{f', the slice its uniform layouts were measured on ({queries}),' if queries else ''} on the built mix, "
          f"{len(calls)} calls", flush=True)
    if a.dry_run:
        for _stage, c in calls:
            print("  " + " ".join(["PY"] + c[1:] if c[0] == sys.executable else c))
        print(f"  check: kvcost.layout_cost.composed_vs_measured.compare, the built mix against the uniform layouts of {grid}")
        runs.answer("check-mix", f"the calls, none run (--dry-run): {len(calls)} measuring calls and the check")
        return 0
    M.mark_captures(calls, M.binary_notes(_binary_version()))
    rc, _times = M.run_calls(calls, log=(print if a.verbose else lambda _l: None))    # one mix: one store, one lane
    if rc != 0:
        runs.answer("check-mix", f"FAIL: measuring the built mix exited {rc} (-v prints each call)")
        return rc
    print(f"  check: the built mix's measured rows against its composed rows, query by query (kvcost.layout_cost."
          f"composed_vs_measured){'' if a.verbose else '; -v prints every line'}", flush=True)
    t_check = time.time()
    verdict, said, rc = _compare_built(a, run, grid, mixdir, name, queries)
    record = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "command": "kvcost " + " ".join(runs.recorded_argv(argv)),
              "mix": a.mix, "spec": runs.recorded_argv([os.path.abspath(spec)])[0], "built_as": name, "workload": run.name,
              "check": verdict,
              "wall_s": {"build_and_measure": round(t_check - t0, 3), "check": round(time.time() - t_check, 3),
                         "all": round(time.time() - t0, 3)}}
    with open(os.path.join(mixdir, "checks.jsonl"), "a") as f:     # one line per check, beside the built spec
        f.write(json.dumps(record, sort_keys=True) + "\n")
    runs.answer("check-mix", f"{verdict}: {os.path.basename(spec)}, built as {name} and measured, {said}, "
                             f"in {time.time() - t0:.0f} s (measured in {grid}; "
                             f"the record: {runs.rel(os.path.join(mixdir, 'checks.jsonl'))})")
    return rc


def measured_slice(grid, reads, param_set=None):
    """the queries the uniform layouts in `grid` (a captures/<dataset>/<scale> dir) were measured on, as a comma list,
    read from their run records' slice (kvcost measure --queries writes it); None when they hold whole query sets.
    Layouts measured on different slices, or on a slice of params, are refused: no one mix capture matches them"""
    seen = {}
    for fam in reads:
        cap = capture_in(grid, fam, param_set)
        for cell in cap.stores():
            if L.is_cell(cell) and os.path.exists(cap.record(cell)):
                sl = json.load(open(cap.record(cell))).get("slice") or {}
                seen[f"{fam} {L.name_of(cell)}"] = (tuple(sorted(sl.get("queries") or ())),
                                                       tuple(sorted(map(str, sl.get("params") or ()))))
    if len(set(seen.values())) > 1:
        raise SystemExit("check-mix: the uniform layouts were measured on different slices: "
                         + "; ".join(f"{k}: {', '.join(q) or 'the whole query set'}" for k, (q, _p) in sorted(seen.items()))
                         + " (measure them on one: kvcost measure ... --queries ...)")
    queries, params = next(iter(seen.values()), ((), ()))
    if params:
        raise SystemExit(f"check-mix: the uniform layouts were measured on a slice of params ({', '.join(params)}); "
                         f"check-mix measures whole params")
    return ",".join(queries) or None


def _compare_built(a, run, grid, mixdir, name, queries):
    """(verdict, what it says, exit code) of the built mix against its composed rows: PASS 0, FAIL 1, REFUSED 2 (the
    inputs cannot be compared: the message says why), CRASH 2 (the check stopped with an error)"""
    from kvcost.layout_cost import composed_vs_measured as CVM
    from kvcost.layout_cost.picks import PlanRule
    w = benchmarks.read_workload(run.path)
    fams = benchmarks.families_read(run.dataset, [w[1]])
    try:
        found = [m for m in CVM.mixes_in(mixdir, grid, fams, run.dataset) if m.name == name]
        drift = CVM.drift_findings(found, fams)
        # both sides under one rule: in-sample on the reported params unless --pick-on names another (the record holds
        # no row for a new mix)
        res = CVM.compare(grid, found, sf=run.scale, dataset=run.dataset, families=fams, workloads=dict([w]),
                          criterion=getattr(a, "criterion", None),
                          rule=PlanRule(in_sample=True if (a.pick_on or "in-sample") == "in-sample" else None),
                          required={w[0]}, plant="count" if getattr(a, "plant_error", False) else None,
                          queries=queries.split(",") if queries else None)
    except (CVM.Refused, SystemExit) as e:
        print(f"  REFUSED: {e}")
        return "REFUSED", "is not compared: the inputs cannot be compared (above)", 2
    except Exception:                          # a crash is not a difference: the check did not compare
        import traceback
        traceback.print_exc()
        return "CRASH", "is not compared: the check stopped with an error (above)", 2
    findings = drift + res.findings
    for line in (res.notes if a.verbose else []) + [res.counts_line(run.scale), res.miss_line()]:
        print(f"  {line}")
    for b in findings[:20]:
        print(f"  DIFF: {b}")
    if not res.ok or drift:
        return "FAIL", f"differs from its composed cost ({len(findings)} findings)", 1
    return "PASS", "equals its composed cost (every pick, every step row, every workload sum; miss-key bytes within the rule)", 0


# -- record-picks ----------------------------------------------------------------------------------------------------
def cmd_record_picks(a, argv):
    """the pick record of the run's query set at its deciding scale (kvcost.layout_cost.grid_pick_record, which writes
    LDBC's inputs/picks/picks.csv): written, repicked (--repick) or checked (--check)"""
    from kvcost.layout_cost import grid_pick_record as G
    run = runs.load(a)
    runs.say_run("record-picks", run)
    from kvevaluator import datasets
    deciding = datasets.get(run.dataset).deciding_scale
    if deciding is None:
        raise SystemExit(f"{run.dataset}: no deciding scale (its queries are picked at each run's own scale)")
    if str(run.scale) != str(deciding):
        raise SystemExit(f"record-picks: the record is decided at {run.dataset}'s deciding scale, {deciding}; this run "
                         f"is at {run.scale} (--dataset {run.dataset}={deciding})")
    if a.keep_by_reference and not a.repick:
        raise SystemExit("record-picks: --keep-by-reference goes with --repick")
    args = ["--sf", str(run.scale)]
    if a.from_dir:
        args += ["--grid", str(Tree(a.from_dir).place_dir("captures", run.dataset, run.scale))]
    for fam_capture in a.family_capture or []:
        args += ["--family", fam_capture]
    if a.criterion:
        args += ["--pick-by", a.criterion]
    args += (["--check"] if a.check else []) + (["--repick"] if a.repick else [])
    args += ["--before", a.before] if a.before else []
    args += ["--file", a.file] if a.file else []
    args += ["--keep-by-reference", a.keep_by_reference] if a.keep_by_reference else []
    print(f"  python -m kvcost.layout_cost.grid_pick_record {' '.join(args)}", flush=True)
    t0 = time.time()
    rc = G.main(args) or 0
    runs.answer("record-picks", f"{'checked' if a.check else 'written'} in {time.time() - t0:.0f} s"
                                f"{'' if rc == 0 else f' (exit {rc})'}")
    return rc


def add_parsers(sub, common):
    def p(name, fn, help_, layouts=False, workload_required=True, workload_help=None, family=False, picks=True):
        x = sub.add_parser(name, help=help_, description=help_)
        common(x, picks=picks)
        x.add_argument("--workload", required=workload_required, type=runs.workload_arg,
                       help=workload_help or "a workload file (items: family, query, frequency, params; optional "
                                             "dataset and scale), or the id or name of one")
        if family:
            x.add_argument("--family", choices=workloads.names(), default=None,
                           help="one family (a query set) by name, in place of --workload")
        if layouts:
            x.add_argument("layouts", nargs="*", type=runs.layout_arg(dirs=True, formats=True),
                           help="layouts: Sp-Co, Co-Ch4k, Ch1k-Ch16k (each Chunked side with its size in KB, any size; "
                                "the grid's 64 KB is Ch64k), a cell id, a named format (dgraph, nebula), a spec file or a spec "
                                "dir, or a search row's mix, mix:<question>/<model>[-<limit>] or mix:best. The paper's "
                                "seven: Sp-Sp Co-Co Ch1k-Ch1k Ch4k-Ch4k Ch16k-Ch16k Ch64k-Ch64k Ch256k-Ch256k")
        x.set_defaults(fn=fn)
        return x
    g = p("generate-plans", cmd_generate_plans, "write each query's plan file from its builder, and print each query: "
                                                "its question, its params, its bodies by name (each with the steps only "
                                                "it has) and its choice points; --steps prints every step", picks=False)
    g.add_argument("--steps", action="store_true", help="print every body's steps, one per line")
    m = p("measure", cmd_measure, "measure whole families on the layouts named: every family the workload reads "
                                  "(its reads on the reported params, and on the held-out params where the family "
                                  "picks its plans there; its writes when it has a write family), or the one --family "
                                  "names, then the grid check, into the output tree's captures/<dataset>/<scale>/; a "
                                  "layout's capture is reused while fresh", layouts=True, workload_required=False,
          workload_help="a workload (an id or a file: the families its items come from); or --family", family=True)
    m.add_argument("--nparam", type=int, default=None, help="the reported params per query (default the dataset's: 5; "
                                                             "LinkBench's whole log, 100,000, on --params log; JOB 1)")
    m.add_argument("--threads", dest="P", type=int, default=None,
                   help="worker threads per measuring call (counts do not depend on it; default: the machine's cores "
                        "over the stores measured at once, at most 8; 8 where no peak is recorded for a store of the "
                        "dataset at the scale)")
    m.add_argument("--fresh", action="store_true", help="re-measure every layout, fresh or not")
    m.add_argument("--reuse-stale", action="store_true", help="keep a capture whose key moved (grid_bodies --reuse-stale)")
    benchmarks.add_pick_on_arg(m, help="the params a plan is picked on: held-out (the default where the dataset has a "
                                       "held-out set: its held-out capture is measured too) or in-sample (the reported "
                                       "params only: no held-out capture, the grid check in-sample; LinkBench's default)")
    m.add_argument("--jobs", type=int, default=None,
                   help="stores measured at once, each in its own call with --threads workers (default, where a "
                        "peak is recorded for a store of the dataset at the scale, its manifest's measure_peak_gb: "
                        "every store named, no more than the machine's cores and than the available memory over "
                        "that peak; where none is recorded, or --threads is given: the cores over the threads; 1: "
                        "one store at a time). The counts do not depend on it; a run record's wall_s is its store's "
                        "time beside the others running. Lower it where memory is short")
    m.add_argument("--check-only", action="store_true", help="the grid check only, on the capture there")
    m.add_argument("--no-check", action="store_true", help="the measuring calls only, no grid check (one store of a "
                                                           "set measured apart: the set's check runs after its last, "
                                                           "--check-only)")
    m.add_argument("--timeout", type=int, default=None, help="seconds per run before it is recorded as not ok "
                                                             "(default the dataset's: JOB 3600, else grid_bodies' 1800)")
    m.add_argument("--queries", default=None, help="comma list: the reads of these queries only, a slice into a tree "
                                                    "of its own (KVEVAL_OUTPUT_DIR=<dir>; no grid check)")
    m.add_argument("--dry-run", action="store_true", help="print every call (PY = this Python), run none")
    m.add_argument("-v", "--verbose", action="store_true",
                   help="print every line of every call (default: one progress line per kind of call about every 30 s, "
                        "`reads job: Ch64k-Ch64k 47/113 · Co-Co done`, each call's other lines, and a failed call's "
                        "last lines; every line is in the output tree's logs/measure/<dataset>/<scale>/<call>.log)")
    bs = p("build-store", cmd_build_store, "build each layout's store on the dataset at the scale, asked for (a layout "
                                           "built before in another form is built anyway; measure builds a missing "
                                           "store itself, and refuses that case at scale 1 or more)", layouts=True,
           workload_required=False, family=True)
    bs.add_argument("--dry-run", action="store_true", help="print each store's place, build none")
    cp = sub.add_parser("compose", help="compose the uniform layouts not measured from the measured diagonals",
                        description="Compose the uniform layouts named (default: every pair of the settings whose "
                                    "diagonal was measured: Sp-Sp, Co-Co and Chunked/Chunked at each chunk size) that "
                                    "were not measured themselves, every query set of the dataset at the scale, into "
                                    "the output tree's composed/<dataset>/<scale>/. A layout P-A takes its props and "
                                    "index parts from P-P and its adjacency parts from A-A; a side whose diagonal is "
                                    "not measured is refused, with the measure to run. A layout measured itself is kept "
                                    "as measured (checked equal to its composed price first). Composed again on every "
                                    "call (seconds), file by file: one byte-identical to the one there is kept, a new "
                                    "one added, a changed one moved aside. Each composed layout's run record holds its wall_s and the measured "
                                    "files it was made from (one measured again since marks it stale). exact/<layout>.csv "
                                    "says per query whether its composed cost is exact and, if not, why. composed/ sits "
                                    "beside the captures it is made from: it reads and writes the output dir")
    common(cp)
    cp.add_argument("layouts", nargs="*", type=L.arg_type(files=False),
                    help="uniform layouts at any chunk size: Sp-Ch4k, Ch1k-Ch16k (the grid's 64 KB is Ch64k) or a cell id; "
                         "default every pair of the measured diagonals")
    cp.add_argument("--jobs", type=int, default=None,
                    help="layouts composed at once, a process each (default: the machine's cores, and no more than "
                         "the available memory allows; 1: one after another). The files do not depend on it")
    cp.add_argument("--dry-run", action="store_true", help="print the call, run none")
    cp.add_argument("-v", "--verbose", action="store_true", help="print the composer's every line (each layout, each "
                                                                 "param set's exact count)")
    cp.set_defaults(fn=cmd_compose)
    c = p("check-mix", cmd_check_mix, "build a mix (the search's best, or a spec file), measure the workload on it, and "
                                      "check measured = composed, query by query, Chunked at any chunk size (each size's "
                                      "Chunked/Chunked measured in the tree) (exit 0 PASS, 1 FAIL, 2 not compared: REFUSED, "
                                      "the inputs cannot be compared, the message says why, or CRASH)")
    c.add_argument("mix", type=runs.layout_arg(),
                   help="mix:best (the last search-mix's mix: summary/<dataset>/<scale>/mix-search/<workload id>/"
                        "mixes/best.json), mix:<question>/<model>[-<limit>] (a question's row), or a spec file; built as "
                        "summary/<dataset>/<scale>/built-mixes/mix_<sha>/mix_<sha>.json, its store in stores/, its counts "
                        "in captures/; each check appends a record with its wall_s to checks.jsonl beside it")
    c.add_argument("--nparam", type=int, default=None, help="the reported params per query (default the dataset's, as "
                                                             "measure)")
    c.add_argument("-v", "--verbose", action="store_true", help="print the check's every line")
    c.add_argument("--plant-error", action="store_true", help=argparse.SUPPRESS)   # the check's control: must exit 1
    c.add_argument("--dry-run", action="store_true", help="print the measuring calls and the check (PY = this Python), "
                                                          "run none, write nothing")
    benchmarks.add_pick_on_arg(c, help="the params both sides' plans are picked on: in-sample (the default, the reported "
                                       "params: the pick record holds no row for a new mix) or held-out (under the pick "
                                       "record: a mix the record holds)")
    r = p("record-picks", cmd_record_picks, "write the query set's pick record at its deciding scale (offline: the "
                                            "body a built store runs); picking itself stays inside cost-layouts and "
                                            "search-mix")
    r.add_argument("--from", dest="from_dir", default=None, metavar="TREE", help="the tree whose captures are read "
                   "(default the output dir): every store of its Interactive capture is decided")
    r.add_argument("--family-capture", action="append", default=None, metavar="WL=CAPTURE",
                   help="an in-sample query set's capture (job, bi, microbench): every store in it decided in-sample "
                        "into the record's section for it; repeat")
    r.add_argument("--repick", action="store_true", help="a re-measure at the deciding scale: the tracked record's plans "
                   "are the incumbents (replaced only by one more than 10%% cheaper on the held-out params)")
    r.add_argument("--keep-by-reference", default=None, metavar="FILE", help="--repick: a JSON {cell: the capture it was "
                   "decided on} of the tracked record's cells this run does not capture, kept as they were")
    r.add_argument("--check", action="store_true", help="check the record equals what the captures give now; write nothing")
    r.add_argument("--before", default=None, help="--check: the previous record (a file, or <git ref>:inputs/picks/"
                   "picks.csv)")
    r.add_argument("--file", default=None, help="the record written or checked (default the tracked inputs/picks/picks.csv)")
