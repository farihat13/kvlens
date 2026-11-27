"""Schema mappings for the LDBC CsvBasic -> Dgraph RDF converter.

One mapping describes how to turn LDBC `CsvBasic-LongDateFormatter` CSV files into
Dgraph RDF *and* the matching `ldbcTypes.schema`. Because both outputs come from the
same spec, renaming a predicate (e.g. per-type `postCreationDate`) updates the schema
and the data together -- they can't drift.

`default` reproduces the published sf0.3 RDF format, with one deliberate change: posts
and comments share a single `msg_<id>` vid (see `Mapping.vid_overrides`) so Dgraph
matches Nebula/Arango. Variants (e.g. `per_type_dates`) are built by copying the default
and tweaking it, so they stay in sync with the base structure.

Add a new variant: write a function that returns a tweaked `default()` Mapping and
register it in MAPPINGS at the bottom.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field


# A literal column on a node. `kind` controls value conversion + schema type:
#   string   -> emitted as-is, schema `string`
#   int      -> emitted as-is, schema `int`
#   datetime -> epoch-ms -> "YYYY-MM-DDThh:mm:ss.SSS+00:00", schema `dateTime`
#   date     -> epoch-ms -> "YYYY-MM-DD", schema `dateTime`
# `schema_type` is the full Dgraph directive (type + index), e.g. "dateTime @index(day)".
@dataclass
class Column:
    csv_field: str       # header name in the CSV
    predicate: str       # RDF predicate name (the configurable part)
    kind: str            # string | int | datetime | date
    schema_type: str     # Dgraph schema directive for `predicate`


# A node type: one CSV of records, each becoming `_:{prefix}_{id}` with a
# `dgraph.type`, an `fqid`, an `id`, and one triple per literal column.
@dataclass
class Entity:
    label: str               # dgraph.type value AND schema type-block name
    prefix: str              # blank-node prefix (e.g. "person")
    csv: str                 # path under the CsvBasic root (e.g. "dynamic/person_0_0.csv")
    id_field: str            # CSV header holding the id (e.g. "id")
    out_file: str            # RDF file to write (e.g. "person_0.rdf")
    columns: list[Column]    # literal columns, EXCLUDING the id field


# A multi-valued literal kept in a separate CSV (LDBC splits person email / speaks
# into their own files). Each row is one value; the golden data routes these to
# Deltas.rdf rather than inlining them in person_0.rdf.
@dataclass
class MultiLiteral:
    csv: str
    subject_prefix: str      # "person"
    subject_field: str       # CSV header for the subject id ("Person.id")
    value_field: str         # CSV header for the value ("email" / "language")
    predicate: str           # RDF predicate ("email" / "language")
    kind: str                # string | int | datetime | date
    schema_type: str         # "[string]" etc.
    out_file: str            # "Deltas.rdf"


# An edge: one CSV of (src, dst[, facet]) rows -> `_:{src} <pred> _:{dst}`.
# Many CSVs may target the same `out_file` (e.g. comment+post hasCreator).
@dataclass
class Relationship:
    csv: str
    predicate: str
    src_prefix: str
    src_field: str           # CSV header for the source id
    dst_prefix: str
    dst_field: str           # CSV header for the destination id
    schema_type: str         # e.g. "[uid] @reverse"
    out_file: str
    facet_field: str | None = None   # CSV header of the facet value, if any
    facet_name: str | None = None    # facet predicate name in RDF
    facet_kind: str | None = None     # string | datetime  (facets are not in schema)
    # Milliseconds ADDED to a datetime facet before formatting. Used only to
    # reproduce a golden-data quirk: the upstream dgraph-benchmarks RDF stores the
    # `likes` creationDate exactly 10s earlier than the CsvBasic CSV (verified: a
    # constant -10000ms on 100% of sf0.3 edges; knows/hasMember have no such offset).
    # Set to 0 if you want the faithful CSV time instead of matching sf0.3.
    facet_offset_ms: int = 0


# `vid_overrides` remaps a node prefix to a DIFFERENT vid/fqid string at emission time,
# WITHOUT changing the schema type block (the block still groups by `prefix`) or the
# `dgraph.type`. We use it to give posts AND comments one shared `msg_` vid, matching
# Nebula/Arango (both store messages under a single `msg_` prefix). LDBC post-ids and
# comment-ids are disjoint, so `_:msg_<id>` never merges two different nodes.
@dataclass
class Mapping:
    name: str
    entities: list[Entity] = field(default_factory=list)
    multiliterals: list[MultiLiteral] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    vid_overrides: dict[str, str] = field(default_factory=dict)


# Shared schema directives, kept in one place so default + variants agree.
_DT = "dateTime @index(day)"           # creationDate, joinDate
_DT_YEAR = "dateTime @index(year)"     # birthday


def default() -> Mapping:
    """The active loader mapping: the published RDF format, with unified message vids.

    Posts and comments share one `msg_<id>` vid/fqid instead of `post_<id>`/`comment_<id>`, as Nebula and Arango store
    messages, so cross-DB tooling needs no per-DB vid rewrite. `dgraph.type` stays post/comment and `<id>` stays the raw
    integer, so the two node types remain distinct; only the fqid string unifies (`Mapping.vid_overrides`; set it to {}
    for the exact published format).

    The generated schema targets the repo's active loader schema `dgraph/data/ldbcTypes.schema` (what `just ldbc-load`
    uses), not the schema bundled with the upstream RDF: the two disagree on 4 predicates' index/reverse directives
    (lastName, isLocatedIn, isPartOf, type). The RDF itself does not depend on them.
    """
    entities = [
        Entity(
            label="person", prefix="person",
            csv="dynamic/person_0_0.csv", id_field="id", out_file="person_0.rdf",
            columns=[
                Column("firstName", "firstName", "string", "string @index(hash)"),
                Column("lastName", "lastName", "string", "string"),
                Column("gender", "gender", "string", "string"),
                Column("birthday", "birthday", "date", _DT_YEAR),
                Column("creationDate", "creationDate", "datetime", _DT),
                Column("locationIP", "locationIP", "string", "string"),
                Column("browserUsed", "browserUsed", "string", "string @index(exact)"),
            ],
        ),
        Entity(
            label="comment", prefix="comment",
            csv="dynamic/comment_0_0.csv", id_field="id", out_file="comment_0.rdf",
            columns=[
                Column("creationDate", "creationDate", "datetime", _DT),
                Column("locationIP", "locationIP", "string", "string"),
                Column("browserUsed", "browserUsed", "string", "string @index(exact)"),
                Column("content", "content", "string", "string"),
                Column("length", "length", "int", "int"),
            ],
        ),
        Entity(
            label="post", prefix="post",
            csv="dynamic/post_0_0.csv", id_field="id", out_file="post_0.rdf",
            columns=[
                Column("imageFile", "imageFile", "string", "string"),
                Column("creationDate", "creationDate", "datetime", _DT),
                Column("locationIP", "locationIP", "string", "string"),
                Column("browserUsed", "browserUsed", "string", "string @index(exact)"),
                Column("language", "language", "string", "[string]"),
                Column("content", "content", "string", "string"),
                Column("length", "length", "int", "int"),
            ],
        ),
        Entity(
            label="forum", prefix="forum",
            csv="dynamic/forum_0_0.csv", id_field="id", out_file="forum_0.rdf",
            columns=[
                Column("title", "title", "string", "string @index(exact)"),
                Column("creationDate", "creationDate", "datetime", _DT),
            ],
        ),
        # NOTE: dgraph.type is "organisation" (British) to match the golden RDF.
        # The published schema names the block "organization" (a latent mismatch);
        # we keep the type-block name consistent with the data instead.
        Entity(
            label="organisation", prefix="organisation",
            csv="static/organisation_0_0.csv", id_field="id", out_file="organisation_0.rdf",
            columns=[
                Column("type", "type", "string", "string @index(hash)"),
                Column("name", "name", "string", "string @index(hash)"),
                Column("url", "url", "string", "string"),
            ],
        ),
        Entity(
            label="place", prefix="place",
            csv="static/place_0_0.csv", id_field="id", out_file="place_0.rdf",
            columns=[
                Column("name", "name", "string", "string @index(hash)"),
                Column("url", "url", "string", "string"),
                Column("type", "type", "string", "string @index(hash)"),
            ],
        ),
        Entity(
            label="tag", prefix="tag",
            csv="static/tag_0_0.csv", id_field="id", out_file="tag_0.rdf",
            columns=[
                Column("name", "name", "string", "string @index(hash)"),
                Column("url", "url", "string", "string"),
            ],
        ),
        Entity(
            label="tagclass", prefix="tagclass",
            csv="static/tagclass_0_0.csv", id_field="id", out_file="tagclass_0.rdf",
            columns=[
                Column("name", "name", "string", "string @index(hash)"),
                Column("url", "url", "string", "string"),
            ],
        ),
    ]

    multiliterals = [
        MultiLiteral(
            csv="dynamic/person_email_emailaddress_0_0.csv",
            subject_prefix="person", subject_field="Person.id",
            value_field="email", predicate="email", kind="string",
            schema_type="[string]", out_file="Deltas.rdf",
        ),
        MultiLiteral(
            csv="dynamic/person_speaks_language_0_0.csv",
            subject_prefix="person", subject_field="Person.id",
            value_field="language", predicate="language", kind="string",
            schema_type="[string]", out_file="Deltas.rdf",
        ),
    ]

    relationships = [
        # hasCreator: comment + post both -> hasCreator_0.rdf
        Relationship("dynamic/comment_hasCreator_person_0_0.csv", "hasCreator",
                     "comment", "Comment.id", "person", "Person.id",
                     "[uid] @reverse", "hasCreator_0.rdf"),
        Relationship("dynamic/post_hasCreator_person_0_0.csv", "hasCreator",
                     "post", "Post.id", "person", "Person.id",
                     "[uid] @reverse", "hasCreator_0.rdf"),
        # hasTag: comment + post + forum -> hasTag_0.rdf
        Relationship("dynamic/comment_hasTag_tag_0_0.csv", "hasTag",
                     "comment", "Comment.id", "tag", "Tag.id",
                     "[uid] @reverse", "hasTag_0.rdf"),
        Relationship("dynamic/post_hasTag_tag_0_0.csv", "hasTag",
                     "post", "Post.id", "tag", "Tag.id",
                     "[uid] @reverse", "hasTag_0.rdf"),
        Relationship("dynamic/forum_hasTag_tag_0_0.csv", "hasTag",
                     "forum", "Forum.id", "tag", "Tag.id",
                     "[uid] @reverse", "hasTag_0.rdf"),
        # isLocatedIn: comment + post + person + organisation -> isLocatedIn_0.rdf
        Relationship("dynamic/comment_isLocatedIn_place_0_0.csv", "isLocatedIn",
                     "comment", "Comment.id", "place", "Place.id",
                     "uid @reverse", "isLocatedIn_0.rdf"),
        Relationship("dynamic/post_isLocatedIn_place_0_0.csv", "isLocatedIn",
                     "post", "Post.id", "place", "Place.id",
                     "uid @reverse", "isLocatedIn_0.rdf"),
        Relationship("dynamic/person_isLocatedIn_place_0_0.csv", "isLocatedIn",
                     "person", "Person.id", "place", "Place.id",
                     "uid @reverse", "isLocatedIn_0.rdf"),
        Relationship("static/organisation_isLocatedIn_place_0_0.csv", "isLocatedIn",
                     "organisation", "Organisation.id", "place", "Place.id",
                     "uid @reverse", "isLocatedIn_0.rdf"),
        # replyOf: comment->comment and comment->post -> replyOf_0.rdf
        Relationship("dynamic/comment_replyOf_comment_0_0.csv", "replyOf",
                     "comment", "Comment.id", "comment", "Comment.id",
                     "uid @reverse", "replyOf_0.rdf"),
        Relationship("dynamic/comment_replyOf_post_0_0.csv", "replyOf",
                     "comment", "Comment.id", "post", "Post.id",
                     "uid @reverse", "replyOf_0.rdf"),
        # forum edges
        Relationship("dynamic/forum_containerOf_post_0_0.csv", "containerOf",
                     "forum", "Forum.id", "post", "Post.id",
                     "[uid] @reverse", "containerOf_0.rdf"),
        Relationship("dynamic/forum_hasMember_person_0_0.csv", "hasMember",
                     "forum", "Forum.id", "person", "Person.id",
                     "[uid] @reverse", "hasMember_0.rdf",
                     facet_field="joinDate", facet_name="joinDate", facet_kind="datetime"),
        Relationship("dynamic/forum_hasModerator_person_0_0.csv", "hasModerator",
                     "forum", "Forum.id", "person", "Person.id",
                     "[uid] @reverse", "hasModerator_0.rdf"),
        # person edges
        Relationship("dynamic/person_hasInterest_tag_0_0.csv", "hasInterest",
                     "person", "Person.id", "tag", "Tag.id",
                     "[uid]", "hasInterest_0.rdf"),
        Relationship("dynamic/person_knows_person_0_0.csv", "knows",
                     "person", "Person.id", "person", "Person.id",
                     "[uid] @reverse", "knows_0.rdf",
                     facet_field="creationDate", facet_name="creationDate", facet_kind="datetime"),
        Relationship("dynamic/person_likes_comment_0_0.csv", "likes",
                     "person", "Person.id", "comment", "Comment.id",
                     "[uid] @reverse", "likes_0.rdf",
                     facet_field="creationDate", facet_name="creationDate", facet_kind="datetime",
                     facet_offset_ms=-10000),  # match golden sf0.3 (likes time is -10s)
        Relationship("dynamic/person_likes_post_0_0.csv", "likes",
                     "person", "Person.id", "post", "Post.id",
                     "[uid] @reverse", "likes_0.rdf",
                     facet_field="creationDate", facet_name="creationDate", facet_kind="datetime",
                     facet_offset_ms=-10000),  # match golden sf0.3 (likes time is -10s)
        Relationship("dynamic/person_studyAt_organisation_0_0.csv", "studyAt",
                     "person", "Person.id", "organisation", "Organisation.id",
                     "[uid]", "studyAt_0.rdf",
                     facet_field="classYear", facet_name="classYear", facet_kind="string"),
        Relationship("dynamic/person_workAt_organisation_0_0.csv", "workAt",
                     "person", "Person.id", "organisation", "Organisation.id",
                     "[uid]", "workAt_0.rdf",
                     facet_field="workFrom", facet_name="workFrom", facet_kind="string"),
        # static taxonomy edges
        Relationship("static/tag_hasType_tagclass_0_0.csv", "hasType",
                     "tag", "Tag.id", "tagclass", "TagClass.id",
                     "uid @reverse", "hasType_0.rdf"),
        Relationship("static/tagclass_isSubclassOf_tagclass_0_0.csv", "isSubclassOf",
                     "tagclass", "TagClass.id", "tagclass", "TagClass.id",
                     "[uid] @reverse", "isSubclassOf_0.rdf"),
        Relationship("static/place_isPartOf_place_0_0.csv", "isPartOf",
                     "place", "Place.id", "place", "Place.id",
                     "[uid] @reverse", "isPartOf_0.rdf"),
    ]

    # Posts + comments share one `msg_` vid (see Mapping.vid_overrides).
    return Mapping("default", entities, multiliterals, relationships,
                   vid_overrides={"comment": "msg", "post": "msg"})


def per_type_dates() -> Mapping:
    """Variant: give each node type its own creationDate predicate.

    e.g. comment.creationDate -> commentCreationDate, post -> postCreationDate.
    Edge FACETS named creationDate/joinDate are left untouched (they are facets,
    not node predicates). The schema regenerates to match automatically.
    """
    m = copy.deepcopy(default())
    m.name = "per_type_dates"
    for ent in m.entities:
        for col in ent.columns:
            if col.csv_field == "creationDate":
                col.predicate = f"{ent.label}CreationDate"
    return m


# Registry: --mapping NAME picks one of these.
MAPPINGS = {
    "default": default,
    "per_type_dates": per_type_dates,
}
