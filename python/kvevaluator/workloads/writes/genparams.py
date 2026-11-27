"""Generate the writes workload's param files from the LDBC CSVs -- deterministic (exact percentile picks over
sorted (count, id)), like the microbench generator.

  $PYTHON -m kvevaluator.workloads.writes.genparams --sf 0.1

Families:
  person_incident  personId | incident | stratum | one column per IU target (TARGETS)
      Anchors stratified by TOTAL incident degree (every edge type, both directions): delete cost tracks every
      edge that touches the vertex, and knows is a small minority of a Person's edges. The IU inserts' other
      endpoints are TYPICAL targets (_typical): the vertex whose list a median existing edge of the same edge
      CSV sits in, since an insert's cost includes its target list's size. The median, not the mean: where a
      write rewrites the whole list (dgraph's posting), the mean insert costs more.
  knows_edges      srcId | dstId | srcDegree | stratum
      Real knows edges in the direction the data stores them: a reversed pair finds nothing on the formats that
      search for the edge, while the formats that address it by key still pay for the tombstone.
  new_pairs        srcId | dstId | srcDegree | stratum
      Person pairs with no knows edge between them, so an edge insert is a true insert, not an overwrite.
"""
import argparse
import csv
import os
import sys
from collections import Counter

from kvevaluator.datasets.ldbc.loader import _root
from kvevaluator.datasets.ldbc.strata import STRATA5, pick_strata, read_pairs
from kvevaluator.workloads.writes.ldbc.queries import (IU1_COMPANIES, IU1_FRIENDS, IU1_TAGS, IU1_UNIS, IU4_TAGS,
                                                        IU6_TAGS, IU7_TAGS)
from kvevaluator.paths import ROOT
from kvevaluator.datasets import params_in

_EXP = str(ROOT)

# Every LDBC edge CSV that can touch a Person, and which column the person sits in. Used to build
# the TOTAL incident degree -- the axis a vertex delete actually scales on.
_PERSON_EDGE_FILES = [
    ("person_knows_person_0_0.csv", (0, 1)),
    ("person_likes_post_0_0.csv", (0,)),
    ("person_likes_comment_0_0.csv", (0,)),
    ("person_hasInterest_tag_0_0.csv", (0,)),
    ("person_studyAt_organisation_0_0.csv", (0,)),
    ("person_workAt_organisation_0_0.csv", (0,)),
    ("person_isLocatedIn_place_0_0.csv", (0,)),
    ("post_hasCreator_person_0_0.csv", (1,)),
    ("comment_hasCreator_person_0_0.csv", (1,)),
    ("forum_hasMember_person_0_0.csv", (1,)),
    ("forum_hasModerator_person_0_0.csv", (1,)),
]


def param_path(sf, family):
    return os.path.join(params_in("ldbc", sf, "writes"), f"{family}.txt")


def _write(sf, family, header, rows):
    path = param_path(sf, family)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("|".join(header) + "\n")
        for row in rows:
            f.write("|".join(str(x) for x in row) + "\n")
    print(f"[writes-params] {os.path.relpath(path, _EXP)}  ({len(rows)} rows)", file=sys.stderr)


# Every IU insert target, picked from exactly the edge CSV its insert extends (same source label, same edge
# type), so the target's list is the one the insert writes into. (column, edge CSV, the column the target sits
# in, how many). A column ending in "Id" is one target; any other is a numbered list (friend1..friend4).
TARGETS = [
    ("replyToId",        "comment_replyOf_post_0_0.csv",        1, 1),                   # IU7 replyOf
    ("likePostId",       "person_likes_post_0_0.csv",           1, 1),                   # IU2
    ("likeCommentId",    "person_likes_comment_0_0.csv",        1, 1),                   # IU3
    ("friend",           "person_knows_person_0_0.csv",         1, IU1_FRIENDS - 1),     # IU1 (me is the last)
    ("cityId",           "person_isLocatedIn_place_0_0.csv",    1, 1),                   # IU1
    ("interest",         "person_hasInterest_tag_0_0.csv",      1, IU1_TAGS),            # IU1
    ("university",       "person_studyAt_organisation_0_0.csv", 1, IU1_UNIS),            # IU1
    ("company",          "person_workAt_organisation_0_0.csv",  1, IU1_COMPANIES),       # IU1
    ("forumTag",         "forum_hasTag_tag_0_0.csv",            1, IU4_TAGS),            # IU4
    ("memberForumId",    "forum_hasMember_person_0_0.csv",      0, 1),                   # IU5: the member list
    ("postForumId",      "forum_containerOf_post_0_0.csv",      0, 1),                   # IU6: the post list
    ("postCountryId",    "post_isLocatedIn_place_0_0.csv",      1, 1),                   # IU6
    ("postTag",          "post_hasTag_tag_0_0.csv",             1, IU6_TAGS),            # IU6
    ("commentCountryId", "comment_isLocatedIn_place_0_0.csv",   1, 1),                   # IU7
    ("commentTag",       "comment_hasTag_tag_0_0.csv",          1, IU7_TAGS),            # IU7
]
# Per-row skips, so an anchor never duplicates an edge it already has: the column its own edges sit in.
OWNED_BY_ANCHOR = {"likePostId": 0, "likeCommentId": 0, "memberForumId": 1}


def target_columns(name, n):
    return [name] if name.endswith("Id") else [f"{name}{i}" for i in range(1, n + 1)]


def _list_sizes(sf, fname, col, owner_col=None, owners=()):
    """{vertex id: its list size} -- how many rows of this edge CSV point at it in column `col`.
    With `owners`: also {owner: {ids it already points at}} for rows whose `owner_col` is one of them
    (an anchor's own likes / memberships, so its insert is not a duplicate edge)."""
    sizes, owned = Counter(), {o: set() for o in owners}
    with open(os.path.join(_root(sf), "dynamic", fname)) as f:
        next(f)
        for line in f:
            cols = line.split("|", 2)
            sizes[int(cols[col])] += 1
            if owner_col is not None and cols[owner_col] in owned:
                owned[cols[owner_col]].add(int(cols[col]))
    return sizes, owned


def _typical(sizes, n=1, skip=()):
    """The n vertices whose lists a median existing edge sits in: rank by (list size, id), walk the running
    total of list sizes to half of all edges, take that vertex and the next ones up. So the target is picked
    the way the data's own edges point (popular vertices get more inserts) -- neither the empty list nor the
    largest. A vertex in `skip` (the insert would duplicate an edge it has) is passed over."""
    ranked = sorted((c, v) for v, c in sizes.items() if c > 0)
    half, run, i = sum(c for c, _ in ranked) / 2, 0, 0
    for i, (c, _) in enumerate(ranked):
        run += c
        if run >= half:
            break
    out = [v for _, v in ranked[i:] if v not in skip][:n]
    out += [v for _, v in reversed(ranked[:i]) if v not in skip][:n - len(out)]
    if len(out) != n:
        raise SystemExit(f"only {len(out)} of {n} targets available -- the edge CSV looks truncated")
    return out


def _incident_degree(sf):
    """{personId: how many edges touch this person}, across every edge type and direction."""
    deg = Counter()
    for fname, cols in _PERSON_EDGE_FILES:
        path = os.path.join(_root(sf), "dynamic", fname)
        if not os.path.exists(path):
            print(f"[writes-params] missing {fname} -- skipped", file=sys.stderr)
            continue
        with open(path, newline="") as f:
            rd = csv.reader(f, delimiter="|")
            next(rd)
            for r in rd:
                for c in cols:
                    deg[r[c]] += 1
    return deg


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sf", default=os.environ.get("SF", "0.1"))
    a = ap.parse_args(argv)
    sf = a.sf

    incident = _incident_degree(sf)
    anchors = pick_strata(incident, STRATA5)
    pids = [pid for pid, _, _ in anchors]
    # Each target is the same typical vertex in every row (the workload prices the write shape, not a
    # target distribution), except where the anchor already has that edge (OWNED_BY_ANCHOR) or is the target.
    picked = {pid: [] for pid in pids}
    for name, fname, col, n in TARGETS:
        owner_col = OWNED_BY_ANCHOR.get(name)
        sizes, owned = _list_sizes(sf, fname, col, owner_col, pids if owner_col is not None else ())
        base = _typical(sizes, n)
        for pid in pids:
            skip = owned.get(pid, set()) | ({int(pid)} if name == "friend" else set())
            picked[pid] += base if not skip & set(base) else _typical(sizes, n, skip)   # same answer, one sort
    rows = [(pid, inc, st, *picked[pid]) for pid, inc, st in anchors]
    _write(sf, "person_incident", ["personId", "incident", "stratum",
                                   *[c for name, _, _, n in TARGETS for c in target_columns(name, n)]], rows)

    knows = read_pairs(sf, "person_knows_person_0_0.csv")
    out_deg = Counter(s for s, _ in knows)               # OUT degree: the stored direction
    out_nbrs = {}
    for s, d in knows:
        out_nbrs.setdefault(s, []).append(d)
    both = {}
    for s, d in knows:                                   # undirected, for the non-adjacency test
        both.setdefault(s, set()).add(d)
        both.setdefault(d, set()).add(s)

    edge_rows, new_rows = [], []
    persons = sorted(out_deg, key=int)
    for src, d_cnt, stratum in pick_strata(out_deg, STRATA5):
        edge_rows.append((src, min(out_nbrs[src], key=int), d_cnt, stratum))
        neg = next((p for p in persons if p != src and p not in both.get(src, ())), None)
        if neg is None:
            raise SystemExit(f"no non-neighbour exists for src {src} (out-degree {d_cnt}); "
                             f"the knows graph is degenerate at this SF")
        new_rows.append((src, neg, d_cnt, stratum))
    _write(sf, "knows_edges", ["srcId", "dstId", "srcDegree", "stratum"], edge_rows)
    _write(sf, "new_pairs", ["srcId", "dstId", "srcDegree", "stratum"], new_rows)


if __name__ == "__main__":
    main()
