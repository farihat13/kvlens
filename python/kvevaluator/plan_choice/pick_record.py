"""The pick record: which body a store runs for a query, for `meta` cells, mixes and the named formats alike. Three files
in inputs/picks/, all read and written here:
  picks.csv            one row per pick: dataset, workload, scale, store, query, body, body_sha, carried. The deciding
                       scale's rows say it ("3"); a scale's own pick (an exception) is its own row at that scale.
  pick_stores.json     what a reader needs that is not a row: the rule (criterion, margin, decided_at), each mix's or
                       chunk cell's spec sha, each built mix's assignment, and the stores bound at each other scale.
  pick_decisions.json  why each pick was made (rule, why, ratios, the captures' fingerprints, counting, by_reference ...):
                       read only by the writers and their gates (load_full).
Every reader calls `load` (a Picks: lookup by (workload, store, query, scale)); the writers build the record as one
dict, `dump` splits it and `load_full` joins it back. The rule a resolver applies: a query whose plan file holds one
legal body on the store resolves to it; more than one needs a row; scale None = the deciding scale's pick. A row marked
`carried` is v1's selection, carried until the store's capture of every body is chosen on: it is no anchor for a new
decision (anchor_of).

    python3 -m kvevaluator.plan_choice.pick_record --carry-selection WORKLOAD [--write]   # v1's selection where no row is
    python3 -m kvevaluator.plan_choice.pick_record --carry-job CAPTURE [--write]          # JOB's selector bodies, carried
"""
import argparse
import csv
import glob
import hashlib
import json
import os
import sys

from kvevaluator import stale
from kvevaluator.paths import ROOT
from kvevaluator import workloads as _W
RECORD_WORKLOAD = _W.names(pick_params="held-out")[0]   # the record's top section: the held-out rule's workload
from kvevaluator.capture import capture_layout

PICKS_FILE = os.path.join(str(ROOT), "inputs", "picks", "picks.csv")   # the table; the other two files sit beside it
STORES_NAME = "pick_stores.json"
DECISIONS_NAME = "pick_decisions.json"
COLUMNS = ["dataset", "workload", "scale", "store", "query", "body", "body_sha", "carried"]
CARRIED = "carried_v1"
LDBC_NAMED = ["dgraph", "nebula", "arangodb", "pertype_csr", "dgraph-c512k", "pertype_csr-c512k"]
SCALES = ["0.1", "0.3", "30"]      # the non-deciding scales an entry may differ at
# the last plan files with v1's selector (`variants`): main before every run named its body from this record
SELECTOR_TREE = "c6b0c32"
# the record's one-file shapes: convert with experiments/meta/convert_pick_record.py, from the commit
# that added inputs/picks/picks.csv (git log --diff-filter=A -- inputs/picks/picks.csv)
OLD_NAMES = ("pick_record.json", "grid_picks.json")

# where each part of the record (the writers' dict) goes: the rest of a section is its rows and their decisions
RULE_TO_STORES = ("criterion", "margin", "decided_at")
TO_STORES = ("spec_sha", "assignment")
RULE_TO_DECISIONS = ("written", "what", "redecided", "head", "code_states")
SECTION_DECISIONS = ("fingerprints", "counting", "scale_fingerprints")
SECTION_KEYS = ("cells", "scales") + SECTION_DECISIONS

_cache = {}


def files_of(path=None):
    """(the table, the stores file, the decisions file) of the record whose table is `path`; refuses an old one-file record"""
    path = path or PICKS_FILE
    if os.path.basename(path) in OLD_NAMES:
        raise ValueError(f"{path}: a pick record in its one-file shape: convert it to the three files "
                         f"with experiments/meta/convert_pick_record.py --in {path} --out DIR, taken from the commit that "
                         f"added inputs/picks/picks.csv (git log --diff-filter=A -- inputs/picks/picks.csv; git show "
                         f"<commit>:experiments/meta/convert_pick_record.py)")
    d = os.path.dirname(os.path.abspath(path))
    return path, os.path.join(d, STORES_NAME), os.path.join(d, DECISIONS_NAME)


def tag(scale):
    """a dataset tag as the record writes it ("0.1", "3", "30"): 0.10, "3.0" and 3 are the same scale; a tag that is
    not a number (LinkBench's "n1m", JOB's "full") is a key as written"""
    try:
        return f"{float(scale):g}"
    except ValueError:
        return str(scale)


def is_carried(e):
    """an entry or row that is v1's carried selection: a row says `carried`, a writer's entry `rule: carried_v1`"""
    return bool(e.get("carried")) or e.get("rule") == CARRIED


class Picks:
    """THE reader's view of the record (load): the rows of picks.csv and pick_stores.json, nothing of the decisions.
    A row is {"body", "body_sha" (None when the table has none), "carried"}."""

    def __init__(self, path, rows, stores):
        self.path = path
        # what the record holds, as text: its repr, so a key built from a query's data (the mix cost cache's) names the
        # record's content and never the object's address
        self._content_sha = hashlib.sha256(json.dumps([sorted(json.dumps(r, sort_keys=True) for r in rows), stores],
                                                      sort_keys=True, default=str).encode()).hexdigest()
        rule = stores["rule"]
        self.decided_at = rule["decided_at"]
        self.criterion = rule.get("criterion")
        self.margin = rule.get("margin")
        self.spec_sha = dict(stores.get("spec_sha") or {})
        self.assignment = dict(stores.get("assignment") or {})
        self._bound = {wl: {sf: frozenset(v) for sf, v in m.items()} for wl, m in (stores.get("bound_scales") or {}).items()}
        self._deciding = tag(self.decided_at)
        self._rows = {}       # (workload, scale tag, store) -> {query: row}
        for r in rows:
            self._rows.setdefault((r["workload"], r["scale"], r["store"]), {})[r["query"]] = {
                "body": r["body"], "body_sha": r["body_sha"] or None, "carried": r["carried"] == "yes"}

    def __repr__(self):
        return f"Picks(content sha256 {self._content_sha})"

    def entry(self, workload, store, q, scale=None):
        """the row for (store, query) at `scale` (its own row where the scale differs, else the deciding scale's), or
        None; `store` a store or a format name, read as its store key (store_key)"""
        store = store_key(store)
        if scale is not None and tag(scale) != self._deciding:
            e = self._rows.get((workload, tag(scale), store), {}).get(q)
            if e is not None:
                return e
        return self._rows.get((workload, self._deciding, store), {}).get(q)

    def cell(self, workload, store):
        """{query: row} of a store at the deciding scale ({} when the record has none); `store` as the record keys it"""
        return dict(self._rows.get((workload, self._deciding, store), {}))

    def has_cell(self, workload, store):
        return (workload, self._deciding, store) in self._rows

    def stores(self, workload):
        """the stores with rows at the deciding scale, sorted"""
        return sorted(s for (w, sf, s) in self._rows if w == workload and sf == self._deciding)

    def exceptions(self, workload, scale, store):
        """{query: row} of a store's own picks at `scale` (the scale exceptions), {} when none"""
        return dict(self._rows.get((workload, tag(scale), store), {}))

    def bound_at(self, scale, workload=RECORD_WORKLOAD):
        """the stores the record checked at `scale` (the binding rule's set, picks.bound_store)"""
        return set(self._bound.get(workload, {}).get(tag(scale), ()))


def _read_table(path):
    """the table's rows, refused when the header is not COLUMNS, a (workload, scale, store, query) repeats, a dataset is
    not its workload's or `carried` is neither yes nor empty"""
    with open(path, newline="") as f:
        return _read_rows(f, path)


def _read_rows(f, path):
    rd = csv.DictReader(f)
    if rd.fieldnames != COLUMNS:
        raise ValueError(f"{path}: header {rd.fieldnames}, not {COLUMNS}: write the table with pick_record.dump")
    rows = list(rd)
    seen = set()
    for i, r in enumerate(rows, 2):
        key = (r["workload"], r["scale"], r["store"], r["query"])
        if key in seen:
            raise ValueError(f"{path}:{i}: {key} twice (one pick per workload, scale, store and query): delete one row")
        seen.add(key)
        if r["dataset"] != _W.get(r["workload"]).dataset:
            raise ValueError(f"{path}:{i}: dataset {r['dataset']!r} is not workload {r['workload']}'s")
        if r["carried"] not in ("yes", ""):
            raise ValueError(f"{path}:{i}: carried {r['carried']!r} (yes or empty)")
    return rows


def _read_json(path):
    with open(path) as f:
        return json.load(f)


def load(path=None):
    """THE reader: the record whose table is `path` (default PICKS_FILE) as a Picks, cached by its two files' state"""
    table, stores, _dec = files_of(path)
    key = (table,) + tuple((os.stat(p).st_mtime_ns, os.stat(p).st_size) for p in (table, stores))
    if key not in _cache:
        _cache[key] = Picks(table, _read_table(table), _read_json(stores))
    return _cache[key]


def _sections(rec):
    """[(workload, its section)] of a writer's record: LDBC Interactive at the top level, another under `workloads`"""
    return [(RECORD_WORKLOAD, rec)] + sorted((rec.get("workloads") or {}).items())


def _check_section(wl, sec, deciding):
    """refuse what the three files cannot hold as written: a key with no place, a scales entry at the deciding scale
    (its rows would join the deciding scale's), a store with no pick (it would vanish)"""
    if wl != RECORD_WORKLOAD and set(sec) - set(SECTION_KEYS):
        raise ValueError(f"pick record workloads/{wl}: keys {sorted(set(sec) - set(SECTION_KEYS))} have no place")
    if any(tag(sf) == deciding for sf in (sec.get("scales") or {})):
        raise ValueError(f"pick record {wl}: a scales entry at the deciding scale ({deciding}): its picks belong in cells")
    empty = sorted(s for s, qs in (sec.get("cells") or {}).items() if not qs)
    if empty:
        raise ValueError(f"pick record {wl}: cells with no pick ({empty}): a store with no row is no store of the record")


def _split_section(wl, sec, deciding):
    """(rows, the section's decisions) of one workload's section of a writer's record"""
    _check_section(wl, sec, deciding)
    d = {k: sec[k] for k in SECTION_DECISIONS if k in sec}
    levels = [(deciding, sec.get("cells") or {})] + [(tag(sf), v) for sf, v in sorted((sec.get("scales") or {}).items())]
    rows, entries = [], {}
    for sf, per_store in levels:
        for store, qs in sorted(per_store.items()):
            for q, e in sorted(qs.items()):
                row, entries.setdefault(sf, {}).setdefault(store, {})[q] = _row_and_decision(wl, sf, store, q, e)
                rows.append(row)
    d["entries"] = entries
    return rows, d


def _row_and_decision(wl, sf, store, q, e):
    """(the table row, the decision) of one writer's entry: the body, its sha and `carried` go to the row, the rest
    (a carried entry's rule aside) to the decision"""
    if e.get("body_sha", "") is None:
        raise ValueError(f"pick record {wl} {sf} {store} {q}: body_sha null (a sha or no key)")
    carried = e.get("rule") == CARRIED
    row = [_W.get(wl).dataset, wl, sf, store, q, e["body"], e.get("body_sha", ""), "yes" if carried else ""]
    return row, {k: v for k, v in e.items() if k not in ("body", "body_sha") and not (carried and k == "rule")}


def split(rec):
    """(rows, stores, decisions) of a writer's record: the three files' contents. Refuses a key it does not place, so
    nothing is dropped"""
    known = set(RULE_TO_STORES) | set(TO_STORES) | set(RULE_TO_DECISIONS) | {"by_reference", "workloads"} | set(SECTION_KEYS)
    if set(rec) - known:
        raise ValueError(f"pick record: keys {sorted(set(rec) - known)} have no place in the three files (pick_record.split)")
    deciding = tag(rec["decided_at"])
    stores = {"rule": {k: rec[k] for k in RULE_TO_STORES if k in rec}}
    stores.update({k: rec[k] for k in TO_STORES if k in rec})
    dec = {"rule": {k: rec[k] for k in RULE_TO_DECISIONS if k in rec}, "workloads": {}}
    if "by_reference" in rec:
        dec["by_reference"] = rec["by_reference"]
    rows = []
    stores["bound_scales"] = {}
    for wl, sec in _sections(rec):
        got, dec["workloads"][wl] = _split_section(wl, sec, deciding)
        rows += got
        if sec.get("scale_fingerprints"):
            stores["bound_scales"][wl] = {sf: sorted(v) for sf, v in sorted(sec["scale_fingerprints"].items())}
    return sorted(rows), stores, dec


def _join_section(sec, d, by_key, wl, deciding):
    """fill a writer's record section from its decisions `d` and the rows (by_key, popped as used)"""
    sec["cells"] = {}
    sec.update({k: d[k] for k in SECTION_DECISIONS if k in d})
    for sf, per_store in sorted(d["entries"].items()):
        for store, qs in sorted(per_store.items()):
            for q, e in sorted(qs.items()):
                r = by_key.pop((wl, sf, store, q), None)
                if r is None:
                    raise ValueError(f"pick record: a decision for {wl} sf{sf} {store} {q} with no row in the table")
                full = dict(e, body=r["body"])
                if r["body_sha"]:
                    full["body_sha"] = r["body_sha"]
                if r["carried"] == "yes":
                    full["rule"] = CARRIED
                at = sec["cells"] if sf == deciding else sec.setdefault("scales", {}).setdefault(sf, {})
                at.setdefault(store, {})[q] = full


def join(rows, stores, dec):
    """the writer's record from the three files' contents (split's inverse); refuses a row with no decision, a decision
    with no row, and bound scales that are not the scale fingerprints' keys"""
    rec = dict(stores["rule"])
    rec.update({k: stores[k] for k in TO_STORES if k in stores})
    rec.update(dec["rule"])
    if "by_reference" in dec:
        rec["by_reference"] = dec["by_reference"]
    deciding = tag(rec["decided_at"])
    by_key = {(r["workload"], r["scale"], r["store"], r["query"]): r for r in rows}
    bound = stores.get("bound_scales") or {}
    for wl, d in sorted(dec["workloads"].items()):
        sec = rec if wl == RECORD_WORKLOAD else rec.setdefault("workloads", {}).setdefault(wl, {})
        _join_section(sec, d, by_key, wl, deciding)
        if {sf: sorted(v) for sf, v in sorted((sec.get("scale_fingerprints") or {}).items())} != bound.get(wl, {}):
            raise ValueError(f"pick record {wl}: the stores file's bound scales are not the decisions' scale fingerprints")
    if by_key:
        raise ValueError(f"pick record: {len(by_key)} rows with no decision, e.g. {sorted(by_key)[:3]}")
    if set(bound) - set(dec["workloads"]):
        raise ValueError(f"pick record: bound scales for workloads {sorted(set(bound) - set(dec['workloads']))} with no section")
    return rec


def load_full(path=None, ref=None):
    """the writer's record (one dict, today's shape) joined from the three files: for the writers and their gates only
    (tests/lint/check_pick_files_read.py keeps the list); every other reader calls load. `ref`: the files at that git
    commit (`path` then relative to the repo root or under it)"""
    if ref is not None and path and not os.path.isabs(path):
        path = os.path.join(str(ROOT), path)
    table, stores, dec = files_of(path)
    if ref is None:
        return join(_read_table(table), _read_json(stores), _read_json(dec))
    import io
    import subprocess

    def show(p):
        rel = os.path.relpath(p, str(ROOT))
        got = subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:{rel}"], capture_output=True, text=True)
        if got.returncode != 0:
            raise SystemExit(f"pick record at {ref}:{rel}: {got.stderr.strip()}")
        return got.stdout
    rows = _read_rows(io.StringIO(show(table)), f"{ref}:{table}")
    return join(rows, json.loads(show(stores)), json.loads(show(dec)))


def load_full_at(spec):
    """a writer's record from a table's path or from <git ref>:<table path> (the gates' --before)"""
    if not os.path.exists(spec) and ":" in spec:
        ref, path = spec.split(":", 1)
        return load_full(path, ref=ref)
    return load_full(spec)


def _dumps_decisions(x, depth=0):
    """JSON with keys sorted, a dict whose values hold no dict or list on one line (a decision, a fingerprint), the rest
    one key per line: a diff shows one line per moved decision"""
    if isinstance(x, dict) and x and not any(isinstance(v, (dict, list)) for v in x.values()):
        return json.dumps(x, sort_keys=True)
    if isinstance(x, dict) and x:
        pad = " " * (depth + 1)
        items = [f"{pad}{json.dumps(k)}: {_dumps_decisions(x[k], depth + 1)}" for k in sorted(x)]
        return "{\n" + ",\n".join(items) + "\n" + " " * depth + "}"
    return json.dumps(x, sort_keys=True)


def _write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def dump(rec, path=None):
    """write a writer's record as the three files beside `path` (the table's path, default PICKS_FILE): the table's rows
    sorted, the JSON keys sorted"""
    table, stores_p, dec_p = files_of(path)
    rows, stores, dec = split(rec)
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLUMNS)
    w.writerows(rows)
    os.makedirs(os.path.dirname(os.path.abspath(table)), exist_ok=True)
    _write(table, buf.getvalue())
    _write(stores_p, json.dumps(stores, indent=1, sort_keys=True) + "\n")
    _write(dec_p, _dumps_decisions(dec) + "\n")


def entry(workload, store, q, scale=None, rec=None):
    """the record's row for (store, query) at `scale` (Picks.entry), or None; `rec` a Picks (default: load())"""
    return (rec or load()).entry(workload, store, q, scale)


def carried_cells(rec):
    """a writer's record's cells decided on no capture of ours (fingerprint null: the named stores' carried v1 selection);
    a writer that means the cells decided on our captures leaves these out"""
    return sorted(c for c, fp in rec.get("fingerprints", {}).items() if fp is None)


def anchor_of(e):
    """the previous pick a new decision keeps unless a candidate is over the margin cheaper; None for a carried entry
    (v1's selection was decided on no capture of ours): the decision then is the one for a store new to the record (the
    plan rule, candidates.decide: the planner's plan, replaced by one more than the margin cheaper)"""
    if e is None or is_carried(e):
        return None
    return e["body"]


def store_key(name, chunk=None):
    """a store's key in the record: the name of its store dir at the canonical schema and the default knobs,
    <format>[-c<K>k] (stores.cpp_store_name's rule): a chunked per-type format always carries its chunk
    (pertype_csr -> pertype_csr-c64k), another format only a chunk arm's (dgraph, dgraph-c512k); a meta cell or a mix
    as it is. `chunk`: the store's own chunk target, when the name does not carry it"""
    from kvevaluator.plan_choice.optimizer.arms import split_arm, store_chunk_of
    base, arm = split_arm(name)
    c = chunk if chunk is not None else arm if arm is not None else store_chunk_of(base)
    return f"{base}-c{c // 1024}k" if c else base


def names_its_body(workload, ir):
    """a plan file whose body a runner names from this record (and sends alone): the generated files and JOB's; the
    parked label_only files keep `variants` and the C++ selects; a bare plan has one body"""
    return "plans" in ir and ("generated_by" in ir or workload == "job")


def legal_bodies(ir, caps, caps_of_label):
    """the bodies of a plan file legal on a store, measure-only ones left out (they are never picked)"""
    from kvevaluator.plan_choice.candidates import body_lacks
    if "plans" not in ir:
        return [None]
    meta = ir.get("bodies") or {}
    return [b for b in ir["plans"] if not body_lacks(ir, b, caps, caps_of_label) and not meta.get(b, {}).get("measure_only")]


def body_for(workload, store, q, ir, caps, caps_of_label, scale=None):
    """the body the store runs for q: its one legal body, else the record's entry (refused when there is a choice and
    no entry, or the entry names a body not legal there; a body that moved since the record runs today's, marked)"""
    legal = legal_bodies(ir, caps, caps_of_label)
    if len(legal) == 1:
        return legal[0]
    e = entry(workload, store, q, scale)
    if e is None:
        how = f"kvcost measure --dataset ldbc=3 --family {workload} {store}"
        raise ValueError(f"no pick recorded for {store} / {workload} {q} ({len(legal)} legal bodies, "
                         f"{os.path.relpath(PICKS_FILE, str(ROOT))}): measure every body ({how}) and decide "
                         f"(python/kvcost/layout_cost/grid_pick_record.py --repick), or, in a check, run one body by name "
                         f"(check_cpp_vs_python.py --force {q}=BODY)")
    if e["body"] not in legal:
        raise ValueError(f"{workload}/{q} on {store}: the record's pick {e['body']!r} is not a legal body here "
                         f"(legal: {legal})")
    from kvevaluator.plans.ir.bundle import body_sha
    now, was = body_sha(ir, e["body"]), e.get("body_sha")
    if now != was:   # today's body of that name runs, marked: a count-equal successor's, or a moved one
        from kvevaluator.plan_choice.candidates import successor_sha
        kind = "under_successor" if was and successor_sha(workload, q, was) == now else "body_moved"
        stale.note(kind, f"{workload}/{q} on {store}: the record's pick {e['body']!r} was body {was}, today's {now} runs")
    return e["body"]


def _plan_file_at(workload, q, tree):
    """q's plan file as it was at `tree`, or None"""
    import subprocess
    from kvevaluator.plan_choice.candidates import require_commit
    from kvevaluator.paths import checkout_of
    require_commit(str(ROOT / "justfile"), tree)
    for rel in _W.paths_in_old_trees(f"python/kvevaluator/workloads/{workload}/plans/{q}.json"):
        if checkout_of(ROOT) is None:   # a release archive of `tree` (require_commit): its files are the tree's
            if (ROOT / rel).is_file():
                return json.loads((ROOT / rel).read_text())
            continue
        out = subprocess.run(["git", "-C", str(ROOT), "show", f"{tree}:{rel}"], capture_output=True, text=True)
        if out.returncode == 0:
            return json.loads(out.stdout)
    return None


def _carried_selection(workload, rec):
    """(cells, scales, refused) for a writer's record `rec`: for every store the generator lists (and LDBC's named stores, which every workload's
    runner may open) and every generated query (the reads outside the suite too) with a choice there
    (two legal bodies or more) and no entry in the record, the body v1's selector took on that store (the plan file's
    `variants` at SELECTOR_TREE) at SF3, and at each other scale where it differs. refused: (store, query, why) where
    the selector took no body, or one that is not that body today"""
    from kvevaluator.plan_choice import generate as G
    from kvevaluator.plan_choice.candidates import extra_queries, workload_queries
    from kvevaluator.plans.ir.bundle import NoVariant, body_sha, select_variant
    schema = G._schema(workload)
    labels = sorted(set(schema.vertex_props) | set(schema.etypes))
    stores = G.stores(workload)
    stores += [st for st in G.stores("interactive") if st.fmt != "meta" and st.name not in {x.name for x in stores}]
    in_sample = _W.get(workload).pick_params != "held-out"     # the workloads table: only LDBC Interactive has held-out rows
    cells, scales, refused = {}, {}, []
    recorded = dict(_sections(rec)).get(workload, {}).get("cells", {})   # the deciding scale's entries
    for q in list(workload_queries(workload)[0]) + extra_queries(workload):
        path = os.path.join(str(ROOT), "python", "kvevaluator", "workloads", workload, "plans", f"{q}.json")
        if not os.path.exists(path):
            continue
        ir = json.load(open(path))
        if "generated_by" not in ir or "plans" not in ir:          # not a generated file: no record entry
            continue
        old = _plan_file_at(workload, q, SELECTOR_TREE)
        for st in stores:
            sc = G.store_caps(st, schema, labels)
            of_label = lambda label, sc=sc: sc.of_label.get(label, frozenset())
            if len(legal_bodies(ir, sc.caps, of_label)) < 2 or q in recorded.get(store_key(st.name), {}):
                continue
            if old is None or "variants" not in old:
                refused.append((st.name, q, f"no selector at {SELECTOR_TREE}"))
                continue

            def take(scale):
                try:
                    v = select_variant(old, sc.caps, of_label, scale)
                except NoVariant:
                    return None, "the selector takes no body"
                if v not in ir["plans"] or body_sha(ir, v) != body_sha(old, v):
                    return None, f"the selector's {v!r} is not that body today"
                if v not in legal_bodies(ir, sc.caps, of_label):
                    return None, f"the selector's {v!r} is not legal here today"
                return v, None
            at3, why = take("3")
            if at3 is None:
                refused.append((st.name, q, why))
                continue
            note = f"v1's selection (the plan file's selector at {SELECTOR_TREE}), carried until this store's capture"
            cells.setdefault(store_key(st.name), {})[q] = {"body": at3, "body_sha": body_sha(ir, at3), "rule": CARRIED,
                                                "why": note, "in_sample": in_sample}
            for sf in SCALES:
                at, why = take(sf)
                if at is None:
                    refused.append((st.name, q, f"sf{sf}: {why}"))
                elif at != at3:
                    scales.setdefault(sf, {}).setdefault(store_key(st.name), {})[q] = {
                        "body": at, "body_sha": body_sha(ir, at), "rule": CARRIED, "in_sample": in_sample,
                        "why": f"v1's selection at scale {sf} (a per-SF pin), carried"}
    return cells, scales, refused


JOB_FORMATS = ("dgraph", "nebula", "arangodb")   # JOB's named formats (its dgraph store is 512 KB: keyed dgraph-c512k)


def _carried_job(capture):
    """JOB's stores: the body the C++ selector ran on each (store, query) in a capture of every body (the run rows with
    planner_pick 1: the 9 cells under <capture>/runs, the named formats under <capture>/formats/runs), checked against
    today's selection (cells.cell_selection) and today's body sha. In-sample: a JOB query has one param row"""
    from kvevaluator.capture.spec_sets import format_spec
    from kvevaluator.layouts import GRID
    from kvevaluator.plan_choice.cells import cell_selection
    from kvevaluator.plan_choice.candidates import candidates
    from kvevaluator.plans.ir.bundle import body_sha
    from kvevaluator.output_tree import Capture
    counts = Capture(capture).dir("counts")
    stores = {os.path.basename(f)[:-4]: f for f in sorted(glob.glob(os.path.join(counts, "*.csv")))}
    for fmt in JOB_FORMATS:   # the named formats sit beside the layouts
        hit = [s for s in stores if s in (fmt, f"{fmt}-c512k")]
        if len(hit) != 1:
            sys.exit(f"{capture}: no single capture of format {fmt} ({hit})")
    cells, bad = {}, []
    for name, path in sorted(stores.items()):
        spec = (os.path.join(GRID, f"{name}.json") if name.startswith("props_") else format_spec(name))
        rows = [r for r in capture_layout.reader(open(path)) if r["planner_pick"] == "1"]
        today = cell_selection("job", spec, "full", sorted({r["query"] for r in rows}))
        for r in rows:
            q, v = r["query"], r["body"]
            ir, cands = candidates("job", q)
            sha = body_sha(dict(cands)[v], v) if v in dict(cands) else None
            if today.get(q) != v or sha != r["body_sha"]:
                bad.append((name, q, v, today.get(q), r["body_sha"], sha))
                continue
            cells.setdefault(name, {})[q] = {
                "body": v, "body_sha": sha, "rule": CARRIED, "in_sample": True,
                "why": "the body the C++ selector ran in the job-20261012 capture (every body, in-sample), carried"}
    return cells, bad


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--carry-selection", metavar="WORKLOAD",
                      help="add v1's selection (the plan files at SELECTOR_TREE) for every store and query with a "
                           "choice and no entry; an existing entry is kept")
    mode.add_argument("--carry-job", metavar="CAPTURE",
                      help="add JOB's section: the selector's body per (store, query) in a capture of every body "
                           "(the job-20261012 snapshot's meta-grid/, unpacked), checked against today's selection")
    ap.add_argument("--write", action="store_true", help="write the entries into the record (else print a summary)")
    a = ap.parse_args()
    rec = load_full(PICKS_FILE)
    if a.carry_job:
        cells, bad = _carried_job(os.path.abspath(a.carry_job))
        print(f"JOB: {sum(len(v) for v in cells.values())} entries over {len(cells)} stores; differ from today's "
              f"selection or body: {len(bad)} {bad[:5]}")
        if bad:
            sys.exit("a carried JOB body is not today's selection or body: refused")
        if "job" in rec.get("workloads", {}):
            sys.exit("the record has a JOB section already")
        if a.write:
            rec.setdefault("workloads", {})["job"] = {"cells": cells, "fingerprints": {k: None for k in cells}}
            dump(rec, PICKS_FILE)
            print(f"written: {os.path.relpath(PICKS_FILE, str(ROOT))}")
        return
    wl = a.carry_selection
    cells, scales, refused = _carried_selection(wl, rec)
    print(f"{wl}: {sum(len(v) for v in cells.values())} entries over {len(cells)} stores; per-scale: "
          + (", ".join(f"sf{sf} {sum(len(v) for v in s.values())}" for sf, s in sorted(scales.items())) or "none"))
    for r in refused:
        print("  refused:", r)
    if refused:
        sys.exit(f"{len(refused)} (store, query) with a choice and no carried body: refused")
    if not a.write or not cells:
        return
    sec = rec if wl == "interactive" else rec.setdefault("workloads", {}).setdefault(wl, {"cells": {}, "fingerprints": {}})
    for name, per in cells.items():
        sec.setdefault("cells", {}).setdefault(name, {}).update(per)
        sec.setdefault("fingerprints", {}).setdefault(name, None)   # a new store: decided on no capture of ours
    for sf, s in scales.items():
        for name, per in s.items():
            sec.setdefault("scales", {}).setdefault(sf, {}).setdefault(name, {}).update(per)
            sec.setdefault("scale_fingerprints", {}).setdefault(sf, {}).setdefault(name, None)
    dump(rec, PICKS_FILE)
    print(f"written: {os.path.relpath(PICKS_FILE, str(ROOT))}")


if __name__ == "__main__":
    main()
