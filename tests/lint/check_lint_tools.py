"""Four standard tools, each as a ratchet: a count per file, which may only go down per package (structure_ratchets.json
beside this file, with check_layers' four counts). The tools come from the `lint` extra (pyproject.toml, pinned).

  ruff-f         ruff's pyflakes rules (F: unused imports and names, undefined names) and unused `noqa` markers
                 (RUF100: a marker for a rule this lint does not run says nothing), Python
  vulture        unused code at 80% confidence or more, Python
  lizard         functions over cyclomatic complexity 15 or 150 lines, Python and C++ (cpp/kvevaluator/src)
  import-linter  direct imports against the rules of check_layers (its MAY_IMPORT, MAY_NOT and FORMAT_USES), written
                 as import-linter contracts; it follows only direct imports, as check_layers does

    python tests/lint/check_lint_tools.py                              # exit 1 if a package's count rose
    python tests/lint/check_lint_tools.py --sites                      # every finding, file:line
    python tests/lint/check_lint_tools.py --write --reason "why the count moved"
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import check_layers as L
from kvevaluator.paths import ROOT
from repo_files import repo_files

BIN = Path(sys.executable).parent   # the tools' entry points, in the venv running this lint
PY_DIRS = ("python/", "experiments/", "tests/", "run/")
LIZARD_DIRS = ("python/", "cpp/kvevaluator/src/")
MAX_CCN, MAX_LENGTH = 15, 150
COUNTS = ("ruff-f", "vulture", "lizard", "import-linter")
FIX = {
    "ruff-f": "remove the unused name or import, or the noqa marker that silences nothing",
    "vulture": "remove the unused code, or use it",
    "lizard": f"split the function (cyclomatic complexity <= {MAX_CCN}, <= {MAX_LENGTH} lines)",
    "import-linter": "import the shared code instead (check_layers' rules)",
}


def run(cmd, root, env=None):
    t0 = time.monotonic()
    r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, env=env)
    return r, time.monotonic() - t0


def ruff(root, files):
    """(rel, line, text) per pyflakes finding, and per `noqa` marker that silences nothing (RUF100: a rule we do not run)"""
    r, t = run([str(BIN / "ruff"), "check", "--isolated", "--no-cache", "--select", "F,RUF100", "--output-format", "json",
                *files], root)
    if r.returncode not in (0, 1):
        raise SystemExit(f"check_lint_tools: ruff failed: {r.stderr[-400:]}")
    found = [(os.path.relpath(f["filename"], root), f["location"]["row"], f"{f['code']} {f['message']}")
             for f in json.loads(r.stdout or "[]")]
    return found, t


def vulture(root, files):
    r, t = run([sys.executable, "-m", "vulture", "--min-confidence", "80", *files], root)
    if r.returncode not in (0, 1, 3):
        raise SystemExit(f"check_lint_tools: vulture failed: {r.stderr[-400:]}")
    found = []
    for line in r.stdout.splitlines():
        m = re.match(r"(.+?):(\d+): (.*)", line)
        if m:
            found.append((m.group(1), int(m.group(2)), m.group(3)))
    return found, t


def lizard(root, files):
    r, t = run([sys.executable, "-m", "lizard", "-l", "python", "-l", "cpp", "-C", str(MAX_CCN), "-L", str(MAX_LENGTH),
                "-w", *files], root)
    found = []
    for line in r.stdout.splitlines():
        m = re.match(r"(.+?):(\d+): warning: (.*)", line)
        if m:
            found.append((os.path.relpath(m.group(1), root) if os.path.isabs(m.group(1)) else m.group(1),
                          int(m.group(2)), m.group(3)))
    return found, t


def modules(root):
    """the dotted modules under python/ that import-linter sees (every dir above them a package with an __init__.py):
    name -> its file, relative to root"""
    files = set(repo_files(root))
    out = {}
    for rel in sorted(files):
        if rel.startswith("python/") and rel.endswith(".py"):
            parts = rel[len("python/"):-3].split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            dirs = rel.split("/")[1:-1]
            if parts and parts[0] in L.PACKAGES and all(
                    "/".join(["python", *dirs[:i + 1], "__init__.py"]) in files for i in range(len(dirs))):
                out[".".join(parts)] = rel
    return out


def contracts(mods):
    """import-linter contracts from check_layers' rules, over the modules that exist: [(name, source, forbidden)]"""
    has = lambda m: m in mods
    out = []
    for pkg in L.PACKAGES:
        bad = [p for p in L.PACKAGES if p not in L.MAY_IMPORT[pkg] and has(p)]
        if has(pkg) and bad:
            out.append((f"{pkg} imports only {', '.join(sorted(L.MAY_IMPORT[pkg]))}", pkg, bad))
    kv = "kvevaluator"
    for unit, banned in sorted(L.MAY_NOT.items()):
        bad = [f"{kv}.{b}" for b in sorted(banned) if has(f"{kv}.{b}")]
        if has(f"{kv}.{unit}") and bad:
            out.append((f"{unit} imports no {', '.join(sorted(banned))}", f"{kv}.{unit}", bad))
    # a workload is a subdir of workloads/ (check_layers.kv_place)
    workloads = sorted({rel.split("/")[3] for rel in mods.values()
                        if rel.startswith(f"python/{kv}/workloads/") and rel.count("/") >= 4})
    for w in workloads:
        bad = [f"{kv}.workloads.{o}" for o in workloads if o != w]
        if bad:
            out.append((f"workload {w} imports no other workload", f"{kv}.workloads.{w}", bad))
    fmts = sorted(m.split(".")[2] for m in mods if m.startswith(f"{kv}.formats.") and m.count(".") == 2)
    for f in fmts:
        if f in L.FORMAT_SHARED:
            continue
        bad = [f"{kv}.formats.{o}" for o in fmts if o != f and o not in L.FORMAT_SHARED
               and o not in L.FORMAT_USES.get(f, set())]
        if bad:
            out.append((f"format {f} imports only the shared format modules and its uses", f"{kv}.formats.{f}", bad))
    return out


def import_linter(root, mods):
    """(rel, line, text) per direct import a contract forbids"""
    roots = sorted({m.split(".")[0] for m in mods})
    cs = contracts(mods)
    if not roots or not cs:
        return [], 0.0
    ini = ["[importlinter]", "root_packages =", *[f"    {r}" for r in roots], ""]
    for i, (name, source, bad) in enumerate(cs):
        ini += [f"[importlinter:contract:c{i}]", f"name = {name}", "type = forbidden", "allow_indirect_imports = true",
                f"source_modules =\n    {source}", "forbidden_modules =", *[f"    {b}" for b in bad], ""]
    with tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False) as fh:
        fh.write("\n".join(ini))
    env = dict(os.environ, COLUMNS="1000", NO_COLOR="1")   # one violation per line, no colour codes
    # run from root/python: `python -c` puts its working dir first on the path, so the packages analyzed are this
    # tree's (a plant's temporary tree too), not the ones installed in the venv
    cli = "import sys; from importlinter.cli import lint_imports_command; sys.exit(lint_imports_command())"
    try:
        r, t = run([sys.executable, "-c", cli, "--config", fh.name, "--no-cache"], Path(root) / "python", env)
    finally:
        os.unlink(fh.name)
    if r.returncode not in (0, 1) or "Contracts:" not in r.stdout:
        raise SystemExit(f"check_lint_tools: lint-imports failed: {(r.stdout + r.stderr)[-600:]}")
    found = []
    for m in re.finditer(r"^-\s+(\S+) -> (\S+) \(([^)]*)\)", r.stdout, re.M):   # one direct import per line
        importer, imported = m.group(1), m.group(2)
        for ln in re.findall(r"l\.(\d+)", m.group(3)):
            found.append((mods.get(importer, importer), int(ln), f"{importer} imports {imported}"))
    return found, t


def scan(root):
    """(problems, files scanned, {count: {rel: n}}, {count: [(rel, line, text)]}, {tool: seconds})"""
    root = Path(root)
    if not (BIN / "ruff").is_file():
        raise SystemExit(f"check_lint_tools: no ruff beside {sys.executable}: install the lint extra (just setup)")
    files = repo_files(root)
    py = [f for f in files if f.endswith(".py") and f.startswith(PY_DIRS)]
    liz = [f for f in files if f.endswith((".py", ".cc", ".h")) and f.startswith(LIZARD_DIRS)]
    jobs = {"ruff-f": (ruff, py), "vulture": (vulture, py), "lizard": (lizard, liz)}
    with ThreadPoolExecutor(len(COUNTS)) as pool:   # four independent processes: the wall time is the slowest one's
        futures = {c: pool.submit(f, root, files) for c, (f, files) in jobs.items() if files}
        futures["import-linter"] = pool.submit(import_linter, root, modules(root))
        done = {c: f.result() for c, f in futures.items()}
    sites = {c: done[c][0] if c in done else [] for c in COUNTS}
    secs = {c: done[c][1] if c in done else 0.0 for c in COUNTS}
    counts = {c: {} for c in COUNTS}
    first = {c: {} for c in COUNTS}
    for c, found in sites.items():
        per = defaultdict(int)
        for rel, line, _ in sorted(found):
            per[rel] += 1
            first[c].setdefault(rel, line)
        counts[c] = dict(sorted(per.items()))
    path = root / "tests" / "lint" / L.STRUCTURE.name
    recorded = json.loads(path.read_text())["counts"] if path.is_file() else {}
    fix = lambda c: f"{FIX[c]}, or record why: check_lint_tools.py --write --reason \"...\""
    problems = L.ratchet_problems(counts, recorded, lambda c: c, lambda c: "{c} findings", fix, first)
    return problems, len(set(py) | set(liz)), counts, sites, secs


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sites", action="store_true", help="print every finding")
    ap.add_argument("--write", action="store_true", help="record today's counts in structure_ratchets.json")
    ap.add_argument("--reason", help="with --write: one line, why a count moved (required)")
    a = ap.parse_args()
    problems, n, counts, sites, secs = scan(ROOT)
    if a.sites:
        for c, found in sites.items():
            for rel, line, text in sorted(found):
                print(f"{c}\t{rel}:{line}\t{text}")
    if a.write:
        if not a.reason:
            raise SystemExit("check_lint_tools: --write needs --reason \"<one line: why the count moved>\"")
        L.write_record(L.STRUCTURE, counts, a.reason, L.structure_summary)
        problems = []
    recorded = json.loads(L.STRUCTURE.read_text())["counts"]
    for c in COUNTS:
        lower = [p for p, k in L.by_package(recorded.get(c, {})).items() if L.by_package(counts[c]).get(p, 0) < k]
        if lower and not a.write:
            print(f"check_lint_tools: {c} is below the record in {', '.join(lower)}: lower it (--write --reason \"...\")")
    for p in problems:
        print(p)
    times = ", ".join(f"{c} {secs[c]:.1f} s" for c in COUNTS)
    print(f"check_lint_tools {'FAIL' if problems else 'OK'}: {len(problems)} problems in {n} files; "
          f"{L.structure_summary(counts)}; times: {times}")
    return 1 if problems or n == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
