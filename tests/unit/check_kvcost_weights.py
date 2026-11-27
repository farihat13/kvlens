#!/usr/bin/env python3
"""kvcost --op-weights G:S:N: one vector reaches both the plan pick and the cost, and a result names it. Toy vectors and
toy tables, no data, no store. Under a second.

    python3 tests/unit/check_kvcost_weights.py

Cases:
  1. parse      "1:30:0.01" is read; the default spelled 1:1.1:0.01 or 100:110:1 is DEFAULT itself; "100,110,1", two
                numbers and a zero are refused; the environment is never read (KVEVAL_WEIGHTS set: still DEFAULT)
  2. pick       two bodies, A = 10 SEEKs, B = 20 GETs: the default picks A (11 < 20); under 1:30:0.01 the criterion
                pick_rule_under gives picks B (20 < 300), and its plan rule has no record, anchor or pin
  3. cost       compose.read_cost and Composer.cost_vec cost read_ops as G + 30 S + 0.01 N under 1:30:0.01
  4. guard      a Composer with another vector but the default criterion, or with the pick record, is refused; the
                default spelled 1:1.1:0.01 makes the same Composer (one cost cache key)
  5. tables     a cost table row names its vector (predict-accuracy's two sides share one by their place's op-weights level); a
                table written before the column reads as the default; a result row with no weights keys as the default
Exit 1 when a case goes the wrong way. With criteria.with_weights made the identity (the pick left at the default
while the cost takes the vector), case 2 fails; on IS2 at sf0.1 the pick is the same under both vectors, so an
end-to-end IS2 run alone does not test the pick."""
import csv
import os
import sys
import tempfile

from kvcost.layout_cost import compose
from kvcost.layout_cost import cost_table as CT
from kvcost.layout_search import results as R
from kvevaluator import weights as W
from kvevaluator.plan_choice import candidates as C

OTHER = "1:30:0.01"
FAILED = []


def case(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail else ''}")
    if not ok:
        FAILED.append(name)


def refused(fn, *args, kind=Exception):
    try:
        fn(*args)
    except kind:
        return True
    return False


def check_parse():
    os.environ["KVEVAL_WEIGHTS"] = "1,30,0.01"            # not read: must change nothing
    case("parse: no spec is DEFAULT, the environment unread", W.parse(None) == W.DEFAULT)
    del os.environ["KVEVAL_WEIGHTS"]
    case("parse: 1:30:0.01", W.parse(OTHER) == {"G": 1.0, "S": 30.0, "N": 0.01})
    case("parse: the default spelled three ways is DEFAULT",
         W.parse("1:1.1:0.01") == W.DEFAULT == W.parse("100:110:1") == W.parse("3:3.3:0.03"))   # 3.3 / 3 != 1.1 in floats
    for bad in ("100,110,1", "1:2", "1:0:1", "a:b:c"):
        case(f"parse: {bad!r} refused", refused(W.parse, bad, kind=ValueError))
    case("text: the default reads 1:1.1:0.01", W.text(W.DEFAULT) == "1:1.1:0.01")


def check_pick():
    bodies = {"A": {"S": 10.0, "G": 0.0, "N": 0.0, "total_bytes": 0.0},
              "B": {"S": 0.0, "G": 20.0, "N": 0.0, "total_bytes": 0.0}}
    crit, rule = compose.pick_rule_under(None, compose.PlanRule(), W.DEFAULT)
    case("pick: the default criterion picks A", C.pick_body(bodies, crit)[0] == "A", f"criterion {crit}")
    crit, rule = compose.pick_rule_under(None, compose.PlanRule(), W.parse(OTHER))
    picked = C.pick_body(bodies, crit)[0]
    case("pick: under 1:30:0.01 the criterion picks B", picked == "B", f"picked {picked}, criterion {crit}")
    case("pick: no pick record, anchor or pin under another vector",
         rule.record is None and not rule.anchor and not rule.pin)


def check_cost():
    row = {"S": 2, "G": 3, "N": 100, **{b: 0 for b in compose.REPORT_BYTE_LANES}}
    w = W.parse(OTHER)
    want = 3 + 30 * 2 + 0.01 * 100
    got = compose.read_cost(row, w)["read_ops"]
    case("cost: read_cost under 1:30:0.01", abs(got - want) < 1e-9, f"{got} (want {want})")
    crit, rule = compose.pick_rule_under(None, compose.PlanRule(), w)
    comp = compose.Composer(criterion=crit, plan_rule=rule, weights=tuple(w[k] for k in "GSN"))
    got = compose.cost_vec({**row, "P": 0, "D": 0}, False, comp)["read_ops"]
    case("cost: cost_vec under the Composer's vector", abs(got - want) < 1e-9, f"{got}")
    got = compose.read_cost(row)["read_ops"]
    case("cost: the default unchanged", abs(got - (3 + 1.1 * 2 + 0.01 * 100)) < 1e-9, f"{got}")


def check_guard():
    other = (1.0, 30.0, 0.01)
    case("guard: another vector, the default criterion: refused", refused(
        lambda: compose.Composer(criterion=None, plan_rule=compose.PlanRule(record=None), weights=other), kind=ValueError))
    crit, _ = compose.pick_rule_under(None, compose.PlanRule(), W.parse(OTHER))
    case("guard: another vector, the pick record on: refused", refused(
        lambda: compose.Composer(criterion=crit, plan_rule=compose.PlanRule(), weights=other), kind=ValueError))
    a = compose.Composer(criterion=None, plan_rule=compose.PlanRule())
    b = compose.Composer(criterion=None, plan_rule=compose.PlanRule(), weights=(1, 1.1, 0.01))
    case("guard: the default spelled 1:1.1:0.01 is the same Composer", a == b and repr(a) == repr(b))


def check_tables():
    vec = {"read_ops": 1.0, "read_bytes": 0.0, "miss_key_bytes": 0.0, "write_ops": 0.0, "write_bytes": 0.0,
           "write_rmw_ops": 0.0, "write_rmw_bytes": 0.0}
    with tempfile.TemporaryDirectory() as tmp:
        pa, pb, old = (os.path.join(tmp, n) for n in ("a.csv", "b.csv", "old.csv"))
        CT.write(pa, [CT.row(CT.MEASURED, "props_split_adj_condensed", "interactive", "is2", 1, "x", 5, vec)])
        CT.write(pb, [CT.row(CT.MEASURED, "props_split_adj_condensed", "interactive", "is2", 1, "x", 5, vec,
                             weights=W.parse(OTHER))])
        case("tables: a row names its vector", CT.read(pb)[0]["weights"] == OTHER)
        with open(pa) as f:
            rows = list(csv.DictReader(f))
        with open(old, "w", newline="") as f:          # the table as written before the column
            w = csv.DictWriter(f, [c for c in rows[0] if c != "weights"], extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        case("tables: a table without the column reads as the default", CT.read(old)[0]["weights"] == "1:1.1:0.01")
    key = {k: None for k in R.KEY}
    case("tables: a result row with no weights keys as the default",
         R.row_key(key) == R.row_key({**key, "weights": W.text(W.DEFAULT)}))


def main():
    for fn in (check_parse, check_pick, check_cost, check_guard, check_tables):
        fn()
    print(f"check_kvcost_weights: {len(FAILED)} failed" + (f" ({', '.join(FAILED)})" if FAILED else ", all cases ok"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
