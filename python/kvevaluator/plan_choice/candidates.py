"""The candidate plans of a read query and the rule that picks the one a format is priced at -- shared by the meta
grid (python/kvevaluator/measure/grid_bodies.py, python/kvcost/layout_cost/picks.py) and the named-format kvstats
runs, so no runner imports a case study.

A query's candidates: every body of its plan bundle, then every body of its scan / fused twins (the workload's
ALTERNATIVES), named "<body>", "<twin>:<body>", or "<twin>" for a single-plan twin.

THE PLAN RULE (every "the plan rule" in this repo means this; `decide` applies it):
- Price a format at the cheapest plan its layout allows, scored by the optimizer's PICK criterion summed over the
  params (optimizer/criteria.NAMED; default `wops_bytes4k` = G + 1.1 S + 0.01 N + read bytes / 4096).
- Choose on the held-out params (HELD_OUT_PARAMS: 11-15), report on params 1-5: never chosen and reported on the same
  params.
- The anchor (the recorded plan, else the planner's own pick) is replaced only by a candidate more than 10% cheaper
  on the held-out params (MARGIN); an exact tie keeps the anchor.
- The pick is decided at the largest reported scale and recorded (inputs/picks/picks.csv); every scale uses
  it, unless on that scale's own held-out params it is more than 10% above the cheapest (the scale exception): then
  that scale's own pick, itself recorded.
- A candidate counts only if it ran on every param and returned exactly the planner's rows.
- With no held-out set (BI, the microbench, a single-body query) the pick is in-sample, and the rule says so.
The choice and the currency are two things: every REPORTED ops number stays weighted ops (G:S:N = 1:1.1:0.01).

The plan-change rule: a plan is added or changed only when it changes a reported conclusion, is off by about 4x or
more on a query's own cost, or is needed for correctness. A candidate must be one a cost-based planner could choose
from the plan, the schema and statistics; one that needs more is measured but never picked (measure-only).
"""
import copy
import json
import os
import subprocess
import sys
from collections import defaultdict
from functools import lru_cache

from kvevaluator.plan_choice.optimizer.criteria import DEFAULT_CRITERION, parse_criterion, resolve_criterion
from kvevaluator.weights import DEFAULT
from kvevaluator.output_tree import Capture
from kvevaluator.weights import wops
from kvevaluator.paths import ROOT
from kvevaluator import stale

from kvevaluator.plan_choice.optimizer.criteria import PICK_BYTE_LANES as READ_LANES   # the criterion's byte total: 7 lanes
PLANS_ROOT = os.path.join(str(ROOT), "python", "kvevaluator", "workloads")


def workload_queries(workload):
    """(queries, ALTERNATIVES) of a read workload: its measured query list (kvevaluator.workloads.query_list) and its scan
    twins. A scan twin (ALTERNATIVES key) is not a query of its own: its plan bodies are extra candidates of its base,
    since it answers the same question by another path. A workload whose module declares none has none."""
    from kvevaluator import workloads as W
    if W.get(workload).kind != "read":
        raise ValueError(f"{workload!r} is not a read workload")
    alts = dict(getattr(W.queries_module(workload), "ALTERNATIVES", {}))
    return W.query_list(workload), alts


def extra_queries(workload):
    """the read queries of a workload's registry outside its suite and not a twin (LDBC's application reads such as
    knows or full_profile_page, microbench's closure): plan_choice/generate.py writes their files, nothing measures them"""
    if workload not in ("interactive", "bi", "microbench"):
        return []
    import importlib
    Q = importlib.import_module(f"kvevaluator.workloads.{workload}.queries")
    qs, alts = workload_queries(workload)
    return sorted(set(Q.REGISTRY) - set(qs) - set(alts))


def plans_dir(workload):
    return os.path.join(PLANS_ROOT, workload, "plans")


def plan_files(workload, q):
    """the IR files query q runs: its own bundle, then its twins' (sorted by name) where they still have a file (a
    generated query's file holds its twins' bodies: plan_choice/generate.py)"""
    _, alternatives = workload_queries(workload)
    twins = sorted(t for t, base in alternatives.items() if base == q)
    return [os.path.join(plans_dir(workload), f"{q}.json")] + [
        p for p in (os.path.join(plans_dir(workload), f"{t}.json") for t in twins) if os.path.exists(p)]


def _twin_file_at_v1(workload, stem):
    """a twin's plan file as it was before its bodies moved into its query's file (plan_choice/generate.V1_TREE): what a
    pin or a captured name "<twin>:<body>" still names, until the pins retire. None if it had none."""
    from kvevaluator.plan_choice.generate import V1_TREE
    path = os.path.join(plans_dir(workload), f"{stem}.json")
    require_commit(path, V1_TREE)
    return _plan_file_at(path, V1_TREE)


def _by_sha_in_query(workload, q, sha):
    """the name of the body with this sha in q's own (generated) file, or None"""
    ir, cur = _head_bodies(os.path.join(plans_dir(workload), f"{q}.json"))
    sha = successor_sha(workload, q, sha)                # the sha comes from an older tree
    same = [b for b, h in cur.items() if h == sha]
    return same[0] if len(same) == 1 else None


def forced(ir, body):
    """the one-body IR that runs `body`: the bundle's plan-level fields with `plans` reduced to that body, no
    `variants` (C++ and Python run a one-body IR's body: ir/plan.cc body_to_run, bundle.select_variant)"""
    f = {k: copy.deepcopy(v) for k, v in ir.items() if k not in ("plans", "variants")}
    f["plans"] = {body: copy.deepcopy(ir["plans"][body])}
    return f


def candidate_names(workload, q):
    """(the query's own IR, [(candidate name, the IR it comes from, its body; None for a single-plan twin)]): its
    bundle's bodies, then its twins' (sorted by twin name), one candidate per run: a twin's body that runs the same
    as an earlier candidate (run_key: the same body_sha and plan-level fields) is left out, so the name kept is the
    base's before a twin's. The one place a candidate is named; no copies: the IRs are the parsed files every caller
    shares (_head_bodies: read once per file state, with each body's sha), read, never changed"""
    from kvevaluator.plans.ir.bundle import plan_level
    base_file, *twin_files = plan_files(workload, q)
    ir, shas = _head_bodies(base_file)
    out = [(b, ir, b) for b in ir.get("plans", {})]
    # a single-plan base is no candidate here (it runs as the planner's own row), but its run is taken: a twin of the
    # same body is left out (candidate_bodies names that run by the query)
    key_of = lambda src, sha: json.dumps([sha, plan_level(src)], sort_keys=True)       # run_key, from the cached shas
    seen = {key_of(ir, h) for h in shas.values()}
    for tf in twin_files:
        twin = os.path.splitext(os.path.basename(tf))[0]
        tir, tshas = _head_bodies(tf)
        for name, body in ([(f"{twin}:{b}", b) for b in tir["plans"]] if "plans" in tir else [(twin, None)]):
            key = key_of(tir, tshas[body])
            if key not in seen:
                seen.add(key)
                out.append((name, tir, body))
    return ir, out


def run_key(ir, body):
    """what running one body of an IR file executes, as a key: its body_sha and the file's plan-level fields (bind /
    let / params, in file order). Two candidates with one key give the same run: one is measured, under one name"""
    from kvevaluator.plans.ir.bundle import body_sha, plan_level
    return json.dumps([body_sha(ir, body), plan_level(ir)], sort_keys=True)


def candidates(workload, q):
    """(the query's own IR, [(candidate name, the IR that runs exactly that candidate)]): candidate_names, each forced
    to its body. A single-plan base IR (no bundle) contributes no candidate of its own: only the planner's run."""
    ir, names = candidate_names(workload, q)      # the shared parsed files: every IR handed on is a copy of its own
    return copy.deepcopy(ir), [(name, forced(src, body) if body is not None else copy.deepcopy(src))
                               for name, src, body in names]


def measure_only_bodies(workload, q):
    """the bodies of q's bundle whose variant entry says `"measure_only": true`: measured and reported like any body,
    never picked (choose), never pinned (the search) -- a body whose gain depends on the PARAMS, not on the layout
    (IC12 C: a small tag class), so a pick on five params would mis-price it (the plan-change rule)"""
    ir = json.load(open(plan_files(workload, q)[0]))
    flagged = {v["plan"] for v in ir.get("variants", []) if v.get("measure_only")}   # a bundle body's flag (hand-set; rare)
    flagged |= {b for b, m in (ir.get("bodies") or {}).items() if m.get("measure_only")}   # a generated file's marks
    _, alternatives = workload_queries(workload)
    mod = __import__(f"kvevaluator.workloads.{workload}.queries", fromlist=["TWIN_MEASURE_ONLY"])
    twins = {t for t, base in alternatives.items() if base == q and t in getattr(mod, "TWIN_MEASURE_ONLY", ())}
    return flagged | twins


def capture_tree(out_dir, cell=None):
    """The git commit a capture's plan files were read from, for resolve_body: a dated snapshot's
    snapshot-meta.json `code_state.tree`; else a run record's own `tree.git_sha` (runs/<cell>.json, written by the grid
    driver: the working tree's commit at the capture; any cell when none is named); else its
    `binary.binary_git_sha` -- a PROXY: the driver reads the plans from the working tree, which can be newer than a
    pinned binary (BI at SF3 ran on a binary one plan fix older than its tree, and every bi12 body read as
    never measured). None when the capture records none of them, or only "unknown" (its names are then looked up at
    HEAD only)."""
    meta = os.path.join(out_dir, "snapshot-meta.json")
    if os.path.exists(meta):
        tree = (json.load(open(meta)).get("code_state") or {}).get("tree")
        if tree:
            return tree
    runs = Capture(out_dir).dir("counts")
    recs = [os.path.join(runs, f"{cell}.json")] if cell else sorted(
        os.path.join(runs, f) for f in (os.listdir(runs) if os.path.isdir(runs) else []) if f.endswith(".json"))
    for p in recs:
        if os.path.exists(p):
            rec = json.load(open(p))
            sha = (rec.get("tree") or {}).get("git_sha") or (rec.get("binary") or {}).get("binary_git_sha")
            if sha and sha != "unknown":   # "unknown": made in a copy with no commit (plan_tree): HEAD's names
                return sha
    return None


PLAN_PIPELINE = 2       # the candidate set a capture is judged against: 2 = the generated plan files (plan_choice/generate.py)


def plan_tree():
    """the commit the plan files are read from (the working tree's HEAD, and whether it is dirty), as a run record's
    `tree` carries it for capture_tree: it can be newer than a pinned binary's"""
    from kvevaluator.paths import ROOT, checkout_of, release_commit
    release = release_commit()
    if release is not None:   # a release archive: its files are that commit's, as archived
        return {"git_sha": release, "git_dirty": False, "release": True}
    if checkout_of(ROOT) is None:   # a copy with no .git and no release stamp: no commit to name
        return {"git_sha": "unknown", "git_dirty": "unknown"}
    git = lambda *a: subprocess.run(["git", *a], capture_output=True, text=True, cwd=str(ROOT)).stdout.strip()
    return {"git_sha": git("rev-parse", "HEAD"), "git_dirty": bool(git("status", "--porcelain", "--untracked-files=no"))}


def capture_plan_pipeline(out_dir, cell=None):
    """The plan pipeline a capture was measured under: its snapshot-meta.json's or run record's `plan_pipeline` (written
    since the generated plan files); a capture whose records carry none is 1, v1's hand-written candidate set
    (plan_choice/generate.V1_TREE), and is judged against that set (unmeasured_candidates)"""
    meta = os.path.join(out_dir, "snapshot-meta.json")
    if os.path.exists(meta):
        m = json.load(open(meta))
        got = m.get("plan_pipeline") or (m.get("code_state") or {}).get("plan_pipeline")
        if got:
            return int(got)
    runs = Capture(out_dir).dir("counts")
    recs = [os.path.join(runs, f"{cell}.json")] if cell else sorted(
        os.path.join(runs, f) for f in (os.listdir(runs) if os.path.isdir(runs) else []) if f.endswith(".json"))
    for p in recs:
        if os.path.exists(p):
            return int(json.load(open(p)).get("plan_pipeline") or 1)
    return 1


_proxied = set()        # (workload, q, name, tree) already reported as newer than the capture's commit
_row_wins = {}          # (workload, q, name, tree) -> (the rows' body_sha, the name's sha at the tree): the row won


_head = {}             # (path, mtime_ns, size) -> (the plan file parsed, {body or None: body_sha})


def _head_bodies(path):
    """(the plan file at HEAD, parsed; {body: body_sha}, a single plan's under None), cached on the file's mtime and size:
    a grid check resolves the same names in every cell. The parsed IR is shared: read it, never change it"""
    from kvevaluator.plans.ir.bundle import body_sha
    st = os.stat(path)
    key = (path, st.st_mtime_ns, st.st_size)
    if key not in _head:
        ir = json.load(open(path))
        _head[key] = (ir, {b: body_sha(ir, b) for b in ir["plans"]} if "plans" in ir else {None: body_sha(ir, None)})
    return _head[key]


@lru_cache(maxsize=None)
def _commit_here(path, tree):
    """is `tree` a commit of the repository that holds `path`? An archived capture records a commit of the source
    repository this artifact was cut from, which its history does not hold. With no .git, only a release archive's own
    commit is here (its files are the tree's)"""
    from kvevaluator.paths import ROOT, checkout_of, release_commit
    if checkout_of(ROOT) is None:
        release = release_commit()
        return release is not None and release.startswith(tree)
    root = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=os.path.dirname(path), text=True).strip()
    return subprocess.run(["git", "cat-file", "-e", f"{tree}^{{commit}}"], cwd=root, capture_output=True).returncode == 0


@lru_cache(maxsize=None)
def require_commit(path, tree):
    """refuse when `tree`, a commit this code names (generate.V1_TREE, pick_record.SELECTOR_TREE), is not in the clone
    that holds `path`: a shallow clone has only its last commits, and a missing commit would read as "no plan file
    then", a silent change of answer (an archived capture's commit may be absent: _commit_here, not this)"""
    from kvevaluator.paths import ROOT, checkout_of
    if not _commit_here(path, tree) and checkout_of(ROOT) is None:
        raise SystemExit(f"commit {tree}: this step reads the plan files as they were at that commit, and this tree has "
                         f"no git history (a release archive, or a copy): run it in a git clone")
    if not _commit_here(path, tree):
        raise SystemExit(f"commit {tree} is not in this clone: the plan files are read as they were there. A shallow "
                         f"clone lacks it: fetch the full history (git fetch --unshallow)")


@lru_cache(maxsize=None)
def _plan_file_at(path, tree):
    """the plan file `path` as committed at `tree` (git show), parsed; None when the file is not in that tree (a twin
    added after the capture's binary was built: the proxy case of resolve_body). In a release archive (no .git) the
    release's own commit is read from the tree; another commit is not here, as in a clone that lacks it (None:
    resolve_body takes the name at HEAD and says so)."""
    from kvevaluator.paths import ROOT, checkout_of, release_commit
    from kvevaluator.workloads import paths_in_old_trees
    if checkout_of(ROOT) is None:
        release = release_commit()
        if release is None or not release.startswith(tree):
            return None
        for rel in paths_in_old_trees(os.path.relpath(path, ROOT)):
            if (ROOT / rel).is_file():
                return json.loads((ROOT / rel).read_text())
        return None
    root = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=os.path.dirname(path), text=True).strip()
    for rel in paths_in_old_trees(os.path.relpath(path, root)):
        try:
            return json.loads(subprocess.check_output(["git", "show", f"{tree}:{rel}"], cwd=root, text=True,
                                                      stderr=subprocess.DEVNULL))
        except subprocess.CalledProcessError:
            continue
    return None


@lru_cache(maxsize=None)
def _body_sha_at(path, tree, body):
    """the body_sha of one body (None: the single plan) of the plan file `path` as committed at `tree` (_plan_file_at):
    a check resolves the same captured names in every cell"""
    from kvevaluator.plans.ir.bundle import body_sha
    return body_sha(_plan_file_at(path, tree), body)


def resolve_body(workload, q, name, tree=None):
    """A captured candidate name -> (plan file stem, body name or None) in the CURRENT plan files. A plan edit can
    rename a body while leaving it byte-identical (is2's `index_walk` is now
    `unsorted_range_index_at_0.1`; ic4's body has another name since the type_index split), or reuse a name for another body
    (is2's `default` named the index walk and the two-phase body on 10-02), so a captured name is
    resolved by BODY, never by name alone, whenever the capture's commit is known: the body of that name in the plan
    file at `tree` (capture_tree) is matched by body_sha to a current body; a body that no longer exists anywhere
    raises -- re-measure. With no commit known, or a name absent at the commit (the binary_git_sha proxy: plans
    newer than the binary), the name is taken as it is at HEAD. Names: "" (a single-plan query runs as itself),
    "<twin>" (a single-plan twin), "<twin>:<body>", "<body>"; a single-plan file's own `plan` name also resolves to
    (stem, None)."""
    from kvevaluator.plans.ir.bundle import body_sha
    a = alias_of(workload, q, name)
    if a is not None and name not in candidate_bodies(workload, q):
        # a name older captures use for a body that has another name today: at the capture's commit, by the body it
        # named there; else the alias table's
        at = _plan_file_at(os.path.join(plans_dir(workload), f"{q}.json"), tree) if tree is not None else None
        if at is not None and ("plans" in at and name in at["plans"] or "plans" not in at and name in ("", at.get("plan"))):
            now = _by_sha_in_query(workload, q, body_sha(at, name if "plans" in at else None))
            if now is None:
                raise KeyError(f"{workload} {q}: captured variant {name!r} (at {tree}) is no body of {q}.json -- re-measure")
            return q, now
        name = a["now"]
    base, *twins = plan_files(workload, q)
    stems = {os.path.basename(t)[:-5] for t in twins}
    if name in stems:                                    # a single-plan twin
        return name, None
    _, alts = workload_queries(workload)
    if alts.get(name) == q and not twins and ":" not in name:   # a single-plan twin whose body is in q's file now
        name = f"{name}:"
    stem, body = name.split(":", 1) if ":" in name else (q, name)
    path = os.path.join(plans_dir(workload), f"{stem}.json")
    if stem != q and not os.path.exists(path):
        # a twin whose bodies are in q's file now: the named body (at the capture's commit, else before the move) by sha
        old = (_plan_file_at(path, tree) if tree is not None else None) or _twin_file_at_v1(workload, stem)
        if old is None or ("plans" in old and body not in old["plans"]):
            raise KeyError(f"{workload} {q}: captured variant {name!r}: no such twin body -- re-measure")
        sha = body_sha(old, body if "plans" in old else None)
        now = _by_sha_in_query(workload, q, sha)
        if now is None:
            raise KeyError(f"{workload} {q}: captured variant {name!r} (body {sha}) is no body of {q}.json -- re-measure")
        return q, now
    ir, cur = _head_bodies(path)
    cur_names = {b for b in cur if b is not None}
    if tree is not None:
        old = _plan_file_at(path, tree)
        if old is not None and "plans" in old and body in old["plans"]:
            sha = _body_sha_at(path, tree, body)
        elif old is not None and "plans" not in old and body in ("", old.get("plan")):   # a single plan then
            sha = _body_sha_at(path, tree, None)
        else:
            sha = None                                   # absent at the commit: the proxy case, HEAD's name below
            where = f"{stem}.json is not in" if old is None else f"is not in {stem}.json at"
            if not _commit_here(path, tree):                 # an archived capture: said once per commit, not per name
                if tree not in _proxied:
                    _proxied.add(tree)
                    print(f"[resolve_body] the capture's recorded commit {tree[:9]} is not in this repository (a capture "
                          f"made in the source repository this artifact was cut from): its plan names are resolved at "
                          f"HEAD; a mismatch against the capture then shows up in the check itself", file=sys.stderr)
            elif "generated_by" in ir and body in cur_names:
                pass                 # a name of today's generated file (the rows were renamed to it): not a proxy case
            elif (workload, q, name, tree) not in _proxied:    # said once per name (a check resolves it per cell)
                _proxied.add((workload, q, name, tree))
                print(f"[resolve_body] {workload} {q}: captured variant {name!r}: {where} the capture's recorded commit "
                      f"{tree[:9]} (plans newer than the binary?) -- taking HEAD's body of that name; a mismatch against "
                      f"the capture then shows up in the check itself", file=sys.stderr)
        if sha is not None:
            sha = successor_sha(workload, q, sha)            # the body at the capture's tree, as today names it
            same = [b for b, h in cur.items() if h == sha]
            if body in same or (body == "" and None in same):   # the same name still holds the same body
                return stem, body or None
            if len(same) != 1:       # two current bodies can be byte-identical (a tagged twin of its default)
                raise KeyError(f"{workload} {q}: captured variant {name!r} (body {sha} at {tree}) matches "
                               f"{same or 'no'} current body of {stem}.json -- the body was retired or changed: re-measure")
            return stem, same[0]
    if "plans" not in ir:
        if body in ("", ir.get("plan")):
            return stem, None
        raise KeyError(f"{workload} {q}: captured variant {name!r} names a body, but {stem}.json is a single plan")
    if body in cur_names:
        return stem, body
    raise KeyError(f"{workload} {q}: captured variant {name!r} is not a body of {stem}.json at HEAD"
                   + (f" nor at the capture's commit {tree}" if tree else " and the capture records no commit")
                   + " -- re-measure")


def candidate_builder(workload, q, label, caps=None, caps_of_label=None, by_caps=False, expect_named=True, where=""):
    """A builder (args -> the pre-lowered IR dict {steps, output, params[, let]}) of the committed CANDIDATE body
    `label` of query q -- "<body>" of q's own bundle, "<twin>:<body>", or "<twin>" (a single-body twin). The one
    resolver of every workload's hand pins (dump_planir's hand overrides, emit's searched picks). Two ways to take
    the body:
    - verbatim (`by_caps` False, the microbench's hand pins): the body as the file holds it, lowered under the caps of
      the formats it was emitted for -- a hand pin can name a body the format's own caps would never select;
    - by capabilities (`by_caps` True, a twin pinned for a format under the plan rule): the TWIN BUNDLE's own variant
      for this profile (`caps`, `caps_of_label`: kvevaluator.plans.ir.bundle.select_variant), so every design the format serves gets
      the body the twin's file already lowers for it; with `expect_named` (the profile the pin was measured on) the
      selected body must be the named one, else loud.
    Loud on a missing file or body; with `caps` loud when the body's validity caps (kvevaluator.plans.ir.capabilities.validity_caps
    of its variant's requires) are not all there. `.validity_caps` carries them, `.scales` is empty."""
    from kvevaluator.plans.ir.bundle import NoVariant, select_variant
    from kvevaluator.plans.ir.capabilities import validity_caps
    _, alternatives = workload_queries(workload)
    if ":" in label:
        stem, body = label.split(":", 1)
    elif label in alternatives:                      # "<twin>": a single-body twin named whole
        stem, body = label, None
    else:
        stem, body = q, label
    path = os.path.join(plans_dir(workload), f"{stem}.json")
    if os.path.exists(path):
        with open(path) as f:
            ir = json.load(f)
    elif stem != q and _twin_file_at_v1(workload, stem) is not None:
        ir = _twin_file_at_v1(workload, stem)    # a pin of a twin's body: the twin's file before its bodies moved
    else:
        raise ValueError(f"{where}{q}: candidate {label!r}: no committed bundle {stem}.json")
    if "plans" in ir:
        if body is None or body not in ir["plans"]:
            raise ValueError(f"{where}{q}: candidate {label!r}: {body!r} is not a body of {stem}.json ({sorted(ir['plans'])})")
        if by_caps and caps is not None:
            try:
                chosen = select_variant(ir, caps, caps_of_label)
            except NoVariant as e:
                raise ValueError(f"{where}{q}: candidate {label!r}: no variant of {stem}.json runs on this store ({e})") from e
            if expect_named and chosen != body:
                raise ValueError(f"{where}{q}: candidate {label!r}: this store's capabilities select {chosen!r} of "
                                 f"{stem}.json, not the pinned body")
            body = chosen
        steps, output = ir["plans"][body]["steps"], ir["plans"][body]["output"]
        meta = (ir.get("bodies") or {}).get(body)
        reqs = ([frozenset(meta.get("requires") or ())] if meta is not None else      # a generated file: the body's own
                [frozenset(v.get("requires") or ()) for v in ir.get("variants", []) if v.get("plan") == body]) or [frozenset()]
    else:
        if body is not None:
            raise ValueError(f"{where}{q}: candidate {label!r} names a body, but {stem}.json is a single plan")
        steps, output = ir["steps"], ir["output"]
        reqs = [frozenset(ir.get("requires") or ())]
    from kvevaluator.plans.ir.capabilities import VALIDITY_CAPS
    need = min((validity_caps(r) for r in reqs), key=len)     # the least any variant of this body needs, implied caps in
    if caps is not None and not need <= frozenset(caps):
        raise ValueError(f"{where}{q}: candidate {label!r} needs {sorted(need - frozenset(caps))}, which this store lacks")
    pre = {"steps": steps, "output": output, "params": ir.get("params", {}), **({"let": ir["let"]} if "let" in ir else {})}
    b = lambda args, _pre=pre: _pre  # -- a fresh callable per pin
    # the guard the dumper stamps into this body's selector: by caps, the variant's own validity caps as the twin's
    # file names them (no implied closure: an implied cap in the guard widens the selector and can capture a more
    # capable format); verbatim, nothing (a hand pin that names a body the format's caps would not select claims no cap)
    # (STAMPED_AT_EMIT: the dumper stamps those from the body's ops itself, never from the twin's file)
    from kvevaluator.plans.ir.capabilities import STAMPED_AT_EMIT
    b.validity_caps = (frozenset(min((frozenset(r) & (VALIDITY_CAPS - STAMPED_AT_EMIT) for r in reqs), key=len))
                       if by_caps else frozenset())
    b.scales = []
    return b


def candidate_bodies(workload, q):
    """{candidate name: body sha} of query q's candidates at HEAD (candidates(): the bundle's bodies, then the twins';
    a single-plan base IR is named by its query, as the captures name it)"""
    from kvevaluator.plans.ir.bundle import plan_level
    base_file, *twin_files = plan_files(workload, q)
    ir, shas = _head_bodies(base_file)
    # a single-plan base is named by its query, as every capture names its one body (the AUTO row's variant)
    out = dict(shas) if "plans" in ir else {ir.get("plan", q): shas[None]}
    seen = {json.dumps([h, plan_level(ir)], sort_keys=True) for h in shas.values()}     # run_key, from the cached shas
    for tf in twin_files:
        twin = os.path.splitext(os.path.basename(tf))[0]
        tir, tshas = _head_bodies(tf)
        for name, h in ({f"{twin}:{b}": h for b, h in tshas.items()} if "plans" in tir else {twin: tshas[None]}).items():
            key = json.dumps([h, plan_level(tir)], sort_keys=True)
            if key not in seen:                      # the same rule as candidate_names: one candidate per run
                seen.add(key)
                out[name] = h
    return out


ALIASES_FILE = os.path.join(str(ROOT), "inputs", "picks", "candidate_aliases.json")


@lru_cache(maxsize=None)
def _aliases():
    with open(ALIASES_FILE) as f:
        return {k: v for k, v in json.load(f).items() if k != "_doc"}


@lru_cache(maxsize=None)
def _successors():
    """{(workload, query, old body sha): new body sha} of the count-equal successors (the ledger's body rows,
    inputs/adoptions.json, written only by tests/gates/check_count_equal_successors.py); a sha listed twice as old,
    or a cycle, refuses"""
    from kvevaluator import adoptions
    pairs = adoptions.body_pairs()
    where = adoptions.path()
    out = {}
    for p in pairs:
        k = (p["workload"], p["query"], p["old_body_sha"])
        if k in out:
            raise ValueError(f"{where}: {k} listed twice as an old body")
        out[k] = p["new_body_sha"]
    for (w, q, sha), nxt in out.items():
        seen = {sha}
        while nxt not in seen and (w, q, nxt) in out:
            seen.add(nxt)
            nxt = out[(w, q, nxt)]
        if nxt in seen:
            raise ValueError(f"{where}: {w} {q}: a cycle through {sha}")
    return out


def successor_sha(workload, q, sha):
    """a captured or recorded body sha -> the sha of the body it is count-equal to today (the successor table, its
    chain followed to the end); any other sha unchanged. For shas that came from a capture, a snapshot or a record,
    never HEAD's own. A timed row matched through it is kept with a warning and a mark (latency_fresh): a body's time
    is not count-equal"""
    if not sha:
        return sha
    table = _successors()
    while (workload, q, sha) in table:
        sha = table[(workload, q, sha)]
    return sha


def alias_of(workload, q, name):
    """{body_sha, now} for a candidate name older captures use that no longer names a candidate (inputs/picks/
    candidate_aliases.json), else None"""
    return _aliases().get(workload, {}).get(q, {}).get(name)


def current_name(workload, q, name, sha=None, heads=None):
    """a captured candidate name as named today: the candidate with the row's body_sha (also when the old name still
    exists but names another body: a renaming reused it), or, with no sha, the alias table's; else the name as is"""
    heads = candidate_bodies(workload, q) if heads is None else heads
    sha = successor_sha(workload, q, sha)                # a body rewritten count-equal since the capture: today's sha
    if sha and heads.get(name) not in (None, sha):
        name_ok = False
    else:
        name_ok = name in heads
    if name_ok:
        return name
    by_sha = [n for n, h in heads.items() if h == sha] if sha else []
    if len(by_sha) == 1:
        return by_sha[0]
    a = alias_of(workload, q, name) if not sha else None
    return a["now"] if a else name


def canonical_rows(rows, workload, captured=None):
    """captured run rows with each variant named as a candidate today: a name that is no longer a candidate takes the
    candidate with the row's own body_sha, or (a row without one) the alias table's (alias_of); any other name is kept
    for resolve_body. Two rows that then share (query, param_idx, variant) ran the same body: one is kept (the
    planner's row if either is) after checking every count, byte lane and result is equal -- a difference raises.
    `captured` (a dict): filled with {id(kept row): the row as captured}, so a caller can hand its result back under
    the capture's own names (choose does: readers look its picks up in the capture's raw rows)."""
    cur = {}
    out, at = [], {}
    for r in rows:
        q, name = r["query"], r["body"]
        if q not in cur:
            cur[q] = candidate_bodies(workload, q)
        raw = r
        now = current_name(workload, q, name, r.get("body_sha"), cur[q])
        if now != name:
            r = {**r, "body": now}
        if captured is not None:
            captured[id(r)] = raw
        k = (q, r["param_idx"], r["body"])
        if k not in at:
            at[k] = len(out)
            out.append(r)
            continue
        kept = out[at[k]]
        cols = ("ok", "S", "G", "N", "S_miss", "G_miss", "N_miss", *READ_LANES, "rows", "result_hash")
        diff = [c for c in cols if c in r and c in kept and str(r[c]) != str(kept[c])]
        if diff:
            raise ValueError(f"{workload} {q} p{r['param_idx']}: two captured rows of body {r['body']!r} differ in "
                             f"{diff}: not the same run")
        if str(r.get("planner_pick")) == "1":
            out[at[k]] = r
    return out


def captured_bodies(runs, workload, q, tree=None):
    """{captured variant name: body sha at HEAD} over the run rows of query q (every row, ok or not: the driver records
    the candidates it refused too); a body that no longer exists at HEAD maps to None. A row's own body_sha (the body
    that ran) wins; a name is resolved at the capture's commit (resolve_body) only where its rows carry none. Where
    both exist and differ (a composed capture: rows of a newer tree under an older record), the row's sha is kept, said
    once on stderr and recorded (row_sha_wins())."""
    from kvevaluator.plans.ir.bundle import body_sha
    head = set(candidate_bodies(workload, q).values())
    out = {}
    for name in sorted({r["body"] for r in runs if r["query"] == q}):
        try:
            stem, body = resolve_body(workload, q, name, tree)
            ir, shas = _head_bodies(os.path.join(plans_dir(workload), f"{stem}.json"))
            resolved = shas[body] if body in shas else body_sha(ir, body)
        except KeyError:
            resolved = None
        ran_shas = {r.get("body_sha") or "" for r in runs if r["query"] == q and r["body"] == name} - {""}
        # rows of an older body reused under its count-equal successor, beside rows of the successor: one body
        row = {successor_sha(workload, q, s) for s in ran_shas}
        if len(row) > 1:
            raise ValueError(f"{workload} {q}: the rows of variant {name!r} carry {len(row)} bodies {sorted(ran_shas)}")
        if not row:
            out[name] = resolved
            continue
        sha = row.pop()                                # a body rewritten count-equal since the capture: today's sha
        ran = sha if sha in ran_shas else ran_shas.pop()
        out[name] = sha if sha in head else None
        if resolved not in (ran, sha) and (workload, q, name, tree) not in _row_wins:
            _row_wins[(workload, q, name, tree)] = (sha, resolved)
            print(f"[captured_bodies] {workload} {q}: variant {name!r}: the rows ran body {sha}, the name at the capture's "
                  f"commit {(tree or 'HEAD')[:9]} is {resolved}: the rows' body is taken", file=sys.stderr)
    return out


def row_sha_wins():
    """the (workload, query, variant, tree) whose rows' body_sha differed from the name resolved at the tree, with both"""
    return dict(_row_wins)


def unmeasured_candidates(runs, workload, q, tree=None, pipeline=None):
    """The candidates HEAD has for query q that the capture `runs` never ran -- no row at all, not even a refused one
    (the driver records every candidate it knows, valid or not; so a candidate with no row did not exist when the
    capture was measured: the capture predates the plan file). Names at HEAD; empty = the capture knows every body.
    Direction (b), a captured body HEAD now refuses on the cell, needs the cell's capabilities: grid_capture.candidate_drift.
    `pipeline` (capture_plan_pipeline): a capture of pipeline 1 is judged against v1's candidate set, and the bodies
    generated since are listed once as never measured, not as drift; None or 2: against HEAD's."""
    have = {sha for sha in captured_bodies(runs, workload, q, tree).values() if sha}
    missing = sorted(n for n, sha in candidate_bodies(workload, q).items() if sha not in have)
    if pipeline is None or pipeline >= PLAN_PIPELINE:
        return missing
    known = _v1_candidates(workload, q)
    if known is None:
        return missing
    new = [n for n in missing if candidate_bodies(workload, q)[n] not in known]
    if new and (workload, q, tree) not in _said_pre_generated:
        _said_pre_generated.add((workload, q, tree))
        print(f"[unmeasured_candidates] {workload} {q}: a capture from before the generated plan files: {len(new)} "
              f"candidates generated since were never measured in it (not drift: they did not exist)", file=sys.stderr)
    return [n for n in missing if n not in new]


_said_pre_generated = set()


@lru_cache(maxsize=None)
def _v1_candidates(workload, q):
    """the body shas query q's candidates had in v1's plan files (plan_choice/generate.V1_TREE: its own file and its twins'),
    None for a query with no file then"""
    from kvevaluator.plan_choice.generate import V1_TREE
    from kvevaluator.plans.ir.bundle import body_sha
    _, alts = workload_queries(workload)
    out = set()
    require_commit(plans_dir(workload), V1_TREE)
    for stem in [q] + sorted(t for t, b in alts.items() if b == q):
        ir = _plan_file_at(os.path.join(plans_dir(workload), f"{stem}.json"), V1_TREE)
        if ir is not None:
            out |= {body_sha(ir, b) for b in ir["plans"]} if "plans" in ir else {body_sha(ir, None)}
    return frozenset(out) or None


def invalid_candidates(workload, q, caps, caps_of_label):
    """{candidate name: the validity caps it lacks} for query q on a store with these capabilities (`caps`, the
    store-wide set; `caps_of_label`, kvevaluator.formats.caps.caps_resolver: a cap a variant names `on` a label is
    checked on that label). A body is valid when one variant naming it has every validity cap of its `requires`
    (kvevaluator.plans.ir.capabilities.validity_caps: selector caps do not count, they are what measuring replaces). A valid
    body can still fail when run or return other rows: `choose` rejects those."""
    _, cands = candidate_names(workload, q)                 # names only: candidates()' forced copies cost a deepcopy per body
    out = {}
    for name, src, body in cands:                            # a body of the query's bundle or of a twin's: its bundle's variants
        if body is None:                                     # a single-plan twin: its own top-level `requires`, every
            miss = sorted(c for c in src.get("requires", []) if c not in caps)   # cap (no fallback: C++ refuses it)
            if miss:
                out[name] = miss
            continue
        lacking = body_lacks(src, body, caps, caps_of_label)
        if lacking:
            out[name] = lacking
    return out


def body_lacks(ir, body, caps, caps_of_label):
    """the validity caps a body of bundle `ir` lacks on a store (empty: the body is valid there). A body is valid when
    one variant naming it has every validity cap of its `requires` (selector caps do not count) and none of its `lacks`
    (a body offered only where a cap does NOT hold: JOB's plain scan, workloads/job/queries/__init__.py; such a cap is
    reported as "holds <cap>"); a cap a variant names `on` a label is checked on that label (`caps_of_label`)"""
    from kvevaluator.plans.ir.capabilities import validity_caps
    meta = (ir.get("bodies") or {}).get(body)
    if meta is not None:     # a generated file: the body's own validity caps (plan_choice/generate.py), each `on` its labels
        on = meta.get("on", {})
        return sorted(c for c in meta.get("requires", [])
                      if not (all(c in caps_of_label(l) for l in on[c]) if c in on else c in caps))
    lacking = None
    for v in ir.get("variants", []):
        if v["plan"] != body:
            continue
        on = v.get("on", {})
        miss = sorted(c for c in validity_caps(v.get("requires", []))
                      if not (all(c in caps_of_label(l) for l in on[c]) if c in on else c in caps))
        miss += sorted(f"holds {c}" for c in v.get("lacks", [])      # as select_variant: every named label lacks it
                       if not (all(c not in caps_of_label(l) for l in on[c]) if c in on else c not in caps))
        if not miss:
            return []
        lacking = lacking or miss
    return lacking or []


def valid_bodies(ir, caps, caps_of_label):
    """the bodies of bundle `ir` valid on a store, one name per body_sha, in the bundle's order: what a gate runs
    beside the selected body, so a body no format selects is still checked on every format it is valid on"""
    from kvevaluator.plans.ir.bundle import body_sha
    if any(c not in caps for c in ir.get("requires", [])):     # the bundle's guard caps: no body runs here
        return []
    seen, out = set(), []
    for b in ir.get("plans", {}):
        sha = body_sha(ir, b)
        if sha not in seen and not body_lacks(ir, b, caps, caps_of_label):
            seen.add(sha)
            out.append(b)
    return out


# a run row's error prefix for a body the SF30 rule did not run (measure/grid_bodies.py): unmeasured, not invalid
NOT_RUN_AT_SF30 = "not run at SF30: "
# a run row's error prefix for a body never run on its store: its capabilities do not allow it (measure/grid_bodies.py),
# or a composed layout's target does not (python/kvcost/layout_cost/compose_cells.py)
INVALID_HERE = "invalid on this cell"


def failed_run(row):
    """a counts row whose run failed (a C++ error, a timeout): not ok, and neither a body the store may not run
    (INVALID_HERE) nor one the SF30 rule did not run (NOT_RUN_AT_SF30). Such a row is a result, not a failure: the
    one rule for every reader that refuses a query with a failed run (measure/run_measure.py, grid_bodies)"""
    return str(row.get("ok", "1")) != "1" and not str(row.get("error") or "").startswith((NOT_RUN_AT_SF30, INVALID_HERE))


def weighted(r, w=None):
    """weighted OPS of one run row -- the reported CURRENCY (`w` scaled to G = 1; None: weights.DEFAULT, 1.0 G + 1.1 S
    + 0.01 N), never the pick rule: `choose` picks under its criterion"""
    return wops(float(r["S"]), float(r["G"]), float(r["N"]), w or DEFAULT)


def vector(rows):
    """the cost vector a criterion scores, summed over run rows: S, G, N and total_bytes (READ_LANES)"""
    rows = list(rows)                            # summed four times: a generator would be spent after the first
    return {"S": sum(float(r["S"]) for r in rows), "G": sum(float(r["G"]) for r in rows),
            "N": sum(float(r["N"]) for r in rows),
            "total_bytes": sum(float(r[k] or 0) for r in rows for k in READ_LANES)}


def pick_body(vectors, criterion=None, planner=None, tie_digits=None):
    """THE plan rule: the variant a format -- or a composed mix (python/kvcost/layout_cost/compose.py) -- is priced at, over
    `{variant: {S, G, N, total_bytes}}` (`vector`): the criterion's argmin (a NAMED pick rule or spec; None = the
    optimizer's default), an exact tie keeping the planner's pick, else the FIRST variant in the caller's order --
    the capture's row order for choose (a bundle's bodies, then its twins: a scan twin that ties its base body never
    displaces it; 415 of 14,440 picks over every capture would flip under sorted order), sorted
    names for compose. -> (variant, {variant: the criterion's scalar}). Every pricer takes its plan from here, so the
    grid, the named formats and the layout-choice study cannot drift apart again (compose.py picked by the
    fewest weighted ops and the composer by composed read_ops while the pins and grids used ops + bytes/4096).
    `tie_digits` N (float counts: a predicted capture): scores equal at N significant digits tie, and a tie goes to the
    first name in sort order (the summation order moves the last bits, so an exact compare would order by noise)."""
    name, spec = resolve_criterion(criterion or DEFAULT_CRITERION)
    scores = parse_criterion(spec).scores(vectors)
    if tie_digits:
        return min(sorted(scores), key=lambda v: float(f"{scores[v]:.{tie_digits}g}")), scores
    order = {v: i for i, v in enumerate(vectors)}
    return min(scores, key=lambda v: (scores[v], v != planner, order[v])), scores


_LINEAR = {}


def pick_score(vec, criterion=None):
    """The plan PICK's scalar of one pick vector `{S, G, N, total_bytes}` under `criterion` (None = the default), for a
    pricer that sums per-part scores (the grouping search's per-label tables and its bounds): the criterion's own
    score read as per-field weights (kvevaluator.plan_choice.optimizer.criteria.linear_score), so it is the same rule pick_body applies.
    A criterion that is not a linear, monotone score (the `weighted:` forms) is refused: a per-part bound is no
    bound of the sum there."""
    from kvevaluator.plan_choice.optimizer.criteria import linear_score
    name, _ = resolve_criterion(criterion or DEFAULT_CRITERION)
    if name not in _LINEAR:
        _LINEAR[name] = linear_score(criterion)
        if _LINEAR[name] is None:
            raise ValueError(f"criterion {name!r} is not a linear score: a per-part pricer cannot sum it")
    w = _LINEAR[name]
    return sum(w[f] * vec[f] for f in w)


HELD_OUT_PARAMS = (11, 12, 13, 14, 15)   # the held-out params a pick is chosen on (1-based rows of each query's param
                                         # file): every capture driver and reader of the window takes it from here


def held_out_window():
    """the held-out params as grid_bodies --params takes them ("11,12,13,14,15")"""
    return ",".join(str(i) for i in HELD_OUT_PARAMS)


MARGIN = 0.10      # the plan rule: a recorded plan is replaced only by one more than this much cheaper on the held-out params


def _valid_vectors(rs, historical=False):
    """one row set of a query -> (planner, params, by_v, {variant: vector} of the candidates valid on it, rejected, aside):
    valid = ran ok on every param of the set and returned THE answer: the one answer every ok body gives on that param
    (answer_key, the query's order with ties as sets; grid_bodies refuses a capture whose bodies split). A capture
    with a planner_pick column (the body the store's capabilities selected): its row is the
    reference there, compared by the raw result_hash where the capture has no answer_key, and `planner` names it;
    a capture without the column has no planner ("": no anchor). A measure-only body is set aside"""
    ans = "answer_key" if all(r.get("answer_key") for r in rs if r["ok"] == "1") else "result_hash"
    if any(str(r.get("planner_pick")) == "1" for r in rs):   # a capture with the selector's row (historical)
        pick = {r["param_idx"]: r for r in rs if r["planner_pick"] == "1"}
        planner = next((r["body"] for r in pick.values() if r["body"]), "")
        ref = {i: r.get(ans) for i, r in pick.items() if r["ok"] == "1"}
        params = sorted(pick, key=int)
    else:                                              # the vote: one answer per param among the ok bodies
        planner = ""
        params = sorted({r["param_idx"] for r in rs}, key=int)
        votes = defaultdict(set)
        for r in rs:
            if r["ok"] == "1":
                votes[r["param_idx"]].add(r.get(ans))
        ref = {i: next(iter(v)) for i, v in votes.items() if len(v) == 1}
    by_v = defaultdict(dict)
    for r in rs:
        by_v[r["body"]][r["param_idx"]] = r
    costs, rejected = {}, {}
    for v, per in by_v.items():
        if per and all(str(r.get("error", "")).startswith(NOT_RUN_AT_SF30) for r in per.values()):
            continue                             # the SF30 rule did not run it: unmeasured, neither priced nor rejected
        if len(ref) < len(params):
            rejected[v] = ("the planner's pick failed on some param (no reference answer)" if planner else
                           "no single answer on some param (no ok body, or the bodies split)")
            continue
        bad = [i for i in params if i not in per or per[i]["ok"] != "1"]
        if bad:
            rejected[v] = "did not run: " + (per[bad[0]]["error"] if bad[0] in per else "missing")
            continue
        wrong = [i for i in params if per[i].get(ans) != ref[i]]
        if wrong:
            rejected[v] = f"different rows on {len(wrong)}/{len(params)} params"
            continue
        costs[v] = vector(per[i] for i in params)
    # a measure-only body (the capture's rows say so: grid_bodies writes the bundle's flag) is priced and reported
    # beside the others but never picked. A capture whose rows have no measure_only column (an older capture) is read as
    # 0 and marked when no body of the query is measure-only at HEAD; refused when one is (it could be picked unseen)
    without = sum(1 for r in rs if "measure_only" not in r)
    if without and not historical:        # an older capture read with PlanRule.historical: read as 0
        q, wl = rs[0]["query"], _workload_of(rs[0]["query"])
        if wl is None or measure_only_bodies(wl, q):
            raise KeyError(f"{q}: {without} run rows have no measure_only column (a capture from before the flag), and a "
                           f"body of {q} is measure-only at HEAD: re-measure the cell (a reader of a committed snapshot "
                           f"names --historical)")
        stale.note("measure_only_unknown", f"{q}: {without} run rows have no measure_only column (a capture from before "
                   f"the flag): read as 0, no body of {q} is measure-only at HEAD")
    mo = {v for v, per in by_v.items() if any(str(r.get("measure_only", "0")) == "1" for r in per.values())}
    aside = {v: costs.pop(v) for v in list(costs) if v in mo}
    return planner, params, by_v, costs, rejected, aside


@lru_cache(maxsize=None)
def _workload_of(q):
    """the workload whose plan files hold query q (a query name is unique across the workloads), else None"""
    for w in ("interactive", "bi", "microbench", "linkbench", "job"):
        try:
            if q in workload_queries(w)[0]:
                return w
        except (OSError, KeyError, ImportError):
            continue
    return None


def tie_order(names, q=None):
    """ONE tie order everywhere (held_out_pick, decide, the grouped searches' re-planning): by body sha,
    so an exact tie never depends on what a body is called: an older capture's name counts by the body
    it names (the alias table); a name with no known body (q unknown, or no candidate) after, by name"""
    w = _workload_of(q) if q else None
    heads = candidate_bodies(w, q) if w else {}

    def sha(v):          # today's name, else an older capture's name through the alias table (its body sha)
        return heads.get(v) or ((alias_of(w, q, v) or {}).get("body_sha") if w else None)
    return sorted(names, key=lambda v: (sha(v) is None, sha(v) or "", v))


def held_out_pick(scores_h, planner, margin=MARGIN, q=None):
    """THE plan rule (the module docstring) over `{variant: the criterion's scalar on the HELD-OUT params}`: the recorded
    plan -- the planner's variant, the bundle's own selection on the store's capabilities -- unless a candidate scores
    under (1 - margin) x the planner's there; then the cheapest held-out candidate (an exact tie among those keeps
    the first in the caller's order). A planner not valid on the held-out set: the cheapest. -> (variant, why)"""
    order = tie_order(scores_h, q)
    if not planner:                                  # no anchor (a store new to the record, or a carried entry)
        best = min(order, key=lambda v: (scores_h[v], order.index(v)))
        return best, "the cheapest on the held-out params (no anchor: a first decision)"
    if planner in scores_h:
        cheaper = [v for v in order if scores_h[v] < (1 - margin) * scores_h[planner]]
        if not cheaper:
            return planner, "the planner's plan (no candidate under the margin on the held-out params)"
        best = min(cheaper, key=lambda v: (scores_h[v], order.index(v)))
        return best, f"{scores_h[best] / scores_h[planner]:.3f}x the planner's on the held-out params"
    best = min(order, key=lambda v: (scores_h[v], order.index(v)))
    return best, (f"the planner's plan {planner!r} is not valid on the held-out params: the cheapest, with NO margin -- "
                  f"a finding to read, not a pick to trust")


_UNRECORDED_SAID = set()


def _one_body(workload, q):
    """the query's plan files hold one body (a bundle of one, or a bare plan): nothing to pick"""
    if workload is None:
        return False
    try:
        return len(candidate_names(workload, q)[1]) <= 1    # names only: candidates() copies every body
    except (KeyError, FileNotFoundError, SystemExit):
        return False


def decide(q, scores, scores_h, planner, margin=MARGIN, pinned=None, scale_picks=None, rejected=None, rejected_h=None,
           single_body=False):
    """THE decision for one query, shared by a cell (choose) and a composed option (layout_choice/compose.query_cost), so
    the two can never differ in which rule picked: `scores` = the criterion's scalar per candidate valid on the reported
    params, `scores_h` the same on the held-out params ({} = no held-out set), `planner` the bundle's own selection on
    the store's (or the option's) capabilities. -> (variant, rule, why). With `pinned` (the recorded decision of the
    largest reported scale) the pick is the recorded variant unless the plan rule's scale exception holds on this scale's own
    held-out rows (over 1 + margin x the cheapest): then this scale's own recorded pick (`scale_picks`), loud when
    missing. Loud on a recorded variant not valid here. A query the record has no pick for (a query someone added, a new
    twin) is picked by the rule below at this scale, said once (it raised before); `single_body` (the
    query's plan files hold one body) is picked silently, since there is nothing to record."""
    rejected, rejected_h = rejected or {}, rejected_h or {}
    if pinned is not None and q in pinned and pinned[q] is None:
        pinned = None                             # a carried entry pins nothing: the rule below (no anchor: the cheapest)
    if pinned is not None and q not in pinned:
        # not in the query set's record (a query someone added, a new twin): the rule below picks it at this run's own
        # scale, said once per query; a one-body query has nothing to record, so nothing is said
        if not single_body and q not in _UNRECORDED_SAID:
            _UNRECORDED_SAID.add(q)
            print(f"[picks] {q}: picked at this run's scale, not recorded (the record has no pick for it; "
                  f"kvcost record-picks writes one at the deciding scale)", file=sys.stderr)
        pinned = None
    if pinned is not None and q in pinned:
        best = pinned[q]
        if best not in scores:
            raise KeyError(f"{q}: the recorded plan {best!r} (the pick record) is not a valid candidate on this capture "
                           f"(valid: {sorted(scores)}; rejected: {rejected.get(best, 'not measured')}) -- a finding, not a per-scale pick")
        rule, why = "recorded", "the plan recorded at the largest reported scale"
        if scores_h:                                  # the exception, on this scale's own held-out rows
            if best not in scores_h:
                raise KeyError(f"{q}: the recorded plan {best!r} is not valid on this scale's held-out params "
                               f"({rejected_h.get(best, 'not measured')}) -- a finding, not a per-scale pick")
            cheapest = min(sorted(scores_h), key=lambda v: scores_h[v])
            ratio = scores_h[best] / scores_h[cheapest] if scores_h[cheapest] else 1.0
            if ratio > 1 + margin:
                own = (scale_picks or {}).get(q)
                if own is None or own not in scores_h:
                    raise KeyError(f"{q}: the recorded plan {best!r} is {ratio:.3f}x the cheapest ({cheapest!r}) on this scale's "
                                   f"held-out params (over 1 + {margin:g}): this scale needs its own recorded pick "
                                   f"(grid_pick_record.py --sf <this scale> --small-scale --record)")
                return own, "recorded (this scale's own pick: the exception of CONVENTIONS 19)", \
                    f"the largest scale's plan is {ratio:.3f}x the cheapest here; the recorded per-scale pick {own!r}"
            why += f"; on this scale's held-out params it is {ratio:.3f}x the cheapest (within 1 + {margin:g})"
        return best, rule, why
    if scores_h:
        best, why = held_out_pick(scores_h, planner, margin, q)
        return best, f"held-out, margin {margin:g}", why
    order = tie_order(scores, q)
    best = min(order, key=lambda v: (scores[v], v != planner, order.index(v)))
    return best, "in-sample", "the cheapest on the reported params (no held-out capture)"


def choose(runs, criterion=None, workload=None, tree=None, held_out=None, margin=MARGIN, pinned=None, scale_picks=None,
           anchor=None, pipeline=None, historical=False, left_out=None):
    """query -> {"body", "planner", "rows": {param_idx: run row}, "costs": {variant: the criterion's scalar},
    "ops": {variant: weighted ops, the currency}, "sums": {variant: the raw vector}, "criterion": name,
    "rejected": {variant: why}, "unmeasured": [HEAD candidates the capture never ran], "rule", "why"} over run rows
    (query, param_idx, variant, planner_pick, ok, error, result_hash, S, G, N, the read lanes). `criterion`: a
    NAMED pick rule or spec (None = the optimizer's default). `workload` (+ `tree`, the capture's commit): with it,
    every query is checked for candidates HEAD has that the capture never ran -- a capture that predates a plan file
    could miss a cheaper body without any sign -- and a loud WARN names them (the checks and the drivers FAIL on
    them: grid_capture.candidate_drift).
    THE RULE (the plan rule): with `held_out` (the same cell's run rows on the held-out
    params HELD_OUT_PARAMS, kvcost measure --workload i01) the pick is held_out_pick over the candidates
    valid on BOTH sets, scored on the held-out rows; costs, ops and sums are reported from the REPORTED rows (this
    set) of that pick -- never chosen and reported on the same params. With `pinned` ({query: variant}, the decision
    recorded at the largest reported scale, inputs/picks/picks.csv) the pick IS the recorded variant, unless
    the plan rule's scale exception holds on THIS scale's own held-out rows (the recorded variant over 1 + margin x the cheapest
    valid candidate there): then the scale's own pick, which must itself be recorded (`scale_picks`, {query: variant},
    the record's "scales" entry for this scale; a missing entry raises: run grid_pick_record.py --small-scale --record). A
    recorded variant not valid on this capture raises (a finding, never a silent pick). A query in the reported rows
    with no held-out rows raises when a held-out set is given. Without `held_out` and `pinned`: the in-sample argmin
    (`rule` says which was used; a check prints it; picks.choose_cell allows it only with in_sample=True).
    The anchor the margin is measured from (`planner` in the result): `anchor` ({query: body or ""}, the store's
    record: its incumbent, "" for a carried entry) for the queries it names; else, on an older capture, the planner_pick
    row's body (the selector's); else none: the cheapest (a first decision, and a carried entry: never an anchor). A
    measure-only body is never the anchor: none then. `historical`: rows with no measure_only column are read as 0 (an
    older capture), never for a working capture. `left_out` ({query: {body: why}}, a predicted record's bodies_left_out):
    candidates the capture left out on purpose, not counted as unmeasured."""
    name, _spec = resolve_criterion(criterion or DEFAULT_CRITERION)
    captured = {}
    if workload:                                 # rows captured under a name that is no longer a candidate: today's name
        runs = canonical_rows(runs, workload, captured)
        held_out = canonical_rows(held_out, workload, captured) if held_out is not None else None
    by_q = defaultdict(list)
    for r in runs:
        by_q[r["query"]].append(r)
    by_q_h = defaultdict(list)
    for r in held_out or []:
        by_q_h[r["query"]].append(r)
    out = {}
    for q, rs in by_q.items():
        unmeasured = unmeasured_candidates(rs, workload, q, tree, pipeline) if workload else []
        unmeasured = [n for n in unmeasured if n not in (left_out or {}).get(q, {})]
        if unmeasured:
            print(f"[choose] WARN {workload} {q}: HEAD has candidates this capture never ran: {unmeasured} -- the capture "
                  f"predates the plan file; its pick may miss a cheaper body (re-measure the cell)", file=sys.stderr)
        planner, params, by_v, costs, rejected, aside = _valid_vectors(rs, historical)
        engine = planner
        if anchor is not None and q in anchor:   # the record's say: its incumbent, or "" (a carried entry: no anchor)
            planner = anchor[q] or ""
        if planner in aside:                     # a measure-only body is never the anchor
            planner = ""
        if not costs:
            out[q] = {"body": None, "planner": planner, "engine_pick": engine, "rows": {}, "costs": {}, "ops": {},
                      "sums": {}, "criterion": name, "rejected": rejected, "unmeasured": unmeasured, "measure_only": aside,
                      "rule": "none", "why": "no valid candidate"}
            continue
        sums = costs
        ops = {v: sum(weighted(by_v[v][i]) for i in params) for v in list(sums) + list(aside)}   # the CURRENCY, raw beside the pick
        _best, scores = pick_body(sums, criterion, planner)
        scores_h, rejected_h, aside_h = {}, {}, {}
        if held_out is not None and q not in by_q_h:
            raise KeyError(f"{q}: no held-out rows for this query (the held-out capture is incomplete: re-measure it)")
        if q in by_q_h:
            planner_h, params_h, by_v_h, costs_h, rejected_h, aside_h = _valid_vectors(by_q_h[q], historical)
            if set(params) & set(params_h):
                raise KeyError(f"{q}: the reported params {sorted(params, key=int)} and the held-out params {sorted(params_h, key=int)} "
                               f"overlap: a pick must never be chosen and reported on the same params (the plan rule)")
            both = {v: vec for v, vec in costs_h.items() if v in scores}          # valid on both sets, never measure-only
            if not both:
                raise KeyError(f"{q}: no candidate is valid on both the reported and the held-out params "
                               f"(held-out rejected: {rejected_h})")
            _b, scores_h = pick_body(both, criterion, planner)
        bodies = {r["body"] for r in rs} | set(unmeasured)            # the bundle's candidate set as this capture + HEAD know it
        # why a body is not a candidate, for decide's refusals only: a measure-only body ran and is costed, never picked
        mo_why = "measure-only (measured and reported, never picked)"
        why_not = {**rejected, **{v: mo_why for v in aside}}
        why_not_h = {**rejected_h, **{v: mo_why for v in aside_h}}
        best, rule, why = decide(q, scores, scores_h, planner, margin, pinned, scale_picks, why_not, why_not_h,
                                 single_body=len(bodies) <= 1 or _one_body(workload, q))
        if rule.startswith("held-out"):
            rule = f"held-out {min(params_h, key=int)}-{max(params_h, key=int)}, margin {margin:g}"
        out[q] = {"body": best, "planner": planner, "engine_pick": engine, "rows": by_v[best], "costs": scores, "ops": ops,
                  "sums": sums, "criterion": name, "rejected": rejected, "unmeasured": unmeasured, "measure_only": aside,
                  "rule": rule, "why": why, "held_out_scores": scores_h}
    return _as_captured(out, runs, captured) if captured else out


def _as_captured(out, runs, captured):
    """choose's result with every variant name as the capture's rows spell it: the pick is made under today's names
    (canonical_rows), but its readers look the names up in the capture's raw rows (run, step and parts files), which an
    older capture holds under the name it used then (ic4's fused body as ic4_fused:default). Per (query, today's name)
    the name its kept rows were captured under (the most rows; today's name when it is one of them)."""
    from collections import Counter
    spell = defaultdict(Counter)
    for r in runs:
        spell[(r["query"], r["body"])][captured.get(id(r), r)["body"]] += 1
    for q, d in out.items():
        def raw(v, q=q):
            names = spell.get((q, v))
            if not names or v in names:
                return v
            return max(sorted(names), key=lambda n: names[n])
        d["body"] = raw(d["body"]) if d["body"] is not None else None
        d["planner"], d["engine_pick"] = raw(d["planner"]), raw(d["engine_pick"])
        for k in ("costs", "ops", "sums", "rejected", "held_out_scores"):
            if isinstance(d.get(k), dict):
                d[k] = {raw(v): x for v, x in d[k].items()}
        if isinstance(d.get("measure_only"), dict):
            d["measure_only"] = {raw(v): x for v, x in d["measure_only"].items()}
        elif isinstance(d.get("measure_only"), (set, list)):
            d["measure_only"] = type(d["measure_only"])(raw(v) for v in d["measure_only"])
        d["rows"] = {p: captured.get(id(r), r) for p, r in d["rows"].items()}
    return out


# ------------------------------------------------------------------------------------------------- the result's identity
ORDERING_OPS = frozenset({"top_k", "read_top_k_by_index"})


def body_order(body):
    """the fields a body's output is ordered by: the keys of the ordering op (a top-k) nearest the output, [] for an
    ordering op with no key fields (an index's order), None when no ordering op feeds the output (rows come in the
    order the access path gives, which another body need not share)"""
    from kvevaluator.plan_choice.generate import _refs
    by = {s["name"]: s for s in body["steps"]}
    out = body["output"]
    todo, seen = list({out} if isinstance(out, str) else _refs(out)), set()
    while todo:                              # breadth first: the ordering op nearest the output
        n = todo.pop(0)
        if n in seen or n not in by:
            continue
        seen.add(n)
        if by[n].get("op") in ORDERING_OPS:
            return [k[0] if isinstance(k, list) else k for k in by[n].get("keys") or []]
        todo += sorted(_refs({k: v for k, v in by[n].items() if k != "name"}))
    return None


def query_order(ir):
    """a plan file's output order (body_order): None (a set) unless a body orders, else the key fields a body names (a
    body ordered by an index's top-k names none: []); two bodies naming different keys, or one ordering and one not,
    refuse"""
    bodies = list((ir.get("plans") or {"": ir}).values())
    got = {json.dumps(body_order(b)) for b in bodies if "steps" in b}
    if "null" in got and len(got) > 1:
        raise ValueError(f"{ir.get('plan')}: some bodies order the output and some do not: {sorted(got)}")
    named = got - {"[]"}
    if len(named) > 1:
        raise ValueError(f"{ir.get('plan')}: its bodies order the output by different keys: {sorted(got)}")
    return json.loads(named.pop() if named else got.pop()) if got else None


def result_key(rows, order):
    """the identity of a run's answer, what every legal body must agree on per (query, row): with `order` (query_order)
    the rows in order, each run of rows equal on the order's fields taken as a set (the query leaves ties unordered;
    [] = strictly as returned, and so when a row lacks an order field: renamed after the sort); with None a set"""
    import hashlib
    rows = [json.dumps(r, sort_keys=True) for r in rows or []]
    if order is None:
        rows = sorted(rows)
    elif order and all(f in json.loads(r) for r in rows for f in order):
        runs, last = [], object()
        for r in rows:
            k = [json.loads(r).get(f) for f in order]
            if k != last:
                runs.append([])
                last = k
            runs[-1].append(r)
        rows = [sorted(run) for run in runs]
    return hashlib.sha1(json.dumps(rows).encode()).hexdigest()[:16]
