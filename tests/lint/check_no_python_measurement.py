"""The Python-measurement retirement gate: no script that writes a result measures on the Python store.

A RATCHET. Every known violation is listed below (KNOWN), keyed by file + rule with an exact count -- not by line.
The gate fails when a file has a hit it does not list (a new violation) AND when a listed hit is gone (a fix must
shrink KNOWN in the same commit). The target is an empty KNOWN.

Python files are scanned by AST (comments and docstrings never match), shell scripts and the justfile by regex with
'#' comments stripped. The Python store stays only as a small-scale oracle: the core modules below implement it and
the allowlisted tests use it (at sf0.1, the demo graph or synthetic data).

Also fails when: fewer than MIN_SCANNED files were scanned (a check that covered nothing); an allowlisted test has
no hit (the allowlist is stale); an allowlisted test's --sf default is not None / "0.1"; an allowlisted file is gone.

Run:  python3 tests/lint/check_no_python_measurement.py
      python3 tests/lint/check_no_python_measurement.py --print-known   (the KNOWN dict for the current tree)
"""
import ast
import os
import re
import sys
from kvevaluator.paths import ROOT
from repo_files import repo_files

EXP = str(ROOT)
SKIP_DIRS = {"output", "__pycache__", ".git", "build", "third_party", ".plans", "docs"}
MIN_SCANNED = 300

STORE_NAMES = {"AbstractKV", "CountingKV", "WriteShadowKV"}
EXEC_CALLS = {"open_store", "build_stores", "open_stores", "_wrap_store", "run_plan", "run_ir",
              "ingest_stream", "store_size", "materialize", "OracleCost"}
EXEC_IMPORTS = {("runner", "execute")}   # too generic as a bare name: only when imported from it
BACKEND_FLAGS = {"--backend", "--measured", "--size-source"}
PY_BACKENDS = {"abstract", "python"}
PY_SWEEP_FILE = re.compile(r"(^|/)(kvbench|label-only|capped|microbench|writes)\.csv$")

RULES = {
    "R1": "imports or calls a Python store class (AbstractKV / CountingKV / WriteShadowKV)",
    "R2": "imports or calls a Python-store runner (open_store, run_plan, run_ir, materialize, store_size, ...)",
    "R3": "imports runner.execute",
    "R4": "passes backend='abstract' / 'python' at a call site",
    "R5": "an argparse --backend / --measured / --size-source that offers a Python backend",
    "R6": "compares a backend variable with 'abstract' / 'python'",
    "R7": "defines a function whose backend default is 'abstract' / 'python'",
    "R8": "names a Python sweep file (kvbench / label-only / capped / microbench / writes .csv)",
    "SH1": "shell: --backend abstract|python",
    "SH2": "shell: --measured python",
    "SH3": "shell: --size-source python",
    "SH4": "shell: a BACKEND default of abstract|python",
    "SH5": "shell: -m kvevaluator.oracle.run",
    "SH6": "shell: --cross-check",
    "SH7": "shell: a driver that accepts a python backend",
}
SH_RULES = [
    ("SH1", re.compile(r"--backend[ =]+\"?(abstract|python)\b")),
    ("SH2", re.compile(r"--measured[ =]+\"?python\b")),
    ("SH3", re.compile(r"--size-source[ =]+\"?python\b")),
    ("SH4", re.compile(r"BACKEND(:-|=)\"?(abstract|python)\b")),
    ("SH5", re.compile(r"-m +evaluator\.run\b")),
    ("SH6", re.compile(r"--cross-check\b")),
    ("SH7", re.compile(r"python\|cpp|cpp\|python|echo python\b")),
]

# the modules that implement the Python store and may use it (the oracle itself)
ALLOW_CORE = {
    "python/kvevaluator/oracle/run.py", "python/kvevaluator/oracle/interp.py",
    "python/kvevaluator/kvstore/kvstore.py", "python/kvevaluator/kvstore/shadow.py",
    "python/kvevaluator/oracle/runner.py",
}
# the small-scale oracle tests (sf0.1, the demo graph, synthetic data); each must still use the store
ALLOW_TESTS = {
    "tests/gates/check_cpp_vs_python.py", "tests/gates/check_cpp_write_effects.py",
    "tests/unit/check_microbench_planir.py",
    "tests/unit/check_planir.py", "tests/unit/check_planir_ldbc.py", "tests/gates/check_write_effects.py", "tests/unit/selftest.py",
    # moved in from the studies: small-scale oracle checks, not result producers
    "tests/unit/check_write_model.py",
    # the write suite's oracle check: replaces the write-gate's kvbench --backend abstract sweeps
    "tests/gates/check_write_queries.py",
    # the engine-fidelity port's oracle checks: the Python engine twins vs the C++ ones, per cell and
    # per step -- what lets validate.py run on C++ alone
    "tests/gates/check_engine_plans.py", "tests/gates/check_engine_ops.py",
    "tests/gates/check_label_scan_keep.py",   # the label row scan with keep on pertype_csr: C++ against its Python twin, hand plans, sf0.1
    "tests/gates/check_job_cpp_vs_python.py",   # the JOB workload's plans: C++ against the Python twins, IMDb's sample1pct tag (writes no result)
    "tests/gates/check_cpp_vs_python_linkbench.py",   # LinkBench's reads: C++ against the Python twins, a 12-node synthetic graph (writes no result)
    "tests/gates/check_linkbench_getlink_ids.py",     # getlink's id list: answers against links.jsonl on both sides, the same 12-node graph (writes no result)
    "tests/gates/check_twin_count_guard.py",  # the guard on a Python twin's counts (run_plan refuses Per-type CSR's where they are not the format's), sf0.1
    "tests/unit/check_chase_directions.py",   # read_chase over both directions in the Python oracle, the demo graph (writes no result)
    "tests/unit/check_twin_refusal_text.py",  # run_plan's refusal text on a stand-in format and store (no data; writes no result)
}

# THE NAMED EXCEPTIONS: one entry per exception, each with its reason. Pinned like KNOWN
# (exact counts, may only shrink), but not part of the "KNOWN empty" target.
EXCEPTIONS = []   # both retired by the engine-fidelity C++ port (engine fidelity: step C; date-index knobs: step F)
# file -> {rule: count} over every exception (a file may belong to one exception only; main() checks)
EXCEPTION = {rel: n for e in EXCEPTIONS for rel, n in e["files"].items()}

# THE RATCHET: file -> {rule: count}. May only shrink. `--print-known` prints it for the current tree.
KNOWN = {
}


def py_hits(path):
    src = open(path, encoding="utf-8").read()
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [(0, "PARSE", f"parse error {e}")]
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for al in node.names:
                if al.name in STORE_NAMES:
                    out.append((node.lineno, "R1", f"import {al.name}"))
                elif al.name in EXEC_CALLS:
                    out.append((node.lineno, "R2", f"import {al.name}"))
                elif any(mod.endswith(m) and al.name == n for m, n in EXEC_IMPORTS):
                    out.append((node.lineno, "R3", f"import {mod}.{al.name}"))
        elif isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if name in STORE_NAMES:
                out.append((node.lineno, "R1", f"call {name}()"))
            elif name in EXEC_CALLS:
                out.append((node.lineno, "R2", f"call {name}()"))
            for kw in node.keywords:
                if kw.arg == "backend" and isinstance(kw.value, ast.Constant) and kw.value.value in PY_BACKENDS:
                    out.append((node.lineno, "R4", f"backend={kw.value.value!r}"))
            if name in {"open_store", "build_stores", "open_stores"}:
                for a in node.args:
                    if isinstance(a, ast.Constant) and a.value in PY_BACKENDS:
                        out.append((node.lineno, "R4", f"{name}(..., {a.value!r})"))
            if name == "add_argument" and node.args and isinstance(node.args[0], ast.Constant) \
                    and node.args[0].value in BACKEND_FLAGS:
                for kw in node.keywords:
                    if kw.arg in ("choices", "default"):
                        vals = []
                        if isinstance(kw.value, (ast.List, ast.Tuple)):
                            vals = [e.value for e in kw.value.elts if isinstance(e, ast.Constant)]
                        elif isinstance(kw.value, ast.Constant):
                            vals = [kw.value.value]
                        bad = sorted(set(vals) & PY_BACKENDS)
                        if bad:
                            out.append((node.lineno, "R5", f"argparse {node.args[0].value} {kw.arg} has {bad}"))
        elif isinstance(node, ast.Compare):
            for c in [node.left] + node.comparators:
                if isinstance(c, ast.Constant) and c.value in PY_BACKENDS:
                    names = [n for n in [node.left] + node.comparators if isinstance(n, (ast.Name, ast.Attribute))]
                    if any((getattr(n, "id", None) or getattr(n, "attr", "")).endswith(("backend", "measured", "size_source"))
                           for n in names):
                        out.append((node.lineno, "R6", f"compare backend == {c.value!r}"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and PY_SWEEP_FILE.search(node.value):
            out.append((node.lineno, "R8", f"python-sweep filename {node.value!r}"))
        elif isinstance(node, ast.FunctionDef):
            for a, d in zip(reversed(node.args.args), reversed(node.args.defaults)):
                if a.arg == "backend" and isinstance(d, ast.Constant) and d.value in PY_BACKENDS:
                    out.append((node.lineno, "R7", f"def {node.name}(backend={d.value!r})"))
    return out


def sh_hits(path):
    out = []
    for i, line in enumerate(open(path, encoding="utf-8"), 1):
        code = re.sub(r"(^|\s)#.*$", "", line)
        for rule, rx in SH_RULES:
            if rx.search(code):
                out.append((i, rule, rx.pattern))
    return out


def sf_default_problems(path):
    """An allowlisted test's --sf default must be None or "0.1" (a small-scale oracle by default)."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "add_argument" and node.args \
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == "--sf":
            for kw in node.keywords:
                if kw.arg != "default":
                    continue
                v = kw.value
                if isinstance(v, ast.Call) and len(v.args) == 2:   # os.environ.get("SF", "0.1")
                    v = v.args[1]
                if not (isinstance(v, ast.Constant) and v.value in (None, "0.1")):
                    bad.append(f"{os.path.relpath(path, EXP)}:{node.lineno}: --sf default is {ast.unparse(kw.value)}")
    return bad


def scan():
    hits, scanned = {}, 0
    for rel in repo_files(EXP):          # the repo's own files: no .venv, no data dir, no ignored output
        if SKIP_DIRS.intersection(rel.split("/")[:-1]):
            continue
        p, fn = os.path.join(EXP, rel), os.path.basename(rel)
        if fn.endswith(".py"):
            h = py_hits(p)
        elif fn.endswith(".sh") or fn == "justfile":
            h = sh_hits(p)
        else:
            continue
        scanned += 1
        if h:
            hits[rel] = sorted(set(h))
    return hits, scanned


def counts(hs):
    c = {}
    for _ln, rule, _what in hs:
        c[rule] = c.get(rule, 0) + 1
    return c


def main():
    hits, scanned = scan()
    viol = {rel: hs for rel, hs in hits.items() if rel not in ALLOW_CORE and rel not in ALLOW_TESTS}
    if "--print-known" in sys.argv:
        print("KNOWN = {")
        for rel in sorted(r for r in viol if r not in EXCEPTION):
            print(f"    {rel!r}: {dict(sorted(counts(viol[rel]).items()))},")
        print("}")
        return 0
    problems = []
    if scanned < MIN_SCANNED:
        problems.append(f"only {scanned} files scanned (< {MIN_SCANNED}): the scan covered too little")
    for rel in sorted(ALLOW_CORE | ALLOW_TESTS):
        if not os.path.exists(os.path.join(EXP, rel)):
            problems.append(f"{rel}: allowlisted but gone (shrink the allowlist)")
    for rel in sorted(ALLOW_TESTS):
        if os.path.exists(os.path.join(EXP, rel)):
            if rel not in hits:
                problems.append(f"{rel}: allowlisted test with 0 hits (stale allowlist entry)")
            problems += sf_default_problems(os.path.join(EXP, rel))
    for rel in sorted(set(KNOWN) & set(EXCEPTION)):
        problems.append(f"{rel}: in both KNOWN and EXCEPTION")
    for e in EXCEPTIONS:
        if not e.get("name") or not e.get("reason") or not e.get("files"):
            problems.append(f"exception {e.get('name')!r}: needs a name, a reason and its files")
    if sum(len(e["files"]) for e in EXCEPTIONS) != len(EXCEPTION):
        problems.append("a file is listed under two named exceptions")
    # the ratchet: every (file, rule) count must equal KNOWN (or the named EXCEPTION) exactly
    for rel in sorted(set(viol) | set(KNOWN) | set(EXCEPTION)):
        have, want = counts(viol.get(rel, [])), KNOWN.get(rel) or EXCEPTION.get(rel, {})
        for rule in sorted(set(have) | set(want)):
            h, w = have.get(rule, 0), want.get(rule, 0)
            if h > w:
                new = [f"{rel}:{ln}: {r} {what}" for ln, r, what in viol[rel] if r == rule]
                problems.append(f"NEW violation(s) {rel} {rule} ({RULES.get(rule, rule)}): {h} > known {w}\n    "
                                + "\n    ".join(new))
            elif h < w:
                problems.append(f"FIXED {rel} {rule}: {h} < known {w} -- shrink "
                                f"{'EXCEPTION' if rel in EXCEPTION else 'KNOWN'} in this commit")
    by_rule = {}
    for rel, hs in viol.items():
        if rel in EXCEPTION:
            continue
        for rule, n in counts(hs).items():
            by_rule[rule] = by_rule.get(rule, 0) + n
    nhits = sum(by_rule.values())
    nexc = sum(n for rel in EXCEPTION for n in counts(viol.get(rel, [])).values())
    print(f"[no-python-measurement] scanned {scanned} files; {len(hits)} with hits; allowlisted {len(ALLOW_CORE)} core + "
          f"{len(ALLOW_TESTS)} tests; ratchet: {len(set(viol) - set(EXCEPTION))} files, {nhits} hits "
          + (f"(+ {len(EXCEPTIONS)} named exceptions, " + ", ".join(e["name"] for e in EXCEPTIONS)
             + f": {len(EXCEPTION)} files, {nexc} hits)" if EXCEPTIONS else "(no named exception)"))
    print("  by rule: " + ", ".join(f"{r} {by_rule[r]}" for r in sorted(by_rule)))
    if problems:
        print("\nFAIL:\n  " + "\n  ".join(problems))
        return 1
    if not KNOWN and not EXCEPTIONS:
        print("OK: KNOWN is empty and no named exception is left -- the retirement is done")
        return 0
    if not KNOWN:
        print("OK: KNOWN is empty -- the retirement is done, but for the named exceptions: "
              + "; ".join(f"{e['name']} ({e['reason']})" for e in EXCEPTIONS))
        return 0
    print("OK: every violation is a listed one (the ratchet holds); target: KNOWN empty")
    return 0


if __name__ == "__main__":
    sys.exit(main())
