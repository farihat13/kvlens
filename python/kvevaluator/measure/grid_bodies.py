#!/usr/bin/env python3
"""Measure every plan body of every query of a read workload (LDBC IC/IS, BI, the microbench, LinkBench's reads) on each
design-space grid cell.

A format is priced at the cheapest plan its layout allows, so per cell and query this runs every body legal on the
cell (each sent alone, by name) on the same params and writes the raw counts. Choosing the body (the pick record) is
analysis (kvcost/layout_cost/picks.py), never done here.

Output, one set of files per cell under the workload's capture (kvevaluator.output_tree; --root for another tree):
counts/<cell>.csv (one row per query, param and body; measured_as names the run a copied row came from, and a
copied row keeps that run's wall_ns), steps/<cell>.csv, parts/<cell>.csv (each step split by store part; the parts sum
to the step), counts/<cell>.json (provenance). Re-running a cell replaces its files. The store is built on first use.
--exec serve (default) sends every run of a cell to one long-lived server; spawn starts one process per run (same
counts). A scan twin (the workload's ALTERNATIVES) is measured as extra bodies of its base query; the microbench's
other *_scan queries are queries of their own.

  python3 -m kvevaluator.measure.grid_bodies --sf 3 [--family interactive|microbench|bi|linkbench] [--nparam 5] [--cells a,b]
                                            [--P 8] [--fresh] [--spec-dir inputs/specs/mixes]
                                            [--exec serve|spawn] [--root DIR [--queries q1,q2] [--params 1,4] [--picks [FILE]]]
--queries / --params (a slice), --picks (only the picked body) and --bodies (only the named bodies) need --root.
"""
import argparse
import csv
import fcntl
import hashlib
import json
import os
import select
import subprocess
import sys
import time

from kvevaluator import stale
from kvevaluator.capture import step_record
from kvevaluator.measure import plan_runs, server, store_layout
from kvevaluator.capture import store_identity
from kvevaluator.capture.binary import binary_path
from kvevaluator.plan_choice.candidates import INVALID_HERE, NOT_RUN_AT_SF30, PLAN_PIPELINE, failed_run, plan_tree, candidate_bodies, candidates, held_out_window, measure_only_bodies, plan_files, query_order, result_key
from kvevaluator.plans.ir.bundle import plan_level
from kvevaluator.measure import grid_capture
from kvevaluator.output_tree import Capture, HELD_OUT, REPORTED, Tree, checked_param_set
from kvevaluator.measure.grid_capture import PICKS_FILE, record_cell_picks, store_is_current
from kvevaluator import datasets as _D, workloads as _W
from kvevaluator.measure.grid_capture import GRID, WORKLOADS, request_format, format_store, invalid_on_cell, measure_only_on_cell, meta_store, parse_slice, query_params, schema_file, spec_json, workload_queries
from kvevaluator.capture.spec_sets import format_of, format_spec
from kvevaluator.capture.spec_sets import named_cells
from kvevaluator.paths import ROOT, recorded_path, store_dir
from kvevaluator.capture import capture_layout
from kvevaluator.measure import counting

EXP = str(ROOT)
# both workloads run on the same canonical LDBC store, so they share its schema
# what a capture HOLDS (its columns and files), bumped here when the driver changes them; what an op is CHARGED is the
# binary's counting versions (measure/counting.py), compared beside it. Records keep it under "measurement_version",
# so they stay comparable.
CAPTURE_SHAPE = 7
FREE_ROWS = True   # run.free_rows on every request of this driver (counts only: it never times a plan for a latency number)
# Every run names its body; no row is "the planner's" (plan files carry no selector).
# The columns of each file kind (RUN_COLS, STEP_COLS, PART_COLS and the write cells' WRITE_*): capture/capture_columns
from kvevaluator.capture.capture_columns import (RUN_COLS, STEP_COLS, PART_COLS, WRITE_RUN_COLS,  # noqa: F401
                                                WRITE_PART_COLS)
from kvevaluator.capture.step_record import READ_BYTE_LANES, READ_OPS  # (a read row's ops and lanes)
# The SF30 rule: per (store, query), the SF3 pick
# and every body within SF30_T of it (wops_bytes4k, mean over the SF3 capture's rows: LDBC's held-out 11-15); a body with
# no SF3 row and a measure-only body the store's SF30 capture has not run yet run too; the rest run only when the pick
# is within SF30_CLOSE of its nearest measured rival on SF30's own rows. A diagonal cell runs every body.
SF30_TAG = "30"        # the scale the rule applies at (a check may set it to drive the rule at sf0.1)
SF30_T = 10.0          # from the SF3-vs-SF30 evidence: the largest within-10% ratio seen was 3.04x
SF30_CLOSE = 0.10
SF30_CRITERION = "wops_bytes4k"
# the cells the composed layouts are built from (Split, Condensed, and Chunked at each size the chunk-size front reads):
# no composed layout or mix is priced from a partial capture. The seven a default grid run measures (layouts.SEVEN)
DIAGONAL_SF30 = grid_capture.SEVEN
assert set(DIAGONAL_SF30) == {"props_split_adj_split", "props_condensed_adj_condensed", "props_chunked_adj_chunked",
                              "props_chunked_c1k_adj_chunked_c1k", "props_chunked_c4k_adj_chunked_c4k",
                              "props_chunked_c16k_adj_chunked_c16k", "props_chunked_c256k_adj_chunked_c256k"}, DIAGONAL_SF30
NOT_RUN = NOT_RUN_AT_SF30       # a row's error prefix for a body the rule did not run (Choose: unmeasured, not invalid)
# a CHECK output, not a measurement (run.emit.row_digest): each KV step's output rows as a digest, in order and with the
# order ignored, in its own file per cell (row-hashes/<cell>.csv) so the shared step record keeps its columns.
# tests/gates/meta/check_row_order.py compares them across cells: what flows between steps must not depend on the layout
DIGEST_KEYS = ["rows_digest", "rows_set_digest"]
DIGEST_COLS = ["workload", "query", "param_idx", "body", "step_idx", "step", "op", "rows", *DIGEST_KEYS]


# the store manifest's fields a capture record keeps (same_store compares key_codec, value_codec_hash and the index +
# filter layout; the block codec and filter are kept, not compared: they change sizes and times, never counts, and each
# record takes sst_bytes from the store it ran on): the one list both record writers and check_stale_store_not_reused.py use
STORE_MANIFEST_KEYS = ("key_codec", "value_codec_hash", "keys", "sst_bytes", "ingest_counts", "index_filter_blocks",
                       "compression", "bloom_bits_per_key")


def journal_dir(out_dir):
    """where a capture's cells in flight are journaled: output/logs/journals/<the capture root's path below output/> (a root
    elsewhere: its absolute path, flattened). Never inside the capture root: the checks and cost tables glob it
    recursively."""
    out = os.path.abspath(out_dir)
    base = str(Tree().root) + os.sep
    rel = out[len(base):] if out.startswith(base) else out.strip(os.sep).replace(os.sep, "__")
    return os.path.join(str(Tree().journals()), rel)


class Journal:
    """A cell in flight, on disk: every finished run (query, param, variant) is appended to its journal (journal_dir) as it
    ends -- its step rows and part rows first, its runs row LAST, each flushed -- so a run that has a runs row is
    complete on disk and a killed cell (a cancel, the RSS stop) resumes after its last finished run. The journal lives
    beside the capture, never in it: the capture's counts/<cell>.csv appears only when the cell is done (write_cell), so no reader can
    take a cell in flight for a capture. <journal>/runs/<cell>.json holds the key the rows were measured under
    ("complete": false); a restart resumes only under an equal key, else the partial files are moved aside (to
    <journal>/_aside-<stamp>/). ONE process per cell: the journal is locked for the cell's whole measurement
    (<journal>/runs/<cell>.lock), a second process on it is refused at once -- two appenders would double step rows."""

    FILES = (("runs", RUN_COLS), ("steps", STEP_COLS + DIGEST_KEYS), ("parts", PART_COLS))   # the journal's step rows carry the digests

    def __init__(self, out_dir, cell, key, fresh):
        self.dir, self.cell, self.key = journal_dir(out_dir), cell, key
        os.makedirs(os.path.join(self.dir, "runs"), exist_ok=True)
        self.lock = open(os.path.join(self.dir, "runs", f"{cell}.lock"), "w")   # kept: a lock file outlives its holder
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(f"[grid_bodies] {cell}: another process is measuring this cell into {os.path.relpath(out_dir)} "
                             f"(its journal {os.path.relpath(self.dir)} is locked): one process per cell")
        self.loaded, self.wall_before, self.resumed = {}, 0.0, 0
        self.paths = {k: os.path.join(self.dir, k, f"{cell}.csv") for k, _ in self.FILES}
        self.side = os.path.join(self.dir, "runs", f"{cell}.json")
        for k, _ in self.FILES:
            os.makedirs(os.path.join(self.dir, k), exist_ok=True)
        if os.path.exists(self.side):
            rec = json.load(open(self.side))
            if not fresh and rec.get("key") and rec["key"] == key:
                self._load()
                self.wall_before, self.resumed = float(rec.get("wall_s_so_far", 0)), int(rec.get("resumed", 0)) + 1
            else:
                self._move_aside("--fresh" if fresh else "its key differs from this run's")
        elif any(os.path.exists(p) for p in self.paths.values()):
            self._move_aside("it has no key file")
        runs = [x[0] for x in self.loaded.values()]
        rows = {"runs": runs, "steps": [r for x in self.loaded.values() for r in x[1]],
                "parts": [r for x in self.loaded.values() for r in x[2]]}
        self.f, self.w = {}, {}
        for k, cols in self.FILES:          # rewritten from the complete runs (drops an interrupted run's rows, a torn line)
            self.f[k] = open(self.paths[k], "w", newline="")
            self.w[k] = csv.DictWriter(self.f[k], fieldnames=cols, restval="")
            self.w[k].writeheader(); self.w[k].writerows(rows[k]); self.f[k].flush()
        self.note_wall(0.0)
        if self.loaded:
            print(f"[grid_bodies] {cell}: RESUMED from {os.path.relpath(self.dir)}: {len(self.loaded)} finished runs kept "
                  f"({self.wall_before:.0f}s measured before), the rest is measured now", flush=True)

    @staticmethod
    def _read(path, whole_lines_only=False):
        if not os.path.exists(path):
            return []
        with open(path, newline="") as f:
            rows = [r for r in capture_layout.reader(f)]
        if whole_lines_only and rows and not open(path, "rb").read().endswith(b"\n"):
            rows = rows[:-1]                # a torn last line
        return [r for r in rows if None not in r and None not in r.values()]

    def _load(self):
        runs = self._read(self.paths["runs"], whole_lines_only=True)
        k3 = lambda r: (r["query"], str(r["param_idx"]), r["body"])
        by = {k3(r): (r, [], []) for r in runs}
        for kind, slot in (("steps", 1), ("parts", 2)):
            for r in self._read(self.paths[kind]):
                if k3(r) in by:             # rows of a run with no runs row = the interrupted run: dropped
                    by[k3(r)][slot].append(r)
        self.loaded = by

    def _move_aside(self, why):
        aside = os.path.join(self.dir, f"_aside-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}")
        for k, _ in self.FILES:
            for path in (self.paths[k],) + ((self.side,) if k == "runs" else ()):
                if os.path.exists(path):
                    os.makedirs(os.path.join(aside, k), exist_ok=True)
                    os.replace(path, os.path.join(aside, k, os.path.basename(path)))
        print(f"[grid_bodies] {self.cell}: a partial capture is NOT resumed ({why}); moved aside to {aside}", flush=True)

    def add(self, row, srows, prows):
        self.w["steps"].writerows(srows); self.f["steps"].flush()
        self.w["parts"].writerows(prows); self.f["parts"].flush()
        self.w["runs"].writerow(row); self.f["runs"].flush()

    def note_wall(self, elapsed):
        tmp = self.side + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"cell": self.cell, "complete": False, "key": self.key, "resumed": self.resumed,
                       "wall_s_so_far": round(self.wall_before + elapsed, 1)}, f, indent=2)
        os.replace(tmp, self.side)

    def release(self):
        """the cell stopped before its capture (CellAbort): the journal stays as it is, for a later run to resume; its
        files and its lock are let go"""
        for f in self.f.values():
            f.close()
        self.lock.close()

    def finish(self):
        """the cell's capture is written: the journal is no longer needed"""
        for f in self.f.values():
            f.close()
        for path in list(self.paths.values()) + [self.side]:
            if os.path.exists(path):
                os.remove(path)


def resume_key(workload, sf, nparam, P, binary_ver, store, spec_path, inputs, exec_mode, slice_, timeout=None, picks=None,
               cell_inputs=None):
    """what a journal's rows depend on: a restart resumes them only when every part is equal"""
    manifest = json.load(open(os.path.join(store, "manifest.json")))
    key = {"workload": workload, "sf": str(sf), "nparam": nparam, "P": P, "inputs": inputs, "cell_inputs": cell_inputs,
           "exec": exec_mode,
           "timeout": timeout,                      # a run recorded "timed out" is not kept under a longer timeout
           "measurement_version": CAPTURE_SHAPE, "slice": {"queries": slice_[0], "params": slice_[1]},
           "spec": spec_json(spec_path),
           "store_manifest": {k: manifest.get(k) for k in STORE_MANIFEST_KEYS},
           "store_content": store_identity.store_content_identity(manifest),
           "binary_git_sha": binary_ver.get("binary_git_sha"), "binary_dirty": binary_ver.get("binary_dirty")}
    if picks:                                       # only a picks-only run has it: every other key stays as it was
        key["picks"] = picks
    return key


def pick_runs(q, name, ir, cands):
    """the runs of a picks-only cell for query q: the recorded body `name` alone, forced (a list of names, --variants:
    each of them, in that order). A single-plan query (no bundle: a body named by its query, no candidate of its own) is
    its one run."""
    if isinstance(name, list):
        return [run for n in name for run in pick_runs(q, n, ir, cands)]
    for cand, cand_ir in cands:
        if cand == name:
            return [(cand, cand_ir)]
    if "plans" not in ir and name == ir.get("plan", q):
        return [(name, ir)]
    raise SystemExit(f"{q}: the recorded pick {name!r} is not a candidate of HEAD's plan files ({[c for c, _ in cands]})")


def bodies_as_picks(workload, queries, names):
    """{query: the names of `names` that are candidates of the query, in candidate order} (--variants); one name is kept
    as a plain name (the --picks form); refuses a query with none, and a name that is no sliced query's candidate"""
    out, used = {}, set()
    for q in queries:
        mine = [n for n, _ in candidates(workload, q)[1] if n in names]
        if not mine:
            raise SystemExit(f"--variants: {q} has none of the listed candidates; at least one is needed")
        out[q] = mine[0] if len(mine) == 1 else mine
        used.update(mine)
    if set(names) - used:
        raise SystemExit(f"--variants: {sorted(set(names) - used)} is no candidate of {sorted(queries)}")
    return out


def request(store, schema, ir, params, P, fmt="meta"):
    # KVEVAL_LABEL_TAGS (a control's label tags) reaches the run as it reaches every other request (server.with_label_tags)
    return {"store": {"dir": store, "schema": schema, "format": fmt}, "plan": ir, "params": params,
            "run": server.with_label_tags({"P": P, "parts": fmt == "meta",    # parts: each op also attributed to the store part it touched (meta)
                    # a counts run: a step's rows are freed after their last reader (peak memory only). Not in the
                    # capture's key: no count, byte or row depends on it (the executor's on / off proof)
                    "free_rows": FREE_ROWS,
                    "perf_level": 2,          # RocksDB counts without its per-operation timers (nothing here reads one)
                    "emit": {"histograms": False,     # no RocksDB histogram snapshots (~2.7 ms per request; nothing here reads them)
                             "row_digest": True}})}   # the rows each step hands on, as a digest (a check; no count reads it)


INVALID = INVALID_HERE + ": "                      # a row's error prefix for a body never run on this layout
INFRA_WHERE = ("store", "lsm_state", "request")   # cli/session.cc: the stages before a plan runs


def ran_and_not_offered(rows):
    """(the runs that ran, the rows of bodies not offered on this layout) of counts rows: a body the layout lacks the
    caps for is written as a row and never run (invalid_error), so it is counted apart from the runs"""
    out = sum(str(r.get("error") or "").startswith(INVALID) for r in rows)
    return len(rows) - out, out


def say_runs(rows):
    """'6 of 6 runs ok, 2 not offered on this layout': a run's ok counted over the runs that ran"""
    ran, out = ran_and_not_offered(rows)
    ok = sum(int(r["ok"]) for r in rows)
    return f"{ok} of {ran} runs ok" + (f", {out} not offered on this layout" if out else "")


def invalid_error(lacks):
    """the error of a run row whose body is invalid on its cell: never run, the caps it lacks named"""
    return f"{INVALID}lacks validity caps {lacks}"


def rows_offered_today(workload, spec_path, rows):
    """A capture's counts rows as a run today writes them on its cell (`spec_path`), for a converter (run/convert-
    paper-results.py, step 15): a row of a body that is no candidate of its query today is left out (no run writes
    it), and an ok row of a body invalid on the cell today becomes the row measure_runs writes for one (ok 0,
    invalid_error; a composed row keeps measured_as "composed", as compose_cells copies its source's). Every other row
    as it is, in its order. -> (rows, {(query, param_idx, body): "dropped" | "invalid"}) for the steps, parts and row
    hashes to follow (they hold no row of either)"""
    from kvevaluator.plan_choice.candidates import current_name
    invalid = invalid_on_cell(workload, spec_path)
    heads, out, fate = {}, [], {}
    for r in rows:
        q = r["query"]
        if q not in heads:
            heads[q] = candidate_bodies(workload, q)
        now = current_name(workload, q, r["body"], r.get("body_sha"), heads[q])
        k = (q, str(r["param_idx"]), r["body"])
        if now not in heads[q]:
            fate[k] = "dropped"
        elif now in invalid.get(q, {}) and str(r["ok"]) == "1":
            fate[k] = "invalid"
            out.append({**dict.fromkeys(r, ""), "sf": r["sf"], "cell": r["cell"], "query": q, "param_idx": r["param_idx"],
                        "body": r["body"], "measure_only": r.get("measure_only", ""), "ok": "0",
                        "error": invalid_error(invalid[q][now])[:200],
                        "measured_as": "composed" if r.get("measured_as") == "composed" else ""})
        else:
            out.append(r)
    return out, fate


def infra_error(err):
    """True when a run's error says the store or the session is wrong for every run of the cell, not a plan's result:
    the session failed before the plan (`error (store|lsm_state|request): ...`). An invalid body, a timeout, a plan or
    run error and a crash stay rows (a crash on the cell's first run: store_opens decides)."""
    return any(err.startswith(f"error ({w})") for w in INFRA_WHERE)


def store_opens(store):
    """(opens, why): the store opened the way a run opens it (stores.store_staleness's probe). Asked after the
    evaluator died on a cell's first run: a store that crashes on open stops the cell, a plan that crashes is a row."""
    from kvevaluator.measure import stores
    verdict, why = stores.store_staleness(store, layout=False, probe=True)
    return verdict == stores.USABLE, why


class CellAbort(Exception):
    """a run hit an infra_error: the cell stops with no capture written, so a later run cannot reuse it"""


class SpawnRunner:
    """One evaluator process (and store open) per run."""

    def __init__(self, binary, store, schema, timeout, fmt="meta"):
        self.binary, self.timeout = binary, timeout
        self.died = False                   # the last call's process crashed (not a session error, not a timeout)

    def call(self, req):
        self.died = False
        try:
            r = subprocess.run([self.binary, "run", "-"], input=json.dumps(req), capture_output=True, text=True,
                               timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return None, f"timeout {self.timeout}s"
        if r.returncode != 0:
            msg = (r.stderr or r.stdout).strip().splitlines()
            self.died = not (msg and msg[-1].startswith("error ("))
            return None, msg[-1] if msg else "nonzero exit"
        resp = json.loads(r.stdout)
        return (None, resp["error"]) if resp.get("error") else (resp, "")

    def close(self):
        pass


class ServeRunner:
    """Every run of a cell over one `kvevaluator serve` (server.CppServer): one store open, a per-run timeout;
    a timed-out or dead server is killed and restarted for the next run."""

    def __init__(self, binary, store, schema, timeout, fmt="meta"):
        self.store, self.schema, self.timeout, self.fmt = store, schema, timeout, fmt
        self.server = None
        self.died = False                   # the last call's server crashed (not a session error, not a timeout)

    def call(self, req):
        self.died = False
        if self.server is None:
            self.server = server.CppServer(self.store, fmt=self.fmt, schema=self.schema).__enter__()
        proc = self.server.proc
        try:
            proc.stdin.write((json.dumps(req) + "\n").encode())
            proc.stdin.flush()
        except BrokenPipeError:   # the server died between runs
            return self._died()
        if not select.select([proc.stdout], [], [], self.timeout)[0]:
            proc.kill()
            self.close()
            return None, f"timeout {self.timeout}s"
        line = proc.stdout.readline()
        try:
            resp = json.loads(line) if line else None
        except json.JSONDecodeError:   # a partial line: the server crashed mid-write
            resp = None
        if resp is None:
            return self._died()
        if resp.get("error"):
            e = resp["error"]   # {"where", "what"}: the same text spawn's `run` prints on its last stderr line
            return None, f"error ({e.get('where')}): {e.get('what')}" if isinstance(e, dict) else str(e)
        return resp, ""

    def _died(self):
        tail = self.server._stderr_tail().strip().splitlines()
        self.died = True
        self.close()
        return None, tail[-1] if tail else "serve died"

    def close(self):
        if self.server is not None:
            self.server.close()
            self.server = None


RUNNERS = {"serve": ServeRunner, "spawn": SpawnRunner}


def query_runs(workload, q):
    """what q's candidates execute, by content, for the freshness key: {candidate name: body sha} (candidates.
    candidate_bodies: the C++ body sha, fragments expanded), each plan file's plan-level fields (kvevaluator.plans.ir.bundle.plan_level), and
    the bundle-wide measure_only bodies (the rows' flag). A file edit that moves none of these (a variant's `requires`,
    `on`, `scales`, the doc fields) leaves it equal; which body a cell selects is cell_fingerprint's half."""
    files = {}
    for path in plan_files(workload, q):
        ir = json.load(open(path))
        files[os.path.splitext(os.path.basename(path))[0]] = plan_level(ir)   # bind / let / params, in file order
    return {"bodies": candidate_bodies(workload, q), "plan_level": files,
            "measure_only": sorted(measure_only_bodies(workload, q))}


def inputs_fingerprint(workload, sf, nparam, params=None, queries=None, param_set=None):
    """sha of what a capture depends on besides the store, the cell's selection (cell_fingerprint) and the code: what
    every query's candidates run (query_runs: body shas, not plan files, so a plan-file edit that leaves the bodies
    unchanged re-measures nothing), the params each query runs on (the resolved arg dicts,
    so a regenerated or overridden param file re-measures exactly when a used row changes) and the schema file.
    `params`: the 1-based param indices a slice runs (the held-out set's, candidates.HELD_OUT_PARAMS): THOSE rows are hashed, not the first
    `nparam`. `queries`: a slice's queries, the only ones it hashes, so a slice of one query stays fresh when another
    query is added to the workload"""
    h = hashlib.sha256()
    queries = list(queries) if queries else workload_queries(workload)[0]
    path = schema_file(workload)
    h.update(_D.get(_W.get(workload).dataset).capture_key_schema.encode())   # the schema's fixed name, not its path
    with open(path, "rb") as f:
        h.update(f.read())
    for q in queries:
        h.update(json.dumps([q, query_runs(workload, q)], sort_keys=True).encode())
    for q in queries:
        if params:
            allp = query_params(workload, sf, q, max(params), param_set)
            rows = [allp[i - 1] for i in sorted(params) if i - 1 < len(allp)]
        else:
            rows = query_params(workload, sf, q, nparam, param_set)
        h.update(json.dumps([q, sorted(params) if params else nparam, rows], sort_keys=True).encode())
    return h.hexdigest()[:16]


def sliceable_queries(workload):
    """the queries a slice may name: every query with a plan file in the workload's plan dirs (one someone added too),
    less the scan twins (their bodies are their base's candidates)"""
    from kvevaluator.measure.workload_plans import all_plans
    _, alts = workload_queries(workload)
    return [q for q in all_plans(workload) if q not in alts]


def cell_runs(workload, sf, spec_path, store, queries=None):
    """what ONE cell runs of the workload's candidates, per query: the candidates invalid on it with the caps each
    lacks (never run; the row's error names them) and its measure-only bodies"""
    queries = queries or workload_queries(workload)[0]
    invalid = invalid_on_cell(workload, spec_path)
    return {q: {"invalid": invalid.get(q, {}),
                "measure_only": sorted(measure_only_on_cell(workload, q, spec_path))} for q in queries}


def _scores(rows):
    """{body: the SF30 criterion's score, mean over its ok rows} of one query's run rows"""
    from kvevaluator.plan_choice.candidates import vector
    from kvevaluator.plan_choice.optimizer.criteria import parse_criterion, resolve_criterion
    by = {}
    for r in rows:
        if str(r["ok"]) == "1":
            by.setdefault(r["body"], []).append(r)
    if not by:
        return {}
    scores = parse_criterion(resolve_criterion(SF30_CRITERION)[1]).scores({v: vector(rs) for v, rs in by.items()})
    return {v: scores[v] / len(by[v]) for v in by}


def _store_caps(spec_path):
    """(caps, caps per label) of a cell or a named store, as the pick record's resolver reads them"""
    from kvevaluator.datasets.ldbc.schema import LDBC_SCHEMA
    from kvevaluator.formats.caps import caps_for, caps_resolver
    if format_of(spec_path):
        from kvevaluator.plan_choice.optimizer.arms import caps_of, resolver_of
        return caps_of(format_of(spec_path), LDBC_SCHEMA), resolver_of(format_of(spec_path), LDBC_SCHEMA)
    res = caps_resolver("meta", LDBC_SCHEMA, spec=spec_path)
    labels = sorted(set(LDBC_SCHEMA.vertex_props) | set(LDBC_SCHEMA.etypes))
    of = {l: frozenset(res(l)) for l in labels}
    return frozenset(caps_for("meta", LDBC_SCHEMA, spec=spec_path)), (lambda label: of.get(label, frozenset()))


def _ratio(a, b):
    return f"{a / b:.2f}x" if b else ("1.00x" if a == b else "inf x")


def sf30_admission(workload, cell, spec_path, queries, sf3_rows, prior_rows, record_name=None):
    """{query: {body: (runs, the condition or the reason)}} under the SF30 rule (SF30_T above): `sf3_rows` the store's
    SF3 run rows, `prior_rows` its earlier SF30 capture's (a measure-only body runs the first time only).
    `record_name`: the store's name in the pick record (plan_runs.record_store_key: pertype_csr-c64k for its 64 KB
    store, dgraph-c512k for Dgraph's 512 KB one); default the cell"""
    from kvevaluator.plan_choice import pick_record
    from kvevaluator.plan_choice.candidates import current_name
    caps, of_label = _store_caps(spec_path)
    invalid = invalid_on_cell(workload, spec_path)
    # the captured rows' bodies as named today (a body renamed, or rewritten count-equal: candidates.successor_sha)
    sf3_rows, prior_rows = ([{**r, "body": current_name(workload, r["query"], r["body"], r.get("body_sha"))}
                             for r in rows if r["query"] in queries] for rows in (sf3_rows, prior_rows))
    out = {}
    for q in queries:
        ir, cands = candidates(workload, q)
        names = [n for n, _ in cands] or [ir.get("plan", q)]
        if cell in DIAGONAL_SF30:
            out[q] = {n: (True, "(iii) a diagonal cell runs every body") for n in names}
            continue
        mo = measure_only_bodies(workload, q) | measure_only_on_cell(workload, q, spec_path)
        cost = _scores([r for r in sf3_rows if r["query"] == q])
        # any prior row counts as seen (a run, or a not-run row): a body dropped once must not count as new again
        seen = {r["body"] for r in prior_rows if r["query"] == q}
        try:
            pick, how = (pick_record.body_for(workload, record_name or cell, q, ir, caps, of_label, "3"),
                         "the SF3 pick (pick record)")
        except ValueError:                        # no record entry: the cheapest SF3 body stands in, said so
            pick, how = (min(cost, key=cost.get) if cost else None), "the SF3 cheapest (no record entry)"
        adm = {}
        for n in names:
            if n in invalid.get(q, {}):
                adm[n] = (True, "invalid here: recorded as such, never run")
            elif n == pick:
                adm[n] = (True, how)
            elif pick not in cost:
                adm[n] = (True, "the pick has no SF3 cost: every body runs")
            elif n not in cost:
                adm[n] = (True, "new: no ok SF3 row on this store")
            elif n in mo and n not in seen:
                adm[n] = (True, "measure-only: the first SF30 capture to see it")
            elif cost[n] <= SF30_T * cost[pick]:
                adm[n] = (True, f"within T: {_ratio(cost[n], cost[pick])} the pick at SF3")
            else:
                adm[n] = (False, f"{_ratio(cost[n], cost[pick])} the pick's SF3 cost, over T = {SF30_T:g}")
        out[q] = adm
    return out


def close_at_sf30(run_rows, q, adm, mo=()):
    """condition (ii): the reason to run the rest when, on SF30's own rows, the pick is within SF30_CLOSE of its nearest
    measured rival that can be picked (not measure-only `mo`), or when the pick has no ok row at SF30; None otherwise"""
    picks = [n for n, (_r, why) in adm.items() if why.startswith(("the SF3 pick", "the SF3 cheapest"))]
    sc = _scores([r for r in run_rows if r["query"] == q])
    if not picks:
        return None
    if picks[0] not in sc:
        return "(ii) the pick has no ok run at SF30: the rest run"
    rivals = {n: v for n, v in sc.items() if n != picks[0] and n not in mo}
    if not rivals:
        return None
    near = min(rivals, key=rivals.get)
    if rivals[near] <= (1 + SF30_CLOSE) * sc[picks[0]]:
        return f"(ii) at SF30 the pick is within {SF30_CLOSE:.0%} of {near} ({rivals[near] / sc[picks[0]]:.3f}x)"
    return None


def answer_splits(run_rows):
    """[(query, param, {answer_key: [bodies]})] where the ok runs of one (query, param) do not give one answer: every
    legal body must return the same answer (candidates.result_key), so a split refuses the capture"""
    by = {}
    for r in run_rows:
        if str(r["ok"]) == "1":
            # int: a row taken from the journal or a prior capture carries it as read from a CSV (a string)
            by.setdefault((r["query"], int(r["param_idx"])), {}).setdefault(r["answer_key"], []).append(r["body"])
    return [(q, i, {k: sorted(v) for k, v in sorted(ans.items())}) for (q, i), ans in sorted(by.items()) if len(ans) > 1]


def cell_fingerprint(workload, sf, spec_path, store, queries=None):
    """sha of cell_runs: the cell's half of the freshness key. A variant's `requires` / `on` / `scales` edit that
    changes what this cell selects, refuses or flags re-measures it; one that does not leaves it fresh."""
    per_q = cell_runs(workload, sf, spec_path, store, queries)
    return hashlib.sha256(json.dumps(per_q, sort_keys=True).encode()).hexdigest()[:16]


def sf3_rows_dir(workload, sf3_root):
    """the SF3 capture's counts dir the SF30 rule prices from (below the tree `sf3_root`): LDBC's held-out rows, the
    others' own"""
    held = _W.get(workload).pick_params == HELD_OUT
    return Tree(sf3_root).capture(workload, "3", HELD_OUT if held else None).dir("counts")


def sibling_capture(out_dir):
    """LDBC's other SF30 capture of the same stores: the reported one <-> the held-out one"""
    cap = Capture(out_dir)
    return os.path.join(os.path.dirname(os.path.normpath(cap.path)), REPORTED if cap.params() == HELD_OUT else HELD_OUT)


def held_out_close(workload, out_dir, cell, sf3_path, sf3_inputs, adm_by_q, store, spec_path, fmt):
    """(where condition (ii) is decided for this SF30 capture, the queries whose (ii) the held-out capture decided), and
    (LDBC's reported capture) the bodies it admits: LDBC's two SF30 captures run one set of bodies, so (ii) is decided
    once, on the held-out capture's rows (the pick rule's set; kvcost measure runs it first), and the reported capture
    takes those decisions when the held-out capture is complete, on this store at this capture shape and counting, under the
    same T and close, and priced from the same SF3 rows. Only (ii) decisions are taken (never another adoption): no
    decision feeds back. A query the held-out capture did not measure decides its (ii) on this capture's rows."""
    if _W.get(workload).pick_params != "held-out" or Capture(out_dir).params() == HELD_OUT:
        return "this capture", set()
    side = Capture(sibling_capture(out_dir)).record(cell)
    rec = json.load(open(side)) if os.path.exists(side) else {}
    rule = rec.get("sf30_rule")
    why_not = ("no held-out SF30 capture" if not rule
               else "it is not complete" if rec.get("complete") is not True
               else "another capture shape" if rec.get("measurement_version") != CAPTURE_SHAPE
               else "other counting versions" if not counted_fresh(rec, store)
               else "another rule (T, close or criterion)" if (rule.get("T"), rule.get("close"), rule.get("criterion"))
               != (SF30_T, SF30_CLOSE, SF30_CRITERION)
               else "other SF3 rows" if (recorded_path(rule.get("sf3_capture") or "", Tree().root)
                                       != recorded_path(sf3_path, Tree().root)) or rule.get("sf3_inputs") != sf3_inputs
               else "another store" if not same_store(rec, store, spec_path, workload, fmt)
               else None)
    if why_not:
        print(f"[grid_bodies] {cell}: the held-out SF30 capture ({side}) is not taken ({why_not}): condition (ii) is "
              f"decided on this capture's rows", flush=True)
        return f"this capture (held-out capture not taken: {why_not})", set()
    decided = set()
    for q, adm in adm_by_q.items():
        if q not in rule.get("admitted", {}):
            continue
        decided.add(q)
        for n, (runs, _w) in list(adm.items()):
            held = rule["admitted"][q].get(n, {})
            if not runs and held.get("ran") and held.get("why", "").startswith("(ii)"):
                adm[n] = (True, f"{held['why']} (decided on the held-out capture)")
    return f"the held-out capture ({os.path.relpath(side, str(Tree().root))})", decided


def _read_rows(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(capture_layout.reader(f))


def row_run_key(body, params):
    """one run's reuse key: what it executes (body_key), the params it runs on, and the capture shape (the counting
    versions are compared once per record, beside it: prior_runs)"""
    return hashlib.sha256(json.dumps([body, params, CAPTURE_SHAPE], sort_keys=True).encode()).hexdigest()[:16]


def reuse_matches(workload, q, prev_row, key, params):
    """(is a prior run row this run, the old body sha it ran or None): its run_key equals this run's, or it ran a body
    the count-equal successor table maps to this one (candidates.successor_sha) with these params and plan-level fields
    -- its key rebuilt from its own body_sha and checked, so only the sha may differ"""
    from kvevaluator.plan_choice.candidates import successor_sha
    if key is None or not prev_row.get("run_key"):
        return False, None
    if prev_row["run_key"] == row_run_key(key, params):
        return True, None
    old = prev_row.get("body_sha")
    sha, level = json.loads(key)
    if (old and old != sha and successor_sha(workload, q, old) == sha
            and prev_row["run_key"] == row_run_key(json.dumps([old, level], sort_keys=True), params)):
        return True, old
    return False, None


def counted_fresh(rec, store):
    """a grid record was counted under the binary's versions for the store's format (measure/counting.py)"""
    with open(os.path.join(store, "manifest.json")) as f:
        fmt = json.load(f)["format"]
    return counting.counting_fresh(rec, fmt, "read", "grid")[0]


def same_store(rec, store, spec_path, workload, fmt):
    """a capture record was measured on this store as it is now (key layout, value codec, content, index + filter
    layout; a meta cell's store is one this binary would build for the spec). The block codec and filter are not
    compared: the counts are taken above RocksDB, so a store rebuilt with another codec or filter reuses them"""
    mpath = os.path.join(store, "manifest.json")
    if not os.path.exists(mpath):
        return False
    m = json.load(open(mpath))
    if not all(rec.get("store_manifest", {}).get(k) == m.get(k) for k in ("key_codec", "value_codec_hash")):
        return False
    if rec.get("store_content") != store_identity.store_content_identity(m):
        return False
    if store_layout.of_manifest(rec["store_manifest"]) != store_layout.of_manifest(m):
        return False
    return bool(fmt) or store_is_current(store, spec_path, workload, layout=False)


UNCLEAN_MARKS = ("binary_unclean", "binary_behind_head")


def clean_binary(rec):
    """a capture record whose runs a clean binary measured: stamped, not dirty, and not marked unclean or behind HEAD
    (kvevaluator.stale); the binaries of the runs it took from earlier captures (reused_runs) clean too"""
    b = rec.get("binary") or {}
    took = (rec.get("reused_runs") or {}).get("measured_by", [])
    return (bool(b.get("binary_git_sha")) and b.get("binary_dirty") is False
            and all(m.get("binary_git_sha") and m.get("binary_dirty") is False for m in took)
            and not {n.get("kind") for n in rec.get("stale", [])} & set(UNCLEAN_MARKS))


def taken_from_prior(out_dir, cell, workload, sf, store, spec_path, fmt, picks=None, fresh_start=False):
    """(reuse, prior rows, prior record) a run of this cell may take from its prior capture, at every scale
    (prior_runs); nothing for --fresh or a --picks run"""
    if fresh_start or picks:
        return {}, [], None
    return prior_runs(out_dir, cell, workload, store, spec_path, fmt)


def reused_record(prior_rec, reused, measured):
    """(the record's reused_runs, the marks it carries) of a run that took `reused` runs from `prior_rec` and measured
    `measured`: reused_runs = {runs, measured, measured_by}: measured_by lists the binaries (git sha, dirty flag) that
    measured the runs taken, across every earlier reuse (the prior record's own binary when it measured any run, and
    the binaries it lists itself); the marks are the prior record's stale and adopted notes, which its runs carry. (None,
    []) when nothing was taken"""
    if not reused or not prior_rec:
        return None, []
    took = (prior_rec.get("reused_runs") or {})
    by = list(took.get("measured_by", []))
    if not took or took.get("measured", 1):           # a record with no reused_runs measured every run it holds
        b = prior_rec.get("binary") or {}
        by.append({"binary_git_sha": b.get("binary_git_sha"), "binary_dirty": b.get("binary_dirty")})
    by = [dict(t) for t in sorted({tuple(sorted(m.items())) for m in by})]
    return ({"runs": reused, "measured": measured, "measured_by": by},
            list(prior_rec.get("stale", [])) + list(prior_rec.get(stale.ADOPTED, [])))


def prior_runs(out_dir, cell, workload, store, spec_path, fmt):
    """(reuse, prior rows, the prior record) of the cell's earlier capture, or ({}, [], None) when it was not measured
    on this store at this capture shape and counting (or it is a --picks capture). THE reuse rule of a cell whose key
    moved (moved_parts): reuse holds the measured runs a new run may take as they are,
    {(query, param_idx, variant): (run row, its step rows with their digests, its part rows)}, ok rows with a run_key;
    each is taken only when its run_key equals the run's, so an added or changed body, or a changed param row, is
    measured, and a body no longer a candidate here (removed, or invalid on the cell now) is not taken: the capture is
    written again as this run's, without its rows. A prior capture whose binary was not clean (dirty or unstamped, or
    its record marked binary_unclean or binary_behind_head) gives nothing: its runs are measured again. --fresh takes
    none. At SF30, WHICH bodies run is decided again from the current SF3 capture every time. prior rows: every row
    (not-run ones too), for `seen`"""
    side = Capture(out_dir).record(cell)
    if not os.path.exists(side):
        return {}, [], None
    rec = json.load(open(side))
    if (rec.get("complete") is False or rec.get("measurement_version") != CAPTURE_SHAPE or rec.get("picks")
            or not clean_binary(rec)
            or not same_store(rec, store, spec_path, workload, fmt) or not counted_fresh(rec, store)):
        return {}, [], None
    key = lambda r: (r["query"], str(r["param_idx"]), r["body"])
    digests = {key(r) + (r["step_idx"],): r for r in _read_rows(Capture(out_dir).path_of("row-hashes", cell))}
    steps, parts = {}, {}
    for r in _read_rows(Capture(out_dir).path_of("steps", cell)):
        d = digests.get(key(r) + (r["step_idx"],), {})
        steps.setdefault(key(r), []).append({**r, **{k: d.get(k, "") for k in DIGEST_KEYS}})
    for r in _read_rows(Capture(out_dir).path_of("parts", cell)):
        parts.setdefault(key(r), []).append(r)
    rows = _read_rows(Capture(out_dir).path_of("counts", cell))
    # keyed by the body's name today (a body renamed, or rewritten count-equal: candidates.current_name); the rows keep
    # the names they were captured under
    from kvevaluator.plan_choice.candidates import current_name
    now = lambda r: (r["query"], str(r["param_idx"]), current_name(workload, r["query"], r["body"], r.get("body_sha")))
    return ({now(r): (r, steps.get(key(r), []), parts.get(key(r), [])) for r in rows if str(r["ok"]) == "1" and r.get("run_key")},
            rows, rec)


def measure_cell(*a, **k):
    """one cell (_measure_cell), its stale notes kept for its own record only (kvevaluator.stale.scope)"""
    with stale.scope():
        return _measure_cell(*a, **k)


def _measure_cell(cell, workload, sf, nparam, P, timeout, binary, out_dir, inputs, spec_dir=GRID, exec_mode="serve",
                  slice_=(None, None), fresh_start=False, fmt=None, picks=None, sf3_root=None, param_set=None):
    """one cell of a spec set, or (fmt) one named format filed under `cell` = its store's name. At SF30 (`sf3_root`:
    the SF3 capture root) the SF30 rule decides which bodies run (sf30_admission). `param_set`: the params the runs
    bind (None: the workload's default set; datasets/ldbc/params.py PARAM_SETS)"""
    spec_path = format_spec(fmt) if fmt else os.path.join(spec_dir, f"{cell}.json")
    at_sf30 = str(sf) == SF30_TAG
    # an SF30 store is never built by a capture (28-45 min each), a --picks one included: it must be there
    # (ensure_stores builds it on purpose)
    store = meta_store(sf, workload, spec_path, build=not at_sf30)
    if at_sf30 and not os.path.exists(os.path.join(store, "manifest.json")):
        raise SystemExit(f"[grid_bodies] {cell}: no store at {store}: an SF30 store is built on purpose "
                         f"(kvevaluator.measure.ensure_stores), never by a capture")
    schema = schema_file(workload)
    run_rows, step_rows, part_rows = [], [], []
    t0 = time.time()
    ver = json.loads(subprocess.run([binary, "--version"], capture_output=True, text=True).stdout or "{}")
    per_q = cell_runs(workload, sf, spec_path, store, slice_[0])
    cell_inputs = hashlib.sha256(json.dumps(per_q, sort_keys=True).encode()).hexdigest()[:16]   # = cell_fingerprint
    journal = Journal(out_dir, cell, resume_key(workload, sf, nparam, P, ver, store, spec_path, inputs, exec_mode, slice_, timeout, picks,
                                                cell_inputs), fresh_start)
    sf30 = sf30_rec = None
    reuse, close_elsewhere = {}, set()
    # the prior capture's runs, taken by their run_key at every scale (prior_runs)
    reuse, prior, prior_rec = taken_from_prior(out_dir, cell, workload, sf, store, spec_path, fmt, picks, fresh_start)
    if str(sf) == SF30_TAG and not picks:
        if sf3_root is None:
            raise SystemExit(f"[grid_bodies] {cell}: an SF30 capture needs the tree holding SF3 (the SF30 rule)")
        sf3_dir = sf3_rows_dir(workload, sf3_root)
        if not os.path.isdir(sf3_dir):         # a wrong root (or another spec set's) must not run every body silently
            raise SystemExit(f"[grid_bodies] {cell}: no SF3 capture at {sf3_dir} (the SF30 rule reads SF3's capture from "
                             f"the same tree: measure SF3 first)")
        sf3_path = os.path.join(sf3_dir, f"{cell}.csv")
        sf3_rows = _read_rows(sf3_path)
        if not sf3_rows:
            print(f"[grid_bodies] {cell}: no SF3 rows at {sf3_path}: every body is new and runs", flush=True)
        queries = slice_[0] or workload_queries(workload)[0]
        sf30 = sf30_admission(workload, cell, spec_path, queries, sf3_rows, prior,
                              plan_runs.record_store_key(request_format(spec_path), store) if fmt else cell)
        sf3_inputs = (json.load(open(sf3_path[:-4] + ".json")).get("inputs")
                      if os.path.exists(sf3_path[:-4] + ".json") else None)
        close_on, close_elsewhere = held_out_close(workload, out_dir, cell, sf3_path, sf3_inputs, sf30, store,
                                                   spec_path, fmt)
        sf30_rec = {"T": SF30_T, "close": SF30_CLOSE, "criterion": SF30_CRITERION, "sf3_capture": recorded_path(sf3_path, Tree().root),
                    "sf3_inputs": sf3_inputs, "close_decided_on": close_on,
                    "close_on_this_capture": sorted(set(sf30) - close_elsewhere),
                    "reused_from": ({k: prior_rec.get(k) for k in ("binary", "P", "tree")} if prior_rec else None),
                    "admitted": sf30}                # filled in by the run: a second pass's bodies say (ii)
    runner = RUNNERS[exec_mode](binary, store, schema, timeout, request_format(spec_path))
    try:
        tally = {}
        reused = measure_runs(cell, workload, sf, nparam, P, runner, store, schema, spec_path, run_rows, step_rows,
                              part_rows, t0, slice_, journal, picks, sf30, reuse,
                              close_elsewhere=close_elsewhere, param_set=param_set, tally=tally)
    except CellAbort as e:
        journal.release()
        print(f"[grid_bodies] FAIL {cell}: {str(e)[:300]} (no capture written)", flush=True)
        return False
    finally:
        runner.close()
    if splits := answer_splits(run_rows):
        for q, i, ans in splits[:10]:
            print(f"[grid_bodies] {cell} {q} p{i}: the bodies split on the answer: {ans}", flush=True)
        raise SystemExit(f"[grid_bodies] {cell}: {len(splits)} (query, param) answer splits (first: {splits[0][:2]}): "
                         f"the capture is refused (its runs stay in the journal)")
    if sf30_rec is not None:
        sf30_rec["admitted"] = {q: {n: {"ran": r, "why": why} for n, (r, why) in adm.items()} for q, adm in sf30.items()}
        sf30_rec["reused_runs"] = reused          # rows taken from the prior SF30 capture by their run_key
        sf30_rec["dropped"] = {q: sorted(n for n, (r, _w) in adm.items() if not r) for q, adm in sf30.items()
                               if any(not r for r, _w in adm.values())}
    reused_runs, carried = reused_record(prior_rec, reused, tally.get("measured", 0))
    write_cell(cell, workload, sf, nparam, P, binary, out_dir, inputs, spec_path, store, exec_mode,
               run_rows, step_rows, part_rows, t0, slice_, journal, picks, cell_inputs, sf30_rec, param_set,
               reused_runs, carried)
    journal.finish()
    ran = [r for r in run_rows if str(r["ok"]) == "1" or failed_run(r)]
    if ran and not any(str(r["ok"]) == "1" for r in ran):        # the rows are results (invalid, timed out): kept
        print(f"[grid_bodies] FAIL {cell}: none of its {len(ran)} runs succeeded (first error: "
              f"{str(ran[0].get('error', ''))[:200]})", flush=True)
        return False
    return True


def body_key(ir, variant=None):
    """what a run executes, as a key (candidates.run_key: the body's sha and the file's plan-level fields) for the body
    the planner runs (`variant`, or the forced one); two candidates with one key give the same run (the counts are
    deterministic), so the driver measures it once. By content, not by file: a twin file's copy of a base body has
    the base's key. None when the body is not known."""
    from kvevaluator.plan_choice.candidates import run_key
    if "plans" in ir:
        name = variant if variant is not None else next(iter(ir["plans"]))   # a forced IR: its one body
        if name not in ir["plans"]:
            return None
        return run_key(ir, name)
    return run_key(ir, None)


def measure_runs(cell, workload, sf, nparam, P, runner, store, schema, spec_path, run_rows, step_rows, part_rows, t0,
                 slice_=(None, None), journal=None, picks=None, sf30=None, reuse=None, close_elsewhere=frozenset(),
                 param_set=None, under_successor=None, tally=None):
    """Every (query, param, variant) run of one cell through the runner, appended to the three row lists; a slice
    (queries, 1-based param indices) keeps only those, each param at its original index. `picks` ({query: body name}):
    only that body is run per query (--picks). With a `journal`, each run is
    also appended to it as it ends, and a run the journal already holds (a resumed cell) is taken from it, not run:
    the lists end up in the same order as an uninterrupted cell's. `sf30` (sf30_admission): per query, the bodies the
    SF30 rule admits; the rest run in a second pass when the pick is close at SF30 (close_at_sf30), else each gets a
    row per param saying it was not run and why (never a failure: Choose reads it as unmeasured). `reuse`
    (prior_runs): the cell's prior capture's runs, each taken instead of run when its run_key is this run's; at SF30 a
    measure-only body the rule drops keeps its rows when every one is reusable (measured before, nothing moved).
    `close_elsewhere`: the queries whose condition (ii) was decided on the held-out capture (held_out_close), not on
    these rows. `under_successor` (a list): filled with each row emitted that ran a body rewritten count-equal since
    (reused, from the journal, or copied)."""
    reuse = reuse or {}
    from kvevaluator.plan_choice.candidates import successor_sha
    reused = 0
    loaded = journal.loaded if journal is not None else {}
    kept = 0
    calls = 0                                     # runner calls so far: the first one opens the store
    today = {}   # the current query's body name -> its sha today

    def emit(row, srows, prows, on_disk=False):
        run_rows.append(row); step_rows.extend(srows); part_rows.extend(prows)
        if journal is not None and not on_disk:
            journal.add(row, srows, prows)
        # a row that ran a body rewritten count-equal since (reused from a prior SF30 capture, taken from the journal,
        # or copied from such a row) keeps its old body_sha and is named in the record: one place for every path
        old, now = row.get("body_sha"), today.get(row["body"])
        if old and now and old != now and successor_sha(workload, row["query"], old) == now:
            if under_successor is not None:
                under_successor.append({"query": row["query"], "param_idx": int(row["param_idx"]), "body": row["body"],
                                        "old_body_sha": old})
            stale.note("under_successor", f"{cell} {row['query']} p{row['param_idx']} {row['body']}: a row of body {old}")

    def emit_not_run(q, rest, adm, params, only_p, mo):
        for idx, _p in enumerate(params, 1):
            if only_p and idx not in only_p:
                continue
            for name, _ in rest:
                if have := loaded.get((q, str(idx), name)):     # journaled before a restart: not added again
                    emit(*have, on_disk=True)
                    continue
                emit({"sf": sf, "cell": cell, "query": q, "param_idx": idx, "body": name,
                      "measure_only": int(name in mo), "ok": 0,
                      "error": (NOT_RUN + adm[name][1])[:200]}, [], [])

    queries, _ = workload_queries(workload)
    only_q, only_p = slice_
    if only_q:   # the benchmark's queries in their order, then a sliced query it does not list (one someone added)
        queries = [q for q in queries if q in only_q] + [q for q in only_q if q not in queries]
    invalid = invalid_on_cell(workload, spec_path)   # candidates that may answer wrong on this layout: never run
    for q in queries:
        # candidates: every body of the query's bundle, plus every body of its twins (ALTERNATIVES), named
        # "<twin>:<body>" (a single-plan twin: "<twin>") -- the same answer by construction, another access path
        # (a single-plan base IR, no bundle, has no other candidate of its own: it runs under its plan name)
        ir, cands = candidates(workload, q)
        if not cands:
            cands = [(ir.get("plan", q), ir)]
        today.clear()
        today.update({name: json.loads(k)[0] for name, c in cands if (k := body_key(c)) is not None})
        order = query_order(ir)                   # what answer_key compares (candidates.result_key)
        # bodies measured and reported, never picked (the row carries the flag): bundle-wide ones, and the ones that are
        # measure-only on THIS cell (a default probe where another variant's selector holds)
        mo = measure_only_bodies(workload, q) | measure_only_on_cell(workload, q, spec_path)
        bodies = [name for name, _ in cands]
        params = query_params(workload, sf, q, max(only_p) if only_p else nparam, param_set)
        def run_params(run_cands):
            """every param of q for these candidates (a pass), in the canonical order"""
            nonlocal kept, reused, calls
            # a query that declares no param runs the same plans at every param index: measured at the first, copied
            param_free = not ir.get("params")
            first = None                              # the first param index's (run row, its steps, its parts)
            for idx, p in enumerate(params, 1):
                if only_p and idx not in only_p:
                    continue
                if param_free and first is not None:
                    for row, srows, prows in first:
                        have = loaded.get((q, str(idx), row["body"]))
                        if have:
                            emit(*have, on_disk=True); kept += 1
                            continue
                        emit({**row, "param_idx": idx,
                              "measured_as": row.get("measured_as") or f"{row['body']}@p{row['param_idx']}"},
                             [{**r, "param_idx": idx} for r in srows], [{**r, "param_idx": idx} for r in prows])
                    continue
                this = []                             # this param's (run row, steps, parts), in run order
                done_body = {}                        # body key -> the (run row, steps, parts) that measured it
                for variant, cand_ir in run_cands:
                    key = body_key(cand_ir)
                    have = loaded.get((q, str(idx), variant))
                    if have and have[0]["error"].startswith(NOT_RUN):
                        have = None                   # journaled as not run before a restart; admitted now: run it
                    if have:                          # finished before the restart: taken from the journal
                        row = have[0]
                        emit(*have, on_disk=True); this.append(have); kept += 1
                        if str(row["ok"]) == "1" and not row["measured_as"] and key is not None:
                            done_body[key] = have
                        continue
                    if key is not None and key in done_body and variant not in invalid.get(q, {}):
                        # the same body under another name: the same run, copied (validity is per name: checked first)
                        row0, srows0, prows0 = done_body[key]
                        row = {**row0, "body": variant, "measure_only": int(variant in mo),
                               "measured_as": row0.get("measured_as") or f"{row0['body']}@p{row0['param_idx']}"}
                        srows = [{**r, "body": variant} for r in srows0]
                        prows = [{**r, "body": variant} for r in prows0]
                        emit(row, srows, prows)
                        this.append((row, srows, prows))
                        continue
                    rk = row_run_key(key, p) if key is not None else ""
                    prev = reuse.get((q, str(idx), variant))
                    same, _old = reuse_matches(workload, q, prev[0], key, p) if prev else (False, None)
                    if same and variant not in invalid.get(q, {}):
                        # measured by an earlier SF30 run of this cell, same body, params, store and version: taken
                        row, srows, prows = ({**prev[0], "sf": sf, "cell": cell, "body": variant},
                                             [{**r, "body": variant} for r in prev[1]], [{**r, "body": variant} for r in prev[2]])
                        row["measure_only"] = int(variant in mo)
                        # a row of an old body (`_old`) keeps its body_sha; emit names it in the record
                        emit(row, srows, prows); this.append((row, srows, prows)); reused += 1
                        if not row["measured_as"]:
                            done_body[key] = (row, srows, prows)
                        continue
                    if variant in invalid.get(q, {}):
                        resp, err = None, invalid_error(invalid[q][variant])
                    else:
                        resp, err = runner.call(request(store, schema, cand_ir, p, P, request_format(spec_path)))
                        calls += 1
                        if resp is None and infra_error(err):
                            raise CellAbort(err)
                        if resp is None and calls == 1 and getattr(runner, "died", False):
                            opens, why = store_opens(store)
                            if not opens:
                                raise CellAbort(f"{err} (on the cell's first run, and the store does not open: {why})")
                    srows, prows = [], []
                    row = {"sf": sf, "cell": cell, "query": q, "param_idx": idx,
                           "body": variant,
                           "ok": int(resp is not None), "error": err[:200]}
                    row["measure_only"] = int(row["body"] in mo)
                    if resp:
                        steps = [s for s in resp["steps"] if s.get("kind") == "kv"]
                        for o in READ_OPS:
                            row[o] = sum(int(s.get(o, 0)) for s in steps)
                        for b in READ_BYTE_LANES:
                            row[b] = sum(int(s["bytes"].get(b, 0)) for s in steps)
                        row["read_bytes"] = sum(row[b] for b in READ_BYTE_LANES)
                        row["rows"] = len(resp.get("rows") or [])
                        row["result_hash"] = hashlib.sha1(json.dumps(resp.get("rows"), sort_keys=True).encode()).hexdigest()[:16]
                        row["answer_key"] = result_key(resp.get("rows"), order)
                        row["body_sha"] = resp.get("provenance", {}).get("body_sha", "")
                        row["run_key"] = rk
                        row["wall_ns"] = (resp.get("totals") or {}).get("wall_ns", "")
                        row["measured_as"] = ""   # measured here; a copied row names the run it came from
                        for i, s in enumerate(steps):
                            srows.append({"query": q, "param_idx": idx, "body": row["body"], "step_idx": i,
                                          "step": s.get("name", ""), "op": s.get("op", ""), **step_record.fields(s, ""),
                                          **{k: s.get(k, "") for k in DIGEST_KEYS}})
                            # the step split by store part (meta's parts); they must sum to the step exactly
                            parts = s.get("parts", {})
                            if "unknown" in parts:   # a key meta's parts decoder could not name: never expected
                                raise SystemExit(f"{cell} {q} p{idx} step {s.get('name')}: a key decoded to part 'unknown'")
                            for lane in READ_OPS if not format_of(spec_path) else ():   # a named format names no parts
                                got = sum(int(c.get(lane, 0)) for c in parts.values())
                                if got != int(s.get(lane, 0)):
                                    raise SystemExit(f"{cell} {q} p{idx} step {s.get('name')}: parts {lane} {got} != {s.get(lane, 0)}")
                            for part, c in sorted(parts.items()):
                                prows.append({"query": q, "param_idx": idx, "body": row["body"], "step_idx": i,
                                              "step": s.get("name", ""), "op": s.get("op", ""), "part": part,
                                              **{o: c.get(o, 0) for o in READ_OPS},
                                              **{b: c["bytes"].get(b, 0) for b in READ_BYTE_LANES},
                                              **step_record.hist_fields(c),
                                              **step_record.decoded_fields(c.get("decoded"), c)})
                    emit(row, srows, prows)
                    this.append((row, srows, prows))
                    if resp and key is not None:
                        done_body[key] = (row, srows, prows)
                if first is None:
                    first = this

        run_cands = pick_runs(q, picks[q], ir, cands) if picks else cands
        if sf30 is None:
            run_params(run_cands)
        else:                                     # the SF30 rule (sf30_admission): the admitted bodies, then maybe the rest
            adm = sf30[q]
            run_params([c for c in run_cands if adm[c[0]][0]])
            rest = [c for c in run_cands if not adm[c[0]][0]]
            # a measure-only body dropped by the rule that an earlier SF30 run measured, with nothing moved: kept
            kept_mo = [c for c in rest if c[0] in mo and all(
                (prev := reuse.get((q, str(idx), c[0]))) and reuse_matches(workload, q, prev[0], body_key(c[1]), p)[0]
                for idx, p in enumerate(params, 1) if not only_p or idx in only_p)]
            for name, _ in kept_mo:
                adm[name] = (True, "measure-only, measured by an earlier SF30 run with the same key: reused")
            if kept_mo:
                run_params(kept_mo)
                rest = [c for c in rest if c not in kept_mo]
            if rest and q not in close_elsewhere and (why := close_at_sf30(run_rows, q, adm, mo)):
                for name, _ in rest:
                    adm[name] = (True, why)
                run_params(rest)
            else:
                emit_not_run(q, rest, adm, params, only_p, mo)
        done = [r for r in run_rows if r["query"] == q]
        if journal is not None:
            journal.note_wall(time.time() - t0)
        print(f"  {cell} {q} done {queries.index(q) + 1}/{len(queries)}, {time.time() - t0:.0f} s: "
              f"{'the pick only' if picks else str(len(bodies)) + ' variants'}, "
              f"{say_runs(done)}"
              + (f" ({kept} runs so far taken from the journal, not re-run)" if kept else "")
              + (f" ({reused} runs so far taken from the prior capture by their run key)" if reused else ""), flush=True)
    print(f"[grid_bodies] {cell}: {calls} runs measured, {reused} taken from the prior capture by their run key"
          + (f", {kept} from the journal" if kept else ""), flush=True)
    if tally is not None:
        tally.update(measured=calls, reused=reused, journal=kept)
    return reused


def write_cell(cell, workload, sf, nparam, P, binary, out_dir, inputs, spec_path, store, exec_mode,
               run_rows, step_rows, part_rows, t0, slice_=(None, None), journal=None, picks=None, cell_inputs=None,
               sf30_rule=None, param_set=None, reused_runs=None, carried=()):
    """The cell's runs / steps / parts CSVs and its provenance record, written when the cell is DONE (a cell in flight
    lives in its journal, Journal). The rows are in the canonical order whether or not the cell was resumed. The
    record names a non-default param set (`param_set`); the default set's record is as before. `reused_runs` and
    `carried` (reused_record): the runs taken from the prior capture by their run_key, the binaries that measured them,
    and the prior record's stale and adopted marks, folded in"""
    os.makedirs(Capture(out_dir).dir("counts"), exist_ok=True)
    os.makedirs(Capture(out_dir).dir("steps"), exist_ok=True)
    os.makedirs(Capture(out_dir).dir("parts"), exist_ok=True)
    cols = RUN_COLS
    with open(Capture(out_dir).path_of("counts", cell), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, restval="")
        w.writeheader(); w.writerows(run_rows)
    scols = STEP_COLS
    with open(Capture(out_dir).path_of("steps", cell), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=scols, extrasaction="ignore")   # the digests go to their own file, below
        w.writeheader(); w.writerows(step_rows)
    os.makedirs(Capture(out_dir).dir("row-hashes"), exist_ok=True)
    with open(Capture(out_dir).path_of("row-hashes", cell), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=DIGEST_COLS, extrasaction="ignore", restval="")
        w.writeheader(); w.writerows({**r, "workload": workload} for r in step_rows)
    pcols = PART_COLS
    with open(Capture(out_dir).path_of("parts", cell), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=pcols)
        w.writeheader(); w.writerows(part_rows)
    manifest = json.load(open(os.path.join(store, "manifest.json")))
    ver = json.loads(subprocess.run([binary, "--version"], capture_output=True, text=True).stdout or "{}")
    # the commit the PLAN FILES were read from (the working tree's HEAD): it can be newer than a pinned binary's, and
    # the drift checks resolve a captured name's body there (python/kvevaluator/plan_choice/candidates.py capture_tree)
    tree = plan_tree()
    with open(Capture(out_dir).record(cell), "w") as f:
        json.dump(stale.fold(stale.fold({"cell": cell, "sf": sf, "workload": workload, "nparam": nparam, "P": P, "free_rows": FREE_ROWS,
                   "measurement_version": CAPTURE_SHAPE, "counting": counting.stamp(manifest["format"]),
                   "inputs": inputs, "cell_inputs": cell_inputs,
                   "slice": {"queries": slice_[0], "params": slice_[1]}, **({"picks": picks} if picks else {}),
                   "complete": True, "resumed": journal.resumed if journal is not None else 0,
                   "wall_s": round(time.time() - t0 + (journal.wall_before if journal is not None else 0.0), 1), "exec": exec_mode,
                   "spec": spec_json(spec_path),
                   "spec_path": spec_path if format_of(spec_path) else recorded_path(spec_path),
                   "store": recorded_path(store, store_dir()),
                   "store_manifest": {k: manifest.get(k) for k in STORE_MANIFEST_KEYS},
                   "store_content": store_identity.store_content_identity(manifest),
                   "binary": ver, "tree": tree, "plan_pipeline": PLAN_PIPELINE,
                   **({"sf30_rule": sf30_rule} if sf30_rule else {}),
                   **({"reused_runs": reused_runs} if reused_runs else {}),
                   **({"param_set": param_set} if param_set else {})}, store, extra=carried), out_dir, unit=cell), f, indent=2)
    # (folded: the reused runs' marks, this cell's notes, the store's own (<store>/stale.jsonl: a marked stream it was
    # built from), the capture's)
    ran, not_offered = ran_and_not_offered(run_rows)
    print(f"[grid_bodies] {workload} {cell}: {ran} runs" + (f" ({not_offered} more rows: bodies not offered on this layout)"
                                                            if not_offered else "")
          + f", {len(step_rows)} steps ({time.time() - t0:.0f}s)")


def moved_parts(out_dir, cell, workload, sf, nparam, inputs, spec_dir=GRID, slice_=(None, None), fmt=None, picks=None):
    """the parts of a cell's capture key that moved since it was measured: [] when it is fresh (fresh()); None when it
    cannot stand for this run at all (not there or not complete, no store, another workload, params count, slice or
    capture shape). The parts: the bodies or param rows (inputs), the picks, what this cell selects (cell_inputs), the
    store (key layout, value codec, content, index + filter layout, a store this binary would build) and the counting
    versions -- a rebuilt store, a changed param row, a moved body or a changed pick re-measures; a plan-file edit that
    moves none of them does not (never keyed on the git SHA, which flips every commit)"""
    side = Capture(out_dir).record(cell)
    if not (os.path.exists(side) and os.path.exists(Capture(out_dir).path_of("counts", cell))
            and os.path.exists(Capture(out_dir).path_of("parts", cell))):
        return None
    rec = json.load(open(side))
    if rec.get("complete") is False:      # never a capture (a cell in flight is in its journal, but refuse the flag too)
        return None
    store = meta_store(sf, workload, format_spec(fmt) if fmt else os.path.join(spec_dir, f"{cell}.json"), build=False)
    mpath = os.path.join(store, "manifest.json")
    if (not os.path.exists(mpath) or rec.get("nparam") != nparam
            or rec.get("workload") != workload
            or rec.get("measurement_version") != CAPTURE_SHAPE
            or rec.get("slice", {"queries": None, "params": None}) != {"queries": slice_[0], "params": slice_[1]}):
        return None
    moved = []
    if rec.get("inputs") != inputs:
        moved.append("inputs (bodies or param rows)")
    if rec.get("picks") != (picks or None):
        moved.append("picks")
    spec_path = format_spec(fmt) if fmt else os.path.join(spec_dir, f"{cell}.json")
    if rec.get("cell_inputs") is None or rec["cell_inputs"] != cell_fingerprint(workload, sf, spec_path, store, slice_[0]):
        moved.append("cell_inputs (what this cell selects)")   # or a record from before the key
    # the same store: codecs, content (schema, record stream), the index + filter layout the sizes depend on, and (a
    # meta cell) a store this binary would build for the spec
    if not same_store(rec, store, spec_path, workload, fmt):
        moved.append("store")
    if not counted_fresh(rec, store):
        moved.append("counting versions")
    return moved


def fresh(out_dir, cell, workload, sf, nparam, inputs, spec_dir=GRID, slice_=(None, None), fmt=None, picks=None):
    """reuse a cell's capture only if nothing in its key moved (moved_parts)"""
    return moved_parts(out_dir, cell, workload, sf, nparam, inputs, spec_dir, slice_, fmt, picks) == []


# the parts of a cell's key a reuse ledger row may declare current: freshness of our own code and decisions. The
# others (the bodies or param rows, what the cell selects, the store) can be identity, so they re-measure as before
ADOPTABLE_PARTS = ("picks", "counting versions")


def ledger_keys(out_dir, cell, workload, sf, picks=None, fmt=None, spec_dir=GRID):
    """(what, made_under, current_for) of a cell's capture for the reuse ledger (kvevaluator.adoptions): its counts
    file's sha, and the adoptable parts (ADOPTABLE_PARTS) as the record holds them and as they are today"""
    from kvevaluator.adoptions import key_sha
    cap = Capture(out_dir)
    rec = json.load(open(cap.record(cell)))
    spec_path = format_spec(fmt) if fmt else os.path.join(spec_dir, f"{cell}.json")
    with open(os.path.join(meta_store(sf, workload, spec_path, build=False), "manifest.json")) as f:
        stamp = counting.stamp(json.load(f)["format"])
    stored = counting._stored(rec, "grid")[0] or {}
    made = key_sha({"picks": rec.get("picks"), "counting": {k: stored.get(k) for k in counting.PARTS["read"]}})
    now = key_sha({"picks": picks or None, "counting": {k: stamp.get(k) for k in counting.PARTS["read"]}})
    with open(cap.path_of("counts", cell), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest(), made, now


def reused(out_dir, cell, workload, moved, reuse_stale, ledger=None):
    """True when the cell's capture is kept: fresh; or its key moved and a reuse ledger row declares it current
    (marked adopted; only when every moved part is in ADOPTABLE_PARTS); or (--reuse-stale) only key parts moved (marked
    reused_stale with the parts: kvevaluator.stale). `ledger`: a call giving (what, made_under, current_for)
    (ledger_keys), made only then"""
    from kvevaluator import adoptions
    if moved == []:
        print(f"[grid_bodies] {workload} {cell}: already measured on this store with these params and "
              f"plans -- reused (--fresh re-measures)")
        return True
    adoptable = bool(moved) and all(p in ADOPTABLE_PARTS for p in moved)
    keys, scope = (ledger() if adoptable and ledger else None), {"workload": workload, "cell": cell}
    side = Capture(out_dir).record(cell)
    with stale.scope():
        adopted = bool(keys) and adoptions.adopt("capture", *keys, f"{workload} {cell}: its {', '.join(moved)} moved", scope)
        if adopted:
            print(f"[grid_bodies] {workload} {cell}: its {', '.join(moved)} moved; a ledger row declares it current -- "
                  f"reused", file=sys.stderr)
        elif moved and reuse_stale:
            stale.note("reused_stale", f"{workload} {cell}: kept although its {', '.join(moved)} moved (--reuse-stale"
                       + (f"; or {adoptions.add_hint('capture', *keys, scope)})" if keys and keys[1] != keys[2] else ")"))
        else:
            if keys and keys[1] != keys[2]:
                print(f"[grid_bodies] {workload} {cell}: its {', '.join(moved)} moved: measured again (or "
                      f"{adoptions.add_hint('capture', *keys, scope)})", file=sys.stderr)
            return False
        rec = stale.fold(json.load(open(side)))
    with open(side + ".tmp", "w") as f:
        json.dump(rec, f, indent=2)
    os.replace(side + ".tmp", side)
    return True


def cell_picks(cell, workload, record, queries=None, scale=None):
    """{query: body} a --picks run measures on this cell (or named store: its record key): the record's entries in the
    workload's section at `scale` (record_cell_picks: that scale's own entry where the record has one, as a runner and
    predict mode read it), for `queries` (default the workload's)"""
    return record_cell_picks(cell, queries or workload_queries(workload)[0], record, scale=scale, workload=workload)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sf", required=True)
    ap.add_argument("--family", dest="workload", default="interactive", choices=WORKLOADS,
                    help="linkbench: --sf is its scale tag (e.g. n1m), --param-set its params (anchors|quick|quick-next|log)")
    ap.add_argument("--nparam", type=int, default=5)
    ap.add_argument("--cells", default="", help="comma list (default: every <spec-dir>/*.json)")
    ap.add_argument("--spec-dir", default=GRID, help="the spec set to measure (default: the grid, inputs/specs/grid); "
                    "every set files its layouts in the workload's one capture")
    ap.add_argument("--P", type=int, default=8, help="worker threads (logical counts are P-independent)")
    ap.add_argument("--timeout", type=int, default=1800, help="seconds per run; a timed-out variant is recorded as not ok")
    ap.add_argument("--reuse-stale", action="store_true", help="keep a cell's capture whose key moved (its bodies or "
                    "param rows, picks, selection, store or counting versions; never another workload, params count, slice "
                    "or capture shape), its record marked reused_stale with the parts (kvevaluator.stale)")
    ap.add_argument("--fresh", action="store_true", help="re-measure cells that already have a counts CSV; also start a "
                    "cell in flight over (its journal under output/logs/journals is moved aside, not resumed)")
    ap.add_argument("--exec", default="serve", choices=sorted(RUNNERS),
                    help="serve: one kvevaluator serve per cell (default); spawn: one process per run")
    ap.add_argument("--root", default=None, help="the output tree to write into (default: the output dir); a slice or a "
                    "control needs its own")
    ap.add_argument("--held-out", action="store_true", help="measure the held-out params (the plan pick's set, "
                    "candidates.HELD_OUT_PARAMS) into the workload's held-out capture")
    ap.add_argument("--format", default="", help="comma list of NAMED formats (dgraph, nebula, arangodb) measured like a "
                    "cell: every valid body of every query, filed by the store's name beside the layouts; "
                    "the cells are then not measured. Wired for --family interactive, bi, microbench and job; a chunk arm (pertype_csr-c512k) names its store")
    ap.add_argument("--queries", default="", help="comma list: measure only these queries (a slice; needs --root)")
    ap.add_argument("--params", default="", help="comma list of 1-based param indices (a slice; needs --root)")
    ap.add_argument("--param-set", default=None, help="the param set the runs bind: one the workload's params module "
                    "lists (LDBC Interactive: ldbc, the default, or uniform); a non-default set's rows go to their own "
                    "capture (<workload>/<param set>)")
    ap.add_argument("--picks", nargs="?", const=PICKS_FILE, default=None, metavar="FILE",
                    help="LDBC: measure only the body FILE (default inputs/picks/picks.csv) records per (cell, query), not every "
                    "body (needs --root; a cell with no row in the record is refused)")
    ap.add_argument("--bodies", default="", help="comma list of candidate names: per sliced query, measure only the "
                    "listed candidates of that query (needs --root and --queries)")
    a = ap.parse_args()
    a.param_set = checked_param_set(a.workload, a.param_set)
    if a.held_out and (a.params or a.param_set):
        raise SystemExit("--held-out measures the held-out params: not with --params or --param-set -- REFUSED")
    if a.params and a.param_set:
        raise SystemExit(f"--params with --param-set {a.param_set}: the set holds rows 1-n only (the held-out rows "
                         f"are the default set's: measure them without --param-set) -- REFUSED")
    if server.label_tags_env() and not (a.root and a.fresh):   # a control's counts: never reused, never filed as the capture's
        raise SystemExit(f"KVEVAL_LABEL_TAGS={server.label_tags_env()} is set: a control run, so give it its own "
                         f"tree (--root) and --fresh -- REFUSED")
    spec_dir = os.path.abspath(a.spec_dir)
    named = None if a.format else named_cells(spec_dir, a.cells, "grid_bodies")   # an unknown --cells name stops here
    binary = binary_path()
    slice_ = parse_slice(a.queries, a.params, a.root, sliceable_queries(a.workload))
    if a.held_out:
        if not Tree().capture(a.workload, a.sf).has_held_out():
            raise SystemExit(f"--held-out: {a.workload} has no held-out params -- REFUSED")
        slice_ = (slice_[0], [int(i) for i in held_out_window().split(",")])
    if a.picks and (a.root is None or a.workload != "interactive"):
        raise SystemExit("--picks: LDBC only (the record is LDBC's), and it needs its own tree (--root)")
    if a.bodies and (a.picks or a.format or a.root is None or not a.queries):
        raise SystemExit("--variants: a slice (needs --root and --queries), not with --picks or --format")
    body_picks = bodies_as_picks(a.workload, slice_[0], a.bodies.split(",")) if a.bodies else None
    out_dir = Tree(a.root).capture(a.workload, a.sf, HELD_OUT if a.held_out else a.param_set).path
    inputs = inputs_fingerprint(a.workload, a.sf, a.nparam, slice_[1], slice_[0], a.param_set)
    # at SF30 a cell is never skipped whole: the rule's selection is recomputed from the current SF3 capture each time,
    # and each run already measured is reused by its own key (prior_runs); below SF30 a cell whose key moved takes its
    # unchanged runs the same way (prior_runs)
    sf30_rule_on = str(a.sf) == SF30_TAG
    sf3_root = str(Tree(a.root).root)             # the SF30 rule reads SF3's capture from the same tree
    failed = []                                   # cells that measured nothing (a store error, no run ok)
    if a.format:
        for fmt in a.format.split(","):
            store = format_store(a.sf, a.workload, fmt)
            cell = os.path.basename(store)                                      # the store's own name is the file's
            picks = (cell_picks(plan_runs.record_store_key(request_format(format_spec(fmt)), store), a.workload, a.picks,
                                slice_[0], a.sf) if a.picks else None)
            if not a.fresh and not sf30_rule_on and reused(out_dir, cell, a.workload, moved_parts(
                    out_dir, cell, a.workload, a.sf, a.nparam, inputs, spec_dir, slice_, fmt=fmt, picks=picks), a.reuse_stale,
                    ledger=lambda: ledger_keys(out_dir, cell, a.workload, a.sf, picks=picks, fmt=fmt)):
                continue
            if not measure_cell(cell, a.workload, a.sf, a.nparam, a.P, a.timeout, binary, out_dir, inputs, spec_dir,
                                a.exec, slice_, fresh_start=a.fresh, fmt=fmt, picks=picks, sf3_root=sf3_root,
                                param_set=a.param_set):
                failed.append(cell)
        return failed_exit(failed)
    cells = named
    if not cells:
        raise SystemExit(f"[grid_bodies] no *.json specs in {spec_dir}")
    for cell in cells:
        picks = cell_picks(cell, a.workload, a.picks, slice_[0], a.sf) if a.picks else body_picks
        if not a.fresh and not (sf30_rule_on and not picks) and reused(out_dir, cell, a.workload, moved_parts(
                out_dir, cell, a.workload, a.sf, a.nparam, inputs, spec_dir, slice_, picks=picks), a.reuse_stale,
                ledger=lambda: ledger_keys(out_dir, cell, a.workload, a.sf, picks=picks, spec_dir=spec_dir)):
            continue
        if not measure_cell(cell, a.workload, a.sf, a.nparam, a.P, a.timeout, binary, out_dir, inputs, spec_dir, a.exec,
                            slice_, fresh_start=a.fresh, picks=picks, sf3_root=sf3_root, param_set=a.param_set):
            failed.append(cell)
    return failed_exit(failed)


def failed_exit(failed):
    """the exit code: 1 when any cell failed (each named again last), else 0"""
    if failed:
        print(f"[grid_bodies] FAILED {len(failed)} cell(s): {', '.join(failed)}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
