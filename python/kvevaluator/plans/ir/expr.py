"""The closed plan-IR expression grammar: an AST (nested one-key dicts) evaluated to a value against a row (filter
predicates, derive functions, sort keys). Deliberately small and closed: extend it here and in the C++ evaluator
(cpp/kvevaluator/src/ir/expr.cc) rather than embedding a lambda.

An expression is a bare literal (number/str/bool/None) or a one-key dict:
  {"field": name}      row[name]           (KeyError if absent -- a plan bug)
  {"get": name}        row.get(name)       (None if absent -- the safe read)
  {"lit": value}       value
  {"param": name}      the bound param value
  {"let": name}        a bind-time derived value
  {"eq|ne|lt|le|gt|ge": [a, b]}
  {"and|or": [a, ...]}   {"not": a}      (and/or return a BOOL, not the operand;
                                          for the `(x or default)` value idiom use coalesce)
  {"in": [needle, haystack]}             (membership; not-in = {"not": {"in": ...}})
  {"isnull|notnull": a}
  {"add|sub|mul|floordiv": [a, b]}
  {"div": [a, b]}              TRUE division (Python /): always a float (ic10's ord copies Dgraph's
                               float64 math, ties and all)
  {"concat": [a, ...]}         the operands' text joined; int and str only (no bool / float / None:
                               their text could differ between the Python and C++ backends)
  {"chain_root": [start, pairs]}   follow parent links from `start` through `pairs`, a list of (child, parent)
                               pairs (a later pair for the same child wins, as dict(pairs)); the end of the chain.
                               In memory, like every expression: no KV op (IS2's original post of a reply chain)
  {"like": [text, pattern]}   SQL LIKE, case-sensitive: `%` in the pattern matches any run of characters (also none),
                              every other character itself. Both operands are strings (a None text is a plan bug:
                              guard it with notnull); a pattern holding `_` (SQL's one-character wildcard) is
                              refused, so matching by bytes (C++) and by characters (Python) cannot differ.
                              In memory, 0 KV ops: a LIKE costs the read of the property it tests
  {"neg": a}
  {"coalesce": [a, ...]}      first non-None
  {"if": [cond, then, else]}  ternary
  {"tuple": [a, ...]}         a tuple (composite sort keys)
  {"map": [xs, body]}         a list: `body` evaluated once per element of the list or tuple `xs`, against the
                              one-field row {"item": element} (the body reads it as {"field": "item"} and sees no
                              outer row). In memory, 0 KV ops: getlink's list of ids -> their vids
  {"month_of": a}  {"day_of": a}   UTC month / day-of-month of an epoch-MILLISECONDS value
  {"year_of": a}              UTC year of an epoch-MILLISECONDS value (BI1's grouping year)
  {"key_id": a}               strip the tag off a vid (format-defined: identity where
                              the vid already IS the external id; see vids.key_id)
  {"vid": [label, raw]}       compose a vid
"""
import operator as _op
from datetime import datetime, timezone
from kvevaluator.schema import vids

# floordiv FLOORS (Python //) -- a C++/Nebula port must floor-divide, not truncate, so a
# neutral plan using floordiv/mod agrees on negative operands. (Live plans use it only on
# non-negative values: ids and epoch-ms dates.)
_BIN = {"eq": _op.eq, "ne": _op.ne, "lt": _op.lt, "le": _op.le, "gt": _op.gt,
        "ge": _op.ge, "add": _op.add, "sub": _op.sub, "mul": _op.mul,
        "floordiv": _op.floordiv, "div": _op.truediv}

_EXACT = 2 ** 53   # above it a C++ double holds an int only approximately: div refuses such an int operand on both sides


def _chain_root(start, pairs):
    """The end of `start`'s parent chain through (child, parent) pairs; a cycle is an error, not a hang."""
    parent = {}
    for child, par in pairs:
        parent[child] = par
    v = start
    for _ in range(len(parent) + 1):
        if v not in parent:
            return v
        v = parent[v]
    raise ValueError(f"chain_root: a cycle through {start!r}")


def _concat_text(v):
    """One concat operand's text: an int or a str only (bool is an int in Python: refused too)."""
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        raise TypeError(f"concat: int or str operands only, got {type(v).__name__} {v!r}")
    return str(v)


def _like(text, pattern):
    """SQL LIKE with `%` only (the C++ twin: ir/expr.cc like_match)."""
    if not isinstance(text, str) or not isinstance(pattern, str):
        raise TypeError(f"like: str operands only, got {type(text).__name__} and {type(pattern).__name__}")
    if "_" in pattern:
        raise ValueError(f"like: a pattern with '_' is not supported: {pattern!r}")
    parts = pattern.split("%")
    if len(parts) == 1:
        return text == pattern
    first, last = parts[0], parts[-1]
    end = len(text) - len(last)                 # the last part is matched at the end, the first at the start
    if end < len(first) or not text.startswith(first) or not text.endswith(last):
        return False
    pos = len(first)
    for part in parts[1:-1]:                    # the parts between, each at its leftmost place after the one before
        i = text.find(part, pos, end)
        if i < 0:
            return False
        pos = i + len(part)
    return True


def _dt(ms):
    """UTC datetime from an epoch-MILLISECONDS value (LDBC dates are ms)."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def evaluate(e, row, env):
    """Evaluate expression `e` against `row`; `env` = {"params": {...}, "lets": {...}}."""
    if not isinstance(e, dict) or len(e) != 1:
        return e                                   # a bare literal used directly
    (op, arg), = e.items()
    if op == "field":   return row[arg]
    if op == "get":     return row.get(arg)
    if op == "lit":     return arg
    if op == "param":   return env["params"][arg]
    if op == "let":     return env["lets"][arg]
    if op == "not":     return not evaluate(arg, row, env)
    if op == "neg":     return -evaluate(arg, row, env)
    if op == "isnull":  return evaluate(arg, row, env) is None
    if op == "notnull": return evaluate(arg, row, env) is not None
    if op == "and":     return all(evaluate(x, row, env) for x in arg)
    if op == "or":      return any(evaluate(x, row, env) for x in arg)
    if op == "tuple":   return tuple(evaluate(x, row, env) for x in arg)
    if op == "map":
        if len(arg) != 2:
            raise ValueError("map: expects [xs, body]")
        xs = evaluate(arg[0], row, env)
        if not isinstance(xs, (list, tuple)):      # a str is not a list here (C++ refuses it too)
            raise TypeError(f"map: expects a list, got {type(xs).__name__}")
        return [evaluate(arg[1], {"item": x}, env) for x in xs]
    if op == "concat":  return "".join(_concat_text(evaluate(x, row, env)) for x in arg)
    if op == "chain_root":
        if len(arg) != 2:
            raise ValueError("chain_root: expects [start, pairs]")
        return _chain_root(evaluate(arg[0], row, env), evaluate(arg[1], row, env))
    if op == "coalesce":
        for x in arg:
            v = evaluate(x, row, env)
            if v is not None:
                return v
        return None
    if op == "if":
        cond, then, els = arg
        return evaluate(then, row, env) if evaluate(cond, row, env) else evaluate(els, row, env)
    if op == "in":      # {"in": [needle, haystack]}  (not-in = {"not": {"in": ...}})
        needle, hay = arg
        return evaluate(needle, row, env) in evaluate(hay, row, env)
    if op == "like":
        if len(arg) != 2:
            raise ValueError("like: expects [text, pattern]")
        return _like(evaluate(arg[0], row, env), evaluate(arg[1], row, env))
    if op == "is_digit": return str(evaluate(arg, row, env)).isdigit()
    if op == "month_of": return _dt(evaluate(arg, row, env)).month   # UTC month of an epoch-ms field
    if op == "day_of":   return _dt(evaluate(arg, row, env)).day     # UTC day-of-month
    if op == "year_of":  return _dt(evaluate(arg, row, env)).year    # UTC year
    if op == "key_id":  return vids.key_id(evaluate(arg, row, env))
    if op == "vid":     return vids.vid(evaluate(arg[0], row, env), evaluate(arg[1], row, env))
    if op in _BIN:
        a, b = (evaluate(x, row, env) for x in arg)
        if op == "floordiv" and (isinstance(a, float) or isinstance(b, float)):   # as the C++ backend: not modelled
            raise TypeError("floordiv on a float: not in the value model")
        if op == "div" and any(isinstance(x, int) and not isinstance(x, bool) and abs(x) > _EXACT for x in (a, b)):
            raise ValueError("div: an int operand above 2^53 (C++ would round it before dividing)")
        return _BIN[op](a, b)
    raise ValueError(f"unknown expression op: {op!r}")


def compile_expr(ast, params=None, lets=None):
    """AST -> callable(row). Params/lets are captured now; row is supplied per call."""
    env = {"params": params or {}, "lets": lets or {}}
    return lambda row: evaluate(ast, row, env)


def _selftest():
    p = {"maxDate": 100, "days": 3}
    L = {"windowEnd": 100 + 3 * 86400000}
    cases = [
        ({"lt": [{"field": "d"}, {"param": "maxDate"}]}, {"d": 50}, True),
        ({"lt": [{"field": "d"}, {"param": "maxDate"}]}, {"d": 150}, False),
        ({"not": {"field": "used"}}, {"used": False}, True),
        ({"add": [{"field": "x"}, {"field": "y"}]}, {"x": 2, "y": 3}, 5),
        ({"mul": [{"param": "days"}, {"lit": 86400000}]}, {}, 3 * 86400000),
        ({"eq": [{"get": "type"}, {"lit": "country"}]}, {}, False),   # missing -> None != country
        ({"coalesce": [{"get": "a"}, {"lit": 9}]}, {"a": None}, 9),
        ({"if": [{"field": "c"}, {"lit": 1}, {"lit": -1}]}, {"c": True}, 1),
        ({"tuple": [{"field": "a"}, {"field": "b"}]}, {"a": 1, "b": 2}, (1, 2)),
        ({"map": [{"lit": [1, 2]}, {"add": [{"field": "item"}, {"lit": 10}]}]}, {"x": 0}, [11, 12]),
        ({"map": [{"lit": []}, {"field": "item"}]}, {}, []),
        ({"le": [{"field": "d"}, {"let": "windowEnd"}]}, {"d": L["windowEnd"]}, True),
        ({"in": [{"field": "b"}, {"lit": [1, 2, 3]}]}, {"b": 2}, True),
        ({"not": {"in": [{"field": "b"}, {"lit": [1, 2]}]}}, {"b": 9}, True),
        ({"div": [{"lit": 7}, {"lit": 2}]}, {}, 3.5),
        ({"add": [{"field": "s"}, {"div": [{"sub": [{"lit": 10 ** 15}, {"field": "id"}]}, {"lit": 10 ** 15}]}]},
         {"s": 3, "id": 933}, 3 + (10 ** 15 - 933) / 10 ** 15),
        ({"concat": [{"lit": "person_"}, {"field": "id"}]}, {"id": 933}, "person_933"),
        ({"like": [{"field": "n"}, {"lit": "%(co-production)%"}]}, {"n": "(as X) (co-production)"}, True),
        ({"like": [{"field": "n"}, {"lit": "B%"}]}, {"n": "aB"}, False),
        ({"like": [{"field": "n"}, {"lit": "%a%b%"}]}, {"n": "xbxa"}, False),
        ({"like": [{"field": "n"}, {"lit": "ab%ab"}]}, {"n": "ab"}, False),      # the ends may not overlap
    ]
    for ast, row, want in cases:
        got = compile_expr(ast, p, L)(row)
        assert got == want and type(got) is type(want), f"{ast} on {row}: got {got!r} want {want!r}"
    for bad in (True, 1.5, None):   # concat refuses what the backends could print differently
        try:
            compile_expr({"concat": [{"lit": "x"}, {"lit": bad}]})({})
        except TypeError:
            continue
        raise AssertionError(f"concat accepted {bad!r}")
    print(f"expr selftest OK ({len(cases)} cases)")


if __name__ == "__main__":
    _selftest()
