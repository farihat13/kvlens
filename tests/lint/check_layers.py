"""Dependencies go one way: kvcost -> kvevaluator, kvengines -> kvevaluator, never back; every import resolves to the
standard library, a declared third-party package, one of our packages, or a module beside the importing script. So a
study under experiments/ cannot import a sibling study's module (with check_paths' ban on sys.path edits).

Inside kvevaluator, the units keep the growth axes apart (UNITS): a dataset never imports a workload, a workload never
imports another workload, a format imports neither, and a format module imports only the shared format modules and
those FORMAT_USES names. In C++, formats/<a>/ never includes formats/<b>/. And a ratchet on hand-naming: the lines of
python/ and experiments/ (one package, "experiments") that name a registered workload or dataset outside that axis's own
dir (hand_naming.json beside this file, per file) may only go down per (axis, package); a raise is recorded with a reason
(--write-hand-naming --reason "...").

Ten more ratchets of the same kind (structure_ratchets.json beside this file; a count may only go down per package):
  big-file      Python and C++ source files over 1,500 lines (python/, cpp/, experiments/)
  env-read      environment variables read in python/ as an identity or a setting (not the documented path variables
                of kvevaluator.paths); C++ getenv calls
  workload-id   I01 / I02 / LDBC_PARAMS named in code outside their workload's own unit (WORKLOAD_IDS)
  cost-model    the cost formulas as code (G + 1.1 S + 0.01 N, a weight dict, the 4096 bytes divisor; not prose) outside
                weights.py and cost_models.py, in python/, cpp/ and experiments/
  facade-import imports of a module kept only as an import surface after a split (FACADES), in every Python file; new
                code imports the module that holds the name (a Python snippet inside a shell script is not counted)
  glossary-word lines that say "price" (pric*) or "suite" in Python and C++ source, comments and docstrings included
                (the terms: price is the bill's alone; compose, cost or benchmark otherwise); the code names
                themselves are check_terms.py's (0 outside its list). The lines left are prose: fixed as files are touched
  layer-up      imports inside python/kvevaluator that go UP the layer order (LAYERS), anywhere in a file (an import
                inside a function too): the count only goes down, to 0
  lane-list     a literal list, tuple or set naming three or more byte lanes (or a dict renaming them), outside their one
                home, step_record.py, which reads them from inputs/contract/capture_columns.json: slice step_record's
                lists instead, so a new lane is one edit and a missed one cannot drop out silently
  cell-id       a uniform layout's id built by hand (an f-string or a concatenation joining props_ and _adj_) outside its home,
                kvevaluator.layouts.cell_id (and paper/draw/paper_style.py's copy, released from gs-paper): a ChK-ChK id
                names both sizes or neither, one rule in one place
  output-path   a path under the output dir built by hand in code that writes results, outside kvevaluator.output_tree

    python tests/lint/check_layers.py      # exit 1 and one line per problem
    python tests/lint/check_layers.py --hand-naming      # the counts, per axis, package and file
    python tests/lint/check_layers.py --write-hand-naming --reason "why the count moved"
    python tests/lint/check_layers.py --structure        # every site of the structure ratchets
    python tests/lint/check_layers.py --write-structure --reason "why the count moved"
"""
import argparse
import ast
import datetime
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from kvevaluator.paths import ROOT
from repo_files import repo_files

PACKAGES = ("kvevaluator", "kvcost", "kvengines")
# a package -> the packages of ours it may import (itself included)
MAY_IMPORT = {
    "kvevaluator": {"kvevaluator"},
    "kvcost": {"kvcost", "kvevaluator"},
    "kvengines": {"kvengines", "kvevaluator"},
}
# import names of the third-party packages the pyproject files declare
THIRD_PARTY = {"numpy", "duckdb", "matplotlib", "mpl_toolkits", "PIL", "rocksdict", "pandas", "requests", "nebula3",
               "arango", "graphviz"}

# kvevaluator's units (python/kvevaluator/<unit>/ or <unit>.py); a unit not listed here is refused, so a new one is
# placed on purpose. The three axes hold one instance per subdir (datasets, workloads) or module (formats).
UNITS = {"__init__", "adoptions", "capture", "contract", "cost", "datasets", "fetch_dataset", "formats", "jsonbytes", "kvstore", "layouts",
         "measure", "oracle", "output_tree", "paths", "plan_choice", "plans", "predict", "schema", "sizes", "stale",
         "weights", "workloads"}
# kvevaluator's layers, bottom to top: a unit imports only its own layer's code and the layers below (the layer-up
# ratchet counts the rest). python/kvevaluator/README.md draws them.
LAYERS = {"__init__": 0, "paths": 0, "contract": 0, "weights": 0, "cost": 0, "stale": 0, "adoptions": 0, "layouts": 0,
          "jsonbytes": 0,
          "schema": 1, "datasets": 2, "sizes": 2, "plans": 3, "kvstore": 4, "formats": 5, "oracle": 6, "workloads": 7, "capture": 8, "output_tree": 8, "fetch_dataset": 8,
          "plan_choice": 9, "measure": 10, "predict": 11}
assert UNITS <= set(LAYERS), f"units with no layer: {sorted(UNITS - set(LAYERS))}"
AXES = ("datasets", "workloads", "formats")
# an axis unit -> the axis units it may not import: a dataset knows no workload; a format knows neither
MAY_NOT = {"datasets": {"workloads"}, "formats": {"datasets", "workloads"}}
# format modules every format may import (shared code), and each format module -> the other formats it may import
FORMAT_SHARED = {"__init__", "base", "caps", "props_codec", "pertype_chunk"}
FORMAT_USES = {
    "arangodb": set(), "nebula": set(), "meta": set(),
    "dgraph": {"dgraph_uidpack", "dgraph_writes"},   # its codec and its write path, split out of it (one class)
    "dgraph_uidpack": set(), "dgraph_writes": set(),
    "dgraph_engine": {"dgraph"},              # extends it (DgraphRollupWrites: the delta+rollup write strategy)
    "arangodb_engine": {"arangodb"},
    "nebula_engine": {"nebula"},
    "pertype_csr": {"nebula"},                # its index keys are nebula's, byte for byte (composition)
}
KV = Path("python") / "kvevaluator"
HAND_NAMING = Path(__file__).with_name("hand_naming.json")
STRUCTURE = Path(__file__).with_name("structure_ratchets.json")

# The structure ratchets. Each scans the source files (.py .cc .h) of SCOPE[count]; a count is per file, the ratchet per
# package (a file moved within its package keeps the total, like hand-naming).
BIG_FILE_LINES = 1500
SOURCE_SUFFIXES = (".py", ".cc", ".h")
SCOPE = {"big-file": ("python/", "cpp/", "experiments/"), "env-read": ("python/", "cpp/"), "workload-id": ("python/", "cpp/"),
         "cost-model": ("python/", "cpp/", "experiments/"), "facade-import": ("python/", "experiments/", "tests/", "paper/", "run/"),
         "glossary-word": ("python/", "experiments/", "tests/", "paper/", "run/", "cpp/"),
         "lane-list": ("python/", "experiments/", "tests/", "paper/", "run/"),
         "cell-id": ("python/", "experiments/", "tests/", "paper/", "run/", "inputs/"),
         "layer-up": ("python/kvevaluator/",),
         "output-path": ("python/", "experiments/", "paper/", "run/", "engines/")}
# output-path: a path under the output dir built by hand in code that writes results, outside the output tree's own
# module: a use of OUTPUT_DIR in Python (not its import), $OUTPUT_DIR/ or "$OUTPUT_DIR"/ in a shell script. Ask
# kvevaluator.output_tree (python -m kvevaluator.output_tree in a shell). Tests are left out: their scratch dirs are theirs.
OUTPUT_PATH = {".py": re.compile(r"\bOUTPUT_DIR\b"), ".sh": re.compile(r"\$\{?OUTPUT_DIR\}?\"?/")}
OUTPUT_PATH_HOMES = ("python/kvevaluator/output_tree.py", "python/kvevaluator/paths.py")
# cell-id: a hand-built layout id (an f-string whose props_..._adj_... token holds a field, before or after _adj_, or a
# concatenation), and its homes
CELL_ID_BUILD = re.compile(r"""f["'][^"'\n]*props_[a-z0-9_]*\{[^}"'\n]*\}(?:[a-z0-9_]|\{[^}"'\n]*\})*_adj_"""
                           r"""|f["'][^"'\n]*props_[a-z0-9_]*_adj_[a-z0-9_]*\{|["']props_["']\s*\+|\+\s*["']_adj_["']""")
CELL_ID_HOMES = ("python/kvevaluator/layouts.py", "paper/draw/paper_style.py")
# lane-list: the byte lanes (with or without the _bytes suffix) and their one home
LANE_HOME = "python/kvevaluator/capture/step_record.py"
LANE_LIST_MIN = 3
# glossary-word: "price" or "suite" in a line, but not the bill's WRITE_PRICE (the engines' own code and the third-party
# sources are not ours to word)
GLOSSARY_WORD = re.compile(r"(?<!write_)pric|\bsuites?\b", re.IGNORECASE)
GLOSSARY_SKIPPED = ("python/kvengines/", "cpp/kvevaluator/third_party/")
# a module kept only as an import surface after a split -> the modules that hold its names now (one release; the
# callers move to those, and the count of imports of the facade may only go down)
FACADES = {"kvevaluator.measure.cppbridge": "capture.binary, capture.store_identity, records, server, stores or plan_runs"}
# the documented path variables: read in kvevaluator/paths.py, the one home for locations (its docstring lists them)
PATH_ENV_NAMES = {"KVEVAL_DATA_DIR", "KVEVAL_OUTPUT_DIR", "KVEVAL_BUILD_DIR", "KVEVAL_CPP_BIN", "KVEVAL_STORE_DIR",
                  "KVEVAL_STATS_DIR", "KVEVAL_SPECS_DIR", "KVEVAL_IMDB_DIR"}
PATHS_MODULE = "python/kvevaluator/paths.py"
CPP_GETENV = re.compile(r'\bgetenv\s*\(\s*([^)]*)\)')
# a workload id (a param set) -> the paths that may name it: its workload's own unit
WORKLOAD_IDS = [
    (re.compile(r"\bI0[12]\b"), ("python/kvcost/layout_cost/workloads/", "python/kvcost/latency/i01.py",
                                  "python/kvevaluator/workloads/interactive/")),
    (re.compile(r"\bLDBC_PARAMS\b"), ("python/kvevaluator/datasets/ldbc/", "python/kvevaluator/workloads/interactive/")),
]
# the cost formulas as code: the op weights 1.1 and 0.01 in one statement, the weight vector as a dict (also stored as
# 100:110:1) or a spec string, and a division by 4096. Prose (a comment, a docstring, a report's text) is not a definition.
CPP_COST_MODEL = re.compile(r"/\s*4096\b|\b1\.1\b.*\b0\.01\b")
SPEC_STRING = re.compile(r"\bS=1\.1\b")
COST_MODEL_HOMES = ("python/kvcost/layout_cost/cost_models.py", "python/kvevaluator/weights.py")
# C++ lines where 4096 is a block size, not the cost model: (file, a text the line holds) -> why it is not counted
NOT_COST_MODEL = {
    ("cpp/kvevaluator/src/exec/executor.cc", "_fetch_4k"): "the 4 KB block-fetch count's sanity bounds (a block size)",
    ("cpp/kvevaluator/src/store/reuse_distance.h", "reuse_kvpair_blocks"): "a KV pair's 4 KB block count (a block size)",
    ("cpp/kvevaluator/src/store/size_hist.h", "blocks_4k"): "a byte count's 4 KB block count (a block size)",
}
STRUCTURE_TEXT = {   # count -> (what a file's count is, what to do about a raise)
    "big-file": ("is over 1,500 lines", "split it by role"),
    "env-read": ("reads the environment on {c} lines", "pass it as an argument, or a documented path variable in kvevaluator.paths"),
    "workload-id": ("names I01 / I02 / LDBC_PARAMS on {c} lines", "read it from the workload's unit"),
    "cost-model": ("defines the cost formulas on {c} lines", "import them from cost_models.py / weights.py"),
    "facade-import": ("imports a facade module (FACADES) on {c} lines", "import the module that holds the name"),
    "glossary-word": ("says price or suite on {c} lines", "say compose, cost or benchmark (price is the bill's alone)"),
    "lane-list": ("retypes a byte-lane list on {c} lines", "slice kvevaluator.capture.step_record's lane lists"),
    "cell-id": ("builds a layout id by hand on {c} lines", "call kvevaluator.layouts.cell_id"),
    "layer-up": ("imports up the layer order on {c} lines", "move the shared code down to a layer both read (LAYERS)"),
    "output-path": ("builds an output path by hand on {c} lines", "ask kvevaluator.output_tree (a shell: python -m kvevaluator.output_tree)"),
}


def package_of(rel):
    parts = rel.split("/")
    return parts[1] if len(parts) > 2 and parts[0] == "python" and parts[1] in PACKAGES else None


def imports_of(tree):
    """(line, top-level name) for each absolute import, anywhere in the file."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield node.lineno, a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.lineno, node.module.split(".")[0]


def beside(root, rel, name):
    """True if `name` is a module or package in the importing script's own dir (Python puts that dir on the path); a
    dir of modules with no __init__.py counts (a namespace package: paper/derive, paper/draw)."""
    d = (root / rel).parent
    return (d / f"{name}.py").is_file() or (d / name / "__init__.py").is_file() or any((d / name).glob("*.py"))


def kv_place(root, parts):
    """(unit, instance) of a path inside python/kvevaluator, as parts below it; the instance is the axis's subdir
    (datasets, workloads) or module (formats), None for the axis's own top-level code"""
    if not parts:
        return None, None
    first = parts[0][:-3] if parts[0].endswith(".py") else parts[0]
    if first not in AXES or len(parts) < 2:
        return first, None
    if first == "formats":
        return first, parts[1][:-3] if parts[1].endswith(".py") else parts[1]
    return first, parts[1] if (Path(root) / KV / first / parts[1]).is_dir() else None


def import_parts(root, node):
    """for each kvevaluator import in the node, the module path below kvevaluator as parts: the deepest one that
    exists, so `from kvevaluator.formats import pertype_chunk` names the module, not the package"""
    names = []
    if isinstance(node, ast.Import):
        names = [a.name.split(".") for a in node.names]
    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        names = [node.module.split(".") + [a.name] for a in node.names]
    for n in names:
        if n[0] != "kvevaluator" or len(n) < 2:
            continue
        parts = n[1:]
        while parts and not ((Path(root) / KV / Path(*parts)).with_suffix(".py").is_file()
                             or (Path(root) / KV / Path(*parts)).is_dir()):
            parts = parts[:-1]
        if parts:
            yield [*parts[:-1], parts[-1] + ".py"] if (Path(root) / KV / Path(*parts)).with_suffix(".py").is_file() else parts


_DEFINED = {}   # a module file -> the names its top level binds (None: it can bind any, a star import or __getattr__)


def module_file(root, dotted):
    """the file of one of our modules (python/<pkg>/... .py or __init__.py), else None"""
    base = Path(root) / "python" / Path(*dotted.split("."))
    for f in (base.with_suffix(".py"), base / "__init__.py"):
        if f.is_file():
            return f
    return base if base.is_dir() and any(base.glob("*.py")) else None   # a namespace package (no __init__.py)


def defined_names(path):
    if path.is_dir():
        return None
    if path not in _DEFINED:
        names, tree = set(), ast.parse(path.read_text())
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(n.name)
            elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                for tgt in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                    names |= {x.id for x in ast.walk(tgt) if isinstance(x, ast.Name)}
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                if any(a.name == "*" for a in n.names):
                    names = None
                    break
                names |= {(a.asname or a.name).split(".")[0] for a in n.names}
            elif isinstance(n, (ast.If, ast.Try, ast.For, ast.With)):   # a name bound in a block counts
                names |= {x.id for x in ast.walk(n) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
                names |= {a.asname or a.name.split(".")[0] for x in ast.walk(n) if isinstance(x, (ast.Import, ast.ImportFrom))
                          for a in x.names}
                names |= {x.name for x in ast.walk(n) if isinstance(x, (ast.FunctionDef, ast.ClassDef))}
        if names is not None and "__getattr__" in names:
            names = None
        _DEFINED[path] = names
    return _DEFINED[path]


def import_name_problems(root, rel, tree):
    """every import of our packages, anywhere in a file (inside a function too), names a module or a name its module
    binds: a move that missed one import fails here, not when that code first runs"""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in PACKAGES and module_file(root, a.name) is None:
                    out.append(f"{rel}:{node.lineno}: import-name: no module {a.name}")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module \
                and node.module.split(".")[0] in PACKAGES:
            f = module_file(root, node.module)
            if f is None:
                out.append(f"{rel}:{node.lineno}: import-name: no module {node.module}")
                continue
            names = defined_names(f)
            for a in node.names:
                if a.name == "*" or names is None or a.name in names or module_file(root, f"{node.module}.{a.name}"):
                    continue
                out.append(f"{rel}:{node.lineno}: import-name: {node.module} has no {a.name}")
    return out


def unit_problems(root, rel, tree):
    """the units rule for one file inside python/kvevaluator"""
    out = []
    parts = Path(rel).parts[2:]
    unit, inst = kv_place(root, parts)
    if unit not in UNITS:
        return [f"{rel}:1: unit: python/kvevaluator/{parts[0]} is not a known unit (check_layers.UNITS)"]
    if unit == "formats" and inst is not None and inst not in FORMAT_SHARED | set(FORMAT_USES):
        out.append(f"{rel}:1: unit: format module {inst} is not in check_layers.FORMAT_USES or FORMAT_SHARED")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level > 0:   # the absolute form is the one the rules can read
            out.append(f"{rel}:{node.lineno}: unit: a relative import (write it as kvevaluator.<unit>...)")
        for target in import_parts(root, node):
            t_unit, t_inst = kv_place(root, target)
            line, module = node.lineno, ".".join(target).removesuffix(".py")
            if t_unit in MAY_NOT.get(unit, ()):
                out.append(f"{rel}:{line}: unit: {unit} imports {t_unit} (kvevaluator.{module})")
            elif unit == "workloads" and t_unit == "workloads" and inst and t_inst and inst != t_inst:
                out.append(f"{rel}:{line}: unit: workload {inst} imports workload {t_inst}")
            elif unit == "formats" and t_unit == "formats" and inst and t_inst and inst != t_inst \
                    and inst != "__init__" and t_inst not in FORMAT_SHARED and t_inst not in FORMAT_USES.get(inst, ()):
                out.append(f"{rel}:{line}: unit: format {inst} imports format {t_inst} (not in FORMAT_USES[{inst!r}])")
    return sorted(set(out), key=out.index)    # one line per (line, rule), however many names the import holds


CPP_FORMATS = "cpp/kvevaluator/src/formats/"
INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]')


def cpp_problems(root, rel):
    """C++: formats/<a>/ includes no formats/<b>/ (the shared code at formats/*: the registry, the common headers)"""
    below = rel[len(CPP_FORMATS):].split("/")
    if len(below) < 2:
        return []
    a, out = below[0], []
    here = Path(rel).parent
    for i, line in enumerate((Path(root) / rel).read_text(errors="replace").splitlines(), 1):
        m = INCLUDE.match(line)
        if not m:
            continue
        inc = m.group(1)
        # "formats/<b>/x.h" or <formats/<b>/x.h> from the source root; "../<b>/x.h" relative to this file
        target = (Path("cpp/kvevaluator/src") / inc) if inc.startswith("formats/") else Path(
            os.path.normpath(here / inc)) if inc.startswith(".") else None
        if target is None:
            continue
        t = target.as_posix()
        if t.startswith(CPP_FORMATS) and len(t[len(CPP_FORMATS):].split("/")) > 1 and t[len(CPP_FORMATS):].split("/")[0] != a:
            out.append(f"{rel}:{i}: unit: C++ format {a} includes formats/{t[len(CPP_FORMATS):].split('/')[0]}/")
    return out


def registered(root, axis):
    """the names the axis's registry holds: one dir with a manifest.json each"""
    return sorted(p.parent.name for p in (Path(root) / KV / axis).glob("*/manifest.json"))


def hand_naming(root, files):
    """{axis: {file: lines}}: the lines of python/ and experiments/ whose string literal is a registered workload (dataset) name,
    outside python/kvevaluator/workloads/ (datasets/); one line counts once however many names it holds"""
    out = {}
    for axis, unit in (("workload", "workloads"), ("dataset", "datasets")):
        names, home, per = set(registered(root, unit)), f"{KV.as_posix()}/{unit}/", {}
        for rel, tree in files:
            if rel.startswith(home):
                continue
            # a dataset (workload) dir naming itself is not hand-naming (datasets/linkbench says "linkbench")
            p = Path(rel).parts
            own = p[3] if len(p) > 4 and p[:2] == ("python", "kvevaluator") and p[2] in ("datasets", "workloads") else None
            lines = {n.lineno for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value in names and n.value != own}
            if lines:
                per[rel] = len(lines)
        out[axis] = dict(sorted(per.items()))
    return out


def ratchet_problems(counts, recorded, tag, say, fix, first_line=None):
    """a package whose total for a count rose: one line per file of it that rose (a file moved within its package
    keeps the total, and passes). `say` is the text of a file's count ({count} and {c} filled in)."""
    out = []
    for axis, per in counts.items():
        old = recorded.get(axis, {})
        now_pkg, old_pkg = by_package(per), by_package(old)
        for pkg, n in now_pkg.items():
            if n <= old_pkg.get(pkg, 0):
                continue
            for rel, c in per.items():
                if package_key(rel) == pkg and c > old.get(rel, 0):
                    line = (first_line or {}).get(axis, {}).get(rel, 1)
                    out.append(f"{rel}:{line}: {tag(axis)}: {say(axis).format(axis=axis, c=c)} (recorded {old.get(rel, 0)}; "
                               f"{pkg} {n} against {old_pkg.get(pkg, 0)}): {fix(axis)}")
    return out


def package_key(rel):
    return rel.split("/")[1] if rel.startswith("python/") else rel.split("/")[0]


def by_package(per):
    out = defaultdict(int)
    for rel, n in per.items():
        out[package_key(rel)] += n
    return dict(sorted(out.items()))


def env_reads(tree):
    """(line, name) of each read of the environment in one parsed file: os.environ.get(X), os.getenv(X), os.environ[X]
    read, X in os.environ. A name that is not a string literal is shown as its source."""
    def name(arg):
        return arg.value if isinstance(arg, ast.Constant) else ast.unparse(arg)
    def is_environ(node):
        return isinstance(node, ast.Attribute) and node.attr == "environ"
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and n.args and isinstance(n.func, ast.Attribute):
            if (n.func.attr == "get" and is_environ(n.func.value)) or n.func.attr == "getenv":
                yield n.lineno, name(n.args[0])
        elif isinstance(n, ast.Subscript) and is_environ(n.value) and isinstance(n.ctx, ast.Load):
            yield n.lineno, name(n.slice)
        elif isinstance(n, ast.Compare) and any(isinstance(o, (ast.In, ast.NotIn)) for o in n.ops) \
                and any(is_environ(c) for c in n.comparators):
            yield n.lineno, name(n.left)


def cost_model_sites(tree):
    """(line, what) of each place a Python file defines the cost formulas as code"""
    nodes = list(ast.walk(tree))
    values = {n.value for n in nodes if isinstance(n, ast.Constant) and isinstance(n.value, float)}
    for n in nodes:
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div) and isinstance(n.right, ast.Constant) \
                and n.right.value == 4096:
            yield n.lineno, "a division by 4096"
        elif isinstance(n, ast.Dict):
            weights = {k.value: v.value for k, v in zip(n.keys, n.values)
                       if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)}
            if weights.get("S") in (1.1, 110, 110.0) and weights.get("N") in (0.01, 1, 1.0) and "G" in weights:
                yield n.lineno, "a weight dict G / S / N"
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and SPEC_STRING.search(n.value):
            yield n.lineno, "a weight spec string"
    if {1.1, 0.01} <= values:      # both weights somewhere in the file: look for a simple statement holding both
        for n in nodes:
            if isinstance(n, ast.stmt) and not hasattr(n, "body"):
                found = {c.value for c in ast.walk(n) if isinstance(c, ast.Constant)}
                if 1.1 in found and 0.01 in found:
                    yield n.lineno, "the weights 1.1 and 0.01"


def facade_imports(tree):
    """(line, facade) of each import of a FACADES module in one parsed file: `import a.b.f`, `from a.b.f import x`,
    `from a.b import f`"""
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name in FACADES:
                    yield n.lineno, a.name
        elif isinstance(n, ast.ImportFrom) and n.module:
            if n.module in FACADES:
                yield n.lineno, n.module
            for a in n.names:
                if f"{n.module}.{a.name}" in FACADES:
                    yield n.lineno, f"{n.module}.{a.name}"


def byte_lanes():
    """the byte-lane names a lane list is made of: the data file's byte_lanes_* groups, bare and with _bytes"""
    from kvevaluator import contract
    groups = contract.load("capture_columns")["groups"]
    lanes = {lane for g, cols in groups.items() if g.startswith("byte_lanes_") for lane in cols}
    return lanes | {f"{lane}_bytes" for lane in lanes}


def lane_list_sites(tree, lanes):
    """(line, how many) of each literal list, tuple or set of LANE_LIST_MIN or more byte lanes in one parsed file, and of
    each dict whose LANE_LIST_MIN or more lane keys map to lane names (a renaming map); a dict of per-lane values (a
    fixture row, a formula per lane) is not a list"""
    for n in ast.walk(tree):
        if isinstance(n, (ast.Tuple, ast.List, ast.Set)):
            names = {e.value for e in n.elts if isinstance(e, ast.Constant) and e.value in lanes}
        elif isinstance(n, ast.Dict):
            pairs = [(k.value, v) for k, v in zip(n.keys, n.values) if isinstance(k, ast.Constant) and k.value in lanes]
            renames = all(isinstance(v, ast.Constant) and v.value in lanes for _, v in pairs)
            names = {k for k, _ in pairs} if renames else set()
        else:
            continue
        if len(names) >= LANE_LIST_MIN:
            yield n.lineno, len(names)


def layer_of(parts):
    """the layer of a path below python/kvevaluator, as parts (LAYERS)"""
    return LAYERS.get(parts[0][:-3] if parts[0].endswith(".py") else parts[0])


def layer_up_sites(root, rel, tree):
    """(line, text) per import of a higher layer in one kvevaluator file"""
    here, seen = layer_of(Path(rel).parts[2:]), set()
    for node in ast.walk(tree):
        for target in import_parts(root, node):
            there, name = layer_of(target), ".".join(target).removesuffix(".py")
            if here is not None and there is not None and there > here and (node.lineno, name) not in seen:
                seen.add((node.lineno, name))
                yield node.lineno, f"layer {here} imports layer {there}: kvevaluator.{name}"


def structure_counts(root, files, trees):
    """({count: {file: n}}, {count: {file: [(line, text)]}}) for the seven structure ratchets. `trees` holds every parsed
    Python file by path."""
    root, counts, sites = Path(root), {c: {} for c in SCOPE}, {c: {} for c in SCOPE}
    lanes = byte_lanes()

    def add(count, rel, line, text):
        counts[count][rel] = counts[count].get(rel, 0) + 1
        sites[count].setdefault(rel, []).append((line, text))

    for rel in files:
        if rel.endswith((".py", ".sh")) and rel.startswith(SCOPE["output-path"]) and rel not in OUTPUT_PATH_HOMES:
            for i, l in enumerate((root / rel).read_text(errors="replace").splitlines(), 1):
                if OUTPUT_PATH[rel[-3:]].search(l) and not l.lstrip().startswith(("#", "from ", "import ")):
                    add("output-path", rel, i, l.strip()[:100])
        if not rel.endswith(SOURCE_SUFFIXES):
            continue
        counted = {c for c, scopes in SCOPE.items() if rel.startswith(scopes) and c != "output-path"}
        if not counted:
            continue
        text = (root / rel).read_text(errors="replace")
        lines = text.splitlines()
        if "big-file" in counted and len(lines) > BIG_FILE_LINES:
            add("big-file", rel, 1, f"{len(lines)} lines")
        if "env-read" in counted and rel in trees and rel != PATHS_MODULE and ("environ" in text or "getenv" in text):
            for line, name in env_reads(trees[rel]):
                if name not in PATH_ENV_NAMES:
                    add("env-read", rel, line, str(name))
        if "facade-import" in counted and rel in trees and any(f.rsplit(".", 1)[1] in text for f in FACADES):
            facade_files = {"python/" + f.replace(".", "/") + ".py" for f in FACADES}
            if rel not in facade_files:
                for line, name in facade_imports(trees[rel]):
                    add("facade-import", rel, line, name)
        if "layer-up" in counted and rel in trees:
            for line, what in layer_up_sites(root, rel, trees[rel]):
                add("layer-up", rel, line, what)
        if "lane-list" in counted and rel in trees and rel != LANE_HOME:
            for line, n in lane_list_sites(trees[rel], lanes):
                add("lane-list", rel, line, f"{n} byte lanes: {lines[line - 1].strip()[:80]}")
        if "env-read" in counted and rel.endswith((".cc", ".h")):
            for i, l in enumerate(lines, 1):
                for m in CPP_GETENV.finditer(l):
                    add("env-read", rel, i, m.group(1).strip('" '))
        glossary = "glossary-word" in counted and not rel.startswith(GLOSSARY_SKIPPED)
        for i, l in enumerate(lines, 1):
            if glossary and GLOSSARY_WORD.search(l):
                add("glossary-word", rel, i, l.strip()[:100])
            if "cell-id" in counted and rel.endswith(".py") and rel not in CELL_ID_HOMES and CELL_ID_BUILD.search(l):
                add("cell-id", rel, i, l.strip()[:100])
            if "workload-id" in counted:
                for pattern, homes in WORKLOAD_IDS:
                    if pattern.search(l) and not rel.startswith(homes):
                        add("workload-id", rel, i, l.strip()[:100])
                        break
            if "cost-model" in counted and rel not in COST_MODEL_HOMES and rel.endswith((".cc", ".h")) \
                    and not l.lstrip().startswith(("//", "*", "/*")) and CPP_COST_MODEL.search(l.split("//")[0]) \
                    and not any(f == rel and t in l for f, t in NOT_COST_MODEL):
                add("cost-model", rel, i, l.strip()[:100])
        if "cost-model" in counted and rel not in COST_MODEL_HOMES and rel.endswith(".py") \
                and any(w in text for w in ("4096", "1.1", "110")):      # a quick text test before the tree walk
            one_per_line = dict(sorted(cost_model_sites(trees[rel]), reverse=True)) if rel in trees else {}
            for line, what in sorted(one_per_line.items()):
                add("cost-model", rel, line, f"{what}: {lines[line - 1].strip()[:80]}")
    return ({c: dict(sorted(per.items())) for c, per in counts.items()},
            {c: dict(sorted(per.items())) for c, per in sites.items()})


def structure_problems(counts, sites, recorded):
    """the ratchet problems of the four structure counts; a file's line is its first site"""
    first = {c: {rel: s[0][0] for rel, s in per.items()} for c, per in sites.items()}
    fix = lambda c: f"{STRUCTURE_TEXT[c][1]}, or record why: check_layers.py --write-structure --reason \"...\""
    return ratchet_problems(counts, recorded, lambda c: c, lambda c: STRUCTURE_TEXT[c][0], fix, first)


def scan(root):
    """One 'path:line: rule: detail' line per problem, and the number of Python files scanned."""
    root = Path(root)
    problems, n, parsed, trees = [], 0, [], {}
    for rel in repo_files(root):
        if rel.startswith(CPP_FORMATS) and rel.endswith((".cc", ".h")):
            problems += cpp_problems(root, rel)
        if not rel.endswith(".py"):
            continue
        n += 1
        try:
            tree = ast.parse((root / rel).read_text(), rel)
        except SyntaxError as e:
            problems.append(f"{rel}:{e.lineno}: syntax: {e.msg}")
            continue
        trees[rel] = tree
        problems += import_name_problems(root, rel, tree)
        pkg = package_of(rel)
        if rel.startswith(KV.as_posix() + "/"):
            problems += unit_problems(root, rel, tree)
        if rel.startswith(("python/", "experiments/")):    # hand-naming's scope
            parsed.append((rel, tree))
        for line, name in imports_of(tree):
            if name in sys.stdlib_module_names or name in THIRD_PARTY or name == "__future__":
                continue
            if name in PACKAGES:
                if pkg and name not in MAY_IMPORT[pkg]:
                    problems.append(f"{rel}:{line}: layer: {pkg} imports {name} (allowed: "
                                    f"{', '.join(sorted(MAY_IMPORT[pkg]))})")
                continue
            if pkg is None and beside(root, rel, name):
                continue
            problems.append(f"{rel}:{line}: unresolved: {name} is not stdlib, a declared package, one of "
                            f"{', '.join(PACKAGES)}" + ("" if pkg else ", or a module beside the script"))
    counts = hand_naming(root, parsed)
    path = root / "tests" / "lint" / HAND_NAMING.name
    recorded = json.loads(path.read_text())["counts"] if path.is_file() else {}
    problems += ratchet_problems(counts, recorded, lambda a: "hand-naming", lambda a: "names a {axis} on {c} lines",
                                 lambda a: "read it from the registry, or record why: check_layers.py "
                                           "--write-hand-naming --reason \"...\"")
    s_counts, s_sites = structure_counts(root, repo_files(root), trees)
    s_path = root / "tests" / "lint" / STRUCTURE.name
    s_recorded = json.loads(s_path.read_text())["counts"] if s_path.is_file() else {}
    problems += structure_problems(s_counts, s_sites, s_recorded)
    s_lower = {c: [pkg for pkg, n in by_package(s_recorded.get(c, {})).items() if by_package(s_counts[c]).get(pkg, 0) < n]
               for c in SCOPE}
    lower = sum(1 for axis, per in recorded.items() for pkg, c in by_package(per).items()
                if by_package(counts.get(axis, {})).get(pkg, 0) < c)
    return problems, n, counts, lower, {"counts": s_counts, "sites": s_sites, "lower": s_lower}


def summary(counts):
    return "; ".join(f"{axis} {sum(per.values())} lines in {len(per)} files ("
                     + ", ".join(f"{p} {c}" for p, c in by_package(per).items()) + ")" for axis, per in counts.items())


def structure_summary(counts):
    return "; ".join(f"{c} {sum(per.values())} in {len(per)} files (" + ", ".join(f"{p} {n}" for p, n in by_package(per).items())
                     + ")" for c, per in counts.items())


def write_record(path, counts, reason, what):
    """record today's counts in a baseline JSON, with the reason appended (shared by the two baselines)"""
    old = json.loads(path.read_text()) if path.is_file() else {"reasons": []}
    summ = what(counts)
    reasons = old["reasons"] + [f"{datetime.date.today().isoformat()}: {reason} ({summ})"]
    kept = {k: v for k, v in old.get("counts", {}).items() if k not in counts}   # another lint's counts in the same file
    path.write_text(json.dumps({"counts": {**kept, **counts}, "reasons": reasons}, indent=1) + "\n")
    print(f"recorded {path.name}: {summ}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hand-naming", action="store_true", help="print the counts per axis, package and file")
    ap.add_argument("--write-hand-naming", action="store_true", help="record today's counts in hand_naming.json")
    ap.add_argument("--structure", action="store_true", help="print every site of the four structure ratchets")
    ap.add_argument("--write-structure", action="store_true", help="record today's counts in structure_ratchets.json")
    ap.add_argument("--reason", help="with a --write-*: one line, why a count moved (required)")
    a = ap.parse_args()
    problems, n, counts, lower, structure = scan(ROOT)
    if a.hand_naming:
        for axis, per in counts.items():
            for rel, c in per.items():
                print(f"{axis}\t{c}\t{rel}")
    if a.structure:
        for count, per in structure["sites"].items():
            for rel, found in per.items():
                for line, text in found:
                    print(f"{count}\t{rel}:{line}\t{text}")
    if (a.write_hand_naming or a.write_structure) and not a.reason:
        raise SystemExit("check_layers: --write-* needs --reason \"<one line: why the count moved>\"")
    if a.write_hand_naming:
        write_record(HAND_NAMING, counts, a.reason, summary)
        problems = [p for p in problems if " hand-naming: " not in p]
    if a.write_structure:
        write_record(STRUCTURE, structure["counts"], a.reason, structure_summary)
        problems = [p for p in problems if not any(f": {c}: " in p for c in SCOPE)]
    for p in problems:
        print(p)
    if lower and not a.write_hand_naming:   # never a failure: a cleanup is not blocked, but the record should follow it
        print(f"check_layers: {lower} (axis, package) totals are below the record: lower it "
              f"(--write-hand-naming --reason \"...\")")
    for count, pkgs in structure["lower"].items():
        if pkgs and not a.write_structure:
            print(f"check_layers: {count} is below the record in {', '.join(pkgs)}: lower it "
                  f"(--write-structure --reason \"...\")")
    print(f"check_layers {'FAIL' if problems else 'OK'}: {len(problems)} problems in {n} Python files; "
          f"hand-naming: {summary(counts)}; structure: {structure_summary(structure['counts'])}")
    return 1 if problems or n == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
