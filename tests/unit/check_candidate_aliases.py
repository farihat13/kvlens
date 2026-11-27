#!/usr/bin/env python3
"""One candidate per run, and the names older captures use (owner: python/kvevaluator/plan_choice/candidates.py).

Store-free, on HEAD's plan files and inputs/picks/candidate_aliases.json:
  1. candidate_names and candidate_bodies name the same candidates for every LDBC / microbench / BI query, and no two
     candidates of a query share a run (body_sha and plan-level fields);
  2. every alias names a body that is no longer a candidate, and its `now` is the candidate with that body today;
     a planted alias whose `now` holds another body is refused;
  3. canonical_rows: a row under an old name takes today's name by its own body_sha, else (no body_sha) by the
     alias; a current name is kept; two rows of one body on one param keep one (the planner's) when every count is
     equal, and raise when a count differs;
  4. choose hands its result back under the capture's own names: a capture that holds a body only under its old name
     (ic4's fused body as ic4_fused:default, sf3/meta-grid-20260929) gets that name as its pick, so a reader finds the
     pick's rows in the capture.

    python3 tests/unit/check_candidate_aliases.py
"""
import json
import sys

from kvevaluator.plan_choice import candidates as C
from kvevaluator.plans.ir.bundle import body_sha

WORKLOADS = ("interactive", "microbench", "bi")


def check_lists():
    """case 1: the two candidate lists agree (but for a single-plan base: candidate_bodies names its run by the query,
    candidate_names leaves it to the planner's row) and hold one candidate per run"""
    bad, n = 0, 0
    for w in WORKLOADS:
        for q in C.workload_queries(w)[0]:
            ir, names = C.candidate_names(w, q)
            keys = [C.run_key(src, body) for _, src, body in names] + ([] if "plans" in ir else [C.run_key(ir, None)])
            heads = {nm: h for nm, h in C.candidate_bodies(w, q).items() if "plans" in ir or nm != ir.get("plan", q)}
            n += len(names)
            if len(set(keys)) != len(keys):
                print(f"  FAIL {w} {q}: two candidates share a run"); bad += 1
            if sorted(heads) != sorted(nm for nm, _, _ in names):
                print(f"  FAIL {w} {q}: candidate_bodies {sorted(heads)} != candidate_names {sorted(nm for nm, _, _ in names)}")
                bad += 1
    print(f"{'PASS' if not bad else 'FAIL'} 1 the two lists agree, one candidate per run: {n} candidates")
    return bad


def alias_problems(table):
    """the entries of an alias table that do not hold at HEAD"""
    out = []
    for w, qs in table.items():
        for q, names in qs.items():
            heads = C.candidate_bodies(w, q)
            for old, a in names.items():
                if old in heads:
                    out.append(f"{w} {q} {old!r} is still a candidate")
                if heads.get(a["now"]) != a["body_sha"]:
                    out.append(f"{w} {q} {old!r} -> {a['now']!r}: that candidate's body is {heads.get(a['now'])}, "
                               f"not {a['body_sha']}")
    return out


def check_aliases():
    """case 2: the table holds at HEAD, and a planted wrong entry is refused"""
    table = C._aliases()
    n = sum(len(m) for qs in table.values() for m in qs.values())
    probs = alias_problems(table)
    for p in probs:
        print(f"  FAIL {p}")
    planted = json.loads(json.dumps(table))
    old, a = next(iter(planted["interactive"]["ic4"].items()))
    a["now"] = next(nm for nm, h in C.candidate_bodies("interactive", "ic4").items() if h != a["body_sha"])
    caught = bool(alias_problems(planted))
    print(f"{'PASS' if not probs and n else 'FAIL'} 2 {n} aliases hold at HEAD; "
          f"{'PASS' if caught else 'FAIL'} the planted wrong alias ({old!r} -> {a['now']!r}) is refused")
    return len(probs) + (not n) + (not caught)


def check_rows():
    """case 3: canonical_rows on hand-made rows of IC4"""
    heads = C.candidate_bodies("interactive", "ic4")
    fused = C.alias_of("interactive", "ic4", "ic4_fused:default")
    base = fused["now"]
    plain = C.alias_of("interactive", "ic4", "default")["now"]     # the planner's body (named `default` in older records)
    counts = {"ok": "1", "S": "3", "G": "7", "N": "11", "result_hash": "h"}
    row = lambda v, sha, p="1", pick="0", **kw: {"query": "ic4", "param_idx": p, "body": v, "planner_pick": pick,
                                               **({"body_sha": sha} if sha is not None else {}), **counts, **kw}
    cases = []
    out = C.canonical_rows([row("ic4_fused:default", heads[base])], "interactive")
    cases.append(("an old name takes today's by its body_sha", [r["body"] for r in out] == [base]))
    out = C.canonical_rows([row("ic4_fused:default", None)], "interactive")
    cases.append(("an old name with no body_sha takes the alias", [r["body"] for r in out] == [base]))
    out = C.canonical_rows([row(plain, heads[plain])], "interactive")
    cases.append(("a current name is kept", [r["body"] for r in out] == [plain]))
    out = C.canonical_rows([row(base, heads[base], pick="0"), row("ic4_fused:default", heads[base], pick="1")], "interactive")
    cases.append(("two equal rows of one body keep one, the planner's",
                  len(out) == 1 and out[0]["planner_pick"] == "1"))
    out = C.canonical_rows([row(base, heads[base]), row("ic4_fused:default", heads[base], p="2")], "interactive")
    cases.append(("one body on two params keeps both", len(out) == 2))
    try:
        C.canonical_rows([row(base, heads[base]), row("ic4_fused:default", heads[base], G="8")], "interactive")
        refused = False
    except ValueError as e:
        refused = "differ in ['G']" in str(e)
    cases.append(("two rows of one body whose counts differ are refused", refused))
    # an exact tie breaks by body sha under an older capture's names as under today's (candidates.tie_order)
    q = "fof_posters_profile"
    old = ["fof_posters_profile_from_person_run:default", "fof_posters_profile_from_person_run:label_rows_one_run_type_index"]
    now = [C.alias_of("microbench", q, n)["now"] for n in old]
    order_old = [C.alias_of("microbench", q, n)["now"] for n in C.tie_order(old, q)]
    cases.append(("a tie breaks the same under old and current names", order_old == C.tie_order(now, q)))
    for name, ok in cases:
        print(f"{'PASS' if ok else 'FAIL'} 3 {name}")
    # 4: the planner ran its body (`plain`); the fused body, cheaper, was captured only as ic4_fused:default
    lanes = {k: "0" for k in C.READ_LANES}
    run = lambda v, sha, g, pick: {"query": "ic4", "param_idx": "1", "body": v, "planner_pick": pick, "ok": "1",
                                   "error": "", "result_hash": "h", "measure_only": "0", "body_sha": sha,
                                   "S": "0", "G": str(g), "N": "0", **lanes}
    rows = [run(plain, heads[plain], 9, "1"), run("ic4_fused:default", heads[base], 2, "0")]
    c = C.choose(rows, workload="interactive")["ic4"]
    ok = (c["body"] == "ic4_fused:default" and c["rows"]["1"]["body"] == "ic4_fused:default"
          and set(c["costs"]) == {plain, "ic4_fused:default"})
    print(f"{'PASS' if ok else 'FAIL'} 4 the pick comes back under the capture's name: {c['body']!r}, costs {sorted(c['costs'])}")
    return sum(not ok for _, ok in cases) + (not ok), len(cases) + 1


def main():
    bad = check_lists() + check_aliases()
    b, n = check_rows()
    bad += b
    print(f"check_candidate_aliases: {n + 2} cases, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
