"""The anchor statistics of a dataset with its own data axis (JOB, LinkBench): the vids the requests' params bind in
the plans, then `kvevaluator graph-stats --anchor-vids` over the record stream (each vid's own degrees and recency).
LDBC's anchor statistics are build_stats' (every Person); these are made per run, from that run's params.

  python3 -m kvevaluator.predict.anchor_vids <workload> <tag> <queries a,b> <nparam> [<param set>]   # the vids, sorted
"""
import json
import os
import re
import subprocess
import sys

VID = re.compile(r"^[0-9a-f]{16}$")


def _walk(x):
    if isinstance(x, str):
        return {x} if VID.match(x) else set()
    if isinstance(x, (list, tuple)):
        return set().union(*map(_walk, x)) if x else set()
    if isinstance(x, dict):
        return set().union(*map(_walk, x.values())) if x else set()
    return set()


def vids(workload, sf, queries, n, param_set=None):
    """[vid]: every vid the first n params of each query bind in any of its bodies' steps, sorted"""
    from kvevaluator.measure import workload_plans
    from kvevaluator.plans.ir.interp import bind_ir
    out = set()
    for q in queries:
        ir = workload_plans.ir_for(workload, q)
        bodies = [{**{k: v for k, v in ir.items() if k not in ("plans", "variants")}, **b} for b in ir["plans"].values()] \
            if "plans" in ir else [ir]
        for args in workload_plans.query_params(workload, sf, q, n, param_set):
            for body in bodies:
                for s in bind_ir(body, args):
                    out |= _walk(s.args)
    return sorted(out)


def build(vid_list, records, schema, out, dataset):
    """the anchor statistics of these vids to `out` (graph-stats over the record stream; its stdout is not kept), with
    the vids beside it (<out>.vids.txt); kept when that list, the record stream and the dataset's build_key (contract,
    graph-stats version, declared statistics) are the ones it was made from. Returns (out, what was done)"""
    from kvevaluator.capture import binary
    from kvevaluator.predict import build_stats
    listed = out + ".vids.txt"
    text = "\n".join(vid_list) + "\n"
    want = build_stats.build_key(dataset)
    if os.path.exists(out) and os.path.exists(listed) and open(listed).read() == text \
            and build_stats._moved(out, records, want) is None:
        return out, "kept"
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(listed, "w") as f:
        f.write(text)
    subprocess.run([binary.binary_path(), "graph-stats", "--schema", schema, "--anchor-vids", listed, "--anchor-out", out,
                    "--records", records], stdout=subprocess.DEVNULL, check=True)
    with open(out) as f:
        ag = json.load(f)                          # refuse a torn file now, not in predict
    ag["provenance"] = {**ag.get("provenance", {}), **want}     # its key, as build_stats.build writes it
    tmp = f"{out}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(ag, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, out)
    return out, f"built ({len(vid_list)} vids)"


def main(argv=None):
    w, sf, qs, n, *rest = argv if argv is not None else sys.argv[1:]
    print("\n".join(vids(w, sf, qs.split(","), int(n), (rest[0] if rest else "") or None)))


if __name__ == "__main__":
    main()
