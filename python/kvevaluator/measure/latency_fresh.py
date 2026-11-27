"""Skip-if-fresh for the latency stages of run/kvstats.sh (warm, cold, durable): reuse a slug's captured cells when
nothing that could change them has changed, else re-measure. Reuse is the default; --fresh forces a re-measure.

A capture's identity, per (slug, query); reuse only if every part matches:
  1. the store fingerprint: the manifest's key_codec, value_codec_hash, schema and key count
  2. the query's param sha and nparam
  3. the query's committed plan-IR sha (a plan edit changes numbers without touching the binary)
  4. the metrics / regimes the stage measures (a counts-only capture cannot stand in for latency)
  5. the counting versions the binary states for the store's format (measure/counting.py), bumped in C++ only when what
     an op is charged changes; never the binary's git sha, which moves on every commit (an older identity's
     measurement_version fields are read by counting_fresh)
  6. for a latency stage, the request protocol its run metas name (measure/capture_protocol) and the stores' index +
     filter setting (index_filter_mismatch); a capture timed otherwise is not fresh, and run/kvstats.sh moves it aside
Freshness is judged from the data's provenance: every row carries the run id that wrote it, and a query is fresh only
when all its rows come from runs whose record is `ok` and whose stamped identity equals the current one.

    from kvevaluator.measure.latency_fresh import identity, fresh_stages
"""
import hashlib
import json
import os

from kvevaluator import adoptions, stale
from kvevaluator.measure import counting
from kvevaluator.measure import plan_runs, stores
from kvevaluator.capture import store_identity
from kvevaluator.measure import kvstats_layout as L
from kvevaluator.capture import step_record
from kvevaluator.measure import workload_plans
from kvevaluator.measure.capture_protocol import capture_index_filter, capture_protocol, index_filter_default, metas_of
from kvevaluator.measure import store_config

# the latency stages run/kvstats.sh runs, with the CSV a stage's rows land in (its counts stage is grid_bodies /
# grid_writes --format, which keep their own reuse)
STAGES = {"warm": lambda sf, wl, slug, root=None: L.latency_csv(sf, wl, "warm-p0", slug, root),
          "cold": lambda sf, wl, slug, root=None: L.latency_csv(sf, wl, L.cold_dir(), slug, root),
          "durable": lambda sf, wl, slug, root=None: L.latency_csv(sf, wl, "durable-p1", slug, root)}

LATENCY_STAGES = ("warm", "cold", "durable")
# how run/kvstats.sh's latency stages time each regime today, as (requests, kv_timing, perf timers): one batch request
# per (store, query) or per write arena, the KV-boundary timer off; the perf level each stage's dir names (warm-p0,
# cold-p2, or cold-p3 with run-kvstats --cold-breakdown; durable-p1's headline pass at perf 1, its breakdown pass at 3)
LATENCY_PROTOCOL = {"warm": ("batch", False, False), "cold": ("batch", False, L.cold_dir() == "cold-p3"),
                    "headline": ("batch", False, False), "breakdown": ("batch", False, True)}


def protocol_mismatch(sf, workload, slug, stage, root=None):
    """For a latency stage: the protocols its capture's run metas name, when any differs from what LATENCY_PROTOCOL
    gives its regime (sorted list of (requests, kv_timing, perf timers)), else None. A capture with no meta (none, or
    not yet written) is None."""
    if stage not in LATENCY_STAGES:
        return None
    seen, bad = set(), False
    for r, per_regime in capture_protocol(STAGES[stage](sf, workload, slug, root)).items():
        seen |= per_regime
        bad = bad or per_regime != {LATENCY_PROTOCOL.get(r)}
    return sorted(seen, key=str) if bad else None


def index_filter_mismatch(sf, workload, slug, stage, root=None):
    """For a latency stage: the (layout, in the cache) settings its capture's run metas name, when any differs from
    today's (capture_protocol.index_filter_default: the binary's ingest layout, index + filter in the cache), else
    None. The store fingerprint leaves the layout out (counts do not depend on it), so a latency stage checks it here."""
    if stage not in LATENCY_STAGES:
        return None
    got = capture_index_filter(STAGES[stage](sf, workload, slug, root))
    bad = any(s != {index_filter_default(r)} for r, s in got.items())
    return sorted(set().union(*got.values()), key=str) if bad else None


def store_config_mismatch(sf, workload, slug, stage, root=None):
    """For a latency stage: what in its capture's run metas differs from inputs/contract/store_config.json's decided settings
    (measure/store_config: violations for warm and cold (cpplatency), write_violations for durable (cppwlatency); an
    older capture records no store options), else None"""
    if stage not in LATENCY_STAGES:
        return None
    check = store_config.write_violations if stage == "durable" else store_config.violations
    bad = []
    for p in metas_of(STAGES[stage](sf, workload, slug, root)):
        with open(p) as f:
            bad += check(json.load(f))[1]
    return sorted(set(bad)) or None


def move_aside_other_protocol(sf, workload, slug, stage, root=None):
    """Move a latency stage's capture timed under another protocol out of the way (mv, never delete), so the next run
    writes a capture of one protocol: <root>/protocol-<requests>[-kv-timer]/<stage dir>/<slug>. Returns the new path,
    or None when the capture is already of LATENCY_PROTOCOL (or absent)."""
    seen = protocol_mismatch(sf, workload, slug, stage, root)
    if seen:
        # named by the protocols found, so a move for the timer or the perf level alone is told apart from one for the
        # requests (a durable capture always holds two: headline at perf 1, breakdown at perf 3)
        tag = "+".join(sorted({q + ("-kv-timer" if t else "") + ("-perf3" if p else "-perf-unknown" if p is None else "")
                               for q, t, p in seen}))
    elif index_filter_mismatch(sf, workload, slug, stage, root):    # timed on another index + filter setting
        seen = index_filter_mismatch(sf, workload, slug, stage, root)
        tag = "+".join(sorted({f"ifb-{lay}-{'in' if inc else 'outside'}" for lay, inc in seen}))
    else:
        seen = store_config_mismatch(sf, workload, slug, stage, root)   # timed under other store settings (inputs/contract/store_config.json)
        if not seen:
            return None
        tag = "store-config-other"
    src = os.path.dirname(STAGES[stage](sf, workload, slug, root))
    dst = os.path.join(L.timings_root(sf, workload, root), f"protocol-{tag}", slug, os.path.basename(src))
    if os.path.exists(dst):
        raise SystemExit(f"[latency_fresh] {src} was timed under {seen}, and its move-aside path {dst} is taken: "
                         "move one of them by hand")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.rename(src, dst)
    return dst


# the regime names a latency stage's run metas carry (cpplatency: warm / cold; cppwlatency --breakdown: two passes)
STAGE_REGIMES = {"warm": ("warm",), "cold": ("cold",), "durable": ("headline", "breakdown")}


def plot_mix_after_run(sf, workload, selection, measured, root=None):
    """Would the figures of `selection` (slugs) mix request protocols once this run has measured `measured` ({(slug,
    stage)}, each then LATENCY_PROTOCOL)? The message naming the other captures, or None. run/kvstats.sh asks before
    measuring, so a mixed figure set fails in seconds, not at the plot step hours later. As plot_kvstats checks: the
    requests and the timer across every regime dir of a regime (warm-p0 is drawn beside warm-p3), and the perf timers
    too within one regime dir (one series)."""
    stage_of_dir = {"warm-p0": "warm", L.cold_dir(): "cold", "durable-p1": "durable"}   # the dirs STAGES writes
    seen = {}                                          # (regime, None or regime dir) -> protocol -> [where]
    # plot_kvstats draws cold-p2 OR cold-p3 (present_read_regimes): with any cold-p2, the dated cold-p3 is not drawn
    p2 = any(os.path.exists(L.latency_csv(sf, workload, L.COLD, slug, root)) for slug in selection) or (
        L.cold_dir() == L.COLD and any(st == "cold" for slug, st in measured if slug in selection))
    for d in L.REGIMES:
        if d == L.COLD_BEFORE and p2:
            continue
        for slug in selection:
            if (slug, stage_of_dir.get(d)) in measured:
                continue                               # replaced by this run
            path = L.latency_csv(sf, workload, d, slug, root)
            if os.path.exists(path):
                for r, ps in capture_protocol(path).items():
                    for pr in ps:
                        where = os.path.relpath(os.path.dirname(path))
                        seen.setdefault((r, None), {}).setdefault(pr[:2], []).append(where)
                        seen.setdefault((r, d), {}).setdefault(pr, []).append(where)
    for slug, st in sorted(measured):
        if slug in selection:
            d = {v: k for k, v in stage_of_dir.items()}[st]
            for r in STAGE_REGIMES.get(st, ()):
                seen.setdefault((r, None), {}).setdefault(LATENCY_PROTOCOL[r][:2], []).append(f"{slug} {st} (this run)")
                seen.setdefault((r, d), {}).setdefault(LATENCY_PROTOCOL[r], []).append(f"{slug} {st} (this run)")
    clash = {k: v for k, v in sorted(seen.items(), key=str) if len(v) > 1}
    if not clash:
        return None
    return "; ".join(f"{r}{'' if d is None else ' in ' + d}: " + ", ".join(
        f"{pr} {sorted(w)[:4]}{' ...' if len(w) > 4 else ''}" for pr, w in sorted(v.items(), key=str))
        for (r, d), v in clash.items())


def _store_format(sf, slug):
    """the format a store was built as (its manifest's `format`)"""
    with open(os.path.join(stores.store_dir("ldbc", sf, slug), "manifest.json")) as f:
        return json.load(f)["format"]


def store_fingerprint(sf, slug):
    """The manifest fields that identify what a store holds (a re-ingest under another layout,
    codec config or binary moves at least one)."""
    p = os.path.join(stores.store_dir("ldbc", sf, slug), "manifest.json")
    with open(p) as f:
        m = json.load(f)
    # the SSTs' index + filter blocks (whole / partitioned) are NOT in it: counts are taken at our store boundary, so
    # they do not depend on that layout (the whole-vs-partitioned count control), and a re-ingest under the
    # new default re-measures no counts. Latency captures carry the layout (measure/capture_protocol), and so do size
    # captures (kvevaluator.measure.grid_bodies.fresh, store-size.csv). tests/gates/check_capture_kinds.py pins the
    # fields below: a fingerprint that ignores one of them fails it.
    return {"key_codec": m.get("key_codec"), "value_codec_hash": m.get("value_codec_hash"),
            "schema": (m.get("schema") or {}).get("name"), "keys": m.get("keys")}


def param_sha(workload, sf, qname):
    rows = workload_plans.query_params(workload, sf, qname, 0)
    return hashlib.sha256(repr(rows).encode()).hexdigest()[:12]


def plan_sha(workload, qname, label_only_family=False):
    """sha of the committed plan-IR file(s) the query runs from (the language-neutral contract;
    an un-dumped Python plan edit is caught by tests/unit/check_plan_files.py, not here)."""
    ir = plan_runs.ir_for(workload, qname, label_only_family=label_only_family)
    from kvevaluator.plans.ir.bundle import file_sha
    return file_sha(ir)                  # the query's own fields (question, params_like) left out


def body_shas(sf, workload, slug, queries, fmt, schema_name="canonical"):
    """{query: sha of the BODY this store runs} (planir/bundle.body_sha: the body the pick record names for the store, or
    the variant a parked file's selector picks, fragment-expanded; the C++ response records the same sha as provenance.body_sha). None for a query with no
    variant on this format. The store's chunk comes from its manifest (the store's fact)."""
    from kvevaluator.plans.ir.bundle import NoVariant, body_sha, require_bare, select_variant
    from kvevaluator.formats.caps import caps_for, caps_resolver
    from kvevaluator.datasets.ldbc.schema import get_schema
    schema = get_schema(schema_name)
    chunk = store_identity.store_chunk_target(stores.store_dir("ldbc", sf, slug))
    caps = caps_for(fmt, schema, chunk=chunk)
    of_label = caps_resolver(fmt, schema, chunk=chunk)
    lo = plan_runs.label_only_family(schema)
    out = {}
    for q in queries:
        # what a runner sends (plan_runs.ir_to_run): the record's body alone, a parked file whole, a bare plan
        ir = plan_runs.ir_to_run(workload, q, fmt, stores.store_dir("ldbc", sf, slug), lo, scale=str(sf))
        try:
            if "plans" not in ir:
                require_bare(ir, caps)                 # a bare plan's own `requires` (property_index)
            out[q] = body_sha(ir, select_variant(ir, caps, of_label, str(sf)) if "plans" in ir else None)
        except NoVariant:
            out[q] = None
    return out


def identity(sf, workload, slug, queries, nparam, schema_name="canonical", fmt=None):
    """The current identity of (slug, queries): what a run record stamps and what a later run
    compares against. `plans` = the whole bundle file's sha (the contract as a file); `bodies` (with `fmt`) = the
    sha of the body THIS store runs -- the freshness key a later run compares when both records have it, so a
    variant edit that does not touch this store's body re-measures nothing (an older record carries no `bodies` and
    compares by `plans`)."""
    lo = schema_name == "label_only"
    ident = {"store": store_fingerprint(sf, slug),
             "nparam": int(nparam),
             "params": {q: param_sha(workload, sf, q) for q in queries},
             "plans": {q: plan_sha(workload, q, lo) for q in queries},
             "format": _store_format(sf, slug),
             # the counting versions it is counted under, and the ones its key compares (the writes workload: both pairs)
             "counting": counting.stamp(_store_format(sf, slug)),
             "counted": "read+write" if workload == "writes" else "read",
             # the step record's column list: a cell written under another list is re-measured (its file's header
             # would refuse the upsert otherwise); a record from before this key is not fresh
             "step_columns": step_record.columns_sha()}
    # the label-tags control (server.build_request): rows measured with it set are another identity, never reused
    # as a plain capture's (and a plain capture never reuses them)
    if os.environ.get("KVEVAL_LABEL_TAGS"):
        ident["label_tags"] = os.environ["KVEVAL_LABEL_TAGS"]
    if fmt is not None:
        ident["bodies"] = body_shas(sf, workload, slug, queries, fmt, schema_name)
    return ident


def _records(sf, workload, root=None):
    """{run_id: record} for every run record of the workload."""
    out = {}
    d = L.timings_root(sf, workload, root) / "runs"
    if not os.path.isdir(d):
        return out
    for name in os.listdir(d):
        if name.endswith(".json"):
            with open(os.path.join(d, name)) as f:
                r = json.load(f)
            out[r.get("run", name[:-5])] = r
    return out


def _vouches(rec, stage):
    """Can this run's record vouch for rows of `stage`? An ok run: yes. A run that was stopped or FAILED vouches for
    nothing: its rows are re-measured."""
    return (rec.get("status") or "") == "ok"


def _query_matches(rec, slug, q, ident, stage=None, workload=None):
    """Does run record `rec` vouch for (slug, q) under the current identity? (`workload`: also through the count-equal
    successor table, _plan_matches)"""
    if not _vouches(rec, stage):
        return False
    stamped = (rec.get("identity") or {}).get(slug)
    if not stamped:
        return False                                  # a record from before the stamp: not fresh
    # a capture with MORE params than asked is a superset (is7's 35-anchor capture vs a 5-param
    # run): fresh, never re-measured down to fewer params
    return (stamped.get("store") == ident["store"] and (stamped.get("nparam") or 0) >= ident["nparam"]
            and stamped.get("params", {}).get(q) == ident["params"].get(q)
            and _plan_matches(stamped, q, ident, workload)
            and counting.counting_fresh(stamped, ident["format"], ident["counted"], "kvstats")[0]
            and stamped.get("step_columns") == ident["step_columns"])


def _moved_parts(rec, slug, q, ident, stage=None, workload=None):
    """the identity parts that moved for (slug, q) since run `rec` timed it, for --reuse-stale: [] when it vouches as is;
    None when its rows cannot stand for this run (no ok run, no stamped identity, fewer params than asked, other step
    columns)"""
    stamped = (rec.get("identity") or {}).get(slug)
    if not _vouches(rec, stage) or not stamped or (stamped.get("nparam") or 0) < ident["nparam"] \
            or stamped.get("step_columns") != ident["step_columns"]:
        return None
    return ([p for p, same in (("store", stamped.get("store") == ident["store"]),
                               ("params", stamped.get("params", {}).get(q) == ident["params"].get(q)),
                               ("plan", _plan_matches(stamped, q, ident, workload)),
                               ("counting", counting.counting_fresh(stamped, ident["format"], ident["counted"], "kvstats")[0]))
             if not same])


def _plan_matches(stamped, q, ident, workload=None):
    """the plan half of the key: the BODY this store runs when both records stamp it (`bodies`), else the whole
    bundle file (`plans`, the older, broader key: any variant edit stales every store's cells). With
    `workload`, a stamped body rewritten count-equal since (candidates.successor_sha) matches its successor: its rows
    are kept, and fresh_stages names them (old_body_under_successor)"""
    a, b = (stamped.get("bodies") or {}).get(q), (ident.get("bodies") or {}).get(q)
    if a is not None and b is not None:
        if workload is not None and a != b:
            from kvevaluator.plan_choice.candidates import successor_sha
            return successor_sha(workload, q, a) == b
        return a == b
    return stamped.get("plans", {}).get(q) == ident["plans"].get(q)


def old_body_under_successor(rec, slug, q, ident):
    """the stamped body sha of a run that matched `ident` only through the count-equal successor table, else None"""
    a = (((rec.get("identity") or {}).get(slug) or {}).get("bodies") or {}).get(q)
    b = (ident.get("bodies") or {}).get(q)
    return a if a is not None and b is not None and a != b else None


# the identity parts _moved_parts names that a reuse ledger row may declare current (our own code: the body, the
# counting versions); a moved store or param row is identity and is timed again
ADOPTABLE_PARTS = ("plan", "counting")


def ledger_keys(runs, rows, slug, q, ident):
    """(what, made_under, current_for) of q's timed rows of a stage for the reuse ledger (kvevaluator.adoptions): the
    rows' sha, and the adoptable parts (ADOPTABLE_PARTS: the body, the counting versions) as the runs stamped them and as
    they are today"""
    from kvevaluator.adoptions import key_sha

    def parts(stamped, cnt):
        return {"body": (stamped.get("bodies") or {}).get(q) or (stamped.get("plans") or {}).get(q),
                "counting": {k: (cnt or {}).get(k) for k in counting.PARTS[ident["counted"]]}}
    made = key_sha([parts(r["identity"][slug], counting._stored(r["identity"][slug], "kvstats")[0]) for r in runs])
    now = key_sha([parts(ident, counting.stamp(ident["format"]))] * len(runs))
    mine = sorted((json.dumps(r, sort_keys=True) for r in rows if r.get("query") == q))
    return hashlib.sha256("\n".join(mine).encode()).hexdigest(), made, now


def fresh_stages(sf, workload, slug, queries, ident, stages, under_successor=None, reuse_stale=None, adopted=None,
                 root=None):
    """{stage: [fresh queries]} for the requested stages: a query is fresh in a stage when the
    stage's CSV holds rows for it and EVERY such row comes from a run that vouches for it (_vouches: an ok run) whose
    stamped identity for the slug equals `ident`. A stage is skippable when every requested query is fresh.
    `under_successor` (a dict): filled with {stage: {query: old body sha}} for the queries fresh only because their
    body was rewritten count-equal since (their rows were made by the old body). `reuse_stale` (a dict, --reuse-stale):
    a query whose runs' identity moved only in its store, params, plan or counting parts (_moved_parts) is kept too,
    filled in as {stage: {query: the moved parts}}."""
    recs = _records(sf, workload, root)
    out = {}
    for stage in stages:
        if (protocol_mismatch(sf, workload, slug, stage, root) or index_filter_mismatch(sf, workload, slug, stage, root)
                or store_config_mismatch(sf, workload, slug, stage, root)):
            out[stage] = []                           # timed under another protocol, index + filter or store setting: nothing is fresh
            continue
        rows = L.read(STAGES[stage](sf, workload, slug, root))
        runs_by_q = {}
        for r in rows:
            if stage in ("warm", "cold", "durable") and r.get("step") != "__plan__":
                continue                              # the plan row stands for the cell
            runs_by_q.setdefault(r["query"], set()).add(r.get("run", ""))
        if stage == "durable" and rows and "run" not in rows[0]:
            # the durable file is REPLACED whole per run (the cells depend on the write sequence),
            # so its rows carry no run id: the provenance is the LATEST ok run that measured this
            # slug's durable stage -- its stamped identity vouches for every query it holds
            latest = max((r for r in recs.values() if r.get("status") == "ok" and slug in (r.get("stores") or [])
                          and "latency" in (r.get("metrics") or []) and r.get("write_latency", "durable") == "durable"
                          and stage not in (r.get("reused") or {}).get(slug, [])),
                         key=lambda r: r.get("run", ""), default=None)
            runs_by_q = {q: {latest["run"]} for q in runs_by_q} if latest else {}
        fresh = []
        for q in queries:
            runs = runs_by_q.get(q)
            if runs and all(rid in recs and _query_matches(recs[rid], slug, q, ident, stage, workload) for rid in runs):
                fresh.append(q)
                old = sorted({o for rid in runs if (o := old_body_under_successor(recs[rid], slug, q, ident))})
                if old and under_successor is not None:
                    under_successor.setdefault(stage, {})[q] = ",".join(old)
            elif runs and all(rid in recs for rid in runs):
                parts = [_moved_parts(recs[rid], slug, q, ident, stage, workload) for rid in runs]
                if not all(p is not None for p in parts):
                    continue
                moved = ",".join(sorted({x for p in parts for x in p}))
                adoptable = all(x in ADOPTABLE_PARTS for p in parts for x in p)
                keys = ledger_keys([recs[rid] for rid in sorted(runs)], rows, slug, q, ident) if adoptable else None
                if keys and adopted is not None and adoptions.covering("capture", *keys, {"cell": slug, "query": q, "stage": stage}):
                    fresh.append(q)
                    adopted.setdefault(stage, {})[q] = (moved, keys)
                elif reuse_stale is not None:
                    fresh.append(q)
                    reuse_stale.setdefault(stage, {})[q] = moved
                elif keys and keys[1] != keys[2]:
                    print(f"[latency_fresh] {slug} {stage} {q}: its {moved} moved: timed again (or "
                          f"{adoptions.add_hint('capture', *keys, {'cell': slug, 'query': q, 'stage': stage})})")
        out[stage] = fresh
    return out


def plan(sf, workload, slugs, queries, nparam, stages, schema_name="canonical", formats=None, reuse_stale=False,
         root=None):
    """{slug: {"identity": ..., "fresh": {stage: [queries]}, "skip": [stages fully fresh]}} -- what run/kvstats.sh
    consults before measuring. `formats` = the format of each slug (same order). reuse_stale (--reuse-stale): rows whose
    identity moved in its store, params, plan or counting parts are kept too, named in "reused_stale"."""
    out = {}
    for i, slug in enumerate(slugs):
        ident = identity(sf, workload, slug, queries, nparam, schema_name, formats[i] if formats else None)
        under, kept, adopted = {}, ({} if reuse_stale else None), {}
        fresh = fresh_stages(sf, workload, slug, queries, ident, stages, under, kept, adopted, root)
        out[slug] = {"identity": ident, "fresh": fresh,
                     "skip": [s for s in stages if set(fresh[s]) >= set(queries)],
                     # rows kept although made by a body since rewritten count-equal (its time is the old body's)
                     "under_successor": under, "reused_stale": kept or {},
                     # rows kept because a reuse ledger row declares them current: {stage: {q: (parts, ledger keys)}}
                     "adopted": adopted}
        _adopt_kept(slug, out[slug])
        for st, qs in sorted(under.items()):
            for q in sorted(qs):
                stale.note("under_successor", _successor_detail(slug, st, q, qs[q]))
        for st, qs in sorted((kept or {}).items()):
            if st in out[slug]["skip"]:
                for q in sorted(qs):
                    stale.note("reused_stale", _reused_detail(slug, st, q, qs[q]))
    return out


def _kept_adopted(plan):
    """(slug, stage, query, moved parts, ledger keys) of each stage a ledger row keeps, sorted"""
    return [(s, st, q, parts, keys) for s, v in sorted(plan.items()) for st, qs in sorted((v.get("adopted") or {}).items())
            if st in v["skip"] for q, (parts, keys) in sorted(qs.items())]


def _adopt_kept(slug, entry):
    """mark this process's records: each stage of `slug` a ledger row keeps is adopted"""
    for s, st, q, parts, keys in _kept_adopted({slug: entry}):
        adoptions.adopt("capture", *keys, _reused_detail(s, st, q, parts, "a ledger row"), {"cell": s, "query": q, "stage": st})


def _adopted_entries(plan):
    """the run record's adopted entries (kvevaluator.stale) for the stages ledger rows keep"""
    return [{"kind": stale.ADOPTED, "detail": _reused_detail(s, st, q, parts, "a ledger row"),
             "rows": [r["id"] for r in adoptions.covering("capture", *keys, {"cell": s, "query": q, "stage": st})],
             "at": "latency_fresh.plan"} for s, st, q, parts, keys in _kept_adopted(plan)]


def _reused_detail(slug, stage, q, parts, by="--reuse-stale"):
    return f"{slug} {stage} {q}: kept although its {parts} moved ({by})"


def _successor_detail(slug, stage, q, old):
    return f"{slug} {stage} {q}: timed under body {old}, reused unless --fresh (which re-times it)"


def stale_of_plan(plan):
    """the run record's stale entries (kvevaluator.stale) for a plan: the reused stages' rows of an old body, those kept
    by --reuse-stale, and the figures that mix request protocols (run/kvstats.sh notes them in each slug's plan, once per figure set)"""
    out = [{"kind": "under_successor", "detail": _successor_detail(s, st, q, old), "at": "latency_fresh.plan"}
           for s, v in sorted(plan.items()) for st, qs in sorted((v.get("under_successor") or {}).items())
           if st in v["skip"] for q, old in sorted(qs.items())]
    out += [{"kind": "reused_stale", "detail": _reused_detail(s, st, q, parts), "at": "latency_fresh.plan"}
            for s, v in sorted(plan.items()) for st, qs in sorted((v.get("reused_stale") or {}).items())
            if st in v["skip"] for q, parts in sorted(qs.items())]
    out += _adopted_entries(plan)
    mixed = sorted({why for v in plan.values() for why in v.get("protocol_mixed") or []})
    return out + [{"kind": "protocol_mixed", "detail": why, "at": "run/kvstats.sh"} for why in mixed]
