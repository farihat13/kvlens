"""One name per thing: a plan's body is a `body`, never a `variant`.
A `variant` is a physical design only: a writes run record's `variant` field (canonical,
label_only ...), an engine's variant build. Older captures and pick records name the body `variant`; they are read through
python/kvevaluator/capture/capture_layout.py.

A quoted "variant" (a key, a column, a field) or a CSV header naming a `variant` column, in the Python, C++ and shell
code and the root justfile, fails unless its file is in ALLOWED with the reason. An ALLOWED file with no such use left
fails too (the list is stale). The engines (engines/, python/kvengines/) are not scanned: their variants are builds.
The list is per file, so a new body-sense use inside an ALLOWED file passes: keep those files' uses few.

A Python name (a variable, function, class, module, attribute) with "pric" or "suite" in it fails the same way:
the composer is compose.py / Composer, a cost is a cost, the benchmark whose
captures a load reads is a `benchmark`, a workload's query list its `queries`; "price" is the bill's alone. A file in
NAME_ALLOWED keeps its names, for the reason given. Prose (comments, docstrings) is counted by check_layers.py's
glossary-word ratchet instead.

    python tests/lint/check_terms.py [--root <checkout>]
"""
import argparse
import io
import re
import sys
import tokenize
from pathlib import Path

from repo_files import repo_files
from kvevaluator.paths import ROOT

SCANNED = ("python/", "experiments/", "tests/", "run/", "cpp/", "paper/")
SKIPPED = ("python/kvengines/", "tests/lint/check_terms.py", "tests/lint/check_lints_catch_plants.py")
SUFFIXES = (".py", ".cc", ".h", ".sh")
USE = re.compile(r"""(["'])variant\1|[",]variant,|,variant[",]""")

# file: why its `variant` is not a body
ALLOWED = {
    "python/kvevaluator/capture/capture_layout.py": "layout 1's name for the body, read as `body`",
    "python/kvevaluator/measure/grid_writes.py": "the run record's `variant` field: a physical design (a schema)",
    "python/kvevaluator/measure/records_key.py": "the records stream's cache key names its physical design (a schema)",
    "tests/unit/check_records_key.py": "the records stream's cache key (records_key.py): a physical design",
    "tests/unit/check_capture_layout.py": "the helper's test: layout 1 rows and records, written to be read",
}


GLOSSARY_NAME = re.compile(r"pric|suite", re.IGNORECASE)
# file: why its price / suite names stay
NAME_ALLOWED = {
    "python/kvcost/layout_cost/cost_models.py": "WRITE_PRICE: the bill's price of a write unit",
    "experiments/layout_choice/plot_bill.py": "WRITE_PRICE: the bill's price of a write unit",
    "paper/derive/price_steps.py": "the paper's price-steps figure: its names are gs-paper's (renamed with the paper)",
    "paper/draw/fig_price_steps.py": "the paper's price-steps figure: its names are gs-paper's (renamed with the paper)",
    "experiments/core_suite/plot_kvstats.py": "the core_suite study's own names (a study rename waits for its owner)",
    "experiments/engine_fidelity/validate.py": "an engine-fidelity plan's status `suite` (vs `draft`): a value in frozen results",
    "paper/derive/engine_fidelity.py": "an engine-fidelity plan's status `suite` (vs `draft`): a value in frozen results",
    "tests/gates/check_engine_plans.py": "an engine-fidelity plan's status `suite` (vs `draft`): a value in frozen results",
    "experiments/logical_census/census.py": "the census CSV's `suite` column: frozen snapshots and the paper read it",
    "experiments/logical_census/plot_census.py": "the census CSV's `suite` column: frozen snapshots and the paper read it",
    "paper/derive/census_workloads.py": "the census CSV's `suite` column: frozen snapshots and the paper read it",
    "python/kvcost/latency/steps.py": "store_totals reads a.suite, which nothing sets: a no-caller retirement (backlog)",
}


def glossary_names(text):
    """{(line, name)} of the Python names in `text` that say price or suite (comments and strings are not names)"""
    out = set()
    for t in tokenize.generate_tokens(io.StringIO(text).readline):
        if t.type == tokenize.NAME and GLOSSARY_NAME.search(t.string):
            out.add((t.start[0], t.string))
    return out


def scan(root, allowed=None, name_allowed=None):
    """One 'path:line: rule: detail' line per problem, and the number of files scanned."""
    allowed = ALLOWED if allowed is None else allowed
    name_allowed = NAME_ALLOWED if name_allowed is None else name_allowed
    root = Path(root)
    problems, n, used, names_used = [], 0, set(), set()
    for rel in repo_files(root):
        if not (rel.startswith(SCANNED) or rel == "justfile") or rel.startswith(SKIPPED):
            continue
        if not (rel.endswith(SUFFIXES) or rel.endswith("justfile")):
            continue
        n += 1
        text = (root / rel).read_text(errors="replace")
        if rel.endswith(".py") and GLOSSARY_NAME.search(text):
            for i, name in sorted(glossary_names(text)):
                if rel in name_allowed:
                    names_used.add(rel)
                    continue
                problems.append(f"{rel}:{i}: glossary-name: `{name}` says price or suite: compose / cost / benchmark / "
                                f"queries (price is the bill's alone), or add {rel!r} to NAME_ALLOWED with the reason")
        for i, line in enumerate(text.split("\n"), 1):
            if not USE.search(line):
                continue
            if rel in allowed:
                used.add(rel)
                continue
            problems.append(f"{rel}:{i}: body-term: a plan's body is `body`, not `variant` (a physical design only); "
                            f"read an older capture through capture_layout.py, or add {rel!r} to ALLOWED with the reason")
    for rel in sorted(set(allowed) - used):
        if (root / rel).exists():
            problems.append(f"{rel}:0: stale-allow: ALLOWED names {rel} but it has no `variant` use: remove the entry")
    for rel in sorted(set(name_allowed) - names_used):
        if (root / rel).exists():
            problems.append(f"{rel}:0: stale-allow: NAME_ALLOWED names {rel} but it has no price / suite name: remove "
                            f"the entry")
    return problems, n


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=str(ROOT), help="the checkout to scan (default: this one)")
    a = ap.parse_args(argv)
    problems, n = scan(a.root)
    for p in problems:
        print(p)
    print(f"check_terms {'FAIL' if problems else 'OK'}: {len(problems)} problems in {n} files ({len(ALLOWED)} allowed; {len(NAME_ALLOWED)} keep price / suite names)")
    return 1 if problems or n == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
