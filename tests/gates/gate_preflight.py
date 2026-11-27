#!/usr/bin/env python3
"""What a gate recipe needs before its first part runs: the C++ binary and the stores, named. A gate whose part cannot
run is INCOMPLETE, never green: this prints every missing piece with the command that makes it and exits 1. A store
whose key layout is not the one the binary reads is such a piece (stale), named with its rebuild line.

One place answers "which binary" (binary_path: KVEVAL_CPP_BIN, else build/cpp/kvevaluator),
"which stores" (GATE_STORES below) and "which tallies" (GATE_TALLIES: the layouts a gate predicts from an output tree
of its own; the gate reads the same list) for `just write-gate`, `just cpp-diff` and `just gate-prep` (`--gate all`). The LDBC CSVs come from the data dir
(kvevaluator.paths.ldbc_dir; a second checkout shares one through its .env); --prep names them when missing. A workload with its own data
(OWN_GATE_STORES: JOB's sample for cpp-diff's job lane) has its data made as `just fetch-dataset` makes it (its dataset's
step, then the workload's data step; IMDb sample1pct: a pinned download and ~3 min on a fresh machine; ~6.5 min with its
stores), then its stores, the same way.

    python3 tests/gates/gate_preflight.py --gate write-gate --sf 0.1      # check; rc 1 + the list when incomplete
    python3 tests/gates/gate_preflight.py --gate all --sf 0.1 --prep      # ingest what is missing (just gate-prep)
"""
import argparse
import contextlib
import os
import subprocess
import sys

from kvevaluator import fetch_dataset, layouts, workloads
from kvevaluator.datasets import fetch_hint
from kvevaluator.capture import store_identity
from kvevaluator.capture.binary import binary_cpp_verdict, binary_dirty_verdict, binary_path, binary_provenance
from kvevaluator.measure.stores import (cpp_store_name, ensure_store, format_store_dir, store_chunk, store_dir,
                                        store_input_stale, store_staleness)

# (format, schema, chunk in bytes or None | "default" = the format's own): the stores a gate's parts open
GATE_STORES = {
    # 5/7 cross-language write effects (dgraph, nebula, arangodb, pertype_csr), 6/7 the write-count oracle (+ dgraph's
    # chunk arm)
    "write-gate": [("dgraph", "canonical", None), ("nebula", "canonical", None), ("arangodb", "canonical", None),
                   ("pertype_csr", "canonical", "default"), ("dgraph", "canonical", 65536),
                   # the write-count oracle's meta cells (check_cpp_write_counts.META_CELLS): a 4th field names the spec
                   ("meta", "canonical", None, "props_split_adj_split"),
                   ("meta", "canonical", None, "props_condensed_adj_condensed"),
                   ("meta", "canonical", None, "props_chunked_adj_chunked")],
    # check_cpp_vs_python's default format; kvsize and the engine checks build their own stores on first use, the
    # engine twins are listed so `gate-prep` makes them once instead of inside a check
    "cpp-diff": [("pertype_csr", "canonical", "default"),
                 ("nebula", "canonical", None), ("dgraph", "canonical", None),     # the BI differential's two formats
                 ("dgraph_engine", "canonical", None), ("nebula_engine", "canonical", None), ("arangodb_engine", "canonical", None)],
    # the chooser gate's named store (tests/gates/meta/check_chooser_named.py --names): the analysis-code gate level's
    # round opens no other store it does not build itself
    "chooser": [("pertype_csr", "canonical", "default")],
}
OPTIONAL = {"dgraph_engine", "nebula_engine", "arangodb_engine"}   # built on first use by their checks: prep only
# gate -> the layouts it predicts (kvcost predict) in an output tree of its own on the shared statistics. A layout's
# tally is assembled from the scale's tally library, and an entry is built only by a run in the library's own tree
# (tally_library.check_writable): so those entries are made here. The ONE list: the gate takes its layouts from it
# (tests/gates/check_predict_sizes.py; kept here, not in the gate, because this file may import no kvcost module:
# tests/unit/check_gate_levels.py, part 3)
GATE_STORES["predict-sizes"] = []        # opens no store
GATE_TALLIES = {"predict-sizes": [layouts.name_of(c) for c in layouts.CELLS]     # the grid's nine uniform layouts
                                 + ["Ch4k-Ch4k"]}                                  # and another chunk size beside them
# (workload, tag, format, the Dgraph chunk knob or None): the stores of a workload with its own data that a gate's parts open
OWN_GATE_STORES = {
    # cpp-diff's job lane (check_job_cpp_vs_python.py): the Python twin of Dgraph holds unbounded postings, so its store
    # is the unchunked `dgraph` (KVEVAL_DGRAPH_CHUNK=0), an oracle-only store beside the reported dgraph-c512k
    "cpp-diff": [("job", "sample1pct", "dgraph", "0"), ("job", "sample1pct", "nebula", None),
                 ("job", "sample1pct", "arangodb", None)],
}


@contextlib.contextmanager
def _dgraph_chunk(knob):
    """the Dgraph chunk knob an own-data store is named and ingested with, set for the block (None: left as it is)"""
    was = os.environ.get("KVEVAL_DGRAPH_CHUNK")
    if knob is not None:
        os.environ["KVEVAL_DGRAPH_CHUNK"] = knob
    try:
        yield
    finally:
        if was is None:
            os.environ.pop("KVEVAL_DGRAPH_CHUNK", None)
        else:
            os.environ["KVEVAL_DGRAPH_CHUNK"] = was


def own_stores(gates, sf, prep, binary, missing):
    """the own-data stores of the gates: the data tag made first (--prep) or named; each store checked, ingested (--prep)
    or named; a stale one named, never re-ingested. Appends to `missing`; returns (stores, ingested)"""
    need = sorted({t for g in gates for t in OWN_GATE_STORES.get(g, [])}, key=str)
    built, data_ok = 0, {}
    for wl, tag, fmt, knob in need:
        if (wl, tag) not in data_ok:
            ds = workloads.get(wl).dataset
            if fetch_dataset.missing(ds, tag) and prep and binary:
                print(f"[gate-prep] making the {ds} data {tag} (a fresh machine: a download and ~3 min)", flush=True)
                fetch_dataset.ensure(ds, tag)
            data_ok[(wl, tag)] = not fetch_dataset.missing(ds, tag)
            if not data_ok[(wl, tag)]:
                missing.append((f"the {ds} data {tag} (sf{sf} gates: {wl}'s oracle check)",
                                f"just gate-prep {sf}  (or: {fetch_hint(ds, tag)})"))
        if not data_ok[(wl, tag)]:
            continue
        with _dgraph_chunk(knob):
            ds = workloads.get(wl).dataset
            path = format_store_dir(ds, tag, fmt)
            name = f"{wl} {tag}/{os.path.basename(path)}"
            if store_identity.store_built(path)[0]:
                state, why = store_staleness(path, workload=wl, layout=False) if binary else (store_identity.USABLE, "")
                why = why if state == store_identity.STALE else store_input_stale(ds, tag, path)
                if why:
                    missing.append((f"store {name} is STALE: {why}", f"move {path} aside: gate-prep ingests it again"))
                continue
            if prep and binary:
                print(f"[gate-prep] ingesting {name}", flush=True)
                ensure_store(ds, tag, fmt)
                built += 1
            else:
                missing.append((f"store {name}", f"just gate-prep {sf}"))
    return len(need), built


def gate_tallies(gates, sf, prep, missing):
    """the tally library's entries the gates' layouts (GATE_TALLIES) are assembled from, at ldbc `sf`: built when
    missing (prep; the record stream dumped first when there is none), else named in `missing`. Chosen by kvcost
    predict's own rule (tally_library.library_tallies), so a gate's predict call finds every entry it asks for.
    -> (layouts, library settings built now)"""
    names = []
    for g in gates:
        names += [x for x in GATE_TALLIES.get(g, []) if x not in names]
    if not names:
        return 0, 0
    from kvevaluator import datasets
    from kvevaluator.measure import run_measure
    from kvevaluator.measure.records import records_cache_path, records_file
    from kvevaluator.paths import ROOT
    from kvevaluator.predict import tally_library as TL
    what = f"the tallies of {len(names)} layouts (sf{sf}; {' + '.join(g for g in gates if g in GATE_TALLIES)})"
    how = f"just gate-prep {sf}"
    stream = records_cache_path("ldbc", sf)
    if not prep and not os.path.exists(stream):
        missing.append((what, f"no record stream {stream}: {how}"))
        return len(names), 0
    try:
        records = records_file("ldbc", sf)
        jobs, why = run_measure.at_once(None, 1, TL.pass_peak_gb(records), "a pass over the record stream")
        specs = {lay.cell: lay.spec for lay in map(layouts.resolve, names)}
        # a check builds nothing: a root that holds no library, so a missing entry is refused and named here
        lib, _ = TL.library_tallies("ldbc", sf, specs, records, str(ROOT / datasets.get("ldbc").schema), jobs,
                                    root=None if prep else os.path.join(os.path.dirname(stream), "check-only"))
    except SystemExit as e:
        built_elsewhere = "outside this run's output root" in str(e)      # the check's own refusal: an entry is missing
        missing.append((what, f"an entry of the tally library is missing: {how}" if built_elsewhere and not prep else
                        f"{e}" + ("" if prep else f"  [{how}]")))
        return len(names), 0
    print(f"[{'gate-prep' if prep else 'preflight'}] tallies: {len(names)} layouts ({', '.join(names)}) from the "
          f"tally library {lib.path}: {len(lib.built)} settings built ({why}), {len(set(lib.reused))} reused", flush=True)
    return len(names), len(lib.built)


def _meta_spec(cell):
    """a meta cell's grid spec file (None for an engine format's store); meta's store name carries it"""
    if cell is None:
        return None
    from kvevaluator.paths import ROOT
    return str(ROOT / "inputs" / "specs" / "grid" / f"{cell}.json")


def unit(entry):
    """(format, schema, chunk, meta cell or None) of a GATE_STORES entry"""
    return (*entry, None) if len(entry) == 3 else tuple(entry)


def store_name(entry):
    fmt, schema, chunk, cell = unit(entry)
    return cpp_store_name(fmt, schema, None, chunk_of(fmt, chunk), spec=_meta_spec(cell))


def chunk_of(fmt, chunk):
    return store_chunk(fmt) if chunk == "default" else chunk


def ingest_cmd(sf, fmt, schema, chunk, cell=None):
    if cell:   # a meta cell: its grid spec's store
        return f"python3 -m kvevaluator.measure.ensure_stores --sf {sf} --cells {cell}"
    c = chunk_of(fmt, chunk)
    return (f"run/ingest-cpp-stores.sh --sf {sf} --formats {fmt}" + ("" if schema == "canonical" else f" --schema {schema}")
            + (f" --chunk {c // 1024}k" if c and chunk != "default" else ""))


def ldbc_data_missing(sf):
    """the SF's LDBC CSV dir when it is missing (the ingest needs it), else None"""
    from kvevaluator.paths import ldbc_dir
    d = ldbc_dir(sf)
    return None if d.is_dir() else d


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gate", required=True, choices=list(GATE_STORES) + ["all"])
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--prep", action="store_true", help="ingest the missing stores (seconds each at sf0.1)")
    ap.add_argument("--lanes", default="all", help="cpp-diff's lanes (run/cpp-diff.sh): the engine parts' needs are "
                    "checked only when they run (all, or a list with engine)")
    ap.add_argument("--names", action="store_true", help="print the gate's store names, one per line, and exit "
                    "(for tests/gates/store_copies.py)")
    a = ap.parse_args()
    stores = dict(GATE_STORES)
    gates = list(stores) if a.gate == "all" else [a.gate]
    if a.names:
        names = [store_name(e) for g in gates for e in stores[g]]
        print("\n".join(sorted(set(names))))
        return 0
    engine_lane = a.lanes == "all" or "engine" in a.lanes.split(",")
    if "cpp-diff" in gates and not a.prep and engine_lane:   # its engine parts import kvengines' clients: refused here, not 10 min in
        try:
            import kvengines  # noqa: F401
        except ImportError as e:
            raise SystemExit(f"preflight cpp-diff: needs the engines extra: run `just setup` ({e})")
    missing = []
    try:
        binary = binary_path()
        if not os.access(binary, os.X_OK) or subprocess.run([binary, "--version"], capture_output=True).returncode != 0:
            raise SystemExit(f"{binary} does not run")
    except SystemExit as e:
        binary = None
        missing.append(("the C++ binary", f"{e}  [build: just build, or set KVEVAL_CPP_BIN to a built kvevaluator]"))
    if binary:     # the binary must have HEAD's C++ (binary_cpp_verdict, the one rule; a named control may override)
        prov = binary_provenance()
        ok, msg = binary_cpp_verdict(prov["binary_git_sha"])
        print(f"preflight: {msg}")
        if not ok:
            missing.append(("a binary with HEAD's C++", msg))
        if not a.prep:   # and a clean one (binary_dirty_verdict); gate-prep only builds stores
            ok, msg = binary_dirty_verdict(prov)
            print(f"preflight: {msg}")
            if not ok:
                missing.append(("a clean binary", msg))
    later = []                               # --prep only: pieces gate-prep does not make
    seen, need = set(), []
    for g in gates:
        for e in stores[g]:
            name = store_name(e)
            if name not in seen:
                seen.add(name)
                need.append((g, name, *unit(e)))
    built = 0
    if a.prep and binary:
        gone = ldbc_data_missing(a.sf)
        if gone:
            print(f"[gate-prep] no LDBC CSVs of sf{a.sf} at {gone}: `{fetch_hint('ldbc', a.sf)}`, or point the data dir at "
                  "a copy (.env)", flush=True)
    for g, name, fmt, schema, chunk, cell in need:
        path = store_dir("ldbc", a.sf, name, _meta_spec(cell))
        have = store_identity.store_built(path)[0]     # a half-built dir is no store: --prep moves it aside and rebuilds
        if have:
            # a store ingested under another key layout than this binary reads would be refused at its first open,
            # minutes into a round: named here, with its rebuild line (never re-ingested silently, --prep or not)
            # (the one check, store_staleness; layout=False: the gates compare counts, which the index +
            # filter layout does not change)
            state, why = store_staleness(path, layout=False) if binary else (store_identity.USABLE, "")
            if state == store_identity.STALE:
                missing.append((f"store {name} (sf{a.sf}; {g}) is STALE: {why}",
                                store_identity.rebuild_hint(a.sf, fmt, schema, None, chunk_of(fmt, chunk))))
            continue
        if a.prep and binary:
            print(f"[gate-prep] ingesting {name} (sf{a.sf})", flush=True)
            ensure_store("ldbc", a.sf, fmt, schema_name=schema, chunk=chunk_of(fmt, chunk), spec=_meta_spec(cell))
            built += 1
        elif fmt not in OPTIONAL or a.prep:
            missing.append((f"store {name} (sf{a.sf}; {g})", ingest_cmd(a.sf, fmt, schema, chunk, cell)))
    n_own, built_own = own_stores(gates, a.sf, a.prep, binary, missing)
    built += built_own
    n_tallies, built_tallies = gate_tallies(gates, a.sf, a.prep, missing) if binary else (0, 0)
    label = " + ".join(gates)
    if missing:
        print(f"INCOMPLETE: {label} cannot run every part -- {len(missing)} piece(s) missing or stale:")
        for what, how in missing:
            print(f"  {what}: {how}")
        print(f"  (the missing stores at once: just gate-prep {a.sf}; a stale store takes its own line above)")
        return 1
    print(f"preflight {label} sf{a.sf}: binary {binary}; {len(need)} stores" + (f" + {n_own} of own-data workloads" if n_own else "")
          + (f", {built} ingested now" if a.prep else " checked")
          + (f"; {n_tallies} layouts' tallies" + (f", {built_tallies} library settings built now" if a.prep else " checked")
             if n_tallies else "")
          + (": the stores are complete" if later else ": every part can run"))
    for what, how in later:
        print(f"  STILL TO MAKE before write-gate (gate-prep does not): {what}: {how}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
