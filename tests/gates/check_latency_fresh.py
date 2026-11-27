#!/usr/bin/env python3
"""The skip-if-fresh guard of run/kvstats.sh's latency stages (measure/latency_fresh), proven BOTH ways on a scratch tree (kvstats.sh --root):
an identical re-run SKIPS every stage; one changed identity part -- a query's param sha, a query's
plan-IR sha, the binary's counting versions, the store fingerprint -- makes the slug RE-measure; --fresh
re-measures a fresh capture. Offline first, on the freshness function itself: a writes capture stamped before the
write path's counting version (measure/counting.py) is stale, a reads capture is still fresh.
The injection is on the RECORD side (the stamped identity of the
runs the data came from), so the check is independent of the code that computes the identity. (The counts stage keeps
its own reuse: grid_bodies / grid_writes --format.)

Opens the sf0.1 dgraph store (read-only, one small query pair): run it with no other
store-toucher on that store.

    python3 tests/gates/check_latency_fresh.py
    LATENCY_FRESH_OFFLINE=1 python3 tests/gates/check_latency_fresh.py   # the offline part only (no store)
"""
import glob
import json
import os
import subprocess
import sys
import tempfile
from kvevaluator.paths import ROOT
from kvevaluator.output_tree import Tree

_EXP = str(ROOT)
CMD = ["run/kvstats.sh", "--sf", "0.1", "--family", "interactive", "--formats", "dgraph", "--queries", "is1,is3",
       "--nparam", "2", "--metrics", "latency", "--warm", "1,1", "--cold", "1"]
STAGES = ("warm", "cold")
fails = 0


def run(root, *extra):
    out = subprocess.run(CMD + ["--root", root] + list(extra), cwd=_EXP, capture_output=True, text=True).stdout
    measured = {s for s in STAGES if any(l.startswith("[run-kvstats") and
                                         f"{'warm-p0' if s == 'warm' else 'cold-p2'} dgraph" in l for l in out.splitlines())}
    skipped = {s for s in STAGES if any(l.startswith(f"[kvstats] fresh: dgraph {s} ") for l in out.splitlines())}
    return measured, skipped


def inject(root, field):
    """Poison one identity part in every ok record of the scratch capture."""
    for p in glob.glob(os.path.join(Tree(root).timings("interactive", "0.1", "runs"), "*.json")):
        with open(p) as f:
            r = json.load(f)
        ident = r.get("identity", {}).get("dgraph")
        if not ident:
            continue
        if field == "param":
            ident["params"]["is1"] = "deadbeef0000"
        elif field == "plan":
            # the BODY this store runs is the plan half of the key (latency_fresh._plan_matches); the whole-file sha
            # `plans` only decides for records without `bodies`
            ident["bodies"]["is3"] = "deadbeef0000"
        elif field == "plan-file-only":
            ident["plans"]["is3"] = "deadbeef0000"   # the bundle FILE moved, this store's body did not: still fresh
        elif field == "version":
            ident["counting"]["seam"] = 99           # counted under another seam version than the binary's
        elif field == "columns":
            ident["step_columns"] = "deadbeef0000"   # written under another step-record column list
        elif field == "store":
            ident["store"]["keys"] = 1
        with open(p, "w") as f:
            json.dump(r, f)


def check(cond, what):
    global fails
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        fails += 1


def check_write_version():
    """offline: the writes stage keys on the write counting versions, reads do not (no store run, no capture)"""
    from kvevaluator.measure import latency_fresh as KF
    KF.store_fingerprint = lambda sf, slug: {"offline": True}   # the version keys are under test, not a store: none is read
    KF._store_format = lambda sf, slug: "dgraph"
    for wl in ("writes", "interactive"):
        ident = KF.identity("0.1", wl, "dgraph", [], 2)
        legacy = {k: v for k, v in ident.items() if k not in ("counting", "format", "counted")}
        legacy["measurement_version"] = ident["counting"]["seam"]          # an identity with no format counting version
        with_write = dict(legacy, write_measurement_version=ident["counting"]["write_seam"])
        moved = dict(ident, counting=dict(ident["counting"], write_format=ident["counting"]["write_format"] + 1))
        fresh = lambda stamped: KF._query_matches({"status": "ok", "identity": {"dgraph": stamped}}, "dgraph", "q", ident)
        # a stamp with no format counting version: it stands for version 1, so it is fresh only
        # while the format's counting version is still 1 (the binary's, read here: a bump makes it stale)
        legacy_ok = ident["counting"]["format"] == 1
        if wl == "writes":
            check(ident["counted"] == "read+write", "writes: the identity compares the write counting versions")
            check(not fresh(legacy) and fresh(with_write) == legacy_ok and fresh(ident) and not fresh(moved),
                  f"writes: stale from before the write version; with it, fresh while the format version is 1 (now "
                  f"{ident['counting']['format']}); fresh with today's stamp, stale when the write format version moved")
        else:
            check(ident["counted"] == "read" and fresh(legacy) == legacy_ok and fresh(ident) and fresh(moved),
                  f"reads (interactive): a capture from before is fresh while the format version is 1 (now "
                  f"{ident['counting']['format']}), and a moved write version does not matter")


def main():
    check_write_version()
    if os.environ.get("LATENCY_FRESH_OFFLINE"):   # the offline part only (no store run; run-meta-gates stream z)
        print(f"{fails} failure(s)" if fails else "LATENCY FRESH OFFLINE OK")
        return 1 if fails else 0
    with tempfile.TemporaryDirectory() as root:
        measured, skipped = run(root)
        check(measured == set(STAGES) and not skipped, "empty scratch: every stage measured")
        measured, skipped = run(root)
        check(not measured and skipped == set(STAGES), "identical re-run: every stage skipped (fresh)")
        inject(root, "plan-file-only")
        measured, skipped = run(root)
        check(not measured and skipped == set(STAGES), "injected plan-file-only (bodies intact): every stage still fresh")
        for field in ("param", "plan", "version", "columns", "store"):
            inject(root, field)
            measured, skipped = run(root)
            check(measured == set(STAGES) and not skipped, f"injected {field}: every stage re-measured")
        measured, skipped = run(root, "--fresh")
        check(measured == set(STAGES), "--fresh on a fresh capture: re-measured")
    if fails:
        print(f"{fails} failure(s)")
        return 1
    print("LATENCY FRESH OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
