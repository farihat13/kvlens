#!/usr/bin/env python3
"""The text of a refused twin step (oracle/runner.py run_plan, for each of Per-type CSR's count caveats): it names the
format, the step, the op, the reason, the case not supported, and a source path that exists in the tree. These are the
strings tests/gates/check_twin_count_guard.py's case (a) matches on the sf0.1 oracle stores, checked here store-free,
so a text edit fails in the fast tests rather than late in the round (a moved doc path in this text once failed a
full gate round). A stand-in format and store: no data, no store, no binary; under a second.

Run:  python3 tests/unit/check_twin_refusal_text.py
"""
import re
import sys

from kvevaluator.cost import KVCost, TwinCountsNotOracle, TwinCountsRefused
from kvevaluator.formats.pertype_csr import PerTypeCsrFormat
from kvevaluator.oracle.runner import run_plan
from kvevaluator.paths import ROOT
from kvevaluator.plans.plan import Step

# each caveat, and the word the guard's case (a) finds in its refusal (check_twin_count_guard.py, plan())
CAVEATS = {"_CAVEAT_LABEL": "id's group", "_CAVEAT_BLOCK": "label block", "_CAVEAT_INDEX": "one key per entry",
           "_CAVEAT_DELETE": "reads its own lists twice"}
# what every refusal names, as the guard's text case asks (its "the refusal text names the format, the step ...")
ALWAYS = ("pertype_csr", "'replies'", "read_neighbors", "formats/pertype_csr.py", "far-end label")


class Store:
    """the two calls run_plan makes on a store"""
    def reset_counter(self):
        self.cost = KVCost(S=1)


class Twin:
    """a format whose one read returns a row and leaves the given caveat"""
    name = "pertype_csr"

    def __init__(self, caveat):
        self.caveat, self.pending = caveat, None

    def read_neighbors(self, store, **_args):
        self.pending = self.caveat
        return [7], KVCost(S=1)

    def take_count_caveat(self):
        c, self.pending = self.pending, None
        return c


def refusal(caveat):
    """the text read back from the refused step's cost"""
    _outs, kv = run_plan([Step("replies", "read_neighbors", srcs=[1], etype="replyOf", dir="in")], Twin(caveat), Store())
    cost = kv[0][2]
    if not isinstance(cost, TwinCountsRefused):
        return None
    try:
        cost.S
    except TwinCountsNotOracle as e:
        return str(e)
    return None


def main():
    fails, cases = [], 0

    def case(what, ok, got=""):
        nonlocal cases
        cases += 1
        print(f"  {'ok  ' if ok else 'FAIL'} {what}" + ("" if ok else f": {str(got)[:240]}"))
        if not ok:
            fails.append(what)

    found = {k for k in vars(PerTypeCsrFormat) if k.startswith("_CAVEAT_")}
    case("every caveat of the format has its word here", found == set(CAVEATS), sorted(found ^ set(CAVEATS)))
    for name, word in CAVEATS.items():
        text = refusal(getattr(PerTypeCsrFormat, name, ""))
        case(f"{name}: the step is refused", text is not None)
        text = text or ""
        case(f"{name}: the text holds its reason ({word!r})", word in text, text)
        missing = [w for w in ALWAYS if w not in text]
        case(f"{name}: the text names the format, the step, the op, the source and the case not supported",
             not missing, f"missing {missing}")
        paths = re.findall(r"(?:python|docs|tests|cpp)/[\w/.-]+\.(?:py|md|cc|h)", text)
        gone = [p for p in paths if not (ROOT / p).is_file()]
        case(f"{name}: every path in the text exists ({len(paths)})", paths and not gone, gone)
    # the control: a step with no caveat keeps its counts
    _outs, kv = run_plan([Step("replies", "read_neighbors", srcs=[1], etype="replyOf", dir="in")], Twin(None), Store())
    case("control: no caveat, the counts come back", isinstance(kv[0][2], KVCost) and kv[0][2].S == 1, kv[0][2])

    print(f"check_twin_refusal_text {'OK' if not fails else 'FAIL'}: {cases} cases over {len(CAVEATS)} caveats, "
          f"{len(fails)} failed")
    return 1 if fails or not cases else 0


if __name__ == "__main__":
    sys.exit(main())
