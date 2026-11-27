"""The two layouts of a grid capture: an older file names a body `variant` (layout 1), a newer one `body` (layout 2).
The header says which; every reader goes through here, so an older capture reads like a fresh one.

kvbench's CSVs are not grid captures: their `variant` is a physical design (canonical, label_only ...) and is
never renamed; a header with kvbench's `backend` column keeps it. A header without `variant` is returned as it is
too, so any CSV may be read through here. Columns renamed later (capture_columns.RENAMED_COLUMNS: the item-size
histograms, records_decoded, ...) are read by their new names in every layout.
"""
import csv
import os

from kvevaluator.capture.capture_columns import APPENDED_COLUMNS, RENAMED_COLUMNS

BODY = "body"
OLD_BODY = "variant"                    # layout 1's name for the body


def fix_header(fields, what="a capture"):
    """the header with every renamed column read by its new name (capture_columns.RENAMED_COLUMNS) and layout 1's
    `variant` read as `body` (kvbench's `variant` kept); a header with an old and a new name of one column is refused"""
    fields = list(fields or [])
    for old, new in RENAMED_COLUMNS.items():
        if old in fields and new in fields:
            raise ValueError(f"{what}: both `{old}` and `{new}` columns (one column under two names)")
    fields = [RENAMED_COLUMNS.get(f, f) for f in fields]
    if "backend" in fields:                 # kvbench's: `variant` is a physical design
        return fields
    if OLD_BODY in fields and BODY in fields:
        raise ValueError(f"{what}: both `{OLD_BODY}` and `{BODY}` columns (neither layout)")
    return [BODY if f == OLD_BODY else f for f in fields]


class _Reader(csv.DictReader):
    """csv.DictReader whose rows read an old workload key as the new one"""

    missing_appended = ()

    def __next__(self):
        row = super().__next__()
        for c in self.missing_appended:     # a column appended after the file was written: "" = not measured
            row[c] = ""
        return row


def reader(f, what="a capture"):
    """a csv.DictReader over the open file `f` whose rows are keyed `body` in either layout (and say `interactive`
    where an old row says `ldbc`)"""
    r = _Reader(f)
    r.fieldnames = fix_header(r.fieldnames, what)
    if r.fieldnames:                    # written before a column was appended: it reads ""
        r.missing_appended = tuple(c for c, (_, after) in APPENDED_COLUMNS.items()
                                   if after in r.fieldnames and c not in r.fieldnames)
    return r


def read_rows(path):
    """every row of the capture CSV at `path`, keyed `body`"""
    with open(path, newline="") as f:
        return list(reader(f, path))


# ---- a writes capture's files: the grid shape (capture_columns) or the old counts driver's ----------
# The old shape (older grid_writes and kvbench files): counts with `backend`, `variant`,
# `db` and the byte lanes as <lane>_bytes, the values read in one column (value_bytes, its GET part get_bytes) and every
# read lane in total_bytes; steps and parts with `sf`, `workload`, `db` and the <lane>_bytes names. Every grid column is
# filled from it exactly, so an old file is a re-parse, never a re-measure. Not in it, left blank: body_sha (a writer
# that knows the plan fills it: grid_writes.upgrade), the per-run wall_ns, and answer_key / run_key (blank on every
# write row: a write plan has one body, nothing to agree with or reuse by).
OLD_WRITE_DROP = ("sf", "workload", "db")          # steps and parts: identity columns the file's place already says
WRITE_EXTRA = {"counts": ("payload_bytes",), "steps": ("run",), "parts": ("run",)}   # kept beside the grid columns


def write_kind(fields):
    """counts, steps or parts, from a writes file's header (either shape)"""
    fields = set(fields or [])
    if "part" in fields:
        return "parts"
    if "step_idx" in fields:
        return "steps"
    if "backend" in fields or "cell" in fields:
        return "counts"
    raise ValueError(f"not a writes capture file: header {sorted(fields)[:8]}...")


def is_writes_file(path):
    """True when the CSV at `path` is a writes capture's (its header has a write lane, in either shape); a read
    capture's file has none"""
    with open(path, newline="") as f:
        fields = set(next(csv.reader(f), []))
    return bool(fields & {"write_key", "write_key_bytes"})


def is_old_write_shape(fields):
    """True for the old counts driver's columns (counts: `backend`; steps and parts: `db`)"""
    fields = set(fields or [])
    return "backend" in fields or ("db" in fields and "body" not in fields)


def write_columns(kind):
    """the grid columns of a writes file of `kind`, then its kept extras"""
    from kvevaluator.capture import capture_columns as C
    base = {"counts": C.WRITE_RUN_COLS, "steps": C.WRITE_STEP_COLS, "parts": C.WRITE_PART_COLS}[kind]
    return list(base) + list(WRITE_EXTRA[kind])


def _from_old_counts(r, cell):
    f = lambda k: int(float(r.get(k) or 0))
    return {"sf": r["sf"], "cell": cell, "query": r["query"], "param_idx": r["param_idx"], "body": r["query"],
            "measure_only": "0", "ok": "1", "error": "",
            **{k: r[k] for k in ("S", "G", "N", "S_miss", "G_miss", "N_miss")},
            "get_key": f("get_key_bytes"), "get_value": f("get_bytes"), "scan_key": f("scan_key_bytes"),
            "scan_value": f("value_bytes") - f("get_bytes"), "seek_miss_key": f("seek_miss_key_bytes"),
            "next_miss_key": f("next_miss_key_bytes"), "get_miss_key": f("get_miss_key_bytes"),
            "read_bytes": f("total_bytes"), "rows": r["rows"], "result_hash": r.get("result_hash", ""),
            "answer_key": "", "body_sha": "", "run_key": "", "wall_ns": "", "measured_as": "",
            "P": r["P"], "D": r["D"], "write_key": f("write_key_bytes"), "write_value": f("write_value_bytes"),
            "payload_bytes": r.get("payload_bytes", "")}


def _from_old_steps(r, cols):
    """a step or part row: a <lane>_bytes column named bare where the grid columns name the lane bare (out_bytes is
    the step record's own name), `body` = the query (a write plan's one body)"""
    out = {"body": r["query"]}
    for k, v in r.items():
        if k in OLD_WRITE_DROP:
            continue
        bare = k[:-len("_bytes")] if k.endswith("_bytes") else None
        out[bare if bare and k not in cols and bare in cols else k] = v
    return out


def required_columns(kind):
    """what a writes file of `kind` must hold to be read at all: its keys, the op counts and the byte lanes (a column
    an older capture did not measure yet, such as the item histograms, reads as empty)"""
    from kvevaluator.capture import step_record as SR
    keys = {"counts": ["query", "param_idx"], "steps": ["query", "param_idx", "step_idx", "step", "op"],
            "parts": ["query", "param_idx", "step_idx", "step", "op", "part"]}[kind]
    return keys + [*SR.BASE_OPS, *SR.WRITE_OPS, *SR.READ_BYTE_LANES, *SR.WRITE_BYTE_LANES]


def _missing(cols, have, kind):
    """the grid columns of `kind` a file's rows lack (its kept extras are optional)"""
    return [c for c in cols if c not in have and c not in WRITE_EXTRA[kind]]


def _cell_of(path, cell):
    """the cell or store a writes file holds: `cell`, else the file's stem (layout 1 prefixes meta-)"""
    if cell:
        return cell
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem[len("meta-"):] if stem.startswith("meta-") else stem


def _grid_rows(kind, rows, cols, cell):
    """an old file's rows in the grid columns (cols, in order; the kept extras where the file has them)"""
    rows = [_from_old_counts(r, cell) for r in rows] if kind == "counts" else [_from_old_steps(r, set(cols)) for r in rows]
    have = [c for c in cols if not rows or c in rows[0]]
    return [{c: r[c] for c in have} for r in rows]


def write_rows(path, cell=None, strict=False):
    """(kind, header, rows) of a writes capture file at `path` in the grid shape (write_columns), from either shape.
    `cell`: the cell or store the file holds (default: the file's stem; an old counts row says only `meta`). A grid
    column the file lacks reads as empty, unless it is required (required_columns); `strict`: every grid column must
    be there (an upgrade in place writes nothing it cannot fill)"""
    with open(path, newline="") as f:
        rd = reader(f, path)
        fields = list(rd.fieldnames or [])
        rows = list(rd)
    kind = write_kind(fields)
    cols = write_columns(kind)
    if is_old_write_shape(fields):
        rows = _grid_rows(kind, rows, cols, _cell_of(path, cell))
        fields = list(rows[0]) if rows else [c for c in cols if c not in WRITE_EXTRA[kind]]
    missing = _missing(cols, set(fields), kind)
    hard = [c for c in missing if strict or c in required_columns(kind)]
    if hard:
        raise ValueError(f"{path}: a {kind} file the grid shape cannot be read from (missing {hard[:5]})")
    if missing:
        rows = [{**r, **dict.fromkeys(missing, "")} for r in rows]
    return kind, [c for c in cols if c not in WRITE_EXTRA[kind] or c in fields], rows
