"""The descriptions behind the layout-choice workloads of the ldbc suite: what each query does (QUERIES), the params
behind "mean over its params" (PARAMS), and the ratios a swept workload is scaled by (RATIOS). The workloads themselves
(W01-W15, I01, I02) are files, inputs/workloads/ldbc/<id>.json, read by benchmarks.workloads(): W01-W15 written by hand, each chosen
so that a different layout should win (w11-w15 the mixed-layout candidates, w15 the control); I01 and I02 written by
workloads/make_workloads.py from LDBC's own mix.

A workload file holds its items (family, query, frequency, params: `family` names the grid capture the query was
measured in (microbench reads, or the writes suite); `params` restricts the item to some of the query's parameters
(param_idx, 1-based: for the stratified families 1..5 = p10 / p50 / p90 / p99 / max of the family's fan-out), null =
the mean over all of them), `expect` (what we predicted BEFORE measuring), `why` (what the measured per-part costs show:
each {name} in it is computed by why_numbers.py from `why_numbers`, or is a scale-free number with its reason in
`why_scale_free`) and `sweep` (optional: the queries whose frequency sweep.py scales by r).
"""

MB, W = "microbench", "writes"

# What each query used here does, in plain words (the plans: python/kvevaluator/workloads/microbench/queries/,
# python/kvevaluator/workloads/writes/). Read by workload_story.py for the per-workload READMEs and figures.
QUERIES = {
    "point7":            "all 7 properties of one Person",
    "point_post":        "all properties of one Post (content included)",
    "hop1_knows":        "a Person's friend list (knows, both directions), counted",
    "hop1p1_knows":      "a Person's friends, then 1 property (firstName) of each friend",
    "hop1p3_knows":      "a Person's friends, then 3 properties of each friend",
    "nhop2":             "friends of friends of a Person (2 knows hops, both directions)",
    "hop2_post_likers":  "who liked a Person's posts: their messages, the Posts among them, each post's likers",
    "entry_eq_scan":     "Persons with a given firstName, with no index: read firstName of every Person",
    "hop1_msgs_date":    "a Person's messages, then creationDate of each (a date filter)",
    "hop1_msgs_content": "a Person's messages, then the content of each",
    "hop1_posts_props":  "a Person's posts, then every property of each post",
    "iu2":               "add a like (a Person likes a Post)",
    "iu6":               "add a Post (with its creator, forum, country and tags)",
    "iu7":               "add a Comment (with its creator, country, parent message and tags)",
    "w_upd_plain":       "update one Person property (lastName)",
}
# The params behind "mean over its params", per family (inputs/params/<family>/sf<SF>/).
PARAMS = {
    MB: "5 anchors at the p10 / p50 / p90 / p99 / max of the query's fan-out (Persons by knows degree or by "
        "message count, Posts by content length; entry_eq_scan: 3 firstName values)",
    W:  "5 Persons (one per knows-degree stratum); each edge's far end is a typical vertex "
        "(the list a median existing edge sits in)",
}


def add_new(into, extra, what):
    """merge another module's entries; a key that already exists is an error (one description per id, one home)"""
    clash = sorted(set(into) & set(extra))
    if clash:
        raise SystemExit(f"workloads: {what} {clash} already defined here -- define each id once")
    into.update(extra)


# I01 and I02's queries and params (ldbc.py) join the dicts above
from kvcost.layout_cost.workloads import ldbc as ldbc_mix
add_new(QUERIES, ldbc_mix.QUERIES, "query")
add_new(PARAMS, ldbc_mix.PARAMS, "params family")

# the ratios r a swept workload's `sweep` queries are scaled by (sweep.py)
RATIOS = [10 ** (e / 4) for e in range(-8, 9)]      # 0.01 .. 100, 4 points per decade
