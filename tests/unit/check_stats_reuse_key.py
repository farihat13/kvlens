#!/usr/bin/env python3
"""A saved statistics file is reused (build_stats --if-stale, kvcost predict) only when a build now would make the
same file: the same dump of the record stream, and the same build key (the contract's x-contract, graph-stats'
version, the dataset's declared statistics), in the file and in the anchor tier beside it. On a 40-person fixture
(check_predict_rules.py's frontier graph): a fresh build is kept; a copy made under an older contract, another
graph-stats version, other declared flags, or with no key (a file made before the key) is built again; so is one
whose anchor tier is of an older contract.

    python3 tests/unit/check_stats_reuse_key.py
"""
import json
import os
import sys
import tempfile

import check_predict_rules as R  # (its fixture graph and schema)
from kvevaluator.predict import build_stats as BS

fails, cases = [], 0


def case(what, ok, got=""):
    global cases
    cases += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {what}" + (f": {got}" if got else ""))
    if not ok:
        fails.append(what)


def edited(src, dst, edit, anchor_edit=None):
    """a copy of the statistics at `src` (and its anchor tier) at `dst`, its provenance changed by `edit`"""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    for a, b, fn in ((src, dst, edit), (BS.anchor_path(src), BS.anchor_path(dst), anchor_edit)):
        with open(a) as f:
            g = json.load(f)
        if fn:
            fn(g["provenance"])
        with open(b, "w") as f:
            json.dump(g, f)
    return dst


def main():
    lines, _, _, _, _ = R.fx_frontier_inclusion()
    with tempfile.TemporaryDirectory() as t:
        rec = os.path.join(t, "records.jsonl")
        order = sorted(range(len(lines)), key=lambda i: (lines[i][0] != "v", lines[i][2] if lines[i][0] == "v" else lines[i][1], i))
        with open(rec, "w") as f:
            for i in order:
                f.write(json.dumps(lines[i]) + "\n")
        with open(rec + ".key.json", "w") as f:
            f.write('{"v": 1}')
        built = os.path.join(t, "fresh", "stats.json")
        BS.build(rec, R.SCHEMA, "ldbc", "fixture", built)
        want = BS.build_key("ldbc")
        with open(built) as f:
            prov = json.load(f)["provenance"]
        with open(BS.anchor_path(built)) as f:
            aprov = json.load(f)["provenance"]
        case("the build writes its key into the file and the anchor tier",
             all(prov.get(k) == v and aprov.get(k) == v for k, v in want.items()) and want["contract"] >= 6,
             str(want))
        case("a fresh build is kept", BS.stale(built, rec, "ldbc") is None and BS.kept(built, rec, "ldbc", anchor=True),
             str(BS.stale(built, rec, "ldbc")))

        def older_contract(p):
            p["contract"] = want["contract"] - 1
        for name, edit, word in (
                ("an older contract (x-contract one below)", older_contract, "contract"),
                ("another graph-stats version", lambda p: p.update(graph_stats_version=want["graph_stats_version"] + 1),
                 "graph_stats_version"),
                ("other declared statistics", lambda p: p.update(declared=["--dataset", "ldbc"]), "declared"),
                ("no key (made before the key)", lambda p: [p.pop(k) for k in want], "contract")):
            path = edited(built, os.path.join(t, name.split()[1], "stats.json"), edit, edit)
            why = BS.stale(path, rec, "ldbc")
            case(f"a file made under {name} is built again", bool(why) and word in why and not BS.kept(path, rec, "ldbc", anchor=True),
                 str(why))
        path = edited(built, os.path.join(t, "anchor-old", "stats.json"), None, older_contract)
        why = BS.stale(path, rec, "ldbc")
        case("a file whose anchor tier is of an older contract is built again", bool(why) and "anchor" in why, str(why))
        with open(rec + ".key.json", "w") as f:
            f.write('{"v": 2}')
        why = BS.stale(built, rec, "ldbc")
        case("a file of another dump of the record stream is built again (as before)",
             bool(why) and "another dump" in why, str(why))
    ok = not fails
    print(f"stats reuse key {'PASS' if ok else 'FAIL'}: {cases} cases, {len(fails)} failed")
    return 0 if ok and cases else 1


if __name__ == "__main__":
    sys.exit(main())
