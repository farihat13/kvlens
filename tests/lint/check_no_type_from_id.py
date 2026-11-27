"""A vid is opaque at query time (python/kvevaluator/schema/vids.py): no plan reads a vertex's type from its id's bits. This
ratchet scans every workload's plan files and query modules for the known type-from-id forms and fails on any hit.

    python3 tests/lint/check_no_type_from_id.py          # exit 1 on a hit; prints the files scanned

Scanned: python/kvevaluator/workloads/<w>/plans/*.json (the committed IR) and workloads/<w>/queries/*.py (the plan
authors). Not scanned: loaders, schema and params modules -- they CONSTRUCT vids (vids.vid, the tag goes in there)
and never run at query time. The patterns, each a way the tag has been or could be read back out of a vid:
  plans:   the expr op "vid_tag" (in neither grammar; a plan file carrying it would not parse)
  queries: vid_tag / tag_of / tag_of_uid (the removed helpers), vids._TAG / _TAG[ (the construction table), uid_of or
           >> SHIFT / >> 60 (the uid's top bits; a tie-break orders by the vid STRING, lexicographic == numeric), and
           a test on a vid's first hex char: startswith("<hex>"), [:1] == / [0] == "<hex>"
A hit that is legal (none today) goes into ALLOW with the file, the line text and the reason, and shrinks only.
"""
import glob
import os
import re
import sys
from kvevaluator.paths import ROOT

EXP = str(ROOT)
WORKLOADS = os.path.join(EXP, "python", "kvevaluator", "workloads")

PLAN_PATTERNS = {
    "vid_tag op": re.compile(r'"vid_tag"'),
}
QUERY_PATTERNS = {
    "vid_tag / tag_of helper": re.compile(r"\b(vid_tag|tag_of|tag_of_uid)\b"),
    "the vid tag table": re.compile(r"\b_TAG\b"),
    "uid top bits": re.compile(r"\buid_of\b|>>\s*(SHIFT|60)\b"),
    "first hex char of a vid": re.compile(r"""startswith\(\s*["'][0-9a-f]["']\s*\)|\[(:1|0)\]\s*[!=]=\s*["'][0-9a-f]["']"""),
}
ALLOW = {}      # (relative path, line text) -> reason; shrinks only


def scan(paths, patterns):
    hits = []
    for p in paths:
        rel = os.path.relpath(p, EXP)
        for n, line in enumerate(open(p, encoding="utf-8"), 1):
            code = line.split("#", 1)[0] if p.endswith(".py") else line
            for name, rx in patterns.items():
                if rx.search(code) and (rel, line.strip()) not in ALLOW:
                    hits.append(f"{rel}:{n}: [{name}] {line.strip()[:110]}")
    return hits


def main():
    plans = sorted(glob.glob(os.path.join(WORKLOADS, "*", "plans", "*.json")))
    queries = sorted(glob.glob(os.path.join(WORKLOADS, "*", "queries", "*.py")))
    if not plans or not queries:
        print(f"FAIL: nothing to scan under {WORKLOADS} ({len(plans)} plans, {len(queries)} query modules)")
        return 1
    hits = scan(plans, PLAN_PATTERNS) + scan(queries, QUERY_PATTERNS)
    for (rel, _), reason in ALLOW.items():
        if not os.path.exists(os.path.join(EXP, rel)):
            hits.append(f"{rel}: allowlisted but gone (shrink ALLOW)")
    for h in hits:
        print(h)
    verdict = "FAIL" if hits else "OK"
    print(f"no-type-from-id {verdict}: {len(plans)} plan files + {len(queries)} query modules in "
          f"{len({p.split(os.sep)[-3] for p in plans + queries})} workloads scanned, {len(hits)} hits, {len(ALLOW)} allowed")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
