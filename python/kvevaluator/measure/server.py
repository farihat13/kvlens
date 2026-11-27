"""One request to the binary and the server that runs a stream of them over one open store. Everything here
runs OUTSIDE a timed window: build a request from (IR, params, store, schema), hand it to the binary (`run` for
one request, `serve` for a stream over one open store), parse the response.

    from kvevaluator.measure.server import CppServer
    from kvevaluator.measure.stores import store_dir
    with CppServer(store_dir("ldbc", "0.1", "pertype_csr-c64k"), "pertype_csr") as srv:
        resp = srv.run(ir_dict, params, run={"cache": "warm", "iterations": {"warmup": 1, "measured": 5}})
        resp["steps"][i]["S"], resp["rows"], resp["totals"]["span_ns"], ...
"""
import json
import os
import subprocess
import sys
import tempfile

from kvevaluator import datasets as _D
from kvevaluator.formats.props_codec import props_codec
from kvevaluator import jsonbytes
from kvevaluator.measure import numa_binding
from kvevaluator.capture.binary import binary_path
from kvevaluator.capture.store_identity import PROPS_CODEC_FORMATS, check_props_codec_name, require_props_codec
from kvevaluator.paths import ROOT

_EXP = str(ROOT)


_EXP = str(ROOT)


SCHEMA_JSON = _D.schema_path("ldbc")


def scale_of_store_dir(store_dir):
    """The dataset tag a store dir names: output/stores/ldbc/sf<X>/<slug> -> "<X>"; .../stores/linkbench/<tag>/<slug>
    -> "<tag>"; None for any other path. The request's store.scale when the caller gives none (the session cross-checks
    it against the manifest's `scale`, which ingest stamps)."""
    import re
    d = os.path.abspath(store_dir)
    for ds in _D.DATASETS.values():      # each dataset's <dataset>/<scale_dir>, e.g. ldbc/sf{scale}, linkbench/{scale}
        pat = re.escape(ds.scale_dir).replace(re.escape("{scale}"), "([^/]+)")
        m = re.search(r"/stores/" + re.escape(ds.output_root_name()) + "/" + pat + r"/[^/]+/?$", d)
        if m and (ds.scale_axis != "sf" or re.fullmatch(r"[0-9.]+", m.group(1))):
            return m.group(1)
    return None


_LABEL_TAGS_WARNED = False


def label_tags_env():
    """KVEVAL_LABEL_TAGS, a control's label tags (None when unset): the one read of it"""
    return os.environ.get("KVEVAL_LABEL_TAGS")


def with_label_tags(run):
    """the run config with KVEVAL_LABEL_TAGS applied (a control's label tags; unchanged when the variable is unset)"""
    # A CONTROL setting, never a measurement's default: which reads may put a label on the rows they output
    # (exec/label_tags.h; both on unless set). KVEVAL_LABEL_TAGS="probes=off" / "hops=off,probes=off" reaches every
    # request of the process, so a before / after table can show what each source of a label moves.
    tags = label_tags_env()
    if tags and "label_tags" not in run:
        run = dict(run)
        global _LABEL_TAGS_WARNED
        if not _LABEL_TAGS_WARNED:               # once per process, loudly: these counts are a control's, not a capture's
            print(f"[server] WARNING: KVEVAL_LABEL_TAGS={tags} is set -- every request of this process runs with "
                  f"these label tags; a CONTROL, never a capture of record", file=sys.stderr)
            _LABEL_TAGS_WARNED = True
        lt = {}
        for part in tags.split(","):
            k, _, v = part.partition("=")
            if k not in ("hops", "probes") or v not in ("on", "off"):
                raise SystemExit(f"KVEVAL_LABEL_TAGS={tags!r}: expected hops=on|off and / or probes=on|off")
            lt[k] = v == "on"
        run["label_tags"] = lt
    return run


def build_request(ir, params, store_dir, fmt="pertype_csr", schema=SCHEMA_JSON, run=None,
                  block_cache_bytes=None, direct_io=False, cache_index_and_filter_blocks=None, scale=None,
                  write_buffer_bytes=None, auto_compactions=False):
    store = {"dir": os.path.abspath(store_dir), "format": fmt, "schema": os.path.abspath(schema)}
    scale = scale if scale is not None else scale_of_store_dir(store_dir)
    if scale is not None:                            # selects a scale-tagged bundle variant (ir/plan.cc select_variant)
        store["scale"] = str(scale)
    if block_cache_bytes is not None:
        store["block_cache_bytes"] = int(block_cache_bytes)
    if direct_io:                                    # RocksDB use_direct_reads: a block-cache miss reads the device
        store["direct_io"] = True
    if cache_index_and_filter_blocks is not None:    # False: index + filter blocks live outside the bounded budget
        store["cache_index_and_filter_blocks"] = bool(cache_index_and_filter_blocks)
    if write_buffer_bytes is not None:               # the memtable size (an open option; RocksDB's 64 MB if absent)
        store["write_buffer_bytes"] = int(write_buffer_bytes)
    if auto_compactions:                             # RocksDB's background compactions on (an open option; off if absent)
        store["auto_compactions"] = True
    run = dict(run or {"P": 1})
    run = with_label_tags(run)
    return {"store": store, "plan": ir, "params": params, "run": run}


def _stderr_filter(text):
    """Drop the fork's DB::Open banner from stderr; keep real messages."""
    return "\n".join(l for l in text.splitlines() if "CUSTOM ROCKSDB LOADED" not in l)


class CppServer:
    """A long-lived `serve` process for one (store, format, schema): the block cache persists
    across requests (WARM), and `cache: cold` reopens in-process. Use as a context manager."""

    def __init__(self, store_dir, fmt="pertype_csr", schema=SCHEMA_JSON, block_cache_bytes=None, direct_io=False,
                 cache_index_and_filter_blocks=None, write_buffer_bytes=None, auto_compactions=False, numa=None):
        self.store_dir, self.fmt, self.schema = store_dir, fmt, schema
        self.numa = numa                       # a binding from numa_binding.choose (timed drivers); None = unbound
        self.write_buffer_bytes, self.auto_compactions = write_buffer_bytes, auto_compactions
        check_props_codec_name(store_dir, fmt)   # name and manifest agree on the property codec
        if fmt in PROPS_CODEC_FORMATS and "KVEVAL_PROPS_CODEC" in os.environ:
            # an explicit request is binding even for a caller that names its store by hand: a named
            # request must never measure a schema store (or the reverse) under a name without the tag
            require_props_codec(store_dir, props_codec())
        self.block_cache_bytes, self.direct_io = block_cache_bytes, direct_io
        self.cache_index_and_filter_blocks = cache_index_and_filter_blocks
        self.proc = None

    def __enter__(self):
        # stderr goes to a file (the fork's banner + any crash message), read back on failure
        self.errfile = tempfile.NamedTemporaryFile(prefix="kvevaluator-serve-", suffix=".err", delete=False)
        self.proc = subprocess.Popen(numa_binding.launch_prefix(self.numa) + [binary_path(), "serve"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self.errfile)
        return self

    def _stderr_tail(self):
        try:
            self.errfile.flush()
            with open(self.errfile.name) as f:
                return _stderr_filter(f.read())[-2000:]
        except OSError:
            return ""

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.proc:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=30)
            except Exception:
                self.proc.kill()
            self.proc = None
            try:
                os.unlink(self.errfile.name)
            except OSError:
                pass

    def request(self, request):
        self.proc.stdin.write((jsonbytes.dumps(request) + "\n").encode())
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            rc = self.proc.wait(timeout=30)
            raise RuntimeError(f"kvevaluator serve died (exit {rc}); stderr tail:\n{self._stderr_tail()}")
        return json.loads(line)

    def run(self, ir, params, run=None):
        return self.request(build_request(ir, params, self.store_dir, self.fmt, self.schema, run,
                                          self.block_cache_bytes, self.direct_io, self.cache_index_and_filter_blocks,
                                          write_buffer_bytes=self.write_buffer_bytes,
                                          auto_compactions=self.auto_compactions))


def step_costs(resp):
    """Per-step (name, op, S, G, N, P, D, S_miss, G_miss, N_miss, get_value_bytes,
    scan_value_bytes, write_key_bytes, write_value_bytes) from a response. The absence-miss OP
    COUNTS sit with the op counts (currency-free: compared even under --skip-bytes) -- they
    prove the two seams declare the same landings missed; the miss KEY bytes are not compared,
    like scan_key/get_key (the Python key model is structural, the C++ key is encoded).
    The WRITE bytes are in the compared tuple on purpose --
    a write-workload cpp-diff must not come back green while the two sides disagree on what a
    removal leaves in a shared posting. (Note the two sides count different VALUE currencies -- neutral model vs encoded
    -- so dgraph-family write-value equality is a non-goal; use
    --skip-bytes + check_cpp_write_effects.py as the gate there.)"""
    return [(s["name"], s["op"], s["S"], s["G"], s["N"], s["P"], s["D"],
             s["S_miss"], s["G_miss"], s["N_miss"],
             s["bytes"]["get_value"], s["bytes"]["scan_value"],
             s["bytes"]["write_key"], s["bytes"]["write_value"])
            for s in resp["steps"]]
