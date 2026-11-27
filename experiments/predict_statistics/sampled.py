#!/usr/bin/env python3
"""The sampled tallies: kvevaluator tally --owner-sample on the seven diagonal layouts, the 49 assembled from them, and
how far each one is from the full tally. Opens no store. The steps the driver (run-predict-statistics-sampled.sh) runs:

    sampled.py tallies --sf 3 [--records R] --out <dir> --percent 1 --seed 1 [--floor F] [--jobs 7] [--fresh]
        (without --records: the dataset's own record stream at --sf; a diagonal already tallied on the same stream
        and sample is kept)
        -> <dir>/p<P>-s<S>/tally-diagonal/<diagonal>.json, then the 49 in <dir>/p<P>-s<S>/tally/meta-<sha12>.json
           (--floor F: tally --owner-floor F, at least F owners of every vertex type, in <dir>/p<P>-floor<F>-s<S>/)
           (specs in <dir>/p<P>-s<S>/specs/; a full tally of a cell: the tree's stats/ldbc/sf<SF>/uniform-49/)
           (the driver's predict stage then runs kvcost predict on them: <dir>/p<P>-s<S>/predicted/ldbc/sf<SF>/...)
    sampled.py control --sf 3 --records R --full <output tree> --out <dir> [--cells a,b] [--jobs 3]
        --owner-sample 100 on the given diagonals: each must equal the full tally (wall time and provenance aside; the record
        stream by its content, schema and lines); exit 1
    sampled.py errors --full <output tree> --sf 3 --out <dir>
        -> <dir>/tally_errors.csv: per (sample, diagonal, row kind, row): the full value, the sampled one, the
           relative error (kinds: group, chunks, locality, and locality_bucket: one owner's span per owner-degree
           bucket, what predict mode reads); <dir>/tally_errors_summary.csv: per (sample, diagonal, row kind, measure): the rows, the
           worst relative error, the byte-weighted mean (value bytes; chunks: chunk counts; locality: owners)
    sampled.py picks --full <output tree> --out <dir> --sf 3 [--set predicted-tier2]   (default: the default tier's set)
        -> <dir>/picks_vs_full.csv: per set, sample, currency (wops, wops_bytes4k), per query and I02, the layout named
           from the sampled tallies against the one named from the full tallies (level predicted; each layout's plan the fewest
           predicted wops_bytes4k; I02 at I01's frequencies), the full prediction's rank and cost of the sampled name,
           and the rank correlation of the 49; a query either side cannot cost on every layout is named
           (queries_not_costed) and left out of both, I02 then over the rest. A run replaces its --set's rows only
The paper's workloads (I01, B01, J01; the driver's --workloads): tallies and control take --dataset ldbc|imdb and
--full-tallies <tally dir> (a snapshot's tally library), and three more steps:
    sampled.py no-locality --full-tallies <dir> --out <dir>      -> <dir>/no-locality/tally: the 49 without locality rows
    sampled.py check-provenance --variant <dir> --full-tallies a,b --percent P --seed S --floor F --out <dir>
        every predicted capture below <dir> read a tally of that sample (owner_sample, sha256 not a full tally's),
        assembled from two of the sample's diagonals; -> <dir>/tallies.json; exit 1 on any refusal or 0 records
    sampled.py full-bound --full-tallies <dir> --sample <sampled variant dir> --out <dir>
        -> <dir>/full-bound-<sample>/tally: the full 49 with only the sample's assumed last chunks swapped in
    sampled.py paper-picks --variants name=tree,... --reference full --out <dir>
        -> <dir>/picks_by_workload.{csv,md}: per workload, tier, model and variant, from each tree's predict-49 tables
"""
import argparse
import csv
import glob
import json
import math
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from kvcost.predict_accuracy import uniform_49_tallies as U
from kvevaluator import contract
from kvevaluator.paths import cpp_bin
from kvevaluator.predict import assemble_tally as A


def sample_dirs(out):
    return sorted(d for d in glob.glob(os.path.join(out, "p*-s*")) if os.path.isdir(d))


def schema_of(dataset):
    """the dataset's schema file (kvevaluator.datasets.schema_path): LDBC's, or IMDb's for J01"""
    from kvevaluator.datasets import schema_path
    return str(schema_path(dataset))


def run_tally(name, records, out_path, spec_dir, percent, seed, floor, dataset):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    spec_path = os.path.join(spec_dir, f"{name}.json")
    os.makedirs(spec_dir, exist_ok=True)
    with open(spec_path, "w") as f:
        json.dump(U.spec_of(name), f, indent=1, sort_keys=True)
    t0 = time.time()
    tmp = f"{out_path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        subprocess.run([str(cpp_bin()), "tally", "--schema", schema_of(dataset or U.DATASET), "--spec", spec_path, "--records", records,
                        "--owner-sample", str(percent), "--seed", str(seed)]
                       + (["--owner-floor", str(floor)] if floor else []), stdout=f, check=True)
    os.replace(tmp, out_path)
    return time.time() - t0


def made_before(path, name, records, percent, seed, floor, binary, version):
    """whether the sampled tally at `path` stands for this call: the same layout, sample and record stream, counted by
    a clean binary at this tally version (the tally library's own rules: tally_library.made_from_stream, reusable),
    its last chunks assumed at the contract's fill (owner_sample.last_chunk_fill; a sample made before has none)"""
    from kvevaluator.predict import tally_library as TL
    if not os.path.exists(path):
        return False
    with open(path) as f:
        have = json.load(f)
    sample = have.get("owner_sample") or {}
    return (TL.canonical(have.get("spec")) == TL.canonical(U.spec_of(name))
            and (sample.get("percent"), sample.get("seed"), sample.get("floor", 0)) == (percent, seed, floor)
            and sample.get("last_chunk_fill") == contract.last_chunk_fill()
            and TL.made_from_stream(have.get("records") or {}, records)
            and TL.reusable(TL.made_by(have.get("provenance") or {}), have.get("tally_version"), binary, version))


def cmd_tallies(a):
    from kvevaluator.predict import tally_library as TL
    if not a.records:        # the dataset's own record stream at --sf, dumped on first use
        from kvevaluator.measure.records import records_file
        if not a.sf:
            sys.exit("sampled.py tallies: give --records, or --sf (the dataset's own record stream at that scale)")
        a.records = records_file(a.dataset or U.DATASET, a.sf)
    d = os.path.join(a.out, U.sample_name(a.percent, a.seed, a.floor))
    binary, version = TL.binary_info()
    jobs, kept = [], []
    for c in U.DIAGONAL_CELLS:
        path = os.path.join(d, "tally-diagonal", f"{c}.json")
        (kept if not a.fresh and made_before(path, c, a.records, a.percent, a.seed, a.floor, binary, version)
         else jobs).append((c, path))
    for c, _ in kept:
        print(f"   sampled tally {os.path.basename(d)} {c}: already made on this record stream and sample -- kept "
              f"(--fresh tallies it again)")
    with ThreadPoolExecutor(a.jobs) as pool:
        for (c, _), s in zip(jobs, pool.map(lambda j: run_tally(j[0], a.records, j[1], os.path.join(d, "specs"),
                                                                 a.percent, a.seed, a.floor, a.dataset), jobs)):
            print(f"   sampled tally {os.path.basename(d)} {c}: {s:.0f} s")
    print(f"sampled tallies {os.path.basename(d)}: {len(jobs)} of {len(jobs) + len(kept)} diagonals tallied, {len(kept)} kept")
    U.assemble_pairs(d)


def cmd_control(a):
    cells = U.DIAGONAL_CELLS if a.cells == "all" else a.cells.split(",")
    d = os.path.join(a.out, "p100-control")
    jobs = [(c, os.path.join(d, f"{c}.json")) for c in cells]
    with ThreadPoolExecutor(a.jobs) as pool:
        list(pool.map(lambda j: run_tally(j[0], a.records, j[1], os.path.join(d, "specs"), 100, 1, 0, a.dataset), jobs))
    bad = 0
    for c, p in jobs:
        with open(p) as f:
            got = json.load(f)
        with open(full_tally(a, c)) as f:
            want = json.load(f)
        # the stream by its content (its path and freshness key name the machine and the key's code that made it)
        for t in (got, want):
            t["records"] = {k: t["records"].get(k) for k in ("content_sha256", "schema_sha256", "lines")}
        diff = A.differences(got, want)
        same_bytes = json.dumps(A.comparable(got)) == json.dumps(A.comparable(want))
        bad += bool(diff) or not same_bytes
        print(f"   --owner-sample 100 {c}: {len(diff)} rows differ, the same bytes outside wall time and provenance: {same_bytes}")
    print(f"sampled control: {len(jobs)} diagonals, {bad} differ: {'PASS' if not bad and jobs else 'FAIL'}")
    return 1 if bad or not jobs else 0


def full_tally(a, c):
    """the full tally of cell c: in --full-tallies (a tally dir, each file named by its spec's store name, as
    kvcost predict --tally-dir reads it: a snapshot's tally library), else the 49's tallies of the output tree --full
    names (kvcost predict on the 49: stats/ldbc/sf<SF>/uniform-49/)"""
    if a.full_tallies:
        from kvevaluator.measure import stores
        return os.path.join(a.full_tallies, f"{stores.meta_store_name(U.write_spec(a.out, c))}.json")
    if not a.sf:
        raise SystemExit("sampled: --full needs --sf (the scale whose 49 tallies it reads)")
    from kvevaluator.output_tree import Tree
    return U.tally_file(str(Tree(a.full).place_dir("stats", U.DATASET, a.sf) / "uniform-49"), c)


def rel(s, f):
    return (s - f) / f if f else (0.0 if s == 0 else math.inf)


def cmd_errors(a):
    rows, summary = [], []
    for sd in sample_dirs(a.out):
        sample = os.path.basename(sd)
        for c in U.DIAGONAL_CELLS:
            with open(os.path.join(sd, "tally-diagonal", f"{c}.json")) as f:
                s = json.load(f)
            with open(full_tally(a, c)) as f:
                full = json.load(f)
            per = {}   # (kind, measure) -> [(rel error, weight)]
            gs = {tuple(g[k] for k in A.GROUP_FIELDS): g for g in s["groups"]}
            for g in full["groups"]:
                k = tuple(g[x] for x in A.GROUP_FIELDS)
                x = gs.get(k, {})
                for m in ("keys", "key_bytes", "value_bytes"):
                    e = rel(x.get(m, 0), g[m])
                    rows.append({"sample": sample, "layout": c, "kind": "group", "row": "|".join(k), "measure": m,
                                 "full": g[m], "sampled": x.get(m, 0), "rel_error": e})
                    per.setdefault(("group", m), []).append((e, g["key_bytes"] + g["value_bytes"]))
            for fam, ch in full["chunks"].items():
                x = s["chunks"].get(fam, {}).get("chunks", 0)
                e = rel(x, ch["chunks"])
                rows.append({"sample": sample, "layout": c, "kind": "chunks", "row": fam, "measure": "chunks",
                             "full": ch["chunks"], "sampled": x, "rel_error": e})
                per.setdefault(("chunks", "chunks"), []).append((e, ch["chunks"]))
            ls = {tuple(r[k] for k in A.LOCALITY_KEY): r for r in s["locality"]}
            for r in full["locality"]:
                k = tuple(r[x] for x in A.LOCALITY_KEY)
                x = ls.get(k, {}).get("mean_chunks", 0)
                e = rel(x, r["mean_chunks"])
                rows.append({"sample": sample, "layout": c, "kind": "locality", "row": "|".join(k),
                             "measure": "mean_chunks", "full": r["mean_chunks"], "sampled": x, "rel_error": e})
                per.setdefault(("locality", "mean_chunks"), []).append((e, r["owners"]))
                # per owner-degree bucket: the span one owner of that degree has (what predict mode reads, by degree)
                sb = {b[0]: b for b in ls.get(k, {}).get("by_degree", [])}
                for b in r["by_degree"]:
                    full_span = b[3] / b[1] if b[1] else 0.0
                    x = sb.get(b[0])
                    samp_span = x[3] / x[1] if x and x[1] else 0.0
                    e = rel(samp_span, full_span) if x and x[1] else math.inf   # a bucket the sample has no owner in
                    rows.append({"sample": sample, "layout": c, "kind": "locality_bucket", "row": "|".join(k) + f"|b{b[0]}",
                                 "measure": "span", "full": full_span, "sampled": samp_span, "rel_error": e})
                    per.setdefault(("locality_bucket", "span"), []).append((e, b[1]))
            for (kind, m), es in sorted(per.items()):
                fin = [(abs(e), w) for e, w in es if math.isfinite(e)]
                wsum = sum(w for _, w in fin)
                signed = [e for e, _ in es if math.isfinite(e)]
                summary.append({"sample": sample, "layout": c, "kind": kind, "measure": m, "rows": len(es),
                                "rows_missing_or_inf": len(es) - len(fin),
                                "rows_over": sum(e > 0.05 for e in signed), "rows_under": sum(e < -0.05 for e in signed),
                                "worst_abs_rel_error": max((e for e, _ in fin), default=0.0),
                                "weighted_mean_abs_rel_error": sum(e * w for e, w in fin) / wsum if wsum else 0.0})
    write(os.path.join(a.out, "tally_errors.csv"), rows)
    write(os.path.join(a.out, "tally_errors_summary.csv"), summary)
    print(f"sampled errors: {len(sample_dirs(a.out))} samples, {len(rows)} rows -> {a.out}/tally_errors*.csv")


def predicted_costs(cap):
    """{(cell, query): (wops, wops_bytes4k)} at level predicted from a predicted capture (kvcost predict), each (cell, query) at
    the plan rule's body (kvcost.predict_accuracy.predicted.pick: candidates.pick_body over the mean of params 1-5; a
    body with a step not estimated left out), each cost compared at 12 significant digits"""
    from kvcost.predict_accuracy import predicted as PD
    best = {}
    for c in U.CELLS:
        picks, _ = PD.pick(cap, c, None, [str(i) for i in range(1, 6)], view="measured")
        for q, (_, m, _) in picks.items():
            best[(c, q)] = (PD.comparable(PD.cost(m, "wops")), PD.comparable(PD.cost(m, "wops_bytes4k")))
    return best


def capture_of(root, sf, cap_set=None):
    """the level-B predicted capture of a set below a run's dir (kvcost predict with the output dir <dir>; default the
    default tier's set, kvcost.cli.predict.default_set)"""
    from kvcost.cli.predict import capture_dirs, default_set
    return capture_dirs(sf, "interactive", (cap_set or default_set(),), root)[0]


def ranks(v):
    order = sorted(v, key=lambda c: (v[c], c))
    return {c: i + 1 for i, c in enumerate(order)}


def spearman(a, b):
    cells = sorted(set(a) & set(b))
    ra, rb = ranks({c: a[c] for c in cells}), ranks({c: b[c] for c in cells})
    n = len(cells)
    return 1 - 6 * sum((ra[c] - rb[c]) ** 2 for c in cells) / (n * (n * n - 1)) if n > 1 else float("nan")


def compare_sample(full, samp, weights, cells):
    """per currency, per query and I02: the layout named from samp against the one named from full ({(cell, query):
    (wops, wops_bytes4k)} each). A query either side cannot cost on every cell (a step not estimated: a sample
    with no owner of a small type has no row for it) is not compared, and I02 sums the queries both cost; -> (rows,
    the queries not costed, in the workload's order)"""
    costed = [q for q in weights if all((c, q) in full and (c, q) in samp for c in cells)]
    missing = [q for q in weights if q not in costed]
    out = []
    for i, cur in enumerate(("wops", "wops_bytes4k")):
        for q in costed + ["I02"]:
            if q == "I02":
                f = {c: sum(weights[x] * full[(c, x)][i] for x in costed) for c in cells}
                s = {c: sum(weights[x] * samp[(c, x)][i] for x in costed) for c in cells}
            else:
                f = {c: full[(c, q)][i] for c in cells}
                s = {c: samp[(c, q)][i] for c in cells}
            rf = ranks(f)
            nf, ns = min(f, key=lambda c: (f[c], c)), min(s, key=lambda c: (s[c], c))
            out.append({"currency": cur, "what": q, "named_full": U.short(nf), "named_sampled": U.short(ns),
                        "same": int(nf == ns or f[ns] <= f[nf] * (1 + 1e-9)), "full_rank_of_sampled_name": rf[ns],
                        "full_cost_of_sampled_name_over_full_named": f[ns] / f[nf] if f[nf] else 1.0,
                        "spearman_49": spearman(f, s), "queries_not_costed": " ".join(missing)})
    return out, missing


def cmd_picks(a):
    from kvcost.cli.predict import default_set
    from kvcost.layout_cost import benchmarks
    a.set = a.set or default_set()
    (i02,) = benchmarks.select("ldbc", a.sf, "i02").values()
    weights = {q: n for _, q, n, _ in i02["items"]}
    full = predicted_costs(capture_of(a.full, a.sf, a.set))
    path = os.path.join(a.out, "picks_vs_full.csv")
    kept = []   # the rows of the other sets (tiers): a run replaces its own set's rows (and a file with no set column)
    if os.path.exists(path):
        with open(path) as f:
            kept = [r for r in csv.DictReader(f) if r.get("set") not in (None, a.set)]
    out = []
    for sd in sample_dirs(a.out):
        rows, missing = compare_sample(full, predicted_costs(capture_of(sd, a.sf, a.set)), weights, U.CELLS)
        out += [{"set": a.set, "sample": os.path.basename(sd), **r} for r in rows]
        for cur in ("wops_bytes4k", "wops"):
            rs = [r for r in rows if r["currency"] == cur]
            w = next(r for r in rs if r["what"] == "I02")
            print(f"   {a.set} {os.path.basename(sd)} {cur}: {sum(r['same'] for r in rs if r['what'] != 'I02')} of "
                  f"{len(rs) - 1} queries name the full tallies' layout; I02 names {w['named_sampled']} (full: "
                  f"{w['named_full']}, its full rank {w['full_rank_of_sampled_name']}), Spearman over the 49 "
                  f"{w['spearman_49']:.4f}" + (f"; not costed, left out of both sides: {', '.join(missing)}"
                                               if missing else ""))
    write(path, kept + out)
    print(f"sampled picks: {a.set}, {len(sample_dirs(a.out))} samples, {len(out)} rows -> {path}")
    return 0 if out else 1


def cmd_no_locality(a):
    """the full tallies with the locality rows removed: predict's landing estimate then costs a Chunked read at the
    uniform spread (cpp/kvevaluator/src/predict/landing.cc, a tally with no "locality" key). The variant is data only:
    every other byte of each tally is kept"""
    d = os.path.join(a.out, "no-locality")
    os.makedirs(os.path.join(d, "tally"), exist_ok=True)
    n = 0
    for c in U.CELLS:
        src = full_tally(a, c)
        with open(src) as f:
            t = json.load(f)
        if "locality" not in t:
            raise SystemExit(f"sampled no-locality: {src} has no locality rows: not a full tally")
        del t["locality"]
        name = os.path.basename(src)   # the spec's store name, as predict --tally-dir finds it
        with open(os.path.join(d, "tally", name), "w") as f:
            json.dump(t, f, indent=1)
            f.write("\n")
        n += 1
    print(f"sampled no-locality: {n} tallies without their locality rows -> {d}/tally (from {a.full_tallies or a.full})")
    return 0 if n == len(U.CELLS) else 1


def cmd_full_bound(a):
    """the control for the sample's last chunk: the full tallies with only chunks.<family>.last_chunk_value_bytes
    swapped for the sample's (every family the sample marks last_chunk_assumed: the pack target x the contract's
    last_chunk_fill), every other byte the full tally's: I01's writes on it against the full tallies give that
    assumption's effect alone, apart from the sample's (--sample: the sampled variant's dir, its 49 in <sample>/tally)
    -> <out>/full-bound-<sample name>/tally (the name is the step's, from when the sample's last chunk was a bound)"""
    name = os.path.basename(os.path.normpath(a.sample))
    d = os.path.join(a.out, f"full-bound-{name}")
    os.makedirs(os.path.join(d, "tally"), exist_ok=True)
    swapped = 0
    for c in U.CELLS:
        src = full_tally(a, c)
        fname = os.path.basename(src)
        with open(src) as f:
            t = json.load(f)
        with open(os.path.join(a.sample, "tally", fname)) as f:
            smp = json.load(f)
        if smp["spec"] != t["spec"]:
            raise SystemExit(f"sampled full-bound: {c}: the sample's tally is of another spec")
        for fam, ch in smp["chunks"].items():
            if ch.get("last_chunk_assumed"):
                if fam not in t["chunks"]:
                    raise SystemExit(f"sampled full-bound: {c}: {fam} is in the sample, not in the full tally")
                t["chunks"][fam]["last_chunk_value_bytes"] = ch["last_chunk_value_bytes"]
                swapped += 1
        with open(os.path.join(d, "tally", fname), "w") as f:
            json.dump(t, f, indent=1)
            f.write("\n")
    print(f"sampled full-bound: {len(U.CELLS)} full tallies, {swapped} last chunks swapped for {name}'s assumed ones -> {d}/tally")
    return 0 if swapped else 1


def sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def cmd_check_provenance(a):
    """every predicted capture below a sampled variant's dir read a sampled tally of that sample: each capture record's
    tally (path, sha256, as kvcost predict writes it) is a file below the variant's dir with that sha256, carrying an
    owner_sample block of the variant's percent, seed and floor, and so do the two diagonal tallies it was assembled
    from; no sha256 is one of the full tallies' (--full-tallies, comma-separated dirs). Writes <variant>/tallies.json
    (path, sha256, owner_sample per tally read); exit 1 on any refusal or when no record was found"""
    d = os.path.realpath(a.variant)
    full = {sha256_file(p) for fd in a.full_tallies.split(",") for p in glob.glob(os.path.join(fd, "*.json"))}
    if not full:
        raise SystemExit(f"sampled check-provenance: no full tally in {a.full_tallies}")
    want = {"percent": a.percent, "seed": a.seed}
    bad, read, records = [], {}, 0

    def check(path, sha, what):
        rp = os.path.realpath(path)
        if not rp.startswith(d + os.sep):
            bad.append(f"{what}: {path} is outside the variant's dir")
            return None
        if not os.path.exists(rp):
            bad.append(f"{what}: {path} is missing")
            return None
        got = sha256_file(rp)
        if sha and got != sha:
            bad.append(f"{what}: {path} has sha256 {got[:12]}, the record says {sha[:12]}")
        if got in full:
            bad.append(f"{what}: {path} is a full tally (its sha256 {got[:12]} is one of --full-tallies)")
        with open(rp) as f:
            t = json.load(f)
        o = t.get("owner_sample")
        if o is None:
            bad.append(f"{what}: {path} has no owner_sample block")
        elif ({k: o.get(k) for k in want} != want or o.get("floor", 0) != a.floor
              or "plant" in o):
            bad.append(f"{what}: {path} owner_sample {({k: o.get(k) for k in ('percent', 'seed', 'floor', 'plant')})}, "
                       f"not percent {a.percent:g} seed {a.seed} floor {a.floor}")
        return t, got
    for rec in sorted(glob.glob(os.path.join(d, "**", "captures", "*", "*", "reported", "counts", "*.json"), recursive=True)):
        with open(rec) as f:
            r = json.load(f)
        records += 1
        tl = r.get("tally") or {}
        if not tl.get("path"):
            bad.append(f"{rec}: no tally named")
            continue
        if tl["path"] in read:
            continue
        got = check(tl["path"], tl.get("sha256"), rec)
        if got is None:
            continue
        t, sha = got
        read[tl["path"]] = {"path": tl["path"], "sha256": sha, "owner_sample": t.get("owner_sample")}
        src = t.get("provenance", {}).get("assembled_from")
        if not src:
            bad.append(f"{tl['path']}: not assembled from two sampled diagonals (no assembled_from)")
            continue
        for side in ("props", "adjacency"):
            check(src[side]["path"], src[side]["sha256"], f"{tl['path']} ({side})")
    with open(os.path.join(d, "tallies.json"), "w") as f:
        json.dump(sorted(read.values(), key=lambda x: x["path"]), f, indent=1)
        f.write("\n")
    for b in bad[:20]:
        print(f"   REFUSED {b}")
    if a.expect is not None and records != a.expect:
        bad.append(f"{records} capture records, expected {a.expect} (49 layouts x the captures the driver made)")
    ok = not bad and records > 0
    print(f"sampled check-provenance {os.path.basename(d)}: {records} capture records, {len(read)} tallies read (and "
          f"their diagonals), {len(full)} full tallies refused by sha256, {len(bad)} refusals: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


PAPER_WORKLOADS = {   # workload -> (its dir in a variant's tree, the tiers predicted, the models in its table)
    "i01": ("i01-tier1", (1,), ("wops_bytes4k",)),
    "b01": ("bi-b01", (1, 2), ("wops_bytes4k",)),
    "j01": ("job-j01", (1, 2), ("wops", "wops_bytes4k")),
}
TIER_SET = {t: s for t, (s, _) in __import__("kvcost.cli.predict", fromlist=["TIER_SETS"]).TIER_SETS.items()}   # each tier's set


def derived_table(tree, tier, workload):
    """{model: {cell: (predicted, measured)}} from a variant's predict-49 table (paper/derive/predict_49.py, run on the
    variant's tree: <tree>/derived/tier<T>/predict-49-<workload>.csv)"""
    out = {}
    with open(os.path.join(tree, "derived", f"tier{tier}", f"predict-49-{workload}.csv")) as f:
        for r in csv.DictReader(f):
            out.setdefault(r["model"], {})[r["cell"]] = (float(r["predicted"]), float(r["measured"]))
    return out


def per_query_predicted(tree, tier, workload, model):
    """{query: {cell: predicted cost in model}}: B01 and J01 from the report's by_query.csv, I01 from cost-layouts'
    uniform.csv (one run of each query)"""
    from kvcost.layout_cost.cost_models import wops_bytes4k
    out = {}
    if workload == "i01":
        path = os.path.join(tree, "i01-tier1", "cost", "uniform.csv")
        with open(path) as f:
            for r in csv.DictReader(f):
                w, b = float(r["wops"]), float(r["bytes"])
                out.setdefault(r["query"], {})[r["layout"]] = w if model == "wops" else wops_bytes4k(w, b)
        return out
    path = os.path.join(tree, PAPER_WORKLOADS[workload][0], "reports", TIER_SET[tier], "layout_choice", "by_query.csv")
    with open(path) as f:
        for r in csv.DictReader(f):
            w, b = float(r["predicted_wops"]), float(r["predicted_bytes"])
            out.setdefault(r["query"], {})[r["cell"]] = w if model == "wops" else wops_bytes4k(w, b)
    return out


def rho(x):
    """Spearman's rho, or nan when it is undefined (a constant side)"""
    return float("nan") if x is None else x


def cmd_paper_picks(a):
    """per workload, model, tier and variant: predict's pick of the 49, its measured cost over the measured best,
    Spearman of the 49 against the measured side and against the full tallies' prediction, and the queries whose own
    pick moves from the full tallies' (picks_by_workload.csv, and a Markdown copy for reading)"""
    from kvcost.layout_cost import query_weighting as QW
    from kvcost.plotting.stats import spearman
    trees = dict(v.split("=", 1) for v in a.variants.split(","))
    if a.reference not in trees:
        raise SystemExit(f"sampled paper-picks: --reference {a.reference} is not one of --variants")
    rows = []
    for wl, (_, tiers, models) in PAPER_WORKLOADS.items():
        for tier in tiers:
            ref = derived_table(trees[a.reference], tier, wl)
            for m in models:
                rq = per_query_predicted(trees[a.reference], tier, wl, m)
                for name, tree in trees.items():
                    t = derived_table(tree, tier, wl)[m]
                    if len(t) != 49 or set(t) != set(ref[m]):
                        raise SystemExit(f"sampled paper-picks: {name} {wl} tier {tier} {m}: {len(t)} layouts, not the 49")
                    cells = sorted(t)
                    pred = {c: t[c][0] for c in cells}
                    meas = {c: t[c][1] for c in cells}
                    full = {c: ref[m][c][0] for c in cells}
                    pick, best, full_pick = QW.best(pred), QW.best(meas), QW.best(full)
                    q = per_query_predicted(tree, tier, wl, m)
                    moved = sorted(x for x in rq if x in q and QW.best(q[x]) != QW.best(rq[x]))
                    rows.append({"workload": wl, "tier": tier, "model": m, "tallies": name, "pick": U.short(pick),
                                 "pick_over_measured_best": meas[pick] / meas[best], "measured_best": U.short(best),
                                 "full_pick": U.short(full_pick), "same_pick_as_full": int(pick == full_pick),
                                 "spearman_vs_measured": rho(spearman([pred[c] for c in cells], [meas[c] for c in cells])),
                                 "spearman_vs_full": rho(spearman([pred[c] for c in cells], [full[c] for c in cells])),
                                 "queries": len(rq), "query_picks_moved": len(moved), "moved": " ".join(moved)})
    path = os.path.join(a.out, "picks_by_workload.csv")
    if os.path.exists(path):   # a run replaces its own tallies' rows; the other runs' rows (other seeds) stay
        with open(path) as f:
            mine = {r["tallies"] for r in rows}
            kept = [r for r in csv.DictReader(f) if r["tallies"] not in mine]
        for r in kept:
            for k in ("tier", "queries", "query_picks_moved", "same_pick_as_full"):
                r[k] = int(r[k])
            for k in ("pick_over_measured_best", "spearman_vs_measured", "spearman_vs_full"):
                r[k] = float(r[k])
        rows = sorted(kept + rows, key=lambda r: (list(PAPER_WORKLOADS).index(r["workload"]), r["tier"], r["model"],
                                                  r["tallies"] != a.reference, r["tallies"]))
    write(path, rows)
    with open(os.path.join(a.out, "picks_by_workload.md"), "w") as f:
        f.write("> Generated by `sampled.py paper-picks`; each "
                "pick's cost is the measured one over the measured best of the 49; Spearman over the 49.\n\n")
        f.write("| workload | tier | model | tallies | pick | measured / best | measured best | same pick as full | "
                "Spearman vs measured | Spearman vs full | query picks moved |\n|" + "---|" * 11 + "\n")
        f.writelines(f"| {r['workload'].upper()} | {r['tier']} | {r['model']} | {r['tallies']} | {r['pick']} | "
                    f"{r['pick_over_measured_best']:.4f} | {r['measured_best']} | {'yes' if r['same_pick_as_full'] else 'no'} | "
                    f"{r['spearman_vs_measured']:.4f} | {r['spearman_vs_full']:.4f} | {r['query_picks_moved']} of "
                    f"{r['queries']}{(': ' + r['moved']) if r['moved'] and len(r['moved']) < 80 else ''} |\n" for r in rows)
    for r in rows:
        print(f"   {r['workload']} tier {r['tier']} {r['model']} {r['tallies']}: pick {r['pick']} "
              f"({r['pick_over_measured_best']:.4f}x the measured best {r['measured_best']}), Spearman vs measured "
              f"{r['spearman_vs_measured']:.4f}, vs full {r['spearman_vs_full']:.4f}; {r['query_picks_moved']} of "
              f"{r['queries']} query picks moved")
    print(f"sampled paper-picks: {len(rows)} rows -> {a.out}/picks_by_workload.csv")
    return 0 if rows else 1


def write(path, rows):
    if not rows:
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, list(rows[0]), lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: (f"{v:.6g}" if isinstance(v, float) else v) for k, v in r.items()})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("step", choices=("tallies", "control", "errors", "picks", "no-locality", "full-bound",
                                     "check-provenance", "paper-picks"))
    ap.add_argument("--sample", help="full-bound: the sampled variant's dir whose bounds are swapped in")
    ap.add_argument("--expect", type=int, default=None, help="check-provenance: the capture records expected")
    from kvevaluator import datasets
    ap.add_argument("--dataset", default=None, choices=datasets.names(),
                    help="tallies, control: the schema's dataset (default: the study's, uniform49.DATASET)")
    ap.add_argument("--full-tallies", default=None, help="the full tallies as a tally dir (a snapshot's tally library; "
                    "check-provenance: comma-separated dirs, every file refused by its sha256)")
    ap.add_argument("--variant", help="check-provenance: a sampled variant's dir")
    ap.add_argument("--variants", help="paper-picks: name=tree,... (each tree as the driver lays it out)")
    ap.add_argument("--reference", default="full", help="paper-picks: the variant the others are compared with")
    ap.add_argument("--sf", help="the scale; tallies without --records: the dataset's own record stream at it")
    ap.add_argument("--records", help="the record stream tallied (default with --sf: the dataset's own, dumped on first use)")
    ap.add_argument("--fresh", action="store_true", help="tallies: tally every diagonal again (default: one already "
                    "made from the same record stream and sample by a clean binary at this tally version is kept)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--full", help="the output tree of the full run (kvcost predict --dataset ldbc=<SF> <the 49 layouts>: its "
                                   "49 tallies in stats/, its predicted sets in predicted/)")
    ap.add_argument("--percent", type=float)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--floor", type=int, default=0, help="tallies: at least this many owners of every vertex type "
                    "(tally --owner-floor; 0: none)")
    ap.add_argument("--cells", default="all")
    ap.add_argument("--jobs", type=int, default=7)
    ap.add_argument("--set", default=None, help="picks: the predicted set compared (default the default tier's, "
                    "kvcost.cli.predict.default_set; predicted-tier2: tier 2)")
    a = ap.parse_args(argv)
    return {"tallies": cmd_tallies, "control": cmd_control, "errors": cmd_errors, "picks": cmd_picks,
            "no-locality": cmd_no_locality, "full-bound": cmd_full_bound, "check-provenance": cmd_check_provenance,
            "paper-picks": cmd_paper_picks}[a.step](a) or 0


if __name__ == "__main__":
    sys.exit(main())
