#!/usr/bin/env python3
"""Convert LDBC CsvBasic-LongDateFormatter CSV data into Dgraph-loadable RDF, for any scale factor.

A mapping (rdf_mappings.py) drives both the RDF and the matching ldbcTypes.schema, so predicate-name variants (e.g.
per-type `postCreationDate`) stay consistent across schema and data.

Usage:
  # the default mapping (posts and comments share the `msg_<id>` vid)
  python -m kvengines.dgraph.ldbc.csv_to_rdf --csv-root <sf dir>/CsvBasic-LongDateFormatter --out <sf dir>/ldbc_rdf
  # a schema variant
  python -m kvengines.dgraph.ldbc.csv_to_rdf ... --mapping per_type_dates

--check GOLDEN_DIR compares the triple sets with a reference RDF. It is meaningful only against a reference made with
the same vid convention: the default mapping's `msg_` vids differ from the published dgraph-benchmarks RDF's
`_:post_` / `_:comment_`, so there every message triple would differ.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import os
import re
import sys
import time

from kvengines.dgraph.ldbc import rdf_mappings as M

EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------------------
# Value conversion
# ---------------------------------------------------------------------------
def epoch_ms_to_datetime(ms: str) -> str:
    """1266161530447 -> '2010-02-14T15:32:10.447+00:00' (UTC, always 3-digit ms)."""
    t = EPOCH + dt.timedelta(milliseconds=int(ms))
    return f"{t:%Y-%m-%dT%H:%M:%S}.{t.microsecond // 1000:03d}+00:00"


def epoch_ms_to_date(ms: str) -> str:
    """628646400000 -> '1989-12-03' (UTC date-only; used for birthday)."""
    t = EPOCH + dt.timedelta(milliseconds=int(ms))
    return f"{t:%Y-%m-%d}"


def convert_value(raw: str, kind: str) -> str:
    """Map a raw CSV cell to its RDF literal text by `kind`."""
    if raw == "":
        return ""  # golden emits empty optionals (e.g. post imageFile) as ""
    if kind == "datetime":
        return epoch_ms_to_datetime(raw)
    if kind == "date":
        return epoch_ms_to_date(raw)
    # string / int: emitted as-is (schema coerces int).
    return raw


# The golden sf0.3 RDF emits literals RAW -- no escaping at all. LDBC content can
# contain a raw TAB (verified in the golden) but never a double-quote or newline, so
# raw emission is both faithful and safe. We guard against the chars that WOULD break
# the RDF (a quote ends the literal early; a newline breaks one-triple-per-line) so
# unexpected input fails loudly instead of producing a silently corrupt file.
def _safe(s: str) -> str:
    if '"' in s or "\n" in s or "\r" in s:
        raise ValueError(f"value contains a char the golden RDF format cannot represent "
                         f"raw (quote/newline): {s!r}")
    return s


def lit(s: str) -> str:
    return f'"{_safe(s)}"'


# ---------------------------------------------------------------------------
# CSV reading
# ---------------------------------------------------------------------------
def read_csv(path: str, limit: int | None):
    """Yield dict rows from a pipe-delimited LDBC CSV. QUOTE_NONE: '|' is the only
    separator and '"' is an ordinary character (LDBC content can contain quotes-free
    text but we never want csv to treat them specially)."""
    with open(path, newline="") as fh:
        reader = csv.reader(fh, delimiter="|", quoting=csv.QUOTE_NONE)
        header = next(reader)
        # Some LDBC relation CSVs repeat a column name (e.g. Place.id|Place.id).
        # We address columns positionally via the mapping, so keep the header list.
        for i, row in enumerate(reader):
            if limit is not None and i >= limit:
                break
            yield header, row


def col_index(header: list[str], name: str, occurrence: int = 0) -> int:
    """Index of the `occurrence`-th column named `name` (0-based occurrence)."""
    seen = 0
    for i, h in enumerate(header):
        if h == name:
            if seen == occurrence:
                return i
            seen += 1
    raise KeyError(f"column {name!r} (occurrence {occurrence}) not in header {header}")


# ---------------------------------------------------------------------------
# RDF emission
# ---------------------------------------------------------------------------
class Writer:
    """Lazily opens one file per RDF output name; many CSVs may share a file."""

    def __init__(self, out_dir: str):
        self.out_dir = out_dir
        self.handles: dict[str, object] = {}
        self.counts: dict[str, int] = {}

    def line(self, fname: str, text: str):
        fh = self.handles.get(fname)
        if fh is None:
            fh = self.handles[fname] = open(os.path.join(self.out_dir, fname), "w")
            self.counts[fname] = 0
        fh.write(text)
        self.counts[fname] += 1

    def close(self):
        for fh in self.handles.values():
            fh.close()


def emit_entity(ent: M.Entity, csv_root: str, w: Writer, limit, vfn):
    path = os.path.join(csv_root, ent.csv)
    n = 0
    for header, row in read_csv(path, limit):
        id_idx = col_index(header, ent.id_field)
        idv = row[id_idx]
        # `vfn` maps the node prefix to its vid string (e.g. post/comment -> msg).
        # `dgraph.type` and `<id>` keep the entity's own label/raw id.
        vp = vfn(ent.prefix)
        subj = f"_:{vp}_{idv}"
        w.line(ent.out_file, f'{subj} <dgraph.type> "{ent.label}" .\n')
        w.line(ent.out_file, f'{subj} <fqid> "{vp}_{idv}" .\n')
        w.line(ent.out_file, f'{subj} <id> "{idv}" .\n')
        for col in ent.columns:
            idx = col_index(header, col.csv_field)
            val = convert_value(row[idx], col.kind)
            w.line(ent.out_file, f'{subj} <{col.predicate}> {lit(val)} .\n')
        n += 1
    return n


def emit_multiliteral(ml: M.MultiLiteral, csv_root: str, w: Writer, limit, vfn):
    path = os.path.join(csv_root, ml.csv)
    n = 0
    for header, row in read_csv(path, limit):
        s_idx = col_index(header, ml.subject_field)
        v_idx = col_index(header, ml.value_field)
        subj = f"_:{vfn(ml.subject_prefix)}_{row[s_idx]}"
        val = convert_value(row[v_idx], ml.kind)
        # Two spaces before '.' to match the golden Deltas.rdf style.
        w.line(ml.out_file, f'{subj} <{ml.predicate}> {lit(val)}  .\n')
        n += 1
    return n


def emit_relationship(rel: M.Relationship, csv_root: str, w: Writer, limit, vfn):
    path = os.path.join(csv_root, rel.csv)
    n = 0
    # src/dst columns may share a header name (e.g. Place.id|Place.id) -> positional.
    same_name = rel.src_field == rel.dst_field
    for header, row in read_csv(path, limit):
        s_idx = col_index(header, rel.src_field, 0)
        d_idx = col_index(header, rel.dst_field, 1 if same_name else 0)
        subj = f"_:{vfn(rel.src_prefix)}_{row[s_idx]}"
        obj = f"_:{vfn(rel.dst_prefix)}_{row[d_idx]}"
        if rel.facet_field is not None:
            f_idx = col_index(header, rel.facet_field)
            raw = row[f_idx]
            if rel.facet_kind == "datetime" and rel.facet_offset_ms:
                raw = str(int(raw) + rel.facet_offset_ms)
            fval = convert_value(raw, rel.facet_kind)
            w.line(rel.out_file,
                   f'{subj} <{rel.predicate}> {obj}  ( {rel.facet_name}="{_safe(fval)}"  )  .\n')
        else:
            w.line(rel.out_file, f'{subj} <{rel.predicate}> {obj}  .\n')
        n += 1
    return n


# ---------------------------------------------------------------------------
# Schema generation (from the same mapping)
# ---------------------------------------------------------------------------
def generate_schema(m: M.Mapping) -> str:
    """Build ldbcTypes.schema from the mapping: type blocks + predicate directives.

    Predicate directives are collected from every column/edge/multi-literal and
    de-duplicated; a predicate used with two different directives is an error
    (catches an inconsistent variant before it reaches Dgraph)."""
    preds: dict[str, str] = {}

    def add_pred(name: str, directive: str):
        if name in preds and preds[name] != directive:
            raise ValueError(
                f"predicate {name!r} has conflicting schema types: "
                f"{preds[name]!r} vs {directive!r}")
        preds[name] = directive

    # Global predicates shared by every node.
    add_pred("fqid", "string @index(hash)")
    add_pred("id", "int @index(int)")

    for ent in m.entities:
        for col in ent.columns:
            add_pred(col.predicate, col.schema_type)
    for ml in m.multiliterals:
        add_pred(ml.predicate, ml.schema_type)
    for rel in m.relationships:
        add_pred(rel.predicate, rel.schema_type)

    # Type blocks: each entity's own literals + its outgoing edges/multi-literals.
    blocks = []
    for ent in m.entities:
        members = ["fqid", "id"] + [c.predicate for c in ent.columns]
        members += [r.predicate for r in m.relationships if r.src_prefix == ent.prefix]
        members += [ml.predicate for ml in m.multiliterals if ml.subject_prefix == ent.prefix]
        # De-dup preserving order (a type may have two edges sharing a predicate name).
        seen, ordered = set(), []
        for p in members:
            if p not in seen:
                seen.add(p)
                ordered.append(p)
        body = "\n".join(f"\t{p}" for p in ordered)
        blocks.append(f"type {ent.label}{{\n{body}\n}}")

    pred_lines = [f"{name:<20}: {directive} ." for name, directive in preds.items()]

    return (f"# Generated by csv_to_rdf.py from mapping '{m.name}'. Do not edit by hand.\n"
            + "\n\n".join(blocks) + "\n\n" + "\n".join(pred_lines) + "\n")


# ---------------------------------------------------------------------------
# Validation: semantic triple-set equality
# ---------------------------------------------------------------------------
_FACET_RE = re.compile(r'(\w+)="([^"]*)"')


def canon(line: str):
    """Parse one RDF line into a hashable, whitespace-independent triple.

    Literal:        ('L', subject, predicate, value)
    Edge (+facets): ('E', subject, predicate, object, ((facet,val), ...))
    Returns None for blank lines."""
    line = line.rstrip("\n")
    if not line.strip():
        return None
    body = line.rstrip()
    assert body.endswith("."), f"line does not end with '.': {line!r}"
    body = body[:-1].strip()
    subj, rest = body.split(" ", 1)
    rest = rest.lstrip()
    assert rest.startswith("<"), f"expected predicate: {line!r}"
    gt = rest.index(">")
    pred = rest[1:gt]
    rest = rest[gt + 1:].strip()
    if rest.startswith('"'):
        return ("L", subj, pred, rest[1:rest.rindex('"')])
    if "(" in rest:
        objpart, facetpart = rest.split("(", 1)
        facetpart = facetpart.rsplit(")", 1)[0]
        facets = tuple(sorted(_FACET_RE.findall(facetpart)))
        return ("E", subj, pred, objpart.strip(), facets)
    return ("E", subj, pred, rest.strip(), ())


def load_triples(rdf_dir: str) -> set:
    triples = set()
    dup = 0
    for path in sorted(glob.glob(os.path.join(rdf_dir, "*.rdf"))):
        with open(path) as fh:
            for line in fh:
                t = canon(line)
                if t is None:
                    continue
                if t in triples:
                    dup += 1
                triples.add(t)
    if dup:
        print(f"  note: {dup} duplicate triples within {rdf_dir} (collapsed in set)")
    return triples


def check(generated_dir: str, golden_dir: str) -> bool:
    print(f"Validating {generated_dir} against golden {golden_dir} ...")
    t0 = time.time()
    gen = load_triples(generated_dir)
    gold = load_triples(golden_dir)
    print(f"  generated triples: {len(gen):,}")
    print(f"  golden triples:    {len(gold):,}   (parsed in {time.time()-t0:.1f}s)")

    missing = gold - gen   # in golden, not produced
    extra = gen - gold     # produced, not in golden
    if not missing and not extra:
        print("  PASS: triple sets are identical.")
        return True

    print(f"  FAIL: {len(missing):,} missing, {len(extra):,} extra")
    for label, s in (("MISSING (golden not produced)", missing), ("EXTRA (produced not in golden)", extra)):
        for t in list(s)[:8]:
            print(f"    {label}: {t}")
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv-root", required=True,
                    help="CsvBasic-LongDateFormatter dir (contains static/ and dynamic/)")
    ap.add_argument("--out", required=True, help="output dir for *.rdf files")
    ap.add_argument("--mapping", default="default", choices=sorted(M.MAPPINGS),
                    help="schema mapping to use (default: default)")
    ap.add_argument("--schema-out", default=None,
                    help="path for generated ldbcTypes.schema "
                         "(default: <out>/../ldbcTypes.schema)")
    ap.add_argument("--check", default=None, metavar="GOLDEN_DIR",
                    help="after converting, validate triple-set equality vs GOLDEN_DIR")
    ap.add_argument("--limit", type=int, default=None,
                    help="max rows per CSV (for quick testing)")
    args = ap.parse_args()

    mapping = M.MAPPINGS[args.mapping]()
    os.makedirs(args.out, exist_ok=True)

    # Schema (from the same mapping).
    schema_out = args.schema_out or os.path.join(os.path.dirname(args.out.rstrip("/")),
                                                 "ldbcTypes.schema")
    with open(schema_out, "w") as fh:
        fh.write(generate_schema(mapping))
    print(f"Wrote schema: {schema_out}")

    # RDF.
    print(f"Converting (mapping='{mapping.name}') {args.csv_root} -> {args.out}")
    t0 = time.time()
    # Resolve a node prefix to its emitted vid string (identity unless overridden,
    # e.g. post/comment -> msg). Used for blank-node labels + fqid, not the schema.
    vfn = lambda p: mapping.vid_overrides.get(p, p)
    w = Writer(args.out)
    for ent in mapping.entities:
        n = emit_entity(ent, args.csv_root, w, args.limit, vfn)
        print(f"  entity {ent.label:<14} {n:>9,} rows -> {ent.out_file}")
    for ml in mapping.multiliterals:
        n = emit_multiliteral(ml, args.csv_root, w, args.limit, vfn)
        print(f"  multi  {ml.predicate:<14} {n:>9,} rows -> {ml.out_file}")
    for rel in mapping.relationships:
        n = emit_relationship(rel, args.csv_root, w, args.limit, vfn)
        print(f"  edge   {rel.predicate:<14} {n:>9,} rows -> {rel.out_file}")
    w.close()
    print(f"Done in {time.time()-t0:.1f}s. "
          f"{sum(w.counts.values()):,} triples across {len(w.counts)} files.")

    if args.check:
        ok = check(args.out, args.check)
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
