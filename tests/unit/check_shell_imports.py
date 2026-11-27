#!/usr/bin/env python3
"""The shell drivers' Python names resolve: every `python -m <module>`, every `from <ours> import <names>` and
`import <ours>` in a `-c` snippet or a heredoc, and every literal `<path>.py` they run, in run/*.sh, the justfile and
experiments/*/run-*.sh. A module removed or renamed fails here, not minutes into the round that runs the driver (a
removed module still named at run/gate-full.sh:70 once failed a full gate round). Three planted names show it refuses.
Imports our modules (no store, no data, nothing runs); a few seconds.

Run:  python3 tests/unit/check_shell_imports.py
"""
import importlib
import importlib.util
import re
import sys

from kvevaluator.paths import ROOT

OURS = ("kvevaluator", "kvcost", "kvengines")
NAME = r"[A-Za-z_][\w.]*"
RUN_MODULE = re.compile(rf"\s-m\s+({NAME})")
FROM_IMPORT = re.compile(rf"\bfrom\s+({NAME})\s+import\s+(\([\w\s,]+\)|[\w \t,]+)")   # one line, or (...)
IMPORT = re.compile(rf"(?:^|[;\s'\"])import[ \t]+({NAME}(?:[ \t]*,[ \t]*{NAME})*)", re.M)
SCRIPT = re.compile(r"(?<![\w$/{}.-])((?:[\w-]+/)+[\w-]+\.py)\b")


def drivers():
    """the scanned files, sorted"""
    return sorted([*ROOT.glob("run/*.sh"), ROOT / "justfile", *ROOT.glob("experiments/*/run-*.sh")])


def module_resolves(mod, run_as_main=False):
    """None when `mod` resolves (a package run with -m needs its __main__), else why not"""
    try:
        spec = importlib.util.find_spec(mod)
    except (ImportError, ValueError) as e:
        return f"{type(e).__name__}: {e}"
    if spec is None:
        return "no such module"
    if run_as_main and spec.submodule_search_locations is not None:
        if importlib.util.find_spec(mod + ".__main__") is None:
            return "a package with no __main__"
    return None


def name_resolves(mod, name):
    """None when `from mod import name` finds a submodule or an attribute, else why not"""
    why = module_resolves(mod)
    if why:
        return why
    m = importlib.import_module(mod)
    if hasattr(m, name):
        return None
    if hasattr(m, "__path__") and importlib.util.find_spec(f"{mod}.{name}") is not None:   # a submodule
        return None
    return f"{mod} has no {name}"


def problems(text, where):
    """[(where:line, what, why)] for every name in `text` that does not resolve"""
    out = []

    def line_of(pos):
        return f"{where}:{text.count(chr(10), 0, pos) + 1}"

    for m in RUN_MODULE.finditer(text):
        mod = m.group(1)
        if mod.split(".")[0] in OURS and (why := module_resolves(mod, run_as_main=True)):
            out.append((line_of(m.start()), f"-m {mod}", why))
    for m in FROM_IMPORT.finditer(text):
        mod = m.group(1)
        if mod.split(".")[0] not in OURS:
            continue
        names = [n.strip() for n in m.group(2).replace("(", " ").replace(")", " ").split(",")]
        names = [n.split()[0] for n in names if n.split()]   # `x as y` -> x; the text after the last name is dropped
        for n in names:
            if (why := name_resolves(mod, n)):
                out.append((line_of(m.start()), f"from {mod} import {n}", why))
    for m in IMPORT.finditer(text):
        for mod in (s.strip() for s in m.group(1).split(",")):
            if mod.split(".")[0] in OURS and (why := module_resolves(mod)):
                out.append((line_of(m.start(1)), f"import {mod}", why))
    for m in SCRIPT.finditer(text):
        path = m.group(1)
        if not (ROOT / path).is_file() and path.split("/")[0] in ("run", "tests", "experiments", "python", "scripts"):
            out.append((line_of(m.start()), path, "no such file"))
    return out


def main():
    files = drivers()
    found, counted = [], {"-m": 0, "from/import": 0, "scripts": 0}
    for f in files:
        text = f.read_text()
        counted["-m"] += sum(1 for m in RUN_MODULE.finditer(text) if m.group(1).split(".")[0] in OURS)
        counted["from/import"] += sum(1 for m in FROM_IMPORT.finditer(text) if m.group(1).split(".")[0] in OURS)
        counted["scripts"] += len(SCRIPT.findall(text))
        found += problems(text, f.relative_to(ROOT))
    for where, what, why in found:
        print(f"  FAIL {where}: {what}: {why}")

    # the plants: each must be refused, or the check proves nothing
    plants = {
        "a removed module run with -m": '"$PYTHON" -m kvevaluator.measure.no_such_module --sf 0.1\n',
        "a removed name in a -c snippet": '"$PYTHON" -c "from kvevaluator.measure import records_gone; print(1)"\n',
        "a removed script": '"$PYTHON" tests/unit/check_no_such_test.py\n',
    }
    missed = [k for k, t in plants.items() if not problems(t, "plant")]
    for k in plants:
        print(f"  {'FAIL' if k in missed else 'ok  '} plant refused: {k}")
    control = '"$PYTHON" -m kvevaluator.measure.records_key --check\n"$PYTHON" -c "from kvevaluator.measure import records"\n'
    control_ok = problems(control, "control") == []
    print(f"  {'ok  ' if control_ok else 'FAIL'} control: live names pass")

    total = sum(counted.values())
    ok = not found and not missed and control_ok and total > 0
    print(f"check_shell_imports {'OK' if ok else 'FAIL'}: {len(files)} files, {counted['-m']} -m modules, "
          f"{counted['from/import']} from-imports, {counted['scripts']} script paths; {len(found)} unresolved, "
          f"{len(plants) - len(missed)}/{len(plants)} plants refused")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
