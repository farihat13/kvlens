#!/usr/bin/env python3
"""The bill on predicted data: its lower and upper forms (compose.BILL_BOUNDS) come from a step row's requests and bytes
alone, so a predicted row (no histograms, no exact unit columns) gives them; the exact form needs a measured capture's
exact unit columns and is refused by name.
Synthetic rows, no data, no store. Under a second.

    python3 tests/unit/check_bill_bounds.py

Cases:
  1. cost_models.needs_bill: bill_lower and bill_upper "bounds", bill_exact "sized", another model "";
  2. a row with its histograms and the same row with them blank give the same bounds (bill_units sized=False);
  3. the blank row asked for the sized units is refused, naming bill_exact;
  4. Composer(bill="bounds") carries the bounds and no sized channel; an unknown setting (True) is refused;
  5. a bounds vector gives bill_lower and bill_upper and refuses bill_exact by name (no channel, no NaN);
  6. the sized units of a measured read row are unchanged by the split (the values before it, by hand);
  7. a measured write row: bill_write_exact = put_units_1k + del_units_1k; the same row without those columns refused,
     naming them; a read row without them (no PUT, no DELETE) is not refused.
Exit 1 when a case goes the wrong way."""
import contextlib
import io
import math
import sys

from kvcost.layout_cost import compose
from kvcost.layout_cost import cost_models as CM
from kvcost.layout_cost.picks import PlanRule

BAD = []


def check(name, ok, got=None):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f": got {got!r}"))
    if not ok:
        BAD.append(name)


def refusal(fn):
    """the SystemExit / ValueError message fn raises, else None"""
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            fn()
    except (SystemExit, ValueError) as e:
        return str(e)
    return None


MEASURED = {"query": "ic2", "step": "topk", "G": "3", "S": "2", "N": "40", "P": "0", "D": "0", "get_key": "60",
            "get_value": "9000", "scan_key": "800", "scan_value": "4000", "write_key": "0", "write_value": "0",
            "get_kvpair_hist": "0:1 13:2", "range_read_hist": "12:2", "put_kvpair_hist": "", "del_kvpair_hist": "",
            "get_fetch_4k": "4", "range_fetch_4k": "3"}
PREDICTED = {**MEASURED, "get_kvpair_hist": "", "range_read_hist": "", "get_fetch_4k": "", "range_fetch_4k": ""}


def vec(r):
    return {c: float(r.get(c) or 0) for c in compose.CHANNELS}


def main():
    # 1
    got = [CM.needs_bill(m) for m in ("bill_lower", "bill_upper", "bill_exact", "wops_bytes4k")]
    check("1 needs_bill", got == ["bounds", "bounds", "sized", ""], got)
    # 2
    a = compose.bill_units(MEASURED, vec(MEASURED), sized=False)
    b = compose.bill_units(PREDICTED, vec(PREDICTED), sized=False)
    check("2 the bounds need no histograms", a == b and set(a) == set(compose.BILL_BOUNDS), (a, b))
    # 3
    msg = refusal(lambda: compose.bill_units(PREDICTED, vec(PREDICTED), sized=True))
    check("3 the sized units of a predicted row refused, naming bill_exact", msg is not None and "bill_exact" in msg, msg)
    # 4
    keys = compose.Composer(criterion=None, plan_rule=PlanRule(), bill="bounds").keys
    check("4 a bounds load carries the bounds, no sized channel",
          set(compose.BILL_BOUNDS) <= set(keys) and not set(compose.BILL_SIZED) & set(keys), keys)
    msg = refusal(lambda: compose.Composer(criterion=None, plan_rule=PlanRule(), bill=True))
    check("4 bill=True refused (a setting, not a switch)", msg is not None and "Composer.bill" in msg, msg)
    # 5
    t = {k: 0.0 for k in keys}
    t.update(b)
    lo, hi = CM.cost(t, "bill_lower"), CM.cost(t, "bill_upper")
    check("5 bounds computed, lower <= upper", math.isfinite(lo) and math.isfinite(hi) and 0 < lo <= hi, (lo, hi))
    msg = refusal(lambda: CM.cost(t, "bill_exact"))
    check("5 bill_exact on a bounds vector refused by name", msg is not None and "bill_exact" in msg
          and "bill_read_exact" in msg, msg)
    # 6: the formula before the split: fetch 4 + 3, one empty GET (bucket 0) = 8; no writes
    s = compose.bill_units(MEASURED, vec(MEASURED), sized=True)
    want = {"bill_read_exact": 8.0, "bill_write_exact": 0.0,
            "bill_read_lower": max(3, 9060 / 4096) + max(2, 4800 / 4096), "bill_read_upper": 3 + 2 + 13860 / 4096,
            "bill_write_lower": 0.0, "bill_write_upper": 0.0}
    check("6 the sized units of a measured row as before", s == want and set(s) == set(compose.BILL), s)
    # 7: two PUTs of 1,500 and 300 bytes (2 + 1 units), a DELETE of unknown size (1)
    w = {**MEASURED, "P": "2", "D": "1", "write_key": "40", "write_value": "1760", "put_kvpair_hist": "9:1 11:1",
         "del_kvpair_hist": "u:1", "put_units_1k": "3", "del_units_1k": "1"}
    s = compose.bill_units(w, vec(w), sized=True)
    check("7 bill_write_exact = put_units_1k + del_units_1k", s["bill_write_exact"] == 4.0, s)
    msg = refusal(lambda: compose.bill_units({**w, "put_units_1k": "", "del_units_1k": ""}, vec(w), sized=True))
    check("7 a write row without the unit columns refused, naming them", msg is not None and "put_units_1k" in msg, msg)
    msg = refusal(lambda: compose.bill_units(MEASURED, vec(MEASURED), sized=True))
    check("7 a read row without them is not refused", msg is None, msg)
    print(f"check_bill_bounds {'FAIL' if BAD else 'OK'}: {7 - len({b[0] for b in BAD})}/7 cases"
          + (f"; failed {BAD}" if BAD else ""))
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
