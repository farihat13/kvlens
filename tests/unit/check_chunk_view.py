#!/usr/bin/env python3
"""The chunk view (compose.chunk_view): what a write's touch of a family's last chunk is charged. Synthetic rows, no
data, no store. Under a second.

    python3 tests/unit/check_chunk_view.py

Cases:
  1. "measured" returns the row as captured;
  2. "half" replaces the touched chunk's bytes by target / 2 on the read (scan_value) and on the write's base
     (write_value keeps the new row's own bytes); keys and S / G / N are unchanged (by hand);
  3. a row with no touch is unchanged under "half";
  4. a row without the last-chunk columns (a capture made before them) is refused under "half" on every cell (a
     Condensed cell, a mix, Per-type CSR: the name does not say whether a component is Chunked), and read as captured
     under "measured";
  5. Composer refuses an unknown view and bill_exact ("sized") under "half"; the default view is "half";
  6. write_vector of the half row: write_bytes and write_rmw_bytes moved by exactly the hand deltas;
  7. every loader that costs a write takes the view with no default (compose.load_writes, load_measured,
     predicted.pick, uniform.uniform_cost), so a caller cannot cost writes under a view it did not choose; a
     predicted write vector under "half" moves by the same deltas, in its raw lanes too;
  8. the CLI: `kvcost search-mix` builds its Composer under the model's view (compose.default_chunk_view): measured
     for bill_lower / bill_upper / bill_exact, half for wops_bytes4k; kvcost takes no --chunk-view (the model decides
     it), and the view adds nothing to an output name (run-layout-choice.sh's bill chain calls search-mix with no
     --chunk-view; it failed before);
  9. kvcost cost-layouts (kvcost.layout_cost.uniform_49.cost_layouts) builds one Composer per chunk view, half with no bill
     lanes and measured with the bill's exact lanes; both views are rows of one place's files (layout_costs).
Exit 1 when a case goes the wrong way."""
import argparse
import contextlib
import inspect
import io
import sys
import tempfile

from kvcost.layout_cost import compose
from kvcost.layout_cost.picks import PlanRule
from kvcost.predict_accuracy import predicted, uniform

BAD = []


def check(name, ok, got=None):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f": got {got!r}"))
    if not ok:
        BAD.append(name)


def refusal(fn):
    """the SystemExit / ValueError message fn raises, else None"""
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            fn()
    except (SystemExit, ValueError) as e:
        return str(e)
    return None


CHUNKED, CONDENSED, PERTYPE = "props_chunked_adj_chunked", "props_condensed_adj_condensed", "pertype_csr"
# an insert that read a 905 B last chunk and wrote it back at 969 B, at a 64 KB target, beside a 1,000 B GET
ROW = {"query": "iu7", "param_idx": "1", "S": "1", "G": "1", "N": "0", "P": "2", "D": "0",
       "S_miss": "0", "G_miss": "0", "N_miss": "0", "get_key": "20", "get_value": "1000", "scan_key": "13",
       "scan_value": "905", "seek_miss_key": "0", "next_miss_key": "0", "get_miss_key": "0",
       "write_key": "33", "write_value": "1069",
       "last_chunk_reads": "1", "last_chunk_read_bytes": "905", "last_chunk_read_target_bytes": "65536",
       "last_chunk_writes": "1", "last_chunk_write_bytes": "969", "last_chunk_write_base_bytes": "905",
       "last_chunk_write_target_bytes": "65536"}


def main():
    print("1. measured: as captured")
    check("the same row", compose.chunk_view(ROW, "measured", CHUNKED) is ROW)

    print("2. half: target / 2 in place of the chunk's bytes")
    h = compose.chunk_view(ROW, "half", CHUNKED)
    check("scan_value = 905 - 905 + 32768", h["scan_value"] == 32768.0, h["scan_value"])
    check("write_value = 1069 - 905 + 32768 (the row's 64 B and the other put's 100 B kept)",
          h["write_value"] == 1069 - 905 + 32768.0, h["write_value"])
    same = [k for k in ROW if k not in ("scan_value", "write_value") and h[k] != ROW[k]]
    check("keys, S / G / N and the other lanes unchanged", not same, same)
    check("the captured row is not changed", ROW["scan_value"] == "905" and ROW["write_value"] == "1069")

    print("3. no touch: unchanged")
    none = {**ROW, **{c: "0" for c in compose.LAST_CHUNK}}
    n = compose.chunk_view(none, "half", CHUNKED)
    check("scan_value and write_value as captured", float(n["scan_value"]) == 905 and float(n["write_value"]) == 1069,
          (n["scan_value"], n["write_value"]))

    print("4. a capture made before the columns")
    old = {k: v for k, v in ROW.items() if not k.startswith("last_chunk_")}
    blank = {**ROW, **{c: "" for c in compose.LAST_CHUNK}}
    for name, r in (("absent", old), ("empty", blank)):
        msg = refusal(lambda: compose.chunk_view(r, "half", CHUNKED))
        check(f"{name}: refused on a Chunked cell, naming --chunk-view measured", msg and "--chunk-view measured" in msg, msg)
        for cell in (CONDENSED, PERTYPE, "my_mix"):
            check(f"{name}: refused on {cell} too", refusal(lambda: compose.chunk_view(r, "half", cell)) is not None)
        check(f"{name}: as captured under measured", compose.chunk_view(r, "measured", CHUNKED) is r)
    check("an unknown view refused", refusal(lambda: compose.chunk_view(ROW, "full", CHUNKED)) is not None)

    print("5. Composer")
    base = dict(criterion=None, plan_rule=PlanRule())
    check("the default view is half", compose.Composer(**base).chunk_view == "half")
    check("an unknown view refused", refusal(lambda: compose.Composer(**base, chunk_view="full")) is not None)
    msg = refusal(lambda: compose.Composer(**base, bill="sized"))
    check("bill_exact under half refused", msg and "measured" in msg, msg)
    check("bill_exact under measured accepted", refusal(lambda: compose.Composer(**base, bill="sized", chunk_view="measured")) is None)
    check("the view is in the repr (the mix cost cache's key)", "chunk_view='measured'" in repr(compose.Composer(**base, chunk_view="measured")))

    print("6. write_vector")
    vm, vh = compose.write_vector(ROW), compose.write_vector(h)
    check("write_bytes moved by 32768 - 905", vh["write_bytes"] - vm["write_bytes"] == 32768 - 905,
          vh["write_bytes"] - vm["write_bytes"])
    check("write_rmw_bytes moved by 32768 - 905", vh["write_rmw_bytes"] - vm["write_rmw_bytes"] == 32768 - 905,
          vh["write_rmw_bytes"] - vm["write_rmw_bytes"])
    check("write_ops and write_rmw_ops unchanged",
          vh["write_ops"] == vm["write_ops"] and vh["write_rmw_ops"] == vm["write_rmw_ops"])

    print("7. the loaders take the view, with no default")
    for fn, arg in ((compose.load_writes, "view"), (compose.load_measured, "view"), (predicted.pick, "view"),
                    (uniform.uniform_cost, "view")):
        p = inspect.signature(fn).parameters.get(arg)
        check(f"{fn.__module__}.{fn.__name__}({arg}) has no default", p is not None and p.default is inspect.Parameter.empty,
              p)
    pm, ph = predicted.vector(ROW, True), predicted.vector(ROW, True, None, "half", CHUNKED)
    check("predicted write vector: measured as write_vector", pm["write_bytes"] == vm["write_bytes"])
    check("predicted write vector: half moves write_rmw_bytes by 32768 - 905",
          ph["write_rmw_bytes"] - pm["write_rmw_bytes"] == 32768 - 905, ph["write_rmw_bytes"] - pm["write_rmw_bytes"])
    check("predicted write vector: the raw scan_value is the view's, as the composer carries it (measured: as captured)",
          ph["scan_value"] == 32768.0 and pm["scan_value"] == 905.0, (ph["scan_value"], pm["scan_value"]))

    print("8. the CLI: search-mix, the view from the model")
    from kvcost.cli import cli, search as SR

    class Built(Exception):
        pass

    def stop(root, option_set, dataset, composer=None, **k):   # the first thing after the Composer: no data is read
        raise Built(composer)

    real_load = compose.load
    compose.load = stop
    try:
        with tempfile.TemporaryDirectory() as tmp:
            def composer_of(model, *extra):
                argv = ["search-mix", "--dataset", "ldbc=0.1", "--from", tmp,
                        "--workload", "i01", "--settings", "Sp,Co,Ch64k", "--cost-model", model, *extra]
                try:
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        cli.main(argv)
                except Built as e:
                    return e.args[0]
                except (ValueError, SystemExit) as e:
                    return f"refused: {e}"
                return "no Composer built"
            for model, view, bill in (("bill_exact", "measured", "sized"), ("bill_lower", "measured", "bounds"),
                                      ("bill_upper", "measured", "bounds"), ("wops_bytes4k", "half", "")):
                c = composer_of(model)
                check(f"{model}: Composer under {view}", isinstance(c, compose.Composer) and (c.chunk_view, c.bill) == (view, bill),
                      c if not isinstance(c, compose.Composer) else (c.chunk_view, c.bill))
            c = composer_of("wops_bytes4k", "--chunk-view", "measured")
            check("--chunk-view: not a kvcost flag (refused by the parser)", isinstance(c, str) and c.startswith("refused"), c)
    finally:
        compose.load = real_load
    names = {m: SR.search_name(argparse.Namespace(), m) for m in ("bill_exact", "wops_bytes4k")}
    check("output names: the view adds nothing", names == {"bill_exact": "bill_exact", "wops_bytes4k": "wops_bytes4k"}, names)

    print("9. kvcost cost-layouts (kvcost.layout_cost.uniform_49.cost_layouts): both chunk views, each its own Composer")
    from kvcost.layout_cost import layout_costs as LC
    from kvcost.layout_cost import uniform_49 as U
    from kvevaluator.output_tree import Tree
    built = []
    real_load = compose.load

    def stop_after(*args, composer=None, **k):   # cost_layouts' load(grid, by_dir, dataset, composer=...): record, then stop
        built.append((composer.chunk_view, composer.bill))
        raise Built(composer)
    compose.load = stop_after
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for v in LC.VIEWS:
                try:
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        U.cost_layouts(Tree(tmp), "ldbc", "0.1", "i01", [], plan_rule=PlanRule(), views=[v])
                except (Built, SystemExit, TypeError):
                    pass
    finally:
        compose.load = real_load
    check("half and measured: one Composer each, the bill's lanes on the measured view only",
          [x for x in built if x[0] in LC.VIEWS] == [("half", ""), ("measured", "sized")], built)
    check("the views are rows, not file names", LC.view_of("bill_exact") == "measured" and LC.view_of("wops") == "half",
          (LC.view_of("bill_exact"), LC.view_of("wops")))

    print(f"check_chunk_view: {'FAIL ' + str(len(BAD)) if BAD else 'ok'} ({len(BAD)} failed)")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
