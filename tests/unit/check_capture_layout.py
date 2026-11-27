#!/usr/bin/env python3
"""capture_layout reads both layouts: a capture with the body named `variant` as a new one (`body`);
a header with both is refused; kvbench's header (its `variant` is a physical design) and a header with neither are
returned as they are; write_rows reads a writes capture file of either shape in the grid one (and refuses one in
neither); a renamed column (capture_columns.RENAMED_COLUMNS) reads by its new name and no writer emits its old one.

    python3 tests/unit/check_capture_layout.py
"""
import io
import os
import sys
import tempfile

from kvevaluator.capture import capture_layout as L

ok = True


N_CASES = [0]


def check(what, good, detail=""):
    global ok
    ok &= bool(good)
    N_CASES[0] += 1
    print(f"{'PASS' if good else 'FAIL'} {what}{': ' + detail if detail else ''}")


def rows(text):
    return list(L.reader(io.StringIO(text)))


check("layout 1 rows read as `body`", rows("query,variant,G\nis7,default,3\n") == [{"query": "is7", "body": "default", "G": "3"}])
check("layout 2 rows as they are", rows("query,body,G\nis7,default,3\n") == [{"query": "is7", "body": "default", "G": "3"}])
check("a header without either as it is", rows("cell,G\nx,1\n") == [{"cell": "x", "G": "1"}])
check("kvbench's header as it is (a design)", L.fix_header(["sf", "backend", "variant", "query"]) == ["sf", "backend", "variant", "query"])
try:
    rows("query,variant,body\nis7,a,b\n")
    check("a header with both is refused", False)
except ValueError:
    check("a header with both is refused", True)

# a writes capture file (write_rows): the old counts driver's columns read in the grid shape, a grid file as it is
OLD_COUNTS = ("sf,backend,variant,query,param_idx,db,S,G,N,P,D,write_key_bytes,write_value_bytes,write_bytes,S_miss,G_miss,"
              "N_miss,seek_miss_key_bytes,next_miss_key_bytes,get_miss_key_bytes,value_bytes,scan_key_bytes,get_bytes,"
              "get_key_bytes,total_bytes,rows,result_hash,payload_bytes\n"
              "0.1,cpp,canonical,iu1,2,meta,1,2,3,4,5,6,7,13,0,1,0,8,9,10,30,11,12,13,81,1,h,40\n")
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "props_split_adj_split.csv")
    with open(p, "w") as f:
        f.write(OLD_COUNTS)
    kind, header, (r,) = L.write_rows(p)
    want = {"cell": "props_split_adj_split", "body": "iu1", "ok": "1", "get_key": 13, "get_value": 12, "scan_key": 11,
            "scan_value": 18, "seek_miss_key": 8, "next_miss_key": 9, "get_miss_key": 10, "read_bytes": 81,
            "write_key": 6, "write_value": 7, "P": "4", "D": "5", "payload_bytes": "40"}
    check("an old counts file reads in the grid shape (scan_value = value_bytes - get_bytes; read_bytes = total_bytes)",
          kind == "counts" and header == L.write_columns("counts") and all(r[k] == v for k, v in want.items()),
          str({k: r[k] for k, v in want.items() if r[k] != v}) if kind == "counts" else kind)
    q = os.path.join(d, "parts.csv")
    with open(q, "w") as f:
        f.write("sf,workload,query,param_idx,db,step_idx,step,op,part,S,get_key_bytes,write_key_bytes,run\n"
                "0.1,writes,iu1,2,meta,0,s,insert_vertex,props:Person,1,2,3,r1\n")
    try:
        L.write_rows(q)
        check("an old parts file missing grid columns is refused", False)
    except ValueError as e:
        check("an old parts file missing grid columns is refused", "missing" in str(e))
    from kvevaluator.capture import step_record
    hist = set(step_record.HISTS) | set(step_record.FETCH) | set(step_record.DECODED)
    old_cols = [c for c in ["sf", "workload", "db", *[x if x in ("query", "param_idx", "step_idx", "step", "op", "part",
                "S", "G", "N", "P", "D", "S_miss", "G_miss", "N_miss") else x + "_bytes" for x in L.write_columns("parts")
                if x not in ("body", "run")]] if c not in hist and c[:-len("_bytes")] not in hist]
    h = os.path.join(d, "parts-before-hists.csv")
    with open(h, "w") as f:
        f.write(",".join(old_cols) + "\n" + ",".join("p:1" if c == "part" else "1" for c in old_cols) + "\n")
    kind, header, (r,) = L.write_rows(h)
    check("an old parts file from before the histograms reads with them empty",
          header == L.write_columns("parts")[:-1] and r["get_kvpair_hist"] == "" and r["get_key"] == "1",
          str([c for c in L.write_columns("parts") if c not in header]))
    try:
        L.write_rows(h, strict=True)
        check("... and strict (an upgrade in place) refuses it", False)
    except ValueError:
        check("... and strict (an upgrade in place) refuses it", True)
    g = os.path.join(d, "new.csv")
    with open(g, "w") as f:
        f.write(",".join(L.write_columns("counts")) + "\n" + ",".join("1" for _ in L.write_columns("counts")) + "\n")
    kind, header, (r,) = L.write_rows(g)
    check("a grid-shaped counts file reads as it is", header == L.write_columns("counts") and r["write_key"] == "1")
    with open(g, "w") as f:
        f.write("query,param_idx,cell,body\niu1,1,c,b\n")
    try:
        L.write_rows(g)
        check("a counts file in neither shape is refused", False)
    except ValueError:
        check("a counts file in neither shape is refused", True)

# Columns renamed (capture_columns.RENAMED_COLUMNS): a header with an old spelling reads by the new one;
# one with both is refused; the step record's shape hash is the same under either spelling (a rename re-measures
# nothing); and no writer emits an old spelling: no column list, and no string literal in the code that writes
# captures (python/, cpp/kvevaluator/src/) outside the map itself. The scan is checked on a planted literal first.
from kvevaluator.capture import capture_columns as CC, step_record as SR
from kvevaluator.paths import ROOT

old = list(CC.RENAMED_COLUMNS)
(r,) = rows(",".join(["query", *old]) + "\n" + ",".join(["q", *("1" for _ in old)]) + "\n")
check("old column spellings read by their new names", all(r.get(CC.RENAMED_COLUMNS[o]) == "1" and o not in r for o in old),
      str(sorted(r)))
try:
    rows("get_item_hist,get_kvpair_hist\n1,2\n")
    check("a header with an old and a new name of one column is refused", False)
except ValueError:
    check("a header with an old and a new name of one column is refused", True)
cols = SR.columns()
inv = {n: o for o, n in CC.RENAMED_COLUMNS.items()}
renamed_in_record = [c for c in cols if c in inv]
check("the step record's shape hash is the same under the old spellings",
      SR.columns_sha() == __import__("hashlib").sha256(",".join(inv.get(c, c) for c in cols if c not in CC.APPENDED_COLUMNS)
                                                       .encode()).hexdigest()[:12]
      and len(renamed_in_record) >= 4, f"{len(renamed_in_record)} renamed columns in the step record")
lists = {"RUN_COLS": CC.RUN_COLS, "WRITE_RUN_COLS": CC.WRITE_RUN_COLS, "STEP_COLS": CC.STEP_COLS,
         "PART_COLS": CC.PART_COLS, "WRITE_PART_COLS": CC.WRITE_PART_COLS, "step_record.columns()": cols,
         **{f"write_columns({k})": L.write_columns(k) for k in ("counts", "steps", "parts")}}
bad = [f"{n}: {c}" for n, cs in lists.items() for c in cs if c in CC.RENAMED_COLUMNS]
check("no capture column list holds an old spelling", not bad, f"{len(lists)} lists; {bad}")

# Columns appended later (capture_columns.APPENDED_COLUMNS): each is in the step record (or a writes capture's steps and
# parts) and hashes as absent, so the
# shape hash is that of the record without them (a capture written before stays fresh); control: the same column NOT
# listed changes the hash. A step file written before reads it as "" (not measured), never 0; a file of another kind
# (no column it follows) gains nothing. An accumulating file without it takes new rows (upsert) and is padded, not
# moved aside (aside_other_header); one that lacks any other column is still moved aside.
from kvevaluator.measure import kvstats_layout as KL

app = dict(CC.APPENDED_COLUMNS)
# the file kind an appended column belongs to: the step record (rt_levels), or a writes capture's steps and parts (the
# exact write units)
kind_of = {c: next((lst for lst in (cols, CC.WRITE_STEP_COLS) if c in lst), ()) for c in app}
check("every appended column is in a capture column list (the step record or a write capture's), after the column it names",
      all(c in lst and after in lst and lst.index(after) < lst.index(c) for c, (_, after) in app.items()
          for lst in (kind_of[c],)), str(app))
sha_with = SR.columns_sha()
CC.APPENDED_COLUMNS.clear()
sha_unlisted = SR.columns_sha()
CC.APPENDED_COLUMNS.update(app)
check("control: an appended column left unlisted changes the shape hash", sha_with != sha_unlisted and SR.columns_sha() == sha_with,
      f"{sha_with} listed, {sha_unlisted} unlisted")
before = [c for c in cols if c not in app]
got = {}
for c in app:
    lst = [x for x in kind_of[c] if x not in app]
    (r,) = rows(",".join(lst) + "\n" + ",".join("1" for _ in lst) + "\n")
    got[c] = (r.get(c), r.get("round_trips"))
check("a step file written before an appended column reads it as empty (not measured)",
      all(v == ("", "1") for v in got.values()), str(got))
check("a file of another kind gains no appended column", "rt_levels" not in rows("query,G\nis7,3\n")[0])
with tempfile.TemporaryDirectory() as t:
    p = os.path.join(t, "steps.csv")
    with open(p, "w", newline="") as f:
        f.write(",".join(["query", *before, "run"]) + "\r\n" + ",".join(["is1", *("1" for _ in before), "r0"]) + "\r\n")
    KL.upsert(p, [{"query": "is2", **{c: "2" for c in cols}}], ("query",), "r1", columns=["query", *cols])
    got = {r["query"]: r for r in L.read_rows(p)}
    check("upsert onto a file without the appended column: old rows read it empty, new rows carry it",
          got["is1"].get("rt_levels") == "" and got["is2"].get("rt_levels") == "2" and got["is1"]["round_trips"] == "1",
          str({q: got[q].get("rt_levels") for q in got}))
    p2 = os.path.join(t, "latency.csv")
    with open(p2, "w", newline="") as f:
        f.write(",".join(["query", *before, "run"]) + "\r\n" + ",".join(["is1", *("1" for _ in before), "r0"]) + "\r\n")
    aside = KL.aside_other_header(p2, ["query", *cols])
    (r2,) = L.read_rows(p2)
    check("aside_other_header pads an appended column, moves nothing aside",
          aside is None and r2.get("rt_levels") == "" and r2["round_trips"] == "1"
          and not any(".old-header-" in x for x in os.listdir(t)))
    with open(p2, "w", newline="") as f:
        f.write("query,G,run\r\nis1,1,r0\r\n")
    check("... and still moves aside a file that lacks another column",
          KL.aside_other_header(p2, ["query", *cols]) is not None)


def old_literals(text):
    """the old spellings quoted as a string literal in `text` (a writer's column name or a response key)"""
    return sorted({o for o in old for q in ('"', "'") if q + o + q in text})


check("the old-spelling scan catches a planted literal", old_literals('cols = ["S", "get_item_hist"]\n') == ["get_item_hist"])
hits, nfiles = [], 0
for base, exts in (("python", (".py",)), (os.path.join("cpp", "kvevaluator", "src"), (".cc", ".h"))):
    for dirpath, _, files in os.walk(os.path.join(str(ROOT), base)):
        for fn in files:
            path = os.path.join(dirpath, fn)
            if not fn.endswith(exts) or path.endswith(os.path.join("capture", "capture_columns.py")):
                continue
            nfiles += 1
            with open(path, errors="replace") as fh:
                hits += [f"{os.path.relpath(path, str(ROOT))}: {o}" for o in old_literals(fh.read())]
check("no writer emits an old spelling (string literals in python/ and cpp/kvevaluator/src/)", nfiles > 100 and not hits,
      f"{nfiles} files; {hits}")

# A plan row from its step rows (step_record.plan_fields, what cpplatency writes as __plan__): the round-trip counts sum,
# rt_levels (per step, text) stays empty; a step with a text rt_levels once made the sum raise
steps = [{c: "" for c in cols} for _ in range(2)]
for s, (rt, crit, text) in zip(steps, ((3, 2, "0:2 1:0"), (1, 1, "4:0"))):
    s.update(round_trips=str(rt), round_trips_critical=str(crit), rt_levels=text, G="1")
try:
    pf = SR.plan_fields(steps)
    check("a plan row sums the round trips and leaves rt_levels empty",
          pf.get("round_trips") == 4 and pf.get("round_trips_critical") == 3 and pf.get("rt_levels") == "",
          str({c: pf.get(c) for c in ("round_trips", "round_trips_critical", "rt_levels")}))
except ValueError as e:
    check("a plan row sums the round trips and leaves rt_levels empty", False, f"raised {e}")

print(f"check_capture_layout: {'OK' if ok else 'FAILED'} ({N_CASES[0]} cases)")
sys.exit(0 if ok else 1)
