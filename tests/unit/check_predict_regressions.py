#!/usr/bin/env python3
"""Predict mode's fixed misses, kept fixed: regression goldens for the facts about a set that the cost of a Chunked
read depends on (the hop that made the set, its owners, its one label), no store. Each case is a tiny graph over the
LDBC schema in a Chunked layout at a 1 KB chunk target, and a hand-written plan; the test runs `kvevaluator predict
--plans` into a level-B capture and compares one step's lanes with the values recorded when the fix was in (GOLDEN).
These are not derived truths: they are the fixed behaviour, so a change that moves one either re-opens the bug or
must say why the new value is right (and re-record it). Each case has a sensitivity variant: the same plan with the
guarded fact taken away, whose lanes must differ from the golden (else the case would not see the bug come back).
    python3 tests/unit/check_predict_regressions.py [--record]   # --record prints the current values for GOLDEN
"""
import csv
import json
import os
import subprocess
import sys
import tempfile

from kvevaluator.paths import ROOT, cpp_bin
from kvevaluator.datasets import schema_path
from kvevaluator.predict import build_stats

SCHEMA = schema_path("ldbc")
SPEC = str(ROOT / "inputs" / "specs" / "grid_chunked_1k" / "props_chunked_c1k_adj_chunked_c1k.json")
from kvevaluator.capture.step_record import BASE_OPS, HIT_BYTE_LANES  # (the lanes, one definition)
LANES = [*BASE_OPS, *HIT_BYTE_LANES]
DAY = 86400000
T0 = 14610 * DAY


def vid(kind, i):
    return f"{kind:01x}{i:015x}"


def person(i):
    return ["v", vid(0, i), "Person", {"id": i, "creationDate": T0, "firstName": f"P{i}" + "x" * 200, "lastName": "L"}]


def message(i, label):
    props = {"id": i, "creationDate": T0 + i * 1000, "locationIP": "1.2.3.4", "browserUsed": "Firefox",
             "content": "x" * 40, "length": 40}
    if label == "Post":
        props["language"] = "en"
    return ["v", vid(1, i), label, props]


def creators_graph():
    """20 persons, each with 50 posts in one id block (person p: ids 100p..100p+49) and 50 comments spread over every
    block (ids 100q+50.., q = the comment's index mod 20). The hasCreator out lists of posts and comments share one
    Chunked family, so a person's posts span few of its chunks and its comments many"""
    lines = [person(i) for i in range(20)]
    for p in range(20):
        for k in range(50):
            lines.append(message(100 * p + k, "Post"))
            lines.append(["e", "hasCreator", vid(1, 100 * p + k), vid(0, p), {}, "Post", "Person"])
    for q in range(20):
        for k in range(50):
            m, owner = 100 * q + 50 + k, (q + k) % 20
            lines.append(message(m, "Comment"))
            lines.append(["e", "hasCreator", vid(1, m), vid(0, owner), {}, "Comment", "Person"])
    return lines


def knows_graph():
    """200 persons, ~200 bytes of props each (several to a 1 KB chunk); person a knows a+1..a+4: one person's friends
    sit in one or two chunks of the Person props"""
    lines = [person(i) for i in range(200)]
    lines += [["e", "knows", vid(0, a), vid(0, (a + d) % 200), {"creationDate": T0}, "Person", "Person"]
              for a in range(200) for d in range(1, 5)]
    return lines


def hop(name, srcs, etype, d, label=None):
    s = {"name": name, "op": "read_neighbors", "etype": etype, "dir": d, "srcs": {"ref": srcs}}
    if label:
        s["label"] = label
    return s


def props(name, vids, label, fields):
    return {"name": name, "op": "read_vertex_props", "vids": {"ref": vids}, "label": label, "props": fields}


def anchor(i=0):
    return {"name": "me", "op": "read_vertex_by_id", "label": "Person", "vid": vid(0, i)}


def case_post_span(with_label):
    """IC4's span (fixed in 97ca57e): a Chunked read of a Post-only set takes its locality span from the
    Post lists of the hop that made it, not from every label's (a person's comments span far more chunks than their
    posts). The read: the posts' own hasCreator lists. Variant: the hop names no label"""
    steps = [anchor(), hop("msgs", "me", "hasCreator", "in", "Post" if with_label else None),
             hop("back", "msgs", "hasCreator", "out")]
    return creators_graph(), steps, "back"


def case_through_props(through):
    """fix D (fixed in 63832b3): a Chunked read after a vertex props read still finds the hop that made
    its set (the props read keeps a subset of the hop's targets): one person's four friends, in one or two chunks.
    Variant: a concat stands between, which the producer walk does not cross for owners, so the read loses its
    producer and prices its vertices as scattered"""
    steps = [anchor(), hop("h", "me", "knows", "out")]
    if through:
        steps.append(props("names", "h", "Person", ["firstName"]))
    else:
        steps.append({"name": "names", "op": "concat", "a": {"ref": "h"}, "b": {"ref": "h"}})
    steps.append(props("p", "names", "Person", ["lastName"]))
    return knows_graph(), steps, "p"


# level true_terms' producer label (fixed in fa5c4d1) needs a traced run, so a store: it is checked by
# run/predict-accuracy.sh's steps.csv; level predicted's side of the same helper (producer_input) is case_post_span

CASES = {"post_span": case_post_span, "through_props": case_through_props}
GOLDEN = {   # recorded at artifact 77e1458 (both fixes in) with --record
    "post_span": {"S": 1.65, "G": 0.0, "N": 0.0, "get_key": 0.0, "get_value": 0.0, "scan_key": 18.15,
                  "scan_value": 436.425},
    "through_props": {"S": 1.6, "G": 0.0, "N": 0.0, "get_key": 0.0, "get_value": 0.0, "scan_key": 16.0,
                      "scan_value": 1788.16},
}


def lanes(lines, steps, step):
    """the step's lanes in predict's level-B capture of a one-body plan"""
    with tempfile.TemporaryDirectory() as t:
        rec = os.path.join(t, "records.jsonl")
        order = sorted(range(len(lines)), key=lambda i: (lines[i][0] != "v",
                                                          lines[i][2] if lines[i][0] == "v" else lines[i][1], i))
        with open(rec, "w") as f:
            for i in order:
                f.write(json.dumps(lines[i]) + "\n")
        st = os.path.join(t, "stats", "stats.json")
        build_stats.build(rec, SCHEMA, "ldbc", "fixture", st)
        os.makedirs(os.path.join(t, "tally"))
        with open(os.path.join(t, "tally", "meta-fixture.json"), "w") as f:
            subprocess.run([str(cpp_bin()), "tally", "--schema", SCHEMA, "--spec", SPEC, "--records", rec], stdout=f,
                           check=True)
        req = {"params": {}, "run": {"P": 1, "parts": True, "trace_args": True},
               "store": {"dir": os.path.join(t, "meta-fixture"), "format": "meta", "scale": "fixture", "schema": SCHEMA},
               "plan": {"plan": "fixture", "params": {}, "let": {}, "question": "a regression fixture",
                        "plans": {"b": {"output": steps[-1]["name"], "steps": steps}},
                        "bodies": {"b": {"sha": "000000000000", "sources": ["b"]}}}}
        path = os.path.join(t, "fixture__case__1.json")
        with open(path, "w") as f:
            json.dump(req, f)
        cap = os.path.join(t, "predicted")
        p = subprocess.run([str(cpp_bin()), "predict", "--plans", "--stats", st, "--anchor-stats",
                            os.path.join(t, "stats", "anchor_stats.json"), "--tally-dir", os.path.join(t, "tally"),
                            "--schema", SCHEMA, "--out-predicted", cap, path], capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"predict --plans exit {p.returncode}: {p.stderr.strip()[-400:]}")
        with open(os.path.join(cap, "steps", "fixture.csv")) as f:
            rows = [r for r in csv.DictReader(f) if r["step"] == step]
        if len(rows) != 1:
            raise RuntimeError(f"{len(rows)} rows for step {step} in the capture")
        return {k: float(rows[0][k]) for k in LANES}


def main(argv):
    record = "--record" in argv
    fails, n = [], 0
    for name, make in CASES.items():
        got = lanes(*make(True))
        variant = lanes(*make(False))
        if record:
            print(f"    {name!r}: {got},")
            continue
        n += 1
        want = GOLDEN.get(name)
        ok_golden, ok_variant = got == want, variant != got
        print(f"  {'ok  ' if ok_golden and ok_variant else 'FAIL'} {name}: {got}; variant {variant}"
              f"{'' if ok_golden else f' (golden {want})'}; variant {'differs' if ok_variant else 'EQUAL: the case is blind'}")
        if not ok_golden:
            fails.append(f"{name}: lanes moved from the golden")
        if not ok_variant:
            fails.append(f"{name}: the variant without the guarded fact prices the same")
    if record:
        return 0
    print(f"check_predict_regressions: {n} cases, {n - len(fails)} pass")
    if n == 0 or fails:
        for f in fails:
            print(f"   {f}")
        print("check_predict_regressions FAIL")
        return 1
    print("check_predict_regressions OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
