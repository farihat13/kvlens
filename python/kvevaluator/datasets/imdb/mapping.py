"""The IMDb -> property graph mapping of the JOB workload (mapping `rows`): the one table the table builder, the
loader, the schema file's check and the plan generator read. Why each choice: README.md, "The mapping"."""

import json as _json
import os as _os

MAPPING = "rows"

# label -> (JOB table, [(property, JOB column, type)]); every column of the table but its id and the foreign keys
# that are edges below. A foreign key to a type table stays an integer property (`*_id`).
VERTICES = {
    "Title": ("title", [("title", "title", "str"), ("imdb_index", "imdb_index", "str"), ("kind_id", "kind_id", "int"),
                        ("production_year", "production_year", "int"), ("imdb_id", "imdb_id", "int"),
                        ("phonetic_code", "phonetic_code", "str"), ("season_nr", "season_nr", "int"),
                        ("episode_nr", "episode_nr", "int"), ("series_years", "series_years", "str"),
                        ("md5sum", "md5sum", "str")]),
    "Name": ("name", [("name", "name", "str"), ("imdb_index", "imdb_index", "str"), ("imdb_id", "imdb_id", "int"),
                      ("gender", "gender", "str"), ("name_pcode_cf", "name_pcode_cf", "str"),
                      ("name_pcode_nf", "name_pcode_nf", "str"), ("surname_pcode", "surname_pcode", "str"),
                      ("md5sum", "md5sum", "str")]),
    "Character": ("char_name", [("name", "name", "str"), ("imdb_index", "imdb_index", "str"), ("imdb_id", "imdb_id", "int"),
                                ("name_pcode_nf", "name_pcode_nf", "str"), ("surname_pcode", "surname_pcode", "str"),
                                ("md5sum", "md5sum", "str")]),
    "Company": ("company_name", [("name", "name", "str"), ("country_code", "country_code", "str"), ("imdb_id", "imdb_id", "int"),
                                 ("name_pcode_nf", "name_pcode_nf", "str"), ("name_pcode_sf", "name_pcode_sf", "str"),
                                 ("md5sum", "md5sum", "str")]),
    "Keyword": ("keyword", [("keyword", "keyword", "str"), ("phonetic_code", "phonetic_code", "str")]),
    "MovieInfo": ("movie_info", [("info", "info", "str"), ("note", "note", "str"), ("info_type_id", "info_type_id", "int")]),
    "MovieRating": ("movie_info_idx", [("info", "info", "str"), ("note", "note", "str"), ("info_type_id", "info_type_id", "int")]),
    "PersonInfo": ("person_info", [("info", "info", "str"), ("note", "note", "str"), ("info_type_id", "info_type_id", "int")]),
    "AkaName": ("aka_name", [("name", "name", "str"), ("imdb_index", "imdb_index", "str"), ("name_pcode_cf", "name_pcode_cf", "str"),
                             ("name_pcode_nf", "name_pcode_nf", "str"), ("surname_pcode", "surname_pcode", "str"),
                             ("md5sum", "md5sum", "str")]),
    # aka_title.episode_of_id (2,876 set; no query reads it) stays an integer: the one foreign key to an entity
    # that is not an edge
    "AkaTitle": ("aka_title", [("title", "title", "str"), ("imdb_index", "imdb_index", "str"), ("kind_id", "kind_id", "int"),
                               ("production_year", "production_year", "int"), ("phonetic_code", "phonetic_code", "str"),
                               ("episode_of_id", "episode_of_id", "int"), ("season_nr", "season_nr", "int"),
                               ("episode_nr", "episode_nr", "int"), ("note", "note", "str"), ("md5sum", "md5sum", "str")]),
    "CompleteCast": ("complete_cast", [("subject_id", "subject_id", "int"), ("status_id", "status_id", "int")]),
    "MovieLink": ("movie_link", [("link_type_id", "link_type_id", "int")]),
    "KindType": ("kind_type", [("kind", "kind", "str")]),
    "InfoType": ("info_type", [("info", "info", "str")]),
    "LinkType": ("link_type", [("link", "link", "str")]),
    "CompCastType": ("comp_cast_type", [("kind", "kind", "str")]),
}

# properties that are never NULL in the dump; every other property is nullable
REQUIRED = {("Title", "title"), ("Title", "kind_id"), ("Name", "name"), ("Character", "name"), ("Company", "name"),
            ("Keyword", "keyword"), ("MovieInfo", "info"), ("MovieInfo", "info_type_id"), ("MovieRating", "info"),
            ("MovieRating", "info_type_id"), ("PersonInfo", "info"), ("PersonInfo", "info_type_id"), ("AkaName", "name"),
            ("AkaTitle", "title"), ("AkaTitle", "kind_id"), ("CompleteCast", "subject_id"), ("CompleteCast", "status_id"),
            ("MovieLink", "link_type_id"), ("KindType", "kind"), ("InfoType", "info"), ("LinkType", "link"),
            ("CompCastType", "kind")}

# the vid tag of each label (vids._TAG holds the same numbers; an IMDb store holds IMDb labels only)
LABELS = list(VERTICES)

# role_type.id -> the edge type of a credit with that role; company_type.id -> the edge type of a company credit.
# The role and the company type are part of the edge's identity (README: "Repeated credits"), so
# `rt.role = 'actress'` selects an edge type and reads nothing.
_ROLE = {1: "cast_actor", 2: "cast_actress", 3: "cast_producer", 4: "cast_writer", 5: "cast_cinematographer",
         6: "cast_composer", 7: "cast_costume_designer", 8: "cast_director", 9: "cast_editor",
         10: "cast_miscellaneous_crew", 11: "cast_production_designer", 12: "cast_guest"}
_COMPANY = {1: "company_distributor", 2: "company_production", 3: "company_special_effects",
            4: "company_miscellaneous"}
# An edge type is declared only when the full data holds a row of it (edge_type_rows_full.json beside this file, written by
# build_tables.py --tag full): a declared type with no edge would still be probed by every arm that names no type.
# build_tables.py refuses a row whose type is not declared.
with open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "edge_type_rows_full.json")) as _f:
    EDGE_TYPE_ROWS = {t: {int(k): v for k, v in d.items()} for t, d in _json.load(_f).items()}
ROLE_EDGE = {i: n for i, n in _ROLE.items() if EDGE_TYPE_ROWS["cast_info"].get(i, 0) > 0}
COMPANY_EDGE = {i: n for i, n in _COMPANY.items() if EDGE_TYPE_ROWS["movie_companies"].get(i, 0) > 0}

# edge type -> (JOB table, source label, source column, target label, target column, [(property, column, type)],
#               the row filter that selects this edge type's rows or None)
EDGES = {}
for _rid, _et in ROLE_EDGE.items():
    # `character` holds char_name.id: a credit points at three things, and an edge has two ends
    EDGES[_et] = ("cast_info", "Name", "person_id", "Title", "movie_id",
                  [("note", "note", "str"), ("nr_order", "nr_order", "int"), ("character", "person_role_id", "int")],
                  f"role_id = {_rid}")
for _cid, _et in COMPANY_EDGE.items():
    EDGES[_et] = ("movie_companies", "Title", "movie_id", "Company", "company_id", [("note", "note", "str")],
                  f"company_type_id = {_cid}")
EDGES.update({
    "has_keyword": ("movie_keyword", "Title", "movie_id", "Keyword", "keyword_id", [], None),
    "episode_of": ("title", "Title", "id", "Title", "episode_of_id", [], None),
    "info_of": ("movie_info", "MovieInfo", "id", "Title", "movie_id", [], None),
    "rating_of": ("movie_info_idx", "MovieRating", "id", "Title", "movie_id", [], None),
    "person_info_of": ("person_info", "PersonInfo", "id", "Name", "person_id", [], None),
    "aka_name_of": ("aka_name", "AkaName", "id", "Name", "person_id", [], None),
    "aka_title_of": ("aka_title", "AkaTitle", "id", "Title", "movie_id", [], None),
    "complete_cast_of": ("complete_cast", "CompleteCast", "id", "Title", "movie_id", [], None),
    "link_from": ("movie_link", "MovieLink", "id", "Title", "movie_id", [], None),
    "link_to": ("movie_link", "MovieLink", "id", "Title", "linked_movie_id", [], None),
})

# D4's rule: an equality index on every vertex property a JOB query tests with = or IN against a constant, directly or
# through a type table (`it.info = 'rating'` tests MovieRating.info_type_id). The type tables' own columns are not in
# it: a type predicate is resolved when the plan is written. The list is derived from the specs.
INDEXES = [("Title", "title"), ("Title", "kind_id"), ("Title", "production_year"), ("Name", "gender"),
           ("Character", "name"), ("Company", "name"), ("Company", "country_code"), ("Keyword", "keyword"),
           ("MovieInfo", "info"), ("MovieInfo", "info_type_id"), ("MovieRating", "info_type_id"),
           ("PersonInfo", "info_type_id"), ("PersonInfo", "note"), ("CompleteCast", "subject_id"),
           ("CompleteCast", "status_id"), ("MovieLink", "link_type_id")]

# the merge keys of "IMDb with repeated credits merged": the first row (smallest id) per key is kept
MERGE_KEYS = {"cast_info": ("person_id", "movie_id", "role_id"),
              "movie_companies": ("movie_id", "company_id", "company_type_id"),
              "movie_keyword": ("movie_id", "keyword_id")}
