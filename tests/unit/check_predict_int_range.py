#!/usr/bin/env python3
"""Range filters on an int property and on rows of several labels (statistics fields `value_range` and `int_values`,
inputs/contract/stats.schema.json), on a hand-checked fixture (no store, seconds). Every expected count is the true
count over the fixture's graph unless it says "model":

  graph-stats  10 persons (each knows the next), each with one post and two comments. Post lengths 0,0,0,0,0,0,1,2,3,9
               (skewed: six are 0), one a day over days 0..9; comment lengths 0..19, all on day 0; person i works at
               one organisation from 2000 + i. value_range: Post [0, 9], Comment [0, 19], workAt [2000, 2009],
               studyAt null; int_values: Post first 0, counts 6,1,1,1,0,0,0,0,0,1; Comment twenty 1s; no studyAt entry
  one label    the 20 comments: length < 5 keeps 5, <= 5 6, > 5 14, >= 5 15; 5 > length 5, 5 < length 14,
               5 >= length 6, 5 <= length < 10 5. The 10 posts: length > 0 keeps 4 (min/max alone would say 9).
               A string field with two bounds keeps half (10)
  two labels   the 30 messages read with labels [Post, Comment]: length < 5 keeps 9 + 5 = 14; creationDate before
               day 5 keeps 5 + 20 = 25; a string field keeps half (15). The same 30 as an unlabelled hop's far ends
               (weights: the far end's shares, 1/3 and 2/3): 14. A concat of comments and persons (persons declare
               no length: none passes): 5 of 30
  weights      model: a concat of the top 3 posts by length and the 20 comments weighs each side by its rows, 3 and
               20: (3 x 9/10 + 20 x 5/20) / 23 of 23 rows = 7.7, read as 8 (type counts, 10 and 20, would give 11)
  edges        the 10 workAt edges: workFrom < 2004 keeps 4, >= 2004 6, also through a hop labelled Organisation;
               knows' creationDate (an edge date, no per-day counts) keeps half (5), not Person's dates
  joins        model: a join of persons (left) and comments (right) on length < 5 keeps 1/4 of its rows (the right
               side alone holds length); a join of posts and comments (both hold it) keeps half
  refusals     no int_values entry: refused, giving the flat form; no value_range: refused; null value_range (no
               holder): 0 rows. Load refuses (and check_stats too): a bad value_range key, an uncounted label or edge
               type, [5, 3], a string; int_values not spanning value_range, a negative count, counts not summing to
               the holders, an edge's counts above its edges, an entry with no value_range. check_stats --schema
               lists a missing value_range entry. graph-stats refuses an int property holding a string

    python3 tests/unit/check_predict_int_range.py
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
from kvevaluator.predict import check_stats as CS

fails, cases = [], 0
POST_LEN = [0, 0, 0, 0, 0, 0, 1, 2, 3, 9]


def case(what, ok, got=""):
    global cases
    cases += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {what}" + (f": {got}" if got else ""))
    if not ok:
        fails.append(what)


def copy(d):
    return json.loads(json.dumps(d))


def org(i):
    return ["v", R.vid(2, i), "Organisation", {"id": i, "name": "o", "type": "company"}]


def work_at(p, o, year):
    return ["e", "workAt", R.vid(0, p), R.vid(2, o), {"workFrom": year}, "Person", "Organisation"]


def fixture():
    lines = [R.person(i) for i in range(10)] + [org(0)]
    lines += [R.post(i, i, length=POST_LEN[i], content="x") for i in range(10)]
    lines += [["v", R.vid(1, 10 + i), "Comment", {"id": 10 + i, "creationDate": R.T0 + 1000, "length": i,
                                                   "browserUsed": "x", "content": "x"}] for i in range(20)]
    lines += [R.has_creator(i, i, "Post") for i in range(10)]   # each person: one post and two comments
    lines += [R.has_creator(10 + i, i // 2, "Comment") for i in range(20)]
    lines += [work_at(i, 0, 2000 + i) for i in range(10)]
    lines += [R.knows(i, (i + 1) % 10) for i in range(10)]
    return lines


def write_stream(path, lines):
    with open(path, "w") as f:   # vertices before edges, each label's and edge type's together
        for x in sorted(lines, key=lambda x: (x[0] != "v", x[2] if x[0] == "v" else x[1])):
            f.write(json.dumps(x) + "\n")


def flt(name, rows, pred):
    return {"name": name, "op": "filter", "rows": {"ref": rows}, "pred": pred}


def cmp(k, field, x, reverse=False):
    return {k: [{"lit": x}, {"field": field}] if reverse else [{"field": field}, {"lit": x}]}


def one_label(label, pred, props=("length", "browserUsed")):
    """every vertex of `label` read with that one label, filtered, then read through a props read"""
    return [R.scan("c", label),
            {"name": "v", "op": "read_vertex_props", "vids": {"ref": "c"}, "label": label, "props": list(props)},
            flt("f", "v", pred), R.through_props("p", "f", label)]


def messages(pred):
    """the 30 messages read with labels [Post, Comment] (two labels), filtered"""
    return [R.scan("all", "Person"), R.hop("h", "all", "hasCreator", "in"),
            {"name": "m", "op": "pluck", "rows": {"ref": "h"}, "field": "dst"},
            {"name": "v", "op": "read_vertex_props", "vids": {"ref": "m"}, "label": ["Post", "Comment"],
             "props": ["length", "creationDate", "content"]},
            flt("f", "v", pred), R.through_props("p", "f", ["Post", "Comment"])]


def main():
    with tempfile.TemporaryDirectory() as t:
        rec = os.path.join(t, "records.jsonl")
        write_stream(rec, fixture())
        R.build_stats.build(rec, R.SCHEMA, "ldbc", "fixture", os.path.join(t, "stats", "stats.json"))
        built = json.load(open(os.path.join(t, "stats", "stats.json")))
        vr = built.get("value_range") or {}
        want = {"prop|Post|length": [0, 9], "prop|Comment|length": [0, 19], "eprop|workAt|workFrom": [2000, 2009],
                "eprop|studyAt|classYear": None}
        case("graph-stats: value_range is the hand values (every int property, null when none holds it)", vr == want, vr)
        iv = built.get("int_values") or {}
        want = {"prop|Post|length": {"first": 0, "counts": [6, 1, 1, 1, 0, 0, 0, 0, 0, 1]},
                "prop|Comment|length": {"first": 0, "counts": [1] * 20},
                "eprop|workAt|workFrom": {"first": 2000, "counts": [1] * 10}}
        case("graph-stats: int_values is the hand values (date_days' form, per value)", iv == want, iv)
        bad = os.path.join(t, "bad.jsonl")
        write_stream(bad, [R.person(0), R.post(0, 0, length="long")])
        r = subprocess.run([str(cpp_bin()), "graph-stats", "--schema", R.SCHEMA, "--records", bad],
                           capture_output=True, text=True)
        case("graph-stats refuses an int property holding a string",
             r.returncode != 0 and "prop|Post|length holds \"long\", not an integer" in r.stderr, r.stderr[-160:])

        os.makedirs(os.path.join(t, "tally"))
        with open(os.path.join(t, "tally", "meta-fixture.json"), "w") as f:
            subprocess.run([str(cpp_bin()), "tally", "--schema", R.SCHEMA, "--spec", R.SPEC, "--records", rec], stdout=f,
                           check=True)
        n = [0]

        def rows(stats, steps, step="p"):
            """(exit code, stderr, the step's rows at level predicted), props_distinct off so a props read passes every row"""
            n[0] += 1
            req = {"params": {}, "run": {"P": 1}, "store": {"dir": os.path.join(t, "meta-fixture"), "format": "meta",
                                                             "scale": "fixture", "schema": R.SCHEMA},
                   "plan": {"plan": "fixture", "params": {}, "let": {}, "estimate_hints": {},
                            "plans": {"b": {"output": steps[-1]["name"], "steps": steps}},
                            "bodies": {"b": {"sha": "000000000000", "sources": ["b"]}}}}
            path = S.write(os.path.join(t, f"fixture__fixture__{n[0]}.json"), req)
            sp = S.write(os.path.join(t, f"s{n[0]}.json"), stats)
            cap = os.path.join(t, f"cap{n[0]}")
            r = subprocess.run([str(cpp_bin()), "predict", "--plans", "--out-predicted", cap, "--stats", sp,
                                "--anchor-stats", os.path.join(t, "stats", "anchor_stats.json"),
                                "--tally-dir", os.path.join(t, "tally"), "--schema", R.SCHEMA,
                                "--rules-off", "props_distinct", path], capture_output=True, text=True)
            got = None
            if r.returncode == 0:
                with open(os.path.join(cap, "steps", "fixture.csv"), newline="") as f:
                    got = next((float(x["rows"]) for x in csv.DictReader(f) if x["step"] == step), None)
            return r.returncode, r.stderr, got

        def check(what, stats, steps, want, step="p"):
            rc, err, got = rows(stats, steps, step)
            case(f"{what}: {want:g}", rc == 0 and got is not None and abs(got - want) < 1e-6, f"{got} {err[-160:]}")

        for what, pred, true in (("length < 5", cmp("lt", "length", 5), 5), ("length <= 5", cmp("le", "length", 5), 6),
                                 ("length > 5", cmp("gt", "length", 5), 14), ("length >= 5", cmp("ge", "length", 5), 15),
                                 ("5 > length", cmp("gt", "length", 5, reverse=True), 5),
                                 ("5 < length", cmp("lt", "length", 5, reverse=True), 14),
                                 ("5 >= length", cmp("ge", "length", 5, reverse=True), 6),
                                 ("5 <= length < 10", {"and": [cmp("le", "length", 5, reverse=True),
                                                               cmp("lt", "length", 10)]}, 5)):
            check(f"one label, the 20 comments: {what}", built, one_label("Comment", pred), true)
        check("one label, the 10 skewed posts: length > 0 (min/max alone: 9)", built,
              one_label("Post", cmp("gt", "length", 0), ("length",)), 4)
        check("one label: a string field with two bounds keeps half", built,
              one_label("Comment", {"and": [cmp("ge", "browserUsed", 1), cmp("lt", "browserUsed", 3)]}), 10)

        check("two labels: length < 5 keeps 9 posts + 5 comments", built, messages(cmp("lt", "length", 5)), 14)
        check("two labels: creationDate before day 5 keeps 5 posts + 20 comments", built,
              messages(cmp("lt", "creationDate", (R.D0 + 5) * R.DAY)), 25)
        def after_index(hi_day, scan=False):
            """posts of days 0-4 by the date index (post i is on day i), read through a props read (or a label scan
            keeping the index's vids), then a filter on creationDate < day `hi_day`"""
            read = ({"name": "v", "op": "read_vertices_by_label", "keep": {"ref": "r"}, "label": "Post",
                     "props": ["creationDate"]} if scan else
                    {"name": "v", "op": "read_vertex_props", "vids": {"ref": "r"}, "label": "Post",
                     "props": ["creationDate"]})
            return [{"name": "r", "op": "read_index_range", "labels": ["Post"], "prop": "creationDate",
                     "lo": R.D0 * R.DAY, "hi": (R.D0 + 5) * R.DAY}, read,
                    flt("f", "v", cmp("lt", "creationDate", (R.D0 + hi_day) * R.DAY)), R.through_props("p", "f", "Post")]
        for how, scan in (("a props read", False), ("a label scan keeping its vids", True)):
            check(f"a date filter after an index read of the same window, through {how}, keeps every row (not 5 x 5/10)",
                  built, after_index(5, scan), 5)
            check(f"a date filter after an index read, through {how}: its share of the read's window, 2 of 5 days",
                  built, after_index(2, scan), 2)
        check("two labels: a string field keeps half", built,
              messages({"and": [cmp("ge", "content", 1), cmp("lt", "content", 3)]}), 15)
        hop = [R.scan("all", "Person"), R.hop("h", "all", "hasCreator", "in"), flt("f", "h", cmp("lt", "length", 5)),
               {"name": "m", "op": "pluck", "rows": {"ref": "f"}, "field": "dst"},
               R.through_props("p", "m", ["Post", "Comment"])]
        check("an unlabelled hop's far ends, weighted by their shares 1/3 and 2/3: length < 5", built, hop, 14)
        cat = [{"name": "a", "op": "read_vertices_by_label", "label": "Comment", "props": ["length"]},
               {"name": "b", "op": "read_vertices_by_label", "label": "Person", "props": ["firstName"]},
               {"name": "c", "op": "concat", "a": {"ref": "a"}, "b": {"ref": "b"}},
               flt("f", "c", cmp("lt", "length", 5)), R.through_props("p", "f", ["Comment", "Person"])]
        check("a concat of comments and persons (persons declare no length): length < 5", built, cat, 5)
        top = [{"name": "a0", "op": "read_vertices_by_label", "label": "Post", "props": ["length"]},
               {"name": "a", "op": "top_k", "rows": {"ref": "a0"}, "k": 3, "keys": [["length", "desc"]]},
               {"name": "b", "op": "read_vertices_by_label", "label": "Comment", "props": ["length"]},
               {"name": "c", "op": "concat", "a": {"ref": "a"}, "b": {"ref": "b"}},
               flt("f", "c", cmp("lt", "length", 5)), R.through_props("p", "f", ["Post", "Comment"])]
        check("model: a concat weighs its sides by their rows, 3 and 20 (type counts would give 11)", built, top, 8)

        def works(pred, label=None):
            h = R.hop("h", "all", "workAt", "out")
            if label:
                h["label"] = label
            return [R.scan("all", "Person"), h, flt("f", "h", pred), R.through_props("p", "f", "Organisation")]
        check("edge: workFrom < 2004 of the 10 workAt edges", built, works(cmp("lt", "workFrom", 2004)), 4)
        check("edge: workFrom >= 2004", built, works(cmp("ge", "workFrom", 2004)), 6)
        check("edge, a hop labelled Organisation: workFrom < 2004", built,
              works(cmp("lt", "workFrom", 2004), "Organisation"), 4)
        kn = R.hop("h", "all", "knows", "out")
        kn["label"] = "Person"
        check("edge date: knows' creationDate keeps half, not Person's dates", built,
              [R.scan("all", "Person"), kn, flt("f", "h", cmp("lt", "creationDate", R.T0 + 1)),
               R.through_props("p", "f", "Person")], 5)

        def joined(left, pred):
            st = [left, {"name": "c", "op": "read_vertices_by_label", "label": "Comment", "props": ["length"]},
                  {"name": "j", "op": "join", "left": {"ref": "l"}, "left_on": "_vid", "right": {"ref": "c"}}]
            if pred:
                st.append(flt("f", "j", pred))
            return st + [R.through_props("p", "f" if pred else "j", ["Person", "Post", "Comment"])]
        for what, left, share in (("persons (no length) and comments: the right side's share, 1/4",
                                   {"name": "l", "op": "read_vertices_by_label", "label": "Person", "props": ["firstName"]}, 0.25),
                                  ("posts and comments (both hold length): half",
                                   {"name": "l", "op": "read_vertices_by_label", "label": "Post", "props": ["length"]}, 0.5)):
            rc, err, whole = rows(built, joined(left, None))
            rc2, err2, kept = rows(built, joined(left, cmp("lt", "length", 5)))
            want = whole * share if whole is not None else None
            case(f"model: a join of {what}",
                 rc == 0 and rc2 == 0 and kept is not None and abs(kept - round(want)) <= 0.5,
                 f"{kept} of {whole} {err2[-160:]}")

        bare = copy(built)
        bare.pop("int_values")
        rc, err, _ = rows(bare, one_label("Comment", cmp("lt", "length", 5)))
        case("no int_values entry: refused, giving the flat form",
             rc != 0 and "no int_values entry 'prop|Comment|length'" in err and '"first": 0' in err and "20 counts" in err,
             err[-260:])
        rc, err, _ = rows(bare, one_label("Comment", cmp("ge", "browserUsed", 1)))
        case("no int_values: a filter on no int property still runs", rc == 0, err[-160:])
        novr = copy(bare)
        novr.pop("value_range")
        rc, err, _ = rows(novr, one_label("Comment", cmp("lt", "length", 5)))
        case("no value_range entry: refused by name", rc != 0 and "no value_range entry 'prop|Comment|length'" in err,
             err[-200:])
        none = copy(built)
        none["value_range"]["prop|Comment|length"] = None
        none["int_values"].pop("prop|Comment|length")
        check("null value_range (no comment holds a length): 0 rows", none, one_label("Comment", cmp("lt", "length", 5)), 0)

        loads = [("value_range", "a key with no kind", "Comment|length", [0, 1], "the key is not"),
                 ("value_range", "an uncounted label", "prop|Nope|length", [0, 1], "no label 'Nope'"),
                 ("value_range", "an uncounted edge type", "eprop|nope|x", [0, 1], "no edge type 'nope'"),
                 ("value_range", "least above greatest", "prop|Comment|length", [5, 3], "not [least, greatest]"),
                 ("value_range", "a string", "prop|Comment|length", "x", "not [least, greatest]"),
                 ("int_values", "values not spanning value_range", "prop|Comment|length",
                  {"first": 1, "counts": [1] * 20}, "do not run from"),
                 ("int_values", "a negative count", "prop|Comment|length",
                  {"first": 0, "counts": [-1] + [1] * 19}, "negative"),
                 ("int_values", "counts not summing to the holders", "prop|Comment|length",
                  {"first": 0, "counts": [2] * 20}, "sum to 40"),
                 ("int_values", "an edge's counts above its edges", "eprop|workAt|workFrom",
                  {"first": 2000, "counts": [5] * 10}, "above edge_totals"),
                 ("int_values", "an entry with no value_range", "eprop|studyAt|classYear",
                  {"first": 0, "counts": [0]}, "value_range has no")]
        for field, what, key, val, says in loads:
            d = copy(built)
            d[field][key] = val
            if field == "value_range" and key in d["int_values"] and key == "prop|Comment|length":
                d["int_values"].pop(key)   # the value_range case alone, not int_values' span check
            rc, err, _ = rows(d, one_label("Comment", cmp("ge", "browserUsed", 1)))
            case(f"load refuses {field}: {what}", rc != 0 and field in err and says in err, err[-200:])
            got = [x for x in CS.check_tier1(d) if x.startswith(field)]
            case(f"check_stats refuses {field}: {what} too", any(says in x for x in got), got[:1])
        miss = copy(built)
        miss["value_range"].pop("eprop|workAt|workFrom")
        miss["int_values"].pop("eprop|workAt|workFrom")
        got = [x for x in CS.check_schema(miss, None, json.load(open(R.SCHEMA))) if x.startswith("value_range")]
        case("check_stats --schema lists the missing value_range entry", got == [
            "value_range.eprop|workAt|workFrom: missing (the schema's int property; null when no element holds it)"], got)
        got = [x for x in CS.check_schema(built, None, json.load(open(R.SCHEMA))) if x.startswith("value_range")]
        case("check_stats --schema: graph-stats' keys are the schema's int properties", got == [], got)
        case("check_stats: graph-stats' own file passes the tier-1 checks", CS.check_tier1(built) == [],
             CS.check_tier1(built)[:2])
    ok = not fails
    print(f"predict int range {'PASS' if ok else 'FAIL'}: {cases} cases, {len(fails)} failed")
    return 0 if ok and cases else 1


if __name__ == "__main__":
    sys.exit(main())
