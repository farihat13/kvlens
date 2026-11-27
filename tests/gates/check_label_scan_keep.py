#!/usr/bin/env python3
"""The label row scan with a keep set (read_vertices_by_label(label, props, keep)) on Per-type CSR and on ArangoDB
(its scan of a collection's documents), the two engine-shaped formats that hold label_rows_one_run: the C++ evaluator against its Python
twin, step by step -- rows and the S / G / N of the scan step.

Hand-built plans at sf0.1 (canonical), one anchor person (ic2's first param): the scan of Person with no keep (the
control), with keep = the anchor's friends listed twice (from a read_neighbors step), with keep = a literal list
holding a Post's vid (no row for it) and with an empty keep. The scan reads every chunk of the label whatever keep
holds, so every case must show the control's counts. A fifth case per format: the label enumerated and point-read
(read_vertex_props) must return the scan's rows, and the scan must be 1 SEEK, 0 GET and, on ArangoDB, one NEXT per row.

    python3 tests/gates/check_label_scan_keep.py
Each format must advertise label_rows_one_run in BOTH evaluators; the engine twin arangodb_engine must not (its one
legal entry_eq_scan body is the one with no requirement). The body each store runs, from the pick record
(pick_record.body_for): the microbench query entry_eq_scan runs its fused-scan body where whole_label_scan_by_default is
held (Per-type CSR); ArangoDB does NOT hold it (its scan is built, the default on the three whole-label scan queries is
held) and runs the enumerate + read body, while the person-run twin nhop2p_from_person_run, which needs only
label_rows_one_run, is legal there.
Exit 0 = every case's rows and scan counts equal in both
evaluators and across the cases. Fewer than 5 cases per format is a failure.
"""
import hashlib
import json
import subprocess
import sys
from kvevaluator.paths import ROOT
from kvevaluator.datasets import schema_path

EXP = ROOT
from kvevaluator.capture import binary
from kvevaluator.measure import plan_runs, server
from kvevaluator.measure.stores import ensure_store
from kvevaluator.formats import make_format
from kvevaluator.plans.plan import Step, ref
from kvevaluator.oracle.run import build_stores
from kvevaluator.oracle.runner import run_plan
from kvevaluator.kvstore.kvstore import CountingKV
from kvevaluator.schema.vids import vid
from kvevaluator.datasets import demo as data

SF, FMTS = "0.1", ("pertype_csr", "arangodb")
PROPS = ["firstName", "birthday", "id"]


def answer(rows):
    """A hash of the rows as a set: the scan's order is the layout's (docid order on ArangoDB), not part of the answer."""
    canon = sorted(json.dumps(r, sort_keys=True, default=str) for r in rows)
    return hashlib.sha1(json.dumps(canon).encode()).hexdigest()[:16]


def cases(p, a_post):
    scan = lambda **kw: Step("out", "read_vertices_by_label", label="Person", props=PROPS, **kw)
    friends = Step("friends", "read_neighbors", srcs=[p], etype="knows", dir="both")
    twice = Step("twice", "concat", a=ref("friends"), b=ref("friends"))                 # every vid twice in the keep set
    return [("no keep (the control)", [scan()], None),
            ("keep = the anchor's friends, each twice", [friends, twice, scan(keep=ref("twice"))], "friends"),
            ("keep = a literal list with a Post's vid", [scan(keep=[p, a_post, p])], 1),
            ("keep = an empty list", [scan(keep=[])], 0)]


def main():
    from kvevaluator.datasets.ldbc.params import param_rows
    p = vid("Person", param_rows(SF, "ic2")[0]["personId"])
    a_post = vid("Post", param_rows(SF, "is4")[0]["messageId"]) if param_rows(SF, "is4") else vid("Post", 1)
    schema = data.schema(SF)
    stores = build_stores(SF, list(FMTS), "abstract")
    total, bad = 0, []
    from kvevaluator.plan_choice.pick_record import body_for, legal_bodies
    from kvevaluator.formats.caps import caps_for, caps_resolver
    from kvevaluator.plan_choice.candidates import body_lacks
    CAP, DEFAULT = "label_rows_one_run", "whole_label_scan_by_default"
    plans = EXP / "python" / "kvevaluator" / "workloads" / "microbench" / "plans"
    bundle = json.load(open(plans / "entry_eq_scan.json"))
    # the person-run twin is a body of its base query's generated file (plan_choice/generate.py), found by its source
    base = json.load(open(plans / "nhop2p.json"))
    twin_body = next(n for n, m in base["bodies"].items() if any(x.startswith("nhop2p_from_person_run") for x in m["sources"]))
    fallback = next(n for n, m in bundle["bodies"].items() if not m.get("requires"))   # the body that needs no cap
    schema_json = schema_path("ldbc")

    def cpp_caps(fmt_name):
        cmd = [binary.binary_path(), "caps", "--schema", schema_json, "--format", fmt_name]
        if fmt_name == "arangodb_engine":          # the engine store holds named documents: the binary wants it said
            cmd += ["--codec-config", json.dumps({"props_codec": "named"})]
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
        got = json.loads(out.stdout)
        return set(got["capabilities"] if isinstance(got, dict) else got)

    # the engine twin runs the engine's own plans: it must not advertise the fair format's scan
    from kvevaluator.formats.arangodb_engine import ArangodbEngineFormat
    twin_caps, twin_cpp_caps = set(ArangodbEngineFormat.capabilities(schema)), cpp_caps("arangodb_engine")
    twin_py, twin_cpp = CAP in twin_caps, CAP in twin_cpp_caps
    twin_legal = legal_bodies(bundle, twin_cpp_caps, lambda label: twin_cpp_caps)
    print(f"[label scan keep] arangodb_engine holds {CAP}: python {twin_py}, cpp {twin_cpp}; entry_eq_scan's legal bodies {twin_legal}", flush=True)
    if twin_py or twin_cpp or twin_legal != [fallback]:
        bad.append(f"arangodb_engine: {CAP} python {twin_py} cpp {twin_cpp}, entry_eq_scan legal {twin_legal} (expected [{fallback!r}])")
    for FMT in FMTS:
        compared, control, scan_rows = 0, None, None
        caps, of_label = caps_for(FMT, schema), caps_resolver(FMT, schema)
        held_py, held_cpp = CAP in of_label("Person"), CAP in cpp_caps(FMT)
        default_scan = DEFAULT in caps
        want_default = FMT != "arangodb"            # ArangoDB's default on the whole-label scan queries is held
        selected = body_for("microbench", FMT, "entry_eq_scan", bundle, caps, of_label, SF)
        scans = lambda name: sum(1 for s in bundle["plans"][name]["steps"] if s["op"] == "read_vertices_by_label" and s.get("props"))
        twin_runs = (not body_lacks(base, twin_body, caps, of_label)
                     and bool(body_lacks(base, twin_body, caps - {CAP}, lambda label: of_label(label) - {CAP})))
        print(f"[label scan keep] {FMT} holds {CAP}: python {held_py}, cpp {held_cpp}; {DEFAULT}: python {default_scan}, cpp "
              f"{DEFAULT in cpp_caps(FMT)}; entry_eq_scan selects {selected!r} ({scans(selected)} label scan with props); nhop2p_from_person_run runs with {CAP} and not without: {twin_runs}", flush=True)
        if (not (held_py and held_cpp) or default_scan != want_default or (DEFAULT in cpp_caps(FMT)) != want_default
                or scans(selected) != (1 if want_default else 0) or not twin_runs):
            bad.append(f"{FMT}: {CAP} python {held_py} cpp {held_cpp}; {DEFAULT} held {default_scan} (expected {want_default}); "
                       f"entry_eq_scan runs {selected!r}; the twin runs by {CAP} alone: {twin_runs}")
        with server.CppServer(ensure_store("ldbc", SF, FMT, chunk=None), FMT) as srv:
            def both(steps):
                fmt = make_format(FMT, schema)
                py_out, py_kv = run_plan(steps, fmt, CountingKV(stores[FMT][1], sizeof=fmt.value_bytes, keysizeof=fmt.key_bytes))
                cpp_out, cpp_kv = plan_runs.run_steps(srv, steps, name="scan-keep")
                return py_out, py_kv, cpp_out, cpp_kv

            for name, steps, want in cases(p, a_post):
                py_out, py_kv, cpp_out, cpp_kv = both(steps)
                rows_py, rows_cpp = py_out["out"], cpp_out["out"]
                pa, ca = py_kv[-1][2], cpp_kv[-1][2]
                counts = (ca.S, ca.G, ca.N)
                diffs = []
                if (pa.S, pa.G, pa.N) != counts:
                    diffs.append(f"scan counts python {(pa.S, pa.G, pa.N)} vs cpp {counts}")
                if answer(rows_py) != answer(rows_cpp):
                    diffs.append(f"rows differ ({len(rows_py)} vs {len(rows_cpp)})")
                if control is None:
                    control, scan_rows, scan_value = counts, rows_cpp, ca.scan_value_bytes
                    if counts[:2] != (1, 0) or (FMT == "arangodb" and counts[2] != len(rows_cpp)):
                        diffs.append(f"the scan is not 1 SEEK, 0 GET{', one NEXT per row' if FMT == 'arangodb' else ''}: {counts}")
                elif counts != control:
                    diffs.append(f"keep moved the scan's counts: {counts} vs the control's {control}")
                n = len(set(py_out["friends"])) if want == "friends" else want
                if n is not None and len(rows_py) != n:
                    diffs.append(f"{len(rows_py)} rows, expected {n}")
                if want == "friends" and not n:
                    diffs.append("the anchor has no friend: the case proves nothing")
                print(f"[label scan keep] {FMT} {name}: {len(rows_py)} rows, scan S {counts[0]} G {counts[1]} N {counts[2]}"
                      f" -- {'DIFFER' if diffs else 'identical'}", flush=True)
                compared += 1
                bad += [f"{FMT} {name}: {x}" for x in diffs]
            # the scan against the point reads: the same rows, in both evaluators
            point = [Step("v", "read_vertices_by_label", label="Person"),
                     Step("out", "read_vertex_props", vids=ref("v"), props=PROPS, label="Person")]
            py_out, _py_kv, cpp_out, cpp_kv = both(point)
            same = answer(py_out["out"]) == answer(cpp_out["out"]) == answer(scan_rows) and len(scan_rows) > 0
            print(f"[label scan keep] {FMT} enumerate + point read against the scan: {len(cpp_out['out'])} rows"
                  f" -- {'identical' if same else 'DIFFER'}", flush=True)
            compared += 1
            if not same:
                bad.append(f"{FMT} the point reads' rows differ from the scan's ({len(cpp_out['out'])} vs {len(scan_rows)})")
            if FMT == "arangodb":
                # every crossed document's value is charged: the scan's value bytes == the point reads' value bytes
                # less one primary-index locator (12 bytes: collection u32 + docid u64) per vertex -- C++ bytes
                docs = cpp_kv[-1][2].get_value_bytes - 12 * len(scan_rows)
                print(f"[label scan keep] arangodb scan value bytes {scan_value} against the point reads' documents {docs}"
                      f" -- {'identical' if scan_value == docs else 'DIFFER'}", flush=True)
                if scan_value != docs or not docs:
                    bad.append(f"arangodb the scan's value bytes {scan_value} != the documents' bytes {docs}")
        if compared != 5:
            bad.append(f"{FMT}: {compared} cases compared, expected 5")
        total += compared
    for b in bad:
        print("DIFF " + b)
    if bad:
        print(f"FAIL ({total} cases compared)")
        return 1
    print(f"PASS: {total} cases on {len(FMTS)} formats, rows and the scan's S / G / N identical in C++ and Python, the same "
          f"counts with any keep, the scan's rows equal to the point reads'")
    return 0

if __name__ == "__main__":
    sys.exit(main())
