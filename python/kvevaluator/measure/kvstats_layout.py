"""The named formats' captures (Dgraph, NebulaGraph, ArangoDB, ...): store slugs, the accumulation rule, the listing,
the figure selections. Counts and steps sit in the workload's capture beside the layouts; timings, run records and
store sizes in the output tree (kvevaluator.output_tree):

    captures/<d>/<s>/<workload>/reported/{counts,steps}/<slug>.csv
    timings/<d>/<s>/<workload>/<slug>/<regime>/steps.csv     regimes: REGIMES
    timings/<d>/<s>/<workload>/runs/<run-id>.json             one provenance record per capture invocation
    summary/<d>/<s>/named-formats/<workload>/plots[-<suffix>]/ figures for one shown selection (plots_dir)
    stats/<d>/<s>/store-size.csv

<slug> = stores.cpp_store_name(fmt, schema, codec, chunk): `dgraph`, `nebula-label_only`, `pertype_csr-c16k`. A capture
accumulates: `upsert` replaces the rows of the queries a run measured (per regime for timings) and keeps the rest;
every row carries `run`. A different header is refused. The only delete is `drop(slug)`.
"""
import csv
import fcntl
import glob
import json
import os
import re
import sys
import time

from kvevaluator import layouts, workloads
from kvevaluator.output_tree import Tree, stats_dir, timing_in
from kvevaluator.capture.capture_columns import APPENDED_COLUMNS, RENAMED_COLUMNS, only_appended_missing
from kvevaluator.measure.stores import cpp_store_name

# the two default baselines (dgraph + nebula are the default set)
BASELINES = ("dgraph", "nebula")
# the formats a slug can start with, in display order (parsing tries the longest name first);
# meta's slug carries its spec tag (meta-<tag>)
FORMAT_ORDER = ("dgraph", "nebula", "dgneb", "meta", "arangodb", "pertype_csr")

# (the row key, the replacement unit): a run that carries a query replaces the query's rows
# in the unit (per regime for latency: a warm-only run keeps the cold rows)
KEYS = {"counts": ("query", "param_idx"),
        "steps": ("query", "param_idx", "step_idx"),
        "parts": ("query", "param_idx", "step_idx", "part"),
        "latency": ("regime", "query", "param_idx", "step_idx")}
SCOPE = {"counts": ("query",), "steps": ("query",), "parts": ("query",), "latency": ("regime", "query")}
REGIMES = ("warm-p0", "cold-p2", "cold-p3", "durable-p1", "steady-p2", "warm-p3", "cold-p3-directio")
COLD = "cold-p2"          # the cold regime dir: direct I/O at PerfLevel 2 (inputs/contract/store_config.json)
COLD_BEFORE = "cold-p3"   # the older cold regime: buffered reads at PerfLevel 3; also run-kvstats --cold-breakdown's


def cold_dir():
    """the cold dir run-kvstats writes: cold-p2, or cold-p3 for its breakdown cold (--cold-breakdown sets
    KVEVAL_COLD_BREAKDOWN=1)"""
    return COLD_BEFORE if os.environ.get("KVEVAL_COLD_BREAKDOWN") == "1" else COLD


def cold_regime(*roots):
    """the cold regime dir a reader of the captures under `roots` (dirs that hold regime dirs) takes: cold-p2 when one
    of them has it, else the dated cold-p3 (None when neither). The one place a reader of the live layout picks between
    them. One regime for every root, never a mix (a cell's ratio to another must be on one read path): when cold-p2 wins,
    the stores that only cold-p3 holds are named on stderr, since the reader leaves them out."""
    def entries(r, d):
        p = os.path.join(r, d)
        return set(os.listdir(p)) if os.path.isdir(p) else set()
    if any(os.path.isdir(os.path.join(r, COLD)) for r in roots):
        left = sorted(f"{os.path.join(r, COLD_BEFORE)}/{e}" for r in roots for e in entries(r, COLD_BEFORE) - entries(r, COLD))
        if left:
            print(f"[cold_regime] {COLD} is read; {len(left)} capture(s) only in the dated {COLD_BEFORE} are left out: "
                  + ", ".join(left[:6]) + (" ..." if len(left) > 6 else ""), file=sys.stderr)
        return COLD
    return COLD_BEFORE if any(os.path.isdir(os.path.join(r, COLD_BEFORE)) for r in roots) else None


def slug(fmt, schema="canonical", codec=None, chunk=None, schema_json=None):
    return cpp_store_name(fmt, schema, codec, chunk, schema_json=schema_json)


def slug_chunk(s):
    """The `-c<N>k` chunk tag of a slug, or None."""
    m = re.search(r"-(c\d+k)(?:-|$)", s)
    return m.group(1) if m else None


def format_of(s):
    """The format a slug starts with (`pertype_csr-c16k-zstd` -> pertype_csr)."""
    for f in sorted(FORMAT_ORDER, key=len, reverse=True):
        if s == f or s.startswith(f + "-"):
            return f
    raise ValueError(f"slug {s!r} names no known format ({', '.join(FORMAT_ORDER)})")


def _chunk_kb(s):
    """The chunk size in KB a slug's `-c<N>k` tag names, 0 without one (the canonical store)."""
    c = slug_chunk(s)
    return int(c[1:-1]) if c else 0


def sort_slugs(slugs):
    """Canonical display order: by format (FORMAT_ORDER), then by the slug's arm without its chunk
    tag, then by chunk size ASCENDING -- so a format's chunk arms sit together and c4k < c16k < c64k
    < c512k (a plain string sort put c16k before c4k). The same order names a selection's plots dir."""
    def key(s):
        c = slug_chunk(s)
        arm = s.replace(f"-{c}", "", 1) if c else s
        return (FORMAT_ORDER.index(format_of(s)), arm, _chunk_kb(s))
    return sorted(slugs, key=key)


# the lanes a parts row carries: the step's op counts + every attributed byte lane (no used_*: the
# operator's used annotation is not a KV op, so it has no part)
PART_OPS = ("S", "G", "N", "P", "D", "S_miss", "G_miss", "N_miss")
PART_BYTES = ("get_key", "get_value", "scan_key", "scan_value", "seek_miss_key", "next_miss_key",
              "get_miss_key", "write_key", "write_value")
PART_LANES = PART_OPS + PART_BYTES     # the grid shape's names (capture_layout.write_rows reads either shape)


def parts_sum_mismatches(step_rows, part_rows):
    """The steps whose parts do not sum to the step's own counts, as readable strings ([] = exact).
    Rows as a writes capture holds them (steps/ and parts/ CSVs, or the in-memory dicts): keyed by
    (query, param_idx, step_idx), compared on PART_LANES. A step with no parts rows must be all zero.
    A row in the catch-all part `unknown` (a key the format could not name) is refused too: the sums
    would close, but the attribution would be silently incomplete."""
    def key(r):
        return (r["query"], str(r["param_idx"]), str(r["step_idx"]))
    sums = {}
    for r in part_rows:
        acc = sums.setdefault(key(r), dict.fromkeys(PART_LANES, 0))
        for c in PART_LANES:
            acc[c] += int(r[c])
    bad = []
    steps = {key(r): r for r in step_rows}
    for k, r in steps.items():
        acc = sums.get(k, dict.fromkeys(PART_LANES, 0))
        diff = [f"{c} {acc[c]}!={r[c]}" for c in PART_LANES if acc[c] != int(r[c])]
        if diff:
            bad.append(f"{'/'.join(k)}: " + ", ".join(diff))
    bad += [f"{'/'.join(k)}: parts for a step the steps rows lack" for k in sums if k not in steps]
    # the item-size histograms are per part too: a step's parts add up to the step's own cell (files from before the
    # histograms carry no such columns)
    from kvevaluator.capture import step_record
    hcols = [c for c in step_record.HISTS if part_rows and c in part_rows[0] and step_rows and c in step_rows[0]]
    by_step = {}
    for r in part_rows:
        by_step.setdefault(key(r), []).append(r)
    for k, r in steps.items():
        for c in hcols:
            got = step_record.hist_sum(p[c] for p in by_step.get(k, []))
            if by_step.get(k) and got != r[c]:
                bad.append(f"{'/'.join(k)}: {c} parts '{got}' != step '{r[c]}'")
    bad += [f"{r['query']}/{r['param_idx']}/{r['step_idx']}: a row in the catch-all part 'unknown'"
            for r in part_rows if r["part"] == "unknown"]
    return bad


# store-size.csv: the on-disk size of each ingested store, recorded ONCE per (sf, slug) at the
# kvstats base. The store dirs (output/stores/...) are transient and machine-local; this CSV
# is committed with the capture, so the store-size figure redraws on a fresh clone with no store
# ingested. Measured by run/kvstats.sh alongside its run record.
# index_filter_blocks: the store's index + filter layout (whole / partitioned; its manifest's): the SST bytes depend on
# it; a row without the column reads "" (a whole store)
STORE_SIZE_COLS = ("sf", "slug", "total_bytes", "sst_bytes", "index_filter_blocks", "recorded")


def store_size_csv(sf, workload):
    return stats_dir(workloads.get(workload).dataset, sf) / "store-size.csv"


def timings_root(sf, workload, root=None):
    return Tree(root).timings(workload, sf)


def latency_csv(sf, workload, regime, s, root=None):
    if regime not in REGIMES:
        raise ValueError(f"regime {regime!r} is not one of {REGIMES}")
    return timing_in(Tree(root).timings(workload, sf), s, regime) / "steps.csv"


def load_store_sizes(sf, workload):
    """{slug: {col: str}} from store-size.csv, or {} when absent."""
    p = store_size_csv(sf, workload)
    out = {}
    if os.path.exists(p):
        with open(p) as f:
            for r in csv.DictReader(f):
                out[r["slug"]] = r
    return out


def upsert_store_sizes(sf, workload, rows):
    """Merge {slug: {total_bytes, sst_bytes, index_filter_blocks, recorded}} into store-size.csv -- replace the given
    slugs' rows, keep every other slug (accumulate across ingests, one row per store)."""
    have = load_store_sizes(sf, workload)
    for s, d in rows.items():
        have[s] = {"sf": sf, "slug": s, **{k: d[k] for k in d}}
    p = store_size_csv(sf, workload)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=STORE_SIZE_COLS)
        w.writeheader()
        for s in sort_slugs(list(have)):
            w.writerow({c: have[s].get(c, "") for c in STORE_SIZE_COLS})


def present(sf, workload, root=None):
    """{slug: {"counts": bool, "steps": bool, "warm-p0": bool, ...}} for every slug with counts or steps in the formats
    capture or a latency file under the workload dir, in canonical order."""
    r = timings_root(sf, workload, root)
    seen = {}
    for kind in ("counts", "steps"):
        for p in glob.glob(os.path.join(Tree(root).capture(workload, sf).dir(kind), "*.csv")):
            name = os.path.basename(p)[:-4]
            if not layouts.is_cell(name):   # the layouts share the capture dir
                seen.setdefault(name, {})[kind] = True
    for regime in REGIMES:
        for p in glob.glob(os.path.join(r, "*", regime, "steps.csv")):
            seen.setdefault(os.path.basename(os.path.dirname(os.path.dirname(p))), {})[regime] = True
    good = {}
    for s in seen:                       # a stray file must not brick the listing for everyone
        try:
            format_of(s)
            good[s] = seen[s]
        except ValueError as e:
            print(f"[kvstats] skipping {s!r} under {r}: {e}", file=sys.stderr)
    return {s: good[s] for s in sort_slugs(good)}


def family_of(s):
    """The slug minus its store-shape knobs (chunk `-c<N>k`, codec `-zstd...`): the format at
    one schema. `pertype_csr-c4k` and `pertype_csr-c64k-zstd` share the family pertype_csr;
    `dgraph-label_only` is its own family (a schema is a design axis, not a store knob)."""
    fmt = format_of(s)
    m = re.fullmatch(r"(-[a-z_]+)?(-c\d+k)?(-zstd[a-z0-9-]*)?", s[len(fmt):])
    if not m:
        raise ValueError(f"slug {s!r} is not <format>[-<schema>][-c<N>k][-<codec>]")
    return fmt + (m.group(1) or "")


def family(sf, workload, s, root=None):
    """Every present slug of `s`'s family (chunk/codec variants side by side), `s` included."""
    return sort_slugs({p for p in present(sf, workload, root) if family_of(p) == family_of(s)} | {s})


ENGINE_FORMATS = ("dgraph", "nebula", "arangodb")


def baseline_show(sf, workload, root=None):
    """The engine-baseline selection: the BARE dgraph / nebula / arangodb slugs the capture holds
    (the canonical stores); a capture with none of them (a schema arm such as label_only) falls
    back to its engine slugs without a chunk tag (dgraph-label_only ...). A chunk arm (dgraph-c512k)
    or a second schema beside the bare stores is never part of the baseline -- plots-all holds it."""
    have = present(sf, workload, root)
    bare = [s for s in have if s in ENGINE_FORMATS]
    if bare:
        return sort_slugs(bare)
    return sort_slugs([s for s in have if format_of(s) in ENGINE_FORMATS and slug_chunk(s) is None])


def all_show(sf, workload, root=None):
    """Every slug the capture holds -- the plots-all selection."""
    return sort_slugs(present(sf, workload, root))


def plots_dir(sf, workload, shown, root=None):
    """The figures dir for one SHOWN selection of slugs (the rule):
    `plots/` when the selection is exactly the two baselines at canonical; else `plots-<suffix>`
    with suffix = the slugs minus BOTH baselines when both are present, joined by '+' (the
    "baselines + extra" case: plots-arangodb, plots-pertype_csr-c16k); when a baseline is
    absent, `only-` + the full sorted slug set (plots-only-arangodb, plots-only-dgraph+pertype_csr-c64k)
    -- the prefix keeps a baseline-less selection from landing on a "baselines + extra" dir."""
    shown = sort_slugs(set(shown))
    if not shown:
        raise ValueError("plots_dir: an empty selection")
    # the two selections a capture publishes (the rule): the ENGINE BASELINE
    # (dgraph, nebula, arangodb as present) and ALL of the capture's slugs -> plots-all
    base = Tree(root).summary(workloads.get(workload).dataset, sf, "named-formats", workload)
    if set(shown) == set(present(sf, workload, root)) and len(shown) > 1:
        return os.path.join(base, "plots-all")
    if tuple(shown) == tuple(BASELINES):
        return os.path.join(base, "plots")
    if all(b in shown for b in BASELINES):
        rest = [s for s in shown if s not in BASELINES]
        suffix = "+".join(rest)
    else:
        suffix = "only-" + "+".join(shown)
    return os.path.join(base, f"plots-{suffix}")


SELECTION_FILE = "selection.json"


def write_selection(plots, shown):
    """The plots dir remembers the slugs it draws, so every existing selection can be re-rendered
    after a capture (a run must not leave another selection's figures stale)."""
    os.makedirs(plots, exist_ok=True)
    with open(os.path.join(plots, SELECTION_FILE), "w") as f:
        json.dump({"shown": sort_slugs(set(shown))}, f)


def selections(sf, workload, root=None):
    """[(plots dir, shown slugs)] for every plots*/ dir under the workload with a selection file."""
    out = []
    base = Tree(root).summary(workloads.get(workload).dataset, sf, "named-formats", workload)
    for d in sorted(glob.glob(os.path.join(base, "plots*"))):
        p = os.path.join(d, SELECTION_FILE)
        if os.path.isdir(d) and os.path.exists(p):
            with open(p) as f:
                out.append((d, json.load(f)["shown"]))
    return out


# a counts row's numbers in the step record's names (step_record.BYTE_LANES): the op counts, the 9 byte lanes, the read
# total and the answer's row count; a write row also its payload
COUNT_FIELDS = ("S", "G", "N", "P", "D", "S_miss", "G_miss", "N_miss")


def _write_counts_row(r):
    """a writes capture's counts row (one per (query, param), capture_layout.write_rows: either shape), as numbers"""
    from kvevaluator.capture import step_record
    f = lambda k: int(float(r.get(k) or 0))
    return {"query": r["query"], "param_idx": r["param_idx"], "body": r["body"], **{k: f(k) for k in COUNT_FIELDS},
            **{k: f(k) for k in step_record.READ_BYTE_LANES}, "write_key": f("write_key"),
            "write_value": f("write_value"), "read_bytes": f("read_bytes"), "rows": f("rows"),
            "payload_bytes": int(float(r["payload_bytes"])) if r.get("payload_bytes") else None}


def store_counts(sf, workload, s, path=None, root=None):
    """the counts rows of store `s` for the body a runner runs on it (plan_runs.body_to_run: the pick record's at this
    scale), lane-named; a write workload's rows (one per (query, param)) as they are. `path`: another formats capture's
    counts file of the store (default: this scale's, counts_csv)"""
    from kvevaluator import workloads as W
    from kvevaluator.measure import plan_runs
    from kvevaluator.measure import grid_capture as G
    from kvevaluator.capture import spec_sets
    from kvevaluator.capture import step_record
    if W.get(workload).kind == "write":
        from kvevaluator.capture.capture_layout import write_rows
        return [_write_counts_row(r) for r in write_rows(path or Tree(root).capture(workload, sf).path_of("counts", s), cell=s)[2]]
    rows = read(path or Tree(root).capture(workload, sf).path_of("counts", s))
    store = G.format_store(sf, workload, s, build=False)
    fmt = G.request_format(spec_sets.format_spec(s))
    body = {}
    out = []
    for r in rows:
        q = r["query"]
        if q not in body:
            body[q] = plan_runs.body_to_run(workload, q, fmt, store)
        if body[q] is not None and r["body"] != body[q]:
            continue
        if r["ok"] not in ("True", "true", "1"):
            continue
        out.append({"query": q, "param_idx": r["param_idx"], "body": r["body"],
                    **{k: int(float(r.get(k) or 0)) for k in COUNT_FIELDS},
                    **{k: int(float(r[k])) for k in step_record.READ_BYTE_LANES}, "write_key": 0, "write_value": 0,
                    "read_bytes": int(float(r["read_bytes"])), "rows": int(float(r["rows"] or 0))})
    return out


def store_steps(sf, workload, s, root=None):
    """the per-step rows of store `s` in the formats capture, for the body a runner runs on it (as store_counts), the
    byte lanes named without the `_bytes` suffix (a write workload's through capture_layout.write_rows: either shape)"""
    from kvevaluator import workloads as W
    from kvevaluator.measure import plan_runs
    from kvevaluator.measure import grid_capture as G
    from kvevaluator.capture import spec_sets
    if W.get(workload).kind == "write":
        from kvevaluator.capture.capture_layout import write_rows
        return write_rows(Tree(root).capture(workload, sf).path_of("steps", s), cell=s)[2]
    rows = read(Tree(root).capture(workload, sf).path_of("steps", s))
    store = G.format_store(sf, workload, s, build=False)
    fmt = G.request_format(spec_sets.format_spec(s))
    body, out = {}, []
    for r in rows:
        q = r["query"]
        if q not in body:
            body[q] = plan_runs.body_to_run(workload, q, fmt, store)
        if body[q] is None or r["body"] == body[q]:
            out.append(r)
    return out


def load_counts(sf, workload, shown, root=None):
    """The per-(query, param) rows of every shown store, the series key `db` = the slug: the formats capture's rows of
    the body the runners run (reads), or its rows (writes), lane-named (step_record.BYTE_LANES, read_bytes = every read
    lane)"""
    rows = []
    for s in shown:
        rows += [{**r, "db": s} for r in store_counts(sf, workload, s, root=root)]
    return rows


def new_run_id():
    return time.strftime("%Y%m%dT%H%M%S")


def write_run(sf, workload, run_id, record, finish=False, root=None):
    """runs/<run-id>.json: the invocation's provenance (command, git, binary, slugs, metrics,
    nparam, warm/cold, wall). A run-id is never reused: the record is written when the run STARTS (status
    "running", so rows a stopped run left on disk have their identity) and once more when it ends
    (`finish`: the same run's record, replaced by its final status)."""
    d = timings_root(sf, workload, root) / "runs"
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{run_id}.json")
    if os.path.exists(p) and not (finish and json.load(open(p)).get("status") == "running"):
        raise SystemExit(f"run record {p} exists -- a run-id is never reused")
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"run": run_id, **record}, f, indent=1)
    os.replace(tmp, p)
    return p


def pad_appended(path, want):
    """rewrite the CSV at `path` under the header `want`, which adds only appended columns (capture_columns.APPENDED_COLUMNS)
    to the file's: each row gets "" there (not measured). Under upsert's lock; written to a temp file, then renamed
    over the old one"""
    lock_path = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".lock")   # upsert's lock
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        tmp = f"{path}.tmp-pad"
        with open(tmp, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=want, restval="")      # the terminator upsert writes
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, path)
    print(f"[kvstats_layout] {path}: the appended column(s) {[c for c in want if c in APPENDED_COLUMNS]} added as empty "
          f"(not measured) to its {len(rows)} rows", file=sys.stderr, flush=True)


def aside_other_header(path, columns):
    """upsert's header rule, for a driver to call BEFORE it measures: an existing file at `path` whose header is not
    `columns` (+ `run`) is moved aside (<path>.old-header-<UTC time>, never padded, never deleted), so this run starts
    a file of its own shape; marked shape_sibling (kvevaluator.stale). The aside path, or None."""
    if not os.path.exists(path):
        return None
    want = list(columns) + ([] if "run" in columns else ["run"])
    with open(path, newline="") as f:
        have = next(csv.reader(f), [])
    if have == want:
        return None
    if only_appended_missing(have, want):                                      # only appended columns missing:
        pad_appended(path, want)                                               # they join as "" (not measured)
        return None
    aside = f"{path}.old-header-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    os.replace(path, aside)
    from kvevaluator import stale
    stale.note("shape_sibling", f"{path}: its header differed from this driver's ({len(have)} columns against "
               f"{len(want)}; only there: {sorted(set(have) - set(want))[:6]}, only here: {sorted(set(want) - set(have))[:6]}): "
               f"moved to {aside}; this run starts the file")
    return aside


def upsert(path, rows, key, run_id, columns=None, scope=None):
    """Merge `rows` into the CSV at `path`. The unit of replacement is `scope` (a prefix of
    `key`, e.g. ("query",)): every existing row whose scope value is carried by this write is
    dropped, then the new rows are added -- so a re-measure of a query with fewer params or fewer
    steps leaves no ghost cells; rows of scopes this write does not carry are kept untouched.
    scope=None = the full key (cell-by-cell replacement). Each written row gets `run` = run_id.
    `columns` fixes the header for a new file (default: the first row's keys, `run` last). An
    existing file with a DIFFERENT header refuses (loud, never padded); a row carrying a column
    the header lacks refuses (never silently dropped). The file is locked for the read-modify-
    write (two runs on one slug serialize). Returns (replaced, added, kept): replaced = rows
    dropped from the carried scopes, added = new rows beyond those, kept = the other rows."""
    if not rows:
        return 0, 0, 0
    rows = [dict(r) for r in rows]
    for r in rows:
        r["run"] = run_id
    header = list(columns) if columns else [c for c in rows[0] if c != "run"] + ["run"]
    if "run" not in header:
        header = header + ["run"]
    for k in key:
        if k not in header:
            raise ValueError(f"upsert: key column {k!r} not in the header {header}")
    extra = {c for r in rows for c in r} - set(header)
    if extra:
        raise ValueError(f"upsert: rows carry columns the header lacks {sorted(extra)} -- add them to "
                         f"the column list (a field must never be dropped silently)")
    scope = tuple(scope) if scope else tuple(key)
    kf = lambda r: tuple(str(r[k]) for k in key)
    sf_ = lambda r: tuple(str(r[k]) for k in scope)
    new = {kf(r): r for r in rows}
    if len(new) != len(rows):
        raise ValueError(f"upsert: duplicate keys within one write ({len(rows) - len(new)} dups)")
    carried = {sf_(r) for r in rows}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # the lock is a dotfile beside the CSV (hidden from listings; a lock file must outlive the
    # holder to serialize the next writer, so it is never removed)
    lock_path = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".lock")
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = []
        if os.path.exists(path):
            with open(path, newline="") as f:
                rd = csv.DictReader(f)
                # a column renamed since the file was written reads by its new name; the file is rewritten under it
                rd.fieldnames = [RENAMED_COLUMNS.get(c, c) for c in rd.fieldnames or []]
                # ... and a column appended since (capture_columns.APPENDED_COLUMNS) reads "" in its old rows
                if not only_appended_missing(rd.fieldnames, header):
                    raise SystemExit(f"upsert: {path} has header {rd.fieldnames}, the new rows have {header} "
                                     f"-- a schema change must not merge silently; rename the old file "
                                     f"and re-measure")
                old = list(rd)
        merged, replaced = [], 0
        for r in old:
            if sf_(r) in carried:
                replaced += 1
                continue
            merged.append(r)
        kept = len(merged)
        merged += rows
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=header)
            w.writeheader()
            for r in merged:
                w.writerow({c: r.get(c, "") for c in header})
        os.replace(tmp, path)
    return replaced, max(0, len(rows) - replaced), kept


def read(path):
    """The rows of one capture CSV ([] when absent)."""
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def drop(sf, workload, s, root=None):
    """The ONLY delete: remove every file of one slug under the workload dir. Returns the paths."""
    cap = Tree(root).capture(workload, sf)
    gone = []
    for p in [cap.path_of("counts", s), cap.path_of("steps", s)]:
        if os.path.exists(p):
            os.remove(p)
            gone.append(p)
    for regime in REGIMES:
        d = os.path.dirname(latency_csv(sf, workload, regime, s, root))
        if os.path.isdir(d):
            import shutil
            shutil.rmtree(d)
            gone.append(d)
    return gone


_SLUG_RE = re.compile(r"^[A-Za-z0-9_+.-]+$")


def check_slug(s):
    if not _SLUG_RE.match(s) or "/" in s:
        raise ValueError(f"bad slug {s!r}")
    format_of(s)
    return s
