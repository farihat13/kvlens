#!/usr/bin/env python3
"""Predict mode's cardinality rules, one fixture per switch (cpp/kvevaluator/src/predict/cardinality.h, Rules); no
store.
Each fixture is a tiny graph over the LDBC schema, written here as a record stream, and a hand-written plan. The test
builds the statistics (build_stats) and one tally from the stream, runs `kvevaluator predict --plans` into a capture,
and checks the rows of one step (the anchor level's: the statistics plus the anchor's own):
  - with every rule on, the rows equal the true count, counted by this test over the fixture's own graph (never taken
    from predict's output). Each graph is shaped so the rule's model is exact there;
  - with the fixture's own switch off (--rules-off), the rows differ from the true count: the plant is caught.
Two more checks hold the models' formulas where the true count is not the point: each asserts the model's value,
derived by hand in its docstring from the graph's shape (a wrong exponent or branch moves it).
Every switch the binary names (predict --rule-names) needs a fixture here, and every fixture names a switch.
    python3 tests/unit/check_predict_rules.py
"""
import atexit
import csv
import json
import os
import subprocess
import sys
import tempfile

from kvevaluator.paths import ROOT, cpp_bin
from kvevaluator.datasets import schema_path
from kvevaluator.predict import build_stats

SCHEMA = schema_path("ldbc")
SPEC = str(ROOT / "inputs" / "specs" / "grid" / "props_split_adj_split.json")   # any layout: rows do not depend on it
DAY = 86400000
D0 = 14610   # 2010-01-01, in days since the epoch
T0 = D0 * DAY


def vid(kind, i):
    """a 16-hex-digit vid: persons kind 0, messages kind 1"""
    return f"{kind:01x}{i:015x}"


def person(i):
    return ["v", vid(0, i), "Person", {"id": i, "creationDate": T0}]


def post(i, day=0, **props):
    return ["v", vid(1, i), "Post", {"id": i, "creationDate": (D0 + day) * DAY + 1000, **props}]


def comment(i):
    return ["v", vid(1, i), "Comment", {"id": i, "creationDate": T0 + 1000, "length": 3}]


def knows(a, b):
    return ["e", "knows", vid(0, a), vid(0, b), {"creationDate": T0}, "Person", "Person"]


def has_creator(m, p, label):
    return ["e", "hasCreator", vid(1, m), vid(0, p), {}, label, "Person"]


def scan(name, label):
    return {"name": name, "op": "read_vertices_by_label", "label": label}


def hop(name, srcs, etype="knows", d="out"):
    return {"name": name, "op": "read_neighbors", "etype": etype, "dir": d, "srcs": {"ref": srcs}}


def through_props(name, src, label="Person"):
    """predict writes rows for the steps that read the store only: a step that does not (a dedup, a filter) is read
    through a props read of its rows, with props_distinct off so that read passes every row on"""
    return {"name": name, "op": "read_vertex_props", "vids": {"ref": src}, "label": label, "props": ["id"]}


THROUGH = ["props_distinct"]   # the base rules off for a fixture read through_props
# off for a check of the distinct-vertex models: the cap at the edge side's vertices would give their true count alone
UNCAPPED = ["dedup_domain_cap"]


def out_lists(edges):
    out = {}
    for a, b in edges:
        out.setdefault(a, []).append(b)
    return out


def hub_graph():
    """persons 0..59 each know one of three hubs (60, 61, 62), 20 each: 60 rows, 3 distinct targets. The distinct
    model is exact here: three targets of 20 in-edges each, 3 (1 - (2/3)^60) rounds to 3"""
    edges = [(i, 60 + i % 3) for i in range(60)]
    return [person(i) for i in range(63)] + [knows(a, b) for a, b in edges], edges


def fx_frontier_inclusion():
    """a hop from the far ends of a hop: each of persons 20..39 has exactly one in-edge, 0..19 none. The first hop's
    rows are those 20 persons, once each, so the second hop's true rows are their summed out-degree, which the via
    model gives exactly (every vertex with a via edge is in: their mean target degree). Persons 0..4 source one edge
    each, persons 20..24 three each: the frontier's mean out-degree (0.75) is not the population's (0.5)"""
    edges = [(t - 20, t) for t in range(20, 25)] + [(20 + (t - 25) % 5, t) for t in range(25, 40)]
    lines = [person(i) for i in range(40)] + [knows(a, b) for a, b in edges]
    out = out_lists(edges)
    frontier = [b for a, b in edges]   # the first hop's rows, from every person
    true = sum(len(out.get(v, [])) for v in frontier)
    return lines, [scan("all", "Person"), hop("h1", "all"), hop("h2", "h1")], "h2", true, []


def fx_dedup_by_degree():
    lines, edges = hub_graph()
    steps = [scan("all", "Person"), hop("h1", "all"), {"name": "d", "op": "dedup", "items": {"ref": "h1"}},
             through_props("p", "d")]
    return lines, steps, "p", len({b for a, b in edges}), THROUGH + UNCAPPED


def fx_filter_keeps_via():
    """a filter that keeps every row (dst = dst; its own estimate is the rough half), then a dedup: the dedup still
    knows the rows came through knows"""
    lines, edges = hub_graph()
    same = {"eq": [{"field": "dst"}, {"field": "dst"}]}
    steps = [scan("all", "Person"), hop("h1", "all"),
             {"name": "f", "op": "filter", "rows": {"ref": "h1"}, "pred": same},
             {"name": "d", "op": "dedup", "items": {"ref": "f"}}, through_props("p", "d")]
    return lines, steps, "p", len({b for a, b in edges}), THROUGH + UNCAPPED


def fx_concat_keeps_via():
    """two hops over knows, concatenated, then a dedup: the concat keeps how both were reached"""
    lines, edges = hub_graph()
    steps = [scan("all", "Person"), hop("h1", "all"), scan("all2", "Person"), hop("h2", "all2"),
             {"name": "c", "op": "concat", "a": {"ref": "h1"}, "b": {"ref": "h2"}},
             {"name": "d", "op": "dedup", "items": {"ref": "c"}}, through_props("p", "d")]
    return lines, steps, "p", len({b for a, b in edges}), THROUGH


def fx_props_distinct():
    """a props read over a hop's rows reads each distinct vertex once"""
    lines, edges = hub_graph()
    steps = [scan("all", "Person"), hop("h1", "all"),
             {"name": "p", "op": "read_vertex_props", "vids": {"ref": "h1"}, "label": "Person", "props": ["id"]}]
    return lines, steps, "p", len({b for a, b in edges}), []


DAYS = [12, 1, 1, 1, 1, 1, 1, 1, 1, 1]   # posts per day: day 0 holds most of them


def dated_posts():
    lines, i = [], 0
    for day, n in enumerate(DAYS):
        for _ in range(n):
            lines.append(post(i, day))
            i += 1
    return lines


def in_day0(lines):
    """the posts created on day 0, counted over the stream"""
    return sum(1 for l in lines if l[0] == "v" and l[2] == "Post" and D0 * DAY <= l[3]["creationDate"] < (D0 + 1) * DAY)


def fx_date_histogram():
    """a date-index read over day 0 of a ten-day span whose posts are uneven by day"""
    lines = [person(0)] + dated_posts()
    step = {"name": "r", "op": "read_index_range", "labels": ["Post"], "prop": "creationDate",
            "lo": D0 * DAY, "hi": (D0 + 1) * DAY}
    return lines, [step], "r", in_day0(lines), []


def fx_date_filter():
    """a date window on a one-label set: person 0 created all 21 posts (the same uneven days), its hop over hasCreator
    names Post (where the filter takes its label from), and the window is day 0. The hop's rows are the 21 posts, once
    each"""
    posts = dated_posts()
    lines = [person(0)] + posts + [has_creator(l[3]["id"], 0, "Post") for l in posts]
    window = {"and": [{"ge": [{"field": "creationDate"}, {"lit": D0 * DAY}]},
                      {"lt": [{"field": "creationDate"}, {"lit": (D0 + 1) * DAY}]}]}
    h = hop("h", "all", "hasCreator", "in")
    h["label"] = "Post"
    steps = [scan("all", "Person"), h, {"name": "f", "op": "filter", "rows": {"ref": "h"}, "pred": window},
             through_props("p", "f", "Post")]
    return lines, steps, "p", in_day0(lines), THROUGH


def fx_notnull_label():
    """a hop to the messages of five persons (30 posts, 10 comments), a props read of a Post-only prop, and a filter
    that needs it: the rows that pass are the hop's rows that reach a Post. Ten more posts have no creator, so the
    rule's share by the hop's own edges (30 of 40) is not its fallback's share by type counts (40 of 50).
    props_distinct is off in both runs: each message is reached once, so a props read keeps every row (its distinct
    model is not exact for that)"""
    lines = [person(i) for i in range(5)]
    for m in range(40):
        label = "Post" if m < 30 else "Comment"
        lines.append(post(m, imageFile=f"p{m}.jpg") if label == "Post" else comment(m))
        lines.append(has_creator(m, m % 5, label))
    lines += [post(m, imageFile=f"p{m}.jpg") for m in range(40, 50)]   # posts with no creator
    steps = [scan("all", "Person"), hop("h", "all", "hasCreator", "in"),
             {"name": "p", "op": "read_vertex_props", "vids": {"ref": "h"}, "label": "Post", "props": ["imageFile"]},
             {"name": "f", "op": "filter", "rows": {"ref": "p"}, "pred": {"notnull": {"field": "imageFile"}}},
             through_props("q", "f", "Post")]
    true = sum(1 for l in lines if l[0] == "e" and l[5] == "Post")
    return lines, steps, "q", true, THROUGH


def named_persons(counts):
    """persons with first names: {name: how many}, numbered in name order"""
    lines, i = [], 0
    for name in sorted(counts):
        for _ in range(counts[name]):
            lines.append(["v", vid(0, i), "Person", {"id": i, "creationDate": T0, "firstName": name}])
            i += 1
    return lines


SKEWED = {"Ann": 12, "Bob": 3, "Cid": 1, "Dee": 1, "Eve": 1, "Fay": 1, "Gus": 1}   # 20 persons, 7 names


def fx_index_value_rows():
    """an index read of a common first name: 12 of the 20 persons are Ann (top_values lists it); off, the read keeps
    the population over its distinct names, 20 / 7 = 3"""
    lines = named_persons(SKEWED)
    step = {"name": "r", "op": "read_index_eq", "label": "Person", "prop": "firstName", "value": "Ann"}
    return lines, [step], "r", SKEWED["Ann"], []


def name_filter(pred):
    """the 20 skewed persons' first names read, then a filter on them, read through a props read"""
    return [scan("all", "Person"),
            {"name": "p", "op": "read_vertex_props", "vids": {"ref": "all"}, "label": "Person", "props": ["firstName"]},
            {"name": "f", "op": "filter", "rows": {"ref": "p"}, "pred": pred}, through_props("q", "f")]


def fx_filter_value_share():
    """a filter firstName = Ann (with the notnull JOB's plans write beside it, implied): 12 of 20 pass; off, half"""
    pred = {"and": [{"notnull": {"get": "firstName"}}, {"eq": [{"get": "firstName"}, {"lit": "Ann"}]}]}
    return named_persons(SKEWED), name_filter(pred), "q", SKEWED["Ann"], THROUGH


def model_filter_value_in():
    """filter_value_share on in: firstName in (Ann, Bob) keeps 12 + 3 = 15 of 20"""
    pred = {"in": [{"get": "firstName"}, {"lit": ["Ann", "Bob"]}]}
    return named_persons(SKEWED), name_filter(pred), "q", 15, THROUGH


def model_filter_value_ne():
    """filter_value_share on ne: firstName <> Ann keeps the 8 others"""
    return named_persons(SKEWED), name_filter({"ne": [{"field": "firstName"}, {"lit": "Ann"}]}), "q", 8, THROUGH


def model_value_rows_rest():
    """value_rows' rest spread: 1,000 names of 3 persons each and 10 names of 1 (3,010 persons, 1,010 names). The
    statistics list 1,000 values (graph-stats' kTopValues): the 1,000 names of 3. An unlisted name gets the rest,
    (3,010 - 3,000) / (1,010 - 1,000) = 1 (the population over the names would give 3)"""
    counts = {f"n{i:04d}": 3 for i in range(1000)}
    counts.update({f"m{i}": 1 for i in range(10)})
    step = {"name": "r", "op": "read_index_eq", "label": "Person", "prop": "firstName", "value": "m5"}
    return named_persons(counts), [step], "r", 1, []


def fx_far_label_consumer():
    """a hop to what is located in 10 places (3 persons and 3 posts each: 60 rows), deduplicated, then a knows hop from
    them (each person knows one other: 30 edges). A post has no knows edge, so the knows hop's sources that count are
    the 30 persons: the rule keeps Person's share of isLocatedIn's edges (30 of 60), and the knows hop gives 30 rows;
    off, 60 sources at the mean degree, 60. dedup_by_degree and hop_edge_cap are off in both runs (the first's
    distinct model is not exact for one edge per vertex; the second would cap the knows hop at its 30 edges)"""
    lines = [["v", vid(2, i), "Place", {"id": i, "name": f"pl{i}", "type": "city"}] for i in range(10)]
    for i in range(10):
        for j in range(3):
            n = 3 * i + j
            lines.append(["v", vid(0, n), "Person", {"id": n, "creationDate": T0}])
            lines.append(["e", "isLocatedIn", vid(0, n), vid(2, i), {}, "Person", "Place"])
            lines.append(post(n))
            lines.append(["e", "isLocatedIn", vid(1, n), vid(2, i), {}, "Post", "Place"])
    lines += [knows(n, (n + 1) % 30) for n in range(30)]
    steps = [scan("all", "Place"), hop("h", "all", "isLocatedIn", "in"),
             {"name": "d", "op": "dedup", "items": {"ref": "h"}}, hop("k", "d")]
    return lines, steps, "k", 30, ["dedup_by_degree", "hop_edge_cap"]


def fx_props_read_consumer():
    """far_label_consumer's graph (10 places, 3 persons and 3 posts located in each), its hop deduplicated, read two
    ways: a props read naming Person and a knows hop. Both read the rows as Person, so the hop keeps Person's share (30
    of 60) and the knows hop gives 30; off, the props read is no consumer, the hop keeps all 60 and the knows hop 60"""
    lines, steps, _, true, base_off = fx_far_label_consumer()
    steps = steps[:3] + [{"name": "pr", "op": "read_vertex_props", "vids": {"ref": "d"}, "label": "Person",
                          "props": ["creationDate"]}, steps[3]]
    return lines, steps, "k", true, base_off


def model_props_read_notnull_once():
    """notnull_label's graph and plan (a hop to 30 posts and 10 comments, a props read naming Post, a filter that needs
    a Post prop): the props read makes the hop keep Post's share (30); the filter after it keeps all 30, not the share
    again (22)"""
    return fx_notnull_label()


def ldbc_also_indexing(label, prop):
    """a copy of the LDBC schema that also indexes label.prop by value (LDBC indexes one non-date prop per label), in a
    temporary file: its path"""
    s = json.load(open(SCHEMA))
    s["indexes"].append({"label": label, "prop": prop, "kind": "value", "id": max(i["id"] for i in s["indexes"]) + 1,
                         "width": 40})
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, prefix="schema-")
    json.dump(s, f)
    f.close()
    atexit.register(os.unlink, f.name)   # the test's own temporary file
    return f.name


def fx_value_share_correlated():
    """a filter firstName = Ann and lastName = Ames on the 20 skewed persons, every Ann named Ames (the two fields
    correlated): 12 pass; the smallest field's share keeps 12 of 20. Off, the product of the shares, 12/20 x 12/20 of 20,
    keeps 7"""
    lines = named_persons(SKEWED)
    for l in lines:
        l[3]["lastName"] = "Ames" if l[3]["firstName"] == "Ann" else "L" + l[3]["firstName"]
    pred = {"and": [{"eq": [{"get": "firstName"}, {"lit": "Ann"}]}, {"eq": [{"get": "lastName"}, {"lit": "Ames"}]}]}
    steps = name_filter(pred)
    steps[1]["props"] = ["firstName", "lastName"]
    return lines, steps, "q", SKEWED["Ann"], THROUGH, None, ldbc_also_indexing("Person", "lastName")


def fx_entry_value_degree():
    """a knows hop from an index read of Ann (12 of the 20 skewed persons): only Ann 0 knows anyone (5 persons), while
    the 3 Bobs know 10 each. The hop gives Ann's own 5 edges (top_degrees lists Ann); off, the 12 Anns at a mean degree
    over the 35 edges"""
    lines = named_persons(SKEWED)
    lines += [knows(0, j) for j in range(1, 6)]
    lines += [knows(b, j) for b in (12, 13, 14) for j in range(10)]
    steps = [{"name": "r", "op": "read_index_eq", "label": "Person", "prop": "firstName", "value": "Ann"}, hop("h", "r")]
    return lines, steps, "h", 5, []


def model_entry_degree_rest():
    """entry_degree's rest spread: 13 persons, one name each; n00-n09 know 5 persons each, m0 and m1 three each, z0 none
    (no owner), and a 14th person with no first name knows 10 (an owner no equality reaches). The list holds the 10
    names of 5 edges; m0 gets the rest over the other owners' names, counting only owners with a name,
    (56 - 50) / (12 - 10) = 3 (over every name, z0's included: 6 / 3 = 2; with the nameless owner's edges: 16 / 2 = 8)"""
    counts = {f"n{i:02d}": 1 for i in range(10)}
    counts.update({"m0": 1, "m1": 1, "z0": 1})
    lines = named_persons(counts)   # m0 = 0, m1 = 1, n00..n09 = 2..11, z0 = 12
    lines += [knows(i, (i + j) % 13) for i in range(2, 12) for j in range(1, 6)]
    lines += [knows(0, j) for j in (5, 6, 7)] + [knows(1, j) for j in (6, 7, 8)]
    lines.append(["v", vid(0, 13), "Person", {"id": 13, "creationDate": T0}])   # no firstName
    lines += [knows(13, j) for j in range(10)]
    steps = [{"name": "r", "op": "read_index_eq", "label": "Person", "prop": "firstName", "value": "m0"}, hop("h", "r")]
    return lines, steps, "h", 3, []


def complete_graph(n=10):
    """persons 0..n-1, each knowing every other (out edges): n (n - 1) rows from a scan's hop, each person n - 1 times
    on each side"""
    edges = [(a, b) for a in range(n) for b in range(n) if a != b]
    return [person(i) for i in range(n)] + [knows(a, b) for a, b in edges], edges


def fx_filter_in_share():
    """a filter_in on a hop's far end, against the nine persons person 0 knows (a hop from a literal vid: its own
    degree, exact). The far ends are spread evenly over the 10 persons, so the allowed share of them (9 of 10) is the
    share of rows kept: every row but the 9 into person 0. The rough rule keeps half"""
    lines, edges = complete_graph()
    h = hop("h", "all")
    h["pairs"] = True
    steps = [scan("all", "Person"), h,
             {"name": "a0", "op": "read_neighbors", "etype": "knows", "dir": "out", "srcs": [vid(0, 0)]},
             {"name": "f", "op": "filter_in", "rows": {"ref": "h"}, "field": "dst", "allowed": {"ref": "a0"}},
             through_props("p", "f")]
    return lines, steps, "p", sum(1 for a, b in edges if b != 0), THROUGH


def fx_dedup_domain_cap():
    """a dedup of a hop's sources (a pluck of src keeps no via, so dedup_by_degree does not apply): the 90 rows name
    10 persons, the population of the edges' source side"""
    lines, edges = complete_graph()
    h = hop("h", "all")
    h["pairs"] = True
    steps = [scan("all", "Person"), h, {"name": "s", "op": "pluck", "rows": {"ref": "h"}, "field": "src"},
             {"name": "d", "op": "dedup", "items": {"ref": "s"}}, through_props("p", "d")]
    return lines, steps, "p", len({a for a, b in edges}), THROUGH


def fx_size_biased_via():
    """a knows hop from persons reached through hasCreator, in a plan whose hint names knows size-biased: each of the
    ten persons created one post, so the hop's sources are every person once, and its rows are every knows edge. The
    degrees are uneven (person 0 knows 9, the rest 1 each), so a neighbour's degree (sum d^2 / sum d = 90 / 18 = 5) is
    not the population's mean (1.8), which the hop takes when its sources did not come through knows"""
    edges = [(0, b) for b in range(1, 10)] + [(a, 0) for a in range(1, 10)]
    lines = [person(i) for i in range(10)] + [post(100 + i) for i in range(10)]
    lines += [has_creator(100 + i, i, "Post") for i in range(10)] + [knows(a, b) for a, b in edges]
    steps = [scan("posts", "Post"), hop("cr", "posts", "hasCreator", "out"), hop("k", "cr")]
    return lines, steps, "k", len(edges), [], {"size_biased": ["knows"]}


def fx_concat_domain():
    """a dedup of two hops' far ends that both land on Person: knows from every person (90 rows) and hasCreator from
    ten posts (post i by person i, 10 rows). Their concat names the 10 persons; the sum of the two sides' domains
    (10 + 10) would let the dedup keep 20. The two hops come by different edges, so the concat keeps no via and
    dedup_by_degree does not apply"""
    lines, edges = complete_graph()
    lines += [post(100 + i) for i in range(10)] + [has_creator(100 + i, i, "Post") for i in range(10)]
    steps = [scan("all", "Person"), hop("h", "all"), scan("posts", "Post"), hop("cr", "posts", "hasCreator", "out"),
             {"name": "c", "op": "concat", "a": {"ref": "h"}, "b": {"ref": "cr"}},
             {"name": "d", "op": "dedup", "items": {"ref": "c"}}, through_props("p", "d")]
    return lines, steps, "p", 10, THROUGH


def model_concat_chain():
    """concat_domain's walk depth: a left-deep chain of nine concats of the same knows hop (JOB unions one hop per edge
    type this way), then a dedup with dedup_by_degree off: the walk reaches the hop through the chain and the dedup
    keeps the 10 persons. A walk stopped at depth 8 finds no domain and keeps all 10 x 90 = 900 rows"""
    lines, edges = complete_graph()
    steps = [scan("all", "Person"), hop("h", "all"), {"name": "c0", "op": "concat", "a": {"ref": "h"}, "b": {"ref": "h"}}]
    for i in range(1, 9):
        steps.append({"name": f"c{i}", "op": "concat", "a": {"ref": f"c{i - 1}"}, "b": {"ref": "h"}})
    steps += [{"name": "d", "op": "dedup", "items": {"ref": "c8"}}, through_props("p", "d")]
    return lines, steps, "p", 10, THROUGH + ["dedup_by_degree"]


def fx_exclude_share():
    """an exclude whose removed set is not made from its items: 20 persons; the items are the creators of posts 0..9
    (persons 0..9), the removed set the creators of posts 10..19 (persons 5..14), each 10 vertices of Person's 20.
    Drawn apart over Person, an item is removed with the chance 10 / 20: 5 kept, the true count (persons 0..4).
    Subtracting the removed set's size keeps 0"""
    lines = [person(i) for i in range(20)] + [post(100 + i) for i in range(20)]
    lines += [has_creator(100 + i, i, "Post") for i in range(10)] + [has_creator(110 + i, 5 + i, "Post") for i in range(10)]
    steps = [scan("posts_a", "Post"), scan("posts_b", "Post"),   # two scans: the sets share no step
             {"name": "pa", "op": "filter_in", "rows": {"ref": "posts_a"}, "field": "_vid", "allowed": [vid(1, 100 + i) for i in range(10)]},
             {"name": "pb", "op": "filter_in", "rows": {"ref": "posts_b"}, "field": "_vid", "allowed": [vid(1, 110 + i) for i in range(10)]},
             hop("a", "pa", "hasCreator", "out"), hop("b", "pb", "hasCreator", "out"),
             {"name": "x", "op": "exclude", "items": {"ref": "a"}, "remove": {"ref": "b"}}, through_props("p", "x")]
    return lines, steps, "p", 5, THROUGH


def model_exclude_drawn():
    """exclude_share's other branch: a removed set made from the items (a filter of them keeps the rough half, 5 of the
    10 creators) shares their steps, so the step keeps items - removed = 5. The share drawn over Person's 20 would remove
    5 / 20 of them and keep 8"""
    lines = [person(i) for i in range(20)] + [post(100 + i) for i in range(10)]
    lines += [has_creator(100 + i, i, "Post") for i in range(10)]
    steps = [scan("posts", "Post"), hop("a", "posts", "hasCreator", "out"),
             {"name": "half", "op": "filter", "rows": {"ref": "a"}, "pred": {"lt": [{"field": "_vid"}, {"field": "_vid"}]}},
             {"name": "x", "op": "exclude", "items": {"ref": "a"}, "remove": {"ref": "half"}}, through_props("p", "x")]
    return lines, steps, "p", 5, THROUGH


def fx_group_key_cap():
    """a group_count of a hop's far ends: the 90 knows rows of the complete graph name 10 persons, one group each; half
    the rows (the rough rule) would be 45"""
    lines, edges = complete_graph()
    steps = [scan("all", "Person"), hop("h", "all"), {"name": "g", "op": "group_count", "items": {"ref": "h"}},
             {"name": "v", "op": "pluck", "rows": {"ref": "g"}, "field": "item"}, through_props("p", "v")]
    return lines, steps, "p", len({b for a, b in edges}), THROUGH


def model_group_floor():
    """group_key_cap's floor: a group_sum by src over the one row of person 0's hop to the post it created makes one
    group; half of one row rounds to 0"""
    lines = [person(i) for i in range(10)] + [post(100 + i) for i in range(10)]
    lines += [has_creator(100 + i, i, "Post") for i in range(10)]
    h = {"name": "h", "op": "read_neighbors", "etype": "hasCreator", "dir": "in", "srcs": [vid(0, 0)], "pairs": True}
    steps = [h, {"name": "g", "op": "group_sum", "rows": {"ref": "h"}, "by": "src", "field": "dst"},
             {"name": "v", "op": "pluck", "rows": {"ref": "g"}, "field": "src"}, through_props("p", "v")]
    return lines, steps, "p", 1, THROUGH


def fx_hop_edge_cap():
    """a knows hop at a neighbour's degree (the plan's size_biased hint; its sources came through knows) from the 10
    distinct far ends of a first knows hop: person 0 knows 1..9 and each of them knows 0, so a neighbour's degree is
    (81 + 9) / 18 = 5, so the hop's estimate passes the type's 18 edges (off: 35, the dedup's ~7 sources x 5), but its
    sources are distinct (a dedup): every edge once, 18 rows. (model_size_biased_kept is the other branch: its sources repeat person 0, and
    its 90 rows are above the 18 edges)"""
    edges = [(0, b) for b in range(1, 10)] + [(a, 0) for a in range(1, 10)]
    lines = [person(i) for i in range(10)] + [knows(a, b) for a, b in edges]
    h1 = hop("h1", "all")
    h1["pairs"] = True
    steps = [scan("all", "Person"), h1, {"name": "v", "op": "pluck", "rows": {"ref": "h1"}, "field": "dst"},
             {"name": "d", "op": "dedup", "items": {"ref": "v"}}, hop("h2", "d")]
    return lines, steps, "h2", len(edges), [], {"size_biased": ["knows"]}


def model_concat_two_labels():
    """concat_domain over sides of different labels: the 90 knows rows (10 persons) and 10 scanned posts; the dedup
    keeps the 20 vertices, Person's 10 and Post's 10 together (a cap at either side's alone would give 10)"""
    lines, edges = complete_graph()
    lines += [post(100 + i) for i in range(10)]
    steps = [scan("all", "Person"), hop("h", "all"), scan("posts", "Post"),
             {"name": "c", "op": "concat", "a": {"ref": "h"}, "b": {"ref": "posts"}},
             {"name": "d", "op": "dedup", "items": {"ref": "c"}}, through_props("p", "d")]
    return lines, steps, "p", 20, THROUGH


def model_concat_shared():
    """concat_domain's walk on shared sides: c_i = concat(c_{i-1}, c_{i-1}), 40 deep, over a filter of a vertex read by
    id (no domain), so every concat's domain is unknown; each is walked once (an unknown domain is kept too), not 2^40
    times. The filter keeps half of its one row, 0, so every step after it has 0 rows"""
    lines = [person(i) for i in range(3)]
    steps = [{"name": "me", "op": "read_vertex_by_id", "label": "Person", "vid": vid(0, 0)},
             {"name": "f", "op": "filter", "rows": {"ref": "me"}, "pred": {"lt": [{"field": "_vid"}, {"field": "_vid"}]}},
             {"name": "c0", "op": "concat", "a": {"ref": "f"}, "b": {"ref": "f"}}]
    for i in range(1, 41):
        steps.append({"name": f"c{i}", "op": "concat", "a": {"ref": f"c{i - 1}"}, "b": {"ref": f"c{i - 1}"}})
    steps += [{"name": "d", "op": "dedup", "items": {"ref": "c40"}}, through_props("p", "d")]
    return lines, steps, "p", 0, THROUGH + ["dedup_by_degree"]


def model_exclude_no_shared_label():
    """exclude_share's label factor: removing 20 scanned posts from 10 persons (the creators of posts 0..9) removes none
    of them, the two share no label: 10 kept. Subtracting the sizes keeps 0. (Post i is by person i, each post has one)"""
    lines = [person(i) for i in range(20)] + [post(100 + i) for i in range(20)]
    lines += [has_creator(100 + i, i, "Post") for i in range(20)]
    steps = [scan("posts_a", "Post"),
             {"name": "pa", "op": "filter_in", "rows": {"ref": "posts_a"}, "field": "_vid", "allowed": [vid(1, 100 + i) for i in range(10)]},
             hop("a", "pa", "hasCreator", "out"), scan("posts_b", "Post"),
             {"name": "x", "op": "exclude", "items": {"ref": "a"}, "remove": {"ref": "posts_b"}}, through_props("p", "x")]
    return lines, steps, "p", 10, THROUGH


def model_group_two_keys():
    """group_key_cap over two keys: a group_agg by (src, dst) of the complete graph's 90 knows pairs, concatenated four
    times (360 rows, half of them 180), is capped at the keys' domains multiplied, 10 x 10 = 100 (the 90 pairs are the
    true groups: the cap is a bound, not the count)"""
    lines, edges = complete_graph()
    h = hop("h", "all")
    h["pairs"] = True
    steps = [scan("all", "Person"), h, {"name": "c1", "op": "concat", "a": {"ref": "h"}, "b": {"ref": "h"}},
             {"name": "c2", "op": "concat", "a": {"ref": "c1"}, "b": {"ref": "c1"}},
             {"name": "g", "op": "group_agg", "rows": {"ref": "c2"}, "by": ["src", "dst"], "aggs": {"n": ["count", None]}},
             {"name": "v", "op": "pluck", "rows": {"ref": "g"}, "field": "src"}, through_props("p", "v")]
    return lines, steps, "p", 100, THROUGH


def model_hop_edge_cap_both():
    """hop_edge_cap in both directions: person 0 knows 1..9 (9 edges). The 10 distinct far ends of a first knows hop
    (a dedup) take a neighbour's degree both ways (the size_biased hint; degrees 9 and 1: (81 + 9) / 18 = 5), more rows
    than edges (off: 35, the dedup's ~7 sources x 5), but each edge comes once from either end: 2 x 9 = 18, the true
    count"""
    lines = [person(i) for i in range(10)] + [knows(0, b) for b in range(1, 10)]
    h1 = hop("h1", "all", "knows", "both")
    h1["pairs"] = True
    steps = [scan("all", "Person"), h1, {"name": "v", "op": "pluck", "rows": {"ref": "h1"}, "field": "dst"},
             {"name": "d", "op": "dedup", "items": {"ref": "v"}}, hop("h2", "d", "knows", "both")]
    return lines, steps, "h2", 18, [], {"size_biased": ["knows"]}


def model_filter_in_correlated():
    """filter_in_share's other branch: an allowed set made from the rows themselves (the hop's own far ends, a
    semi-join back) is not drawn apart from them, so the filter keeps the rough half of the 90 rows: 45 (the share
    rule would give 90 x 10 / 10 = 90)"""
    lines, edges = complete_graph()
    h = hop("h", "all")
    h["pairs"] = True
    steps = [scan("all", "Person"), h, {"name": "v", "op": "pluck", "rows": {"ref": "h"}, "field": "dst"},
             {"name": "d", "op": "dedup", "items": {"ref": "v"}},
             {"name": "f", "op": "filter_in", "rows": {"ref": "h"}, "field": "dst", "allowed": {"ref": "d"}},
             through_props("p", "f")]
    return lines, steps, "p", 45, THROUGH


def model_filter_in_side():
    """filter_in_share on an edge type whose two sides differ: 20 posts, each created by one of 10 persons (post i by
    person i % 10); the hasCreator hop's far end is Person, so a filter of its 20 rows against all 10 persons keeps
    every row (10 / 10). The near side (the 20 posts) taken as the domain would keep half"""
    lines = [person(i) for i in range(10)] + [post(100 + i) for i in range(20)]
    lines += [has_creator(100 + i, i % 10, "Post") for i in range(20)]
    h = hop("h", "posts", "hasCreator", "out")
    h["pairs"] = True
    steps = [scan("posts", "Post"), h, scan("persons", "Person"),
             {"name": "f", "op": "filter_in", "rows": {"ref": "h"}, "field": "dst", "allowed": {"ref": "persons"}},
             through_props("p", "f")]
    return lines, steps, "p", 20, THROUGH


def model_size_biased_kept():
    """size_biased_via's other branch: a knows hop from the far ends of a knows hop keeps the hint. Person 0 knows 1..9
    and each of them knows 0 (out-degrees 9 and 1): the first hop's 18 rows hold person 0 nine times, so the second
    hop's true rows are 9 x 9 + 9 x 1 = 90 = 18 sources x the neighbour degree (sum d^2 / sum d = 90 / 18 = 5)"""
    edges = [(0, b) for b in range(1, 10)] + [(a, 0) for a in range(1, 10)]
    lines = [person(i) for i in range(10)] + [knows(a, b) for a, b in edges]
    return lines, [scan("all", "Person"), hop("h1", "all"), hop("h2", "h1")], "h2", 90, [], {"size_biased": ["knows"]}


def model_dedup_unsaturated():
    """the distinct model off its saturation: persons 0..19 each know one of persons 20..39, once each, so the 20 rows
    reach 20 vertices of in-degree 1. The model draws n = 20 rows over E = 20 edges: 20 (1 - (1 - 1/20)^20) = 12.83,
    which rounds to 13 (an exponent of n - 1 gives 12; the true count is 20)"""
    lines = [person(i) for i in range(40)] + [knows(i, 20 + i) for i in range(20)]
    steps = [scan("all", "Person"), hop("h1", "all"), {"name": "d", "op": "dedup", "items": {"ref": "h1"}},
             through_props("p", "d")]
    return lines, steps, "p", round(20 * (1 - (1 - 1 / 20) ** 20)), THROUGH


def model_frontier_bisection():
    """the via model when the frontier is smaller than the vertices it could reach (the bisection branch). B = persons
    1..40 have in-degree 2 and out-degree 2, to A = persons 41..120 (in-degree 1, out-degree 0). Person 0, the anchor,
    knows 20 of B, so the anchor's hop gives n = 20 rows, against 120 vertices with a via edge. The model solves for
    the share c of a vertex's via edges in the frontier: a c + b (1 - (1 - c)^2) = n, with a = 80 vertices of via
    degree 1 and b = 40 of via degree 2; x = 1 - c is the root of b x^2 + a x + (n - a - b) = 0. The predicted rows are
    n times the frontier's mean target degree, b 2 (1 - x^2) (the A bucket's targets are 0): 19.3, rounded to 19 (the
    true count is 40; an exponent of 1 for B gives 13)"""
    b_set = list(range(1, 41))
    a_set = list(range(41, 121))
    edges = [(0, v) for v in b_set[:20]]                       # the anchor knows 20 of B
    edges += [(200, v) for v in b_set[:20]]                    # the other in-edge of those 20
    edges += [(201, v) for v in b_set[20:]] + [(202, v) for v in b_set[20:]]   # B's second half: two in-edges too
    edges += [(b_set[i // 2], v) for i, v in enumerate(a_set)]  # each of B knows two of A
    lines = [person(i) for i in range(121)] + [person(i) for i in (200, 201, 202)] + [knows(x, y) for x, y in edges]
    a, b, n = len(a_set), len(b_set), 20
    x = (-a + (a * a - 4 * b * (n - a - b)) ** 0.5) / (2 * b)
    steps = [{"name": "me", "op": "read_vertex_by_id", "label": "Person", "vid": vid(0, 0)},
             hop("h1", "me"), hop("h2", "h1")]
    return lines, steps, "h2", round(b * 2 * (1 - x * x)), []


def model_concat_two_vias():
    """a concat of two sets reached differently keeps no via: a knows hop (60 rows on the hub graph) and a hasCreator
    hop from 10 posts (10 rows), concatenated, then a dedup, which then has no edge kind to weigh by and keeps all
    60 + 10 = 70 rows"""
    lines, _ = hub_graph()
    lines += [post(100 + i) for i in range(10)] + [has_creator(100 + i, 60 + i % 3, "Post") for i in range(10)]
    steps = [scan("all", "Person"), hop("h1", "all"), scan("posts", "Post"), hop("h2", "posts", "hasCreator", "out"),
             {"name": "c", "op": "concat", "a": {"ref": "h1"}, "b": {"ref": "h2"}},
             {"name": "d", "op": "dedup", "items": {"ref": "c"}}, through_props("p", "d")]
    return lines, steps, "p", 60 + 10, THROUGH + UNCAPPED


def model_int_range():
    """date_filter on an int property (Comment.length): the window's integer width over the property's range,
    max - min + 1 (statistics field value_range). Person 0 created 20 comments of lengths 0..19; length >= 5 and
    length < 10 keeps 5 of the 20 values: 20 rows x 5 / 20 = 5"""
    lines = [person(0), post(0)]
    for i in range(20):
        lines.append(["v", vid(1, 1 + i), "Comment", {"id": 1 + i, "creationDate": T0 + 1000, "length": i}])
        lines.append(has_creator(1 + i, 0, "Comment"))
    window = {"and": [{"ge": [{"field": "length"}, {"lit": 5}]}, {"lt": [{"field": "length"}, {"lit": 10}]}]}
    h = hop("h", "all", "hasCreator", "in")
    h["label"] = "Comment"
    steps = [scan("all", "Person"), h, {"name": "f", "op": "filter", "rows": {"ref": "h"}, "pred": window},
             through_props("p", "f", "Comment")]
    return lines, steps, "p", 20 * 5 // 20, THROUGH


MODELS = {"dedup_by_degree": model_dedup_unsaturated, "frontier_inclusion": model_frontier_bisection,
          "concat_keeps_via": model_concat_two_vias, "date_filter": model_int_range,
          "filter_in_share": model_filter_in_correlated, "filter_in_share (sides)": model_filter_in_side,
          "size_biased_via": model_size_biased_kept, "concat_domain (depth)": model_concat_chain,
          "exclude_share (drawn)": model_exclude_drawn, "group_key_cap (floor)": model_group_floor,
          "concat_domain (two labels)": model_concat_two_labels,
          "concat_domain (shared sides)": model_concat_shared,
          "exclude_share (no shared label)": model_exclude_no_shared_label,
          "group_key_cap (two keys)": model_group_two_keys,
          "hop_edge_cap (both)": model_hop_edge_cap_both,
          "filter_value_share (in)": model_filter_value_in, "filter_value_share (ne)": model_filter_value_ne,
          "index_value_rows (the rest)": model_value_rows_rest,
          "entry_value_degree (the rest)": model_entry_degree_rest,
          "props_read_consumer (notnull once)": model_props_read_notnull_once}

FIXTURES = {"frontier_inclusion": fx_frontier_inclusion, "dedup_by_degree": fx_dedup_by_degree,
            "filter_keeps_via": fx_filter_keeps_via, "concat_keeps_via": fx_concat_keeps_via,
            "props_distinct": fx_props_distinct, "date_histogram": fx_date_histogram, "date_filter": fx_date_filter,
            "notnull_label": fx_notnull_label, "filter_in_share": fx_filter_in_share,
            "dedup_domain_cap": fx_dedup_domain_cap, "size_biased_via": fx_size_biased_via,
            "concat_domain": fx_concat_domain, "exclude_share": fx_exclude_share,
            "group_key_cap": fx_group_key_cap, "hop_edge_cap": fx_hop_edge_cap,
            "index_value_rows": fx_index_value_rows, "filter_value_share": fx_filter_value_share,
            "far_label_consumer": fx_far_label_consumer, "entry_value_degree": fx_entry_value_degree,
            "props_read_consumer": fx_props_read_consumer,
            "value_share_correlated": fx_value_share_correlated}


def switches():
    """the rule switches the binary names (predict --rule-names: Rules::names())"""
    p = subprocess.run([str(cpp_bin()), "predict", "--rule-names"], capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"check_predict_rules: FAIL: predict --rule-names exit {p.returncode}: {p.stderr.strip()}")
    return json.loads(p.stdout)


def predicted_rows(t, lines, steps, rules_off, hints=None, schema=SCHEMA):
    """{step: rows} from predict --plans over a one-body plan, with the given rules off: the step rows of the
    anchor level's capture (--out-predicted-anchor), one capture dir per call (a capture made with other rules off is
    refused). The anchor level, not predicted: model_frontier_bisection's hand value assumes the anchor's own degree, which
    only the anchor statistics give (the other fixtures have no anchor step, so the two levels agree there)"""
    rec = os.path.join(t, "records.jsonl")
    if not os.path.exists(rec):
        # an ingest reads each label's vertices, then each edge type's edges, together
        def group(i):
            kind, a, b = lines[i][0], lines[i][1], lines[i][2]
            return (kind != "v", b if kind == "v" else a, i)
        order = sorted(range(len(lines)), key=group)
        with open(rec, "w") as f:
            for i in order:
                f.write(json.dumps(lines[i]) + "\n")
        build_stats.build(rec, schema, "ldbc", "fixture", os.path.join(t, "stats", "stats.json"))
        os.makedirs(os.path.join(t, "tally"))
        with open(os.path.join(t, "tally", "meta-fixture.json"), "w") as f:
            subprocess.run([str(cpp_bin()), "tally", "--schema", schema, "--spec", SPEC, "--records", rec], stdout=f,
                           check=True)
    req = {"params": {}, "run": {"P": 1, "parts": True, "trace_args": True},
           "store": {"dir": os.path.join(t, "meta-fixture"), "format": "meta", "scale": "fixture", "schema": schema},
           "plan": {"plan": "fixture", "params": {}, "let": {}, "question": "a rule fixture",
                    "plans": {"b": {"output": steps[-1]["name"], "steps": steps}},
                    **({"estimate_hints": hints} if hints else {}),
                    "bodies": {"b": {"sha": "000000000000", "sources": ["b"]}}}}
    path = os.path.join(t, "fixture__fixture__1.json")   # <cell>__<query>__<param>: the capture's names
    with open(path, "w") as f:
        json.dump(req, f)
    cap = tempfile.mkdtemp(prefix="cap-", dir=t)
    cmd = [str(cpp_bin()), "predict", "--plans", "--out-predicted-anchor", cap,
           "--stats", os.path.join(t, "stats", "stats.json"),
           "--anchor-stats", os.path.join(t, "stats", "anchor_stats.json"), "--tally-dir", os.path.join(t, "tally"),
           "--schema", schema]
    if rules_off:
        cmd += ["--rules-off", ",".join(sorted(rules_off))]
    try:   # a walk that does not end is a failure, not a hang
        p = subprocess.run(cmd + [path], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError("predict --plans ran over 120 s")
    if p.returncode != 0:
        raise RuntimeError(f"predict --plans exit {p.returncode}: {p.stderr.strip()[-400:]}")
    with open(os.path.join(cap, "steps", "fixture.csv"), newline="") as f:
        return {r["step"]: float(r["rows"]) for r in csv.DictReader(f)}


def main():
    names = switches()
    fails = []
    uncovered = sorted(set(names) - set(FIXTURES))
    unknown = sorted(set(FIXTURES) - set(names))
    if uncovered:
        fails.append(f"switches with no fixture: {uncovered}")
    if unknown:
        fails.append(f"fixtures naming no switch of the binary: {unknown}")
    exact = caught = 0
    for name, make in FIXTURES.items():
        lines, steps, step, true, base_off, *rest = make()   # a sixth field: the plan's estimate_hints; a seventh: a schema
        hints = rest[0] if rest else None
        schema = rest[1] if len(rest) > 1 else SCHEMA
        with tempfile.TemporaryDirectory() as t:
            try:
                on = predicted_rows(t, lines, steps, base_off, hints, schema)[step]
                off = predicted_rows(t, lines, steps, base_off + [name], hints, schema)[step]
            except (RuntimeError, KeyError) as e:
                fails.append(f"{name}: {type(e).__name__}: {e}")
                print(f"  FAIL {name}: {e}")
                continue
        ok_on, ok_off = on == true, off != true
        exact += ok_on
        caught += ok_off
        print(f"  {'ok  ' if ok_on and ok_off else 'FAIL'} {name}: step {step}, true {true}; rules on {on:g}"
              f"{' (base off: ' + ','.join(base_off) + ')' if base_off else ''}; {name} off {off:g}"
              f"{'' if ok_off else ' (plant NOT caught)'}")
        if not ok_on:
            fails.append(f"{name}: rows {on:g} with the rule on, true count {true}")
        if not ok_off:
            fails.append(f"{name}: the plant ({name} off) gives the true count {true}: not caught")
    models_ok = 0
    for name, make in MODELS.items():
        lines, steps, step, want, base_off, *hints = make()
        with tempfile.TemporaryDirectory() as t:
            try:
                got = predicted_rows(t, lines, steps, base_off, hints[0] if hints else None)[step]
            except (RuntimeError, KeyError) as e:
                fails.append(f"model {name}: {type(e).__name__}: {e}")
                continue
        models_ok += got == want
        print(f"  {'ok  ' if got == want else 'FAIL'} model {make.__name__}: step {step}, the model's value {want}; "
              f"predicted {got:g}")
        if got != want:
            fails.append(f"model {make.__name__}: predicted {got:g}, the model's value by hand {want}")
    n = len(FIXTURES)
    print(f"check_predict_rules: {len(names)} switches, {n} fixtures; {exact} of {n} exact, {caught} of {n} plants "
          f"caught; {models_ok} of {len(MODELS)} model values")
    if fails:
        for f in fails:
            print(f"   {f}")
        print("check_predict_rules FAIL")
        return 1
    print("check_predict_rules OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
