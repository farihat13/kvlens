"""run/gate-full.sh's own rules, each on a throwaway worktree at HEAD (run by hand; it builds and runs). The worktrees
belong to a clone made under --scratch (`git clone --shared`), so the lock, the records, the build cache and the planted
commits are that clone's: the check never touches this clone's state, and a real gate running here does not change it.
  dirty       a tracked edit, then an untracked file: refused (exit 2) within seconds, the file named, nothing built
  lock        a second `full` while another holds the clone's lock: refused at once, the holder named
  plant       a committed lint violation: GATE_FULL_FAIL check, no stage row after check, no new record (~3 min; ~10 min
              the first time, when preflight builds the gate stores in the shared output dir)
  late        (--late) a committed wrong golden count: GATE_FULL_FAIL golden-counts, no record (~8 min)
--control runs dirty, lock and plant once more on a gate-full.sh with that case's rule removed, and the case must then FAIL
for the expected reason (a check that encodes a rule is run on code without it): dirty and lock get past the refusal and
reach the build, which the control replaces with `false` (no gate runs); plant runs the stages after check, with the
controls and the round replaced by `true`. The worktrees and their output dirs stay under --scratch and
are printed at the end, for whoever removes them.

    python tests/gates/check_gate_full.py --scratch DIR [--late] [--control] [--cases dirty,lock,plant]
"""
import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from kvevaluator import paths

ROOT = paths.ROOT
CLONE = None     # the scratch clone the worktrees belong to, and the commit they check out (main() sets both)
HEAD = None
CASES = ["dirty", "lock", "plant"]
OUTS = {}   # worktree -> the output of each gate-full run in it
# each control's edits to the worktree's run/gate-full.sh: (a line's start, the line that replaces it); the first edit
# removes the rule, the others keep the run short
BUILD_FALSE = ("  stage build just build", "  stage build false")
CONTROL_EDITS = {"dirty": [('[ -z "$CHANGED" ] || refuse', ":"), BUILD_FALSE],
                 "lock": [("flock -n 8 || refuse", ":"), BUILD_FALSE],
                 "plant": [("unset CHAIN_KEEP_GOING", "export CHAIN_KEEP_GOING=1"),
                           ("  stage controls ", "  stage controls true"),
                           ("  stage -v '^META_GATES_DONE' round ", "  stage round true")]}
# what shows the control failed for the expected reason (the run got past the removed rule)
CONTROL_REASON = {"dirty": "GATE_FULL_FAIL build", "lock": "GATE_FULL_FAIL build", "plant": "=== rc=0 golden-counts"}


def sh(cmd, cwd, env=None, timeout=None):
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, shell=isinstance(cmd, str))
    return r.returncode, r.stdout + r.stderr


def worktree(scratch, name, control):
    """a detached worktree at HEAD with its own venv and a .env: this checkout's data dir, and one output dir for every
    case (the gate stores are built once, by the first case that reaches preflight; the cases run one at a time)"""
    wt = scratch / f"{name}{'-control' if control else ''}-{int(time.time())}"
    rc, out = sh(["git", "worktree", "add", "--detach", str(wt), HEAD], CLONE)
    if rc:
        sys.exit(f"check_gate_full: worktree add: {out}")
    data = paths.datasets_dir().parent          # this checkout's data dir (datasets/ and results/ in it)
    (wt / ".env").write_text(f"KVEVAL_DATA_DIR={data}\nKVEVAL_OUTPUT_DIR={scratch / 'output'}\n")
    rc, out = sh(["just", "setup"], wt, timeout=600)
    if rc:
        sys.exit(f"check_gate_full: just setup in {wt}: {out[-500:]}")
    if control:
        f = wt / "run" / "gate-full.sh"
        text = f.read_text()
        for old, new in CONTROL_EDITS[name]:
            lines = [ln for ln in text.splitlines() if ln.startswith(old)]
            assert len(lines) == 1, f"control {name}: no single line starting {old!r} in run/gate-full.sh"
            text = text.replace(lines[0] + "\n", new + "   # control\n")
        f.write_text(text)
        rc, out = sh(["bash", "-n", str(f)], wt)
        assert rc == 0, f"control {name}: the edited run/gate-full.sh does not parse: {out}"
        commit(wt, "control: a gate-full rule removed", ["run/gate-full.sh"])
    return wt


def commit(wt, msg, files):
    sh(["git", "add", *files], wt)
    rc, out = sh(["git", "commit", "-q", "-m", msg, "--", *files], wt)
    if rc:
        sys.exit(f"check_gate_full: commit in {wt}: {out}")


def env_of(wt):
    """the caller's environment without its KVEVAL_ settings and venv: the worktree's .env decides"""
    return {k: v for k, v in os.environ.items() if not k.startswith(("KVEVAL_", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"))}


def records_dir():
    """the scratch clone's gate records (gate_records_dir's rule: <clone>/build/gate-records)"""
    return CLONE / "build" / "gate-records"


def records_of(sha):
    return sorted(records_dir().glob(f"{sha}-*.json")) if records_dir().exists() else []


def full(wt, timeout=3600):
    """(rc, output, seconds); rc None when it ran past the timeout"""
    t0 = time.time()
    try:
        rc, out = sh(["run/gate-full.sh"], wt, env=env_of(wt), timeout=timeout)
    except subprocess.TimeoutExpired as e:
        rc, out = None, f"TIMEOUT after {timeout} s: {e.output or ''}"
    OUTS.setdefault(wt, []).append(out)
    return rc, out, time.time() - t0


def stage_rows(out):
    logs = [ln.split("logs in ", 1)[1].strip() for ln in out.splitlines() if "logs in " in ln]
    if not logs:
        return []
    f = Path(logs[0]) / "stages.tsv"
    return [ln.split("\t")[0] for ln in f.read_text().splitlines()] if f.exists() else []


def case_dirty(wt):
    """-> (ok, why)"""
    f = wt / "README.md"
    f.write_text(f.read_text() + "\nplanted\n")
    rc, out, secs = full(wt, timeout=120)
    sh(["git", "checkout", "--", "README.md"], wt)
    ok1 = rc == 2 and "GATE_FULL_REFUSED" in out and "README.md" in out and secs < 30 and "=== GATE_FULL" not in out
    (wt / "planted_untracked.txt").write_text("x\n")
    rc2, out2, secs2 = full(wt, timeout=120)
    os.replace(wt / "planted_untracked.txt", wt.parent / f"{wt.name}-moved-planted_untracked.txt")
    ok2 = rc2 == 2 and "planted_untracked.txt" in out2 and secs2 < 30
    return ok1 and ok2, f"tracked edit rc={rc} {secs:.0f}s; untracked file rc={rc2} {secs2:.0f}s"


def case_lock(wt):
    records_dir().mkdir(parents=True, exist_ok=True)
    with open(records_dir() / ".full.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write("check_gate_full holder\n"); fh.flush()
        rc, out, secs = full(wt, timeout=120)
    return rc == 2 and "holds" in out and secs < 30, f"rc={rc} {secs:.0f}s: {out.strip().splitlines()[-1][:120] if out.strip() else ''}"


def case_plant(wt, late=False):
    if late:   # one golden count off by one: the check stage passes, golden-counts must fail
        g = wt / "tests" / "fixtures" / "golden" / "counts.json"
        d = json.loads(g.read_text())
        cell = next(c for c in d["cells"] if isinstance(c.get("S"), int))
        cell["S"] += 1
        g.write_text(json.dumps(d, indent=1) + "\n")
        commit(wt, "plant: one golden count off by one", [str(g.relative_to(wt))])
        stop = "golden-counts"
    else:      # a doc where lint allows none
        p = wt / "python" / "kvevaluator" / "planted_notes.md"
        p.write_text("planted\n")
        commit(wt, "plant: a doc where lint allows none", [str(p.relative_to(wt))])
        stop = "check"
    sha = sh(["git", "rev-parse", "HEAD"], wt)[1].strip()
    before = records_of(sha)
    rc, out, secs = full(wt)
    rows = stage_rows(out)
    after_stop = rows[rows.index(stop) + 1:] if stop in rows else ["(no row for the stage)"]
    ok = rc == 1 and f"GATE_FULL_FAIL {stop}" in out and not after_stop and records_of(sha) == before
    return ok, f"rc={rc} {secs:.0f}s; stages {rows}; after {stop}: {after_stop}; records {len(records_of(sha))}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scratch", required=True, help="a dir outside this checkout for the throwaway worktrees")
    ap.add_argument("--late", action="store_true", help="also the late plant (golden-counts, ~8 min)")
    ap.add_argument("--control", action="store_true", help="also each case on gate-full.sh without its rule: must FAIL")
    ap.add_argument("--cases", default=",".join(CASES), help=f"the cases to run (default all: {','.join(CASES)})")
    a = ap.parse_args(argv)
    chosen = a.cases.split(",")
    if not chosen or set(chosen) - set(CASES):
        sys.exit(f"check_gate_full: --cases takes {','.join(CASES)}, got {a.cases}")
    global CLONE, HEAD
    scratch = Path(a.scratch).resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    HEAD = sh(["git", "rev-parse", "HEAD"], ROOT)[1].strip()
    CLONE = scratch / f"clone-{int(time.time())}"
    rc, out = sh(["git", "clone", "--quiet", "--shared", "--no-checkout", str(ROOT), str(CLONE)], scratch)
    if rc == 0:   # HEAD may be on no branch the clone took: fetch it by its sha
        rc, out = sh(["git", "fetch", "--quiet", str(ROOT), HEAD], CLONE)
    if rc:
        sys.exit(f"check_gate_full: the scratch clone: {out}")
    cases = chosen + (["late"] if a.late else [])
    # the late plant's control would test the same rule as the plant's (stop at the first failure), ~15 min longer
    runs = [(c, False) for c in cases] + ([(c, True) for c in chosen] if a.control else [])
    made, fails = [], 0
    for name, control in runs:
        wt = worktree(scratch, name, control)
        made.append(wt)
        fn = {"dirty": case_dirty, "lock": case_lock, "plant": case_plant,
              "late": lambda w: case_plant(w, late=True)}[name]
        ok, why = fn(wt)
        if control:   # a control passes when the case FAILS without its rule, for the expected reason
            reason = CONTROL_REASON[name] in "\n".join(OUTS.get(wt, []))
            good = not ok and reason
            why += f"; reason {CONTROL_REASON[name]!r} {'seen' if reason else 'NOT seen'}"
        else:
            good = ok
        fails += not good
        print(f"  {'ok  ' if good else 'FAIL'} {name}{' (control: rule removed, must fail)' if control else ''}: {why}")
    print(f"check_gate_full: {len(runs) - fails} of {len(runs)} cases as expected; the scratch clone {CLONE}, its worktrees: "
          + ", ".join(map(str, made)))
    print("check_gate_full", "OK" if not fails and runs else "FAIL")
    return 1 if fails or not runs else 0


if __name__ == "__main__":
    sys.exit(main())
