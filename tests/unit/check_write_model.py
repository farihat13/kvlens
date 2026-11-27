"""Check the write cost model against mb2's MEASURED engine write counts.

The model this validates lives in the formats' `_w_*` operators; the numbers it is validated
against were measured on real Dgraph, NebulaGraph and ArangoDB by mb2's write sweep
(the live-engine write sweep, MB81-89 at sf0.3, 10 params each,
the per-param CSV: paper-results' engines/ldbc/sf0.3/write-sweep/).
Two things about how the comparison is run:

RUNS THE ENGINE FORMATS, NOT THE SIM ONES. mb2 measured real engines, and the real dgraph carries
the fqid/id index the SIM dropped when int vids landed. Comparing mb2 against the SIM dgraph would
compare two different physical designs, so this uses ENGINE_FORMATS on the engine store -- the same
split experiments/engine_fidelity uses.

CHECKS SCALING LAWS, NOT JUST CONSTANTS. mb2's engine schemas are not this experiment's canonical
design: they index different columns, so the absolute per-op counts are confounded by schema the
way an engine's read count is confounded by its planner. Matching them exactly would be
curve-fitting. What is NOT confounded is the SHAPE, so four laws are verdicted explicitly:

  L1  insert profile      dgraph's vertex insert grows with property count; nebula and arango
                          are flat in it (mb2: MB83 == MB84 == 4 on both).
  L2  index locality      dgraph pays index writes only when the indexed column is touched
                          (MB82 > MB81); nebula and arango re-maintain every index on any update
                          (MB82 == MB81), so their index cost is never isolable.
  L3  delete + connectivity   the same user-level "delete this vertex" scales with connectivity on
                          dgraph and is flat on the other two -- because it is not the same
                          operation: half of every incident edge lives in a posting keyed by the
                          NEIGHBOUR, so dgraph must reach them, while nebula's DELETE VERTEX and
                          arango's document removal leave the edges dangling.
  L4  delete currency     dgraph never issues a real DELETE (D identically 0 -- a logical delete is
                          a tombstone PUT); nebula and arango do.

The absolute numbers are printed side by side anyway, with the ratio, so the gaps are visible
rather than argued away. Dgraph's ratio is 2.00x on every primitive: the ENGINE format
prices a delta plus a rollup per dirtied key (one rollup per write), the live engine today counts one rewrite per
dirtied key (one rollup per write: the drain measure) -- which is what the canonical (paper) Dgraph charges, exactly
(the write-fidelity measurements). The four laws check the model's shape; they do not read these constants.

  python3 tests/unit/check_write_model.py --sf 0.1

Exit status is non-zero if a LAW fails. A constant that misses is reported, not failed: the schema
difference above makes an exact match the wrong bar.
"""
import argparse
import os
import statistics
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
from kvevaluator.oracle.run import build_stores  # the Python oracle store
from kvevaluator.oracle.runner import run_plan
from kvevaluator.measure.report import totals
from kvevaluator.workloads.writes import params as w_params
from kvevaluator.workloads.writes import queries as w_queries

DBS = ["dgraph", "nebula", "arangodb"]

# mb2's measured medians: {query: {db: (PUT, DEL)}}, from the live write sweep (sf0.3, the engines,
# Dgraph measured by the `drain` mode with the background rollup paused, as the lifecycle start does;
# the same Dgraph counts with the rollup resumed). Every param of a primitive gave the same P/D (NebulaGraph's edge
# writes vary by 1: one commit key per raft partition written). Write op counts per primitive do not depend on the
# scale; only the vertex delete does (degree), so its Dgraph row is a range (MB2_DEL_V_DGRAPH_RANGE).
# The earlier baseline (sf0.1, another machine's build, `forceall`, rollup running): Dgraph MB81 2/0,
# MB82 6/0, MB83 13/0, MB84 27/0, MB88 3/0 -- 2x today's per dirtied key, unexplained (rollup running or not does
# not change today's counts; the build and the machine are the unmeasured difference); nebula / arango as today,
# except nebula's deletes (0 PUT then, 1-2 commit-key PUTs now); MB85 / MB86 were not recorded.
MB2 = {
    "w_upd_plain":    {"mb": "MB81", "dgraph": (1, 0),  "nebula": (4, 2), "arangodb": (4, 3)},
    "w_upd_indexed":  {"mb": "MB82", "dgraph": (3, 0),  "nebula": (4, 2), "arangodb": (4, 3)},
    "w_ins_v_min":    {"mb": "MB83", "dgraph": (7, 0),  "nebula": (4, 0), "arangodb": (4, 0)},
    "w_ins_v_full":   {"mb": "MB84", "dgraph": (15, 0), "nebula": (4, 0), "arangodb": (4, 0)},
    "w_ins_e_plain":  {"mb": "MB85", "dgraph": (2, 0),  "nebula": (5, 0), "arangodb": (5, 0)},
    "w_ins_e_facet":  {"mb": "MB86", "dgraph": (2, 0),  "nebula": (5, 0), "arangodb": (5, 0)},
    # MB87's dgraph row maps to w_del_v (dgraph's delete reaches the reverse postings, so it is
    # inherently the with-edges operation); its nebula/arango rows map to w_del_v_record.
    "w_del_v_record": {"mb": "MB87", "dgraph": None,    "nebula": (1, 4), "arangodb": (0, 4)},
    # dgraph's MB87 belongs here (its delete inherently reaches the reverse postings), but as a
    # RANGE, not a median: a median over mb2's degree draw vs ours compares the draws, not the models.
    # L3 checks the slope instead; see MB2_DEL_V_DGRAPH_RANGE.
    "w_del_v":        {"mb": "MB87", "dgraph": None,    "nebula": None,  "arangodb": None},
    "w_del_e":        {"mb": "MB88", "dgraph": (2, 0),  "nebula": (2, 3), "arangodb": (0, 5)},
}

# mb2's dgraph MB87 across its 10 params (sf0.3): 27 (leaf) .. 331 (hub) PUT; the RANGE is the
# target, quoted by the L3 verdict. (sf0.1: 61 .. 765.)
MB2_DEL_V_DGRAPH_RANGE = (27, 331)


def measure_all(sf, backend, nparam, rebuild=False):
    """{query: {db: [KVCost per param]}} over the writes suite, on the ENGINE formats."""
    stores = build_stores(sf, DBS, backend, schema_name="canonical", engine=True, writes=True,
                          rebuild=rebuild)
    out = {}
    for qname in MB2:                        # the write PRIMITIVES; IU1-IU8 are grid_writes' job
        out[qname] = {}
        for db in DBS:
            fmt, store = stores[db]
            costs = []
            for args in w_params.query_params(sf, qname, nparam):
                # Each param starts from the pristine store AND from clean write state, so a
                # cell's cost never depends on which cell ran before it.
                store._backend.reset()
                if hasattr(fmt, "reset_write_state"):
                    fmt.reset_write_state()
                plan = w_queries.plan_for(qname, db, args)
                _outputs, kvcosts = run_plan(plan, fmt, store)
                costs.append(totals(kvcosts))
            if not costs:
                raise SystemExit(f"no params produced for {qname}/{db} -- the param family is "
                                 f"empty, so nothing can be compared")
            out[qname][db] = costs
    return out


def _med(costs, attr):
    return int(statistics.median([getattr(c, attr) for c in costs])) if costs else 0


def report_constants(res):
    print("\n=== model vs mb2 measured (write ops per operation, median over params) ===")
    print("    engine formats, canonical schema. A gap is REPORTED, not failed: mb2's engine")
    print("    schemas index different columns, so the constants are schema-confounded.\n")
    hdr = f"  {'query':16s} {'mb2':5s} {'db':9s} {'model P/D':>12s} {'mb2 P/D':>10s} {'ratio':>8s}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for qname in MB2:                       # the primitives only -- IU1-IU8 have no mb2 twin
        for db in DBS:
            costs = res[qname][db]
            P, D = _med(costs, "P"), _med(costs, "D")
            ref = MB2[qname].get(db)
            if ref is None:
                shown, ratio = "not recorded", "-"
            else:
                shown = f"{ref[0]}/{ref[1]}"
                tot_ref, tot_mod = ref[0] + ref[1], P + D
                ratio = f"{tot_mod / tot_ref:.2f}x" if tot_ref else ("-" if not tot_mod else "inf")
            print(f"  {qname:16s} {MB2[qname]['mb']:5s} {db:9s} {P:>6d}/{D:<5d} {shown:>10s} "
                  f"{ratio:>8s}")


def report_laws(res):
    """Verdict the four scaling laws. These are what the model must reproduce."""
    print("\n=== scaling laws (the part that is NOT schema-confounded) ===\n")
    fails = []

    def verdict(name, ok, detail):
        print(f"  {'PASS' if ok else 'FAIL'}  {name}\n        {detail}")
        if not ok:
            fails.append(name)

    # L1: insert profile -- dgraph grows with property count, the other two are flat.
    mn = {db: _med(res["w_ins_v_min"][db], "P") for db in DBS}
    fl = {db: _med(res["w_ins_v_full"][db], "P") for db in DBS}
    verdict("L1 insert profile",
            fl["dgraph"] > mn["dgraph"] and fl["nebula"] == mn["nebula"]
            and fl["arangodb"] == mn["arangodb"],
            f"dgraph {mn['dgraph']}->{fl['dgraph']} PUT (grows with props); "
            f"nebula {mn['nebula']}->{fl['nebula']}, arango {mn['arangodb']}->{fl['arangodb']} (flat)")

    # L2: index locality -- separable on dgraph, always-paid on the row/document stores.
    pl = {db: _med(res["w_upd_plain"][db], "P") + _med(res["w_upd_plain"][db], "D") for db in DBS}
    ix = {db: _med(res["w_upd_indexed"][db], "P") + _med(res["w_upd_indexed"][db], "D")
          for db in DBS}
    verdict("L2 index locality",
            ix["dgraph"] > pl["dgraph"] and ix["nebula"] == pl["nebula"]
            and ix["arangodb"] == pl["arangodb"],
            f"dgraph {pl['dgraph']}->{ix['dgraph']} write ops when the indexed column is touched; "
            f"nebula {pl['nebula']}->{ix['nebula']}, arango {pl['arangodb']}->{ix['arangodb']} "
            f"(unchanged -- every index is re-maintained either way)")

    # L3: the same user-level delete scales on dgraph, is flat on the other two.
    dg = [c.P + c.D for c in res["w_del_v"]["dgraph"]]
    flat = {db: [c.P + c.D for c in res["w_del_v_record"][db]] for db in ("nebula", "arangodb")}
    dg_slope = (max(dg) / min(dg)) if dg and min(dg) else 0
    verdict("L3 delete + connectivity",
            dg_slope > 2 and all(len(set(v)) == 1 for v in flat.values()),
            f"dgraph delete {min(dg)}..{max(dg)} write ops across the degree strata "
            f"({dg_slope:.0f}x; mb2 measured {MB2_DEL_V_DGRAPH_RANGE[0]}.."
            f"{MB2_DEL_V_DGRAPH_RANGE[1]}); nebula {sorted(set(flat['nebula']))}, "
            f"arango {sorted(set(flat['arangodb']))} (flat)")

    # L4: dgraph never issues a real DELETE.
    dg_dels = sum(_med(res[q]["dgraph"], "D") for q in MB2)
    others = sum(_med(res[q][db], "D") for q in ("w_del_v_record", "w_del_e") for db in
                 ("nebula", "arangodb"))
    verdict("L4 delete currency",
            dg_dels == 0 and others > 0,
            f"dgraph D=0 on every primitive (deletes are tombstone PUTs); "
            f"nebula+arango issue {others} real DELETEs across the delete primitives")
    return fails


def report_bytes(res):
    """The divergence the op columns cannot show: dgraph's edge-insert PUT count is flat in degree
    while its written BYTES are not, because the rollup rewrites the whole posting."""
    print("\n=== ops vs bytes: the same operation, two answers (edge insert) ===\n")
    print(f"  {'db':9s} {'PUT (min..max)':>16s} {'write bytes (min..max)':>26s}")
    print("  " + "-" * 53)
    for db in DBS:
        costs = res["w_ins_e_facet"][db]
        ps = [c.P + c.D for c in costs]
        bs = [c.write_bytes for c in costs]
        print(f"  {db:9s} {f'{min(ps)}..{max(ps)}':>16s} {f'{min(bs)}..{max(bs)}':>26s}")
    print("\n  dgraph's op count is flat in degree; its bytes are not. Both are true, and which one"
          "\n  matters depends on whether writes are op-bound or bandwidth-bound. At k=1 (rollup per"
          "\n  mutation -- the point mb2 measured at) the byte number is the UPPER bound; real steady"
          "\n  state divides it by writes_per_rollup.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sf", default="0.1")   # a small-scale oracle: never the env's SF
    ap.add_argument("--backend", choices=["abstract"], default="abstract",
                    help="the abstract oracle (byte-identical S/G/N; the python disk "
                         "backend is not shipped, step 5)")
    ap.add_argument("--nparam", type=int, default=None, help="first N params (default: all)")
    ap.add_argument("--rebuild", action="store_true",
                    help="re-ingest the ENGINE stores. a counts run's --fresh does not reach them "
                         "(it re-measures, it does not re-ingest), so a format edit leaves them stale and "
                         "this is how they get refreshed.")
    a = ap.parse_args()

    res = measure_all(a.sf, a.backend, a.nparam, a.rebuild)
    report_constants(res)
    fails = report_laws(res)
    report_bytes(res)
    if fails:
        print(f"\nFAIL: {len(fails)} law(s) not reproduced: {fails}")
        return 1
    print("\nALL LAWS REPRODUCED (constants are reported above; see the schema caveat)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
