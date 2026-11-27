#!/usr/bin/env python3
"""`kvevaluator tally --owner-sample P --owner-floor F` on a tiny graph (cpp/kvevaluator/src/cli/tally.cc; no store,
seconds). 12 Persons; Person i likes Posts 0 .. 2^i - 1, so the raw count of a sample's likes (out) keys, its Split
row's keys over the Person scale, is the bit set of the sampled Persons. 2,048 Posts and 300 Comments (one group,
Message), each with a creator.

  whole     F = 20 > 12 Persons: Person's rate is 1.0 and its rows (props, index, likes out, knows, hasCreator in)
            equal the full tally's, unscaled; on Chunked props and adjacency too, with the chunks of the families
            Person owns alone (props:Person, knows) and the locality of the knows lists
  plain     the Posts (above F) keep P: their rate is P / 100 and their rows equal the plain sample's
  contains  F = 6 (Person's rate 0.5, raised from P): over seeds 1-8, every Person in the plain sample is in the floor
            sample (the bit sets), and the floor sample is larger at least once
  zero      F = 0 gives the plain output, byte for byte outside wall time and provenance
  ends      a plain sample on Chunked reads only its sample: every sampled family's first_id is the sample's lowest
            owner (at or above the full tally's), its last_chunk_value_bytes the family's pack target x the contract's
            last_chunk_fill, flagged last_chunk_assumed
  refuse    a group whose labels get different rates (Comments 30, Posts 2,048, F = 6) refuses

    python3 tests/unit/check_tally_owner_floor.py
"""
import json
import os
import subprocess
import sys
import tempfile

import check_predict_rules as R  # (its record helpers)
from kvevaluator import contract
from kvevaluator.paths import ROOT, cpp_bin
from kvevaluator.predict import assemble_tally as A

SPEC = str(ROOT / "inputs" / "specs" / "grid" / "props_split_adj_split.json")
CHUNKED = str(ROOT / "inputs" / "specs" / "grid" / "props_chunked_adj_chunked.json")
P = 10
fails, cases = [], 0


def case(what, ok, got=""):
    global cases
    cases += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {what}" + (f": {got}" if got else ""))
    if not ok:
        fails.append(what)


def graph(comments=300):
    lines = [R.person(i) for i in range(12)] + [R.knows(i, (i + 1) % 12) for i in range(12)]
    lines += [R.post(i) for i in range(2048)] + [R.comment(i) for i in range(2048, 2048 + comments)]
    lines += [R.has_creator(m, m % 12, "Post" if m < 2048 else "Comment") for m in range(2048 + comments)]
    lines += [["e", "likes", R.vid(0, p), R.vid(1, m), {"creationDate": R.T0}, "Person", "Post"]
              for p in range(12) for m in range(2 ** p)]
    return sorted(lines, key=lambda x: x[0] != "v")   # vertices before edges, as the record stream has them


def tally(records, *flags, spec=SPEC):
    r = subprocess.run([str(cpp_bin()), "tally", "--schema", R.SCHEMA, "--spec", spec, "--records", records, *flags],
                       capture_output=True, text=True)
    return (json.loads(r.stdout) if r.returncode == 0 else None), r


def rows(t, pred):
    return sorted((json.dumps(g, sort_keys=True) for g in t["groups"] if pred(g)))


def person_row(g):
    return (g["part"] in ("vertex_props", "index") and g["type"] == "Person") or \
           (g["part"] == "adjacency" and g["owner"] == "Person")


def likes_keys(t):
    return next((g["keys"] for g in t["groups"] if g["part"] == "adjacency" and g["type"] == "likes"
                 and g["direction"] == "out"), 0)   # 0: no Person sampled


def write(path, lines):
    with open(path, "w") as f:
        for x in lines:
            f.write(json.dumps(x, separators=(",", ":")) + "\n")   # compact, as the record stream is (the count pass reads it)


def main():
    with tempfile.TemporaryDirectory() as t:
        records = os.path.join(t, "records.jsonl")
        write(records, graph())
        full, _ = tally(records)
        plain, _ = tally(records, "--owner-sample", str(P), "--seed", "1")
        whole, _ = tally(records, "--owner-sample", str(P), "--seed", "1", "--owner-floor", "20")
        rates = whole["owner_sample"]["rates"]
        case("whole: F = 20 > 12 Persons gives Person the rate 1.0", rates["Person"] == 1.0, rates["Person"])
        pr = rows(full, person_row)
        case(f"whole: Person's {len(pr)} rows equal the full tally's (unscaled)", pr == rows(whole, person_row) and pr != [])
        case("whole: the plain sample's Person rows differ (the control)", rows(plain, person_row) != pr)
        cf, _ = tally(records, spec=CHUNKED)
        cw, _ = tally(records, "--owner-sample", str(P), "--seed", "1", "--owner-floor", "20", spec=CHUNKED)
        fams = ("props:Person", "knows:out", "knows:in")
        # a family kept whole keeps its own last chunk and first id too (tally.cc: only a sampled family's last chunk
        # is assumed)
        same = [x in cf["chunks"] and cf["chunks"][x] == cw["chunks"].get(x) for x in fams]
        knows = lambda t: [r for r in t["locality"] if r["etype"] == "knows"]
        case(f"whole, Chunked: Person's rows, the chunks of {', '.join(fams)} and the {len(knows(cf))} knows locality "
             "rows equal the full tally's", rows(cf, person_row) == rows(cw, person_row) and all(same)
             and knows(cf) == knows(cw) != [], same)
        case(f"plain: the Posts keep the rate P / 100 ({P / 100})", rates["Post"] == P / 100 == rates["Comment"],
             (rates["Post"], rates["Comment"]))
        post = lambda g: g["type"] == "Post" and g["part"] in ("vertex_props", "index")
        case("plain: Post's rows equal the plain sample's", rows(whole, post) == rows(plain, post) != [])
        case("plain: Post's rows are scaled (they differ from the full tally's)", rows(plain, post) != rows(full, post))
        case("the output names its floor", whole["owner_sample"].get("floor") == 20, whole["owner_sample"].get("floor"))

        raised, grew, members = 0, 0, 0
        for seed in range(1, 9):
            p, _ = tally(records, "--owner-sample", str(P), "--seed", str(seed))
            f, _ = tally(records, "--owner-sample", str(P), "--seed", str(seed), "--owner-floor", "6")
            rate = f["owner_sample"]["rates"]["Person"]
            sp, sf = round(likes_keys(p) * P / 100), round(likes_keys(f) * rate)   # the bit sets of sampled Persons
            if sp & ~sf:
                case(f"contains: seed {seed}: the plain sample's Persons {sp:b} are in the floor's {sf:b}", False)
            raised += rate == 0.5
            grew += sf != sp
            members += bin(sp).count("1")
        case(f"contains: over 8 seeds, each of the plain samples' {members} Persons is in the floor sample; the floor's "
             f"rate 0.5, larger in {grew} seeds",
             raised == 8 and grew > 0 and members > 0 and not any(x.startswith("contains: seed") for x in fails))

        cs, _ = tally(records, "--owner-sample", str(P), "--seed", "1", spec=CHUNKED)
        sf = sorted(f for f, c in cs["chunks"].items() if c.get("last_chunk_assumed"))
        case(f"ends: {len(sf)} sampled Chunked families, each first_id the sample's own, at or above the full tally's",
             sf != [] and all(int(cs["chunks"][f]["first_id"], 16) >= int(cf["chunks"][f]["first_id"], 16) for f in sf), sf)
        with open(CHUNKED) as fh:
            owners = json.load(fh)
        target = lambda f: owners["vertex_props" if f.startswith("props:") else "adjacency"]["owners"]["pack"]
        case(f"ends: each last chunk assumed the pack target x {contract.last_chunk_fill()}",
             sf != [] and all(cs["chunks"][f]["last_chunk_value_bytes"] == round(target(f) * contract.last_chunk_fill())
                              for f in sf), [(f, cs["chunks"][f]["last_chunk_value_bytes"], target(f)) for f in sf])
        case("ends: the sample names its fill", cs["owner_sample"].get("last_chunk_fill") == contract.last_chunk_fill(),
             cs["owner_sample"].get("last_chunk_fill"))
        zero, _ = tally(records, "--owner-sample", str(P), "--seed", "1", "--owner-floor", "0")
        case("zero: F = 0 gives the plain output byte for byte (wall time, provenance aside)",
             json.dumps(A.comparable(zero)) == json.dumps(A.comparable(plain)))

        bad = os.path.join(t, "bad.jsonl")
        write(bad, graph(comments=30))
        out, r = tally(bad, "--owner-sample", str(P), "--seed", "1", "--owner-floor", "6")
        case("refuse: Post and Comment (group Message) at different rates refuse",
             out is None and "different rates" in r.stderr, r.stderr.strip()[-120:])
        out, r = tally(records, "--owner-floor", "6")
        case("refuse: --owner-floor without --owner-sample", out is None and r.returncode == 2, r.stderr.strip())

    print(f"check_tally_owner_floor: {cases} cases, {len(fails)} failed: {'PASS' if not fails and cases else 'FAIL'}")
    return 1 if fails or not cases else 0


if __name__ == "__main__":
    sys.exit(main())
