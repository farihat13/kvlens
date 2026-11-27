"""Author filter predicates / derive functions as expression ASTs instead of Python
lambdas, so a plan serializes to the neutral IR. Each builder returns an `Expr`, which
is BOTH callable and carries `.ast`. The callable is compiled FROM the AST, so there is
one source of truth -- the callable can't drift from what serializes. Drop it in a plan
exactly where a lambda went:

    Step("recent", "filter", rows=ref("m"), pred=lt(field("creationDate"), end))
    Step("score",  "derive", into="s", fn=add(field("xCount"), field("yCount")))

A bare Python value in a builder arg is taken as a literal, so `lt(field("d"), end)` with
`end` an int works. Serialize (serialize.py) emits `expr.ast` in place of the lambda.
"""
from kvevaluator.plans.ir.expr import compile_expr


class Expr:
    __slots__ = ("ast", "_fn")

    def __init__(self, ast):
        self.ast = ast
        self._fn = compile_expr(ast)          # the callable IS the AST -- no drift

    def __call__(self, row):
        return self._fn(row)


def _a(x):
    """An operand: an Expr contributes its AST; a bare value is a literal."""
    return x.ast if isinstance(x, Expr) else {"lit": x}


def field(name):    return Expr({"field": name})     # row[name]  (missing -> KeyError)
def get(name):      return Expr({"get": name})       # row.get(name)  (missing -> None)
def lit(v):         return Expr({"lit": v})
def param(name):    return Expr({"param": name})     # for a bind-time value (rare in baked plans)
def let(name):      return Expr({"let": name})


def _bin(op):
    def f(a, b):
        return Expr({op: [_a(a), _a(b)]})
    return f


eq, ne, lt, le, gt, ge = (_bin(o) for o in ("eq", "ne", "lt", "le", "gt", "ge"))
add, sub, mul, floordiv = (_bin(o) for o in ("add", "sub", "mul", "floordiv"))
div = _bin("div")                                   # true division: a float (Python /)
def concat(*xs):    return Expr({"concat": [_a(x) for x in xs]})   # text of int / str operands, joined
def like(text, pattern):   return Expr({"like": [_a(text), _a(pattern)]})   # SQL LIKE, `%` only; no KV op
def chain_root(start, pairs):   # the end of start's parent chain through (child, parent) pairs; no KV op
    return Expr({"chain_root": [_a(start), _a(pairs)]})


def and_(*xs):      return Expr({"and": [_a(x) for x in xs]})
def or_(*xs):       return Expr({"or": [_a(x) for x in xs]})
def not_(x):        return Expr({"not": _a(x)})
def isnull(x):      return Expr({"isnull": _a(x)})
def notnull(x):     return Expr({"notnull": _a(x)})
def coalesce(*xs):  return Expr({"coalesce": [_a(x) for x in xs]})
def if_(c, t, e):   return Expr({"if": [_a(c), _a(t), _a(e)]})
def tuple_(*xs):    return Expr({"tuple": [_a(x) for x in xs]})
def map_(xs, body): return Expr({"map": [_a(xs), _a(body)]})   # body per element; it reads item()
def item():         return Expr({"field": "item"})          # the element, inside map_'s body
def in_(x, xs):     return Expr({"in": [_a(x), _a(xs)]})
def key_id(x):      return Expr({"key_id": _a(x)})
def month_of(x):    return Expr({"month_of": _a(x)})   # UTC month of an epoch-ms field
def day_of(x):      return Expr({"day_of": _a(x)})     # UTC day-of-month
def year_of(x):     return Expr({"year_of": _a(x)})    # UTC year
def is_digit(x):    return Expr({"is_digit": _a(x)})   # str(x).isdigit()
def make_vid(label, raw):
    """Encode a vid from a NUMERIC raw id (no digit guard -- for a computed synthetic id, unlike
    compose_vid which passes a non-digit through). Uses the existing `vid` grammar op."""
    return Expr({"vid": [_a(label), _a(raw)]})
def mod(a, b):
    """a % b, composed from existing ops (a - (a // b) * b) -- no `mod` grammar op needed. FLOOR
    semantics (floordiv floors, matching Python %); a C++/Nebula port must floor-divide, not
    truncate, to agree on negative operands (LDBC ids are non-negative, so no live gap)."""
    return sub(a, mul(floordiv(a, b), b))
def compose_vid(label, raw):
    """Match person_vid: compose the encoded vid from a bare numeric id, else pass the
    id through (so demo vids like "A" work). is_digit short-circuits, so vid() is never
    called on a non-digit."""
    r = _a(raw)
    return Expr({"if": [{"is_digit": r}, {"vid": [_a(label), r]}, r]})
