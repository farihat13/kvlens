"""Each lint passes a clean tree and catches every planted violation (a lint that catches nothing is not a check).

    python tests/lint/check_lints_catch_plants.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import check_code_only
import check_duplicate_code
import check_kvcost_calls_parse
import check_layers
import check_lint_tools
import check_no_research_repo
import check_paths
import check_pick_files_read
import check_planner_pick_reads
import check_plot_style
import check_terms
import check_tests_reached
from kvevaluator.paths import ROOT

# 16 lines of logic; a copy of it with other names is a duplicate
DUPLICATED = """def total(rows):
    out = {}
    for row in rows:
        key = row["a"]
        out[key] = out.get(key, 0) + row["b"]
        if key > 3:
            out[key] -= 1
        else:
            out[key] += 1
    best = max(out, key=out.get)
    if best is None:
        return 0, 0
    share = out[best] / max(1, len(rows))
    return best, share
"""

CLEAN = {
    "README.md": "# demo\n",
    "python/kvevaluator/__init__.py": '"""demo."""\nimport json\nimport numpy\n',
    "python/kvcost/__init__.py": "from kvevaluator import paths\n",
    "python/kvevaluator/paths.py": "ROOT = None\n",
    "experiments/demo/run.py": "import helper\nimport kvcost\n",
    "experiments/demo/helper.py": "X = 1\n",
    "python/kvcost/plotting/__init__.py": "",
    "python/kvcost/plotting/style.py": "def apply_house_style():\n    return None\n",
    "experiments/demo/plot_demo_ok.py": "from kvcost.plotting import style\nstyle.apply_house_style()\n",   # a plotter on the style
    "inputs/params.txt": "1\n",
    # allowed: the one way a driver reaches run/env.sh, and a location set for a child process (not a read)
    "experiments/demo/run-demo.sh": 'source "$(dirname "${BASH_SOURCE[0]}")/../../run/env.sh"\nKVEVAL_OUTPUT_DIR=o true\n',
    "experiments/demo/child.py": "import os\nenv = dict(os.environ, KVEVAL_STORE_DIR='s')\nos.environ['KVEVAL_CPP_BIN'] = 'b'\n",
    # allowed: writing the runs CSV's planner_pick (a capture), not reading it
    "experiments/demo/capture.py": "row = {'planner_pick': 1}\nrow['planner_pick'] = 0\nCOLS = ('query', 'planner_pick')\n",
    # kvevaluator's units: a workload reads a dataset, a format its listed base and the shared base, the registry
    # (formats/*.cc) includes every format; a registered name used inside its own unit is not hand-naming
    "python/kvevaluator/datasets/__init__.py": "",
    "python/kvevaluator/workloads/__init__.py": "",
    "python/kvevaluator/formats/__init__.py": "",
    "python/kvevaluator/datasets/d1/__init__.py": "X = 1\n",
    "python/kvevaluator/datasets/d1/manifest.json": "{}\n",
    "python/kvevaluator/workloads/w1/__init__.py": "from kvevaluator.datasets.d1 import X\n",
    "python/kvevaluator/workloads/w1/manifest.json": "{}\n",
    "python/kvevaluator/workloads/w1/queries.py": "NAME = 'w1'\n",
    "python/kvevaluator/workloads/w2/__init__.py": "X = 2\n",
    "python/kvevaluator/formats/base.py": "class F:\n    pass\n",
    "python/kvevaluator/formats/dgraph.py": "from kvevaluator.formats.base import F\n",
    "python/kvevaluator/formats/dgraph_engine.py": "from kvevaluator.formats.dgraph import F\n",
    "python/kvevaluator/formats/meta.py": "from kvevaluator.formats.base import F\n",
    "cpp/kvevaluator/src/formats/registry.cc": '#include "formats/meta/format.h"\n#include "formats/dgraph/format.h"\n',
    "cpp/kvevaluator/src/formats/meta/format.cc": '#include "formats/meta/format.h"\n#include "formats/common.h"\n',
    # hand-naming already recorded is allowed (the record, and the name it records)
    "python/kvcost/named.py": "WORKLOAD = 'w1'\n",
    # the structure ratchets: the homes may hold what the counts forbid elsewhere (the weights and cost models, a
    # workload's own unit, the dataset that reads its param set), and a recorded count is allowed
    "python/kvevaluator/weights.py": "DEFAULT = {'G': 100, 'S': 110, 'N': 1}\nSEEK_PER_GET = 1.1\nCOST = 1.1 * 2 + 0.01\n",
    "python/kvcost/layout_cost/cost_models.py": "BYTES_PER_OP = 4096\nSUM = 3 / 4096\nWRITE_PRICE = 5.0   # the bill's\n",
    "python/kvcost/layout_cost/workloads/ldbc.py": "I01 = 'mix'\nW17 = 'reads'\n",
    "python/kvevaluator/datasets/ldbc/params.py": "import os\nSET = os.environ.get('LDBC_PARAMS', 'ldbc')\n",
    # a fixture row of per-lane values is not a lane list
    "python/kvcost/fixture_row.py": "ROW = {'get_key': 1, 'get_value': 2, 'scan_key': 3}\n",
    # the standard tools' counts: this tree's imports are unused on purpose (they test the layer rules)
    "tests/lint/structure_ratchets.json": json.dumps({"counts": {
        "env-read": {"python/kvevaluator/datasets/ldbc/params.py": 1},
        "ruff-f": {"experiments/demo/run.py": 2, "python/kvcost/__init__.py": 1, "python/kvevaluator/__init__.py": 2,
                   "python/kvevaluator/formats/dgraph.py": 1, "python/kvevaluator/formats/dgraph_engine.py": 1,
                   "python/kvevaluator/formats/meta.py": 1, "python/kvevaluator/workloads/w1/__init__.py": 1},
        "vulture": {"experiments/demo/run.py": 2, "python/kvevaluator/formats/dgraph.py": 1,
                    "python/kvevaluator/formats/dgraph_engine.py": 1, "python/kvevaluator/formats/meta.py": 1}},
        "reasons": []}) + "\n",
    "python/kvcost/source_a.py": DUPLICATED,
    # tests reached: one named by a runner (it imports a helper), one listed by hand with a trigger
    "tests/fast_tests.txt": "tests/unit/check_a.py\n",
    "tests/unit/check_a.py": "from helper_a import X\nprint(X)\n",
    "tests/unit/helper_a.py": "X = 1\n",
    "tests/gates/check_b.py": "print(1)\n",
    "tests/by_hand.txt": "# test | triggers | cost | note\ntests/gates/check_b.py | tests/gates/* | seconds | demo\n",
    "tests/lint/hand_naming.json": '{"counts": {"workload": {"python/kvcost/named.py": 1}, "dataset": {}}, "reasons": []}\n',
}

# (lint, rule tag it must print, file, content)
PLANTS = [
    (check_code_only, "kind:", "docs/notes.md", "# notes\n"),
    (check_code_only, "kind:", "REPRODUCE.md", "# only the root's README.md ships\n"),
    (check_code_only, "kind:", "paper/figure.pdf", "%PDF\n"),
    (check_code_only, "kind:", "inputs/blob.gz", "x"),
    (check_code_only, "size:", "inputs/big.csv", "1\n" * (check_code_only.MAX_BYTES // 2 + 1)),
    (check_paths, "machine-path:", "run/b.py", "OUT = '/mnt/data/out'\n"),
    (check_paths, "root-walk:", "tests/lint/check_w.py",
     'import glob, os\nEXP = "."\nfor p in glob.glob(os.path.join(EXP, "**", "*.py"), recursive=True):\n    pass\n'),
    (check_paths, "root-walk:", "tests/lint/check_v.py", 'import os\nfrom pathlib import Path\nROOT = Path(".")\nfiles = list(ROOT.rglob("*.py"))\n'),
    (check_paths, "schema-path:", "tests/gates/check_s.py",
     'import os\nEXP = "."\nS = os.path.join(EXP, "python", "kvevaluator", "datasets", "ldbc", f"schema_{1}.json")\n'),
    (check_paths, "schema-path:", "run/s.sh", 'S="$ROOT/python/kvevaluator/datasets/linkbench/schema.json"\n'),
    (check_paths, "schema-path:", "run/t.sh", 'S="$ROOT/inputs/schemas/ldbc/schema_label_only.json"\n'),
    (check_paths, "schema-path:", "tests/gates/check_t.py",
     'import os\nEXP = "."\nS = os.path.join(EXP, "inputs", "schemas", "imdb", "schema.json")\n'),
    (check_paths, "sys-path:", "run/c.py", "import sys\nsys.path.insert(0, 'x')\n"),
    (check_paths, "root-math:", "run/d.py", "import os\nR = os.path.dirname(os.path.dirname(__file__))\n"),
    (check_paths, "root-math:", "run/e.sh", 'R="$(dirname "${BASH_SOURCE[0]}")/../.."\n'),
    (check_paths, "parent-hops:", "run/m.py", 'import os\nhere = os.path.dirname(__file__)\nR = os.path.join(here, "..", "..")\n'),
    (check_paths, "parent-hops:", "run/n.py", 'import os\nR = os.path.join(os.path.dirname(__file__), *[".."] * 3)\n'),
    (check_paths, "root-override:", "run/f.py", "import os\nR = os.environ.get('KVEVAL_ROOT')\n"),
    (check_paths, "root-override:", "run/g.sh", 'R="${KVEVAL_REPO:-.}"\n'),
    (check_paths, "location-read:", "run/h.py", "import os\nD = os.environ['KVEVAL_DATA_DIR']\n"),
    (check_paths, "location-read:", "run/i.py", "import os\nD = os.environ.get('KVEVAL_STORE_DIR')\n"),
    (check_paths, "location-read:", "run/j.sh", 'B="${KVEVAL_BUILD_DIR:-build}"\n'),
    (check_paths, "data-dir:", "run/o.py", "from kvevaluator.paths import DATA_DIR\nG = DATA_DIR / 'sf3' / 'grid'\n"),
    (check_paths, "data-dir:", "experiments/demo/p.sh", 'G="$DATA_DIR/sf3/grid"\n'),
    (check_paths, "env-source:", "run/k.sh", "source /opt/artifact/run/env.sh\n"),
    (check_paths, "env-source:", "experiments/demo/l.sh", 'cd "$(dirname "$0")" && source ../../run/env.sh\n'),
    (check_layers, "layer:", "python/kvevaluator/x.py", "import kvcost\n"),
    (check_layers, "layer:", "python/kvcost/y.py", "from kvengines import client\n"),
    (check_layers, "unresolved:", "python/kvcost/z.py", "import layout_pricing\n"),
    (check_layers, "unresolved:", "experiments/demo/w.py", "import other_study_module\n"),
    # a study imports only the shared packages and its own modules, never a sibling study's
    (check_layers, "unresolved:", "experiments/demo/v.py", "from experiments.other_study import helper\n"),
    # kvevaluator's units: a dataset knows no workload, a workload no other workload, a format neither, nor another
    # format it does not list; an unknown unit or format module; a C++ format including another's dir
    (check_layers, "unit:", "python/kvevaluator/datasets/d1/reader.py", "from kvevaluator.workloads.w1 import queries\n"),
    (check_layers, "unit:", "python/kvevaluator/workloads/w2/gen.py", "from kvevaluator.workloads.w1.queries import NAME\n"),
    (check_layers, "unit:", "python/kvevaluator/formats/arangodb.py", "from kvevaluator.datasets.d1 import X\n"),
    (check_layers, "unit:", "python/kvevaluator/formats/nebula.py", "from kvevaluator.workloads.w1 import queries\n"),
    (check_layers, "layer-up:", "python/kvevaluator/schema/s.py", "def f():\n    from kvevaluator.datasets.d1 import X\n"),
    (check_layers, "import-name:", "experiments/demo/u.py", "def f():\n    from kvevaluator.datasets.d1 import Y\n"),
    (check_layers, "unit:", "python/kvevaluator/datasets/d1/rel.py", "from ..workloads.w1 import queries\n"),
    (check_layers, "unit:", "python/kvevaluator/formats/meta.py", "from kvevaluator.formats.dgraph import F\n"),
    (check_layers, "unit:", "python/kvevaluator/formats/newformat.py", "X = 1\n"),
    (check_layers, "unit:", "python/kvevaluator/newunit/x.py", "X = 1\n"),
    (check_layers, "unit:", "cpp/kvevaluator/src/formats/meta/plan.cc", '#include "formats/dgraph/format.h"\n'),
    (check_layers, "unit:", "cpp/kvevaluator/src/formats/meta/scan.cc", "#include <formats/dgraph/format.h>\n"),
    (check_layers, "unit:", "cpp/kvevaluator/src/formats/meta/read.cc", '#include "../dgraph/format.h"\n'),
    # hand-naming: a registered workload's name outside its unit, with no record of it
    (check_layers, "hand-naming:", "python/kvcost/report.py", "WORKLOAD = 'w1'\n"),
    # the structure ratchets: a file over 1,500 lines, an environment read, a workload id and a cost formula in shared code
    (check_layers, "big-file:", "python/kvcost/big.py", "X = 1\n" * 1600),
    (check_layers, "big-file:", "cpp/kvevaluator/src/big.cc", "int x;\n" * 1600),
    (check_layers, "big-file:", "experiments/demo/big.py", "X = 1\n" * 1600),
    (check_layers, "env-read:", "python/kvcost/knob.py", "import os\nSF = os.environ.get('SF', '0.1')\n"),
    (check_layers, "env-read:", "python/kvcost/knob2.py", "import os\nSF = os.getenv('SF')\n"),
    (check_layers, "env-read:", "python/kvcost/knob3.py", "import os\nSF = os.environ['SF']\n"),
    (check_layers, "env-read:", "python/kvcost/knob4.py", "import os\nON = 'SF' in os.environ\n"),
    (check_layers, "env-read:", "cpp/kvevaluator/src/knob.cc", 'const char* sf = getenv("SF");\n'),
    (check_layers, "workload-id:", "python/kvcost/w17_named.py", "WORKLOAD = 'I02'\n"),
    (check_layers, "workload-id:", "python/kvcost/layout_search/named2.py", "# priced on I01's mix\n"),
    (check_layers, "workload-id:", "python/kvcost/ldbc_params_named.py", "SET = 'LDBC_PARAMS'\n"),
    (check_layers, "cost-model:", "python/kvcost/model.py", "def wops(g, s, n):\n    return g + 1.1 * s + 0.01 * n\n"),
    (check_layers, "cost-model:", "python/kvcost/model2.py", "def blend(ops, nbytes):\n    return ops + nbytes / 4096\n"),
    (check_layers, "cost-model:", "python/kvcost/model3.py", "WEIGHT = {'G': 1.0, 'S': 1.1, 'N': 0.01}\n"),
    (check_layers, "cost-model:", "cpp/kvevaluator/src/model.cc", "double blend(double o, double b) { return o + b / 4096; }\n"),
    (check_layers, "cost-model:", "experiments/demo/model4.py", "def f(r):\n    return r['G'] + 1.1 * r['S'] + 0.01 * r['N']\n"),
    (check_layers, "facade-import:", "python/kvcost/facade1.py", "from kvevaluator.measure import cppbridge\n"),
    (check_layers, "facade-import:", "python/kvcost/facade2.py", "from kvevaluator.measure.cppbridge import binary_path\n"),
    (check_layers, "facade-import:", "tests/unit/facade3.py", "import kvevaluator.measure.cppbridge\n"),
    (check_layers, "glossary-word:", "python/kvcost/words1.py", "# the suite's layouts, priced\nX = 1\n"),
    (check_layers, "cell-id:", "python/kvcost/ids1.py", "def cell(p, a):\n    return f\"props_{p}_adj_{a}\"\n"),
    (check_layers, "cell-id:", "experiments/demo/ids2.py", "def store(a):\n    return f\"meta-props_condensed_adj_{a}\"\n"),
    (check_layers, "cell-id:", "paper/derive/ids3.py", "def cell(p, a):\n    return \"props_\" + p + \"_adj_\" + a\n"),
    (check_layers, "cell-id:", "tests/gates/ids4.py", "def sized(k):\n    return f\"props_chunked_adj_chunked_c{k}\"\n"),
    (check_layers, "glossary-word:", "cpp/kvevaluator/src/words2.cc", "// price the step\nint x = 1;\n"),
    # the standard tools (check_lint_tools): an unused import, unreachable code, a complex Python function, a long C++
    # function, and imports the layer rules forbid (a package going back, a format reading a workload, two workloads)
    (check_lint_tools, "ruff-f:", "python/kvcost/unused.py", "import json\n"),
    (check_lint_tools, "ruff-f:", "python/kvcost/dead_noqa.py", "import json  # noqa: E402\nprint(json)\n"),
    (check_lint_tools, "vulture:", "python/kvcost/dead.py", "def f():\n    return 1\n    print(2)\n"),
    (check_lint_tools, "lizard:", "python/kvcost/branchy.py",
     "def f(x):\n" + "".join(f"    if x == {i}:\n        return {i}\n" for i in range(16)) + "    return -1\n"),
    (check_lint_tools, "lizard:", "cpp/kvevaluator/src/long.cc",
     "int f(int x) {\n" + "  x += 1;\n" * 160 + "  return x;\n}\n"),
    (check_lint_tools, "import-linter:", "python/kvevaluator/y.py", "import kvcost\n"),
    (check_lint_tools, "import-linter:", "python/kvevaluator/formats/arangodb.py", "from kvevaluator.workloads.w1 import queries\n"),
    (check_lint_tools, "import-linter:", "python/kvevaluator/workloads/w2/gen.py",
     "from kvevaluator.workloads.w1.queries import NAME\n"),
    # a block of 12 or more lines copied into another file, its names changed
    (check_duplicate_code, "duplicate-code:", "python/kvcost/copy_of_a.py",
     DUPLICATED.replace("total", "sum_up").replace("rows", "items").replace("out", "acc").replace("best", "top")),
    (check_duplicate_code, "duplicate-code:", "experiments/demo/copy_of_a.py",
     DUPLICATED.replace("total", "sum_up").replace("rows", "items").replace("out", "acc").replace("best", "top")),
    # a test no runner names and by_hand.txt does not list; a helper nothing imports; by_hand.txt lines for a gone
    # file, for a test a runner already names, and with a trigger that matches no file
    (check_tests_reached, "orphan:", "tests/unit/check_orphan.py", "print(1)\n"),
    (check_tests_reached, "helper:", "tests/unit/helper_b.py", "Y = 1\n"),
    (check_tests_reached, "by-hand-stale:", "tests/by_hand.txt", "tests/gates/check_b.py | tests/gates/* | s | n\n"
     "tests/gates/check_gone.py | tests/gates/* | s | n\n"),
    (check_tests_reached, "by-hand-stale:", "tests/by_hand.txt", "tests/gates/check_b.py | tests/gates/* | s | n\n"
     "tests/unit/check_a.py | tests/unit/* | s | n\n"),
    (check_tests_reached, "trigger:", "tests/by_hand.txt", "tests/gates/check_b.py | python/nothing_here/* | s | n\n"),
    # a cell's body chosen by the capabilities' pick, not the pick record (the old captures join, before 78e8fca)
    (check_planner_pick_reads, "planner-pick:", "python/kvcost/demo_join.py",
     "def picks(rows):\n    return [r for r in rows if r['planner_pick'] == '1']\n"),
    (check_planner_pick_reads, "planner-pick:", "experiments/demo/q.py",
     "def best(rows):\n    return [r for r in rows if str(r.get('planner_pick')) == '1']\n"),
    # a second reader of the pick record: its decisions read raw, its stores file read in a shell stage (e.g. a stage's
    # raw read of `planner`), the decisions joined in by a reader
    (check_pick_files_read, "pick-file:", "python/kvcost/demo_why.py",
     "import json\nWHY = json.load(open('inputs/picks/pick_decisions.json'))\n"),
    (check_pick_files_read, "pick-file:", "experiments/demo/run-demo.sh",
     "python3 -c \"import json; print(json.load(open('inputs/picks/pick_stores.json'))['rule'])\"\n"),
    (check_pick_files_read, "load-full:", "experiments/demo/planner.py",
     "from kvevaluator.plan_choice import pick_record\ndef point(c):\n    return pick_record.load_full()['cells'][c]['ic10']['planner']\n"),
    # a kvcost call with a flag kvcost does not have: in a doc's code block, a script, a Python argv
    (check_kvcost_calls_parse, "kvcost-call:", "docs/demo.md",
     "Measure it:\n\n```\nkvcost measure --dataset ldbc=0.1 --no-such-flag Sp-Sp\n```\n"),
    (check_kvcost_calls_parse, "kvcost-call:", "experiments/demo/run-demo-call.sh",
     '"$PYTHON" -m kvcost.cli.cli compose --dataset "ldbc=$SF" --no-such-flag\n'),
    (check_kvcost_calls_parse, "kvcost-call:", "tests/demo_call.py",
     'import sys\nCMD = [sys.executable, "-m", "kvcost.cli.cli", "search-mix", "--no-such-flag", f"{sys.argv[1]}"]\n'),
    # a body named `variant` (a key, a column), in Python and in a C++ CSV header
    (check_terms, "body-term:", "python/kvcost/demo_rows.py", "def bodies(rows):\n    return {r['variant'] for r in rows}\n"),
    (check_terms, "body-term:", "cpp/demo/out.cc", 'std::cout << "cell,query,variant,body_sha";\n'),
    (check_terms, "body-term:", "justfile", 'cells:\n    python3 x.py --column "variant"\n'),
    (check_terms, "glossary-name:", "python/kvcost/demo_names.py", "def price_mix(w):\n    return w\n"),
    (check_terms, "glossary-name:", "tests/unit/demo_suite.py", "from kvcost.layout_cost import benchmarks\nSUITES = benchmarks.DATASETS\n"),
    # a pointer to the research repo, in a comment and in a README (the name built from the lint's own constant)
    (check_no_research_repo, "research-repo:", "python/kvcost/demo_ref.py",
     f"# the reason: {check_no_research_repo.NAME} docs/design/x.md\nX = 1\n"),
    (check_no_research_repo, "research-repo:", "experiments/demo/README.md",
     f"The results are in {check_no_research_repo.NAME}.\n"),
    # a plotter that draws with bare matplotlib, off the shared style
    (check_plot_style, "plot-style:", "experiments/demo/plot_demo.py",
     "import matplotlib.pyplot as plt\ndef draw(xs):\n    plt.plot(xs)\n"),
    # a byte-lane list retyped (a tuple, a renaming dict) instead of sliced from step_record
    (check_layers, "lane-list:", "python/kvcost/lanes1.py", "LANES = ('get_key', 'get_value', 'scan_key')\n"),
    (check_layers, "output-path:", "python/kvcost/out1.py", "from kvevaluator.paths import OUTPUT_DIR\nP = OUTPUT_DIR / 'x'\n"),
    (check_layers, "output-path:", "run/out2.sh", 'D="$OUTPUT_DIR/x"\n'),
    (check_layers, "output-path:", "run/out3.sh", 'D="$OUTPUT_DIR"/x\n'),
    (check_layers, "lane-list:", "tests/unit/lanes2.py",
     "COLS = {'get_key': 'get_key_bytes', 'scan_key': 'scan_key_bytes', 'seek_miss_key': 'seek_miss_key_bytes'}\n"),
]


# (lint, file, content): with the repo's own .gitignore in the tree, these must NOT be reported
IGNORED = [
    (check_code_only, "check.log", "a run's output, redirected into the repo root\n"),
]


def make_tree(d, files):
    for rel, text in files.items():
        p = Path(d) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    subprocess.run(["git", "init", "-q", d], check=True)


def main():
    os.environ.pop("KVEVAL_LINT_TRACKED_ONLY", None)   # the plants' trees are untracked: they test the rules, not the file list
    failures = 0
    lints = (check_code_only, check_paths, check_layers, check_duplicate_code, check_planner_pick_reads, check_terms,
             check_lint_tools, check_tests_reached, check_no_research_repo, check_pick_files_read,
             check_plot_style, check_kvcost_calls_parse)
    for lint in lints:
        with tempfile.TemporaryDirectory() as d:
            make_tree(d, CLEAN)
            problems, n = lint.scan(d)[:2]
            ok = not problems and n > 0
            failures += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {lint.__name__} on the clean tree: {len(problems)} problems in {n} files")
    for lint, tag, rel, text in PLANTS:
        with tempfile.TemporaryDirectory() as d:
            make_tree(d, {**CLEAN, rel: text})
            problems = lint.scan(d)[0]
            caught = [p for p in problems if p.startswith(rel) and f" {tag}" in p]
            failures += not caught
            print(f"{'ok  ' if caught else 'FAIL'} {lint.__name__} catches {tag[:-1]} in {rel}")
    # hand-naming moved within its package keeps the package's total: not a raise
    with tempfile.TemporaryDirectory() as d:
        moved = {k: v for k, v in CLEAN.items() if k != "python/kvcost/named.py"}
        make_tree(d, {**moved, "python/kvcost/cli/moved.py": "WORKLOAD = 'w1'\n"})
        hit = [p for p in check_layers.scan(d)[0] if " hand-naming: " in p]
        failures += bool(hit)
        print(f"{'ok  ' if not hit else 'FAIL'} check_layers passes hand-naming moved within its package")
    # one ratchet file, two writers: check_layers' --write-structure keeps check_lint_tools' counts, and the reverse
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "structure_ratchets.json"
        path.write_text(json.dumps({"counts": {"ruff-f": {"a.py": 1}, "big-file": {"b.py": 1}}, "reasons": []}))
        check_layers.write_record(path, {"big-file": {}}, "test", check_layers.structure_summary)
        check_layers.write_record(path, {"ruff-f": {"a.py": 2}}, "test", check_layers.structure_summary)
        kept = json.loads(path.read_text())["counts"]
        ok = kept == {"ruff-f": {"a.py": 2}, "big-file": {}}
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} a write to structure_ratchets.json keeps the other lint's counts: {kept}")
    gitignore = (ROOT / ".gitignore").read_text()
    for lint, rel, text in IGNORED:
        with tempfile.TemporaryDirectory() as d:
            make_tree(d, {**CLEAN, ".gitignore": gitignore, rel: text})
            problems = lint.scan(d)[0]
            hit = [p for p in problems if p.startswith(rel)]
            failures += bool(hit)
            print(f"{'ok  ' if not hit else 'FAIL'} {lint.__name__} skips {rel} (the repo's .gitignore)")
    total = len(lints) + len(PLANTS) + len(IGNORED) + 2
    print(f"check_lints_catch_plants {'FAIL' if failures else 'OK'}: {total - failures}/{total} cases")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
