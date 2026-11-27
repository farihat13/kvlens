"""A JOB query's pattern + an entry point -> a plan (the plan IR's Step list). One body per entry; the rule that
orders the rest is fixed (README, "The plans"):

1. ENTRY: one alias with a predicate, read by a property index (`index`), by its label's vid list and a read per
   vertex (`scan`), or by one pass over the label's own key run where the store keeps a label's records as one run
   (`run`; the capability label_rows_one_run). Every entry is built here; which bodies a store is offered is the
   bundle's (queries/__init__.py: the plain scan only where the label lacks whole_label_scan_by_default).
2. OUTWARD from the entry along the tree. At a variable, the arms whose side holds a predicate go first, then the
   smaller mean degree; after each arm the variable keeps only the vertices that arm matched, so the next arm reads less.
3. BACK DOWN in memory: every arm is cut to the vertices that survived all the others (no KV operation).
4. OUTPUTS: each MIN over the rows that are left. A property only an output needs is read here, for the survivors
   (`late`), or with the predicate's read of the same vertex when there is one (`early`: the twin for layouts that
   return the whole row anyway).
No arm is multiplied with another: a MIN needs the values that take part in a result row, not the rows.
"""
from kvevaluator.plans.plan import Plan, Step, ref
from kvevaluator.plans.ir import authoring as E
from kvevaluator.datasets.imdb import mapping
from kvevaluator.workloads.job.pattern import EDGE, EDGE_PROP, SATELLITE, TYPE_TABLES, _columns_of

# mean rows per source vertex, from the dump (casestudy statistics; the order rule's only number)
MEAN_DEGREE = {("cast_info", "dst"): 15.5, ("cast_info", "src"): 8.9, ("movie_companies", "src"): 2.4,
               ("movie_companies", "dst"): 11.1, ("movie_keyword", "src"): 9.5, ("movie_keyword", "dst"): 33.7,
               ("movie_info", "owner"): 6.0, ("movie_info_idx", "owner"): 3.0, ("person_info", "owner"): 5.4,
               ("aka_name", "owner"): 1.5, ("aka_title", "owner"): 1.8, ("complete_cast", "owner"): 1.4,
               ("movie_link", "from"): 4.7, ("movie_link", "to"): 1.9}
PROP_TYPE = {(label, p): t for label, (_, props) in mapping.VERTICES.items() for p, _, t in props}
PROP_TYPE.update({("cast_info", p): t for p, _, t in mapping.EDGES["cast_actor"][5]})
PROP_TYPE.update({("movie_companies", "note"): "str"})
INDEXED = set(mapping.INDEXES)


class CannotBuild(Exception):
    """this body does not exist for the query (a declared limit, never a fallback)"""


def _get(name):
    return E.Expr({"get": name})


def prop_of(e, col):
    return EDGE_PROP.get((e.table, col), col)


def _typed(e, col, value):
    key = (e.label if e.kind != "edge" else e.table, prop_of(e, col))
    t = PROP_TYPE[key]
    if t == "int" and not isinstance(value, int):
        raise CannotBuild(f"{e.alias}.{col} is an integer, compared with {value!r}")
    if t == "str" and not isinstance(value, str):
        raise CannotBuild(f"{e.alias}.{col} is a string, compared with {value!r}")
    return value


def expr_of(e, pred):
    """a spec predicate as a plan-IR expression; an atom on a NULL is false, as in SQL's WHERE"""
    k = pred[0]
    if k in ("and", "or"):
        parts = [expr_of(e, p) for p in pred[1]]
        return E.and_(*parts) if k == "and" else E.or_(*parts)
    col = pred[3] if k == "cmp" else pred[2]
    f = _get(prop_of(e, col))
    if k == "null":
        return E.isnull(f) if pred[3] else E.notnull(f)
    if k == "cmp":
        op = {"=": E.eq, "!=": E.ne, "<": E.lt, "<=": E.le, ">": E.gt, ">=": E.ge}[pred[1]]
        return E.and_(E.notnull(f), op(f, E.lit(_typed(e, col, pred[4]))))
    if k == "in":
        return E.and_(E.notnull(f), E.in_(f, E.lit([_typed(e, col, v) for v in pred[3]])))
    if k == "like":
        m = E.like(f, E.lit(pred[3]))
        return E.and_(E.notnull(f), E.not_(m) if pred[4] else m)
    if k == "between":
        return E.and_(E.notnull(f), E.ge(f, E.lit(_typed(e, col, pred[3]))), E.le(f, E.lit(_typed(e, col, pred[4]))))
    raise ValueError(pred)


def index_served(e):
    """whether an index serves one of the element's tests (an indexed equality or IN, or type ids on an indexed column)"""
    for p in e.preds:
        if (p[0] == "cmp" and p[1] == "=") or p[0] == "in":
            if (e.label, prop_of(e, p[3] if p[0] == "cmp" else p[2])) in INDEXED:
                return True
    return e.kind != "edge" and any((e.label, col) in INDEXED for col in e.type_in)


class Builder:
    def __init__(self, pattern, entry, access="index", outputs="late", entry_counts=None):
        self.p, self.steps, self.n = pattern, [], 0
        self.access, self.early = access, outputs == "early"
        self.entry_counts = entry_counts or {}
        self.rows, self.fields = {}, {}          # alias -> its rows step; alias -> {role: field}
        self.have = {}                            # alias -> the properties its rows hold
        self.children = {}                        # variable -> [(element, role)] expanded from it
        self.final = {}                           # variable -> the step holding its surviving vids
        e0 = pattern.elements[entry]
        if e0.kind == "edge":
            raise CannotBuild("an edge table is not an entry (no edge index, no edge scan)")
        r = self.enter(e0)
        self.entry_rows = r
        self.rows[e0.alias] = self.descend(e0, r, None)
        self.down(e0, None)
        self.finish()

    # ---- helpers --------------------------------------------------------------------------------------------------
    def step(self, tag, op, **args):
        self.n += 1
        name = f"s{self.n:02d}_{tag}"
        self.steps.append(Step(name, op, **args))
        return name

    def distinct(self, tag, rows, field):
        return self.step(f"{tag}_set", "dedup", items=ref(self.step(f"{tag}_col", "pluck", rows=ref(rows), field=field)))

    def conds(self, e, skip=None, skip_type=None):
        """the element's predicates and type-id tests as one expression (None when there is none)"""
        parts = [expr_of(e, p) for i, p in enumerate(e.preds) if i != skip]
        for col, (ids, _, _) in sorted(e.type_in.items()):
            if col != skip_type:
                parts.append(E.and_(E.notnull(_get(col)), E.in_(_get(col), E.lit(list(ids)))))
        return None if not parts else parts[0] if len(parts) == 1 else E.and_(*parts)

    def cond_props(self, e, skip=None, skip_type=None):
        cols = set()
        for i, p in enumerate(e.preds):
            if i != skip:
                cols |= _columns_of(p)
        cols |= {c for c in e.type_in if c != skip_type}
        return {prop_of(e, c) for c in cols}

    def output_props(self, e):
        return {prop_of(e, c) for _, c in e.outputs} | {idcol for _, _, _, idcol in e.type_outputs}

    def vertex_rows(self, e, vids, skip=None, skip_type=None):
        """the element's own vertices among `vids` that pass its predicates: rows {_vid, props}, or None when nothing
        has to be read (no predicate left and no output to read early)"""
        props = self.cond_props(e, skip, skip_type)
        if props and self.early:
            props |= self.output_props(e)
        self.have[e.alias] = props
        if not props:
            return None
        r = self.step(f"{e.alias}_props", "read_vertex_props", vids=ref(vids), props=sorted(props), label=e.label)
        c = self.conds(e, skip, skip_type)
        return self.step(f"{e.alias}_keep", "filter", rows=ref(r), pred=c) if c is not None else r

    # ---- the entry ------------------------------------------------------------------------------------------------
    def index_choice(self, e):
        """(kind, what) of the index read that serves one of the element's tests, the one matching the fewest rows"""
        best = None
        for i, p in enumerate(e.preds):
            if not ((p[0] == "cmp" and p[1] == "=") or p[0] == "in"):
                continue
            col = p[3] if p[0] == "cmp" else p[2]
            if (e.label, prop_of(e, col)) in INDEXED:
                n = self.entry_counts.get((self.p.query, e.alias, f"p{i}"))
                if best is None or (n is not None and n < best[0]):
                    best = (n if n is not None else 1 << 62, "pred", i)
        for col in sorted(e.type_in):
            if (e.label, col) in INDEXED:
                n = self.entry_counts.get((self.p.query, e.alias, f"t:{col}"))
                if best is None or (n is not None and n < best[0]):
                    best = (n if n is not None else 1 << 62, "type", col)
        return best

    def enter(self, e):
        a, skip, skip_type = e.alias, None, None
        if self.access == "run":
            # the label's own key run, read once with the properties (label_rows_one_run: valid where a label's
            # records are one run). The run crosses every property anyway, so the outputs come with it
            props = sorted(self.cond_props(e) | self.output_props(e))
            rows = self.step(f"{a}_run", "read_vertices_by_label", label=e.label, props=props)
            self.have[a] = set(props)
            c = self.conds(e)
            if c is not None:
                rows = self.step(f"{a}_keep", "filter", rows=ref(rows), pred=c)
            return self.entered(e, rows)
        if self.access == "scan":
            vids = self.step(f"{a}_scan", "read_vertices_by_label", label=e.label)
        else:
            best = self.index_choice(e)
            if best is None:
                raise CannotBuild(f"{a}: no test an index serves")
            if best[1] == "pred":
                skip = best[2]
                p = e.preds[skip]
                col = p[3] if p[0] == "cmp" else p[2]
                values = [p[4]] if p[0] == "cmp" else list(p[3])
            else:
                skip_type = col = best[2]
                values = list(e.type_in[col][0])
            if not values:
                raise CannotBuild(f"{a}: the type predicate leaves no id")
            prop, parts = prop_of(e, col), []
            for v in values:      # every index of this workload is an equality index, on a string or an integer
                parts.append(self.step(f"{a}_ix", "read_index_eq", label=e.label, prop=prop, value=_typed(e, col, v)))
            vids = parts[0]
            for x in parts[1:]:
                vids = self.step(f"{a}_ix_all", "concat", a=ref(vids), b=ref(x))
            if len(parts) > 1:
                vids = self.step(f"{a}_ix_set", "dedup", items=ref(vids))
        rows = self.vertex_rows(e, vids, skip, skip_type)
        if rows is None:
            rows = self.step(f"{a}_rows", "annotate", items=ref(vids), field="entry", value=1)
        return self.entered(e, rows)

    def entered(self, e, rows):
        """the entry's rows {_vid, props} as the element's rows over its variables"""
        a = e.alias
        # the entry's rows, counted: the reference check reads this step's rows_in (it is not part of the answer)
        self.entry_count = self.step("entry_n", "count", items=ref(rows))
        if e.kind == "entity":
            self.fields[a] = {"self": "_vid"}
            return rows
        own = self.step(f"{a}_vids", "pluck", rows=ref(rows), field="_vid")
        if e.kind == "satellite":
            nb = self.step(f"{a}_owner", "read_neighbors", srcs=ref(own), etype=SATELLITE[e.table][1], dir="out", pairs=True)
            self.fields[a] = {"owner": "dst", "vertex": "src"}
            return self.step(f"{a}_with", "join", left=ref(nb), right=ref(rows), left_on="src", right_on="_vid")
        f = self.step(f"{a}_from", "rename", rows=ref(self.step(f"{a}_from0", "read_neighbors", srcs=ref(own), etype="link_from", dir="out", pairs=True)), names={"src": "ml", "dst": "a"})
        t = self.step(f"{a}_to", "rename", rows=ref(self.step(f"{a}_to0", "read_neighbors", srcs=ref(own), etype="link_to", dir="out", pairs=True)), names={"src": "ml", "dst": "b"})
        both = self.step(f"{a}_ends", "join_multi", left=ref(f), right=ref(t), left_on="ml", right_on="ml")
        self.fields[a] = {"from": "a", "to": "b", "vertex": "ml"}
        return self.step(f"{a}_with", "join", left=ref(both), right=ref(rows), left_on="ml", right_on="_vid")

    # ---- outward ----------------------------------------------------------------------------------------------------
    def expand(self, e, role, S):
        """the element's rows that touch a vertex of S (the variable at `role`), its own predicates applied"""
        a = e.alias
        if e.kind == "entity":
            self.fields[a] = {"self": "_vid"}
            return self.vertex_rows(e, S)
        if e.kind == "satellite":
            nb = self.step(f"{a}_of", "read_neighbors", srcs=ref(S), etype=SATELLITE[e.table][1], dir="in", pairs=True)
            self.fields[a] = {"owner": "src", "vertex": "dst"}
            rows = self.vertex_rows(e, self.distinct(a, nb, "dst"))
            if rows is None:
                return nb
            kept = self.step(f"{a}_kept", "filter_in", rows=ref(nb), field="dst", allowed=ref(self.step(f"{a}_ok", "pluck", rows=ref(rows), field="_vid")))
            return self.step(f"{a}_with", "join", left=ref(kept), right=ref(rows), left_on="dst", right_on="_vid")
        if e.kind == "link":
            near, far = ("link_from", "link_to") if role == "from" else ("link_to", "link_from")
            nb = self.step(f"{a}_near", "rename", rows=ref(self.step(f"{a}_near0", "read_neighbors", srcs=ref(S), etype=near, dir="in", pairs=True)), names={"src": "a", "dst": "ml"})
            rows = self.vertex_rows(e, self.distinct(a, nb, "ml"))
            if rows is not None:
                kept = self.step(f"{a}_kept", "filter_in", rows=ref(nb), field="ml", allowed=ref(self.step(f"{a}_ok", "pluck", rows=ref(rows), field="_vid")))
                nb = self.step(f"{a}_with", "join", left=ref(kept), right=ref(rows), left_on="ml", right_on="_vid")
            fr = self.step(f"{a}_far", "rename", rows=ref(self.step(f"{a}_far0", "read_neighbors", srcs=ref(self.distinct(a + "_ml", nb, "ml")), etype=far, dir="out", pairs=True)), names={"src": "ml", "dst": "b"})
            self.fields[a] = {role: "a", ("to" if role == "from" else "from"): "b", "vertex": "ml"}
            return self.step(f"{a}_ends", "join_multi", left=ref(nb), right=ref(fr), left_on="ml", right_on="ml")
        # an edge
        if role == "character":
            raise CannotBuild(f"{a}: a credit cannot be reached from its character (the character is a property of the edge)")
        if not e.etypes:
            raise CannotBuild(f"{a}: the type predicate leaves no edge type")
        props = sorted(self.cond_props(e) | self.output_props(e) | ({"character"} if "character" in e.vars else set()))
        self.have[a] = set(props)
        rows = None
        for et in e.etypes:
            r = self.step(f"{a}_{et}", "read_neighbors", srcs=ref(S), etype=et, dir="out" if role == "src" else "in",
                          pairs=True, **({"edge_props": props} if props else {}))
            rows = r if rows is None else self.step(f"{a}_all", "concat", a=ref(rows), b=ref(r))
        self.fields[a] = {role: "src", ("dst" if role == "src" else "src"): "dst"}
        c = self.conds(e)
        if "character" in e.vars:
            has = E.notnull(_get("character"))
            c = has if c is None else E.and_(has, c)
        if c is not None:
            rows = self.step(f"{a}_keep", "filter", rows=ref(rows), pred=c)
        if "character" in e.vars:
            rows = self.step(f"{a}_ch", "derive", rows=ref(rows), into="ch", fn=E.Expr({"vid": [E.lit("Character").ast, E.field("character").ast]}))
            self.fields[a]["character"] = "ch"
        return rows

    def selective_beyond(self, e, came):
        if e.selective():
            return True
        return any(self.selective_beyond(x, w) for role, w in e.vars.items() if w != came
                   for x in self.p.at(w) if x is not e)

    def visit(self, v, S, came):
        arms = [(e, next(r for r, w in e.vars.items() if w == v)) for e in self.p.at(v) if e is not came]
        arms.sort(key=lambda er: (not self.selective_beyond(er[0], v), MEAN_DEGREE.get((er[0].table, er[1]), 1.0), er[0].alias))
        self.children[v] = []
        for e, role in arms:
            rows = self.expand(e, role, S)
            if rows is None:
                self.children[v].append((e, role, False))
                continue
            rows = self.descend(e, rows, role)
            self.rows[e.alias] = rows
            self.children[v].append((e, role, True))
            S = self.distinct(f"{e.alias}_back", rows, self.fields[e.alias][role])
        return S

    def descend(self, e, rows, came_role):
        for role, w in e.vars.items():
            if role == came_role:
                continue
            f = self.fields[e.alias][role]
            S = self.distinct(f"{e.alias}_{role}", rows, f)
            S2 = self.visit(w, S, e)
            if S2 != S:
                rows = self.step(f"{e.alias}_cut", "filter_in", rows=ref(rows), field=f, allowed=ref(S2))
        return rows

    # ---- back down, in memory ---------------------------------------------------------------------------------------
    def down(self, e, came_role):
        for role, w in e.vars.items():
            if role == came_role:
                continue
            S = self.distinct(f"{e.alias}_{role}_final", self.rows[e.alias], self.fields[e.alias][role])
            self.final[w] = S
            for c, crole, has_rows in self.children[w]:
                if not has_rows:
                    continue
                self.rows[c.alias] = self.step(f"{c.alias}_final", "filter_in", rows=ref(self.rows[c.alias]),
                                               field=self.fields[c.alias][crole], allowed=ref(S))
                self.down(c, crole)

    # ---- the outputs --------------------------------------------------------------------------------------------------
    def count_aliases(self):
        """{alias: a free step counting its final rows} (an entity that was never read: its variable's final
        vertices): what the reference check compares with COUNT(DISTINCT alias.id) over the join. The counts go
        into the answer row (`rows_<alias>`): they are the same for every body of a query, so a capture's result
        hash covers them"""
        out = {}
        for a, e in sorted(self.p.elements.items()):
            items = self.rows[a] if self.rows.get(a) is not None else self.final[e.vars["self"]]
            out[a] = self.step(f"{a}_final_n", "count", items=ref(items))
        return out

    def finish(self):
        counts = self.count_aliases()
        base = self.step("base", "derive", rows=ref(self.step("base0", "as_row", value=ref(self.entry_count), field="entry_rows")),
                         into="k", fn=E.lit(0))
        for a, e in sorted(self.p.elements.items()):
            if not (e.outputs or e.type_outputs):
                continue
            need = self.output_props(e)
            rows = self.rows.get(a)
            if not need <= self.have.get(a, set()):      # a property only the output needs: read for the survivors
                if e.kind == "entity":
                    vids = self.final[e.vars["self"]]
                else:
                    vids = self.distinct(f"{a}_out", rows, self.fields[a]["vertex"])
                rows = self.step(f"{a}_out", "read_vertex_props", vids=ref(vids), props=sorted(need), label=e.label)
            for name, col in e.outputs:
                base = self.minimum(base, rows, name, prop_of(e, col))
            for name, table, tcol, idcol in e.type_outputs:
                names = [{idcol: r["id"], "type_name": r[tcol]} for r in TYPE_TABLES[table]]
                named = self.step(f"{name}_named", "join", left=ref(rows), right=names, left_on=idcol, right_on=idcol)
                base = self.minimum(base, named, name, "type_name")
        for a, c in counts.items():
            one = self.step(f"{a}_n_k", "derive", rows=ref(self.step(f"{a}_n_row", "as_row", value=ref(c), field=f"rows_{a}")),
                            into="k", fn=E.lit(0))
            base = self.step(f"{a}_n_in", "left_join", left=ref(base), right=ref(one), left_on="k", right_on="k")
        # every body of a query returns the same row: the MIN columns, then each alias's final row count
        self.steps.append(Step("out", "project", rows=ref(base), fields=self.p.output_names + [f"rows_{a}" for a in counts]))

    def minimum(self, base, rows, name, field):
        some = self.step(f"{name}_some", "filter", rows=ref(rows), pred=E.notnull(_get(field)))
        agg = self.step(f"{name}_min", "group_agg", rows=ref(some), by=[], aggs={name: ["min", field]})
        keyed = self.step(f"{name}_k", "derive", rows=ref(agg), into="k", fn=E.lit(0))
        return self.step(f"{name}_in", "left_join", left=ref(base), right=ref(keyed), left_on="k", right_on="k", default={name: None})


def build(pattern, entry, access="index", outputs="late", entry_counts=None):
    """the Plan of one body, or CannotBuild"""
    return Plan(Builder(pattern, entry, access, outputs, entry_counts).steps, params={}, let={})
