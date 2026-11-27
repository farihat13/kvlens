"""BI12 How many persons have a given number of messages (spec 8.4 BI/read/12; reference neo4j/queries/bi-12.cypher)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir import authoring as E
from kvevaluator.workloads.bi._shared import (MESSAGE_LABELS, HAS_CONTENT_FIELD, message_window_steps,
                                               message_scan_steps, chase_levelwise_steps, with_label_run)

FAR_FUTURE = 4102444800000   # 2100-01-01 UTC: the open upper bound of "created after $startDate"


def bi12(args):
    """Per Person, the number of Messages that have content, were created after $startDate (strict), have
    length < $lengthThreshold (strict) and are written in one of $languages -- a Comment's language is its root
    Post's (the reference: `(message)-[:REPLY_OF*0..]->(post:Post)`, `post.language IN $languages`). Then, per
    messageCount value, the number of Persons with exactly that count; every Person counts (the reference's
    OPTIONAL MATCH: a Person with no qualifying message has messageCount 0). Sorted personCount desc,
    messageCount desc.

    Entry as BI1 (the creationDate index window ($startDate, +inf) on both labels; three bodies by capability,
    `message_window_steps`), length and date per message, the comments' root Post by read_chase (replyOf to
    the end), the root's language read once per distinct root; hasCreator per qualifying message; the person
    histogram over the whole Person label (read_vertices_by_label + left_join default 0). The root chase is the
    LEVEL-WISE one (chase_levelwise_steps: one read_neighbors over the frontier per level, each chain node read
    once); `bi12_source` is the recorded twin with read_chase (one source at a time: on a chunked adjacency a whole
    chunk per source per hop -- an upper bound, the record of that op's cost); `bi12_scan` is the type-pass twin."""
    return _bi12(args, entry="index", typed="probe", chase="level")


def bi12_dgraph(args):
    """dgraph's body: the per-predicate day index returns every label's vertices in the window; the type postings
    restrict it (as bi1_dgraph)."""
    return _bi12(args, entry="index", typed="posting", chase="level")


def bi12_scoped(args):
    """The label-scoped body (as bi1_scoped): the Post-label read on the window, the Comment-label read on the rest."""
    return _bi12(args, entry="index", typed="scoped", chase="level")


def bi12_scan(args):
    """The type-pass twin (as bi1_scan): enumerate both labels, read the date with the props, filter the date."""
    return _bi12(args, entry="scan", typed=None, chase="level")


def bi12_source(args):
    """The per-source chase twin (read_chase), kept as the record of what the per-source op costs."""
    return _bi12(args, entry="index", typed="probe", chase="source")


def bi12_source_dgraph(args):
    return _bi12(args, entry="index", typed="posting", chase="source")


def bi12_source_scoped(args):
    return _bi12(args, entry="index", typed="scoped", chase="source")


def _bi12(args, entry, typed, chase):
    start, thr, langs = E.param("startDate"), E.param("lengthThreshold"), E.param("languages")
    # no `language` here: a message's language is its ROOT Post's, read once per distinct root in `root_lang`; the
    # window read asked for it per message and nothing used it (on dgraph one more predicate read per message)
    props = ["creationDate", "length"]
    if entry == "index":
        head = [Step("win", "read_index_range", labels=list(MESSAGE_LABELS), prop="creationDate",
                     lo=E.let("after"), hi=E.lit(FAR_FUTURE))]
        head += message_window_steps("win", props, typed, out="props")
    else:
        head = message_scan_steps(props, out="props")
    return Plan(head + [
        # strict after $startDate (dgraph's day superset trimmed), content present, length below the threshold
        Step("qual",     "filter", rows=ref("props"),
             pred=E.and_(E.gt(E.field("creationDate"), start), E.gt(E.field(HAS_CONTENT_FIELD), E.lit(0)),
                         E.lt(E.field("length"), thr))),
        Step("qual_v",   "pluck", rows=ref("qual"), field="_vid"),
    ] + (
        # a Comment's language is its root Post's: chase replyOf to the end (a Post roots itself)
        [Step("roots",    "read_chase", srcs=ref("qual_v"), etype="replyOf", dir="out"),
         Step("rooted",   "attach", rows=ref("qual"), values=ref("roots"), into="_root")] if chase == "source"
        else chase_levelwise_steps("qual", "replyOf", out="rooted")
    ) + [
        Step("roots_v",  "pluck", rows=ref("rooted"), field="_root"),
        Step("root_d",   "dedup", items=ref("roots_v")),
        Step("root_lang", "read_vertex_props", vids=ref("root_d"), props=["language"], label="Post"),
        Step("root_named", "rename", rows=ref("root_lang"), names={"language": "_rootLanguage"}),
        Step("with_lang", "join", left=ref("rooted"), right=ref("root_named"), left_on="_root", right_on="_vid"),
        Step("in_lang",  "filter", rows=ref("with_lang"), pred=E.in_(E.field("_rootLanguage"), langs)),
        Step("in_lang_v", "pluck", rows=ref("in_lang"), field="_vid"),
        Step("creators", "read_neighbors", srcs=ref("in_lang_v"), etype="hasCreator", dir="out"),
        Step("per_person", "group_count", items=ref("creators")),
        Step("pp_named", "rename", rows=ref("per_person"), names={"item": "_vid", "count": "messageCount"}),
        # every Person, those with no qualifying message at count 0
        Step("persons",  "read_vertices_by_label", label="Person"),
        Step("person_rows", "annotate", items=ref("persons"), field="_k", value=1),
        Step("counts",   "left_join", left=ref("person_rows"), right=ref("pp_named"), left_on="_vid", right_on="_vid",
             default={"messageCount": 0}),
        Step("hist",     "group_agg", rows=ref("counts"), by=["messageCount"], aggs={"personCount": ["count", None]}),
        Step("sorted",   "top_k", rows=ref("hist"), keys=[("personCount", "desc"), ("messageCount", "desc")], k=None),
        Step("out",      "project", rows=ref("sorted"), fields=["messageCount", "personCount"]),
    ], params={"startDate": {"type": "int"}, "lengthThreshold": {"type": "int"}, "languages": {"type": "list"}},
       let={"after": E.add(start, E.lit(1))})      # the window is OPEN at $startDate: the index range starts 1 ms after


def bi12_scan_run(args):
    """The label-run twin of bi12_scan: the roots' languages read by one kept scan of the Post key run
    (_shared.with_label_run), not one point read per distinct root."""
    return with_label_run(bi12_scan(args), {"root_lang"})


SIM = {"bi12": {"default": bi12, "dgraph": bi12_dgraph, "nebula": bi12_scoped, "arangodb": bi12_scoped,
                "pertype_csr": bi12_scoped},
       "bi12_scan": bi12_scan,
       "bi12_scan_run": bi12_scan_run,
       "bi12_source": {"default": bi12_source, "dgraph": bi12_source_dgraph, "nebula": bi12_source_scoped,
                       "arangodb": bi12_source_scoped, "pertype_csr": bi12_source_scoped}}
