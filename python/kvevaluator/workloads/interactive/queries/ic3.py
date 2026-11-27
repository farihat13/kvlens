"""LDBC IC3 plans (sim + engine)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import and_, le, lt, eq, ne, notnull, get, field, add, param, let
from kvevaluator.plans.ir import authoring as E
from kvevaluator.workloads.interactive._shared import (MESSAGE_LABELS, PERSON_RENAME, _anchor_person, _friend_set,
                                                 _scan_lookup, person_vid, PERSON_PARAM, PERSON_LET, DATE_WINDOW_PARAM,
                                                 DATE_WINDOW_LET)


# ------------------------------------------------------------------------ IC3

IC3_FIELDS = ["personId", "personFirstName", "personLastName", "xCount", "yCount", "count"]

def _ic3_steps(args, date_via_index, label_only=False, logical=False):
    """IC3: friends+fof who are FOREIGN to countries X and Y (their city is not
    part of either) and posted messages located in BOTH X and Y within the
    window -> top 20 by (xCount+yCount desc, personId asc).
    Country membership of persons goes person->city->country FORWARD (the
    reverse from a country would drag in every message located there); message
    country is a direct isLocatedIn hop, read only for in-window messages.

    `date_via_index` picks HOW the window filter is applied to the fof messages:
    False = read creationDate off EVERY fof message then filter (the baseline);
    True  = enumerate the window from the creationDate index + intersect with the
    fof messages (cheaper when the fof set >> the window, IC3's case). See ic3 /
    ic3_getdate.

    label_only: the two Place.name lookups become read_vertices_by_label(Place) + name read
    + filter (the label-index-only design); the caller also passes
    date_via_index=False, since there is no date index to enumerate the window."""
    p = let("me_vid")
    if logical:                                  # the optimizer's entry choice points
        cxy_entry = [Step("ctryx", "entry_eq", label="Place", prop="name", value=param("countryXName")),
                     Step("ctryy", "entry_eq", label="Place", prop="name", value=param("countryYName"))]
    elif label_only:
        cxy_entry = (_scan_lookup("ctryx", "Place", "name", param("countryXName"))
                     + _scan_lookup("ctryy", "Place", "name", param("countryYName")))
    else:
        cxy_entry = [Step("ctryx", "read_index_eq", label="Place", prop="name", value=param("countryXName")),
                     Step("ctryy", "read_index_eq", label="Place", prop="name", value=param("countryYName"))]
    steps = _friend_set(p, 2) + cxy_entry + [
        Step("ctries",    "concat", a=ref("ctryx"), b=ref("ctryy")),
        # eligible = friend set minus persons whose city sits in X or Y
        Step("pcity",     "read_neighbors", srcs=ref("f"), etype="isLocatedIn", dir="out", pairs=True),
        Step("pcity_v",   "pluck", rows=ref("pcity"), field="dst"),
        Step("pcity_d",   "dedup", items=ref("pcity_v")),
        Step("ccountry",  "read_neighbors", srcs=ref("pcity_d"), etype="isPartOf", dir="out", pairs=True),
        Step("bad_city",  "filter_in", rows=ref("ccountry"), field="dst", allowed=ref("ctries")),
        Step("bad_cityv", "pluck", rows=ref("bad_city"), field="src"),
        Step("bad_ploc",  "filter_in", rows=ref("pcity"), field="dst", allowed=ref("bad_cityv")),
        Step("bad_person","pluck", rows=ref("bad_ploc"), field="src"),
        Step("eligible",  "exclude", items=ref("f"), remove=ref("bad_person")),
        # their messages in the window; country read only for survivors
        Step("fmsg",      "read_neighbors", srcs=ref("eligible"), etype="hasCreator", dir="in", pairs=True),
        Step("fmsg_v",    "pluck", rows=ref("fmsg"), field="dst"),
    ]
    if logical:                                  # the optimizer's window choice point
        steps += [
            Step("win_msg", "window_narrow", vids=ref("fmsg_v"), labels=["Post", "Comment"],
                 prop="creationDate", lo=param("startDate"), hi=let("end"), idx_name="win_idx"),
        ]
    elif date_via_index:
        # Enumerate the window from the creationDate index and intersect with the
        # fof messages, instead of reading the date off all of them. The precise
        # read_vertex_props+filter below runs on the small intersection -- it also trims
        # dgraph's day-granular read_index_range superset back to the exact window.
        steps += [
            Step("win_idx", "read_index_range", labels=["Post", "Comment"], prop="creationDate",
                 lo=param("startDate"), hi=let("end")),
            Step("win_msg", "intersect", items=ref("fmsg_v"), other=ref("win_idx")),
        ]
    steps += [
        Step("msg_date",  "read_vertex_props", vids=ref("win_msg" if (date_via_index or logical) else "fmsg_v"),
             props=["creationDate"], label=["Post", "Comment"]),
        Step("in_win",    "filter", rows=ref("msg_date"),
             pred=and_(le(param("startDate"), field("creationDate")), lt(field("creationDate"), let("end")))),
        Step("winv",      "pluck", rows=ref("in_win"), field="_vid"),
        Step("msg_loc",   "read_neighbors", srcs=ref("winv"), etype="isLocatedIn", dir="out", pairs=True),
        Step("msg_ctry",  "rename", rows=ref("msg_loc"), names={"src": "_msg", "dst": "_ctry"}),
        Step("auth_msg",  "rename", rows=ref("fmsg"), names={"src": "person", "dst": "_msg2"}),
        Step("in_x",      "filter_in", rows=ref("msg_ctry"), field="_ctry", allowed=ref("ctryx")),
        Step("in_y",      "filter_in", rows=ref("msg_ctry"), field="_ctry", allowed=ref("ctryy")),
        Step("join_x",    "join", left=ref("in_x"), right=ref("auth_msg"), left_on="_msg", right_on="_msg2"),
        Step("join_y",    "join", left=ref("in_y"), right=ref("auth_msg"), left_on="_msg", right_on="_msg2"),
        Step("x_person",  "pluck", rows=ref("join_x"), field="person"),
        Step("x_count",   "group_count", items=ref("x_person")),
        Step("y_person",  "pluck", rows=ref("join_y"), field="person"),
        Step("y_count",   "group_count", items=ref("y_person")),
        # "in both": xCount > 0 AND yCount > 0
        Step("x_persons", "pluck", rows=ref("x_count"), field="item"),
        Step("y_persons", "pluck", rows=ref("y_count"), field="item"),
        Step("both",      "intersect", items=ref("x_persons"), other=ref("y_persons")),
        Step("x_both",    "filter_in", rows=ref("x_count"), field="item", allowed=ref("both")),
        Step("x_named",   "rename", rows=ref("x_both"), names={"count": "xCount"}),
        Step("y_named",   "rename", rows=ref("y_count"), names={"count": "yCount", "item": "_p2"}),
        Step("counts",    "join", left=ref("x_named"), right=ref("y_named"), left_on="item", right_on="_p2"),
        Step("summed",    "derive", rows=ref("counts"), into="count",
             fn=add(field("xCount"), field("yCount"))),
        Step("pid",       "read_vertex_props", vids=ref("both"), props=["id"], label="Person"),
        Step("pid_named", "rename", rows=ref("pid"), names={"id": "personId"}),
        Step("ranked",    "join", left=ref("summed"), right=ref("pid_named"), left_on="item", right_on="_vid"),
        Step("top20",     "top_k", rows=ref("ranked"), keys=[("count", "desc"), ("personId", "asc")],
             k=20),
        # firstName/lastName are projection-only -- (count, personId) fully rank the set, so defer
        # the names to the 20 winners instead of reading them for all of `both`. Mirrors IC5.
        Step("topv",      "pluck", rows=ref("top20"), field="item"),
        Step("names",     "read_vertex_props", vids=ref("topv"), props=["firstName", "lastName"],
             label="Person"),
        Step("names_r",   "rename", rows=ref("names"),
             names={"firstName": "personFirstName", "lastName": "personLastName"}),
        Step("named",     "join", left=ref("top20"), right=ref("names_r"), left_on="item", right_on="_vid"),
        Step("out",       "project", rows=ref("named"),
             fields=IC3_FIELDS),
    ]
    return Plan(steps, params={**PERSON_PARAM, **DATE_WINDOW_PARAM,
                               "countryXName": {}, "countryYName": {}},
                let={**PERSON_LET, **DATE_WINDOW_LET})


def ic3_logical(args):
    """The LOGICAL ic3 for the optimizer's search: THREE choice points -- the two
    Place.name entries (entry_eq each) and the date window (window_narrow:
    range-index enumerate+intersect; the full per-message date read is not offered)."""
    return _ic3_steps(args, date_via_index=True, logical=True)


def ic3(args):
    """IC3 (index-window plan): enumerate the window from the creationDate index + intersect with the fof messages
    (read_index_range), instead of reading the date off every fof message (ic3_getdate, the recorded loser).
    Cheaper because IC3's fof set >> the window.
    """
    return _ic3_steps(args, date_via_index=True)


def ic3_getdate(args):
    """Recorded loser for IC3 (see ic3): read creationDate off EVERY fof message
    then window-filter (country read only for survivors). Optimal only when the
    candidate set is SMALLER than the window (IC4's shape, which keeps this
    pattern); IC3's fof set dwarfs the window, so the read_index_range plan wins."""
    return _ic3_steps(args, date_via_index=False)


# ---- the list-join twins (queries.TWIN_TRANSFORM)

def _ic3_twin(plan, edit):
    """`plan` (a Plan or a baked Step list) with `edit` applied to a fresh copy of its steps"""
    steps = [Step(s.out, s.op, **dict(s.args)) for s in (plan.steps if isinstance(plan, Plan) else plan)]
    steps = edit(steps)
    return Plan(steps, params=plan.params, let=plan.let, output=plan.output) if isinstance(plan, Plan) else steps


def _ic3_replace(steps, first, last, new):
    """`steps` with the run first..last (step names, inclusive) replaced by `new`"""
    names = [s.out for s in steps]
    return steps[:names.index(first)] + new + steps[names.index(last) + 1:]


def _ic3_country_lists(steps):
    by = {s.out: s for s in steps}
    new = [Step("x_loc", "read_neighbors", srcs=ref("ctryx"), etype="isLocatedIn", dir="in", pairs=True),
           Step("y_loc", "read_neighbors", srcs=ref("ctryy"), etype="isLocatedIn", dir="in", pairs=True),
           Step("x_ctry", "rename", rows=ref("x_loc"), names={"src": "_ctry", "dst": "_msg"}),
           Step("y_ctry", "rename", rows=ref("y_loc"), names={"src": "_ctry", "dst": "_msg"}),
           by["auth_msg"],
           Step("in_x", "filter_in", rows=ref("x_ctry"), field="_msg", allowed=ref("winv")),
           Step("in_y", "filter_in", rows=ref("y_ctry"), field="_msg", allowed=ref("winv"))]
    return _ic3_replace(steps, "msg_loc", "in_y", new)


def _ic3_exact_window(steps):
    return _ic3_replace(steps, "msg_date", "winv", [Step("winv", "dedup", items=ref("win_msg"))])


def ic3_country_lists(plan):
    """IC3's twin V1 (the plan-change rule): the window messages located in X and in Y come from the two countries'
    isLocatedIn IN lists (`x_loc` / `y_loc`), cut to the window's messages in memory, instead of one isLocatedIn OUT
    read per window message (`msg_loc`). Same answer. What an engine needs to choose: the two countries' list lengths
    against the number of window messages, both in its statistics.
    """
    return _ic3_twin(plan, _ic3_country_lists)


def ic3_exact_window(plan):
    """IC3's twin V0: no per-message creationDate re-read (`msg_date` + `in_win`) after the index window. Valid only
    where the date index returns exactly [lo, hi): TWIN_REQUIRES sorted_range_index. A day-bucketed index returns the
    boundary days whole, a superset, and the base keeps its re-read there."""
    return _ic3_twin(plan, _ic3_exact_window)


def ic3_exact_window_country_lists(plan):
    """IC3's twins V0 and V1 together (the same requirement as V0)."""
    return _ic3_twin(plan, lambda steps: _ic3_country_lists(_ic3_exact_window(steps)))


LOGICAL = {"ic3": ic3_logical,
           "ic3_getdate": ic3_getdate}   # ablation alias: single candidate (passthrough)


def ic3_labelonly(args):
    """IC3 with only the label index: both Place.name lookups become
    read_vertices_by_label + filter, and the window is read off every fof message (no date
    index) -- i.e. ic3_getdate's date handling plus scanned Place entries."""
    return _ic3_steps(args, date_via_index=False, label_only=True)


# ── IC3 engine-faithful plans ──────────────────────────────────────────────────────────────────
# Each builder transcribes the engine's captured sf0.3 plan
# (ldbc/<db>/ic3/p1/plan.txt). Every KV step cites the plan
# node it models; in-memory steps cost nothing and are grouped.


def _ic3_counts(pairs, places, tags, country, lo, hi, out):
    """In memory: per author, the distinct messages located in `country` with creationDate in
    [lo, hi). `pairs` = (author src, message dst), `places` = (message src, place dst),
    `tags` = the message rows read by the isLocatedIn Traverse."""
    return [
        Step(f"{out}_pn", "rename", rows=ref(f"{out}_names"), names={"name": "_place"}),
        Step(f"{out}_mp", "join", left=ref(places), right=ref(f"{out}_pn"),
             left_on="dst", right_on="_vid"),
        Step(f"{out}_mr", "rename", rows=ref(tags), names={"_vid": "_msg"}),
        Step(f"{out}_md", "join", left=ref(f"{out}_mp"), right=ref(f"{out}_mr"),
             left_on="src", right_on="_msg"),
        Step(f"{out}_ok", "filter", rows=ref(f"{out}_md"),
             pred=and_(eq(get("_place"), country), notnull(get("creationDate")),
                       le(lo, field("creationDate")), lt(field("creationDate"), hi))),
        Step(f"{out}_okv", "pluck", rows=ref(f"{out}_ok"), field="src"),
        Step(f"{out}_a", "filter_in", rows=ref(pairs), field="dst", allowed=ref(f"{out}_okv")),
        Step(f"{out}_av", "pluck", rows=ref(f"{out}_a"), field="src"),
        Step(out, "group_count", items=ref(f"{out}_av")),
    ]


def ic3_nebula_engine(args):
    """IC3 nebula, from nebula/ic3/p1/plan.txt.

    The countryY side (nodes 18-20, 50) runs only for the `other`s with cntX > 0 (node 61
    feeds Argument 18), and the own-city side (27, 28, 51) only for those with cntY > 0 too.
    sf0.3 vs live: S, G and N exact at every param."""
    p = person_vid(args["person"])
    cx, cy = args["countryXName"], args["countryYName"]
    lo = int(args["startDate"])
    hi = lo + int(args["durationDays"]) * 86400000
    steps = [
        Step("me", "read_vertex_by_id", vid=p),
        # node 47 Traverse knows BOTH 1..2, dedup: 1, no vertexProps: first-step Person._tag
        # probe on the start (1 GET), then 1 SEEK per direction per frontier vertex + 1 NEXT
        Step("mtag", "tag_probe", vids=[p], label="Person"),
        Step("reach", "read_closure", srcs=ref("me"), etype="knows", dir="both", max_levels=2),
        # nodes 52/54 Filter, 55 Project, 9 Dedup: in memory
        Step("others", "exclude", items=ref("reach"), remove=[p]),
        # node 53 AppendVertices Person [firstName,lastName], dedup: 1
        Step("oprops", "read_vertex_props", vids=ref("others"), label="Person",
             props=["id", "firstName", "lastName"]),
        # node 11 Traverse hasCreator IN from each other: vertexProps Person
        # [firstName,lastName] (1 GET per source) + 1 SEEK + 1 NEXT per edge
        Step("osrc", "read_vertex_props", vids=ref("others"), label="Person",
             props=["firstName", "lastName"]),
        Step("mpairs", "read_neighbors", srcs=ref("others"), etype="hasCreator", dir="in",
             pairs=True),
        Step("msgs", "pluck", rows=ref("mpairs"), field="dst"),
        # node 12 Traverse isLocatedIn OUT from each message: vertexProps = all 8 tags
        # (8 GETs per source) + 1 SEEK + 1 NEXT per edge
        Step("mtags", "all_tag_props", vids=ref("msgs")),
        Step("ppairs", "read_neighbors", srcs=ref("msgs"), etype="isLocatedIn", dir="out",
             pairs=True),
        # node 49 AppendVertices Place [name], dedup: 1
        Step("plc", "pluck", rows=ref("ppairs"), field="dst"),
        Step("cntx_names", "read_vertex_props", vids=ref("plc"), label="Place", props=["name"]),
    ] + _ic3_counts("mpairs", "ppairs", "mtags", cx, lo, hi, "cntx") + [
        # nodes 14-16, 60, 61: cntX per other, keep cntX > 0 (in memory, above)
        Step("sx", "pluck", rows=ref("cntx"), field="item"),
        # node 19 Traverse hasCreator IN from the cntX > 0 others: vertexProps Person (1 GET
        # per source) + 1 SEEK + 1 NEXT per edge
        Step("ysrc", "read_vertex_props", vids=ref("sx"), label="Person",
             props=["firstName", "lastName"]),
        Step("ympairs", "read_neighbors", srcs=ref("sx"), etype="hasCreator", dir="in",
             pairs=True),
        Step("ymsgs", "pluck", rows=ref("ympairs"), field="dst"),
        # node 20 Traverse isLocatedIn OUT: all 8 tags per source message + 1 SEEK + 1 NEXT
        Step("ymtags", "all_tag_props", vids=ref("ymsgs")),
        Step("yppairs", "read_neighbors", srcs=ref("ymsgs"), etype="isLocatedIn", dir="out",
             pairs=True),
        # node 50 AppendVertices Place [name], dedup: 1
        Step("yplc", "pluck", rows=ref("yppairs"), field="dst"),
        Step("cnty_names", "read_vertex_props", vids=ref("yplc"), label="Place", props=["name"]),
    ] + _ic3_counts("ympairs", "yppairs", "ymtags", cy, lo, hi, "cnty") + [
        # nodes 57, 42, 43: cntY per survivor, keep cntY > 0 (in memory, above)
        Step("sy", "pluck", rows=ref("cnty"), field="item"),
        # node 28 Traverse isLocatedIn OUT from the cntY > 0 others: vertexProps Person (1 GET
        # per source) + 1 SEEK + 1 NEXT per edge
        Step("lsrc", "read_vertex_props", vids=ref("sy"), label="Person",
             props=["firstName", "lastName"]),
        Step("lpairs", "read_neighbors", srcs=ref("sy"), etype="isLocatedIn", dir="out",
             pairs=True),
        # node 51 AppendVertices Place [name], dedup: 1
        Step("lplc", "pluck", rows=ref("lpairs"), field="dst"),
        Step("lnames", "read_vertex_props", vids=ref("lplc"), label="Place", props=["name"]),
        # node 58 Filter loc.name not X/Y, 46/59 join, 37 Project, 38 TopN 20: in memory
        Step("lj", "join", left=ref("lpairs"), right=ref("lnames"), left_on="dst",
             right_on="_vid"),
        Step("lok", "filter", rows=ref("lj"),
             pred=and_(ne(get("name"), cx), ne(get("name"), cy))),
        Step("lokv", "pluck", rows=ref("lok"), field="src"),
        Step("cxr", "rename", rows=ref("cntx"), names={"count": "cntX"}),
        Step("cyr", "rename", rows=ref("cnty"), names={"count": "cntY"}),
        Step("cyk", "filter_in", rows=ref("cyr"), field="item", allowed=ref("lokv")),
        Step("both", "join", left=ref("cyk"), right=ref("cxr"), left_on="item", right_on="item"),
        Step("named", "join", left=ref("both"), right=ref("oprops"), left_on="item",
             right_on="_vid"),
        Step("total", "derive", rows=ref("named"), into="count",
             fn=add(field("cntX"), field("cntY"))),
        # ORDER BY `count` DESC, personId ASC: personId is id(other), Nebula's STRING vid ("person_<id>"),
        # so a count tie sorts as strings, not numbers (engine/data/queries/nebula/interactive-complex-3.gql)
        Step("vidstr", "derive", rows=ref("total"), into="_vidstr", fn=E.concat("person_", E.field("id"))),
        Step("top", "top_k", rows=ref("vidstr"), keys=[("count", "desc"), ("_vidstr", "asc")], k=20),
        # LDBC output columns (in memory)
        Step("labeled", "rename", rows=ref("top"),
             names={**PERSON_RENAME, "cntX": "xCount", "cntY": "yCount"}),
        Step("out", "project", rows=ref("labeled"), fields=IC3_FIELDS),
    ]
    return steps


def ic3_dgraph_engine(args):
    """IC3 dgraph, from dgraph/ic3/p1/plan.txt (blocks 0-10).

    `eq(type, "country")` / `eq(type, "city")` read ONE index posting each (eqfilterpath=index).
    The evaluator's store has no `type` index, so that SEEK lands on the dgraph.type Place
    posting instead -- same op count, different key (as the anchor's fqid stands in for `id`).

    The ge/lt creationDate filters are read per vertex: the live baseline runs at
    ineqfilterpath=per_vertex (the sf0.3 capture pinned `index`; same plan tree).

    Engine semantics kept as-is: validFriends is NOT pruned by its isLocatedIn child filter (no
    @cascade), so residents of X/Y are not excluded.

    sf0.3 vs live: S exact, G -4 at every param (the split Comment type posting, read twice --
    see ic2.py)."""
    p = person_vid(args["person"])
    lo = int(args["startDate"])
    hi = lo + int(args["durationDays"]) * 86400000
    steps = _anchor_person(p) + [      # block 0: eq(id) + type(person)
        # blocks 1-2: eq(name, $country) (1 index SEEK) @filter(eq(type, "country")) (1 SEEK)
        Step("cxv", "read_index_eq", label="Place", prop="name", value=args["countryXName"]),
        Step("cx", "read_filter_by_label_index", vids=ref("cxv"), label="Place"),
        Step("cyv", "read_index_eq", label="Place", prop="name", value=args["countryYName"]),
        Step("cy", "read_filter_by_label_index", vids=ref("cyv"), label="Place"),
        # block 3: knows + ~knows from P; block 4: the same from uid(a, b)
        Step("l1", "read_neighbors", srcs=ref("me"), etype="knows", dir="both"),
        Step("l2", "read_neighbors", srcs=ref("l1"), etype="knows", dir="both"),
        # block 5: FOF = uid(a,b,c,d) @filter(type(person) (1 SEEK) AND NOT uid(P))
        Step("all", "concat", a=ref("l1"), b=ref("l2")),
        Step("alld", "dedup", items=ref("all")),
        Step("typed", "read_filter_by_label_index", vids=ref("alld"), label="Person"),
        Step("fof", "exclude", items=ref("typed"), remove=[p]),
        # block 6: uid(CX, CY) @filter(eq(type,"country")) (1 SEEK), ~isPartOf per country,
        # @filter(eq(type,"city")) (1 SEEK)
        Step("ctry0", "concat", a=ref("cx"), b=ref("cy")),
        Step("ctry", "read_filter_by_label_index", vids=ref("ctry0"), label="Place"),
        Step("cities0", "read_neighbors", srcs=ref("ctry"), etype="isPartOf", dir="in"),
        Step("cities", "read_filter_by_label_index", vids=ref("cities0"), label="Place"),
        # block 7: validFriends = uid(FOF) @filter(type(person)) (1 SEEK); child isLocatedIn per
        # friend (1 SEEK each) @filter(eq(type,"city") (1 SEEK) AND NOT uid(cities) (in memory))
        Step("vf", "read_filter_by_label_index", vids=ref("fof"), label="Person"),
        Step("oloc", "read_neighbors", srcs=ref("vf"), etype="isLocatedIn", dir="out"),
        Step("olocc", "read_filter_by_label_index", vids=ref("oloc"), label="Place"),
    ]
    # blocks 8 and 9: one COUNT-ONLY pass per country over the same messages
    for tag, country in (("x", "cx"), ("y", "cy")):
        steps += [
            # ~hasCreator per validFriend (1 posting SEEK each)
            Step(f"m_{tag}", "read_neighbors", srcs=ref("vf"), etype="hasCreator", dir="in",
                 pairs=True),
            Step(f"mv_{tag}", "pluck", rows=ref(f"m_{tag}"), field="dst"),
            # @filter[or](type(post), type(comment)): 1 type-index SEEK each
            Step(f"tp_{tag}", "read_filter_by_label_index", vids=ref(f"mv_{tag}"), label="Post"),
            Step(f"tc_{tag}", "read_filter_by_label_index", vids=ref(f"mv_{tag}"), label="Comment"),
            # ge(creationDate) and lt(creationDate): two filter nodes, each reads the date
            Step(f"d1_{tag}", "read_vertex_props", vids=ref(f"mv_{tag}"), props=["creationDate"]),
            Step(f"d2_{tag}", "read_vertex_props", vids=ref(f"mv_{tag}"), props=["creationDate"]),
            # isLocatedIn uid_in(<country>): one isLocatedIn posting read per message
            Step(f"ml_{tag}", "read_neighbors", srcs=ref(f"mv_{tag}"), etype="isLocatedIn",
                 dir="out", pairs=True),
            # in memory: the conjunction, counted per author
            Step(f"win_{tag}", "filter", rows=ref(f"d2_{tag}"),
                 pred=and_(notnull(field("creationDate")), le(lo, field("creationDate")),
                           lt(field("creationDate"), hi))),
            Step(f"winv_{tag}", "pluck", rows=ref(f"win_{tag}"), field="_vid"),
            Step(f"inc_{tag}", "filter_in", rows=ref(f"ml_{tag}"), field="dst",
                 allowed=ref(country)),
            Step(f"okm_{tag}", "filter_in", rows=ref(f"inc_{tag}"), field="src",
                 allowed=ref(f"winv_{tag}")),
            Step(f"okv_{tag}", "pluck", rows=ref(f"okm_{tag}"), field="src"),
            Step(f"auth_{tag}", "filter_in", rows=ref(f"m_{tag}"), field="dst",
                 allowed=ref(f"okv_{tag}")),
            Step(f"av_{tag}", "pluck", rows=ref(f"auth_{tag}"), field="src"),
            Step(f"cnt_{tag}", "group_count", items=ref(f"av_{tag}")),
        ]
    steps += [
        # block 10: gt(val(rcx),0) AND gt(val(rcy),0), orderdesc val(total), first 20 -- in memory
        Step("cxr", "rename", rows=ref("cnt_x"), names={"count": "cntX"}),
        Step("cyr", "rename", rows=ref("cnt_y"), names={"count": "cntY"}),
        Step("both", "join_multi", left=ref("cxr"), right=ref("cyr"),
             left_on="item", right_on="item"),
        Step("total", "derive", rows=ref("both"), into="count",
             fn=add(field("cntX"), field("cntY"))),
        Step("top", "top_k", rows=ref("total"), keys=[("count", "desc"), ("item", "asc")], k=20),
        Step("topv", "pluck", rows=ref("top"), field="item"),
        # block 10: friendId: id, friendFirstName, friendLastName for the <= 20 survivors
        Step("props", "read_vertex_props", vids=ref("topv"), props=["id", "firstName", "lastName"]),
        Step("named", "join", left=ref("top"), right=ref("props"),
             left_on="item", right_on="_vid"),
        Step("ranked", "top_k", rows=ref("named"), keys=[("count", "desc"), ("id", "asc")], k=20),
        # LDBC output columns (in memory)
        Step("labeled", "rename", rows=ref("ranked"),
             names={**PERSON_RENAME, "cntX": "xCount", "cntY": "yCount"}),
        Step("out", "project", rows=ref("labeled"), fields=IC3_FIELDS),
    ]
    return steps


SIM = {
    "ic3": ic3,
}

def _ic3_arango_count(side, friends, country, lo, hi):
    """One of IC3's two count subqueries ([41]+[44] for X, [52]+[55] for Y): per friend, its
    messages in the window located in `country`. The two subqueries do not share work."""
    return [
        # [41]/[52] TraversalNode hasCreator INBOUND f, vertexProjections [_id, creationDate],
        # postFilter lo <= msg.creationDate < hi: traverse_filtered (the friend, every
        # message, every in-window message again)
        Step(f"{side}_m", "traverse_filtered", srcs=ref(friends), etype="hasCreator", dir="in",
             depth=1, props=["id", "creationDate"], label=MESSAGE_LABELS,
             pred=E.and_(E.notnull(E.get("creationDate")), E.le(lo, E.field("creationDate")),
                         E.lt(E.field("creationDate"), hi))),
        # [44]/[55] TraversalNode isLocatedIn OUTBOUND msg, vertexProjections [_id], postFilter
        # msgPlace._id == country._id (the FilterNode after it has no LIMIT above: no cut)
        Step(f"{side}_mv", "pluck", rows=ref(f"{side}_m"), field="_vid"),
        Step(f"{side}_l", "traverse_filtered", srcs=ref(f"{side}_mv"), etype="isLocatedIn",
             dir="out", depth=1, pred=E.lit(True), allowed=ref(country), props=["id"],
             label="Place"),
        # [50]/[61] LENGTH(...) per friend: in memory
        Step(f"{side}_lm", "pluck", rows=ref(f"{side}_l"), field="_src"),
        Step(f"{side}_ok", "filter_in", rows=ref(f"{side}_m"), field="_vid",
             allowed=ref(f"{side}_lm")),
        Step(f"{side}_f", "pluck", rows=ref(f"{side}_ok"), field="_src"),
        Step(f"{side}_c", "group_count", items=ref(f"{side}_f")),
    ]


def ic3_arangodb_engine(args):
    """IC3 arango, from arangodb/ic3/p1/plan.txt.

    No date index: the window is a postFilter on the hasCreator traversal, so every message of
    every friend is fetched (pk + doc) to test its date, and the two count subqueries ([41]
    for X, [52] for Y) each walk every friend's messages again. The friend walk [28] (knows
    1..2, postFilter friend._id != start) emits one row per PATH; [32] reads each row's city
    before COLLECT f = friend dedups them.

    sf0.3 vs live: S, G and N exact at all 6 params (p1: S=8509 G=733314 N=351085). The answer
    is EMPTY at all 6 params, live and sim alike (no friend posted in both countries in the
    window), so the in-memory tail after the counts is not exercised by these params."""
    p = person_vid(args["person"])
    cx, cy = args["countryXName"], args["countryYName"]
    lo = int(args["startDate"])
    hi = lo + int(args["durationDays"]) * 86400000
    return [
        # [76]/[77] IndexNode Place persistent(name), no projections, [74]/[72] LIMIT 1:
        # 1 SEEK + 1 NEXT + the document each
        Step("cx", "read_index_eq", label="Place", prop="name", value=cx),
        Step("cy", "read_index_eq", label="Place", prop="name", value=cy),
        # [19]/[24] TraversalNode isPartOf INBOUND country, vertexProjections [_id], no filter:
        # the first traversal (+1 open SEEK); each city fetched (pk + doc)
        Step("open", "traversal_open", items=ref("cx"), etype="isPartOf"),
        Step("citx", "read_neighbors", srcs=ref("open"), etype="isPartOf", dir="in"),
        Step("citxd", "docs_per_step", vids=ref("citx"), label="Place", props=["id"]),
        Step("city", "read_neighbors", srcs=ref("cy"), etype="isPartOf", dir="in"),
        Step("cityd", "docs_per_step", vids=ref("city"), label="Place", props=["id"]),
        # [28] TraversalNode knows IN,OUT 1..2 from the start string, no vertexProjections,
        # postFilter friend._id != start: traverse_filtered, one row per path
        Step("fr", "traverse_filtered", srcs=[p], etype="knows", dir="both", depth=2,
             props=["id", "firstName", "lastName"], label="Person",
             pred=E.ne(E.field("_vid"), p)),
        # [32] TraversalNode isLocatedIn OUTBOUND friend, vertexProjections [_id], [71] LIMIT 1
        # right on it (the cut; one isLocatedIn per person, so nothing is left to cut): once per
        # PATH row, not per distinct friend -- 1 SEEK + 1 NEXT + the city document each
        # (traverse_paths, which does not dedup its sources; read_neighbors would)
        Step("frv", "pluck", rows=ref("fr"), field="_vid"),
        Step("fcity", "traverse_paths", srcs=ref("frv"), etype="isLocatedIn", dir="out", depth=1),
        Step("fcityd", "docs_per_step", vids=ref("fcity"), label="Place", props=["id"]),
        # [37]/[38] FILTER friendCity NOT IN citiesX AND NOT IN citiesY, [39] COLLECT f = friend,
        # [70] SORT f: in memory. One city per person, so the city list lines up with the rows.
        Step("bad", "concat", a=ref("citx"), b=ref("city")),
        Step("fcityn", "project", rows=ref("fcityd"), fields=["_vid"]),
        Step("fcityr", "rename", rows=ref("fcityn"), names={"_vid": "_city"}),
        Step("frc", "zip_rows", left=ref("fr"), right=ref("fcityr")),
        Step("frb", "flag_in", rows=ref("frc"), field="_city", allowed=ref("bad"),
             into="_in_xy"),
        Step("fr_ok", "filter", rows=ref("frb"), pred=E.not_(E.field("_in_xy"))),
        Step("fr_u", "dedup_rows", rows=ref("fr_ok"), field="_vid"),
        Step("f", "pluck", rows=ref("fr_u"), field="_vid"),
    ] + _ic3_arango_count("x", "f", "cx", lo, hi) + _ic3_arango_count("y", "f", "cy", lo, hi) + [
        # [62]/[63] FILTER xCount > 0 AND yCount > 0, [66]/[67] SORT count DESC, id ASC LIMIT 20
        Step("xr", "rename", rows=ref("x_c"), names={"count": "xCount"}),
        Step("yr", "rename", rows=ref("y_c"), names={"item": "_y", "count": "yCount"}),
        Step("yv", "pluck", rows=ref("y_c"), field="item"),
        Step("xy0", "filter_in", rows=ref("xr"), field="item", allowed=ref("yv")),
        Step("xy", "join", left=ref("xy0"), right=ref("yr"), left_on="item", right_on="_y"),
        Step("summed", "derive", rows=ref("xy"), into="count",
             fn=add(field("xCount"), field("yCount"))),
        Step("frn", "rename", rows=ref("fr_u"),
             names={"id": "personId", "firstName": "personFirstName",
                    "lastName": "personLastName"}),
        Step("named", "join", left=ref("summed"), right=ref("frn"), left_on="item",
             right_on="_vid"),
        Step("top20", "top_k", rows=ref("named"), keys=[("count", "desc"), ("personId", "asc")],
             k=20),
        Step("out", "project", rows=ref("top20"),
             fields=IC3_FIELDS),
    ]


LABEL_ONLY = {
    "ic3": ic3_labelonly
}

ENGINE = {
    "ic3": {"nebula": ic3_nebula_engine, "dgraph": ic3_dgraph_engine,
            "arangodb": ic3_arangodb_engine}
}

# what predict's cardinality rules may assume for these queries (plan file field estimate_hints, read by
# predict/cardinality.cc): a hop over knows from a set of persons reached through knows prices each person's degree as
# a neighbour's (sum d^2 / sum d), except the anchor's own first hop
_KNOWS_HINTS = {"size_biased": ["knows"]}
ESTIMATE_HINTS = {"ic3": _KNOWS_HINTS, "ic3_getdate": _KNOWS_HINTS}
