"""Build the dataset statistics (kvevaluator graph-stats over the dataset's record stream: the cardinality drivers and
the neutral content sizes; the contract inputs/contract/stats.schema.json) and save them at the per-SF location every
consumer reads: output/stats/<dataset>/<scale>/stats.json (output_tree.stats_dir), with the anchor tier (the anchor labels' own degrees and recency) beside it
in anchor_stats.json.

What the statistics declare comes from the dataset's statistics/build.json (the anchor labels and the date axis; LDBC:
Person, and Post + Comment by creationDate), so the build has no dataset's names in it. --records / --schema /
--out name another dataset's stream, schema and output file.

Shared infra, not a study script: the C++ predictor (`kvevaluator predict --stats --anchor-stats`) and
run/predict-accuracy.sh read them. One pass over
the record stream; consumers re-read the files, never re-scan.

  python3 -m kvevaluator.predict.build_stats --sf 0.1
  python3 -m kvevaluator.predict.build_stats --sf 0.1 --tier 2                # the full file only (the default adds stats-tier1.json)
  python3 -m kvevaluator.predict.build_stats --family job --tag sample1pct     # JOB / LinkBench
  python3 -m kvevaluator.predict.build_stats --records R --schema S --out O     # any record stream
"""
import argparse
import hashlib
import json
import os
import subprocess

from kvevaluator import datasets
from kvevaluator.output_tree import stats_dir
from kvevaluator.paths import CONTRACT_DIR, ROOT

_EXP = str(ROOT)


CONTRACT = CONTRACT_DIR / "stats.schema.json"   # the statistics contract; its x-contract is in the key


def stats_file(dataset, scale):
    """where a dataset's statistics live: output/stats/<dataset>/<scale>/stats.json"""
    return os.path.join(str(stats_dir(dataset, scale)), "stats.json")


def anchor_path(stats_file):
    """the anchor tier beside its statistics file"""
    return os.path.join(os.path.dirname(os.path.abspath(stats_file)), "anchor_stats.json")


def _git_sha():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=_EXP,
                              capture_output=True, text=True).stdout.strip() or "unknown"
    except OSError:
        return "unknown"


def declared(dataset):
    """what a dataset's statistics declare: datasets/<name>/statistics/build.json ({} when it has none)"""
    datasets.get(dataset)   # a name the registry knows
    p = ROOT / "python" / "kvevaluator" / "datasets" / dataset / "statistics" / "build.json"
    return json.loads(p.read_text()) if p.exists() else {}


def declared_args(dataset):
    """graph-stats' flags for what the dataset declares"""
    st = declared(dataset)
    args = ["--dataset", dataset]
    if st.get("anchor_labels"):
        args += ["--anchor-labels", ",".join(st["anchor_labels"])]
    if st.get("date_axis"):
        args += ["--date-axis", ",".join(st["date_axis"]["labels"]) + ":" + st["date_axis"]["prop"]]
    return args


def own_paths(workload, tag):
    """JOB's or LinkBench's record stream, schema and stats.json (both in output_tree.stats_dir)"""
    from kvevaluator.measure import grid_capture
    from kvevaluator import workloads
    from kvevaluator.measure.records import records_cache_path
    ds = workloads.get(workload).dataset
    return records_cache_path(ds, tag), str(ROOT / grid_capture.SCHEMAS[workload]), stats_file(ds, tag)


def build_key(dataset):
    """what decides a statistics file's content besides its record stream: the contract's version (x-contract), the
    binary's graph-stats version (kGraphStatsVersion, bumped with any change to its numbers) and the dataset's declared
    statistics (build.json's flags). build() writes it into each file's provenance; stale() compares it"""
    from kvevaluator.capture import binary
    v = json.loads(subprocess.run([binary.binary_path(), "graph-stats", "--version"], capture_output=True, text=True,
                                  check=True).stdout)
    return {"contract": json.loads(CONTRACT.read_text())["x-contract"],
            "graph_stats_version": v["graph_stats_version"], "declared": declared_args(dataset)}


def _moved(path, records, want):
    """why the statistics file at `path` is not of `records` and of `want` (build_key) now, or None"""
    with open(path) as f:
        g = json.load(f)
    from kvevaluator.measure import records_key
    have = g.get("records", {})
    key = records_key.key_path(records)
    stream = hashlib.sha256(open(key, "rb").read()).hexdigest() if os.path.exists(key) else None
    now = json.load(open(key)) if os.path.exists(key) else {}
    if have.get("content_sha256") and now.get(records_key.CONTENT):
        # by the stream's bytes: a re-dump of the same bytes keeps the statistics
        if have["content_sha256"] != now[records_key.CONTENT]:
            return (f"made from another record stream (content {have['content_sha256'][:12]}, now "
                    f"{now[records_key.CONTENT][:12]})")
    elif have.get("key_sha256") not in {stream} | {e["key_sha256"] for e in now.get(records_key.SAME_CONTENT, [])}:
        return f"made from another dump of the record stream (key {str(have.get('key_sha256'))[:12]}, now {str(stream)[:12]})"
    prov = g.get("provenance", {})
    why = [f"{k} {prov.get(k)!r} (now {v!r})" for k, v in want.items() if prov.get(k) != v]
    return f"made under another {', '.join(why)}" if why else None


def _adopt(path, records):
    """a statistics file kept through same_content_as (made before the stream's bytes were recorded): its records block
    gets the stream's content_sha256, so the C++ predict compares it with a tally by content (in the output dir only)"""
    from kvevaluator.measure import records_key
    key = records_key.key_path(records)
    now = json.load(open(key)) if os.path.exists(key) else {}
    with open(path) as f:
        g = json.load(f)
    if g.get("records", {}).get("content_sha256") or not now.get(records_key.CONTENT):
        return
    from kvevaluator.output_tree import Tree
    out = os.path.realpath(str(Tree().root))
    if not os.path.realpath(path).startswith(out + os.sep):   # never a file outside the output dir (a snapshot's)
        return
    g["records"]["content_sha256"] = now[records_key.CONTENT]
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(g, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def stale(stats, records, dataset):
    """why a statistics file, or the anchor tier beside it when there is one, is not what a build would make now:
    made from another dump of the record stream (another line count, or its freshness key moved; graph-stats names
    the stream, util/record_stream.h), or under another build_key (contract, graph-stats version, declared
    statistics). None when it is (or is missing: then it is built)"""
    if not os.path.exists(stats):
        return None
    want = build_key(dataset)
    why = _moved(stats, records, want)
    if why is None and os.path.exists(anchor_path(stats)):
        why = _moved(anchor_path(stats), records, want)
        why = why and f"its anchor tier: {why}"
    if why is None:
        for path in (stats, anchor_path(stats)):
            if os.path.exists(path):
                _adopt(path, records)
    return why


def kept(path, records, dataset, anchor=False):
    """--if-stale: True (and said) when the statistics at `path` (and, `anchor`, the anchor tier beside them) exist and
    are what a build would make now; False when they must be built, the reason said"""
    if not os.path.exists(path) or (anchor and not os.path.exists(anchor_path(path))):
        return False
    why = stale(path, records, dataset)
    print(f"[stats] {os.path.relpath(path)}: " + (f"stale, built again: {why}" if why else "of this record stream: kept"),
          flush=True)
    return why is None


def build(records, schema_path, dataset, scale, out):
    """graph-stats over a record stream: the statistics to `out` (with the scale and the provenance), and the anchor
    tier, when the dataset declares anchor labels, beside it"""
    from kvevaluator.capture import binary
    anchor = anchor_path(out)
    args = declared_args(dataset)
    with_anchor = "--anchor-labels" in args
    cmd = [binary.binary_path(), "graph-stats", "--schema", schema_path, "--records", records] + args
    if with_anchor:
        cmd += ["--anchor-out", anchor]
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    g = json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout)
    g["provenance"] = {**g["provenance"], **build_key(dataset), "sf": scale, "schema_hash": g["schema_hash"],
                       "git_sha": _git_sha()}
    with open(out, "w") as f:
        json.dump(g, f, indent=1, sort_keys=True)
        f.write("\n")
    if with_anchor:   # the anchor tier carries the same key, so one reuse check covers both files
        with open(anchor) as f:
            ag = json.load(f)
        ag["provenance"] = g["provenance"]
        with open(anchor, "w") as f:
            json.dump(ag, f, indent=1, sort_keys=True)
            f.write("\n")
    return g, (anchor if with_anchor else None)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sf", default=os.environ.get("SF", "0.1"), help="LDBC's scale (its record stream and schema)")
    ap.add_argument("--records", help="another dataset's record stream (with --schema, --out and --dataset)")
    ap.add_argument("--schema", help="that dataset's schema.json")
    ap.add_argument("--out", help="where its stats.json goes")
    ap.add_argument("--dataset", help="the dataset whose manifest declares what the statistics hold (with --records)")
    ap.add_argument("--family", dest="workload", choices=("job", "linkbench"), help="a dataset with its own data axis (with --tag)")
    ap.add_argument("--tag", help="its data tag (job: sample1pct, full; linkbench: n1m, ...)")
    ap.add_argument("--if-stale", action="store_true", help="build only when the file is missing or stale: made from "
                                                             "another dump of its record stream, or under another "
                                                             "contract, graph-stats version or declared statistics")
    from kvevaluator.predict import tier1
    ap.add_argument("--tier", type=int, choices=(1, 2), default=tier1.DEFAULT_TIER,
                    help="1 (the default, tier1.DEFAULT_TIER): the full statistics and stats-tier1.json beside them, tier "
                         "1 + the flat rows, derived again from the full file on every call (kvevaluator.predict.tier1; "
                         "the full file is built first only when missing or stale); 2: the full statistics only")
    a = ap.parse_args()
    if a.workload:
        if not a.tag:
            ap.error("--family needs --tag")
        from kvevaluator import workloads
        a.records, a.schema, a.out = own_paths(a.workload, a.tag)
        a.dataset = workloads.get(a.workload).dataset
        if not os.path.exists(a.records):
            raise SystemExit(f"[stats] no record stream {a.records}: build the {a.workload} {a.tag} stores first")
    if a.records or a.schema or a.out:
        if not (a.records and a.schema and a.out and a.dataset):
            ap.error("--records, --schema, --out and --dataset go together")
        scale = a.tag if a.workload else a.sf   # the data's scale: predict checks it against the store's manifest
        path, records, ds, schema, with_anchor = a.out, a.records, a.dataset, a.schema, False
    else:
        from kvevaluator.measure.records import records_file
        path = stats_file("ldbc", a.sf)
        records = records_file("ldbc", a.sf)
        ds = "ldbc"   # LDBC's own scale axis (--sf); another dataset comes with --records or --family
        scale, schema, with_anchor = a.sf, str(ROOT / datasets.get(ds).schema), True
    # tier 1 is derived from the full file: that one is built only when missing or stale (one pass over the records)
    if (a.if_stale or a.tier == 1) and kept(path, records, ds, anchor=with_anchor):
        g = None
    else:
        g, anchor = build(records, schema, ds, scale, path)
        print(f"[stats] -> {os.path.relpath(path)}{' + ' + os.path.relpath(anchor) if anchor else ''}  "
              f"{sum(g['type_counts'].values())} vertices, schema {g['schema_hash'][:12]}, git {g['provenance']['git_sha']}")
    if a.tier == 1:
        out = tier1.for_tier(path, 1)   # derived again every call (seconds), --if-stale or not
        print(f"[stats] -> {os.path.relpath(out)}  tier 1 + the flat rows, from {os.path.relpath(path)}")


if __name__ == "__main__":
    main()
