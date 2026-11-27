#!/usr/bin/env python3
"""The decided store and timing settings hold in a timed run (inputs/contract/store_config.json): one tiny
capture per timed regime, with the flags grid_latency.py gives that regime (its own parser and defaults), on the binary
under test; each capture's steps.csv.meta.json (regimes.<r>: perf level, direct I/O, the store options the server
reported) against the block, field by field (kvevaluator.measure.store_config.violations). Then two write captures
(one write, one param, each on its own arena) held to the steady rule (store_config.write_violations, want steady):
cppwlatency --cache steady, and the writes after a steady read warm-up (--warm-reads: the steady capture above warms the
cache). 0 fields compared fails.

    python3 tests/gates/check_store_config.py [--sf 0.1] [--format dgraph] [--query is1]
    --expect-fail [readahead|fill_cache]: the run must find that control binary's store-option violation
                   (store_readahead_off, store_fill_cache_off; exit 0 then, with "CONTROL BINARY: caught by its signature")
    --names: print the store this check opens (for store copies) and exit
    --plant: seven plants, each must be caught: in place of the cold flags, PerfLevel 3 undeclared (cpplatency must
             refuse it) and buffered reads (the record must show direct_io False); a steady run at twice the decided
             cache; a warm run on a copy of the store ingested at 16 KB blocks (in a temp dir, seconds at sf0.1); a
             write capture timed --cache warm, held to the steady rule; --cache steady with a hand-given cache
             (cppwlatency must refuse it); the writes after a warm-up from the twice-the-cache steady capture
             (cppwlatency must refuse a capture of other read settings)
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from kvevaluator.paths import ROOT

EXP = str(ROOT)
from kvevaluator.measure import store_config
from kvevaluator.measure.reingest_stores import ingest_command
from kvevaluator.capture import binary
from kvevaluator import output_tree
from kvevaluator.measure import stores
from kvcost.latency import grid_latency as GL

PLANT_BLOCK_BYTES = 16384
SIGNATURE = {"readahead": ["store option max_auto_readahead_size = 0"],
             "fill_cache": ["store option fill_cache = False"],
             "plant": ["runs RocksDB's per-call timers inside the walls", "cold-plant-buffered: cold: direct_io False",
                       "steady-plant-cache2x: steady: the driver's block_cache_bytes",
                       "steady-plant-cache2x: steady: the server's block_cache_bytes",
                       f"warm-plant-block16k: warm: store option block_size = {PLANT_BLOCK_BYTES}",
                       "steady-write-plant-warm: steady write: the driver's direct_io False",
                       "steady-write-plant-warm: steady write: pass headline: the server's block_cache_bytes",
                       "steady-write-plant-cache-flag: cppwlatency refused it: --cache steady takes its cache",
                       "steady-warmup-plant-cache2x: cppwlatency refused it: --warm-reads: its steady settings differ"]}
WRITE_QUERY = "w_upd_plain"     # one write, one param: the write capture this check makes (a property update, ~ms)


def regimes_at(sf, steady_mib):
    """{regime: cpplatency flags} as grid_latency.py gives them: one warm, one cold, one steady at steady_mib"""
    ns = GL.build_parser().parse_args(["--sf", sf, "--counts", "-", "--regimes", "warm,cold,steady",
                                       "--warm", "1,1", "--cold", "1", "--steady", f"{steady_mib},1"])
    return GL.regimes_of(ns)


def write_runs(plant, reads="", reads2x=""):
    """{name: cppwlatency flags}: the steady write capture, the writes after a warm-up from the steady read capture
    `reads`, and with plant their three plants (`reads2x`: the steady capture at twice the cache)"""
    runs = {"steady-write": ["--cache", "steady"],
            "steady-warmup-write": ["--cache", "steady", "--warm-reads", reads]}
    if plant:
        runs["steady-write-plant-warm"] = ["--cache", "warm"]
        runs["steady-write-plant-cache-flag"] = ["--cache", "steady", "--block-cache-gb", "8"]
        runs["steady-warmup-plant-cache2x"] = ["--cache", "steady", "--warm-reads", reads2x]
    return runs


def plant_block_store(sf, fmt, root):
    """ingest the store again under root/<dataset>/<scale>/<fmt> (the store tree's shape, as KVEVAL_STORE_DIR=root reads it)
    at PLANT_BLOCK_BYTES (its manifest's other options kept)"""
    with open(os.path.join(stores.store_dir("ldbc", sf, fmt), "manifest.json")) as f:
        manifest = json.load(f)
    out = os.path.join(root, str(output_tree.place("ldbc", sf)), fmt)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    cmd, why = ingest_command(manifest, out)
    if cmd is None or "@spec" in cmd:
        raise SystemExit(f"FAIL: cannot rebuild {fmt} from its manifest for the block plant ({why or 'a spec format'})")
    stores.run_ingest("ldbc", sf, cmd + ["--block-size", str(PLANT_BLOCK_BYTES)])


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--format", default="dgraph", help="an engine format whose sf store exists (gate-prep builds them)")
    ap.add_argument("--query", default="is1")
    ap.add_argument("--expect-fail", nargs="?", const="readahead", choices=["readahead", "fill_cache"])
    ap.add_argument("--plant", action="store_true")
    ap.add_argument("--names", action="store_true", help="print the store this check opens (for store copies) and exit")
    a = ap.parse_args()
    if a.names:
        print(a.format)
        return 0
    print("binary:", json.dumps(binary.binary_provenance()), flush=True)
    rule = store_config.decided()
    print("decided:", json.dumps(rule, sort_keys=True), flush=True)
    mib = store_config.steady_cache_mib(a.sf, rule)
    print(f"steady cache: {mib} MiB at sf {a.sf}", flush=True)
    regimes = regimes_at(a.sf, mib)
    got = sorted(store_config.regime_of(r) for r in regimes)
    if got != ["cold", "steady", "warm"]:
        raise SystemExit(f"FAIL: grid_latency gave the regimes {sorted(regimes)}, want one warm, one cold, one steady")
    plants = set()
    if a.plant:                                   # the driver's cold flags with readahead off (the plant), in two halves
        cold = next(r for r in regimes if r.startswith("cold"))
        base = regimes.pop(cold)
        cut = base[:base.index("--perf-level")]
        regimes["cold-plant-perf3"] = cut + ["--perf-level", "3"]
        regimes["cold-plant-buffered"] = cut + ["--perf-level", "2", "--buffered-reads"]
        regimes["steady-plant-cache2x"] = next(f for r, f in regimes_at(a.sf, 2 * mib).items() if r.startswith("steady"))
        regimes["warm-plant-block16k"] = next(f for r, f in regimes.items() if r.startswith("warm"))
        plants = {"cold-plant-perf3", "cold-plant-buffered", "steady-plant-cache2x", "warm-plant-block16k"}
    total, bad = 0, []
    with tempfile.TemporaryDirectory(prefix="check-store-config-", dir=os.environ.get("TMPDIR")) as tmp:
        plant_env = None
        if a.plant:                                   # the 16 KB store: its own root, so no copy or original is touched
            plant_root = os.path.join(tmp, "plant-stores")
            plant_block_store(a.sf, a.format, plant_root)
            plant_env = {k: v for k, v in os.environ.items() if k != "KVEVAL_STORE_COPIES"}
            plant_env["KVEVAL_STORE_DIR"] = plant_root
        for regime, flags in regimes.items():
            out = os.path.join(tmp, regime)
            cmd = [sys.executable, os.path.join(EXP, "python", "kvevaluator", "measure", "cpplatency.py"), "--sf", a.sf, "--family", "interactive",
                   "--format", a.format, "--arms", a.format, "--queries", a.query, "--nparam", "1", *flags, "--out", out]
            env = plant_env if regime == "warm-plant-block16k" else None
            p = subprocess.run(cmd, cwd=EXP, capture_output=True, text=True, env=env)
            if p.returncode != 0:
                said = (p.stdout + p.stderr).strip().splitlines()[-1:] or ["(no output)"]
                if regime not in plants:            # only a plant may be refused; any other exit is a failure
                    print(p.stdout[-2000:] + p.stderr[-2000:])
                    raise SystemExit(f"FAIL {regime}: cpplatency exit {p.returncode} ({' '.join(flags)})")
                bad.append(f"{regime}: cpplatency refused it: {said[0]}")
                print(f"  {regime}: refused by cpplatency ({' '.join(flags)}): {said[0]}", flush=True)
                continue
            with open(os.path.join(out, "steps.csv.meta.json")) as f:
                meta = json.load(f)
            n, b = store_config.violations(meta, rule)
            total += n
            bad += [f"{regime}: {x}" for x in b]          # the label: a plant's signature matches its own run only
            print(f"  {regime}: {n} fields compared, {len(b)} differ ({' '.join(flags)})", flush=True)
            for x in b:
                print(f"    {x}")
        # the write capture: cppwlatency on an arena of the same store (its own link copy, removed after), held to the
        # steady rule whatever cache it ran with (a plant timed warm must fail it)
        wplants = {r for r in write_runs(True) if "plant" in r}
        reads = os.path.join(tmp, next(r for r in regimes if store_config.regime_of(r) == "steady" and r not in plants))
        writes = write_runs(a.plant, reads, os.path.join(tmp, "steady-plant-cache2x"))
        for name, flags in writes.items():
            out = os.path.join(tmp, name)
            cmd = [sys.executable, "-m", "kvevaluator.measure.cppwlatency", "--sf", a.sf, "--arms", a.format,
                   "--queries", WRITE_QUERY, "--nparam", "1", "--pass", "headline", *flags, "--out", out]
            p = subprocess.run(cmd, cwd=EXP, capture_output=True, text=True)
            if p.returncode != 0:
                said = (p.stdout + p.stderr).strip().splitlines()[-1:] or ["(no output)"]
                if name not in wplants:
                    print(p.stdout[-2000:] + p.stderr[-2000:])
                    raise SystemExit(f"FAIL {name}: cppwlatency exit {p.returncode} ({' '.join(flags)})")
                bad.append(f"{name}: cppwlatency refused it: {said[0]}")
                print(f"  {name}: refused by cppwlatency ({' '.join(flags)}): {said[0]}", flush=True)
                continue
            with open(os.path.join(out, "steps.csv.meta.json")) as f:
                meta = json.load(f)
            n, b = store_config.write_violations(meta, rule, want="steady")
            if "--warm-reads" in flags and not ((meta.get("warm_reads") or {}).get(a.format) or {}).get("cells"):
                raise SystemExit(f"FAIL {name}: its meta records no read warm-up (warm_reads: {meta.get('warm_reads')})")
            total += n
            bad += [f"{name}: {x}" for x in b]
            print(f"  {name}: {n} fields compared, {len(b)} differ ({' '.join(flags)})", flush=True)
            for x in b:
                print(f"    {x}")
        regimes.update(writes)
    print(f"{len(regimes)} regimes, {total} fields compared, {len(bad)} differ")
    if total == 0:
        print("FAIL: 0 fields compared")
        return 1
    if a.expect_fail or a.plant:
        sigs = SIGNATURE["plant" if a.plant else a.expect_fail]
        missed = [sig for sig in sigs if not any(sig in x for x in bad)]
        if not missed:
            print(f"CONTROL {'PLANT' if a.plant else 'BINARY'}: caught by its signature ({', '.join(map(repr, sigs))})")
            return 0
        print(f"CONTROL VOID: no violation with the signature {', '.join(map(repr, missed))}")
        return 1
    print("PASS: every timed regime runs with the decided settings" if not bad else "FAIL: settings differ from inputs/contract/store_config.json")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
