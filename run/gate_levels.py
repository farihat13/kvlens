#!/usr/bin/env python3
"""Which merge gate a branch needs, and the key of a capture the analysis-code level may reuse. The merge check reads
this file at the branch tip.

    python3 run/gate_levels.py [--level analysis|analysis-code] <changed path>...
        prints each path outside that level as "needs full: <path>"; exit 1 when any (default --level analysis)
    python3 run/gate_levels.py --level-of <changed path>...      prints the lowest level that covers every path
    python3 run/gate_levels.py --capture-key [COMMIT]             prints the capture key of COMMIT (default HEAD)
    python3 run/gate_levels.py --reusable-capture RECORDS_DIR [KEY]   prints "<capture dir>\t<record>" of the newest full
        pass record (clean binary) with that capture key (default HEAD's) whose capture still holds the recorded bytes
        (capture_digest); nothing if none
    python3 run/gate_levels.py --capture-digest DIR               prints capture_digest(DIR)

The levels, lowest first (each covers the one before):
- analysis (`just gate analysis`: build, binary, check): ANALYSIS_ONLY, paper/, docs/ and Markdown. No stage
  that only `gate full` runs reads these paths.
- analysis-code (`just gate analysis-code`): ANALYSIS_CODE_ONLY, which adds python/kvcost, experiments/ and the unit
  tests, less the files a dropped gate still runs or loads (FULL_INSIDE_ANALYSIS_CODE, FULL_INSIDE_DIRS). Its round keeps every gate those paths can move (stream k's composition and search gates, z's store-free
  gates, `predict sizes`) and reuses the measured rows of a full gate's capture whose key (capture_key) equals this
  commit's. tests/unit/check_gate_levels.py holds the split: what the dropped stages import, and what the reused
  capture is made from.
- full: every other path (cpp/, python/kvevaluator/, inputs/, run/, tests/gates/, tests/lint/, the build files).

The capture key is the git object of each of CAPTURE_KEY_PATHS: the measured rows of `kvcost measure` are made only
from these (python/kvevaluator/measure/run_measure.py; check_grid and compose_cells are re-run on a copy by
`kvcost measure --check-only` and `kvcost compose`). A branch at the
analysis-code level changes none of them, so its key is the last full gate's on the same trees. A full gate's record
also keeps the digest of its capture's files: a capture written after its gate (meta-gates `--only m,k --capture`) is
not reused.
Standard library only: the merge check runs it outside the repo's venv.
"""
import fnmatch
import hashlib
import os
import subprocess
import sys

# fnmatch's * crosses "/": "paper/*" is every file under paper/
ANALYSIS_ONLY = ("paper/*", "docs/*", "*.md")
ANALYSIS_CODE_ONLY_DIRS = ("python/kvcost/", "experiments/", "tests/unit/")
ANALYSIS_CODE_ONLY = ANALYSIS_ONLY + tuple(d + "*" for d in ANALYSIS_CODE_ONLY_DIRS)
# files under those dirs that a gate the analysis-code level drops still runs or imports (a store-config gate times through
# grid_latency, the corners run check_meta_vs_formats, cpp-diff runs unit tests of its own): a change to one needs
# full. tests/unit/check_gate_levels.py computes the set from the gates and refuses a missing or a stale entry.
FULL_INSIDE_ANALYSIS_CODE = (
    "experiments/meta/check_meta_vs_formats.py",
    "python/kvcost/predict_accuracy/__init__.py",
    "python/kvcost/predict_accuracy/uniform_49_tallies.py",
    "python/kvcost/__init__.py",
    "python/kvcost/latency/__init__.py",
    "python/kvcost/latency/grid_latency.py",
    "python/kvcost/layout_cost/__init__.py",
    "python/kvcost/layout_cost/mixes.py",
    "python/kvcost/layout_cost/picks.py",
    "python/kvcost/layout_cost/settings.py",
    "tests/unit/check_caps_parity.py",
    "tests/unit/check_manifest_schema_path.py",
    "tests/unit/check_microbench_planir.py",
    "tests/unit/check_optimizer_rules.py",
    "tests/unit/check_plan_files.py",
    "tests/unit/check_plan_ops_supported.py",
    "tests/unit/check_planir.py",
    "tests/unit/check_planir_ldbc.py",
    "tests/unit/check_write_model.py",
    "tests/unit/check_writes_planir.py",
    "tests/unit/selftest.py",
)
# dirs a dropped gate reads by a computed name (the known-bad binaries' patches: run/meta-build-controls.sh)
FULL_INSIDE_DIRS = ("experiments/meta/controls/",)
LEVELS = {"analysis": ANALYSIS_ONLY, "analysis-code": ANALYSIS_CODE_ONLY}   # lowest first; anything else: full
# what the reused capture's measured rows are made from (the binary, the measuring code, the specs and inputs, the
# drivers, the gates that check a capture, the Python environment): every path here is outside ANALYSIS_CODE_ONLY
CAPTURE_KEY_PATHS = ("cpp", "python/kvevaluator", "inputs", "run", "tests/gates", "pyproject.toml", "uv.lock",
                     ".python-version")


def in_level(path, level):
    """True if the path matches one of the level's patterns (at analysis-code: and is not FULL_INSIDE_ANALYSIS_CODE)"""
    if level == "analysis-code" and not in_level(path, "analysis") and (
            path in FULL_INSIDE_ANALYSIS_CODE or path.startswith(FULL_INSIDE_DIRS)):
        return False
    return any(fnmatch.fnmatchcase(path, p) for p in LEVELS[level])


def needs_full(path, level="analysis"):
    """True unless the path matches one of the level's patterns"""
    return not in_level(path, level)


def level_of(paths):
    """the lowest level whose patterns cover every path: analysis, analysis-code or full"""
    return next((lv for lv in LEVELS if all(in_level(p, lv) for p in paths)), "full")


def capture_key(commit="HEAD", repo=None):
    """sha256 over "<path> <git object sha>" of each CAPTURE_KEY_PATHS entry at `commit` (a missing one: "<path> -")"""
    repo = repo or subprocess.run(["git", "-C", os.path.dirname(os.path.abspath(__file__)), "rev-parse", "--show-toplevel"],
                                  capture_output=True, text=True, check=True).stdout.strip()
    lines = []
    for d in CAPTURE_KEY_PATHS:
        r = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "-q", f"{commit}:{d}"], capture_output=True, text=True)
        lines.append(f"{d} {r.stdout.strip() or '-'}")
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def capture_digest(capture_dir):
    """sha256 over "<relative path> <sha256 of the file>" of every file under <capture_dir>/captures, sorted (None if absent)"""
    root = os.path.join(capture_dir, "captures")
    if not os.path.isdir(root):
        return None
    lines = []
    for d, _subdirs, files in os.walk(root):
        for name in files:
            p = os.path.join(d, name)
            with open(p, "rb") as f:
                lines.append(f"{os.path.relpath(p, root)} {hashlib.sha256(f.read()).hexdigest()}")
    return hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()


def reusable_capture(records_dir, key):
    """(capture dir, record path, why): the capture of the newest `full` pass record whose binary is clean, whose
    capture key is `key` and whose capture dir still holds the files the record's digest names; (None, None, why) when
    none qualifies. A record that does not parse, or lacks a field, is skipped and counted."""
    import json
    seen = {"full passes": 0, "with this key": 0, "capture gone or changed": 0, "unreadable records": 0}
    recs = []
    for name in sorted(os.listdir(records_dir)) if os.path.isdir(records_dir) else []:
        if name.endswith(".json"):
            try:
                rec = json.load(open(os.path.join(records_dir, name)))
            except (OSError, ValueError):
                rec = None
            if not isinstance(rec, dict) or not isinstance(rec.get("end", ""), str):
                seen["unreadable records"] += 1
                continue
            recs.append((rec, os.path.join(records_dir, name)))
    for rec, path in sorted(recs, key=lambda x: x[0].get("end", ""), reverse=True):
        binary = rec.get("binary")
        if rec.get("level", "full") != "full" or rec.get("verdict") != "pass" or not isinstance(binary, dict) \
                or binary.get("dirty") is not False:
            continue
        seen["full passes"] += 1
        cap = rec.get("capture")
        if not isinstance(cap, dict) or cap.get("key") != key:
            continue
        seen["with this key"] += 1
        cpath, digest = cap.get("path"), cap.get("digest")
        if not isinstance(cpath, str) or not digest or capture_digest(cpath) != digest:
            seen["capture gone or changed"] += 1
            continue
        return cpath, path, f"the capture of {os.path.basename(path)} (key {key[:12]}, digest {digest[:12]})"
    return None, None, "no capture to reuse: " + ", ".join(f"{v} {k}" for k, v in seen.items())


def main(argv):
    if argv[:1] == ["--reusable-capture"]:
        cap, rec, why = reusable_capture(argv[1], argv[2] if len(argv) > 2 else capture_key())
        print(why, file=sys.stderr)
        if cap:
            print(f"{cap}\t{rec}")
        return 0
    if argv[:1] == ["--capture-digest"] and len(argv) == 2:
        print(capture_digest(argv[1]))
        return 0
    if argv[:1] == ["--capture-key"]:
        print(capture_key(argv[1] if len(argv) > 1 else "HEAD"))
        return 0
    if argv[:1] == ["--level-of"]:
        print(level_of(argv[1:]))
        return 0
    level = "analysis"
    if argv[:1] == ["--level"]:
        if len(argv) < 2 or argv[1] not in LEVELS:
            print(f"--level: one of {', '.join(LEVELS)}", file=sys.stderr)
            return 2
        level, argv = argv[1], argv[2:]
    full = [p for p in argv if needs_full(p, level)]
    for p in full:
        print(f"needs full: {p}")
    print(f"gate level: {level_of(argv)}; {len(full)} of {len(argv)} paths outside {level}")
    return 1 if full else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
