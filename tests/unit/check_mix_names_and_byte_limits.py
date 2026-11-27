#!/usr/bin/env python3
"""Two search-mix inputs, on planted files (no store, no search):

1. a mix name, mix:<question>/<model> (layout_search.results.mix_spec), finds its question's one row: in the rows file
   named by the question's settings and sizes (mixes-of-Sp-Co-Ch1k-...-Ch256k.json for an any-size question), of the
   question's own search (a two-step row another run left beside the proven one is not it; the question's local search
   call is left out: its file is its own), and its
   spec is written to the output dir's mixes/<stem>-<model>.json. A name no row answers is refused by name.
2. --byte-limit as a list (layout_search.questions.parse_limits): multiples 1x,2x (or 1,2) of the reference's bytes,
   each named B=<b>, and sizes (68MB) in bytes, in one list.

Run:  python3 tests/unit/check_mix_names_and_byte_limits.py
"""
import argparse
import json
import os
import sys
import tempfile

results = []


def case(name, ok, detail=""):
    results.append(ok)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail and not ok else ""))


def refused(fn):
    try:
        fn()
    except SystemExit as e:
        return str(e)
    return None


def mix_names(tmp):
    from kvcost.layout_search import export as X
    from kvcost.layout_search import mix_questions as MQ
    from kvcost.layout_search import results as R
    from kvevaluator.output_tree import Tree
    any_size = "Sp,Co,Ch1k,Ch4k,Ch16k,Ch64k,Ch256k"
    q = {"dataset": "ldbc", "rows": "q-planted", "calls": [
        {"flags": ["--settings", any_size, "--cost-model", "wops_bytes4k"]},
        {"flags": ["--settings", any_size, "--cost-model", "wops_bytes4k", "--search", "local"]}]}
    MQ.get = lambda name: q if name == "q-planted" else (_ for _ in ()).throw(SystemExit(f"no question {name}"))
    X.row_spec = lambda row: ({"planted": row["tag"]}, None)        # the spec of a row: which row was taken
    path = R.home(str(Tree().summary("ldbc", "0.1")), "q-planted", "per-type-any-size",
                  sizes=["1k", "4k", "16k", "256k"])
    os.makedirs(os.path.dirname(path))
    row = lambda tag, search, nodes: {"question": "fewest", "model": "wops_bytes4k", "limit": None, "search": search,
                                      "max_nodes": nodes, "assignment": {"Person props": "Co"}, "tag": tag}
    json.dump({"rows": [row("the proven row", "proven", None), row("the two-step row", "two-step", 1)]}, open(path, "w"))
    a = argparse.Namespace(sf="0.1", run=None, from_dir=None, workload=None)
    try:
        out = R.mix_spec("mix:q-planted/wops_bytes4k", a)
    except SystemExit as e:
        out = f"refused: {e}"
    got = json.load(open(out)) if os.path.isfile(str(out)) else None
    case("mix:<question>/<model>: the question's own (proven) row, from the rows file its settings and sizes name",
         got == {"planted": "the proven row"} and os.path.basename(out) == "mixes-of-Sp-Co-Ch1k-Ch4k-Ch16k-Ch64k-Ch256k-"
         "wops_bytes4k.json", (out, got))
    why = refused(lambda: R.mix_spec("mix:q-planted/bill_exact", a))
    case("a model the question does not search: refused, the question's calls named", why is not None
         and "0 rows of question q-planted" in why, why)


def byte_limits():
    from kvcost.layout_search import questions as Q
    ref = 100.0
    case("--byte-limit 1x,2x: multiples of the reference's bytes, named B=1, B=2",
         Q.parse_limits("1x,2x", None, None, ref) == {100.0: "B=1", 200.0: "B=2"}, Q.parse_limits("1x,2x", None, None, ref)
         if not refused(lambda: Q.parse_limits("1x,2x", None, None, ref)) else "refused")
    case("--byte-limit 1,2: the same", Q.parse_limits("1,2", None, None, ref) == {100.0: "B=1", 200.0: "B=2"})
    case("--byte-limit 2x,68MB: a multiple and a size in one list",
         Q.parse_limits("2x,68MB", None, None, ref) == {200.0: "B=2", 68e6: "bytes"})
    case("--byte-limit 2y: refused by name", "--byte-limit 2y" in (refused(lambda: Q.parse_limits("1x,2y", None, None, ref))
                                                                   or ""))


def main():
    tmp = tempfile.mkdtemp(prefix="mix-names-")
    os.environ["KVEVAL_OUTPUT_DIR"] = os.path.join(tmp, "output")
    for part in (mix_names, byte_limits):
        try:
            part(tmp) if part is mix_names else part()
        except Exception as e:      # a crash is a failed case, named
            case(f"{part.__name__}: no crash", False, f"{type(e).__name__}: {e}")
    n_ok = sum(results)
    print(f"check_mix_names_and_byte_limits {'OK' if n_ok == len(results) else 'FAIL'}: {n_ok}/{len(results)} cases")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
