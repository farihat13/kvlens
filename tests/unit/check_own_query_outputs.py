#!/usr/bin/env python3
"""Two rules a query someone adds meets in kvcost's step commands, on stubs (no store, no binary):
(1) its question and params_like, declared in its Python (Plan(question=..., params_like=...)), reach its plan file
right after `plan`; a field declared there and in the workload's tables is refused; two builders that disagree are
refused; a builder that declares nothing adds nothing (the control);
(2) search-mix writes beside its result file in the output tree (summary/<dataset>/<scale>/mix-search/<workload id>/:
<option set>.json, <option set>.csv, mixes/), one mix spec per option set, model and byte limit
(mixes/per-type-wops_bytes4k.json, mixes/per-type-wops_bytes4k-0.5x.json) and mixes/best.json; with --from another
tree, its captures are read and every file still goes to the output dir's place, nothing to the tree read.

Run:  python3 tests/unit/check_own_query_outputs.py
"""
import argparse
import os
import sys
import tempfile
import types

results = []


def case(name, ok, detail=""):
    results.append(ok)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail and not ok else ""))


def refused(fn):
    """the refusal's text, or None when fn returned"""
    try:
        fn()
    except (ValueError, SystemExit) as e:
        return str(e)
    return None


def query_fields():
    from kvevaluator import workloads
    from kvevaluator.plan_choice import generate
    from kvevaluator.plans.plan import Plan, Step

    def body(**kw):
        return lambda args: Plan([Step("a", "read_vertex_by_id", vid="1", label="Person")], **kw)
    stub = types.SimpleNamespace(
        LOGICAL={"tq_declared": body(question="Given a person, who?", params_like="ic1"),
                 "tq_plain": body()},
        REGISTRY={"tq_two": {"dgraph": body(params_like="ic1"), "nebula": body(params_like="is7")}})
    real_module, real_table = generate._queries_module, workloads.query_fields
    generate._queries_module = lambda w: stub
    try:
        got = generate.declared_fields("interactive", "tq_declared")
        case("declared: question and params_like read from the Plan",
             got == {"question": "Given a person, who?", "params_like": "ic1"}, got)
        workloads.query_fields = lambda name, q: {}
        ir = workloads.with_query_fields("interactive", "tq_declared", {"plan": "tq_declared", "params": {}}, got)
        case("... written right after `plan`", list(ir) == ["plan", "question", "params_like", "params"], list(ir))
        workloads.query_fields = lambda name, q: {"question": "the table's"}
        why = refused(lambda: workloads.with_query_fields("interactive", "tq_declared", {"plan": "tq_declared"}, got))
        case("declared in both the Python and the tables: refused, naming the field",
             why is not None and "question" in why and "keep one" in why, why)
        workloads.query_fields = lambda name, q: {"question": "the table's", "params_like": "ic1"}
        ir = workloads.with_query_fields("interactive", "tq_plain", {"plan": "tq_plain"},
                                         generate.declared_fields("interactive", "tq_plain"))
        case("control: declared nothing, the tables' fields as before",
             ir == {"plan": "tq_plain", "question": "the table's", "params_like": "ic1"}, ir)
        why = refused(lambda: generate.declared_fields("interactive", "tq_two"))
        case("two builders declaring different params_like: refused", why is not None and "is7" in why, why)
    finally:
        generate._queries_module, workloads.query_fields = real_module, real_table


def search_outputs(tmp):
    from kvcost.cli import search as SR
    run = types.SimpleNamespace(path="w.json", scale="0.1", name="w", dataset="ldbc")
    run.families = lambda: ["interactive"]
    SR.runs.load = lambda a: run
    SR.runs.say_run = lambda verb, r: None
    SR.require_composed = lambda r, tree=None: os.path.join(str(tree.root) if tree else tmp, "captures")
    row = {"run_id": "1", "assignment": {}}
    SR.cmd_best_mix = lambda *x: 0
    SR.load_workload = lambda dataset, sf, spec: {"id": "w"}
    SR.R.ResultFile = lambda path: types.SimpleNamespace(read=lambda: [row])
    SR.X.result_rows = lambda rows: (["run_id"], rows)
    SR.X.row_spec = lambda r: ({"spec": 1}, "props_condensed_adj_chunked")
    ad = types.SimpleNamespace(DATASET="ldbc")
    place = os.path.join(tmp, "output", "summary", "ldbc", "sf0.1", "mix-search", "w")

    def search(limit, frm=None):
        a = argparse.Namespace(from_dir=frm, workload="w.json", sf=None, dataset=None, out=None, size=[],
                               option_set="per-type", byte_limit=limit, search="proven")
        SR.run_defaults(a, "search-mix", "wops_bytes4k")
        a.wrun, a.front, a.model, a.row_run_id, a.trace = run, False, "wops_bytes4k", None, None
        SR.cmd_search_mix(a, [], ad, None)
        return a
    outs = {limit: search(limit).out for limit in (None, "0.5x", "2x")}
    case("search-mix: no dir of its own (the output dir's place)", outs == {None: None, "0.5x": None, "2x": None}, outs)
    specs = sorted(os.listdir(os.path.join(place, "mixes")))
    case("one mix spec per option set, model and byte limit, beside the result file",
         specs == ["best.json", "mixes-of-Sp-Co-Ch64k-wops_bytes4k-0.5x.json", "mixes-of-Sp-Co-Ch64k-wops_bytes4k-2x.json",
                   "mixes-of-Sp-Co-Ch64k-wops_bytes4k.json"], specs)
    case("the rows as <stem>.csv beside the result file", os.path.exists(os.path.join(place, "mixes-of-Sp-Co-Ch64k.csv")))
    before = sorted(os.listdir(place))
    other = os.path.join(tmp, "other")
    os.makedirs(other)
    a = search(None, frm=other)
    case("--from another tree: its captures read", a.captures == os.path.join(other, "captures"), a)
    case("--from another tree: the mix specs and rows in the output dir's place, nothing in the tree read",
         sorted(os.listdir(place)) == before and os.listdir(other) == [], (os.listdir(place), os.listdir(other)))


def main():
    tmp = tempfile.mkdtemp(prefix="own-query-")
    os.environ["KVEVAL_OUTPUT_DIR"] = os.path.join(tmp, "output")
    for name, part in (("query fields", query_fields), ("search outputs", lambda: search_outputs(tmp))):
        try:
            part()
        except Exception as e:  # -- code without the rule (the control) fails here, by part
            case(f"{name}: ran", False, f"{type(e).__name__}: {e}")
    n_ok = sum(results)
    print(f"check_own_query_outputs {'OK' if n_ok == len(results) else 'FAIL'}: {n_ok}/{len(results)} cases")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
