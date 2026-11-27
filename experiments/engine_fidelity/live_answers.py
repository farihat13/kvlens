"""The live engines' ANSWERS, fetched once and stored raw -- so `validate.py --live` re-asks an engine
only when what decides its answer has changed, and a parser fix is a re-parse, not a re-run.

What an engine returns depends on three things, none of them our code:
  the query text   engine/data/queries/<db>/...        -> query_sha
  the bind params  the param row, as sent to the engine -> params
  the loaded data  the engine backup's provenance file  -> data (engines/<db>/backup/load-sf<SF>.txt, else provenance-sf<SF>.txt)
A stored answer is reused while all three match; otherwise the engine is asked again (it must be up).
A capture read from another tree (validate.py --from paper-results: `tree`) is never written, so no engine is asked
for it, and this machine's load says nothing about it: an answer there is checked against today's query text and
params and against the capture's own load (`load`: the `data_created` stamp of the database's rows in the capture's
live_kvstats.csv, so the answer and the counts come from one load). One that does not match is an error that names
what differs.
`stored_only` (validate.py --stored-answers) reuses a stored answer whose params match without checking
the query text or the loaded data -- for offline work on a machine with no engine up; each such answer is
reported as `stored-unchecked`, never as a verified one.

Stored per cell next to the captured plan, one file per identity (the snapshot copies them):
  <out_dir(workload, sf)>/<db>/<query>/p<idx>/answer.json   {query_sha, params, data, fetched_at, raw}
`raw` is the client response as JSON: Dgraph's `data` dict and ArangoDB's row list as returned; Nebula's
ResultSet as {keys, rows} of `cast_primitive` values -- the same values its `as_data_frame()` holds,
so the parser reads a stand-in with the same frame. TuGraph's as returned, {header, result}: its texts name their
columns as NebulaGraph's do, so NebulaGraph's parser reads it through the same stand-in (_tugraph_frame).
"""
from __future__ import annotations

import dataclasses
import functools
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from kvengines import live_workloads as W
from kvevaluator.paths import ROOT

from kvengines import complex_kv_reads as CKR


@functools.lru_cache(maxsize=None)
def _backup_place(db):
    """(its dir under engines/, the tag its backup and load record carry after sf<SF>) of a configuration or a database:
    engines/env.sh's engine_dir and engine_backup_tag, the one place that names them"""
    out = subprocess.run(["bash", "-c", 'source engines/env.sh > /dev/null && echo "$(engine_dir "$1") $(engine_backup_tag "$1")"',
                          "_", db], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    return out[0], (out[1] if len(out) > 1 else "")


class Unparsed(Exception):
    """The live response had rows, the parser returned none."""


def answer_path(workload, sf, qname, db, pidx, tree=None) -> Path:
    return W.out_dir(workload, sf, tree=tree) / db / qname / f"p{pidx}" / "answer.json"


def _data_provenance(db, sf):
    """The engine backup's provenance record (schema sha, build time) -- a reload changes it. None
    when the backup predates provenance files: then a reload goes unnoticed, so refresh by hand."""
    folder, tag = _backup_place(db)
    backup = ROOT / "engines" / folder / "backup"
    p = backup / f"load-sf{sf}{tag}.txt"             # this machine's load (engines/provenance.sh), else the tracked record
    if not p.exists():
        p = backup / f"provenance-sf{sf}{tag}.txt"
    if not p.exists():
        return None
    return dict(ln.split("=", 1) for ln in p.read_text().splitlines() if "=" in ln)


def identity(workload, sf, qname, db, pidx):
    """What decides the engine's answer, or None when the SF has no such param. db: a configuration (CKR.CONFIGURATIONS)
    or a database; its engine picks the text and the params, its own load the data."""
    qtext = W.query_path(workload, CKR.engine(db), qname).read_text()
    params = W.db_params(workload, sf, qname, CKR.engine(db), pidx, qtext)
    if params is None:
        return None
    return {"query_sha": hashlib.sha256(qtext.encode()).hexdigest()[:16],
            "params": json.loads(json.dumps(params, default=str)),
            "data": _data_provenance(db, sf)}


def _to_json(db, raw):
    if CKR.engine(db) == "nebula":
        return {"keys": list(raw.keys()), "rows": raw.as_primitive()}
    return raw


class _NebulaStandIn:
    """What the Nebula parser reads from a ResultSet: `as_data_frame()` of primitive values."""

    def __init__(self, stored):
        self._keys, self._rows = stored["keys"], stored["rows"]

    def as_data_frame(self):
        import pandas as pd
        if not self._rows:
            return pd.DataFrame()
        return pd.DataFrame({k: [r[k] for r in self._rows] for k in self._keys})

    def row_size(self):
        return len(self._rows)


# TuGraph's answer, read for NebulaGraph's parser: its REST server renders a list as one unquoted string, so its texts
# return each organisation as one string '|name|year|place|' (engines/queries/ldbc/tugraph/interactive-complex-1.cypher)
# and its emails and languages are one string joined by ';' (TuGraph has no list property)
_TUGRAPH_RECORDS = ("friendUniversities", "friendCompanies")
_TUGRAPH_JOINED = ("friendEmails", "friendLanguages")


def _tugraph_frame(raw):
    """TuGraph's {header, result} as the {keys, rows} NebulaGraph's stand-in reads, its records and joined strings
    made lists"""
    keys = [h["name"] for h in raw["header"]]
    rows = []
    for values in raw["result"]:
        row = dict(zip(keys, values))
        for k in _TUGRAPH_RECORDS:
            if isinstance(row.get(k), str):     # "[|a|2001|b|,|c|2005|d|]" -> [["a", "2001", "b"], ["c", "2005", "d"]]
                body = row[k].strip()[1:-1]
                row[k] = [rec.strip("|").split("|") for rec in body.split("|,|")] if body else []
        for k in _TUGRAPH_JOINED:
            if isinstance(row.get(k), str):
                row[k] = row[k].split(";") if row[k] else []
        rows.append(row)
    return {"keys": keys, "rows": rows}


def _execute(workload, sf, qname, db, pidx):
    from kvengines import connect
    make = {"dgraph": lambda: connect("dgraph"),
            "nebula": lambda: connect("nebula"),
            "arangodb": lambda: connect("arangodb", host="127.0.0.1", request_timeout=600),
            "tugraph": lambda: connect("tugraph")}[CKR.engine(db)]
    qtext = W.query_path(workload, CKR.engine(db), qname).read_text()
    client = make()
    pin = _pinner(db)
    try:
        pin(db)
        return client.execute(qtext, W.db_params(workload, sf, qname, CKR.engine(db), pidx, qtext)).raw
    finally:
        pin(CKR.cheapest_of(db))
        try:
            client.close()
        except Exception:
            pass


def _pinner(db):
    """pins a configuration's server knobs (CKR.knobs: Dgraph's equality-filter path) at measure.py's counting point,
    so an answer is the configuration's own; afterwards the engine's first configuration's are pinned again, as
    measure.py leaves the server. A no-op for an engine whose configurations have no knobs"""
    if not any(CKR.knobs(x) for x in CKR.DBS if CKR.engine(x) == CKR.engine(db)):
        return lambda _: None
    import measure as M
    from kvengines.kv import KVProfiler
    prof = KVProfiler()
    return lambda x: M._pin(prof, M.CACHE_MODE, M.INEQ_PATH, x)


def fetch(workload, sf, qname, db, pidx, refresh=False, stored_only=False, tree=None, load=None):
    """(raw JSON answer, "stored" | "stored-unchecked" | "fetched"). Asks the engine only when no stored
    answer matches the cell's identity (or `refresh`). `stored_only` never asks: it reuses a stored answer
    with the same params (query text and data unchecked) or raises FileNotFoundError. `tree`: a capture that is only
    read (W.capture_tree), from the load stamped `load`: never asks either; its answer is "stored" when the query
    text, the params and that load match, else FileNotFoundError."""
    ident = identity(workload, sf, qname, db, pidx)
    path = answer_path(workload, sf, qname, db, pidx, tree)
    differs = "no answer file"
    if not refresh and path.exists():
        stored = json.loads(path.read_text())
        if tree is not None:      # the capture's own load, not this machine's
            ident = {**ident, "data": {**(stored.get("data") or {}), "created": load}}
        if {k: stored.get(k) for k in ident} == ident and (tree is None or load):
            return stored["raw"], "stored"
        if stored_only and stored.get("params") == ident["params"]:
            return stored["raw"], "stored-unchecked"
        differs = ("its " + ", ".join(k for k in ident if stored.get(k) != ident[k]) + " differ" if load or tree is None
                   else "the capture's counts name no load")
    if stored_only:
        raise FileNotFoundError(f"no stored answer with these params at {path}")
    if tree is not None:
        raise FileNotFoundError(f"no stored answer for today's query text and params from the capture's load at {path} "
                                f"({differs})")
    raw = _to_json(db, _execute(workload, sf, qname, db, pidx))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**ident, "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "raw": raw}, indent=1, default=str) + "\n")
    return json.loads(json.dumps(raw, default=str)), "fetched"   # the stored form, either way


def parse(qname, db, raw):
    """The LDBC rows (dicts) the kvengines parser reads from a stored answer."""
    from kvengines.cost_eval.loader import parse_query_spec
    from kvengines.ldbc.parsers.arangodb import ArangoDBLDBCParser
    from kvengines.ldbc.parsers.dgraph import DgraphLDBCParser
    from kvengines.ldbc.parsers.nebula import NebulaLDBCParser
    db = CKR.engine(db)                 # a configuration's answer is read as its engine's
    parser = {"dgraph": DgraphLDBCParser, "nebula": NebulaLDBCParser,
              "arangodb": ArangoDBLDBCParser, "tugraph": NebulaLDBCParser}[db]
    qt, num = parse_query_spec(CKR.base_query(qname))     # a query-text variant answers its base query
    frame = {"nebula": lambda: _NebulaStandIn(raw), "tugraph": lambda: _NebulaStandIn(_tugraph_frame(raw))}
    parsed = parser().parse(qt, num, frame.get(db, lambda: raw)())
    rows = raw["result"] if db == "tugraph" else raw.values() if isinstance(raw, dict) else ()
    if not parsed and any(isinstance(v, list) and v for v in rows):
        # the engine returned rows the parser didn't read (query blocks and parser drifted
        # apart): the live answer is UNKNOWN, not empty -- never score it as a mismatch
        raise Unparsed(f"parser read 0 rows; response blocks "
                       f"{ {k: len(v) for k, v in raw.items() if isinstance(v, list)} }")
    return [dataclasses.asdict(r) if dataclasses.is_dataclass(r) else dict(r) for r in parsed]


def live_rows(qname, db, sf, pidx, workload="interactive", refresh=False, stored_only=False, tree=None, load=None):
    """(the engine's parsed answer for one cell, where it came from: fetch's source)."""
    raw, source = fetch(workload, sf, qname, db, pidx, refresh, stored_only, tree, load)
    return parse(qname, db, raw), source
