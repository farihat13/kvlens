"""The reuse ledger (inputs/adoptions.json): rows that declare an earlier result current for today's code.

A freshness check (a key from our own code moved: a body sha, a records key, a capture's inputs) calls `check(...)`
before it warns. A row that covers the result (its content hash, the key it was made under, today's key) keeps it and
marks it `adopted` (kvevaluator.stale: the record's `adopted` list, beside `stale`), with no warning; else the check
notes it stale as before. Rows chain: a row made_under A current_for B and one B -> C cover A -> C.

A row is named by what it covers, never by a path:

    id           <kind>-<scope values (SCOPE_ORDER)>-<what[:12]>[-<made_under[:8]>-<current_for[:8]>] (a body: what[:12])
    kind         body | records_stream | capture | tally | snapshot (ROW_KINDS)
    scope        what else names the result: {workload, query} for a body; {} when the hash alone does
    what         a content hash: a body sha, a stream's records_content_sha256, a capture's counts sha, a snapshot's
                 content_digest
    made_under   the key it was made under (key_sha of the key's parts; a body: its own sha)
    current_for  the key it is declared current for
    why          {"proof": <the check that proved it>, ...} or {"judgment": <one line>, "moved": <the largest move>}
    who          agent (a proof; or a judgment carried over, why.migrated_from) | user (a judgment, or a proof)
    when         UTC, ISO 8601
    evidence     optional: what the proof ran on (trees, binaries, a witness)

Rows enter only through `add` (or `set_body_pairs`, the count-equal successor gate's writer): the whole ledger is
checked (`problems`) before it is written, sorted by id.

    python -m kvevaluator.adoptions list [--kind K]      # `just adoptions`
    python -m kvevaluator.adoptions check                # every row valid (tests-fast)
    python -m kvevaluator.adoptions add KIND --what H --made H --now H --why "..." [--scope k=v ...]   # a judgment: who=user
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from functools import lru_cache

from kvevaluator.paths import ROOT

LEDGER = os.path.join(str(ROOT), "inputs", "adoptions.json")
_PATH = [LEDGER]                    # use(): another ledger file (tests)
ROW_KINDS = {
    "body": "a plan body rewritten count-equal: rows of the old body stand for the new (its successor)",
    "records_stream": "a records stream whose dumping code moved and is shown to write today's bytes",
    "capture": "a grid cell's counts or a latency stage, made under an older key, declared current",
    "tally": "a predict library entry made from another records stream, declared current",
    "snapshot": "one format of a pinned snapshot, counted under older versions or key layout, declared current",
}
FIELDS = ("id", "kind", "scope", "what", "made_under", "current_for", "why", "who", "when")
_HEX = re.compile(r"^[0-9a-f]{8,64}$")
_WHEN = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
_DOC = ("The reuse ledger: rows that declare an earlier result current for today's code (kvevaluator.adoptions; "
        "list them: just adoptions). A row enters only through kvevaluator.adoptions.add, or, for a body, "
        "tests/gates/check_count_equal_successors.py --add, which proves it.")


def path():
    return _PATH[0]


def use(p):
    """read and write the ledger at `p` from now on in this process (None: the tracked one); the earlier path"""
    was, _PATH[0] = _PATH[0], p or LEDGER
    return was


def key_sha(parts):
    """a key's parts (any JSON value) as one sha: the first 16 hex of sha256 over its canonical JSON"""
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


@lru_cache(maxsize=None)
def _rows(p):
    if not os.path.exists(p):
        return ()
    with open(p) as f:
        return tuple(json.load(f)["rows"])


def rows(kind=None):
    """the ledger's rows (of one kind), as dicts"""
    return [dict(r) for r in _rows(path()) if kind is None or r["kind"] == kind]


SCOPE_ORDER = ("snapshot", "format", "dataset", "workload", "query", "cell", "sf")    # a scope's values in this order, then the rest by name


def scope_values(scope):
    return [str(scope[k]) for k in sorted(scope, key=lambda k: (SCOPE_ORDER.index(k) if k in SCOPE_ORDER else 99, k))]


def row_id(kind, scope, what, current_for=None, made_under=None):
    keys = [k[:8] for k in (made_under, current_for) if k and k != what]
    return "-".join([kind] + scope_values(scope) + [what[:12]] + keys)


def _why_problems(rid, r):
    """a row's why and who: a proof or a judgment, and a judgment is a person's (or carried over)"""
    why = r["why"] if isinstance(r["why"], dict) else {}
    out = [] if bool(why.get("proof")) != bool(why.get("judgment")) else [f"{rid}: why needs a proof or a judgment, one of them"]
    if r["who"] not in ("agent", "user"):
        out.append(f"{rid}: who {r['who']!r} is neither agent nor user")
    elif why.get("judgment") and r["who"] != "user" and not why.get("migrated_from"):
        out.append(f"{rid}: a judgment is a person's (who: user), never an agent's (but one carried over from "
                   f"before the ledger: why.migrated_from)")
    return out


def row_problems(r):
    """what is wrong with one row, one line each"""
    rid = r.get("id", "?")
    missing = [f for f in FIELDS if f not in r]
    if missing:
        return [f"{rid}: missing {missing}"]
    out = [] if r["kind"] in ROW_KINDS else [f"{rid}: kind {r['kind']!r} is not one of {sorted(ROW_KINDS)}"]
    out += [f"{rid}: {f} {r[f]!r} is no content hash (8-64 hex)" for f in ("what", "made_under", "current_for")
            if not isinstance(r[f], str) or not _HEX.match(r[f])]
    if r["made_under"] == r["current_for"]:
        out.append(f"{rid}: made_under == current_for (nothing to adopt)")
    if not isinstance(r["when"], str) or not _WHEN.match(r["when"]):
        out.append(f"{rid}: when {r['when']!r} is not UTC ISO 8601 (2026-11-05T21:00:00Z)")
    if r["kind"] == "body" and not {"workload", "query"} <= set(r["scope"]):
        out.append(f"{rid}: a body row's scope names its workload and query")
    return out + _why_problems(rid, r)


def _edge(r):
    """a row's start: (kind, scope, what, made_under); a body's what is its made_under"""
    return (r["kind"], json.dumps(r["scope"], sort_keys=True), r["what"] if r["kind"] != "body" else "", r["made_under"])


def problems(all_rows):
    """what is wrong with a ledger's rows: one line each (empty when every row holds)"""
    out, ids, edges = [], set(), {}
    for r in all_rows:
        out += row_problems(r)
        if any(f not in r for f in FIELDS):
            continue
        if r["id"] in ids:
            out.append(f"{r['id']}: listed twice")
        ids.add(r["id"])
        if _edge(r) in edges:
            out.append(f"{r['id']}: a second row from the same result and key ({edges[_edge(r)]})")
        edges[_edge(r)] = r["id"]
    return out + [f"a cycle: {c}" for c in _cycles(all_rows)]


def _cycles(all_rows):
    nxt = {_edge(r): r["current_for"] for r in all_rows if not [f for f in FIELDS if f not in r]}
    out = []
    for (kind, scope, what, made), now in nxt.items():
        seen = {made}
        while now not in seen and (kind, scope, what, now) in nxt:
            seen.add(now)
            now = nxt[(kind, scope, what, now)]
        if now in seen:
            out.append(f"{kind} {scope} {what[:12]} through {made}")
    return out


def _write(all_rows):
    bad = problems(all_rows)
    if bad:
        raise SystemExit("[adoptions] REFUSED, the ledger would not hold:\n  " + "\n  ".join(bad))
    p = path()
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump({"_doc": _DOC, "rows": sorted(all_rows, key=lambda r: r["id"])}, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, p)
    _rows.cache_clear()


def now_utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def add(kind, what, made_under, current_for, why, who, scope=None, evidence=None, when=None):
    """append one row (checked with the whole ledger before it is written); the row"""
    scope = dict(scope or {})
    row = {"id": row_id(kind, scope, what, current_for, made_under), "kind": kind, "scope": scope, "what": what,
           "made_under": made_under, "current_for": current_for, "why": why, "who": who, "when": when or now_utc()}
    if evidence:
        row["evidence"] = evidence
    _write(rows() + [row])
    return row


def covering(kind, what, made_under, current_for, scope=None):
    """the rows that carry a result (`what`, made under `made_under`) to `current_for`, in chain order; [] when none
    (the key unmoved needs no row: [] too)"""
    scope = json.dumps(dict(scope or {}), sort_keys=True)
    by = {r["made_under"]: r for r in rows(kind)
          if json.dumps(r["scope"], sort_keys=True) == scope and r["what"] == what}
    chain, key = [], made_under
    while key != current_for and key in by and len(chain) <= len(by):
        chain.append(by[key])
        key = by[key]["current_for"]
    return chain if chain and key == current_for else []


def adopted_or_stale(kind, what, made_under, current_for, stale_kind, detail, scope=None, hint=True):
    """the one call a freshness check makes when its key moved: True when a ledger row covers the result (marked
    `adopted: <row ids>`, no warning), else False after noting `stale_kind` (warned and marked, as before); the stale
    warning names the add command for a judgment (hint)"""
    from kvevaluator import stale
    if made_under != current_for and adopt(kind, what, made_under, current_for, detail, scope):
        return True
    hint = hint and made_under != current_for          # an unmoved key: the move is elsewhere, no row can cover it
    stale.note(stale_kind, f"{detail} ({add_hint(kind, what, made_under, current_for, scope)})" if hint else detail)
    return False


def adopt(kind, what, made_under, current_for, detail, scope=None):
    """True, and the result marked adopted, when ledger rows cover it; False (nothing noted) when none does"""
    chain = covering(kind, what, made_under, current_for, scope)
    if chain:
        from kvevaluator import stale
        stale.adopted([r["id"] for r in chain], detail)
    return bool(chain)


def add_hint(kind, what, made_under, current_for, scope=None):
    """the command that declares a result current (a judgment: a person's, who: user)"""
    sc = "".join(f"--scope {v} " for v in (f"{k}={scope[k]}" for k in sorted(scope or {})))
    return (f"to declare it current: python -m kvevaluator.adoptions add {kind} --what {what} --made {made_under} "
            f"--now {current_for} {sc}--why \"...\" --moved <largest move>")


# the count-equal successor table: body rows, read and written as the gate's pairs
def _pair(r):
    e = r.get("evidence", {})
    return {"workload": r["scope"]["workload"], "query": r["scope"]["query"], "old_body_sha": r["what"],
            "new_body_sha": r["current_for"], "old_tree": e.get("old_tree"), "old_body": e.get("old_body"),
            "new_tree": e.get("new_tree"), "new_body": e.get("new_body"), "proof": e.get("proof", {})}


def body_pairs():
    """the body rows as the successor gate's pairs {workload, query, old_*, new_*, proof}, sorted"""
    return sorted((_pair(r) for r in rows("body")), key=lambda p: (p["workload"], p["query"], p["old_body_sha"]))


def body_row(p, when=None, who="agent"):
    """a successor pair as a body row (the proof is check_count_equal_successors.py's)"""
    scope = {"workload": p["workload"], "query": p["query"]}
    return {"id": row_id("body", scope, p["old_body_sha"]), "kind": "body", "scope": scope,
            "what": p["old_body_sha"], "made_under": p["old_body_sha"], "current_for": p["new_body_sha"],
            "why": {"proof": "tests/gates/check_count_equal_successors.py", "verdict": p["proof"].get("verdict")},
            "who": who, "when": when or now_utc(),
            "evidence": {k: p[k] for k in ("old_tree", "old_body", "new_tree", "new_body", "proof")}}


def set_body_pairs(pairs):
    """replace the body rows by these pairs; a row already in the ledger keeps its who and when"""
    have = {r["id"]: r for r in rows("body")}
    new = []
    for p in pairs:
        r = body_row(p)
        if r["id"] in have:
            r["who"], r["when"] = have[r["id"]]["who"], have[r["id"]]["when"]
        new.append(r)
    _write([r for r in rows() if r["kind"] != "body"] + new)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="the rows, one line each")
    ls.add_argument("--kind", choices=sorted(ROW_KINDS))
    sub.add_parser("check", help="every row valid; exit 1 naming each problem")
    ad = sub.add_parser("add", help="a judgment row (who: user): the result declared current for today's key")
    ad.add_argument("kind", choices=sorted(set(ROW_KINDS) - {"body"}))
    ad.add_argument("--what", required=True)
    ad.add_argument("--made", required=True)
    ad.add_argument("--now", required=True)
    ad.add_argument("--why", required=True, help="one line: why the result holds for today's code")
    ad.add_argument("--moved", default="", help="the largest reported number's move, e.g. '0.2%%' (or 'none')")
    ad.add_argument("--scope", action="append", default=[], help="k=v, as the warning printed it")
    a = ap.parse_args()
    if a.cmd == "check":
        all_rows = rows()
        bad = problems(all_rows)
        for x in bad:
            print(f"  FAIL {x}")
        kinds = {k: sum(r["kind"] == k for r in all_rows) for k in sorted(ROW_KINDS)}
        print(f"[adoptions] {len(all_rows)} rows checked ({', '.join(f'{k} {n}' for k, n in kinds.items())}): "
              f"{'OK' if not bad else f'FAIL ({len(bad)})'}")
        return 1 if bad else 0
    if a.cmd == "add":
        scope = dict(s.split("=", 1) for s in a.scope)
        r = add(a.kind, a.what, a.made, a.now, {"judgment": a.why, "moved": a.moved or "not stated"}, "user", scope)
        print(f"[adoptions] added {r['id']}")
        return 0
    for r in rows(a.kind):
        why = r["why"].get("proof") or r["why"].get("judgment")
        sc = " ".join(scope_values(r["scope"]))
        print(f"{r['kind']:<15} {sc:<22} {r['what'][:12]}  {r['made_under'][:12]} -> {r['current_for'][:12]}  "
              f"{r['who']:<5} {r['when']}  {why}")
    print(f"[adoptions] {len(rows(a.kind))} rows ({path()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
