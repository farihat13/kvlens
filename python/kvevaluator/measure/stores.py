"""C++ stores: their names (the knobs and the meta spec in a name), where they live (one rule for every dataset:
output/stores/<dataset>/<scale>/<name>, e.g. ldbc/sf3/dgraph, linkbench/n1m/nebula; never shared with
the Python stores), building one on first use from the dataset's record stream (ensure_store), and whether a built one
is still usable (staleness).
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile

from kvevaluator import datasets as _D
from kvevaluator import workloads as _W
from kvevaluator.contract import store_build
from kvevaluator.formats.meta import meta_spec
from kvevaluator.formats.pertype_chunk import CHUNKED_FORMATS, pertype_chunk
from kvevaluator.formats.pertype_csr import pertype_far_label
from kvevaluator.formats.props_codec import props_codec
from kvevaluator.measure import store_aside
from kvevaluator.capture.binary import binary_path
from kvevaluator.measure.records import records_cache_path, records_file
from kvevaluator.measure.server import CppServer
from kvevaluator.capture.store_identity import (BUSY, CPP_ENGINE_PROPS_CODEC, MISSING, PROPS_CODEC_FORMATS, STALE,
                                                USABLE, check_key_layout, codec_suffix, ingest_needed, manifest_schema_path,
                                                props_codec_tag, rebuild_hint, require_props_codec, stale_key_layout,
                                                store_built)
from kvevaluator.output_tree import place, stores_dir as _tree_stores_dir
from kvevaluator.paths import store_dir as _stores_root



def meta_config_stale(manifest, spec_path, workload="interactive"):
    """Why a configurable-format store's recorded config is not what THIS binary writes for the spec (None when it is):
    an old store and its capture agree with each other after a change to what the format stores for the same spec, and
    the binary refuses that store at open (own placement's adjacency chunk). The LDBC axis (ldbc,
    microbench, bi, writes) is checked against the committed LDBC schema; another workload's store against the schema
    file its manifest names (gone = a loud stop: a skipped check would call a store of another layout current)."""
    if _D.get(_W.get(workload).dataset).own_axis:
        schema = manifest_schema_path(manifest)
        if schema is None:
            raise SystemExit(f"its manifest's schema {(manifest.get('schema') or {}).get('path')!r} is in neither the "
                             f"recorded place nor this tree: the spec check cannot run (re-ingest the store)")
    else:
        schema = _schema_artifact("canonical")
    with open(spec_path) as f:
        want = canonical_meta_spec(json.load(f), schema)
    return None if manifest.get("value_codec") == want else "its recorded config is not what this binary writes for the spec"


def store_staleness(path, spec_path=None, workload="interactive", layout=True, probe=False):
    """THE check of whether a C++ store can be used as it is: (state, why), state one of USABLE / MISSING / STALE /
    BUSY. From the manifest alone (opens nothing): not built (store_built: no dir, or a half-built one) is MISSING,
    which every caller builds through ingest_needed (a half-built dir moves aside); the key-layout stamp (stale_key_layout);
    with `layout`, the index + filter layout this binary ingests (store_layout); with `spec_path` (a configurable-format
    store), the config this binary writes for the spec (meta_config_stale). `probe` also opens the store the way a run
    does (one plan-less request) for what the manifest cannot show (a schema or codec mismatch at open); a refusal there
    is STALE, an open that fails for any other reason (a LOCK another process holds, an IO error) is BUSY. Every
    "is this store usable" decision calls this: ensure_stores, the gate preflights, reingest --stale, the grid's capture
    reuse (grid_capture.store_is_current)."""
    built, why = store_built(path)
    if not built:
        return MISSING, why
    with open(os.path.join(path, "manifest.json")) as f:
        m = json.load(f)
    stale = stale_key_layout(path)
    if stale:
        return STALE, f"key layout: the store has {stale[0]!r}, this binary reads {stale[1]!r}"
    if layout:
        from kvevaluator.measure import store_layout  # (store_layout imports this module)
        if not store_layout.is_current(m):
            return STALE, (f"index_filter_blocks {store_layout.of_manifest(m)!r}, this binary ingests "
                           f"{store_layout.ingest_default()!r}")
        why = store_layout.build_settings_stale(path, m)   # its codec and filter (inputs/contract/store_config.json)
        if why:
            return STALE, why
    if spec_path is not None:
        why = meta_config_stale(m, spec_path, workload)
        if why:
            return STALE, why
    if probe:
        return _probe_open(path, m)
    return USABLE, ""


def _probe_open(path, manifest):
    """open the store the way a run does: a refusal at `where == "store"` is STALE unless it is a LOCK / IO failure"""
    schema = manifest_schema_path(manifest) or ""
    try:
        with CppServer(path, manifest["format"], **({"schema": schema} if schema else {})) as srv:
            resp = srv.request({"store": {"dir": os.path.abspath(path), "format": manifest["format"],
                                          "schema": os.path.abspath(srv.schema)}})
    except Exception as e:  # -- the server did not start: no verdict
        return BUSY, f"the probe could not run: {e}"
    err = resp.get("error") or {}
    if err.get("where") != "store":
        return USABLE, ""                                   # it opened (a plan-less request then fails at the plan)
    what = str(err.get("what", ""))
    if any(s in what.lower() for s in ("lock", "resource temporarily unavailable", "io error")):
        return BUSY, f"the open failed, not a refusal: {what[:200]}"
    return STALE, f"the binary refuses it: {what[:200]}"


def scale_root(dataset, scale, stores_root=None):
    """the dir a dataset's stores at a scale sit in: <stores root>/<dataset>/<scale> (output_tree.place), e.g.
    stores/ldbc/sf3, stores/linkbench/n1m, stores/imdb-rows/full. `stores_root`: another tree's stores dir (default this
    tree's, cpp_stores_root)"""
    return os.path.join(stores_root, str(place(dataset, scale))) if stores_root else str(_tree_stores_dir(dataset, scale))


def store_dir(dataset, scale, name, spec=None):
    """a named store of the dataset at the scale: <scale_root>/<name> (store_path: a meta name finds its spec's store).
    KVEVAL_STORE_COPIES=<dir>: where <dir>/<its path below the stores root> exists, that dir instead -- a hard-link copy
    of the store made for one gate run (tests/gates/store_copies.py), so a gate part can read the store while another
    process holds the original open (a session opens its store read-write: one opener per dir). A store with no copy
    there is the original."""
    path = store_path(scale_root(dataset, scale), name, spec)
    copies = os.environ.get("KVEVAL_STORE_COPIES")
    if copies:
        copy = copy_dir(copies, dataset, scale, path)
        if os.path.isdir(copy):
            return copy
    return path


def copy_dir(copies, dataset, scale, store):
    """where a copy of `store` (a store dir) sits under a copies root: <copies>/<dataset>/<scale>/<store's name>, the
    stores tree's own shape (store_dir reads it; tests/gates/store_copies.py and the gates that plant a copy write it)"""
    return os.path.join(copies, str(place(dataset, scale)), os.path.basename(store))


def cpp_stores_root():
    """The root of this tree's C++ stores: <output>/stores, or KVEVAL_STORE_DIR=<dir> (another root
    for the stores, <dir>/<dataset>/<scale>/<name>, e.g. to ingest a second set and compare it with the first; the record stream
    cache stays under the default root). Write arenas live under it too (measure/arenas.py)."""
    return str(_stores_root())


# --- stores on first use: the store-dir convention, the ingest (the record stream: records.py) --------------
# The one naming rule every cpp study shares:
#   <format>[-<schema>][-c<N>k][-<codec>]   e.g. pertype_csr-c64k, dgraph-label_only, nebula-zstd
# (the chunk tag: always for the chunked per-type formats, opt-in for dgraph's chunk knob
# (dgraph-c64k, the fairness arm; bare `dgraph` = unbounded postings)).

PERTYPE_INDEX_LAYOUT_DEFAULT = "posting"


def pertype_index_layout():
    """The secondary-index layout the chunked per-type formats' stores are built with:
    KVEVAL_PERTYPE_INDEX_LAYOUT (posting | sorted_keys), unset = posting. sorted_keys builds one key per index
    entry; its stores carry a `-sorted_keys` name tag, so the two never collide."""
    v = os.environ.get("KVEVAL_PERTYPE_INDEX_LAYOUT", PERTYPE_INDEX_LAYOUT_DEFAULT)
    if v not in ("posting", "sorted_keys"):
        raise SystemExit(f"KVEVAL_PERTYPE_INDEX_LAYOUT={v!r}: expected posting or sorted_keys")
    return v


# The chunk KNOB formats: their bare store is the format's own default and an explicit chunk
# rides in the name. dgraph (the fairness arm): unset = unbounded single-KV postings, real
# dgraph's layout and the regression anchor (`dgraph`); KVEVAL_DGRAPH_CHUNK=65536 selects
# `dgraph-c64k`, 524288 `dgraph-c512k` (real dgraph's 512K posting split), everywhere (the grid drivers,
# the write drivers, the kvstats slug).
CHUNK_KNOB_FORMATS = ("dgraph",)


_CHUNK_KNOB_ENV = {"dgraph": "KVEVAL_DGRAPH_CHUNK"}


def knob_chunk(db):
    v = os.environ.get(_CHUNK_KNOB_ENV[db])
    return int(v) if v else None


# The NebulaGraph family's KEY ORDER (cpp/kvevaluator/src/formats/nebula/keys.h): vid_first = the engine's order, the
# default and the baseline; type_first = the other order, kept as an arm so the two orders are measured from one binary. The non-default value rides in the store name (nebula-typefirst),
# selected everywhere by KVEVAL_NEBULA_KEY_ORDER.
NEBULA_KEY_ORDER_DEFAULT = "vid_first"


NEBULA_KEY_ORDER_FORMATS = ("nebula", "nebula_engine")


def nebula_key_order():
    v = os.environ.get("KVEVAL_NEBULA_KEY_ORDER", NEBULA_KEY_ORDER_DEFAULT)
    if v not in ("vid_first", "type_first"):
        raise SystemExit(f"KVEVAL_NEBULA_KEY_ORDER={v!r}: expected vid_first or type_first")
    return v


def far_label_of(db):
    """(setting, its env var) for a format that stores far-end labels, else None: the one place that says which
    formats have the arm"""
    if db in CHUNKED_FORMATS:
        return pertype_far_label(), "KVEVAL_PERTYPE_FAR_LABEL"
    return None


def canonical_meta_spec(spec, schema_json):
    """The spec as meta canonicalizes it (Config::to_json, via `kvevaluator ingest --print-config`), checked
    against the schema on the way (an unknown label / prop fails here, with the binary's message)."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(spec, f)
        spec_file = f.name
    try:
        r = subprocess.run([binary_path(), "ingest", "--schema", schema_json, "--format", "meta", "--out", os.devnull,
                            "--spec", spec_file, "--print-config"], capture_output=True, text=True)
    finally:
        os.unlink(spec_file)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
    if r.returncode or not lines:
        raise SystemExit(f"[stores] meta spec refused: {(r.stderr or r.stdout).strip()}")
    return json.loads(lines[-1])


SPEC_ENV = "KVEVAL_META_SPEC"     # a hand run's default for --spec (cli_spec); library code never reads it


def add_spec_arg(parser):
    """--spec: the meta store's spec file, for a CLI that can run meta (read with cli_spec)"""
    parser.add_argument("--spec", default=None,
                        help=f"meta: the store's spec file (inputs/specs/grid/<cell>.json, ...); default ${SPEC_ENV}")


def cli_spec(value):
    """a CLI's meta spec: its --spec, else $KVEVAL_META_SPEC (the one read of it), with one stderr line saying which;
    None when neither is set (meta then refuses where it needs the spec)"""
    if value:
        _say_once(f"[stores] meta spec {value} (from --spec)")
        return value
    value = os.environ.get(SPEC_ENV)
    if value:
        _say_once(f"[stores] meta spec {value} (from ${SPEC_ENV}: no --spec given)")
    return value or None


# -- the meta store cache -------------------------------------------------------------------------
# A new meta store is named meta-<sha12>: sha12 = meta_spec_sha of its expanded spec (canonical_meta_spec), so the
# store's content names it. A spec edited under the same file name can never open the old store, and two spec files
# holding one layout share one store. A store named before (meta-<spec file stem>) is never renamed or moved:
# store_path finds it by the spec its manifest records. In messages a store shows its layout's short name (the spec
# file's stem) beside the hash; a new store keeps that name in META_LAYOUT_FILE beside its manifest.
META_LAYOUT_FILE = "layout.json"


_META_NAMES = {}          # a meta store name cpp_store_name made -> (sha12, the spec file's stem, the expanded spec)


_CANONICAL = {}           # (spec, schema) -> the expanded spec (one binary call per spec per process)


_META_INDEX = {}          # store root -> (its meta dirs, {sha12: [dirs]}, {dir: (layout name or None, expanded spec)})


_SAID = set()


def meta_spec_sha(canonical):
    """the 12 hex digits a meta store is named by: sha256 of its expanded spec, keys sorted"""
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()[:12]


def meta_label(name):
    """a meta store's name for people: meta-<sha12> (<layout name>)"""
    got = _META_NAMES.get(name)
    return f"{name} ({got[1]})" if got else name


def _canonical_cached(spec, schema_json):
    key = (json.dumps(spec, sort_keys=True), schema_json)
    if key not in _CANONICAL:
        _CANONICAL[key] = canonical_meta_spec(spec, schema_json)
    return _CANONICAL[key]


def _meta_index(root):
    """the meta stores below a store root: {sha12 of the recorded spec: [dirs]} and each dir's layout name and spec,
    read from the manifests; kept per process and read again when a store appears or goes"""
    dirs = tuple(sorted(d for d in os.listdir(root) if d.startswith("meta") and
                        os.path.isfile(os.path.join(root, d, "manifest.json")))) if os.path.isdir(root) else ()
    if root in _META_INDEX and _META_INDEX[root][0] == dirs:
        return _META_INDEX[root]
    by_sha, facts = {}, {}
    for d in dirs:
        with open(os.path.join(root, d, "manifest.json")) as f:
            m = json.load(f)
        if m.get("format") != "meta" or not isinstance(m.get("value_codec"), dict):
            continue
        lay = os.path.join(root, d, META_LAYOUT_FILE)
        name = json.load(open(lay)).get("layout") if os.path.isfile(lay) else None
        by_sha.setdefault(meta_spec_sha(m["value_codec"]), []).append(d)
        facts[d] = (name, m["value_codec"])
    _META_INDEX[root] = (dirs, by_sha, facts)
    return _META_INDEX[root]


def _say_once(msg):
    if msg not in _SAID:
        _SAID.add(msg)
        print(msg, file=sys.stderr, flush=True)


def store_path(root, name, spec=None):
    """THE lookup of a store by name below a store root. A meta store name cpp_store_name made (meta-<sha12>): that dir
    when it exists, else a store named meta-<stem> whose manifest records the same expanded spec, used in place
    (said once), else the new path (a build makes it, after meta_build_guard). An arm named meta-<layout name> with no
    dir of that name: the one store whose layout file names it (two or more: refused, naming them), else the store of
    `spec`'s (a mix equal to a built layout: check-mix measured it on that store). Any other name:
    root/name."""
    path = os.path.join(root, name)
    if os.path.isdir(path) or not name.startswith("meta"):
        return path
    _, by_sha, facts = _meta_index(root)
    if name in _META_NAMES:
        sha, tag, _ = _META_NAMES[name]
        pre, post = name.split(f"-{sha}", 1)
        same = sorted(d for d in by_sha.get(sha, []) if d.startswith(pre + "-") and d.endswith(post))
        if not same:
            return path
        old = f"{pre}-{tag}{post}"
        use = old if old in same else same[0]
        _say_once(f"[stores] reusing {use} for spec {sha} ({tag}): it holds the same expanded spec")
        return os.path.join(root, use)
    tag = name[len("meta-"):]
    named = sorted(d for d, (lay, _) in facts.items() if lay == tag)
    if len(named) > 1:
        raise SystemExit(f"[stores] {name}: {len(named)} meta stores hold a layout named {tag} ({', '.join(named)}): "
                         f"name the store by its spec or by its dir")
    if named:
        return os.path.join(root, named[0])
    if spec:   # no store names this layout (a mix equal to a built layout reused its
        by_spec = cpp_store_name("meta", "canonical", None, None, spec=spec)   # store): the store of the spec, by its sha
        if by_spec != name and os.path.isdir(os.path.join(root, by_spec)):
            _say_once(f"[stores] {name}: the store of its spec, {by_spec}")
            return os.path.join(root, by_spec)
    return path


def _spec_diff(old, new, at=""):
    """the fields where two expanded specs differ, as dotted paths"""
    if isinstance(old, dict) and isinstance(new, dict):
        return [p for k in sorted(set(old) | set(new)) for p in _spec_diff(old.get(k), new.get(k), f"{at}.{k}" if at else k)]
    return [] if old == new else [at or "(the whole spec)"]


def meta_build_guard(root, name, scale):
    """before a meta store is built: never a silent re-ingest of a layout that is already built in another form. A store
    with the same layout name but another expanded spec (made by an older binary, or from an edited spec) is named
    with the fields that differ; at scale 1 or more the build is refused unless it was asked for (kvcost build-store
    sets KVEVAL_BUILD_STORE=1); below, it is built, said once"""
    if name not in _META_NAMES:
        return
    sha, tag, canon = _META_NAMES[name]
    _, _, facts = _meta_index(root)
    pre, post = name.split(f"-{sha}", 1)
    same = sorted(d for d, (lay, _) in facts.items() if (d == f"{pre}-{tag}{post}" or lay == tag) and d != name)
    if not same:
        return
    d = same[0]
    diff = _spec_diff(facts[d][1], canon)
    why = (f"{d} holds the layout {tag} in an older canonical form; the binary now writes {', '.join(diff[:6])}"
           + (f" and {len(diff) - 6} more" if len(diff) > 6 else ""))
    try:
        small = float(scale) < 1
    except (TypeError, ValueError):
        small = False                      # a scale tag (n1m, sample1pct): treated as large
    if not small and os.environ.get("KVEVAL_BUILD_STORE") != "1":
        raise SystemExit(f"[stores] {why}: building {meta_label(name)} at scale {scale} is refused unless asked for: "
                         f"kvcost build-store")
    _say_once(f"[stores] {why}: building {meta_label(name)}")


def note_meta_layout(path, name):
    """after a meta store is built: its layout's short name and spec hash beside its manifest"""
    if name in _META_NAMES:
        sha, tag, _ = _META_NAMES[name]
        with open(os.path.join(path, META_LAYOUT_FILE), "w") as f:
            json.dump({"layout": tag, "spec_sha12": sha}, f, indent=1)
            f.write("\n")


def store_chunk(db):
    """The chunk a format's store name carries: pertype_chunk() for the chunked formats,
    the env knob (None unless asked) for dgraph, none for the rest (no chunk tag)."""
    if db in CHUNKED_FORMATS:
        return pertype_chunk()
    if db in CHUNK_KNOB_FORMATS:
        return knob_chunk(db)
    return None


def cpp_store_name(db, schema_name, codec, chunk, variant=None, schema_json=None, spec=None):
    """the store's dir name; for meta, meta-<sha12> of its expanded spec (the file `spec`), against `schema_json` (default the LDBC
    schema `schema_name`; an own-data store passes its dataset's): the dir it opens is store_path's"""
    # the non-default index layout rides in the name of a chunked per-type store, like the chunk
    layout = "" if db not in CHUNKED_FORMATS or pertype_index_layout() == PERTYPE_INDEX_LAYOUT_DEFAULT \
        else f"-{pertype_index_layout()}"
    if far_label_of(db) and far_label_of(db)[0] != "on":                   # the arm without the stored far-end label
        layout += "-nofarlabel"
    if db in NEBULA_KEY_ORDER_FORMATS and nebula_key_order() != NEBULA_KEY_ORDER_DEFAULT:   # the type-first arm
        layout += "-typefirst"
    meta = None
    if db == "meta":   # the expanded spec's hash names the store (the meta store cache above)
        tag, content = meta_spec(spec)
        canon = _canonical_cached(content, schema_json or _schema_artifact(schema_name))
        meta = (meta_spec_sha(canon), tag, canon)
        layout += f"-{meta[0]}"
    layout += props_codec_tag(db)   # the non-default edge-prop codec
    name = (db + ("" if schema_name == "canonical" else f"-{schema_name}") + ("" if variant is None else f"-{variant}") + layout
            + (f"-c{chunk // 1024}k" if chunk else "") + codec_suffix(codec))
    if meta:
        _META_NAMES[name] = meta
    return name


def meta_store_name(spec_path, dataset=None):
    """the name a built meta store of the spec in this file has: LDBC's (dataset None), or an own-data dataset's, named
    against its schema (format_store_name, the name ensure_store builds)"""
    if dataset is None:
        return cpp_store_name("meta", "canonical", None, None, spec=spec_path)
    return format_store_name(dataset, "meta", spec=spec_path)


def run_ingest(dataset, scale, cmd, quiet=True, variant=None, data=None):
    """Run one `kvevaluator ingest` argv over the dataset's cached record stream at the scale -- the one place every
    store build from a stream goes through (ensure_store, measure/reingest_stores.py). quiet=True hides the binary's
    progress lines but a FAILED ingest is always loud: its stderr is printed before the SystemExit."""
    import sys
    with open(records_file(dataset, scale, variant, data)) as f:
        r = subprocess.run(cmd, stdin=f, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE if quiet else None, text=True)
    if r.returncode != 0:
        if quiet and r.stderr:
            print(r.stderr.rstrip(), file=sys.stderr)
        raise SystemExit(f"[stores] ingest FAILED (exit {r.returncode}): {' '.join(cmd)}")


def csv_root(sf):
    """The LDBC CSV directory the C++ native producer reads (--csv-root): the loader's tree."""
    from kvevaluator.datasets.ldbc.loader import _root
    return _root(sf)


def _schema_artifact(schema_name):
    """LDBC's compiled schema of a design"""
    return schema_artifact(_LDBC, schema_name)


def ingest_multi(sf, formats, schema_name, codec, chunks, ram_budget_gb=0, rebuild=False,
                 quiet=True, root=None):
    """ONE pass over the SF's LDBC CSVs building several formats' stores concurrently
    (`kvevaluator ingest --multi`: the source is parsed once, one encoder per format; a RAM
    budget batches formats, byte-identically). Same store names as ensure_store; every
    requested store is (re)built -- the caller decides which exist. `chunks` = {format: chunk
    or None}: each format's chunk rides with its --multi item (fmt=prefix@chunk), so dgraph at
    its unbounded default and pertype_csr at 64K share one pass. `root` overrides the store
    root (the sf0.1 identity check builds beside the single-pass stores). Returns
    {format: store path}. LDBC's only: the other datasets have no CSV ingest."""
    root = root or scale_root(_LDBC, sf)
    csvs = csv_root(sf)
    if not os.path.isdir(os.path.join(csvs, "dynamic")):
        raise SystemExit(f"ingest_multi: no LDBC CSVs at {csvs} (needs dynamic/ + static/)")
    if chunks is not None and not isinstance(chunks, dict):
        raise TypeError(f"ingest_multi: chunks must be {{format: chunk or None}}, got {chunks!r}")
    names = {db: cpp_store_name(db, schema_name, codec, (chunks or {}).get(db)) for db in formats}
    with store_aside.building([store_path(root, n) for n in names.values()]):   # one builder per store
        return _ingest_multi_locked(sf, formats, schema_name, codec, chunks, ram_budget_gb, rebuild, quiet, root, csvs,
                                    names)


def _ingest_multi_locked(sf, formats, schema_name, codec, chunks, ram_budget_gb, rebuild, quiet, root, csvs, names):
    """ingest_multi's body, under the build locks of every store it builds"""
    import sys
    items, paths = [], {}
    for db in formats:
        c = (chunks or {}).get(db)
        name = names[db]
        path = store_path(root, name)
        if not ingest_needed(path, rebuild):          # a half-built dir is no store: it moves aside below
            raise SystemExit(f"ingest_multi: {path} exists -- pass rebuild to replace it")
        prefix = path[:-len(codec_suffix(codec))] if codec_suffix(codec) else path   # the binary appends -<codec>
        items.append(f"{db}={prefix}" + (f"@{c}" if c else ""))
        paths[db] = path
    os.makedirs(root, exist_ok=True)
    # --out is the single path's prefix; under --multi the binary only takes the codec suffix
    # from it, so a nominal prefix under the root keeps the suffix rule identical
    cmd = [binary_path(), "ingest", "--schema", _schema_artifact(schema_name),
           "--out", os.path.join(root, "multi"), "--compression", codec or store_build("compression"),
           "--csv-root", csvs, "--multi", ",".join(items), "--scale", str(sf)]   # the manifest's `scale`
    if any(db in CHUNKED_FORMATS for db in formats):
        cmd += ["--index-layout", pertype_index_layout()]
    # --far-label is ONE flag for the pass: the formats that have the setting must agree on it
    fars = {db: far_label_of(db)[0] for db in formats if far_label_of(db)}
    if len(set(fars.values())) > 1:
        raise SystemExit(f"one ingest pass cannot build {fars}: --far-label is shared by the pass; build them apart")
    if fars:
        cmd += ["--far-label", next(iter(fars.values()))]
    if any(db in NEBULA_KEY_ORDER_FORMATS for db in formats):
        cmd += ["--key-order", nebula_key_order()]
    if any(db in PROPS_CODEC_FORMATS for db in formats):
        cmd += ["--props-codec", props_codec()]         # one codec per pass (the binary shares codec_config)
    if ram_budget_gb:
        cmd += ["--ram-budget-gb", str(ram_budget_gb)]
    print(f"[stores] one-pass ingest of {', '.join(formats)} at sf{sf}", file=sys.stderr)
    with store_aside.replacing(list(paths.values())):   # rebuild: the old stores move aside; a failure puts them back
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE if quiet else None, text=True)
        if r.returncode != 0:
            if quiet and r.stderr:
                print(r.stderr.rstrip(), file=sys.stderr)
            raise SystemExit(f"[stores] ingest FAILED (exit {r.returncode}): {' '.join(cmd)}")
        for db, path in paths.items():
            assert os.path.isdir(path), f"ingest did not produce {path}"
    return paths


def schema_artifact(dataset, schema_name="canonical"):
    """the schema.json a dataset's stores are ingested and opened with (its manifest's; LDBC also has its named
    designs, schema_<name>.json)"""
    if dataset != _LDBC and schema_name != "canonical":
        raise SystemExit(f"{dataset}: no schema design {schema_name!r} (only LDBC has designs)")
    try:
        return _D.schema_path(dataset, schema_name)
    except FileNotFoundError as e:
        raise SystemExit(str(e))


_LDBC = "ldbc"   # the dataset with schema designs, data variants and a one-pass CSV ingest (ingest_multi)


def default_chunk(dataset, db):
    """the chunk a store is ingested with and named by when the caller names none: the format's knob (store_chunk).
    Where the dataset's manifest sets a chunk for the format (store_knobs: IMDb's Dgraph store) the knob MUST be set:
    forgetting it would measure unbounded postings (14.8 M uids in one value at JOB's full tag) under the name
    `dgraph`. dataset_defaults() sets it to the reported 512 KB; 0 asks for the unchunked store by name (the Python
    oracle's twin has unbounded postings)."""
    chunk = store_chunk(db)
    knob = _D.get(dataset).store_knobs.get(db, {}).get("chunk")
    if knob is not None and _CHUNK_KNOB_ENV[db] not in os.environ:
        raise SystemExit(f"{dataset}: set {_CHUNK_KNOB_ENV[db]} ({knob} = the reported split; 0 = the "
                         f"unchunked store, for the Python oracle) -- stores.dataset_defaults() sets the reported one")
    return chunk


def dataset_defaults(dataset):
    """the knobs every entry point on the dataset runs under unless the caller set them: its manifest's store_knobs
    (IMDb: the Dgraph format at its reported 512 KB posting split)"""
    for db, knobs in _D.get(dataset).store_knobs.items():
        if "chunk" in knobs:
            os.environ.setdefault(_CHUNK_KNOB_ENV[db], str(knobs["chunk"]))


def format_store_dir(dataset, scale, db, spec=None, chunk="default", codec=None, schema_name="canonical",
                     variant=None):
    """the dir ensure_store gives for these arguments, without building it"""
    return store_dir(dataset, scale, format_store_name(dataset, db, spec, chunk, codec, schema_name, variant), spec)


def format_store_name(dataset, db, spec=None, chunk="default", codec=None, schema_name="canonical", variant=None):
    """the name of the store format_store_dir gives (its dir's last part, before store_path's lookup of an older name)"""
    if chunk == "default":
        dataset_defaults(dataset)
        chunk = default_chunk(dataset, db)
    return cpp_store_name(db, schema_name, codec, chunk, variant, schema_json=schema_artifact(dataset, schema_name),
                          spec=spec)


def _ingest_cmd(db, schema_json, out, codec, scale, chunk):
    """`kvevaluator ingest` for one store: every flag the store's name carries, the same for every dataset"""
    cmd = [binary_path(), "ingest", "--schema", schema_json, "--format", db, "--out", out,
           "--compression", codec or store_build("compression"), "--scale", str(scale)]   # the manifest's `scale`
    if chunk:
        cmd += ["--chunk", str(chunk)]
    if db in CHUNKED_FORMATS:
        cmd += ["--index-layout", pertype_index_layout()]
    if far_label_of(db):
        cmd += ["--far-label", far_label_of(db)[0]]
    if db in NEBULA_KEY_ORDER_FORMATS:
        cmd += ["--key-order", nebula_key_order()]
    if db in PROPS_CODEC_FORMATS:
        cmd += ["--props-codec", props_codec()]
    if db in CPP_ENGINE_PROPS_CODEC:
        cmd += ["--props-codec", CPP_ENGINE_PROPS_CODEC[db]]   # the engine's own facets, whatever the knob
    return cmd


def store_input_stale(dataset, scale, path, schema_name="canonical", variant=None):
    """Why a BUILT store must not be reused for what it was ingested from (None = it may be): built under another
    schema than the dataset's schema.json now, or from other records than the scale's cache holds now (compared when
    the cache's key names its content). The one rule for ensure_store and the gate preflight."""
    with open(schema_artifact(dataset, schema_name)) as f:
        want = json.load(f)["hash"]
    with open(os.path.join(path, "manifest.json")) as f:
        m = json.load(f)
    # a store is reused by its dir name: refuse one built under another schema (e.g. before `link reverse`
    # was dropped) -- never measure a stale layout silently
    got = m.get("schema", "")
    if want not in str(got):
        return (f"{dataset} store {path} was built under another schema "
                f"(its manifest: {str(got)[:90]}...; current schema.json hash {want[:16]}...) -- "
                f"remove the store dir and re-run, it is re-ingested on first use")
    # ... and from the records the cache holds now (the manifest keeps the ingested stream's sha256)
    from kvevaluator.measure import records_key
    kp = records_key.key_path(records_cache_path(dataset, scale, variant))
    if os.path.exists(kp):
        with open(kp) as f:
            rec = json.load(f).get(records_key.CONTENT)
        have = m.get("records", {})
        have = have.get("sha256") if isinstance(have, dict) else str(have)
        if rec and have and have != rec:
            return (f"{dataset} store {path} was ingested from other records "
                    f"({str(have)[:16]}... vs the cache's {rec[:16]}...) -- remove the store dir and re-run")
    return None


def _check_built(dataset, scale, path, db, name, schema_name, codec, chunk, variant, spec):
    """every check on a built store, the same for every dataset: what its name says it holds is what it holds"""
    hint = (rebuild_hint(scale, db, schema_name, codec, chunk) if dataset == _LDBC
            else "remove the store dir and re-run (it is re-ingested on first use)")
    # a store from before a key-layout bump is reused by name but refused at open: say so here
    check_key_layout(path, hint)
    why = store_input_stale(dataset, scale, path, schema_name, variant)
    if why:
        raise SystemExit(f"[stores] {why}")
    if db in CPP_ENGINE_PROPS_CODEC:
        require_props_codec(path, CPP_ENGINE_PROPS_CODEC[db])
    if db in PROPS_CODEC_FORMATS:
        # the name picks the codec, the manifest must agree: a store ingested by hand under another
        # --props-codec round-trips at open (session.cc) yet holds the other codec's bytes
        require_props_codec(path, props_codec())
    # an arm rides in the store's NAME; the manifest must hold that arm (a store rebuilt by hand, or by a tool that
    # dropped the flag, would otherwise be measured as the other arm)
    with open(os.path.join(path, "manifest.json")) as f:
        vc = json.load(f).get("value_codec") or {}
    if db in NEBULA_KEY_ORDER_FORMATS and vc.get("key_order", NEBULA_KEY_ORDER_DEFAULT) != nebula_key_order():
        raise SystemExit(f"store {path} holds key_order={vc.get('key_order')!r}, its name says {nebula_key_order()!r} "
                         f"(KVEVAL_NEBULA_KEY_ORDER) -- rebuild it")
    if far_label_of(db) and ("on" if vc.get("far_label", True) else "off") != far_label_of(db)[0]:
        raise SystemExit(f"store {path} holds far_label={vc.get('far_label')!r}, its name says {far_label_of(db)[0]!r} "
                         f"({far_label_of(db)[1]}) -- rebuild it")
    if db == "meta":
        # a spec file names its store: the ingested (canonical) spec must equal the file's canonical form, else
        # the file was edited after its store was built -- never measure the wrong cell
        want = _canonical_cached(meta_spec(spec)[1], schema_artifact(dataset, schema_name))
        if vc != want:
            raise SystemExit(f"[stores] {name}: the store's spec differs from {spec}\n"
                             f"  store: {json.dumps(vc, sort_keys=True)}\n  file:  {json.dumps(want, sort_keys=True)}\n"
                             f"  (the file was edited after its store was built -- or this binary writes a spec differently: "
                             f"remove the stale store)")


def ensure_store(dataset, scale, db, spec=None, chunk="default", codec=None, schema_name="canonical", variant=None,
                 data=None, rebuild=False, quiet=True):
    """THE entry for a C++ store: one format of the dataset at a scale (LDBC's sf, or a data tag), ingested on first use
    from the dataset's record stream (records_file) under scale_root, at the dataset's knobs (dataset_defaults).
    `chunk`: "default" = the format's (default_chunk), None = no chunk, else bytes. `schema_name` (a named design) and
    `variant` (a data variant) are LDBC's; `data` an own-data dataset's data dir for its first dump. Returns the store
    path. quiet=False lets the binary's progress lines through (the ingest driver's choice)."""
    import sys
    dataset_defaults(dataset)
    if chunk == "default":
        chunk = default_chunk(dataset, db)
    sj = schema_artifact(dataset, schema_name)
    name = cpp_store_name(db, schema_name, codec, chunk, variant, schema_json=sj, spec=spec)
    path = store_dir(dataset, scale, name, spec)
    # one builder per store: a second process that needs it waits on the build lock, then finds it built; a built
    # store takes no lock
    if rebuild or not store_built(path)[0]:
        records_file(dataset, scale, variant, data)   # a missing dataset stops here, before the lock and the store dir
        with store_aside.building(path):
            if ingest_needed(path, rebuild):   # not built (no dir, or half-built: moved aside), or rebuild
                os.makedirs(os.path.dirname(path), exist_ok=True)   # a root named by KVEVAL_STORE_DIR may be new
                out = path[:-len(codec_suffix(codec))] if codec_suffix(codec) else path
                cmd = _ingest_cmd(db, sj, out, codec, scale, chunk)
                spec_file = None
                if db == "meta":   # meta's config IS its spec JSON, passed as a temp file
                    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                        json.dump(meta_spec(spec)[1], f)
                        spec_file = f.name
                    cmd += ["--spec", spec_file]
                meta_build_guard(os.path.dirname(path), name, scale)
                print(f"[stores] ingesting {dataset} {scale}/{meta_label(name)} ({'rebuild' if rebuild else 'first use'})",
                      file=sys.stderr)
                try:
                    with store_aside.replacing(path):   # rebuild: the old store moves aside; a failure puts it back
                        run_ingest(dataset, scale, cmd, quiet, variant, data)
                        note_meta_layout(path, name)
                        assert os.path.isdir(path), f"ingest did not produce {path}"
                finally:
                    if spec_file:
                        os.unlink(spec_file)
    _check_built(dataset, scale, path, db, name, schema_name, codec, chunk, variant, spec)
    return path


def main(argv=None):
    """`python -m kvevaluator.measure.stores root <dataset> <scale>`: the dir the dataset's stores at the scale sit in
    (scale_root); `records <dataset> <scale>`: its record cache's path (records_cache_path). For the shell drivers,
    which never build a store path by hand."""
    import argparse
    ap = argparse.ArgumentParser(description=main.__doc__.split(";")[0])
    ap.add_argument("what", choices=("root", "records"))
    ap.add_argument("dataset", choices=_D.names())
    ap.add_argument("scale")
    a = ap.parse_args(argv)
    print(scale_root(a.dataset, a.scale) if a.what == "root" else records_cache_path(a.dataset, a.scale))


if __name__ == "__main__":
    main()
