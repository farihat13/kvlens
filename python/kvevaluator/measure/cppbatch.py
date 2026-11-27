"""The batch request (cpp/kvevaluator/src/cli/session.cc, Session::batch): many requests run back to back inside one serve
process, each plan sent once, the responses written to a file. Shared by every driver that sends batches:
measure/cpplatency.py (every regime) and measure/cppwlatency.py (the write legs).
A line may carry its own `run` (merged over the batch's run, key by key) and `apply` (a write plan: false = shadow
mode); both need a kvevaluator binary with per-line run and apply, which run_lines checks.

A batch response line differs from a single request's response: no provenance (once, in the batch's response), and
seq / variant / body_sha / batch_t_start_ns / batch_t_end_ns on the line. as_single() turns a line back into the
single-request shape, so a driver's row writers and raw files stay as they were.
"""
import json
import os
import re

from kvevaluator import jsonbytes
from kvevaluator.measure.server import build_request


def store_req(srv, extra):
    """a request naming only the server's store (+ `extra`: a batch, an lsm_state, a warmup_write)"""
    req = build_request(None, None, srv.store_dir, srv.fmt, srv.schema, None, srv.block_cache_bytes, srv.direct_io,
                        srv.cache_index_and_filter_blocks, write_buffer_bytes=srv.write_buffer_bytes,
                        auto_compactions=srv.auto_compactions)
    req.update(extra)
    return req


def prepared_run(srv, run):
    """a run config as a single request gets it (build_request: KVEVAL_LABEL_TAGS)"""
    return build_request(None, None, srv.store_dir, srv.fmt, srv.schema, run)["run"]


def run_batch(srv, plans, lines, path_base, run, apply=False, keep_rows=False):
    """One batch: plans {name: IR}, lines [{seq, plan, params}] in run order -> (the batch's response, the responses
    file). The request file (path_base.requests.jsonl) is removed after; the responses file (path_base.responses.jsonl)
    is the caller's to read and remove. run is every request's run config; KVEVAL_LABEL_TAGS reaches it as it
    reaches a single request (build_request)."""
    req_path, resp_path = path_base + ".requests.jsonl", path_base + ".responses.jsonl"
    ok = False
    try:
        with open(req_path, "w") as f:
            for ln in lines:
                if "run" in ln:
                    ln = dict(ln, run=prepared_run(srv, ln["run"]))
                f.write(json.dumps(ln, separators=(",", ":"), default=jsonbytes.default) + "\n")   # bytes as CppServer sends them
        run = prepared_run(srv, run)
        r = srv.request(store_req(srv, {"batch": {"plans": plans, "requests": req_path, "responses": resp_path,
                                                  "apply_writes": apply, "emit_rows": keep_rows, "run": run}}))
        if r.get("error"):
            raise SystemExit(f"batch {os.path.basename(path_base)}: {r['error']}")
        ok = True
    finally:                                         # a failed batch (an error, a dead server, ^C) leaves no file
        for p in (req_path,) if ok else (req_path, resp_path):
            if os.path.exists(p):
                os.unlink(p)
    return r, resp_path


def responses(path):
    """the responses file's lines, parsed, in run order"""
    with open(path) as f:
        for line in f:
            yield json.loads(line)


# the run fields a response's run echo must carry as asked (the guard against a binary that ignores a line's run)
ECHOED = ("P", "cache", "iterations", "perf_level", "kv_timing")


def run_lines(srv, plans, lines, path_base, run, apply=False, keep_rows=False, labels=None):
    """One batch -> its responses in the single-request shape, in line order. Checks the seq of every response, their
    count, and that each response ran under the run its line asked for (the line's run over `run`; a write plan's
    apply_writes = the line's apply, else `apply`). labels: one name per line (e.g. query/p3/cold), added to an
    error that names a line's seq."""
    try:
        br, rpath = run_batch(srv, plans, lines, path_base, run, apply=apply, keep_rows=keep_rows)
    except SystemExit as e:
        m = re.search(r"seq (\d+)", str(e))
        if labels and m and int(m.group(1)) < len(labels):
            raise SystemExit(f"{e} [line {m.group(1)} = {labels[int(m.group(1))]}]")
        raise
    out = []
    try:
        for i, line in enumerate(responses(rpath)):
            if i >= len(lines) or line["seq"] != lines[i]["seq"]:
                raise SystemExit(f"{path_base}: response {i} has seq {line['seq']}, its line {lines[i]['seq'] if i < len(lines) else None}")
            out.append(as_single(line, br))
    finally:
        os.unlink(rpath)
    if len(out) != len(lines):
        raise SystemExit(f"{path_base}: {len(out)} responses for {len(lines)} requests")
    for ln, r in zip(lines, out):
        want = dict(run, **ln.get("run", {}))
        echo = r.get("run") or {}
        bad = [k for k in ECHOED if k in want and echo.get(k) != want[k]]
        # a line that ran applied must have asked; the converse (an apply line that ran in shadow) is its caller's
        # check, since a read plan never applies
        if echo.get("apply_writes") and not ln.get("apply", apply):
            bad.append("apply_writes")
        if bad:
            raise SystemExit(f"{path_base} seq {ln['seq']}: ran with run {[(k, echo.get(k)) for k in bad]}, asked "
                             f"{[(k, want.get(k, ln.get('apply', apply))) for k in bad]} -- does this kvevaluator binary take a line's run?")
    return br, out


def as_single(line, batch_resp):
    """A response line in a single request's shape: provenance = the batch's, plus the line's variant and body_sha
    (keys sorted, as the C++ side writes them). seq and batch_t_start_ns / batch_t_end_ns stay (free data). A slim
    line gets its run echo back from the batch's runs (a per_iteration that copied its step stays dropped)."""
    r = dict(line)
    if "run_id" in r:
        one = batch_resp["batch"]["runs"][r.pop("run_id")]
        r["run"] = one["apply"] if r.pop("applied", False) else one["read"]
    prov = dict(batch_resp["provenance"])
    prov["body"], prov["body_sha"] = r.pop("body"), r.pop("body_sha")
    r["provenance"] = {k: prov[k] for k in sorted(prov)}
    return {k: r[k] for k in sorted(r)}
