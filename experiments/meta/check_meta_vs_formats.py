#!/usr/bin/env python3
"""meta against the separate engine formats: the same answers everywhere, and at each engine-shaped corner the same
work wherever no listed difference allows otherwise.

Two modes, LDBC IC/IS (`--family interactive`, the C++ binary driven directly on real substitution params) or the writes
workload (`--family writes`: 12 write primitives + IU1-IU8, through grid_writes in the shadow overlay):
  --corner E          meta at corners/corner_E.json vs the format E (dgraph / nebula / pertype_csr). Per
                      (query, param): answers equal; and per step the ops (S / G / N, + P / D for writes) and, apart,
                      the value bytes equal (key bytes are meta's own), except for the queries corners/expected_diffs.json
                      lists for E under `ops` / `bytes`, each with its reason. A difference outside the list fails.
  --spec FILE --format F --answers-only
                      any spec (e.g. a grid cell) vs the format F: answers only (a layout must not change an answer).
The corners are engine-shaped full specs (inputs/specs/corners/); their work differs from the engines' by the listed,
predicted rows.

  python3 experiments/meta/check_meta_vs_formats.py --corner nebula [--family interactive|writes] [--sf 0.1] [--nparam 3]
  python3 experiments/meta/check_meta_vs_formats.py --spec inputs/specs/grid/props_split_adj_split.json \
      --format nebula --answers-only
Exit 1 on any answer mismatch, an unlisted difference, or 0 cells compared.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

from kvevaluator.capture import binary, store_identity, capture_layout
from kvevaluator.measure import plan_runs, records, stores, store_aside
from kvevaluator.formats import pertype_csr
from kvevaluator.formats.props_codec import props_codec
from kvevaluator.datasets import schema_path
from kvevaluator.output_tree import Tree
from kvevaluator.datasets.ldbc.params import query_params
from kvevaluator.paths import ROOT

EXP = str(ROOT)
SCHEMA = schema_path("ldbc")
CORNERS = os.path.join(EXP, "inputs", "specs", "corners")
BIN = binary.binary_path()
QUERIES = ["ic1", "ic2", "ic3", "ic4", "ic5", "ic6", "ic7", "ic8", "ic9", "ic10", "ic11", "ic12",
           "is1", "is2", "is3", "is4", "is5", "is6", "is7"]
# each engine format's environment, for the reads store and the writes run alike. pertype_csr's reference is the
# reported store at 64 KB (far label on): the corner packs at 64 KB with meta's own chunk and the far label, so rows and
# ops match where expected_diffs.json says, and bytes differ by the chunk encoding (codec/lists.h vs codec/chunks.h)
FORMAT_ENV = {"pertype_csr": {"KVEVAL_PERTYPE_CSR_CHUNK": "65536"}}
WRITE_OPS = ("S", "G", "N", "P", "D")
WRITE_BYTES = ("write_value",)


def expected(workload, fmt):
    """{"ops": (queries allowed to differ, reason), "bytes": (...)} for a corner; '*' = every query"""
    with open(os.path.join(CORNERS, "expected_diffs.json")) as f:
        e = json.load(f).get(workload, {}).get(fmt, {})
    return {lane: (set(e[lane]["queries"]), e[lane]["why"]) if lane in e else (set(), "") for lane in ("ops", "bytes")}


def allows(allowed, lane, q):
    qs = allowed[lane][0]
    return q in qs or "*" in qs


def far_label(fmt):
    """the far-end label setting FORMAT_ENV gives an engine format that has one ("on" / "off"), else None"""
    if fmt == "meta" or not stores.far_label_of(fmt):
        return None
    return FORMAT_ENV.get(fmt, {}).get("KVEVAL_PERTYPE_FAR_LABEL", pertype_csr.PERTYPE_FAR_LABEL_DEFAULT)


def ensure_store(sf, out_dir, fmt, spec, tag):
    """Ingest one store if it is not built (store_identity.ingest_needed: a half-built dir moves aside); return its dir.
    spec=None for an engine format."""
    store = os.path.join(out_dir, tag)
    far = far_label(fmt)
    if not store_identity.ingest_needed(store):
        if far is not None:   # a store of the other arm under this name is never reused
            have = json.load(open(os.path.join(store, "manifest.json"))).get("value_codec", {}).get("far_label", True)
            if ("on" if have else "off") != far:
                raise SystemExit(f"{store}: built with far_label={have}, the check needs {far} -- move it aside and re-run")
        if fmt in store_identity.PROPS_CODEC_FORMATS:
            store_identity.require_props_codec(store, props_codec())
        if spec is not None:   # a spec edited under the same tag must never reuse the old store
            have = json.load(open(os.path.join(store, "manifest.json"))).get("value_codec")
            if have != stores.canonical_meta_spec(spec, SCHEMA):
                raise SystemExit(f"{store}: built from another spec -- move it aside and re-run")
        return store
    os.makedirs(out_dir, exist_ok=True)
    cmd = [BIN, "ingest", "--schema", SCHEMA, "--format", fmt, "--out", store, "--scale", str(sf)]
    if fmt in store_identity.PROPS_CODEC_FORMATS:
        cmd += ["--props-codec", props_codec()]
    if far is not None:
        cmd += ["--far-label", far]
    if spec is not None:
        spec_file = os.path.join(out_dir, f".{tag}.spec.json")
        with open(spec_file, "w") as f:
            json.dump(spec, f)
        cmd += ["--spec", spec_file]
    print(f"  ingest {tag} ...", file=sys.stderr)
    with open(records.records_file("ldbc", sf)) as rin, store_aside.replacing(store):
        subprocess.run(cmd, stdin=rin, stdout=subprocess.DEVNULL, check=True)
    return store


def run_query(store, fmt, ir, params, P=1, scale=None):
    req = {"store": {"dir": store, "schema": SCHEMA, "format": fmt, **({"scale": str(scale)} if scale else {})},
           "plan": ir, "params": params, "run": {"P": P}}
    r = subprocess.run([BIN, "run", "-"], input=json.dumps(req), capture_output=True, text=True)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        raise RuntimeError(msg[-1] if msg else "nonzero exit")
    return json.loads(r.stdout)


def read_steps(resp):
    """per read step: (op counts, value bytes) -- compared apart (key bytes are meta's own key family)"""
    steps = [s for s in resp.get("steps", []) if str(s.get("op", "")).startswith("read")]
    ops = [(s.get("op"), s.get("S"), s.get("G"), s.get("N")) for s in steps]
    vb = [(s.get("bytes", {}).get("get_value"), s.get("bytes", {}).get("scan_value")) for s in steps]
    return ops, vb


def check_ldbc(a, spec, tag, fmt, allowed):
    out_dir = str(Tree().check("meta-vs-formats", "ldbc", a.sf))   # the check's own stores, apart from the shared ones
    meta_store = ensure_store(a.sf, out_dir, "meta", spec, f"meta-{tag}")
    ref_store = ensure_store(a.sf, out_dir, fmt, None,
                             fmt + ("-nofarlabel" if far_label(fmt) == "off" else "") + store_identity.props_codec_tag(fmt))
    rows_bad, ops_bad, listed, cells, exact = [], [], {"ops": set(), "bytes": set()}, 0, 0
    for q in QUERIES:
        q_rows_ok = q_ops_ok = True
        # one body on both sides: the format's (the pick record's, plan_runs.ir_to_run): a corner reproduces the format,
        # so its work differs only by the layout, never by a plan choice
        ir = plan_runs.ir_to_run("interactive", q, fmt, ref_store, scale=a.sf)
        for idx, p in enumerate(query_params(a.sf, q, a.nparam), 1):
            cells += 1
            try:
                m, d = run_query(meta_store, "meta", ir, p, a.P, a.sf), run_query(ref_store, fmt, ir, p, a.P, a.sf)
            except RuntimeError as e:
                rows_bad.append((q, idx, f"ERROR: {e}")); q_rows_ok = False; continue
            if m.get("rows") != d.get("rows"):
                rows_bad.append((q, idx, "rows differ")); q_rows_ok = False
            if a.answers_only:
                continue
            (mo, mb), (do, db) = read_steps(m), read_steps(d)
            if mo == do and mb == db:
                exact += 1
            for lane, same in (("ops", mo == do), ("bytes", mb == db)):
                if same:
                    continue
                if allows(allowed, lane, q):
                    listed[lane].add(q)
                else:
                    ops_bad.append((q, idx, lane)); q_ops_ok = False
        mark = "ok" if (q_rows_ok and q_ops_ok) else ("rows" if not q_rows_ok else "WORK")
        tags = [lane for lane in ("ops", "bytes") if q in listed[lane]]
        print(f"  {q:5} {mark:5} {'listed: ' + ', '.join(tags) if tags else ''}")
    print(f"\ncells={cells}  rows-mismatch={len(rows_bad)}"
          + ("" if a.answers_only else f"  work exact={exact}/{cells}  listed ops-diff={sorted(listed['ops'])}  "
                                       f"listed bytes-diff={len(listed['bytes'])} queries  unlisted-diff={len(ops_bad)}"))
    if rows_bad:
        print("ROWS/ERROR:", rows_bad[:10])
    if ops_bad:
        print("UNLISTED DIFF:", ops_bad[:10])
    for lane in ("ops", "bytes"):
        stale = sorted(allowed[lane][0] - listed[lane] - {"*"})
        if stale or ("*" in allowed[lane][0] and not listed[lane]):   # listed but no difference: the list is stale
            print(f"LISTED, DID NOT DIFFER ({lane}):", stale or "* (no query differed)")
    return cells, bool(rows_bad or ops_bad)


def grid_writes(sf, nparam, sel, env_extra, out):
    """{(query, param): counts row} of one write run of grid_writes into its own output tree `out`: `sel` names the
    store (--spec-dir D --cells C for a spec, --format F for a named format)"""
    import glob
    env = dict(os.environ, **env_extra)
    cmd = [sys.executable, "-m", "kvevaluator.measure.grid_writes", "--sf", sf, "--nparam", str(nparam), "--fresh",
           "--root", out] + sel
    r = subprocess.run(cmd, cwd=EXP, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"grid_writes {' '.join(sel)} failed:\n{(r.stderr or r.stdout)[-2000:]}")
    files = glob.glob(os.path.join(out, "**", "counts", "*.csv"), recursive=True)
    if len(files) != 1:
        raise SystemExit(f"grid_writes {' '.join(sel)}: {len(files)} counts files under {out}, one expected")
    return {(row["query"], row["param_idx"]): row for row in capture_layout.write_rows(files[0])[2]}


def check_writes(a, spec_path, fmt, allowed):
    with tempfile.TemporaryDirectory() as tmp:
        spec_dir, cell = os.path.dirname(os.path.abspath(spec_path)), os.path.basename(spec_path)[:-len(".json")]
        m = grid_writes(a.sf, a.nparam, ["--spec-dir", spec_dir, "--cells", cell], {}, os.path.join(tmp, "meta"))
        r = grid_writes(a.sf, a.nparam, ["--format", fmt], FORMAT_ENV.get(fmt, {}), os.path.join(tmp, fmt))
    keys = sorted(set(m) | set(r))
    missing = [k for k in keys if k not in m or k not in r]
    both = [k for k in keys if k in m and k in r]
    rows_bad = [k for k in both if m[k].get("result_hash") != r[k].get("result_hash")]
    diffs, unlisted, listed = [], [], {"ops": set(), "bytes": set()}
    for k in ([] if a.answers_only else both):
        for lane, cols in (("ops", WRITE_OPS), ("bytes", WRITE_BYTES)):
            d = {c: (m[k][c], r[k][c]) for c in cols if m[k][c] != r[k][c]}
            if not d:
                continue
            diffs.append((k, d))
            if allows(allowed, lane, k[0]):
                listed[lane].add(k[0])
            else:
                unlisted.append((k, d))
    exact = len(both) - len({k for k, _ in diffs})
    print(f"  {len(both)}/{len(keys)} (query, param) cells on both sides; answers differ in {len(rows_bad)}"
          + ("" if a.answers_only else f"; work exact {exact}, listed ops-diff {sorted(listed['ops'])}, listed "
                                       f"bytes-diff {len(listed['bytes'])} queries, unlisted {len(unlisted)}")
          + (f"; MISSING {missing[:5]}" if missing else ""))
    for k, d in (unlisted or diffs)[:6]:
        print(f"    {k}: " + ", ".join(f"{c} meta {mv} vs {fmt} {rv}" for c, (mv, rv) in d.items()))
    for k in rows_bad[:6]:
        print(f"    {k}: answer differs")
    for lane in ("ops", "bytes"):
        stale = sorted(allowed[lane][0] - listed[lane] - {"*"})
        if stale or ("*" in allowed[lane][0] and not listed[lane] and not a.answers_only):
            print(f"  LISTED, DID NOT DIFFER ({lane}):", stale or "* (no query differed)")
    return len(both), bool(missing or rows_bad or unlisted)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--corner", choices=("dgraph", "nebula", "pertype_csr"),
                    help="meta at corners/corner_<E>.json vs the format E")
    ap.add_argument("--spec", help="any meta spec file (with --format and --answers-only)")
    ap.add_argument("--format", help="the engine format to compare with (--spec mode)")
    ap.add_argument("--answers-only", action="store_true", help="compare the answers only")
    ap.add_argument("--family", dest="workload", default="interactive", choices=("interactive", "writes"))
    ap.add_argument("--sf", default=os.environ.get("SF", "0.1"))
    ap.add_argument("--nparam", type=int, default=3)
    ap.add_argument("--P", type=int, default=1, help="worker threads (the logical counts are P-independent)")
    a = ap.parse_args()
    if bool(a.corner) == bool(a.spec):
        raise SystemExit("give --corner E, or --spec FILE --format F --answers-only")
    if a.spec and not (a.format and a.answers_only):
        raise SystemExit("--spec compares answers only: give --format F --answers-only")
    spec_path = os.path.abspath(a.spec) if a.spec else os.path.join(CORNERS, f"corner_{a.corner}.json")
    fmt = a.format or a.corner
    spec = json.load(open(spec_path))
    tag = os.path.splitext(os.path.basename(spec_path))[0]
    allowed = {"ops": (set(), ""), "bytes": (set(), "")} if a.answers_only else expected(a.workload, fmt)
    print(f"\nmeta[{tag}] vs {fmt} -- {a.workload}, sf{a.sf}, {a.nparam} params/query, P={a.P}, "
          f"props_codec={props_codec()}" + (", answers only" if a.answers_only else "")
          + "".join(f"\n  listed {lane} differences: {sorted(qs)} -- {why}" for lane, (qs, why) in allowed.items() if qs)
          + "\n")
    if a.workload == "interactive":
        cells, fail = check_ldbc(a, spec, tag, fmt, allowed)
    else:
        cells, fail = check_writes(a, spec_path, fmt, allowed)
    fail = fail or cells == 0
    print("RESULT:", "FAIL" if fail else f"PASS ({cells} cells: answers match"
          + ("" if a.answers_only else "; work matches outside the listed differences") + ")")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
