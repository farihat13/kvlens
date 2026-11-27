#!/usr/bin/env python3
"""LDBC Interactive's uniform param set, per scale factor -> inputs/params/ldbc/sf<SF>/interactive/uniform/ (tracked): the same
files and columns as LDBC's, with the bound vertex drawn uniformly at random instead of LDBC's curated choice.

  IC1-IC12  row i = LDBC's hosted row i with personId replaced by the i-th person of the query's own seeded draw over
            every Person (each query its own draw, as LDBC's rows bind other persons per query); the other columns
            (dates, names, tags, countries) stay LDBC's row i. IS1-IS3 read IC1's file, as they do on LDBC's set.
  IS4-IS7   row i = the i-th message of the query's own seeded draw over every Post and Comment; kind, depth and
            replies are that message's own (they document the row; the queries bind messageId only).

A query reads this set with --param-set uniform (datasets/ldbc/params.py); its captures then get their own
<params> dir (output_tree.params_dir). Deterministic: the same (SF, --n, --seed) writes the same bytes. Seconds at sf0.1, about a minute at SF3 (the comment CSVs).

    python -m kvevaluator.workloads.interactive.gen_uniform_params --sf 3 [--n 5] [--seed 137]
"""
import argparse
import hashlib
import json
import os
import sys

from kvevaluator.datasets.ldbc import params as P
from kvevaluator.datasets.ldbc import uniform as U

IC = [f"ic{n}" for n in range(1, 13)]
SHORT = [f"is{n}" for n in range(4, 8)]


def _read(path):
    with open(path) as f:
        header = f.readline().rstrip("\n").split("|")
        return header, [line.rstrip("\n").split("|") for line in f]


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sf", required=True)
    ap.add_argument("--n", type=int, default=5, help="rows per file (the grid runs params 1-5)")
    ap.add_argument("--seed", type=int, default=137)
    a = ap.parse_args()
    out = P.uniform_dir(a.sf)
    os.makedirs(out, exist_ok=True)

    persons = U.ids(a.sf, U.PERSONS)
    messages = U.ids(a.sf, U.MESSAGES)
    draws = {q: U.draw(persons if q in IC else messages, a.n, f"{a.seed}-{q}") for q in IC + SHORT}
    facts = U.message_facts(a.sf, {m: messages[m] for q in SHORT for m in draws[q]})

    files = {}
    for q in IC + SHORT:
        src = P.param_file(a.sf, q, source="hosted")
        header, rows = _read(src)
        if len(rows) < a.n:
            sys.exit(f"{src}: {len(rows)} rows, fewer than --n {a.n}")
        new = []
        for i, row in enumerate(rows[:a.n]):
            r = dict(zip(header, row))
            if q in IC:
                r["personId"] = str(draws[q][i])
            else:
                m = draws[q][i]
                kind, depth, replies = facts[m]
                r.update(messageId=str(m), kind=kind, depth=str(depth))
                if "replies" in r:
                    r["replies"] = str(replies)
            new.append("|".join(r[c] for c in header))
        name = os.path.basename(src)
        path = os.path.join(out, name)
        with open(path, "w") as f:
            f.write("|".join(header) + "\n" + "\n".join(new) + "\n")
        files[name] = {"from": os.path.relpath(src, str(P.ROOT)), "from_sha256": _sha(src), "sha256": _sha(path),
                       "replaced": ["personId"] if q in IC else ["messageId", "kind", "depth"] + (["replies"] if "replies" in header else [])}
    meta = {
        "what": "LDBC Interactive's uniform param set: LDBC's rows 1-n with the bound person (IC) or message (IS4-7) "
                "drawn uniformly at random; every other column is LDBC's row i",
        "generated_by": f"python -m kvevaluator.workloads.interactive.gen_uniform_params --sf {a.sf} --n {a.n} --seed {a.seed}",
        "seed": a.seed, "n": a.n,
        "population": {"Person": len(persons), "Post+Comment": len(messages)},
        "draws": draws,
        "python": sys.version.split()[0],
        "files": files,
    }
    with open(os.path.join(out, "uniform_params.meta.json"), "w") as f:
        json.dump(meta, f, indent=1)
        f.write("\n")
    print(f"[gen_uniform_params] sf{a.sf}: {len(files)} files, {a.n} rows each, one draw per query "
          f"({len({x for q in IC for x in draws[q]})} distinct persons, {len({x for q in SHORT for x in draws[q]})} distinct "
          f"messages; of {len(persons)} persons, {len(messages)} messages) -> {out}")


if __name__ == "__main__":
    main()
