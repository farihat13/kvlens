"""One way to say a result was made from an input that moved: a warning, and a mark in what the step writes.

A guard that finds a moved input (a body whose sha changed, a dirty binary, an in-sample pick, ...) and can still give a
result calls `note(kind, detail)` instead of refusing: a warning line once per (kind, detail), kept for the process.
Every writer of a record or meta adds `"stale": notes()` when the list is not empty (`fold`); a record with no
`stale` is clean. A process that writes one record per unit (a cell) opens a `scope()` per unit: its record folds the
notes made inside it only. Where a result's unit is a row (a query's cost, a timed cell), its CSV has one `stale` column: the
row's kinds joined by `;` (`column`), empty when clean.

A result that a ledger row declares current (kvevaluator.adoptions) is kept with `adopted(rows, detail)`: no warning, and
the record's `adopted` list (beside `stale`, never in it) names the rows.

A shell driver notes into the record or the directory a later step folds in:

    python -m kvevaluator.stale start --into <dir> [--unit U]        # at the driver's start: an earlier run's notes aside
    python -m kvevaluator.stale note --into <dir> [--unit U] <kind> <detail>   # folded into the record written there

An analysis step whose outputs are tables (no record) marks its dir with `mark_dir(dir, unit)` (its own
stale-<unit>.jsonl, rewritten each run).

`--unit` (a cell): the note is that unit's only (<dir>/stale-<U>.jsonl), so processes that write one cell each into one
dir at once neither mark nor move each other's notes; without it the note is the dir's (<dir>/stale.jsonl), folded into
every record written there.

The kinds are one vocabulary (KINDS); a kind not in it raises: a typo is a bug, not a marker.
"""
import argparse
import contextlib
import inspect
import json
import os
import sys
import time

KINDS = {
    "body_moved": "a decided or captured body's sha is not that body at HEAD; today's body runs or is costed",
    "under_successor": "a row of an old body, taken as its count-equal successor's (the reuse ledger's body rows, inputs/adoptions.json)",
    "candidates_unmeasured": "HEAD has candidates the capture never ran; picked among those it ran",
    "measure_only_unknown": "the capture has no measure_only column; read as 0",
    "pick_in_sample": "no held-out capture or record row: picked on the reported params or the cell's own capture",
    "protocol_mixed": "timings of different protocols or store settings in one figure, score or resumed grid",
    "foreign_load": "another evaluator or engine process, or no NUMA node set, while timing",
    "no_sudo": "timed without passwordless sudo: the page cache not dropped, the machine not prepared",
    "numa_scannable": "a bound server had a mapping the NUMA scanner may scan",
    "timing_incomplete": "a wall capture without its breakdown (or the reverse)",
    "binary_unclean": "the binary is dirty, unstamped, or its C++ differs from HEAD",
    "binary_behind_head": "the binary is not an ancestor of HEAD, or count code moved since it",
    "stream_keyed_by_content": "a records stream or cache keyed by its content sha (no key file, a dirty dump, a code-only move)",
    "capture_shape_moved": "composed or read captures differ in measurement_version, inputs or record shape",
    "composed_stale": "the composed capture is older than its measured inputs",
    "binaries_differ": "inputs made by different binaries (same tally and counting versions)",
    "predictions_skipped": "predictions of another body than the capture's, left out",
    "table_stale": "a results table differs from the rows composed now",
    "shape_sibling": "an existing file of another shape: a sibling file is written beside it",
    "reused_stale": "--reuse-stale kept a result whose freshness key moved",
    "timed_counts_differ": "a timed run's counts differ from the counts capture's row of the picked plan (the counts gate)",
}

_NOTES = []
_SEEN = set()
_WARNED_INTO = set()         # (kind, detail) note_into has warned of in this process
_SCOPES = []                 # the open scopes, innermost last: each a list of the notes made while it is open
STALE_FILE = "stale.jsonl"   # a directory's notes from a shell step, folded into the record written there
ADOPTED = "adopted"          # a note of a result a ledger row declares current: the record's `adopted` list, not `stale`


def notes_file(directory, unit=None):
    """a shell step's notes in `directory`: the dir's own, or one unit's (a cell)"""
    return os.path.join(directory, STALE_FILE if unit is None else f"stale-{unit}.jsonl")


def _check(kind):
    if kind not in KINDS:
        raise ValueError(f"stale kind {kind!r} is not one of kvevaluator.stale.KINDS ({', '.join(sorted(KINDS))})")


def _caller(depth=2):
    f = inspect.stack()[depth]
    return f"{inspect.getmodule(f.frame).__name__ if inspect.getmodule(f.frame) else '?'}.{f.function}"


def _keep(entry, line):
    """keep a note (or an adoption) for the open scopes and the process; print `line` once per (kind, detail)"""
    key = (entry["kind"], entry["detail"])
    for sc in _SCOPES:
        if all((n["kind"], n["detail"]) != key for n in sc):
            sc.append(dict(entry))
    if key in _SEEN:
        return
    _SEEN.add(key)
    _NOTES.append(entry)
    print(line, file=sys.stderr, flush=True)


def note(kind, detail, at=None):
    """warn once per (kind, detail) in this process and keep the note for the records this process writes"""
    _check(kind)
    detail = str(detail)
    _keep({"kind": kind, "detail": detail, "at": at or _caller()}, f"[stale] WARNING {kind}: {detail}")


def adopted(row_ids, detail, at=None):
    """a result kept because ledger rows declare it current (kvevaluator.adoptions): marked `adopted` in the records
    this process writes (beside `stale`, never in it), with no warning"""
    detail = str(detail)
    _keep({"kind": ADOPTED, "detail": detail, "rows": list(row_ids), "at": at or _caller()},
          f"[stale] adopted ({', '.join(row_ids)}): {detail}")


def keep(entries):
    """keep the notes another process of this command made (a forked worker's notes(): run_measure.at_a_time), each
    as note() or adopted() made here would"""
    for e in entries:
        said = f"adopted ({', '.join(e.get('rows', []))})" if e["kind"] == ADOPTED else f"WARNING {e['kind']}"
        _keep(dict(e), f"[stale] {said}: {e['detail']}")


@contextlib.contextmanager
def scope():
    """the notes made while it is open, for one unit's record (fold uses the innermost open scope)"""
    sc = []
    _SCOPES.append(sc)
    try:
        yield sc
    finally:
        _SCOPES.remove(sc)


def notes():
    """the notes kept so far, in the order they were made"""
    return [dict(n) for n in _NOTES]


def kinds():
    """the stale kinds noted so far, sorted (an adoption is not one)"""
    return sorted({n["kind"] for n in _NOTES if n["kind"] != ADOPTED})


def column(row_kinds):
    """a CSV row's `stale` cell: its kinds, sorted and joined by ';' (empty when clean; an adoption is not stale)"""
    row_kinds = [k for k in row_kinds if k != ADOPTED]
    for k in row_kinds:
        _check(k)
    return ";".join(sorted(set(row_kinds)))


def _merge(have, new):
    """have + the notes of new that it does not hold (by kind and detail), in order"""
    out, seen = list(have), {(n["kind"], n["detail"]) for n in have}
    for n in new:
        if (n["kind"], n["detail"]) not in seen:
            seen.add((n["kind"], n["detail"]))
            out.append(n)
    return out


def _shell_notes(directory, unit):
    """a shell step's notes in `directory`: the dir's, and the unit's when one is given"""
    if directory is None:
        return []
    out = []
    for path in [notes_file(directory)] + ([notes_file(directory, unit)] if unit is not None else []):
        if os.path.exists(path):
            with open(path) as f:
                out += [json.loads(line) for line in f if line.strip()]
    return out


def fold(record, directory=None, unit=None, extra=()):
    """the record with `stale` set to its own notes, then `extra` (another record's, e.g. a composed record's second
    source), this unit's (the innermost open scope's, else the process's) and a shell step's (<directory>/stale.jsonl,
    and stale-<unit>.jsonl for a unit's record); adoptions go to `adopted` instead; the record unchanged when there
    are none"""
    new = [dict(n) for n in extra] + ([dict(n) for n in _SCOPES[-1]] if _SCOPES else notes()) + _shell_notes(directory, unit)
    merged = _merge(list(record.get("stale", [])) + list(record.get(ADOPTED, [])), new)
    for field, part in (("stale", [n for n in merged if n["kind"] != ADOPTED]),
                        (ADOPTED, [n for n in merged if n["kind"] == ADOPTED])):
        if part:
            record[field] = part
    return record


def note_into(directory, kind, detail, unit=None, at="shell"):
    """a note for a record another process writes: appended to <directory>/stale.jsonl (or stale-<unit>.jsonl), which
    the record written there folds in (`fold`); also warned here, once per (kind, detail) in this process (a measure
    notes one binary note into every store's capture dir)"""
    _check(kind)
    if not os.path.isdir(directory):
        raise SystemExit(f"[stale] --into {directory}: not a directory (a shell step notes into the dir whose record "
                         f"folds its notes)")
    with open(notes_file(directory, unit), "a") as f:
        f.write(json.dumps({"kind": kind, "detail": detail, "at": at}, sort_keys=True) + "\n")
    if (kind, detail) not in _WARNED_INTO:
        _WARNED_INTO.add((kind, detail))
        print(f"[stale] WARNING {kind}: {detail}", file=sys.stderr, flush=True)


def mark_dir(directory, unit):
    """an analysis step's mark on the files it wrote in `directory` (tables, no record to fold into): this process's
    notes (the innermost scope's, else all) as <directory>/stale-<unit>.jsonl, rewritten each run, so a clean re-run
    leaves none; a driver's notes in the dir's own stale.jsonl are left. The kinds marked (sorted)"""
    new = [dict(n) for n in _SCOPES[-1]] if _SCOPES else notes()
    path = notes_file(directory, unit)
    if new:
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            f.writelines(json.dumps(n, sort_keys=True) + "\n" for n in new)
        os.replace(tmp, path)
    elif os.path.exists(path):
        os.remove(path)          # this step's own mark from an earlier run: this run is clean
    return sorted({n["kind"] for n in new if n["kind"] != ADOPTED})


def start(directory, unit=None):
    """a run's start: an earlier run's notes in `directory` (the dir's, or one unit's) are moved to
    stale[-<unit>].before-<their time>.jsonl beside them, so this run's records fold this run's notes only (a capture
    dir is kept across runs). Nothing to do for a dir that is not there yet"""
    path = notes_file(directory, unit) if os.path.isdir(directory) else None
    if path and os.path.exists(path):
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(os.path.getmtime(path)))
        aside = path[:-len(".jsonl")] + f".before-{stamp}.jsonl"
        os.replace(path, aside)
        print(f"[stale] an earlier run's notes moved aside: {aside}", file=sys.stderr, flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("note", help="note a moved input into a directory's stale.jsonl (its record folds it in)")
    n.add_argument("--into", required=True)
    n.add_argument("--unit", help="the note is this unit's only (a cell): stale-<unit>.jsonl")
    n.add_argument("kind", choices=sorted(KINDS))
    n.add_argument("detail")
    st = sub.add_parser("start", help="at a driver's start: move an earlier run's notes in the dir aside")
    st.add_argument("--into", required=True)
    st.add_argument("--unit", help="one unit's notes (a cell): stale-<unit>.jsonl")
    sub.add_parser("kinds", help="print the kinds and their meaning")
    a = ap.parse_args()
    if a.cmd == "kinds":
        for k, v in KINDS.items():
            print(f"{k}\t{v}")
        return 0
    if a.cmd == "start":
        start(a.into, a.unit)
        return 0
    note_into(a.into, a.kind, a.detail, a.unit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
