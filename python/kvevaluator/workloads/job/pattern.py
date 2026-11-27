"""A JOB query spec as a graph pattern under the mapping: variables (the vertices joins meet at) and elements
(one per table alias: a vertex's own row, a row-vertex hanging off its owner, an edge, a link row between two titles).

Type tables are resolved here, when the plan is written: `it.info = 'rating'` becomes "info_type_id is one of these
ids" on the alias that joins `it`, and a role or a company type becomes the set of edge types (README, "Type tables").
"""
import json
import os

from kvevaluator.paths import ROOT

from kvevaluator.datasets.imdb import mapping


from kvevaluator.workloads.job import sqlspec
TYPE_TABLES = json.load(open(os.path.join(str(ROOT), "python", "kvevaluator", "datasets", "imdb", "type_tables.json")))
ENTITY = {"title": "Title", "name": "Name", "char_name": "Character", "company_name": "Company", "keyword": "Keyword"}
# row-vertex tables: table -> (label, the edge type to its owner, the owner column, the owner's label)
SATELLITE = {"movie_info": ("MovieInfo", "info_of", "movie_id", "Title"),
             "movie_info_idx": ("MovieRating", "rating_of", "movie_id", "Title"),
             "person_info": ("PersonInfo", "person_info_of", "person_id", "Name"),
             "aka_name": ("AkaName", "aka_name_of", "person_id", "Name"),
             "aka_title": ("AkaTitle", "aka_title_of", "movie_id", "Title"),
             "complete_cast": ("CompleteCast", "complete_cast_of", "movie_id", "Title")}
# edge tables: table -> (source column, source label, target column, target label, the type column, its edge types)
EDGE = {"cast_info": ("person_id", "Name", "movie_id", "Title", "role_id", mapping.ROLE_EDGE),
        "movie_companies": ("movie_id", "Title", "company_id", "Company", "company_type_id", mapping.COMPANY_EDGE),
        "movie_keyword": ("movie_id", "Title", "keyword_id", "Keyword", None, {0: "has_keyword"})}
# a column that names a vertex and is not a join end of the edge itself
EDGE_PROP = {("cast_info", "person_role_id"): "character"}


class Element:
    """one alias of the query. kind: entity | satellite | edge | link. vars: {role: variable id}"""

    def __init__(self, alias, table, kind):
        self.alias, self.table, self.kind = alias, table, kind
        self.vars = {}            # role ("self", "owner", "src", "dst", "character", "from", "to") -> variable
        self.preds = []           # the alias's own conjuncts
        self.type_in = {}         # column -> the ids a joined type table allows
        self.etypes = None        # edge: the edge types its type predicates leave
        self.outputs = []         # [(output name, column)]
        self.type_outputs = []    # [(output name, type table, its column, this alias's id column)]

    @property
    def label(self):
        return ENTITY[self.table] if self.kind == "entity" else SATELLITE[self.table][0] if self.kind == "satellite" else "MovieLink"

    def selective(self):
        return bool(self.preds or self.type_in or (self.etypes is not None and len(self.etypes) < len(EDGE[self.table][5])))


class Pattern:
    def __init__(self, spec):
        self.query = spec["query"]
        self.elements, self.var_label = {}, {}
        aliases = spec["aliases"]
        parent = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for a, ca, b, cb in spec["joins"]:
            parent[find((a, ca))] = find((b, cb))
        var_of = lambda a, c: find((a, c))
        types = {a: t for a, t in aliases.items() if t in TYPE_TABLES}
        # a type alias -> the ids its predicates leave
        type_ids = {}
        for a, t in types.items():
            rows = [r for r in TYPE_TABLES[t] if all(sqlspec.holds(p, r) for p in spec["predicates"][a])]
            type_ids[find((a, "id"))] = (a, t, [r["id"] for r in rows])
        for a, t in aliases.items():
            if t in types.values() and a in types:
                continue
            kind = "entity" if t in ENTITY else "satellite" if t in SATELLITE else "edge" if t in EDGE else "link" if t == "movie_link" else None
            if kind is None:
                raise ValueError(f"{self.query}: no mapping for table {t}")
            e = Element(a, t, kind)
            cols = self._columns(e)
            for role, col, label in cols:
                key = (a, col)
                if role == "character" and key not in parent:
                    continue                                   # the character is not joined: it stays a property
                v = var_of(a, col)
                e.vars[role] = v
                if self.var_label.setdefault(v, label) != label:
                    raise ValueError(f"{self.query}: variable of {a}.{col} is both {self.var_label[v]} and {label}")
            self.elements[a] = e
        # type constraints and edge types
        for (a, c), _ in list(parent.items()):
            v = find((a, c))
            if v in type_ids and a in self.elements:
                ta, tt, ids = type_ids[v]
                self.elements[a].type_in[c] = (ids, ta, tt)
        for v, (ta, tt, ids) in type_ids.items():
            users = [(a, c) for (a, c) in parent if find((a, c)) == v and a in self.elements]
            if len(users) != 1:
                raise ValueError(f"{self.query}: type alias {ta} is joined by {len(users)} columns {users} (1 is the grammar)")
        for a, e in self.elements.items():
            keep = []
            tcol = EDGE[e.table][4] if e.kind == "edge" else None
            ids = set(EDGE[e.table][5]) if e.kind == "edge" else None
            for p in spec["predicates"][a]:
                cols = _columns_of(p)
                if tcol and cols == {tcol}:                     # a predicate on the edge's own type column
                    ids = {i for i in ids if sqlspec.holds(p, {tcol: i})}
                elif tcol and tcol in cols:
                    raise ValueError(f"{self.query}: {a}: a conjunct mixes {tcol} with other columns: {p}")
                else:
                    keep.append(p)
            e.preds = keep
            if e.kind == "edge":
                if tcol in e.type_in:
                    ids &= set(e.type_in.pop(tcol)[0])
                e.etypes = [EDGE[e.table][5][i] for i in sorted(ids)]
        for name, a, c in spec["outputs"]:
            if a in types:
                v = find((a, "id"))
                users = [(x, col) for (x, col) in parent if find((x, col)) == v and x in self.elements]
                self.elements[users[0][0]].type_outputs.append((name, types[a], c, users[0][1]))
            else:
                self.elements[a].outputs.append((name, c))
        self.output_names = [o[0] for o in spec["outputs"]]
        self._check_tree()

    @staticmethod
    def _columns(e):
        if e.kind == "entity":
            return [("self", "id", ENTITY[e.table])]
        if e.kind == "satellite":
            _, _, col, owner = SATELLITE[e.table]
            return [("owner", col, owner)]
        if e.kind == "link":
            return [("from", "movie_id", "Title"), ("to", "linked_movie_id", "Title")]
        sc, sl, dc, dl, _, _ = EDGE[e.table]
        out = [("src", sc, sl), ("dst", dc, dl)]
        if e.table == "cast_info":
            out.append(("character", "person_role_id", "Character"))
        return out

    def _check_tree(self):
        variables = set(self.var_label)
        used = {v for e in self.elements.values() for v in e.vars.values()}
        if used != variables:
            raise ValueError(f"{self.query}: a join variable no mapped column reaches: {variables - used}")
        edges = sum(len(e.vars) for e in self.elements.values())
        if edges != len(self.elements) + len(variables) - 1:
            raise ValueError(f"{self.query}: the pattern is not a tree ({len(self.elements)} elements, "
                             f"{len(variables)} variables, {edges} incidences)")

    def at(self, v):
        """the elements that touch variable v, in alias order"""
        return [e for a, e in sorted(self.elements.items()) if v in e.vars.values()]


def _columns_of(pred):
    if pred[0] in ("and", "or"):
        return set().union(*(_columns_of(p) for p in pred[1]))
    return {pred[3]} if pred[0] == "cmp" else {pred[2]}
