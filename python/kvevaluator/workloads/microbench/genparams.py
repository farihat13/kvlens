"""Generate the microbench param files from the LDBC CSVs -- deterministic (exact percentile picks over sorted
(count, id)), so a re-run on the same data reproduces the files byte for byte.

  $PYTHON -m kvevaluator.workloads.microbench.genparams --sf 0.1

Works for any SF whose LDBC CSVs are downloaded (`just fetch-dataset ldbc=<sf>`); params.param_rows regenerates a missing
file on demand. The degree/term-size strata are the microbench's power-law axis: anchors at p10/p50/p90/p99/max of
each fan-out distribution, so the cost-vs-degree curve per format is swept, not sampled at one point.
"""
import argparse
import csv
import os
import statistics
import sys
from collections import Counter

from kvevaluator.datasets.ldbc.loader import _root
from kvevaluator.datasets.ldbc.strata import STRATA3, STRATA5, pick_strata, read_pairs
from kvevaluator.datasets import fetch_hint, params_in
from kvevaluator.workloads.microbench.params import param_path

DAY = 86400000
def _write(sf, family, header, rows):
    path = param_path(sf, family)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("|".join(header) + "\n")
        for r in rows:
            f.write("|".join(str(x) for x in r) + "\n")
    print(f"  {family:22s} {len(rows):>3d} rows -> {os.path.relpath(path)}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sf", default=os.environ.get("SF", "0.1"))
    a = ap.parse_args(argv)
    sf = a.sf
    if not os.path.isdir(_root(sf)):
        raise SystemExit(f"no LDBC CSVs for sf{sf} at {_root(sf)}\n"
                         f"  download them first:  {fetch_hint('ldbc', sf)}")
    print(f"[genparams] sf{sf} -> {os.path.relpath(params_in('ldbc', sf, 'microbench'))}")

    # --- knows degree (undirected: both endpoints count) --------------------
    knows = read_pairs(sf, "person_knows_person_0_0.csv")
    deg = Counter()
    for s, d in knows:
        deg[s] += 1
        deg[d] += 1
    _write(sf, "person_knows", ["personId", "degree", "stratum"],
           pick_strata(deg, STRATA5))

    # --- messages per person (posts + comments, hasCreator dst) -------------
    msgs = Counter()
    for fname in ("post_hasCreator_person_0_0.csv", "comment_hasCreator_person_0_0.csv"):
        for _, person in read_pairs(sf, fname):
            msgs[person] += 1
    _write(sf, "person_msgs", ["personId", "messages", "stratum"],
           pick_strata(msgs, STRATA5))

    # --- posts by content length (point_post): the row a post read pays is mostly its content --
    lengths = {}
    with open(os.path.join(_root(sf), "dynamic", "post_0_0.csv"), newline="") as f:
        rd = csv.reader(f, delimiter="|")
        head = next(rd)
        idc, lc = head.index("id"), head.index("length")
        for r in rd:
            lengths[r[idc]] = int(r[lc])
    _write(sf, "post_lengths", ["postId", "length", "stratum"], pick_strata(lengths, STRATA5))

    # --- members per forum ---------------------------------------------------
    members = Counter(s for s, _ in read_pairs(sf, "forum_hasMember_person_0_0.csv"))
    _write(sf, "forum_members", ["forumId", "members", "stratum"],
           pick_strata(members, STRATA5))

    # --- messages per tag -----------------------------------------------------
    tmsgs = Counter()
    for fname in ("post_hasTag_tag_0_0.csv", "comment_hasTag_tag_0_0.csv"):
        for _, tag in read_pairs(sf, fname):
            tmsgs[tag] += 1
    _write(sf, "tag_msgs", ["tagId", "messages", "stratum"],
           pick_strata(tmsgs, STRATA5))

    # --- edge-probe pairs: positive + negative per stratified src ------------
    nbrs = {}
    for s, d in knows:
        nbrs.setdefault(s, set()).add(d)
        nbrs.setdefault(d, set()).add(s)
    persons = sorted(deg, key=int)               # persons with >=1 knows edge
    pair_rows = []
    for src, d_cnt, stratum in pick_strata(deg, STRATA3):
        pos = min(nbrs[src], key=int)
        neg = next((p for p in persons if p != src and p not in nbrs[src]), None)
        if neg is None:                       # src knows every knows-participant --
            raise SystemExit(                 # degenerate graph, name it instead of
                f"no non-neighbor exists for src {src} (degree {d_cnt}); "  # StopIteration
                f"the knows graph is degenerate at this SF")
        pair_rows.append((src, pos, "yes", d_cnt, stratum))
        pair_rows.append((src, neg, "no", d_cnt, stratum))
    _write(sf, "knows_pairs", ["srcId", "dstId", "expected", "srcDegree", "stratum"],
           pair_rows)

    # --- firstName frequency strata (entry_eq term size) ---------------------
    first = Counter()
    dates = []
    with open(os.path.join(_root(sf), "dynamic", "person_0_0.csv"), newline="") as f:
        for r in csv.DictReader(f, delimiter="|"):
            first[r["firstName"]] += 1
            dates.append(int(r["creationDate"]))
    ranked = sorted(first.items(), key=lambda kv: (kv[1], kv[0]))
    fn_rows, seen = [], set()
    for name, q in STRATA3:
        val, cnt = ranked[round(q * (len(ranked) - 1))]
        if val not in seen:
            seen.add(val)
            fn_rows.append((val, cnt, name))
    _write(sf, "firstnames", ["firstName", "freq", "stratum"], fn_rows)

    # --- person creationDate windows (entry_range) ---------------------------
    start = statistics.median_low(dates)         # a real ms value, NOT day-aligned,
    _write(sf, "person_date_windows",           # so the day-boundary trim is exercised
           ["startDate", "days", "label"],
           [(start, 1, "1d"), (start, 30, "30d"), (start, 365, "365d")])

    # --- message maxDate bounds (topk) ---------------------------------------
    mdates = []
    for fname in ("post_0_0.csv", "comment_0_0.csv"):
        with open(os.path.join(_root(sf), "dynamic", fname), newline="") as f:
            for r in csv.DictReader(f, delimiter="|"):
                mdates.append(int(r["creationDate"]))
    mdates.sort()
    med = mdates[len(mdates) // 2]
    _write(sf, "msg_maxdates", ["maxDate", "kind"],
           [(mdates[-1] + 1, "all"),                       # newest overall
            (med, "median"),                               # mid-dataset bound
            (((med // DAY) + 1) * DAY, "dayaligned")])     # exactly a day boundary:
    # the day-aligned bound makes dgraph's boundary token empty of >= hi values --
    # the degenerate single-bucket case, which a microbench should cover.

    # --- reply-chain depths (chain / chain3) ----------------------------------
    parent = {}
    for c, p in read_pairs(sf, "comment_replyOf_post_0_0.csv"):
        parent[("c", c)] = ("p", p)
    for c, p in read_pairs(sf, "comment_replyOf_comment_0_0.csv"):
        parent[("c", c)] = ("c", p)
    posts = sorted({p for (_, p) in
                    (v for v in parent.values() if v[0] == "p")}, key=int)
    depth_cache = {}

    def depth(node):
        seen_path = []
        cur = node
        while cur not in depth_cache:
            if cur[0] == "p":
                depth_cache[cur] = 0
                break
            seen_path.append(cur)
            cur = parent[cur]
        for i, n in enumerate(reversed(seen_path)):
            depth_cache[n] = depth_cache[parent[n]] + 1
        return depth_cache[node]

    by_depth = {}
    for node in parent:                          # every comment that replies
        d_ = depth(node)
        cur = by_depth.get(d_)
        if cur is None or int(node[1]) < int(cur):
            by_depth[d_] = node[1]
    chain_rows = [(posts[0], 0)] if posts else []
    for d_ in range(1, 7):
        if d_ in by_depth:
            chain_rows.append((by_depth[d_], d_))
        else:
            print(f"  ! no comment at reply depth {d_} in sf{sf} -- row skipped",
                  file=sys.stderr)
    _write(sf, "reply_chains", ["messageId", "depth"], chain_rows)

    # tagclass_subtrees: TagClass anchors stratified by SUBTREE size (the class itself included),
    # so the closure shape covers a leaf, mid-size subtrees and the whole hierarchy.
    children = {}
    for child, parent_ in read_pairs(sf, "tagclass_isSubclassOf_tagclass_0_0.csv", "static"):
        children.setdefault(parent_, []).append(child)
    with open(os.path.join(_root(sf), "static", "tagclass_0_0.csv"), newline="") as f:
        rd = csv.reader(f, delimiter="|")
        next(rd)
        classes = [r[0] for r in rd]

    def subtree(tc):
        return 1 + sum(subtree(c) for c in children.get(tc, []))
    sizes = {tc: subtree(tc) for tc in classes}
    _write(sf, "tagclass_subtrees", ["tagClassId", "subtree", "stratum"],
           pick_strata(sizes, STRATA5))

    print("[genparams] done")


if __name__ == "__main__":
    main()
