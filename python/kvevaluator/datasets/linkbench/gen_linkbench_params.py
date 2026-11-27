#!/usr/bin/env python3
"""LinkBench params FROM LinkBench's own request stream: requests.jsonl (the seeded REQUEST phase, logged by
experiments/linkbench/FileDumpGraphStore) -> one params file per (sample, query) + the empirical op mix.

Each request becomes a call of one of our queries (workloads/linkbench/queries):
  get_link_list -> getlinklist_<t>   count_links -> countlink_<t>   get_link -> getlink_<t>   get_node -> getnode
  upsert_link   -> addlink_<t> if (id1, link_type, id2) is NOT in the loaded graph, else updatelink_<t>
                   (LinkBench's addLink and updateLink both upsert; what the write costs depends on existence)
  delete_link   -> deletelink_<t> (LinkBench hides: expunge=false; a missing link is still looked up)
  add_node -> addnode   update_node -> updatenode   delete_node -> deletenode
where <t> = link1 / link2 (loader.LINK_TYPES). Every write is measured against the LOADED graph, so existence is
checked against links.jsonl.

Samples: quick = each query's first k requests, quick-next = its next k (k = min(--per-query, half its requests)):
two independent samples for the heavy-tail check. `log` = every logged request: the exact workload total, the one to
use when quick and quick-next disagree (a hub that a few requests hit is caught by one sample and not the other).
request_log.jsonl is the whole log in order, {seq, query, row, args, ran_as, note}: the file a mixed read + write
run replays. The params are not tracked; each run checks the files it wrote against the scale's tracked
params.sha256.txt (rc 1 on a difference), and --track rewrites that list and the tracked mix.json.

    python3 python/kvevaluator/datasets/linkbench/gen_linkbench_params.py --scale n1m [--per-query 2000] [--track]
"""
import argparse
import base64
import collections
import hashlib
import json
import os
import shutil
import sys
from kvevaluator.paths import ROOT
from kvevaluator.datasets import data_in
from kvevaluator.datasets import frequencies_in, params_in

EXP = str(ROOT)
from kvevaluator.datasets.linkbench.loader import LINK_TYPES
from kvevaluator.datasets.linkbench.params import SAMPLES, sequence_meaning
from kvevaluator import jsonbytes


def data_bytes(b64):
    """a LinkBench byte blob as the loader stores it (its raw bytes; written to JSON as {"$bytes": base64})"""
    return base64.b64decode(b64)


def link_props(r):
    return {"visibility": r["visibility"], "version": r["version"], "time": r["time"], "data": data_bytes(r["data"])}


def node_props(r):
    return {"ntype": r["type"], "version": r["version"], "time": r["time"], "data": data_bytes(r["data"])}


def query_of(r, exists):
    """(query name, args) for one logged request; None for a call we do not model"""
    op = r["op"]
    if op == "get_node":
        return "getnode", {"node": r["id"]}
    if op == "add_node":
        return "addnode", {"node": r["id"], **node_props(r)}
    if op == "update_node":
        return "updatenode", {"node": r["id"], "version": r["version"], "time": r["time"], "data": data_bytes(r["data"])}
    if op == "delete_node":
        return "deletenode", {"node": r["id"]}
    t = LINK_TYPES[r["link_type"]]
    if op == "get_link_list":
        return f"getlinklist_{t}", {"node": r["id1"]}
    if op == "count_links":
        return f"countlink_{t}", {"node": r["id1"]}
    if op == "get_link":
        if not r["id2s"]:                   # none at n1m; an empty list would answer nothing: refuse, don't invent one
            raise SystemExit(f"gen_linkbench_params: get_link of {r['id1']} lists no id2s")
        return f"getlink_{t}", {"node": r["id1"], "id2s": list(r["id2s"])}
    key = (r["id1"], r["link_type"], r["id2"])
    if op == "upsert_link":
        name = f"updatelink_{t}" if key in exists else f"addlink_{t}"
        return name, {"src": r["id1"], "dst": r["id2"], **link_props(r)}
    if op == "delete_link":   # a missing link is still looked up (the op finds nothing and writes nothing)
        return f"deletelink_{t}", {"src": r["id1"], "dst": r["id2"]}
    raise SystemExit(f"gen_linkbench_params: unknown request op {op!r}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scale", required=True)
    ap.add_argument("--data", default=None, help="the JSONL dir (default <data dir>/datasets/linkbench/<scale>/data)")
    ap.add_argument("--per-query", type=int, default=2000, help="requests per query in EACH sample (quick, quick-next)")
    ap.add_argument("--out", default=None, help="the params dir (default <data dir>/datasets/linkbench/<scale>/params)")
    ap.add_argument("--track", action="store_true", help="write the tracked list (inputs/params/linkbench/<scale>/linkbench/params.sha256.txt) "
                    "and mix.json from this run, instead of checking against them")
    ap.add_argument("--check-tracked", action="store_true", help="no data: check that the scale's tracked mix.json is "
                    "the one its params.sha256.txt lists, then exit")
    a = ap.parse_args()
    if a.check_tracked:
        return check_tracked(a.scale)
    data = a.data or str(data_in("linkbench", a.scale) / "data")
    final = a.out or str(data_in("linkbench", a.scale) / "params")
    # written into a fresh dir and moved in place only once checked, so a failed check never leaves params a reader takes
    out = final.rstrip("/") + ".partial"
    if os.path.exists(out):
        shutil.rmtree(out)
    with open(os.path.join(data, "requests.jsonl"), "rb") as f:
        raw = f.read()
    reqs = [json.loads(line) for line in raw.splitlines()]
    wanted = {(r["id1"], r["link_type"], r["id2"]) for r in reqs if r["op"] in ("upsert_link", "delete_link")}
    exists = set()
    with open(os.path.join(data, "links.jsonl")) as f:           # only the links a write asks about
        for line in f:
            r = json.loads(line)
            key = (r["id1"], r["link_type"], r["id2"])
            if key in wanted:
                exists.add(key)
    counts = collections.Counter()
    by_query = collections.defaultdict(list)
    log = []                                     # (query, row in log/<query>.jsonl, args), in log order
    for r in reqs:
        name, args = query_of(r, exists)
        counts[name] += 1
        if args is not None:
            by_query[name].append(args)
            log.append((name, len(by_query[name]), args))
    n = a.per_query
    for sample in SAMPLES:
        os.makedirs(os.path.join(out, sample), exist_ok=True)
    for q, rows in sorted(by_query.items()):
        k = min(n, len(rows) // 2)                 # a rare query splits its requests evenly: quick and quick-next never empty
        for sample, part in (("quick", rows[:k]), ("quick-next", rows[k:2 * k]), ("log", rows)):
            with open(os.path.join(out, sample, f"{q}.jsonl"), "w") as f:
                for args in part:
                    f.write(jsonbytes.dumps(args, separators=(",", ":")) + "\n")
    with open(os.path.join(out, "request_log.jsonl"), "w") as f:
        for seq, ((q, row, args), (ran, note)) in enumerate(zip(log, sequence_meaning([(q, a) for q, _r, a in log]))):
            f.write(jsonbytes.dumps({"seq": seq, "query": q, "row": row, "args": args, "ran_as": ran, "note": note},
                                    separators=(",", ":")) + "\n")
    total = sum(counts.values())
    with open(os.path.join(out, "mix.json"), "w") as f:
        # the data by its content, not its path: the file is the same on every machine that has the same data
        json.dump({"requests": total, "per_query_sample": n, "requests_sha256": hashlib.sha256(raw).hexdigest(),
                   "share": {q: c / total for q, c in sorted(counts.items())}, "count": dict(sorted(counts.items()))},
                  f, indent=1)
    short = {q: len(rows) // 2 for q, rows in by_query.items() if len(rows) < 2 * n}
    print(f"{total:,} requests -> {len(by_query)} queries -> {final}"
          + (f"; samples smaller than {n} (per sample): {short}" if short else ""))
    for q, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {q:22s} {c:>8,}  {c / total:6.2%}")
    track_or_check(out, a.scale, a.track)
    if os.path.exists(final):
        shutil.rmtree(final)
    os.rename(out, final)


def written_files(out):
    """every file this run wrote below its fresh params dir, as sorted paths relative to it"""
    names = ["mix.json", "request_log.jsonl"]
    for sample in SAMPLES:
        names += [f"{sample}/{f}" for f in os.listdir(os.path.join(out, sample)) if f.endswith(".jsonl")]
    return sorted(names)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def track_or_check(out, scale, track):
    """with --track, write the scale's tracked list and mix.json from `out`; else check `out` against them"""
    tracked = params_in("linkbench", scale, "linkbench")
    listing, mix = tracked / "params.sha256.txt", frequencies_in("linkbench", scale) / "mix.json"
    got = {n: sha256_of(os.path.join(out, n)) for n in written_files(out)}
    if track:
        os.makedirs(tracked, exist_ok=True)
        os.makedirs(mix.parent, exist_ok=True)
        with open(listing, "w") as f:
            f.writelines(f"{h}  {n}\n" for n, h in sorted(got.items()))
        with open(os.path.join(out, "mix.json"), "rb") as src, open(mix, "wb") as dst:
            dst.write(src.read())
        print(f"tracked: {len(got)} files -> {os.path.relpath(listing, EXP)}, and {os.path.relpath(mix, EXP)}")
        return
    if not os.path.exists(listing):
        print(f"no tracked list {os.path.relpath(listing, EXP)}: nothing to check (--track writes one)")
        return
    with open(listing) as f:
        want = {n: h for h, n in (line.split() for line in f if line.strip())}
    bad = sorted(n for n in set(want) | set(got) if want.get(n) != got.get(n))
    if os.path.exists(mix) and sha256_of(mix) != got.get("mix.json"):
        bad.append(f"{os.path.relpath(mix, EXP)} (the tracked copy)")
    if bad:
        raise SystemExit(f"gen_linkbench_params: {len(bad)} of {len(want)} files differ from {os.path.relpath(listing, EXP)}, "
                         f"first: {bad[:3]}; left in {out}, the params dir unchanged. The data is not the measured run's "
                         f"(check <data>/manifest.json), or the params' format changed (then --track, deliberately)")
    print(f"params: {len(got)} files equal to {os.path.relpath(listing, EXP)}")


def check_tracked(scale):
    """the scale's tracked mix.json is the one its params.sha256.txt lists (a store-free test); rc 1 if not"""
    tracked, mix = params_in("linkbench", scale, "linkbench"), frequencies_in("linkbench", scale) / "mix.json"
    with open(tracked / "params.sha256.txt") as f:
        want = {n: h for h, n in (line.split() for line in f if line.strip())}
    ok = want.get("mix.json") == sha256_of(mix)
    print(f"tracked {scale}: mix.json {'is' if ok else 'is NOT'} the one params.sha256.txt lists ({len(want)} files listed)")
    return 0 if ok and len(want) > 1 else 1


if __name__ == "__main__":
    sys.exit(main())
