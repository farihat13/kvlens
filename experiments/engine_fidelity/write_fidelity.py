"""Write fidelity: the paper's canonical formats vs the live engines' write counts, per §5 write primitive.

Reads saved data only: the live write sweep (mb2 MB81-89, sf0.3: per-param PUT / DELETE and the
write's own reads) and the canonical formats' write counts (--capture: a writes capture's counts dir holding
dgraph-c512k.csv, nebula.csv and arangodb.csv). A write's op counts do not depend on the scale, except the vertex delete
(its degree), which is compared by the scaling law instead, so any scale's capture serves; paper-results/ holds none
(the paper reports none at SF30): make one with
`kvcost measure --dataset ldbc=0.1 --family writes dgraph-c512k nebula arangodb` (captures/ldbc/sf0.1/writes/reported/counts/).
For each (engine, primitive), live = ours + the NAMED differences below; the script checks that arithmetic and
reports the residual. Writes OUT (<output>/engines/ldbc/sf0.3/write-fidelity/write_fidelity.csv), which
paper/derive/engine_fidelity.py reads for the fidelity.writes.* keys, and prints the markdown table. Exit 1 when no
cell was compared or a cell has an unexplained residual. `just results engine-fidelity` runs the measure and this.

  python3 experiments/engine_fidelity/write_fidelity.py --sweep <write_sweep.csv> --capture <writes counts dir>
The paper's live write sweep: --sweep paper-results/engines/ldbc/sf0.3/write-sweep/write_sweep.csv (`just fetch-results`).
"""
import argparse
import csv
import os
import statistics
import sys
from kvengines.live_workloads import FIDELITY_SCALES, WRITE_FIDELITY, WRITE_STORES
from kvevaluator.output_tree import Tree

STORE = WRITE_STORES
OUT = os.path.join(str(Tree().engines("ldbc", FIDELITY_SCALES[0], WRITE_FIDELITY)), "write_fidelity.csv")

# The paper's write primitives and the mb2 query that measures each
PRIMITIVES = [("w_upd_plain", 81), ("w_upd_indexed", 82), ("w_upd_dated", None), ("w_ins_v_min", 83),
              ("w_ins_v_full", 84), ("w_ins_e_plain", 85), ("w_ins_e_facet", 86), ("w_del_e", 88),
              ("w_del_v_record", 87), ("w_del_v", 87)]
ENGINE = {"dgraph": "Dgraph", "nebula": "Nebula", "arangodb": "Arango"}   # the sweep CSV's db names

# The named differences (engine behaviour or schema, not the format), each checked in code: (P, D, name)
REINDEX = "re-maintains every index of the type on any update (ours: only the changed columns' entries)"
COMMIT = "raft commit key, one PUT per partition written (nebula src/kvstore/Part.cpp:350,410)"
REVISION = ("new document id per revision: the old document DELETEd, the primary index re-pointed, and -- every "
            "index entry carries the id -- every index entry rewritten (arangod/RocksDBEngine/RocksDBCollection.cpp:1272,1891)")
KNOWS_IDX = "mb2's engine schema indexes knows.creationDate (non-sparse), ours does not"
ID_PRED = "Dgraph stores the LDBC id as a predicate with an index; ours loads it as the uid (ID-lookup rule)"
DG_IDX = "mb2's Dgraph schema also indexes browserUsed and birthday (ours: firstName, creationDate)"
MB84_PROPS = "MB84 sets 7 properties, ours 9 (email, language)"
NULL_IDX = ("a null entry in the knows.creationDate index: NebulaGraph indexes a null column "
            "(src/common/utils/IndexKeyUtils.cpp:174-199; MB89, an unindexed edge, has one PUT less)")
VERTEX_KEY = "the vertex key, removed unconditionally (src/storage/mutate/DeleteVerticesProcessor.cpp:123)"
NAMED = {
    ("dgraph", "w_ins_v_min"): [(2, 0, ID_PRED)],
    ("dgraph", "w_ins_v_full"): [(2, 0, ID_PRED), (2, 0, DG_IDX), (-2, 0, MB84_PROPS)],
    ("nebula", "w_upd_plain"): [(2, 2, REINDEX + ": Person's 2 indexes"), (1, 0, COMMIT)],
    ("nebula", "w_upd_indexed"): [(1, 1, REINDEX + ": the other Person index"), (1, 0, COMMIT)],
    ("nebula", "w_ins_v_min"): [(1, 0, COMMIT)],
    ("nebula", "w_ins_v_full"): [(1, 0, COMMIT)],
    ("nebula", "w_ins_e_plain"): [(2, 0, COMMIT + ": 2 partitions"), (1, 0, NULL_IDX)],
    ("nebula", "w_ins_e_facet"): [(2, 0, COMMIT + ": 2 partitions"), (1, 0, KNOWS_IDX)],
    ("nebula", "w_del_e"): [(2, 0, COMMIT + ": 2 partitions"), (0, 1, KNOWS_IDX)],
    ("nebula", "w_del_v_record"): [(1, 0, COMMIT), (0, 1, VERTEX_KEY)],
    ("arangodb", "w_upd_plain"): [(3, 3, REVISION + ": Person's 2 indexes")],
    ("arangodb", "w_upd_indexed"): [(2, 2, REVISION + ": the other Person index")],
    ("arangodb", "w_ins_e_plain"): [(1, 0, KNOWS_IDX + " (a null entry)")],
    ("arangodb", "w_ins_e_facet"): [(1, 0, KNOWS_IDX)],
    ("arangodb", "w_del_e"): [(0, 1, KNOWS_IDX)],
}


def med(vals):
    return statistics.median(vals) if vals else None


def compare(sweep, capture):
    """one row per (engine, primitive): live and our PUT / DELETE medians, the named differences, the verdict"""
    live = {}
    for r in csv.DictReader(open(sweep)):
        live.setdefault((r["db"], int(r["mb"])), []).append(r)
    rows = []
    for db in ENGINE:
        cap = list(csv.DictReader(open(os.path.join(capture, f"{STORE[db]}.csv"))))
        for q, mb in PRIMITIVES:
            ours = [r for r in cap if r["query"] == q]
            oP, oD = med([float(r["P"]) for r in ours]), med([float(r["D"]) for r in ours])
            row = {"engine": db, "primitive": q, "mb": f"MB{mb}" if mb else "", "ours_P": oP, "ours_D": oD,
                   "live_P": "", "live_D": "", "live_params": 0, "named": "", "residual": "", "verdict": ""}
            if mb is None:
                row["verdict"] = "no live primitive (model only)"
            elif q == "w_del_v" or (q == "w_del_v_record" and db == "dgraph"):
                lv = live.get((ENGINE[db], mb), [])
                if q == "w_del_v" and db == "dgraph":
                    ps = [int(r["P"]) for r in lv]
                    row.update(live_P=f"{min(ps)}..{max(ps)}", live_D=0, live_params=len(lv),
                               verdict="grows with degree in both (Dgraph's MB87 deletes the node's predicates and "
                                       "reverse postings; not the same operation)")
                else:
                    row["verdict"] = "not measured this way (MB87 deletes the record on nebula / arango)"
            else:
                lv = live.get((ENGINE[db], mb), [])
                lP, lD = med([int(r["P"]) for r in lv]), med([int(r["D"]) for r in lv])
                named = NAMED.get((db, q), [])
                nP, nD = sum(n[0] for n in named), sum(n[1] for n in named)
                rP, rD = lP - oP - nP, lD - oD - nD
                row.update(live_P=lP, live_D=lD, live_params=len(lv), named="; ".join(f"{p:+g}P/{d:+g}D {name}" for p, d, name in named),
                           residual=f"{rP:+g}P/{rD:+g}D",
                           verdict=("equal" if not named and rP == 0 and rD == 0 else
                                    "equal after the named differences" if rP == 0 and rD == 0 else "unexplained residual"))
            rows.append(row)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep", required=True, help="the live write sweep CSV (mb2 MB81-89, sf0.3)")
    ap.add_argument("--capture", required=True, help="the canonical formats' write counts: a writes capture's counts "
                    "dir with dgraph-c512k.csv, nebula.csv and arangodb.csv (kvcost measure --dataset ldbc=0.1 "
                    "--family writes dgraph-c512k nebula arangodb writes captures/ldbc/sf0.1/writes/reported/counts/)")
    a = ap.parse_args(argv)
    rows = compare(a.sweep, a.capture)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print("| engine | primitive | live P/D | ours P/D | verdict | residual |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        fmt = lambda x: f"{x:g}" if isinstance(x, (int, float)) else str(x)
        lv = f"{fmt(r['live_P'])}/{fmt(r['live_D'])}" if r["live_P"] != "" else "—"
        print(f"| {r['engine']} | {r['primitive']} | {lv} | {r['ours_P']:g}/{r['ours_D']:g} | {r['verdict']} | {r['residual']} |")
    n = sum(1 for r in rows if r["residual"])
    bad = sum(r["verdict"] == "unexplained residual" for r in rows)
    print(f"\n{n} compared cells: " + ", ".join(f"{v} {sum(r['verdict'] == v for r in rows)}" for v in
          ("equal", "equal after the named differences", "unexplained residual")))
    print(f"[saved] {OUT}")
    if n == 0 or bad:
        print(f"write_fidelity: FAILED: {n} cells compared, {bad} with an unexplained residual", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
