"""Generate tests/fixtures/expr.json: expression-grammar cases as PYTHON (planir/expr.py) evaluates them, for the C++
parity test (test_expr.cc). Each case: the AST, the row, and Python's result -- its JSON text, type and neutral
byte size (graph.value_bytes), or the error.

    python cpp/kvevaluator/tests/gen_expr_fixtures.py           (from the repo root, in its venv)
    python cpp/kvevaluator/tests/gen_expr_fixtures.py --check   (the committed file still matches expr.py: drift check)

Also `neutral_order`: values sorted by Python's _neutral_key (nest's order), for C++ neutral_compare.
"""
import json
import os
import sys

from kvevaluator.schema.graph import value_bytes
from kvevaluator.formats.base import _neutral_key
from kvevaluator.plans.ir.expr import compile_expr

E15 = 10 ** 15
CASES = [   # (ast, row)
    ({"like": [{"field": "t"}, {"lit": '%(co-production)%'}]}, {"t": '(as X) (co-production)'}),
    ({"like": [{"field": "t"}, {"lit": '%(co-production)%'}]}, {"t": '(presents)'}),
    ({"like": [{"field": "t"}, {"lit": 'B%'}]}, {"t": 'Bob'}),
    ({"like": [{"field": "t"}, {"lit": 'B%'}]}, {"t": 'aBob'}),
    ({"like": [{"field": "t"}, {"lit": '%'}]}, {"t": 'x'}),
    ({"like": [{"field": "t"}, {"lit": '%'}]}, {"t": ''}),
    ({"like": [{"field": "t"}, {"lit": ''}]}, {"t": ''}),
    ({"like": [{"field": "t"}, {"lit": 'abc'}]}, {"t": 'abc'}),
    ({"like": [{"field": "t"}, {"lit": 'abc'}]}, {"t": 'abcd'}),
    ({"like": [{"field": "t"}, {"lit": '%Downey%Robert%'}]}, {"t": 'Downey, Robert Jr.'}),
    ({"like": [{"field": "t"}, {"lit": '%Downey%Robert%'}]}, {"t": 'Robert Downey'}),
    ({"like": [{"field": "t"}, {"lit": '%a%b%'}]}, {"t": 'xbxa'}),
    ({"like": [{"field": "t"}, {"lit": 'ab%ab'}]}, {"t": 'ab'}),
    ({"like": [{"field": "t"}, {"lit": 'ab%ab'}]}, {"t": 'abab'}),
    ({"like": [{"field": "t"}, {"lit": 'ab%ab'}]}, {"t": 'abxab'}),
    ({"like": [{"field": "t"}, {"lit": 'a%b%c'}]}, {"t": 'aXbXc'}),
    ({"like": [{"field": "t"}, {"lit": 'a%%c'}]}, {"t": 'ac'}),
    ({"like": [{"field": "t"}, {"lit": '%ström%'}]}, {"t": 'Ã…ngström (voice)'}),
    ({"like": [{"field": "t"}, {"lit": '日本%タイトル'}]}, {"t": '日本語のタイトル'}),
    ({"like": [{"field": "t"}, {"lit": '%ss%ss%pi'}]}, {"t": 'mississippi'}),
    ({"like": [{"field": "t"}, {"lit": 'm%ss%ss%ss%'}]}, {"t": 'mississippi'}),
    ({"like": [{"field": "t"}, {"lit": 'a_b'}]}, {"t": 'a_b'}),
    ({"like": [{"get": "missing"}, {"lit": "%a%"}]}, {}),              # refused: a None text
    ({"like": [{"lit": "a"}, {"lit": 1}]}, {}),                        # refused: a pattern that is no string
    ({"like": [{"lit": "a"}]}, {}),                                    # arity
    ({"div": [{"lit": 7}, {"lit": 2}]}, {}),
    ({"div": [{"lit": 1}, {"lit": 3}]}, {}),
    ({"div": [{"lit": 6}, {"lit": 3}]}, {}),                        # an integral float: 2.0
    ({"div": [{"lit": -7}, {"lit": 2}]}, {}),
    ({"div": [{"lit": True}, {"lit": 4}]}, {}),                     # bool counts as int
    ({"div": [{"lit": E15}, {"lit": 1}]}, {}),                      # 1e15 as a float: its JSON text
    ({"div": [{"lit": 10 ** 16}, {"lit": 1}]}, {}),
    ({"div": [{"lit": 1}, {"lit": 10 ** 6}]}, {}),                  # 1e-06
    ({"div": [{"lit": 1}, {"lit": 0}]}, {}),                        # ZeroDivisionError
    # ic10's ord: score + (1e15 - id) / 1e15, for ids that a double cannot tell apart at this size
    ({"add": [{"field": "s"}, {"div": [{"sub": [{"lit": E15}, {"field": "id"}]}, {"lit": E15}]}]}, {"s": 3, "id": 933}),
    ({"add": [{"field": "s"}, {"div": [{"sub": [{"lit": E15}, {"field": "id"}]}, {"lit": E15}]}]}, {"s": 3, "id": 934}),
    ({"add": [{"field": "s"}, {"div": [{"sub": [{"lit": E15}, {"field": "id"}]}, {"lit": E15}]}]},
     {"s": 812, "id": 30786325583618}),
    ({"sub": [{"div": [{"lit": 1}, {"lit": 4}]}, {"lit": 1}]}, {}),  # float - int
    ({"mul": [{"div": [{"lit": 1}, {"lit": 4}]}, {"lit": 3}]}, {}),
    ({"neg": {"div": [{"lit": 1}, {"lit": 4}]}}, {}),
    ({"eq": [{"div": [{"lit": 6}, {"lit": 3}]}, {"lit": 2}]}, {}),   # 2.0 == 2
    ({"lt": [{"div": [{"lit": 7}, {"lit": 2}]}, {"lit": 4}]}, {}),
    ({"gt": [{"div": [{"lit": 7}, {"lit": 2}]}, {"lit": 3}]}, {}),
    ({"concat": [{"lit": "person_"}, {"field": "id"}]}, {"id": 933}),
    ({"concat": [{"lit": "a"}, {"lit": "b"}, {"lit": -5}]}, {}),
    ({"concat": []}, {}),
    ({"concat": [{"lit": "x"}, {"lit": True}]}, {}),               # refused: bool
    ({"concat": [{"lit": "x"}, {"div": [{"lit": 1}, {"lit": 2}]}]}, {}),   # refused: float
    ({"concat": [{"lit": "x"}, {"get": "missing"}]}, {}),           # refused: None
    ({"floordiv": [{"div": [{"lit": 7}, {"lit": 2}]}, {"lit": 1}]}, {}),   # refused: float floordiv
    ({"tuple": [{"div": [{"lit": 1}, {"lit": 2}]}, {"lit": "k"}]}, {}),
    ({"chain_root": [{"field": "v"}, {"field": "chain"}]}, {"v": "c3", "chain": [["c1", "p0"], ["c2", "c1"], ["c3", "c2"]]}),
    ({"chain_root": [{"field": "v"}, {"field": "chain"}]}, {"v": "p0", "chain": [["c1", "p0"]]}),   # already the root
    ({"year_of": {"field": "d"}}, {"d": 1293840000000}),             # 2011-01-01T00:00:00Z -> 2011
    ({"year_of": {"field": "d"}}, {"d": 1293839999999}),             # one ms before -> 2010
    ({"year_of": {"field": "d"}}, {"d": 1330560000000}),             # 2012-02-29 (a leap day) -> 2012
    ({"year_of": {"field": "d"}}, {"d": -86400000}),                 # 1969-12-31 -> 1969 (floor, not truncate)
    ({"year_of": {"field": "d"}}, {"d": 0}),                         # the epoch -> 1970
    ({"chain_root": [{"field": "v"}, {"field": "chain"}]}, {"v": "x", "chain": []}),
    ({"chain_root": [{"field": "v"}, {"field": "chain"}]}, {"v": "a", "chain": [["a", "b"], ["a", "c"]]}),   # last wins
    ({"chain_root": [{"field": "v"}, {"field": "chain"}]}, {"v": "a", "chain": [["a", "b"], ["b", "a"]]}),   # a cycle
    ({"chain_root": [{"field": "v"}]}, {"v": "a"}),                                      # arity
    # 1 == True == 1.0 as keys (Python dict; C++ ValueHash / ValueEq): the int key is found from a float or a bool start
    ({"chain_root": [{"div": [{"lit": 2}, {"lit": 2}]}, {"lit": [[1, "a"]]}]}, {}),
    ({"chain_root": [{"lit": True}, {"lit": [[1, "a"]]}]}, {}),
    # int vs float, exact around 2^53 (2^53 + 1 is not 2.0^53)
    ({"eq": [{"lit": 2 ** 53 + 1}, {"div": [{"lit": 2 ** 53}, {"lit": 1}]}]}, {}),
    ({"gt": [{"lit": 2 ** 53 + 1}, {"div": [{"lit": 2 ** 53}, {"lit": 1}]}]}, {}),
    ({"eq": [{"lit": 2 ** 53}, {"div": [{"lit": 2 ** 53}, {"lit": 1}]}]}, {}),
    ({"lt": [{"lit": 2 ** 60}, {"div": [{"lit": 1}, {"lit": 2}]}]}, {}),
    ({"lt": [{"lit": -(2 ** 60)}, {"div": [{"lit": 1}, {"lit": 2}]}]}, {}),
    ({"div": [{"lit": 2 ** 53 + 1}, {"lit": 3}]}, {}),                              # refused: int above 2^53
    # map: the body per element against the row {"item": x}; getlink's id list -> vids (compose_vid's if / is_digit)
    ({"map": [{"field": "ids"}, {"if": [{"is_digit": {"field": "item"}}, {"concat": [{"lit": "Node_"}, {"field": "item"}]},
                                        {"field": "item"}]}]}, {"ids": [3, "A", 12]}),
    ({"map": [{"tuple": [{"lit": 1}, {"lit": 2}]}, {"mul": [{"field": "item"}, {"lit": 2}]}]}, {}),      # a tuple operand
    ({"map": [{"lit": []}, {"field": "item"}]}, {}),                                                      # empty -> []
    ({"in": [{"field": "d"}, {"map": [{"field": "ids"}, {"add": [{"field": "item"}, {"lit": 1}]}]}]}, {"d": 5, "ids": [1, 4]}),
    ({"map": [{"field": "xs"}, {"map": [{"field": "item"}, {"field": "item"}]}]}, {"xs": [[1, 2], [3]]}),  # inner item shadows
    ({"map": [{"lit": "abc"}, {"field": "item"}]}, {}),                     # refused: a str is not a list
    ({"map": [{"lit": 7}, {"field": "item"}]}, {}),                         # refused: an int
    ({"map": [{"lit": [1]}, {"field": "x"}]}, {"x": 1}),                    # refused: the body sees no outer row
    ({"map": [{"lit": [1]}]}, {}),                                          # arity
]

NEUTRAL = [None, True, False, 3, 2.5, -1, 10.5, 9.5, 2 ** 53 + 1, 2.0 ** 53, "a", "B", [1, "x"], [1, 2.5], (0,), [], ""]


def build():
    out = []
    for ast, row in CASES:
        try:
            v = compile_expr(ast)(row)
            out.append({"ast": ast, "row": row, "type": type(v).__name__, "json": json.dumps(v),
                        "bytes": value_bytes(v)})   # Python's neutral byte model, for C++ neutral_bytes
        except Exception as e:
            out.append({"ast": ast, "row": row, "error": type(e).__name__})
    order = sorted(NEUTRAL, key=_neutral_key)
    return {"cases": out, "neutral_order": json.loads(json.dumps(order))}


def main():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "expr.json")
    text = json.dumps(build(), indent=1) + "\n"
    if "--check" in sys.argv:
        if open(path).read() != text:
            sys.exit(f"DRIFT: {path} no longer matches planir/expr.py -- regenerate it (and re-run test_expr)")
        print(f"expr fixtures OK: {path} matches planir/expr.py")
        return
    with open(path, "w") as f:
        f.write(text)
    print(f"{len(json.loads(text)['cases'])} cases + neutral order -> {path}")


if __name__ == "__main__":
    main()
