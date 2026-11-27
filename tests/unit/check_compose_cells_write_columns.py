#!/usr/bin/env python3
"""compose_cells --check reads a measured writes file of the older shape (lanes named <lane>_bytes, `db`, no `body`)
through capture_layout.write_rows, as compose_writes reads its sources, so it compares like with like: a write row is
compared and found equal, a planted difference is found, and the bill's write runs are compared. Synthetic files in a
temp dir, no data, no store. Under a second.

    python3 tests/unit/check_compose_cells_write_columns.py

Cases, on a composed root (<root>/writes/reported/, output_tree):
  1. an old-shape measured write parts file against the same rows in the grid shape: every row compared, 0 differ;
  2. a planted difference (one written value byte) is found;
  3. --bill's write runs: compared, the three forms equal;
  4. a composed writes counts row carries its parts' last-chunk sums (props parts from the props source, adjacency
     parts from the adjacency source), as it carries their lanes;
  5. a planted difference in the exact write units (put_units_1k) is found.
Exit 1 when a case goes the wrong way. Without the reader (the measured file read by name), the check either stopped
with KeyError 'get_key_bytes' (its columns taken from the measured header) or compared the op counts alone and missed
the planted byte difference (its columns taken from the grid's names): case 2 fails. Without the write units among the
compared columns, case 5 fails. Without the last-chunk columns
among compose_writes' summed ones, a composed counts row leaves them blank, which the half chunk view refuses: case 4
fails."""
import csv
import os
import sys
import tempfile

from kvcost.layout_cost import compose_cells as CC
from kvevaluator.capture import capture_columns, capture_layout, step_record
from kvevaluator.output_tree import writes_in

CELL = "props_split_adj_condensed"
# the grid shape (capture_layout.write_columns), and the older shape it is read from: `sf`, `workload` and `db` beside
# the keys, no `body`, each byte lane named <lane>_bytes, the renamed columns by their old names (capture_columns)
NEW = capture_layout.write_columns("parts")
_LANES = set(step_record.READ_BYTE_LANES) | set(step_record.WRITE_BYTE_LANES)
_OLD_NAME = {new: old for old, new in capture_columns.RENAMED_COLUMNS.items()}
OLD = ["sf", "workload", "db"] + [f"{c}_bytes" if c in _LANES else _OLD_NAME.get(c, c) for c in NEW if c != "body"]
# two write runs (two params of one update), two parts each: an update of a property, a read of a list it rewrites
ROWS = [dict(query="w_upd", param_idx=str(p), step_idx=str(i), step=s, op=op, part=part, S=S, G=G, N="0", P=P, D="0",
             get_key=gk, get_value=gv, write_key=wk, write_value=wv, get_hist=gh, put_hist=ph, get_fetch=gf, put_units="1")
        for p in (1, 2)
        for i, s, op, part, S, G, P, gk, gv, wk, wv, gh, ph, gf in (
            (0, "out", "update_prop", "props:Person", "0", "0", "1", "0", "0", "11", "40", "0", "6:1", "0"),
            (1, "nb", "read_neighbors", "adj:Person:knows:out", "1", "1", "1", "9", "900", "9", "950", "10:1", "10:1", "1"))]


def old_row(r):
    return {**dict.fromkeys(OLD, "0"), "sf": "3", "workload": "w", "db": "meta", "run": "t", "query": r["query"],
            "param_idx": r["param_idx"], "step_idx": r["step_idx"], "step": r["step"], "op": r["op"], "part": r["part"],
            "S": r["S"], "G": r["G"], "N": r["N"], "P": r["P"], "D": r["D"], "get_key_bytes": r["get_key"],
            "get_value_bytes": r["get_value"], "write_key_bytes": r["write_key"], "write_value_bytes": r["write_value"],
            "get_item_hist": r["get_hist"], "range_read_hist": "0", "put_item_hist": r["put_hist"],
            "del_item_hist": "", "get_fetch_4k": r["get_fetch"], "range_fetch_4k": "0", "keys_per_range_read": "",
            "put_units_1k": r["put_units"], "del_units_1k": "0"}


def new_row(r):
    return {**dict.fromkeys(NEW, "0"), "run": "t", "body": r["query"], "query": r["query"], "param_idx": r["param_idx"],
            "step_idx": r["step_idx"], "step": r["step"], "op": r["op"], "part": r["part"], "S": r["S"], "G": r["G"],
            "N": r["N"], "P": r["P"], "D": r["D"], "get_key": r["get_key"], "get_value": r["get_value"],
            "write_key": r["write_key"], "write_value": r["write_value"], "get_kvpair_hist": r["get_hist"],
            "range_read_hist": "0", "put_kvpair_hist": r["put_hist"], "del_kvpair_hist": "",
            "get_fetch_4k": r["get_fetch"], "range_fetch_4k": "0", "keys_per_range_read": "",
            "put_units_1k": r["put_units"], "del_units_1k": "0"}


def put(path, header, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


BAD = []


def check(name, ok, got=None):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f": got {got!r}"))
    if not ok:
        BAD.append(name)


def cases(tag, grid, out):
    """cases 1, 2, 3 and 5 on one grid root"""
    w = writes_in(grid, "ldbc", None)
    put(w.path_of("parts", CELL), OLD, [old_row(r) for r in ROWS])
    put(writes_in(out, "ldbc", None).path_of("parts", CELL), NEW, [new_row(r) for r in ROWS])
    # 1
    wn = [0, 0]
    try:
        n, bad, first = CC.check(grid, out, CELL, wn)
    except Exception as e:
        n, bad, first = 0, -1, f"KeyError {e}"
    check(f"{tag} 1 old-shape write parts compared: {wn[0]} of {len(ROWS)} rows, {bad} differ",
          wn[0] == len(ROWS) and n == len(ROWS) and bad == 0, (n, bad, first))
    # 3 (before the plant)
    table = {}
    runs = CC.bill_check(grid, out, CELL, table)
    forms = {f: table.get((f, "write runs"), {}) for f in CC.BILL_FORMS}
    check(f"{tag} 3 the bill's write runs: {runs} compared",
          runs == 2 and all(e and e["runs"] == 2 and e["worst"] == 0 for e in forms.values()), forms)
    # 2
    planted = [new_row(r) for r in ROWS]
    planted[1]["write_value"] = str(int(planted[1]["write_value"]) + 1)
    put(writes_in(out, "ldbc", None).path_of("parts", CELL), NEW, planted)
    n, bad, first = CC.check(grid, out, CELL, [0, 0])
    check(f"{tag} 2 a planted written-value difference found", bad == 1 and "write_value" in first, (bad, first))
    # 5
    planted = [new_row(r) for r in ROWS]
    planted[0]["put_units_1k"] = "2"
    put(writes_in(out, "ldbc", None).path_of("parts", CELL), NEW, planted)
    n, bad, first = CC.check(grid, out, CELL, [0, 0])
    check(f"{tag} 5 a planted write-units difference found", bad == 1 and "put_units_1k" in first, (bad, first))


def last_chunk_case(tag, grid, out):
    """case 4: compose_writes sums each last-chunk column of the parts it takes into the counts row"""
    lc = list(step_record.LAST_CHUNK)
    src = {"props_split_adj_split": 1, "props_condensed_adj_condensed": 100}     # each source's last-chunk values differ
    for cell, k in src.items():
        w = writes_in(grid, "ldbc", None)
        put(w.path_of("parts", cell), NEW, [{**new_row(r), **{c: str(k * (i + 1)) for i, c in enumerate(lc)}} for r in ROWS])
        flat = [{**dict.fromkeys(capture_layout.write_columns("counts"), "0"), "sf": "3", "cell": cell, "query": "w_upd",
                 "param_idx": str(p), "body": "w_upd", "ok": "1", "error": ""} for p in (1, 2)]
        put(w.path_of("counts", cell), capture_layout.write_columns("counts"), flat)
    CC.compose_writes(CC.Source(grid, "props_split_adj_split"), CC.Source(grid, "props_condensed_adj_condensed"), CELL, out)
    got = capture_layout.write_rows(writes_in(out, "ldbc", None).path_of("counts", CELL), cell=CELL)[2]
    # each param: one props part (from Sp-Sp, x1) and one adjacency part (from Co-Co, x100)
    want = {c: 1 * (i + 1) + 100 * (i + 1) for i, c in enumerate(lc)}
    rows = [{c: r.get(c) for c in lc} for r in got]
    check(f"{tag} 4 a composed counts row carries its parts' last-chunk sums ({len(lc)} columns, {len(got)} rows)",
          len(got) == 2 and all(r[c] not in (None, "") and int(float(r[c])) == want[c] for r in rows for c in lc), rows)


def main():
    CC.DATASET[0], CC.PARAM_SET[0], CC.READ_SUBS[0], CC.WITH_WRITES[0] = "ldbc", None, (), True
    with tempfile.TemporaryDirectory() as tmp:
        cases("grid", os.path.join(tmp, "grid"), os.path.join(tmp, "out"))
        last_chunk_case("grid", os.path.join(tmp, "lc", "grid"), os.path.join(tmp, "lc", "out"))
    print(f"check_compose_cells_write_columns {'FAIL' if BAD else 'OK'}: {5 - len(BAD)}/5 cases"
          + (f"; failed {BAD}" if BAD else ""))
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
