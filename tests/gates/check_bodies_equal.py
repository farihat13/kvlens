#!/usr/bin/env python3
"""Do every body of a query's bundle return the SAME rows on one C++ store? The equal-answers check for a new body or
twin (the changing-plans proof, rows IN ORDER), and the only one for a format with no Python twin. Each body is
forced (candidates.forced), run on the first --nparam params over one `kvevaluator serve`, its rows hashed as the grid
does (sha1 of the JSON rows, sorted keys); per param the hashes over the bodies must be ONE value. Prints per body the
summed S / G / N, read bytes (the 4 hit lanes) and weighted ops (G + 1.1 S + 0.01 N). Exit 1 on any difference or
failed run; 0 params covered is a failure.

--pick also picks among the bodies on the window's sums under the plan rule (candidates.pick_body's scores,
candidates.held_out_pick: a body replaces the query's own only when more than 10% cheaper); pass --params 11-15 for the
held-out window. --control drops the last row of the last body's answer on every param: the check must FAIL.

    python3 tests/gates/check_bodies_equal.py --sf 0.1 --query ic5 --format nebula [--store nebula] [--nparam 5]
"""
import argparse
import hashlib
import importlib
import json
import os
import select
import sys

from kvevaluator.measure import server, stores
from kvevaluator import jsonbytes
from kvevaluator.measure.grid_capture import query_params, schema_file
from kvevaluator.plan_choice.candidates import candidate_bodies, candidates, forced, held_out_pick, invalid_candidates, pick_body

from kvevaluator.capture.step_record import HIT_BYTE_LANES  # (the lanes, one definition)
LANES = HIT_BYTE_LANES


def _derivation(workload, q, body):
    """for a twin body "<twin>:<body>": its sha and the sha of the body it was derived from (the pinned twin's body of
    the same name when the registry says the twin derives from one, TWIN_DERIVES_FROM, else the base bundle's body), so
    the evidence names what a twin was built on (a scan twin silently built on a replaced body)"""
    if ":" not in body:
        return ""
    twin, name = body.split(":", 1)
    mod = importlib.import_module(f"kvevaluator.workloads.{workload}.queries")
    src = getattr(mod, "TWIN_DERIVES_FROM", {}).get(twin)
    shas = candidate_bodies(workload, q)
    base = f"{src}:{name}" if src else name
    return f"  [sha {shas.get(body)}; derived from {base} sha {shas.get(base)}]"


def beyond_requirement(workload, q, fmt, bodies):
    """{body: the validity capabilities it requires that the format lacks} (candidates.invalid_candidates, the rule the
    grid and the pin check use). A body forced beyond its requirement can agree with the base on a few params by luck;
    that agreement is not validity."""
    if workload != "interactive" or fmt == "meta":
        return {}
    from kvevaluator.datasets.ldbc.schema import LDBC_SCHEMA
    from kvevaluator.formats.caps import caps_for, caps_resolver
    bad = invalid_candidates(workload, q, caps_for(fmt, LDBC_SCHEMA), caps_resolver(fmt, LDBC_SCHEMA))
    return {b: bad[b] for b, _ in bodies if b in bad}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--family", dest="workload", default="interactive")
    ap.add_argument("--query", required=True)
    ap.add_argument("--format", required=True, help="the C++ format of the store (dgraph, nebula, arangodb, pertype_csr, meta)")
    ap.add_argument("--store", default=None, help="the store dir name under output/stores/ldbc/sf<SF>/ (default: the format's)")
    ap.add_argument("--nparam", type=int, default=5)
    ap.add_argument("--params", default=None, metavar="LO-HI", help="a param window (1-based, inclusive) instead of the "
                    "first --nparam, e.g. 11-15, the held-out params a pick is decided on")
    ap.add_argument("--pick", action="store_true", help="also pick among the bodies under the plan rule (see above)")
    ap.add_argument("--P", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--body", default=None, help="run this one body only (e.g. a peak-RSS measurement; no comparison)")
    ap.add_argument("--valid-only", action="store_true", help="skip a body that lacks a validity capability on the format (default: run it with a NOTE)")
    ap.add_argument("--control", action="store_true", help="drop the last row of the last body's answer: must FAIL")
    a = ap.parse_args()

    store_name = a.store or (f"{a.format}-c64k" if a.format == "pertype_csr" else a.format)   # the chunked per-type store carries its chunk
    store = stores.store_dir("ldbc", a.sf, store_name)
    if not os.path.exists(os.path.join(store, "manifest.json")):
        raise SystemExit(f"no store at {store} (run/ingest-cpp-stores.sh --sf {a.sf} --formats {a.format})")
    schema = schema_file(a.workload)
    ir, cands = candidates(a.workload, a.query)
    # the base's bodies (a bundle) or the base itself, then its twins (the scan / transform twins), each once
    bodies, seen = [], set()
    for b, x in ([(b, forced(ir, b)) for b in ir.get("plans", {})] or [(a.query, ir)]) + list(cands):
        if b not in seen:
            seen.add(b)
            bodies.append((b, x))
    if a.body:
        bodies = [(b, x) for b, x in bodies if b == a.body] or sys.exit(f"no body {a.body!r} in {a.query}'s bundle")
    beyond = beyond_requirement(a.workload, a.query, a.format, bodies)
    if a.valid_only:
        bodies = [(b, x) for b, x in bodies if b not in beyond]
    for b, missing in beyond.items():
        print(f"  NOTE {b}: forced beyond its requirement on {a.format} (lacks {missing}): an agreement below is NOT validity there")

    if a.params:
        lo, hi = (int(x) for x in a.params.split("-"))
        allp = query_params(a.workload, a.sf, a.query, None)
        params = allp[lo - 1:hi]
        if len(params) < hi - lo + 1:
            raise SystemExit(f"{a.query}: the param file has {len(allp)} rows, the window {a.params} needs {hi}")
    else:
        params = query_params(a.workload, a.sf, a.query, a.nparam)

    hashes, bad = {}, 0
    sums = {b: {"S": 0, "G": 0, "N": 0, "bytes": 0} for b, _ in bodies}
    with server.CppServer(store, fmt=a.format, schema=schema) as srv:
        for i, p in enumerate(params, 1):
            for b, bir in bodies:
                req = server.build_request(bir, p, store, a.format, schema, {"P": a.P})
                srv.proc.stdin.write((jsonbytes.dumps(req) + "\n").encode())
                srv.proc.stdin.flush()
                if not select.select([srv.proc.stdout], [], [], a.timeout)[0]:
                    raise SystemExit(f"{b} p{i}: timeout")
                resp = json.loads(srv.proc.stdout.readline())
                if resp.get("error"):
                    print(f"  {b} p{i}: FAILED {str(resp['error'])[:160]}")
                    bad += 1
                    continue
                rows = resp.get("rows")
                if a.control and b == bodies[-1][0] and rows:
                    rows = rows[:-1]
                hashes.setdefault(i, {})[b] = hashlib.sha1(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:16]
                for s in resp.get("steps", []):
                    if s.get("kind") == "kv":
                        for k in ("S", "G", "N"):
                            sums[b][k] += int(s.get(k, 0))
                        sums[b]["bytes"] += sum(int((s.get("bytes") or {}).get(lane, 0)) for lane in LANES)

    differ = [i for i, hs in hashes.items() if len(set(hs.values())) > 1]
    for i in sorted(hashes):
        print(f"  p{i}: " + ", ".join(f"{b} {h}" for b, h in hashes[i].items()) + ("  DIFFER" if i in differ else ""))
    for b, s in sums.items():
        wops = s["G"] + 1.1 * s["S"] + 0.01 * s["N"]
        print(f"  {b:16s} S {s['S']:>10} G {s['G']:>8} N {s['N']:>10}  read bytes {s['bytes']:>14,}  weighted ops {wops:>12.0f}"
              + _derivation(a.workload, a.query, b))
    if a.pick and hashes:
        vec = {b: {"S": s["S"], "G": s["G"], "N": s["N"], "total_bytes": s["bytes"]} for b, s in sums.items() if b not in beyond}
        base = bodies[0][0]
        _, scores = pick_body(vec, None, base)
        chosen, why = held_out_pick(scores, base, q=a.query)
        print(f"  PICK on params {a.params or f'1-{len(params)}'}: {chosen} ({why}); scores "
              + ", ".join(f"{b} {scores[b] / scores[base]:.3f}x" for b in scores if scores.get(base)))
    n = len(hashes)
    ok = n and not differ and not bad
    print(f"{a.format} ({store_name}) {a.query}: {len(bodies)} bodies x {n} params; {len(differ)} params differ; {bad} failed runs"
          + (" -- PASS" if ok else " -- FAIL") + (" (control)" if a.control else "")
          + (f"; skipped, lacking a validity capability here: {sorted(beyond)}" if beyond and a.valid_only else
             f"; forced beyond their requirement here (agreement is not validity): {sorted(beyond)}" if beyond else ""))
    return 0 if ok else 1   # with --body: one body, "0 differ" means it ran


if __name__ == "__main__":
    sys.exit(main())
