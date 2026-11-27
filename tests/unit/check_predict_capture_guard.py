#!/usr/bin/env python3
"""A predicted capture is one identity, as a measured cell is (grid_bodies.write_cell): `predict --plans` into a capture
made from other inputs is refused, never partly replaced. Calls on one fixture (synthetic statistics, a spec-only tally,
two requests, so every step is not estimated and a call that writes exits 1): the same call again writes; another request set, another tally and another statistics file are refused
(rc 2, the moved input named); --fresh replaces and names what moved; one dir for both levels is refused. The anchor
level runs only with --anchor-stats: without them only the population level is written, and --out-predicted-anchor is
refused; the population record never names the anchor statistics (it never reads them), so one
that does is still reused, while the anchor level with other anchor statistics is refused. The
statistics and the tally must name one record stream (its line count, its freshness key): another is refused, and a run's
statistics are rebuilt once their stream's key moves (build_stats.stale). Under a second.

Run:  python3 tests/unit/check_predict_capture_guard.py
"""
import json
import os
import subprocess
import sys
import tempfile

from check_predict_stats_schema import SCHEMA, complete_stats

from kvevaluator.measure import predict_requests
from kvevaluator.paths import INPUTS_DIR, cpp_bin

CELL = "props_split_adj_split"


def main():
    h = json.load(open(SCHEMA))["hash"]
    fails, cases = [], 0
    with tempfile.TemporaryDirectory() as t:
        json.dump(complete_stats(h), open(f"{t}/stats.json", "w"))
        other = complete_stats(h)
        other["ndv"] = {k: v + 1 for k, v in other["ndv"].items()}   # another file that still agrees with its tier 1
        json.dump(other, open(f"{t}/stats-other.json", "w"))
        json.dump({"anchor_degrees": {}, "anchor_recency": {}, "records": {"path": "r.jsonl", "lines": 100},
                   "schema_hash": h}, open(f"{t}/anchor.json", "w"))
        for d in (f"stores/ldbc/sf0.1/meta-{CELL}", "tally", "req"):
            os.makedirs(f"{t}/{d}")
        spec = json.load(open(os.path.join(str(INPUTS_DIR), "specs", "grid", f"{CELL}.json")))
        tally = f"{t}/tally/meta-{CELL}.json"
        json.dump({"spec": spec}, open(tally, "w"))
        predict_requests.main(["--family", "interactive", "--sf", "0.1", "--cells", CELL, "--queries",
                               "ic2@index_walk,ic2@two_phase", "--nparam", "1", "--store-root", f"{t}/stores",
                               "--out", f"{t}/req"])
        reqs = sorted(f"{t}/req/{f}" for f in os.listdir(f"{t}/req"))

        def call(requests, stats="stats.json", fresh=False, dirs=("cap/p", "cap/a"), anchor="anchor.json"):
            # anchor None: no --anchor-stats; a dir None: that level's --out-... not given
            r = subprocess.run([str(cpp_bin()), "predict", "--plans", "--stats", f"{t}/{stats}",
                                *(["--anchor-stats", f"{t}/{anchor}"] if anchor else []),
                                "--tally-dir", f"{t}/tally", "--schema", SCHEMA,
                                *(["--out-predicted", f"{t}/{dirs[0]}"] if dirs[0] else []),
                                *(["--out-predicted-anchor", f"{t}/{dirs[1]}"] if dirs[1] else []),
                                *(["--fresh"] if fresh else []), *requests], capture_output=True, text=True)
            return r.returncode, r.stderr

        def case(what, got, want_rc, want_word=None):
            nonlocal cases
            cases += 1
            rc, err = got
            ok = rc in want_rc and (want_word is None or want_word in err)
            print(f"  {'ok  ' if ok else 'FAIL'} {what}: rc {rc} (want {'/'.join(map(str, want_rc))}"
                  + (f", naming {want_word!r}" if want_word else "") + ")" + ("" if ok else f": {err.strip()[-300:]}"))
            if not ok:
                fails.append(what)

        WRITES, REFUSED = (0, 1), (2,)      # 1: written, its steps not estimated (the spec-only tally)
        case("the first call writes", call(reqs), WRITES)
        case("the same call again writes", call(reqs), WRITES)
        case("another request set (one body of two) is refused", call(reqs[:1]), REFUSED, "requests")
        json.dump({"spec": spec, "note": "another tally"}, open(tally, "w"))
        case("another tally is refused", call(reqs), REFUSED, "tally")
        json.dump({"spec": spec}, open(tally, "w"))
        case("other statistics are refused", call(reqs, stats="stats-other.json"), REFUSED, "stats")
        case("--fresh replaces, naming what moved", call(reqs[:1], fresh=True), WRITES, "replacing (--fresh)")
        rec = json.load(open(f"{t}/cap/p/counts/{CELL}.json"))
        n = rec.get("inputs", {}).get("requests", {}).get("count")
        case("the replaced record names its request set", (0 if n == 1 else 1, f"requests.count {n}"), (0,))
        case("one dir for both levels is refused", call(reqs, dirs=("cap/x", "cap/x")), REFUSED, "one dir")

        # the anchor level only with --anchor-stats; the population record never names them
        def anchor_field(d):   # a missing record counts as naming them: the case fails
            rec = f"{t}/{d}/counts/{CELL}.json"
            return not os.path.exists(rec) or "anchor_stats" in json.load(open(rec)).get("inputs", {})
        case("with --anchor-stats the population record names no anchor statistics",
             (1 if anchor_field("cap/p") else 0, "inputs.anchor_stats in cap/p") if anchor_field("cap/a") else
             (1, "the anchor record names none"), (0,))
        case("no --anchor-stats: the population level alone writes", call(reqs, dirs=("cap/plain", None), anchor=None),
             WRITES, "no anchor level")
        case("... and its record names no anchor statistics, no anchor capture",
             (1 if anchor_field("cap/plain") or os.path.exists(f"{t}/cap/a-plain") else 0, "cap/plain"), (0,))
        case("--out-predicted-anchor without --anchor-stats is refused",
             call(reqs, dirs=("cap/plain", "cap/plain-a"), anchor=None), REFUSED, "needs --anchor-stats")
        rec_path = f"{t}/cap/plain/counts/{CELL}.json"
        rec = json.load(open(rec_path))
        rec["inputs"]["anchor_stats"] = {"path": "old.json", "sha256": "0" * 64}   # a record that names the anchor statistics
        json.dump(rec, open(rec_path, "w"))
        case("a population capture whose record names anchor statistics is reused",
             call(reqs, dirs=("cap/plain", None), anchor=None), WRITES)
        json.dump({"anchor_degrees": {"x": {}}, "anchor_recency": {}, "records": {"path": "r.jsonl", "lines": 100},
                   "schema_hash": h}, open(f"{t}/anchor-other.json", "w"))
        case("the anchor level with other anchor statistics is refused",
             call(reqs[:1], dirs=(None, "cap/a"), anchor="anchor-other.json"), REFUSED, "anchor statistics")
        case("... while the population level takes other anchor statistics",
             call(reqs[:1], dirs=("cap/p", None), anchor="anchor-other.json"), WRITES)   # --fresh above: one request

        # the statistics and the tally name one record stream (util/record_stream.h); each case its own capture dirs
        def with_streams(name, stats_records, tally_records):
            st = complete_stats(h)
            st["records"] = stats_records
            json.dump(st, open(f"{t}/stats-{name}.json", "w"))
            json.dump({"anchor_degrees": {}, "anchor_recency": {}, "records": stats_records, "schema_hash": h},
                      open(f"{t}/anchor-{name}.json", "w"))      # graph-stats writes one records into both files
            json.dump({"spec": spec, "records": tally_records}, open(tally, "w"))
            return call(reqs, stats=f"stats-{name}.json", dirs=(f"cap/{name}-p", f"cap/{name}-a"),
                        anchor=f"anchor-{name}.json")
        r100 = {"path": "r.jsonl", "lines": 100}
        case("one stream (lines and key equal) writes",
             with_streams("same", {**r100, "key_sha256": "aa"}, {**r100, "key_sha256": "aa"}), WRITES)
        case("another line count is refused", with_streams("lines", r100, {**r100, "lines": 99}), REFUSED,
             "different record streams")
        case("another freshness key is refused",
             with_streams("key", {**r100, "key_sha256": "aa"}, {**r100, "key_sha256": "bb"}), REFUSED, "freshness keys")
        case("a key on one side only is refused", with_streams("onekey", r100, {**r100, "key_sha256": "aa"}), REFUSED,
             "make it again")
        case("another freshness key from the same sources (a re-dump by another commit) writes",
             with_streams("sources", {**r100, "key_sha256": "aa", "sources_sha256": "ss"},
                          {**r100, "key_sha256": "bb", "sources_sha256": "ss"}), WRITES)
        case("other sources are refused",
             with_streams("othersrc", {**r100, "key_sha256": "aa", "sources_sha256": "ss"},
                          {**r100, "key_sha256": "aa", "sources_sha256": "tt"}), REFUSED, "other sources")
        case("other sources of the same bytes (a re-dump after a code edit) writes",
             with_streams("samebytes", {**r100, "key_sha256": "aa", "sources_sha256": "ss", "content_sha256": "cc"},
                          {**r100, "key_sha256": "bb", "sources_sha256": "tt", "content_sha256": "cc"}), WRITES)
        case("other bytes from the same sources are refused",
             with_streams("otherbytes", {**r100, "key_sha256": "aa", "sources_sha256": "ss", "content_sha256": "cc"},
                          {**r100, "key_sha256": "aa", "sources_sha256": "ss", "content_sha256": "dd"}), REFUSED,
             "other content")
        json.dump({"spec": spec}, open(tally, "w"))

        # a run rebuilds its own statistics when their stream's key moved (build_stats.stale)
        from kvevaluator.predict import build_stats as BS
        open(f"{t}/r.jsonl", "w").write("{}\n")
        open(f"{t}/r.jsonl.key.json", "w").write('{"v": 1}')
        import hashlib
        st = complete_stats(h)
        st["records"] = {"path": f"{t}/r.jsonl", "lines": 1,
                         "key_sha256": hashlib.sha256(b'{"v": 1}').hexdigest()}
        st["provenance"] = {**st.get("provenance", {}), **BS.build_key("ldbc")}   # made by a build now
        json.dump(st, open(f"{t}/stats-run.json", "w"))
        fresh_now = BS.stale(f"{t}/stats-run.json", f"{t}/r.jsonl", "ldbc")
        open(f"{t}/r.jsonl.key.json", "w").write('{"v": 2}')
        moved = BS.stale(f"{t}/stats-run.json", f"{t}/r.jsonl", "ldbc")
        case("build_stats.stale: None for the stream's own statistics, a reason once its key moves",
             (0 if fresh_now is None and moved and "another dump" in moved else 1, f"{fresh_now!r} / {moved!r}"), (0,))
        open(f"{t}/r.jsonl.key.json", "w").write('{"v": 1}')
        kept_now = BS.kept(f"{t}/stats-run.json", f"{t}/r.jsonl", "ldbc")
        open(f"{t}/r.jsonl.key.json", "w").write('{"v": 2}')
        kept_moved = BS.kept(f"{t}/stats-run.json", f"{t}/r.jsonl", "ldbc")
        kept_missing = BS.kept(f"{t}/none.json", f"{t}/r.jsonl", "ldbc")
        case("build_stats --if-stale: keeps the stream's own statistics, builds stale or missing ones",
             (0 if (kept_now, kept_moved, kept_missing) == (True, False, False) else 1,
              f"{kept_now} {kept_moved} {kept_missing}"), (0,))
    ok = not fails
    print(f"predict capture guard {'PASS' if ok else 'FAIL'}: {cases} cases, {len(fails)} failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
