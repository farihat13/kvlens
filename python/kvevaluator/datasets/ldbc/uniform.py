"""Vertex ids drawn uniformly at random from LDBC's CSVs (seeded, without replacement), and the few per-message facts a
short-read param row states: the draw behind the uniform param set (workloads/interactive/gen_uniform_params.py)."""
import csv
import os
import random

from kvevaluator.datasets.ldbc.loader import _root

PERSONS = ("person",)
MESSAGES = ("post", "comment")      # a message is a post or a comment; their ids do not overlap (checked in ids())


def _rows(sf, fname):
    """the rows of a dynamic CSV, header skipped (positional: headers repeat names)"""
    with open(os.path.join(_root(sf), "dynamic", fname), newline="") as f:
        rd = csv.reader(f, delimiter="|")
        next(rd)
        yield from rd


def ids(sf, entities):
    """{id: entity} over every vertex of the entities (e.g. MESSAGES); refuses an id that two entities share"""
    out = {}
    for e in entities:
        for r in _rows(sf, f"{e}_0_0.csv"):
            i = int(r[0])
            if out.setdefault(i, e) != e:
                raise SystemExit(f"sf{sf}: id {i} is both a {out[i]} and a {e}: a uniform draw over {entities} is undefined")
    if not out:
        raise SystemExit(f"sf{sf}: no {'/'.join(entities)} rows: the LDBC CSVs look missing or truncated")
    return out


def draw(population, n, seed):
    """n ids drawn uniformly without replacement from `population` (a collection of ints), the same for the same
    (population, n, seed): the population is sorted first, so file order never matters"""
    pop = sorted(population)
    if n > len(pop):
        raise SystemExit(f"a draw of {n} from a population of {len(pop)}")
    return random.Random(seed).sample(pop, n)


def message_facts(sf, messages):
    """{message id: (kind, depth, replies)} for the given {id: "post" | "comment"}: depth 0 for a post, else the
    number of reply hops to its post (1 for a comment on a post); replies = the comments replying directly to it (the
    columns of cost_eval's short-read rows)"""
    parent = {int(c): int(p) for c, p in _rows(sf, "comment_replyOf_comment_0_0.csv")}
    replies = dict.fromkeys(messages, 0)
    for fname in ("comment_replyOf_comment_0_0.csv", "comment_replyOf_post_0_0.csv"):
        for _, p in _rows(sf, fname):
            if int(p) in replies:
                replies[int(p)] += 1
    out = {}
    for m, kind in messages.items():
        d, cur = (0 if kind == "post" else 1), m
        while cur in parent:            # a comment on a comment: one more hop up
            cur, d = parent[cur], d + 1
        out[m] = (kind, d, replies[m])
    return out
