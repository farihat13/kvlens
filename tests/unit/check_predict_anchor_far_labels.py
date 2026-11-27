#!/usr/bin/env python3
"""The anchor's own far-label mix (statistics field `anchor_far_labels`, inputs/contract/stats.schema.json), on a
hand-checked fixture (no store, seconds):

  graph-stats  person 0 has 30 posts, person 1 has 90 comments: the anchor file holds person 0's hasCreator|in as
               {Post: 30} and person 1's as {Comment: 90}; knows (one far label) has no entry
  predict      the IS2 shape: a person, its messages (hasCreator in), their Comment props. The population mix is 25%
               posts, 75% comments. At predicted_anchor on person 0 the step reads the props of 0 comments (before the field:
               30 x 0.75 = 22.5); on person 1, 90 (67.5). Without the field (an older anchor file) it falls back to the
               population's mix. Level B is unchanged: the mean 60 messages x 0.75 = 45

    python3 tests/unit/check_predict_anchor_far_labels.py
"""
import csv
import json
import os
import subprocess
import sys
import tempfile

import check_predict_rules as R  # (its record helpers and statistics builder)
import check_predict_stats_schema as S  # (its file helper)
from kvevaluator.paths import cpp_bin

fails, cases = [], 0


def case(what, ok, got=""):
    global cases
    cases += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {what}" + (f": {got}" if got else ""))
    if not ok:
        fails.append(what)


def fixture():
    """person 0 with 30 posts, person 1 with 90 comments, and one knows edge"""
    lines = [R.person(0), R.person(1), R.knows(0, 1)]
    lines += [R.post(i) for i in range(30)] + [R.comment(i) for i in range(30, 120)]
    lines += [R.has_creator(i, 0, "Post") for i in range(30)] + [R.has_creator(i, 1, "Comment") for i in range(30, 120)]
    return lines


def steps(person):
    return [{"name": "me", "op": "read_vertex_by_id", "vid": R.vid(0, person), "label": "Person"},
            {"name": "my_msgs", "op": "read_neighbors", "srcs": {"ref": "me"}, "etype": "hasCreator", "dir": "in"},
            {"name": "c", "op": "read_vertex_props", "vids": {"ref": "my_msgs"}, "props": ["length"],
             "label": "Comment"}]


def main():
    with tempfile.TemporaryDirectory() as t:
        rec = os.path.join(t, "records.jsonl")
        with open(rec, "w") as f:   # vertices before edges, as the record stream has them
            for x in sorted(fixture(), key=lambda x: x[0] != "v"):
                f.write(json.dumps(x) + "\n")
        R.build_stats.build(rec, R.SCHEMA, "ldbc", "fixture", os.path.join(t, "stats", "stats.json"))
        anchor_path = os.path.join(t, "stats", "anchor_stats.json")
        anchor = json.load(open(anchor_path))
        afl = anchor.get("anchor_far_labels") or {}
        p0, p1 = afl.get(R.vid(0, 0), {}), afl.get(R.vid(0, 1), {})
        case("graph-stats: person 0's hasCreator|in is {Post: 30}", p0.get("hasCreator|in") == {"Post": 30}, p0)
        case("graph-stats: person 1's hasCreator|in is {Comment: 90}", p1.get("hasCreator|in") == {"Comment": 90}, p1)
        case("graph-stats: knows (one far label) has no entry", not any(k.startswith("knows") for k in {**p0, **p1}),
             sorted({**p0, **p1}))

        os.makedirs(os.path.join(t, "tally"))
        with open(os.path.join(t, "tally", "meta-fixture.json"), "w") as f:
            subprocess.run([str(cpp_bin()), "tally", "--schema", R.SCHEMA, "--spec", R.SPEC, "--records", rec], stdout=f,
                           check=True)
        stats_path = os.path.join(t, "stats", "stats.json")
        n = [0]

        def comment_gets(person, anchor_file):
            """(exit code, stderr, the Comment props step's G at level predicted, at predicted_anchor), anchored on `person`"""
            n[0] += 1
            req = {"params": {}, "run": {"P": 1}, "store": {"dir": os.path.join(t, "meta-fixture"), "format": "meta",
                                                             "scale": "fixture", "schema": R.SCHEMA},
                   "plan": {"plan": "fixture", "params": {}, "let": {}, "estimate_hints": {},
                            "plans": {"b": {"output": "c", "steps": steps(person)}},
                            "bodies": {"b": {"sha": "000000000000", "sources": ["b"]}}}}
            path = S.write(os.path.join(t, f"fixture__fixture__{n[0]}.json"), req)
            caps = [os.path.join(t, f"cap{n[0]}"), os.path.join(t, f"cap{n[0]}-anchor")]
            r = subprocess.run([str(cpp_bin()), "predict", "--plans", "--out-predicted", caps[0], "--out-predicted-anchor",
                                caps[1], "--stats", stats_path, "--anchor-stats", anchor_file, "--tally-dir",
                                os.path.join(t, "tally"), "--schema", R.SCHEMA, path], capture_output=True, text=True)
            got = [None, None]
            if r.returncode == 0:
                for i, cap in enumerate(caps):
                    with open(os.path.join(cap, "steps", "fixture.csv"), newline="") as f:
                        got[i] = next((float(x["G"]) for x in csv.DictReader(f) if x["step"] == "c"), None)
            return r.returncode, r.stderr[-200:], got

        old = dict(anchor)
        old.pop("anchor_far_labels", None)
        old_path = S.write(os.path.join(t, "anchor_old.json"), old)
        for person, own, pop, what in ((0, 0, 22.5, "30 posts"), (1, 90, 67.5, "90 comments")):
            rc, err, (b, ba) = comment_gets(person, anchor_path)
            case(f"predicted_anchor on person {person} ({what}): its own mix, {own} comments' props", rc == 0 and ba == own,
                 f"{ba} {err}")
            case(f"B on person {person}: unchanged, the population's 60 x 0.75 = 45", rc == 0 and b == 45, f"{b} {err}")
            rc, err, (b, ba) = comment_gets(person, old_path)
            case(f"an anchor file without the field, person {person}: the population mix, {pop}",
                 rc == 0 and ba == pop, f"{ba} {err}")
    ok = not fails
    print(f"predict anchor far labels {'PASS' if ok else 'FAIL'}: {cases} cases, {len(fails)} failed")
    return 0 if ok and cases else 1


if __name__ == "__main__":
    sys.exit(main())
