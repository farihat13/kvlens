#!/usr/bin/env python3
"""run/gate_levels.py's levels and the analysis-code level's split (no store, no binary):
  1. the path rule: hand cases for each level, and no capture-key path overlaps ANALYSIS_CODE_ONLY;
  2. the capture key, on a scratch git repo: a commit under python/kvcost or experiments/ keeps it, one in any of
     CAPTURE_KEY_PATHS (cpp/, python/kvevaluator, inputs/, run/, tests/gates/, the Python environment files) moves it;
  3. what the level drops: every gate the analysis-code round does not run (run/meta-gates.sh's streams but zn, ps, yc,
     d, k, and gate-full.sh's full-only stages), followed through the shell scripts, `just` recipes, Python imports and
     the paths a call runs or loads (a literal path, one built from pieces, a dir read by a computed name), reaches no
     file an analysis-code branch may change (a hit means that file takes full, or that gate moves into the level);
  4. what the level reuses: every Python call kvcost measure and compose make (python/kvevaluator/measure/
     run_measure.py) is a measured step (a kvevaluator module: inside the capture key's trees) or one of the two steps
     measure --check-only and compose re-run (tests/gates/check_grid.py, kvcost.layout_cost.compose_cells); a new call
     fails until it is placed;
  5. which capture is reused (reusable_capture, on scratch records): a malformed record is skipped, and a capture whose
     files changed after its gate (the record's digest) is not reused.

    python3 tests/unit/check_gate_levels.py [--plant kvcost-in-dropped]   (the plant must exit 1)
"""
import argparse
import json
import ast
import importlib.util
import os
import re
import subprocess
import sys
import tempfile

from kvevaluator.paths import ROOT as _ROOT

ROOT = str(_ROOT)
_spec = importlib.util.spec_from_file_location("gate_levels", os.path.join(ROOT, "run", "gate_levels.py"))
GL = importlib.util.module_from_spec(_spec)      # run/gate_levels.py: standard library only, not a package
_spec.loader.exec_module(GL)

FAILED = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f": {detail}" if not ok and detail != "" else ""))
    if not ok:
        FAILED.append(name)


def read(rel):
    with open(os.path.join(ROOT, rel)) as f:
        return f.read()


def shell_functions(text):
    """{name: body} of the `name() {` ... `}` functions of a shell script (bodies end at a line that is `}`)"""
    out, name, body = {}, None, []
    for ln in text.splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\(\) *\{", ln)
        if m and name is None:
            name, body = m.group(1), [ln[m.end():]]
            continue
        if name is not None:
            if ln.strip() == "}":
                out[name], name = "\n".join(body), None
            else:
                body.append(ln)
    return out


def just_recipes():
    """{recipe: body} of the justfile"""
    out, name = {}, None
    for ln in read("justfile").splitlines():
        m = re.match(r"^([a-z][a-z0-9-]*)( [^:]*)?:( .*)?$", ln)
        if m and not ln.startswith(" "):
            name = m.group(1)
            out[name] = ""
        elif name and ln.startswith("    "):
            out[name] += ln + "\n"
        elif ln.strip() == "":
            continue
        else:
            name = None
    return out


# a repo file a text runs; a `-m` module; a `just` recipe; a dir whose files a text names by a computed name
# (`experiments/meta/controls/$p.patch`, read with `git show`)
REF = re.compile(r"((?:tests|run|experiments|python)/[A-Za-z0-9_./-]+\.(?:py|sh))|-m ([a-z_][a-z0-9_.]*)"
                 r"|\bjust ([a-z][a-z0-9-]*)|((?:tests|run|experiments|python)/[A-Za-z0-9_./-]*/)\$")
TOPS = ("tests", "run", "experiments", "python")


def module_file(mod):
    """the repo file of a module under python/ (kvevaluator, kvcost), or None"""
    base = os.path.join(ROOT, "python", *mod.split("."))
    for p in (base + ".py", os.path.join(base, "__init__.py")):
        if os.path.exists(p):
            return os.path.relpath(p, ROOT)
    return None


def py_refs(rel):
    """the repo files a Python file reaches directly: its imports of kvevaluator / kvcost (each module and its parent
    packages) and the repo paths it runs or loads (subprocess, runpy, importlib: the strings in such a call)"""
    src = read(rel)
    out = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        for m in mods:
            if m.split(".")[0] in ("kvevaluator", "kvcost"):
                parts = m.split(".")
                for i in range(1, len(parts) + 1):
                    f = module_file(".".join(parts[:i]))
                    if f:
                        out.add(f)
    # the repo paths it runs or loads: the plain strings in the arguments of a call that does (a path in a message, a
    # docstring, a comment or a list of files is not a call)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _callee(node.func) in RUNS):
            continue
        for arg in list(node.args) + [k.value for k in node.keywords]:
            for c in ast.walk(arg):
                if isinstance(c, ast.Constant) and isinstance(c.value, str):
                    for m in REF.finditer(c.value):
                        if m.group(1) and os.path.exists(os.path.join(ROOT, m.group(1))):
                            out.add(m.group(1))
            # a path built from pieces: ROOT / "experiments" / "meta" / "x.py", os.path.join(ROOT, "experiments", ...)
            pieces = sorted((c.lineno, c.col_offset, c.value) for c in ast.walk(arg)
                            if isinstance(c, ast.Constant) and isinstance(c.value, str))
            words = [v for _l, _c, v in pieces]
            for i, w in enumerate(words):
                if w in TOPS:
                    for k in range(i + 2, len(words) + 1):
                        cand = "/".join(words[i:k])
                        if os.path.isfile(os.path.join(ROOT, cand)):
                            out.add(cand)
    return out


RUNS = {"run", "call", "check_call", "check_output", "Popen", "system", "run_path", "spec_from_file_location",
        "execv", "execvp"}   # the calls that run or load a file


def _callee(f):
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None


def closure(entries, recipes, skip=()):
    """every repo file reached from `entries` (repo paths, `-m` modules, `just` recipes, as REF matches them); a file in
    `skip` is not followed"""
    seen, todo = set(), list(entries)
    while todo:
        kind, x = todo.pop()
        if kind == "dir":
            seen.add("dir:" + x)
            continue
        if kind == "just":
            if ("just", x) in seen or x not in recipes:
                continue
            seen.add(("just", x))
            todo += refs_of_text(recipes[x])
            continue
        f = module_file(x) if kind == "mod" else x
        if not f or f in seen or f in skip or not os.path.exists(os.path.join(ROOT, f)):
            continue
        seen.add(f)
        if f.endswith(".sh"):
            todo += refs_of_text(read(f))
        else:
            todo += [("file", r) for r in py_refs(f)]
    return {x for x in seen if isinstance(x, str)}   # files, and "dir:<path>/" for a dir named by a computed name


def refs_of_text(text):
    """the REF matches of a shell or justfile text, its comments left out (a whole # line, and ` # ...` after code)"""
    text = "\n".join(re.sub(r"\s#\s.*$", "", ln) for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    out = []
    for m in REF.finditer(text):
        if m.group(1):
            out.append(("file", m.group(1)))
        elif m.group(2):
            out.append(("mod", m.group(2)))
        elif m.group(3):
            out.append(("just", m.group(3)))
        elif m.group(4):
            out.append(("dir", m.group(4)))
    return out


def dropped_entries():
    """(name, the text of what it runs) of every gate the analysis-code level does not run. Streams g and m run only
    meta-gates' `capture` and `compose` helpers, which this walk does not expand: what they run (kvcost measure and
    compose: run_measure's calls) is section 4's"""
    mg = shell_functions(read("run/meta-gates.sh"))
    kept = {"stream_zn", "stream_ps", "stream_yc", "stream_d", "stream_k"}
    out = [(n, mg[n]) for n in sorted(mg) if n.startswith("stream_") and n not in kept]
    out.append(("z_stores", mg["z_stores"]))
    # stream_z, stream_p and stream_y call the kept zn, ps and yc, and z runs `just lint`, which the level's check stage runs too
    # (just check): drop those from their text
    out = [(n, re.sub(r"^\s*stream_(zn|ps|yc)\s*$|^.*\bjust lint\b.*$", "", t, flags=re.M)) for n, t in out]
    gf = shell_functions(read("run/gate-full.sh"))
    out += [("gate-full " + n, gf[n]) for n in ("preflight", "records")]
    out += [("gate-full " + n, f"just {n}") for n in ("golden-counts", "tally-version")]
    out.append(("gate-full controls", "run/meta-build-controls.sh"))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--plant", choices=["kvcost-in-dropped"], help="a planted defect the check must catch (exit 1)")
    a = ap.parse_args()

    # 1. the path rule
    cases = [("paper/derive/x.py", "analysis"), ("docs/a.md", "analysis"), ("python/kvcost/README.md", "analysis"),
             ("python/kvcost/layout_cost/compose.py", "analysis-code"), ("experiments/job/x.py", "analysis-code"),
             ("python/kvcost/layout_cost/picks.py", "full"), ("tests/unit/selftest.py", "full"),
             ("tests/unit/check_x.py", "analysis-code"), ("tests/fast_tests.txt", "full"),
             ("experiments/meta/controls/store_readahead_off.patch", "full"), ("python/kvcost/layout_cost/grid_pick_record.py", "analysis-code"), ("python/kvcost/predict_accuracy/uniform_49_tallies.py", "full"),
             ("pyproject.toml", "full"), (".python-version", "full"),
             ("python/kvevaluator/measure/grid_bodies.py", "full"), ("cpp/kvevaluator/src/a.cc", "full"),
             ("inputs/paper_results.json", "full"), ("run/meta-gates.sh", "full"), ("tests/gates/check_grid.py", "full"),
             ("tests/lint/check_layers.py", "full"), ("justfile", "full"), ("uv.lock", "full")]
    bad = [(p, lv, GL.level_of([p])) for p, lv in cases if GL.level_of([p]) != lv]
    check(f"1 the level of {len(cases)} paths", not bad, bad)
    check("1 a mixed branch takes its highest path's level",
          GL.level_of(["docs/a.md", "python/kvcost/x.py"]) == "analysis-code"
          and GL.level_of(["python/kvcost/x.py", "run/x.sh"]) == "full")
    overlap = [d for d in GL.CAPTURE_KEY_PATHS if GL.in_level(d, "analysis-code") or GL.in_level(d + "/x", "analysis-code")]
    check("1 no capture-key tree is inside ANALYSIS_CODE_ONLY", not overlap, overlap)

    # 2. the capture key on a scratch repo
    with tempfile.TemporaryDirectory() as tmp:
        g = lambda *x: subprocess.run(["git", "-C", tmp, *x], check=True, capture_output=True, text=True)
        g("init", "-q")
        g("config", "user.email", "t@t"), g("config", "user.name", "t")
        def commit(path, text):
            os.makedirs(os.path.dirname(os.path.join(tmp, path)), exist_ok=True)
            with open(os.path.join(tmp, path), "w") as f:
                f.write(text)
            g("add", path)
            g("commit", "-q", "-m", path)
            return GL.capture_key("HEAD", tmp)
        key_file = lambda d: d if "." in d else f"{d}/a"   # a key path is a dir, or a file (pyproject.toml, uv.lock)
        for d in GL.CAPTURE_KEY_PATHS:
            k0 = commit(key_file(d), "0")
        moved = {}
        for path in ("python/kvcost/a.py", "experiments/x/b.py", "tests/unit/c.py", "paper/d.tex"):
            moved[path] = commit(path, "1") != k0
        check("2 a commit at the analysis-code level keeps the key", not any(moved.values()), moved)
        moved = {}
        for d in GL.CAPTURE_KEY_PATHS:
            k = GL.capture_key("HEAD", tmp)
            moved[d] = commit(key_file(d), "changed") != k
        check(f"2 a commit in each of the {len(GL.CAPTURE_KEY_PATHS)} key paths moves the key", all(moved.values()), moved)

    # 3. what the level drops reaches no path an analysis-code branch may change
    # the walk's own cases: a load from path pieces, and a dir read by a computed name
    want = {"tests/unit/check_registry.py": "run/reproduce.py",
            "tests/gates/meta/check_chooser_named.py": "python/kvcost/layout_cost/grid_pick_record.py"}
    missed = {f: w for f, w in want.items() if w not in py_refs(f)}
    check(f"3 the walk follows a load built from path pieces ({len(want)} cases)", not missed, missed)
    check("3 the walk follows a dir read by a computed name (the control patches)",
          ("dir", "experiments/meta/controls/") in refs_of_text(read("run/meta-build-controls.sh")))
    recipes = just_recipes()
    entries = dropped_entries()
    hits, dir_hits = {}, {}
    reached = set()
    for name, text in entries:   # run/meta-gates.sh is not followed: its streams are entries of their own
        files = closure(refs_of_text(text), recipes, skip=("run/meta-gates.sh",))
        if a.plant == "kvcost-in-dropped" and name == "stream_x":
            files.add("python/kvcost/layout_cost/compose.py")
        reached |= files
        for f in files:
            if f.startswith("dir:"):
                if f[4:].startswith(GL.ANALYSIS_CODE_ONLY_DIRS):
                    dir_hits.setdefault(f[4:], set()).add(name)
            elif f.startswith(GL.ANALYSIS_CODE_ONLY_DIRS) and not GL.in_level(f, "analysis") \
                    and not f.startswith(GL.FULL_INSIDE_DIRS):
                hits.setdefault(f, set()).add(name)
    listed = set(GL.FULL_INSIDE_ANALYSIS_CODE)
    new = {f: sorted(v) for f, v in sorted(hits.items()) if f not in listed}
    stale = sorted(listed - set(hits))
    check(f"3 the {len(entries)} dropped gates reach {len(hits)} files under the analysis-code paths, each in "
          f"FULL_INSIDE_ANALYSIS_CODE ({len(reached)} files reached in all)", not new and len(reached) > 50,
          f"add to FULL_INSIDE_ANALYSIS_CODE (a dropped gate runs it): {new}")
    check(f"3 every one of the {len(listed)} FULL_INSIDE_ANALYSIS_CODE entries is reached by a dropped gate", not stale,
          f"no dropped gate reaches these any more: remove them: {stale}")
    check(f"3 the dirs the dropped gates read by a computed name ({sorted(dir_hits)}) are FULL_INSIDE_DIRS",
          set(dir_hits) == set(GL.FULL_INSIDE_DIRS), f"reached {sorted(dir_hits)}, listed {sorted(GL.FULL_INSIDE_DIRS)}")

    # 4. what the level reuses: the Python calls kvcost measure and kvcost compose make (run_measure.calls, compose_call)
    text = read("python/kvevaluator/measure/run_measure.py")
    calls = set(re.findall(r'"-m", "([a-z_.]+)"', text)) | set(re.findall(r'"(tests/[A-Za-z0-9_./-]+\.py)"', text))
    derived = {"tests/gates/check_grid.py", "kvcost.layout_cost.compose_cells"}
    measured = {c for c in calls if c.startswith("kvevaluator.")}
    other = sorted(calls - derived - measured)
    check(f"4 kvcost measure's and compose's {len(calls)} kinds of Python call are measured ({len(measured)}) or "
          f"re-derived ({len(calls & derived)})", len(measured) >= 3 and not other and calls & derived == derived, other)
    bad_imports = re.findall(r"^\s*(?:from|import) (kvcost|experiments)\b", text, re.M)
    check("4 run_measure.py, which makes the calls, imports no kvcost or experiments", not bad_imports, bad_imports)

    # 5. which record's capture is reused: a malformed record is skipped, a capture changed after its gate is not reused
    with tempfile.TemporaryDirectory() as tmp:
        recs, cap = os.path.join(tmp, "records"), os.path.join(tmp, "cap")
        os.makedirs(recs), os.makedirs(os.path.join(cap, "captures", "counts"))
        with open(os.path.join(cap, "captures", "counts", "a.json"), "w") as f:
            f.write("1")
        good = {"level": "full", "verdict": "pass", "end": "20261110T000000Z", "binary": {"dirty": False},
                "capture": {"path": cap, "key": "K", "digest": GL.capture_digest(cap)}}
        for name, rec in (("good", good), ("null-binary", dict(good, binary=None, end="20261111T000000Z")),
                          ("list", [1]), ("null-path", dict(good, capture={"path": None, "key": "K"}, end="20261112T000000Z")),
                          ("no-digest", dict(good, capture={"path": cap, "key": "K"}, end="20261113T000000Z"))):
            with open(os.path.join(recs, name + ".json"), "w") as f:
                json.dump(rec, f)
        with open(os.path.join(recs, "broken.json"), "w") as f:
            f.write("{")
        found = GL.reusable_capture(recs, "K")
        check("5 malformed records are skipped; the good one is found", found[0] == cap, found[2])
        check("5 another key finds none", GL.reusable_capture(recs, "other")[0] is None)
        with open(os.path.join(cap, "captures", "counts", "a.json"), "w") as f:
            f.write("2")   # written after its gate
        found = GL.reusable_capture(recs, "K")
        check("5 a capture changed after its gate is not reused", found[0] is None, found[2])

    print(f"check_gate_levels: {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
