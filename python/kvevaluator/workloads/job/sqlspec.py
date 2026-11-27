"""JOB's SQL texts -> query specs (the parsed form the plan generator reads; tracked under specs/).

A JOB query is one block: SELECT MIN(a.c) AS name, ... FROM table AS alias, ... WHERE conjunct AND conjunct ...
A spec is {"query", "aliases": {alias: table}, "joins": [[alias, col, alias, col]], "predicates": {alias: [pred]},
"outputs": [[name, alias, col]]}; a pred is one of
  ["cmp", op, alias, col, value]   op in = != < <= > >=     ["between", alias, col, lo, hi]
  ["in", alias, col, [values]]     ["like", alias, col, pattern, negated]     ["null", alias, col, is_null]
  ["and", [pred, ...]]             ["or", [pred, ...]]
Every conjunct of the WHERE clause names ONE alias (parse() refuses a query where one does not).
"""
import re

_TOKEN = re.compile(r"""\s*(?:(?P<str>'(?:[^']|'')*')|(?P<num>-?\d+(?:\.\d+)?)|(?P<op><=|>=|!=|<>|=|<|>|\(|\)|,|;)
                        |(?P<word>[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)?))""", re.X)
_KEYWORDS = {"AND", "OR", "NOT", "LIKE", "IN", "IS", "NULL", "BETWEEN", "AS", "SELECT", "FROM", "WHERE", "MIN"}


def _tokens(text):
    out, pos = [], 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise ValueError(f"cannot read the SQL at: {text[pos:pos + 40]!r}")
        pos = m.end()
        if m.group("str") is not None:
            out.append(("str", m.group("str")[1:-1].replace("''", "'")))
        elif m.group("num") is not None:
            out.append(("num", float(m.group("num")) if "." in m.group("num") else int(m.group("num"))))
        elif m.group("op") is not None:
            out.append(("op", m.group("op")))
        else:
            w = m.group("word")
            out.append(("kw", w.upper()) if w.upper() in _KEYWORDS and "." not in w else ("id", w))
    return out


class _Parser:
    def __init__(self, toks):
        self.t, self.i = toks, 0

    def peek(self, k=0):
        return self.t[self.i + k] if self.i + k < len(self.t) else ("end", None)

    def take(self, kind=None, value=None):
        tok = self.peek()
        if (kind and tok[0] != kind) or (value is not None and tok[1] != value):
            raise ValueError(f"expected {kind} {value!r}, got {tok} at token {self.i}")
        self.i += 1
        return tok[1]

    def literal(self):
        tok = self.peek()
        if tok[0] not in ("str", "num"):
            raise ValueError(f"expected a literal, got {tok}")
        self.i += 1
        return tok[1]

    def col(self):
        a, c = self.take("id").split(".")
        return a, c

    def or_expr(self):
        items = [self.and_expr()]
        while self.peek() == ("kw", "OR"):
            self.i += 1
            items.append(self.and_expr())
        return items[0] if len(items) == 1 else ["or", items]

    def and_expr(self):
        items = [self.atom()]
        while self.peek() == ("kw", "AND"):
            self.i += 1
            items.append(self.atom())
        return items[0] if len(items) == 1 else ["and", items]

    def atom(self):
        if self.peek() == ("op", "("):
            self.i += 1
            e = self.or_expr()
            self.take("op", ")")
            return e
        a, c = self.col()
        tok = self.peek()
        if tok[0] == "op" and tok[1] in ("=", "!=", "<>", "<", "<=", ">", ">="):
            self.i += 1
            op = "!=" if tok[1] == "<>" else tok[1]
            if self.peek()[0] == "id":
                b, d = self.col()
                if op != "=":
                    raise ValueError(f"a join that is not an equality: {a}.{c} {op} {b}.{d}")
                return ["join", a, c, b, d]
            return ["cmp", op, a, c, self.literal()]
        negated = False
        if tok == ("kw", "NOT"):
            self.i += 1
            negated = True
            tok = self.peek()
        if tok == ("kw", "LIKE"):
            self.i += 1
            return ["like", a, c, self.take("str"), negated]
        if tok == ("kw", "IN"):
            self.i += 1
            self.take("op", "(")
            vals = [self.literal()]
            while self.peek() == ("op", ","):
                self.i += 1
                vals.append(self.literal())
            self.take("op", ")")
            if negated:
                raise ValueError("NOT IN is not in JOB; add it to the spec grammar before using it")
            return ["in", a, c, vals]
        if tok == ("kw", "BETWEEN") and not negated:
            self.i += 1
            lo = self.literal()
            self.take("kw", "AND")
            return ["between", a, c, lo, self.literal()]
        if tok == ("kw", "IS") and not negated:
            self.i += 1
            is_null = True
            if self.peek() == ("kw", "NOT"):
                self.i += 1
                is_null = False
            self.take("kw", "NULL")
            return ["null", a, c, is_null]
        raise ValueError(f"unknown predicate after {a}.{c}: {tok}")


def aliases_of(pred):
    if pred[0] in ("and", "or"):
        return set().union(*(aliases_of(p) for p in pred[1]))
    if pred[0] == "join":
        return {pred[1], pred[3]}
    return {pred[2]} if pred[0] == "cmp" else {pred[1]}


def parse(name, text):
    p = _Parser(_tokens(text))
    p.take("kw", "SELECT")
    outputs = []
    while True:
        p.take("kw", "MIN"); p.take("op", "(")
        a, c = p.col()
        p.take("op", ")"); p.take("kw", "AS")
        outputs.append([p.take("id"), a, c])
        if p.peek() != ("op", ","):
            break
        p.i += 1
    p.take("kw", "FROM")
    aliases = {}
    while True:
        table = p.take("id")
        p.take("kw", "AS")
        aliases[p.take("id")] = table
        if p.peek() != ("op", ","):
            break
        p.i += 1
    p.take("kw", "WHERE")
    where = p.or_expr()
    if p.peek() == ("op", ";"):
        p.i += 1
    if p.peek()[0] != "end":
        raise ValueError(f"{name}: text left after the WHERE clause: {p.peek()}")
    conjuncts = where[1] if where[0] == "and" else [where]
    joins, preds = [], {a: [] for a in aliases}
    for c in conjuncts:
        if c[0] == "join":
            joins.append(c[1:])
            continue
        al = aliases_of(c)
        if len(al) != 1 or "join" in repr(c):
            raise ValueError(f"{name}: a conjunct over {sorted(al)} (one alias per conjunct is the grammar): {c}")
        preds[al.pop()].append(c)
    return {"query": name, "aliases": aliases, "joins": joins, "predicates": preds, "outputs": outputs}


def _lit(v):
    return "'" + v.replace("'", "''") + "'" if isinstance(v, str) else repr(v)


def to_sql(pred, alias=None):
    """the predicate as SQL (for the reference's row counts); alias: write this alias in place of the pred's own"""
    k = pred[0]
    if k in ("and", "or"):
        return "(" + f" {k.upper()} ".join(to_sql(p, alias) for p in pred[1]) + ")"
    if k == "cmp":
        return f"{alias or pred[2]}.{pred[3]} {pred[1]} {_lit(pred[4])}"
    a = alias or pred[1]
    if k == "in":
        return f"{a}.{pred[2]} IN ({', '.join(_lit(v) for v in pred[3])})"
    if k == "like":
        return f"{a}.{pred[2]} {'NOT ' if pred[4] else ''}LIKE {_lit(pred[3])}"
    if k == "null":
        return f"{a}.{pred[2]} IS {'' if pred[3] else 'NOT '}NULL"
    if k == "between":
        return f"{a}.{pred[2]} BETWEEN {_lit(pred[3])} AND {_lit(pred[4])}"
    raise ValueError(pred)


def like(text, pattern):
    """SQL LIKE with `%` only, as planir/expr.py's"""
    from kvevaluator.plans.ir.expr import _like
    return _like(text, pattern)


def holds(pred, row):
    """the predicate on a row dict {col: value}, with SQL's rule that a NULL operand makes an atom not true
    (used on the type tables, whose predicates are resolved when a plan is written)"""
    k = pred[0]
    if k == "and":
        return all(holds(p, row) for p in pred[1])
    if k == "or":
        return any(holds(p, row) for p in pred[1])
    if k == "null":
        return (row[pred[2]] is None) == pred[3]
    v = row[pred[3] if k == "cmp" else pred[2]]
    if v is None:
        return False
    if k == "cmp":
        op, x = pred[1], pred[4]
        return {"=": v == x, "!=": v != x, "<": v < x, "<=": v <= x, ">": v > x, ">=": v >= x}[op]
    if k == "in":
        return v in pred[3]
    if k == "like":
        return like(v, pred[3]) != pred[4]
    if k == "between":
        return pred[3] <= v <= pred[4]
    raise ValueError(pred)


def main():
    """write specs/<query>.json from the fetched SQL texts (output/imdb/job-queries): python3 -m kvevaluator.workloads.job.sqlspec"""
    import glob
    import json
    import os
    from kvevaluator.datasets.imdb import paths
    here = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(os.path.join(here, "specs"), exist_ok=True)
    files = glob.glob(os.path.join(paths.queries_dir(), "[0-9]*.sql"))
    for path in sorted(files):
        name = "job" + os.path.basename(path)[:-4]
        with open(os.path.join(here, "specs", f"{name}.json"), "w") as f:
            json.dump(parse(name, open(path).read()), f, indent=1, ensure_ascii=False)
            f.write("\n")
    print(f"wrote {len(files)} specs")


if __name__ == "__main__":
    main()
