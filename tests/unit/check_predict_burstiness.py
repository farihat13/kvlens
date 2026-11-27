#!/usr/bin/env python3
"""The ranked date-index walk's burstiness (statistics field `burstiness`, inputs/contract/stats.schema.json), on a
hand-checked fixture (no store, seconds):

  graph-stats  person 0 has 160 posts, 80 on day 0 and 80 on day 1; person 1 has 160 posts on day 2 (N = 320, k = 20;
               an owner counts from 4k = 80 neighbours). Person 0's 20th newest is on day 1: crossed = 160 (day 2) +
               80 / 2 = 200, even = 20 * 320 / 160 = 40, 5. Person 1's is on day 2: crossed = 0 + 160 / 2 = 80, even =
               40, 2. b = (160 * 5 + 160 * 2) / 320 = 3.5. hasCreator|out: every post's one creator, no owner with 4k
               neighbours: 1
  predict      a walk over a hop's far ends (persons -> hasCreator|in -> the walk, k = 20): no burstiness entry refuses,
               naming the flat form (1); at 1 the walk crosses k N / candidates = 20 entries (the even spread); at 2,
               40; the built 3.5, 70. A walk with no hop behind it (a label scan) reads none
  load         a key that is not edge type|out or in|property, or a value below 0, is refused (check_stats too)
  check_stats  the walk's plan needs burstiness hasCreator|in|creationDate

    python3 tests/unit/check_predict_burstiness.py
"""
import csv
import json
import os
import re
import subprocess
import sys
import tempfile

import check_predict_rules as R  # (its record helpers and statistics builder)
import check_predict_stats_schema as S  # (its file helper)
from kvevaluator.paths import cpp_bin
from kvevaluator.predict import check_stats as CS

KEY = "hasCreator|in|creationDate"
fails, cases = [], 0


def case(what, ok, got=""):
    global cases
    cases += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {what}" + (f": {got}" if got else ""))
    if not ok:
        fails.append(what)


def copy(d):
    return json.loads(json.dumps(d))


def fixture():
    """two persons, 320 posts: person 0 has 80 on day 0 and 80 on day 1, person 1 has 160 on day 2"""
    days = [(0, 0)] * 80 + [(0, 1)] * 80 + [(1, 2)] * 160   # (creator, day) per post
    lines = [R.person(0), R.person(1)] + [R.post(i, d) for i, (_, d) in enumerate(days)]
    lines += [R.has_creator(i, p, "Post") for i, (p, _) in enumerate(days)]
    return lines


def walk(vids_steps, vids):
    return vids_steps + [{"name": "top", "op": "read_top_k_by_index", "vids": vids, "prop": "creationDate", "k": 20,
                          "labels": ["Post"], "props": ["content"]}]


def main():
    with tempfile.TemporaryDirectory() as t:
        rec = os.path.join(t, "records.jsonl")
        with open(rec, "w") as f:   # vertices before edges, as the record stream has them
            for x in sorted(fixture(), key=lambda x: x[0] != "v"):
                f.write(json.dumps(x) + "\n")
        R.build_stats.build(rec, R.SCHEMA, "ldbc", "fixture", os.path.join(t, "stats", "stats.json"))
        built = json.load(open(os.path.join(t, "stats", "stats.json")))
        got = (built.get("burstiness") or {}).get(KEY)
        case(f"graph-stats: {KEY} is the hand value 3.5", got == 3.5, got)
        one = (built.get("burstiness") or {}).get("hasCreator|out|creationDate")
        case("graph-stats: hasCreator|out (every post has one creator, fewer than 4k) is 1", one == 1.0, one)

        os.makedirs(os.path.join(t, "tally"))
        with open(os.path.join(t, "tally", "meta-fixture.json"), "w") as f:
            subprocess.run([str(cpp_bin()), "tally", "--schema", R.SCHEMA, "--spec", R.SPEC, "--records", rec], stdout=f,
                           check=True)
        hop = [R.scan("all", "Person"), R.hop("h", "all", "hasCreator", "in"),
               {"name": "m", "op": "pluck", "rows": {"ref": "h"}, "field": "dst"}]
        n = [0]

        def crossed(stats, steps):
            """(exit code, stderr, the walk's entries crossed at level predicted: its N, one NEXT per entry)"""
            n[0] += 1
            req = {"params": {}, "run": {"P": 1}, "store": {"dir": os.path.join(t, "meta-fixture"), "format": "meta",
                                                             "scale": "fixture", "schema": R.SCHEMA},
                   "plan": {"plan": "fixture", "params": {}, "let": {}, "estimate_hints": {},
                            "plans": {"b": {"output": "top", "steps": steps}},
                            "bodies": {"b": {"sha": "000000000000", "sources": ["b"]}}}}
            path = S.write(os.path.join(t, f"fixture__fixture__{n[0]}.json"), req)
            sp = S.write(os.path.join(t, f"s{n[0]}.json"), stats)
            cap = os.path.join(t, f"cap{n[0]}")
            r = subprocess.run([str(cpp_bin()), "predict", "--plans", "--out-predicted", cap, "--stats", sp,
                                "--anchor-stats", os.path.join(t, "stats", "anchor_stats.json"),
                                "--tally-dir", os.path.join(t, "tally"), "--schema", R.SCHEMA, path],
                               capture_output=True, text=True)
            got = None
            if r.returncode == 0:
                with open(os.path.join(cap, "steps", "fixture.csv"), newline="") as f:
                    got = next((float(x["N"]) for x in csv.DictReader(f) if x["step"] == "top"), None)
            return r.returncode, r.stderr, got

        steps = walk(hop, {"ref": "m"})
        bare = copy(built)
        bare.pop("burstiness", None)
        rc, err, _ = crossed(bare, steps)
        m = re.search(r'"burstiness": \{"([^"]+)": ([0-9.]+)\}', err)
        case("no burstiness entry: refused, naming the key and the flat form",
             rc != 0 and f"no burstiness entry '{KEY}'" in err and m is not None, err[-260:])
        flat = copy(bare)
        flat["burstiness"] = {m.group(1): float(m.group(2))} if m else {}
        case("the flat form is the key at 1", m is not None and (m.group(1), float(m.group(2))) == (KEY, 1.0),
             m.groups() if m else None)
        rc, err, c1 = crossed(flat, steps)
        case("at 1 the walk crosses the even spread's k N / candidates = 20", rc == 0 and c1 == 20, f"{c1}, {err[-160:]}")
        two = copy(flat)
        two["burstiness"][KEY] = 2.0
        rc, err, c2 = crossed(two, steps)
        case("at 2 it crosses 40", rc == 0 and c2 == 40, c2)
        rc, err, cb = crossed(built, steps)
        case("the built 3.5 crosses 70", rc == 0 and cb == 70, cb)
        rc, err, cs = crossed(bare, walk([R.scan("posts", "Post")], {"ref": "posts"}))
        case("a walk over a label scan (no hop behind it) reads no burstiness", rc == 0 and cs == 20, f"{cs}, {err[-160:]}")
        chain = [R.scan("all", "Person"), R.hop("h", "all", "hasCreator", "in"),
                 {"name": "m", "op": "pluck", "rows": {"ref": "h"}, "field": "dst"},
                 {"name": "d", "op": "dedup", "items": {"ref": "m"}}]
        rc, err, cd = crossed(two, walk(chain, {"ref": "d"}))
        case("through pluck and dedup the hop is still found: at 2, 40", rc == 0 and cd == 40, f"{cd}, {err[-160:]}")
        src = hop + [{"name": "s", "op": "pluck", "rows": {"ref": "h"}, "field": "src"}]
        rc, err, _ = crossed(bare, walk(src, {"ref": "s"}))
        case("a pluck of the hop's src stops the chain: no burstiness read", rc == 0 and "burstiness" not in err,
             err[-160:])
        # the anchor's own hop, no hi, k <= 20: its recency is read instead (C++ dback); with hi it needs b
        own = [{"name": "me", "op": "read_vertex_by_id", "vid": R.vid(0, 0), "label": "Person"},
               R.hop("mine", "me", "hasCreator", "in")]
        rc, err, _ = crossed(bare, walk(own, {"ref": "mine"}))
        case("the anchor's own hop, no hi: its recency is read, no burstiness", rc == 0 and "burstiness" not in err,
             err[-160:])
        with_hi = walk(own, {"ref": "mine"})
        with_hi[-1]["hi"] = (R.D0 + 3) * R.DAY
        rc, err, _ = crossed(bare, with_hi)
        case("the same walk with hi reads burstiness (refused without it)", rc != 0 and f"no burstiness entry '{KEY}'" in err,
             err[-160:])

        for what, bad, says in (("a key with no property", {"hasCreator|in": 1}, "the key is not"),
                                ("a key in direction both", {"hasCreator|both|creationDate": 1}, "not out or in"),
                                ("a value below 0", {KEY: -0.5}, "not a number >= 0")):
            d = copy(built)
            d["burstiness"] = bad
            rc, err, _ = crossed(d, steps)
            case(f"load refuses {what}", rc != 0 and "burstiness" in err and says in err, err[-200:])
            case(f"check_stats refuses {what} too", any(x.startswith("burstiness") for x in CS.check_tier1(d)),
                 CS.check_tier1(d)[:1])
        by_name = {s["name"]: s for s in steps}
        needs = [(f, k, req) for f, k, req, _ in CS.needs_of(steps[-1], {}, by_name) if f == "burstiness"]
        case("check_stats: the walk needs burstiness hasCreator|in|creationDate", needs == [("burstiness", KEY, True)],
             needs)
    ok = not fails
    print(f"predict burstiness {'PASS' if ok else 'FAIL'}: {cases} cases, {len(fails)} failed")
    return 0 if ok and cases else 1


if __name__ == "__main__":
    sys.exit(main())
