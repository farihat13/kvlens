"""Every module imports (run under `python -I`: no cwd, no script dir, no PYTHONPATH): the installed packages, and every
script under experiments/ and paper/ (one process per directory, that directory first on the path, as the script sees
it when run; `__name__` is not "__main__", so a script's main() does not run). A script that does work at import is
listed below with its reason, and a listed file that is gone fails the check, so the list only shrinks.

    python -I tests/lint/check_import_all.py              # kvevaluator, kvcost, experiments/, paper/
    python -I tests/lint/check_import_all.py --engines    # and kvengines and the scripts that need it (after `just setup`)
"""
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

from kvevaluator.paths import ROOT

# repo_files lists the files a lint scans; loaded by path, since `-I` leaves this file's dir off sys.path
_spec = importlib.util.spec_from_file_location("repo_files", Path(__file__).with_name("repo_files.py"))
_repo_files = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_repo_files)
SCRIPT_TREES = ("experiments/", "paper/")
# Scripts that do work at import: never imported here.
RUNS_AT_IMPORT = {
    "experiments/linkbench/plot_whole_log_summary.py": "reads its flags at import (_arg('--data'))",
    "experiments/linkbench/steady_whole_log_table.py": "reads argv and opens a capture at import",
    "experiments/mix_benefit/check_cross_benchmark_numbers.py": "asserts on the JOB plan files at import",
    "paper/derive/complex_kv_reads.py": "reads its snapshot's files at module level (the derive runs at import)",
    "paper/derive/stored_size.py": "reads its snapshot's files at module level (the derive runs at import)",
}
# Scripts that import only with something optional present: imported when it is, else listed as not imported.
NEEDS_ENGINES = "needs the engines extra (requests; `just setup`): imported under --engines"
NEEDS_BINARY = "resolves the built binary at import (`just build`): imported when it exists"
CONDITIONAL = {
    "experiments/engine_fidelity/holdout_visibility.py": NEEDS_ENGINES,
    "experiments/engine_fidelity/live_answers.py": NEEDS_ENGINES,
    "experiments/engine_fidelity/validate.py": NEEDS_ENGINES,
    "experiments/meta/check_meta_vs_formats.py": NEEDS_BINARY,
}
# The child process: imports each file of one directory by path, under its own name (a sibling it imports is shared),
# and prints one JSON line per file: [file, null] or [file, the traceback].
CHILD = r"""
import importlib.util, json, os, sys, traceback
d, files = sys.argv[1], sys.argv[2:]
sys.path.insert(0, d)
sys.argv = [sys.argv[0]]
for f in files:
    name = os.path.splitext(os.path.basename(f))[0]
    try:
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, f)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
        err = None
    except BaseException:
        sys.modules.pop(name, None)
        err = traceback.format_exc(limit=3)
    sys.__stdout__.write(json.dumps([f, err]) + "\n")
    sys.__stdout__.flush()
"""


def modules_of(pkg_dir, name):
    """Dotted names of every .py under a package dir."""
    for p in sorted(pkg_dir.rglob("*.py")):
        parts = p.relative_to(pkg_dir).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        yield ".".join((name, *parts))


def condition_holds(reason, engines):
    if reason == NEEDS_ENGINES:
        return engines
    from kvevaluator.paths import cpp_bin      # NEEDS_BINARY
    try:
        return Path(cpp_bin()).is_file()
    except (Exception, SystemExit):
        return False


def scripts(engines):
    """(imported, failed, not imported) over experiments/ and paper/; failed = [(file, why)]"""
    listed = {**RUNS_AT_IMPORT, **CONDITIONAL}
    failed = [(f, "listed in check_import_all.py but gone: remove its line") for f in sorted(listed)
              if not (ROOT / f).is_file()]
    not_imported, by_dir = [], {}
    for rel in _repo_files.repo_files(ROOT):
        if rel.startswith(SCRIPT_TREES) and rel.endswith(".py"):
            p = ROOT / rel
            if rel in RUNS_AT_IMPORT or (rel in CONDITIONAL and not condition_holds(CONDITIONAL[rel], engines)):
                not_imported.append(rel)
            else:
                by_dir.setdefault(str(p.parent), []).append(str(p))
    imported = 0
    with tempfile.TemporaryDirectory() as cwd:      # a script that writes to its cwd writes there
        for d, files in sorted(by_dir.items()):
            r = subprocess.run([sys.executable, "-I", "-c", CHILD, d, *files], cwd=cwd, capture_output=True, text=True,
                               timeout=600, env={**os.environ, "MPLBACKEND": "Agg"})
            done = {}
            for line in r.stdout.splitlines():
                if line.startswith("["):
                    f, err = json.loads(line)
                    done[f] = err
            for f in files:
                rel = os.path.relpath(f, ROOT)
                if f not in done:                       # the process died before this file
                    failed.append((rel, f"no result: the directory's process ended with rc {r.returncode}\n"
                                        f"{r.stderr[-800:]}"))
                elif done[f]:
                    failed.append((rel, done[f]))
                else:
                    imported += 1
    return imported, failed, not_imported


def main(argv):
    engines = "--engines" in argv
    packages = ["kvevaluator", "kvcost"] + (["kvengines"] if engines else [])
    if not sys.flags.isolated:
        sys.exit("run it as `python -I tests/lint/check_import_all.py`")
    sys.argv = sys.argv[:1]          # a module that parses argv at import must not see this script's flags
    ok, failed = 0, []
    for name in packages:
        pkg = importlib.import_module(name)
        for mod in modules_of(Path(pkg.__file__).parent, name):
            try:
                importlib.import_module(mod)
                ok += 1
            except (Exception, SystemExit):     # a script that exits at import fails here, not the check
                failed.append(mod)
                print(f"FAIL {mod}\n{traceback.format_exc(limit=3)}")
    s_ok, s_failed, s_not = scripts(engines)
    for f, why in s_failed:
        print(f"FAIL {f}\n{why}")
    for f in s_not:
        print(f"  not imported: {f}: {RUNS_AT_IMPORT.get(f) or CONDITIONAL[f]}")
    failed += [f for f, _ in s_failed]
    print(f"check_import_all {'FAIL' if failed else 'OK'}: {ok + s_ok} imported, {len(failed)} failed "
          f"({', '.join(packages)}: {ok}; experiments/ and paper/: {s_ok}, and {len(s_not)} listed, not imported)")
    return 1 if failed or ok == 0 or s_ok == 0 else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
