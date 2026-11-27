"""The per-fragment WIN demo: two fragments on two types, each cheap ONLY in its own parent's plan shape, so a store
with a layout per type that matches each fragment beats both pure formats.

    fof_posters_profile(person): the 2-hop reach's messages, kept to Posts, their authors, and
    the profile (id, firstName, lastName) of every author born on or after 1985-01-01.

Fragment A -- the TYPE FILTER on the Message set (the logical filter_by_label, lowered by capability):
    dgraph's path    read_filter_by_label_index   1 SEEK into the type posting, intersect in RAM
    nebula's paths   read_filter_by_label_probe   1 GET probe PER candidate message
                     read_filter_by_label_index   whole-type SCAN of its per-type index, 1 SEEK + |Post| NEXT
Fragment B -- the authors' PROFILE, filtered on birthday:
    dgraph's shape   two-phase: birthday per author (1 GET each), then names for the survivors (1 GET each)
    nebula's shape   one-phase: the whole row per author (1 GET each) carries everything

Nebula's registry entry takes the whole-type scan for fragment A: the candidates outnumber |Post|/100, so the
probe would over-charge nebula. The probe body stays in the bundle as a measured candidate. With Message on
dgraph's layout and Person on nebula's, the per-label plan (index filter + one-phase) pays neither parent's cost;
on the plan-shape question it ties nebula's shape, whose index op on dgraph-laid-out Posts IS dgraph's index path.
"""
from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import let, field, ge, lit
from kvevaluator.workloads.microbench._shared import PERSON_PARAM, PERSON_LET

# the birthday split: 1985-01-01 UTC sits inside LDBC's birthday range (~1980-1995), so
# roughly half the authors survive the filter -- the two-phase re-read is then half the
# authors, not none of them (the shared DATE_CUTOFF is a 2011 creation-date bound: every
# birthday is before it, and the demo returned no rows)
BIRTHDAY_CUTOFF = 473385600000


def _reach_posters(via=None):
    """The shared prefix: anchor -> 2-hop reach -> their messages -> Posts -> the posters. The
    type filter is the LOGICAL filter_by_label: the registry (with a schema) and the IR dumper
    lower it to the format's access path by capability (kvevaluator.plans.ir.lowering) -- dgraph's index op
    under a design with the type index, the probe otherwise."""
    return [
        Step("a",     "read_vertex_by_id", vid=let("anchor_vid"), label="Person"),
        Step("f1",    "read_neighbors", srcs=ref("a"), etype="knows", dir="both"),
        Step("f1d",   "dedup", items=ref("f1")),
        Step("f2",    "read_neighbors", srcs=ref("f1d"), etype="knows", dir="both"),
        Step("reach", "dedup", items=ref("f2")),
        # (person, message) pairs: the poster is known from the read, so no SEEK per post to
        # find its author -- that re-read is fragment-neutral and would swamp both fragments
        Step("msgs",  "read_neighbors", srcs=ref("reach"), etype="hasCreator", dir="in", pairs=True),
        Step("mv",    "pluck", rows=ref("msgs"), field="dst"),
        Step("msgd",  "dedup", items=ref("mv")),
        Step("posts", "filter_by_label", vids=ref("msgd"), label="Post", **({"via": via} if via else {})),
        Step("pp",    "filter_in", rows=ref("msgs"), field="dst", allowed=ref("posts")),
        Step("auth",  "pluck", rows=ref("pp"), field="src"),
        Step("authors", "dedup", items=ref("auth")),
    ]


def _two_phase():
    return [
        Step("born",  "read_vertex_props", vids=ref("authors"), props=["birthday"], label="Person"),
        Step("young", "filter", rows=ref("born"), pred=ge(field("birthday"), lit(BIRTHDAY_CUTOFF))),
        Step("yv",    "pluck", rows=ref("young"), field="_vid"),
        Step("prof",  "read_vertex_props", vids=ref("yv"), props=["id", "firstName", "lastName"],
             label="Person"),
        Step("out",   "project", rows=ref("prof"), fields=["id", "firstName", "lastName"]),
    ]


def _one_phase():
    return [
        Step("born",  "read_vertex_props", vids=ref("authors"),
             props=["id", "firstName", "lastName", "birthday"], label="Person"),
        Step("young", "filter", rows=ref("born"), pred=ge(field("birthday"), lit(BIRTHDAY_CUTOFF))),
        Step("out",   "project", rows=ref("young"), fields=["id", "firstName", "lastName"]),
    ]


def _one_phase_from_person_run():
    """_one_phase with the authors' rows read by ONE scan of the Person label's key run kept to the authors, in place of
    a point read per author; the rows are put back in the authors' order in memory, so the answer equals the base's.
    """
    return [
        Step("scan",  "read_vertices_by_label", label="Person",
             props=["id", "firstName", "lastName", "birthday"], keep=ref("authors")),
        Step("inorder", "annotate", items=ref("authors"), field="author", value=1),
        Step("born",  "join", left=ref("inorder"), right=ref("scan"), left_on="_vid"),
        Step("young", "filter", rows=ref("born"), pred=ge(field("birthday"), lit(BIRTHDAY_CUTOFF))),
        Step("out",   "project", rows=ref("young"), fields=["id", "firstName", "lastName"]),
    ]


def _plan(profile, via=None):
    return Plan(_reach_posters(via) + profile(), params=PERSON_PARAM, let=PERSON_LET)


def fof_posters_profile_scan_onephase(args):
    """The row stores' shape with the whole-type-scan filter: via="index" lowers nebula's type filter to the SCAN of
    its per-type index (1 SEEK + |Post| NEXT) instead of a probe per candidate; one-phase profile."""
    return _plan(_one_phase, via="index")


def fof_posters_profile(args):
    """dgraph's shape (the default): two-phase profile; the type filter lowers to the index op."""
    return _plan(_two_phase)


def fof_posters_profile_onephase(args):
    """The row stores' shape: one-phase profile; the type filter lowers to nebula's probe or arango's free index op, so
    arango's body is the COMBINED shape.
    """
    return _plan(_one_phase)


def fof_posters_profile_from_person_run(args):
    """The PERSON-RUN twin: the authors' profile read as one sequential scan of Person kept to the authors, one-phase on
    every layout. Valid where the label's records are one key run (`label_rows_one_run`); picked by cost.
    """
    return _plan(_one_phase_from_person_run)


def fof_posters_profile_from_person_run_scan(args):
    """The same twin on the whole-type-scan filter path (the body the formats on nebula's plans take)."""
    return _plan(_one_phase_from_person_run, via="index")


# ================================================================ engine plans
# Transcribed from the engines' captured sf0.3 plans (microbench/<db>/<shape>/p<train>/plan.txt), at the training
# param only (blind holdout: live_workloads.train_param).
# One section per engine; each cites the plan node behind every KV step.


# ------------------------------------------------------------ dgraph engine plans

from kvevaluator.workloads.microbench._shared import dgraph_engine_anchor


def fof_posters_profile_dgraph_engine(args):
    """From dgraph/fof_posters_profile/p3/plan.txt.
    Block 0: the id anchor; `knows`, `~knows` (1 posting read each).
    Block 1: uid(f1, r1); `knows`, `~knows` per friend (2 posting reads each).
    Block 2: uid(f2, r2) -- the reach, anchor and friends included; `count(~hasCreator
        @filter(type(post)))` per reached person: 1 ~hasCreator posting read each, and the
        type(post) filter is ONE post type-posting read for the whole block (typefilterpath=index).
    Block 3: authors = nposts > 0 -- in memory.
    Block 4: `@filter(birthday ge(...))` [vals=n]: per author at ineqfilterpath=per_vertex, 1 GET
        each; then `id`, `firstName`, `lastName` per survivor, 1 GET each."""
    return Plan(dgraph_engine_anchor("Person") + [                    # block 0: eq(id) + type(person)
        Step("f1",    "read_neighbors", srcs=ref("me"), etype="knows", dir="both"),   # block 0
        Step("f1d",   "dedup", items=ref("f1")),                       # block 1: uid(f1, r1)
        Step("f2",    "read_neighbors", srcs=ref("f1d"), etype="knows", dir="both"),  # block 1
        Step("reach", "dedup", items=ref("f2")),                       # block 2: uid(f2, r2)
        Step("msgs",  "read_neighbors", srcs=ref("reach"), etype="hasCreator", dir="in",
             pairs=True),                                              # block 2: ~hasCreator
        Step("mv",    "pluck", rows=ref("msgs"), field="dst"),
        Step("posts", "read_filter_by_label_index", vids=ref("mv"), label="Post"),   # block 2: type(post)
        Step("pp",    "filter_in", rows=ref("msgs"), field="dst", allowed=ref("posts")),
        Step("auth",  "pluck", rows=ref("pp"), field="src"),
        Step("authors", "dedup", items=ref("auth")),                   # block 3: nposts > 0
        Step("born",  "read_vertex_props", vids=ref("authors"), props=["birthday"]),  # block 4: ge filter
        Step("young", "filter", rows=ref("born"), pred=ge(field("birthday"), lit(BIRTHDAY_CUTOFF))),
        Step("yv",    "pluck", rows=ref("young"), field="_vid"),
        Step("prof",  "read_vertex_props", vids=ref("yv"),
             props=["id", "firstName", "lastName"]),                   # block 4: id, firstName, lastName
        Step("out",   "project", rows=ref("prof"), fields=["id", "firstName", "lastName"]),
    ], params=PERSON_PARAM, let=PERSON_LET)


ENGINE_DGRAPH = {"fof_posters_profile": fof_posters_profile_dgraph_engine}
ENGINE_DRAFT_DGRAPH = {}


# ------------------------------------------------------------ nebula engine plans

from kvevaluator.plans.ir.authoring import notnull  # (nebula section only)


def fof_posters_profile_nebula_engine(args):
    """From nebula/fof_posters_profile/p3/plan.txt. Node 3 Expand: GO 2 STEPS over KNOWS both
    directions (types -12, 12); step 2 starts from the distinct step-1 vertices. Node 4 Dedup
    gives the reach. Node 8 Expand has maxSteps 0 (no read, ExpandExecutor.cpp:50); node 9
    ExpandAll walks HAS_CREATOR reverse (type -20) from every reach vertex. Node 11 GetVertices:
    tagId 3 (Post), props [creationDate], dedup 1 -- 1 GET per message, plus storaged's prefix
    scan of the vid for each message that is not a Post (NebulaEngineFormat.read_vertex_props).
    Nodes 26 HashLeftJoin, 27 Filter IS NOT EMPTY and 28 HashInnerJoin keep the Post edges;
    YIELD DISTINCT dst(edge) is the distinct authors. Node 23 GetVertices: tagId 2 (Person),
    every prop, dedup 1 -- 1 GET per author. Node 24 Filter birthday and node 29 Project are in
    memory."""
    return Plan([
        Step("a",     "read_vertex_by_id", vid=let("anchor_vid")),     # node 1 Project: the vid
        Step("f1",    "read_neighbors", srcs=ref("a"), etype="knows", dir="both"),   # node 3 step 1
        Step("f1d",   "dedup", items=ref("f1")),
        Step("f2",    "read_neighbors", srcs=ref("f1d"), etype="knows", dir="both"), # node 3 step 2
        Step("reach", "dedup", items=ref("f2")),                       # node 4 Dedup
        Step("msgs",  "read_neighbors", srcs=ref("reach"), etype="hasCreator", dir="in",
             pairs=True),                                              # node 9 ExpandAll
        Step("mv",    "pluck", rows=ref("msgs"), field="dst"),
        Step("pv",    "read_vertex_props", vids=ref("mv"), props=["creationDate"],
             label="Post"),                                            # node 11 GetVertices
        Step("po",    "filter", rows=ref("pv"),
             pred=notnull(field("creationDate"))),                     # node 27 Filter
        Step("posts", "pluck", rows=ref("po"), field="_vid"),
        Step("pp",    "filter_in", rows=ref("msgs"), field="dst",
             allowed=ref("posts")),                                    # nodes 26/28 joins
        Step("auth",  "pluck", rows=ref("pp"), field="src"),
        Step("authors", "dedup", items=ref("auth")),                   # YIELD DISTINCT dst(edge)
        Step("born",  "read_vertex_props", vids=ref("authors"),
             props=["id", "firstName", "lastName", "birthday"],
             label="Person"),                                          # node 23 GetVertices
        Step("young", "filter", rows=ref("born"),
             pred=ge(field("birthday"), lit(BIRTHDAY_CUTOFF))),        # node 24 Filter
        Step("out",   "project", rows=ref("young"),
             fields=["id", "firstName", "lastName"]),                  # node 29 Project
    ], params=PERSON_PARAM, let=PERSON_LET)


ENGINE_NEBULA = {"fof_posters_profile": fof_posters_profile_nebula_engine}
ENGINE_DRAFT_NEBULA = {}


# ------------------------------------------------------------ arangodb engine plans

from kvevaluator.plans.ir.authoring import eq as _eq, get as _get  # (arangodb section only)

# a message vid's collection is resolved per vertex (the engine read it off the edge's _id)
_ARANGO_MESSAGE_COLLECTIONS = ["Post", "Comment"]


def fof_posters_profile_arangodb_engine(args):
    """From arangodb/fof_posters_profile/p3/plan.txt.

      [4]  TraversalNode knows ANY from the anchor, 1..1, vertexProjections [_id]: the first
           traversal's extra SEEK, 2 SEEK + 1 NEXT per edge, each friend's document per step.
      [5]  TraversalNode knows ANY from f, 1..1, vertexProjections [_id], run once per [4]
           row (no dedup across friends): traverse_paths, and the reached document per step.
      [9]  UNIQUE(reach): in memory. [11] EnumerateList over it.
      [12] TraversalNode hasCreator INBOUND per reach person, vertexProjections [_id],
           postFilter IS_SAME_COLLECTION("Post", m._id): traverse_filtered (the start person
           and every message fetched for the filter, every Post fetched again).
      [17] UNIQUE(authors): in memory. [18] EnumerateList.
      [19] DOCUMENT(a) per distinct author: pk + doc. [20]/[21] the birthday filter and
      [22] the {id, firstName, lastName} object are in memory."""
    return Plan([
        Step("open",    "traversal_open", items=[let("anchor_vid")], etype="knows"),  # [4] first traversal
        Step("f",       "read_neighbors", srcs=ref("open"), etype="knows", dir="both"),  # [4]
        Step("fdoc",    "docs_per_step", vids=ref("f"), props=[], label="Person"),      # [4] vertex fetch
        Step("r",       "traverse_paths", srcs=ref("f"), etype="knows", dir="both",
             depth=1),                                                 # [5] once per [4] row
        Step("rdoc",    "docs_per_step", vids=ref("r"), props=[], label="Person"),      # [5] vertex fetch
        Step("reach",   "dedup", items=ref("r")),                      # [9] UNIQUE
        Step("posts",   "traverse_filtered", srcs=ref("reach"), etype="hasCreator", dir="in",
             depth=1, props=["id"], label=_ARANGO_MESSAGE_COLLECTIONS,
             pred=_eq(_get("_label"), lit("Post"))),                   # [12] + postFilter
        Step("auth",    "pluck", rows=ref("posts"), field="_src"),
        Step("authors", "dedup", items=ref("auth")),                   # [17] UNIQUE
        Step("born",    "docs_per_step", vids=ref("authors"), label="Person",
             props=["id", "firstName", "lastName", "birthday"]),       # [19] DOCUMENT(a)
        Step("young",   "filter", rows=ref("born"),
             pred=ge(field("birthday"), lit(BIRTHDAY_CUTOFF))),        # [20] + [21]
        Step("out",     "project", rows=ref("young"),
             fields=["id", "firstName", "lastName"]),                  # [22]
    ], params=PERSON_PARAM, let=PERSON_LET)


ENGINE_ARANGODB = {"fof_posters_profile": fof_posters_profile_arangodb_engine}
ENGINE_DRAFT_ARANGODB = {}


# what predict's cardinality rules may assume for these queries (plan file field estimate_hints, read by
# predict/cardinality.cc): a hop over knows from a set of persons reached through knows prices each person's degree as
# a neighbour's (sum d^2 / sum d), except the anchor's own first hop
_KNOWS_HINTS = {"size_biased": ["knows"]}
ESTIMATE_HINTS = {"fof_posters_profile": _KNOWS_HINTS}
