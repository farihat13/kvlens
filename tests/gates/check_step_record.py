"""The new free columns of the step record, on real plans at sf0.1 (pertype_csr, a Chunked and a Split cell of meta):
(1) each item-size histogram holds one entry per operation and its buckets bracket the summed lane, per step and per part;
(2) the histograms, the chunk statistics and rows_in are the same at P=1 and P=8; (3) an injected wrong value is caught.
    python3 tests/gates/check_step_record.py [--sf 0.1]"""
import argparse
import os
import sys
from kvevaluator.paths import ROOT

EXP = str(ROOT)
from kvevaluator.capture import step_record
from kvevaluator.measure import plan_runs, server
from kvevaluator.datasets.ldbc.params import query_params
from kvevaluator.datasets.ldbc.schema import get_schema

QUERIES = ("is1", "is2", "is7", "ic1", "ic2", "ic8", "ic9", "ic11")
# histogram column -> (its operation count, the byte lanes its sizes sum to; None: no exact lane)
RULES = {"get_kvpair_hist": ("G", ("get_key", "get_value")), "range_read_hist": ("S", ("scan_key", "scan_value")),
         "keys_per_range_read": ("S", None), "put_kvpair_hist": ("P", None), "del_kvpair_hist": ("D", None)}


def problems(where, obj):
    """what is wrong with one step's (or part's) histogram columns against its counts"""
    out = []
    cols = step_record.hist_fields(obj)
    for col, (op, lanes) in RULES.items():
        if step_record.hist_count(cols[col]) != int(obj.get(op, 0)):
            out.append(f"{where}: {col} holds {step_record.hist_count(cols[col])} entries, {op} = {obj.get(op, 0)}")
        if lanes:
            lane = sum(int(obj["bytes"].get(x, 0)) for x in lanes)
            lo, hi = step_record.hist_bounds(cols[col])
            if not lo <= lane <= hi:
                out.append(f"{where}: {col} allows {lo}..{hi} bytes, the lanes sum to {lane}")
    # exact Fetch (step_record.FETCH): a GET hit is one store block; a kind's 4 KB units lie between its bytes / 4096
    # and that plus one per request; never more store blocks than 4 KB units
    b = obj["bytes"]
    hits = int(obj.get("G", 0)) - int(obj.get("G_miss", 0))
    if cols["get_store_blocks"] != hits:
        out.append(f"{where}: get_store_blocks {cols['get_store_blocks']}, {hits} GET hits")
    for kind, op, lanes in (("get", "G", ("get_key", "get_value")), ("range", "S", ("scan_key", "scan_value"))):
        lane, units, blocks = sum(int(b.get(x, 0)) for x in lanes), cols[f"{kind}_fetch_4k"], cols[f"{kind}_store_blocks"]
        if not -(-lane // 4096) <= units <= lane // 4096 + int(obj.get(op, 0)):
            out.append(f"{where}: {kind}_fetch_4k {units} outside {-(-lane // 4096)}..{lane // 4096 + int(obj.get(op, 0))}")
        if blocks > units:
            out.append(f"{where}: {kind}_store_blocks {blocks} above {kind}_fetch_4k {units}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--names", action="store_true", help="print the store names this check opens (for store copies) and exit")
    a = ap.parse_args()
    if a.names:
        print("pertype_csr-c64k meta-props_chunked_adj_chunked meta-props_split_adj_split")
        return
    schema = get_schema("canonical")
    stores = [("pertype_csr", None)] + [("meta", os.path.join(EXP, "inputs", "specs", "grid", c + ".json"))
                                         for c in ("props_chunked_adj_chunked", "props_split_adj_split")]
    bad, nsteps, nparts, ncmp, caught, noff = [], 0, 0, 0, 0, 0
    for fmt, spec in stores:
        name = fmt if not spec else os.path.basename(spec)[:-5]
        srv = plan_runs.open_server(a.sf, fmt, spec=spec)
        try:
            lo_family = plan_runs.label_only_family(schema)
            for q in QUERIES:
                ir = plan_runs.ir_to_run("interactive", q, fmt, srv.store_dir, lo_family,
                                         scale=server.scale_of_store_dir(srv.store_dir), spec=spec)
                args = query_params(a.sf, q, 1)[0]
                resp = {P: srv.run(ir, args, run={"P": P, "parts": True, "emit": {"histograms": False}}) for P in (1, 8)}
                for P, r in resp.items():
                    if r.get("error"):
                        raise SystemExit(f"{name} {q} P={P}: {r['error']}")
                # run.emit.kvpair_stats off (a latency run): no histogram in the response, every count and byte lane equal
                off = srv.run(ir, args, run={"P": 1, "parts": True, "emit": {"histograms": False, "kvpair_stats": False}})
                noff += 1
                for s1, s0 in zip(resp[1]["steps"], off["steps"]):
                    lanes = lambda s: (s["S"], s["G"], s["N"], s["S_miss"], s["G_miss"], s["N_miss"], s["bytes"], s.get("parts") and
                                       {p: {k: v for k, v in c.items() if k not in ("hist", "decoded")} for p, c in s["parts"].items() if any(c.get(o) for o in ("S", "G", "N", "P", "D"))})
                    new = ("hist", "chunks", "rows_in", "rows_in_labelled", "decoded")
                    if any(k in s0 for k in new) or any(k in c for c in s0.get("parts", {}).values() for k in new) \
                            or set(s0["codec"]) != {"decode_ns", "decompress_ns", "chunks_decoded"} or lanes(s1) != lanes(s0):
                        bad.append(f"{name} {q} {s1['name']}: kvpair_stats off changed a count or left a new field in the response")
                for s1, s8 in zip(resp[1]["steps"], resp[8]["steps"]):
                    where = f"{name} {q} {s1['name']}"
                    nsteps += 1
                    bad += problems(where, s1)
                    for part, c in s1.get("parts", {}).items():
                        nparts += 1
                        bad += problems(f"{where} part {part}", c)
                    dec = lambda s: {k: v for k, v in (s.get("codec") or {}).items() if not k.endswith("_ns") and k != "chunks_decoded"}
                    for k, get in (("hist", lambda s: s.get("hist")), ("chunks", lambda s: s.get("chunks")),
                                   ("rows_in", lambda s: s.get("rows_in")), ("rows_in_labelled", lambda s: s.get("rows_in_labelled")),
                                   ("decode counts", dec),
                                   *((k, lambda s, k=k: s.get(k)) for k in step_record.ROUND_TRIPS),
                                   ("parts' decode counts", lambda s: {p: c.get("decoded") for p, c in s.get("parts", {}).items()})):
                        ncmp += 1
                        if get(s1) != get(s8):
                            bad.append(f"{where}: {k} differs between P=1 and P=8: {get(s1)} | {get(s8)}")
                    if {p: c.get("hist") for p, c in s1.get("parts", {}).items()} != {p: c.get("hist") for p, c in s8.get("parts", {}).items()}:
                        bad.append(f"{where}: the parts' histograms differ between P=1 and P=8")
                    # injections on a copy: one entry too many, and the top bucket's entries moved to a bucket whose
                    # lower edge is above everything the true histogram allows
                    for col, key in (("get_kvpair_hist", "get"), ("range_read_hist", "range_bytes")):
                        h = (s1.get("hist") or {}).get(key)
                        if not h or "u" in h:
                            continue
                        k0 = sorted(h, key=int)[-1]
                        more = {**s1, "hist": {**s1["hist"], key: {**h, k0: h[k0] + 1}}}
                        far = step_record.hist_bounds(step_record.hist_text(h))[1].bit_length() + 2
                        moved = {**s1, "hist": {**s1["hist"], key: {**{k: v for k, v in h.items() if k != k0},
                                                                    str(far): h[k0]}}}
                        if not problems(where, more) or (int(k0) and not problems(where, moved)):
                            bad.append(f"{where}: an injected wrong {col} was NOT caught")
                        caught += 1
        finally:
            srv.close()
    for b in bad[:20]:
        print("  " + b)
    print(f"step record: {len(stores)} stores x {len(QUERIES)} queries: {nsteps} steps and {nparts} part rows checked "
          f"(entries = operations, buckets bracket the lanes), {ncmp} values compared between P=1 and P=8, "
          f"{caught} steps with injections caught, {noff} plans equal with kvpair_stats off; {len(bad)} problem(s)")
    if bad or not nsteps or not nparts or not caught:
        raise SystemExit(1)
    print("STEP RECORD OK")


if __name__ == "__main__":
    main()
