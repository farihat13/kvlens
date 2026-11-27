"""What a built C++ store is, read from its dir and manifest: the key layouts and roles it was ingested
under, whether it is built or an ingest is live, its props codec and chunk target, its schema.
"""
import json
import os
import subprocess
import sys

from kvevaluator.contract import store_build
from kvevaluator.formats.props_codec import PROPS_CODEC_DEFAULT, props_codec
from kvevaluator.capture.binary import binary_path
from kvevaluator.paths import ROOT

_EXP = str(ROOT)


_KEY_LAYOUTS = None


def key_layouts():
    """{format: the key layout this binary reads/writes} from `kvevaluator --version`, once per
    process. A binary from before the field answers {} -- then no layout check can say anything,
    and every check passes (no information is not a refusal)."""
    global _KEY_LAYOUTS
    if _KEY_LAYOUTS is None:
        out = subprocess.run([binary_path(), "--version"], capture_output=True, text=True, check=True).stdout
        _KEY_LAYOUTS = json.loads(out).get("key_layouts", {})
    return _KEY_LAYOUTS


_ENGINE_KEY_LAYOUTS = None


# db -> its C++ ENGINE format (engine fidelity only; the three engine twins of the engine-fidelity port). The
# store dir follows cpp_store_name like any format's (output/stores/ldbc/sf<SF>/dgraph_engine). An engine store is
# ingested with the engine's own facet codec where the format has one (CPP_ENGINE_PROPS_CODEC: dgraph's named facets):
# the props-codec knob normalizes the COMPARED formats, never an engine reproduction.
CPP_ENGINE_FORMATS = {"dgraph": "dgraph_engine", "nebula": "nebula_engine", "arangodb": "arangodb_engine"}


CPP_ENGINE_PROPS_CODEC = {"dgraph_engine": "named", "arangodb_engine": "named"}


def engine_key_layouts():
    """key_layouts() for the ENGINE formats (engine fidelity only), which the binary lists apart so no
    "every format" list picks one up. {} from a binary without them."""
    global _ENGINE_KEY_LAYOUTS
    if _ENGINE_KEY_LAYOUTS is None:
        out = subprocess.run([binary_path(), "--version"], capture_output=True, text=True, check=True).stdout
        _ENGINE_KEY_LAYOUTS = json.loads(out).get("engine_key_layouts", {})
    return _ENGINE_KEY_LAYOUTS


def allocator():
    """The malloc the binary links ("tcmalloc" | "glibc"): provenance.allocator.name; a binary from before the field
    is glibc. Latencies measured on two allocators are not comparable."""
    out = subprocess.run([binary_path(), "--version"], capture_output=True, text=True, check=True).stdout
    return (json.loads(out).get("allocator") or {}).get("name", "glibc")


def key_roles_version():
    """The binary's key_role classifier version (formats/format.h kKeyRolesVersion; 0 = a binary without one)."""
    out = subprocess.run([binary_path(), "--version"], capture_output=True, text=True, check=True).stdout
    return json.loads(out).get("key_roles_version", 0)


def store_roles(path):
    """The logical size of a C++ store per key ROLE: {headline: {sub: [keys, key_bytes, value_bytes]}} (plus
    "labels" where the format names a key's label), from `kvevaluator size --roles` -- cached in <store>/sizes.json,
    keyed by the manifest's sha256 (a re-ingest rewrites it) AND the binary's key_roles_version (a classifier fix
    bumps it), so a cached table is never served stale."""
    import hashlib
    with open(os.path.join(path, "manifest.json"), "rb") as f:
        key = {"manifest_sha256": hashlib.sha256(f.read()).hexdigest(), "key_roles_version": key_roles_version()}
    cache = os.path.join(path, "sizes.json")
    if os.path.exists(cache):
        with open(cache) as f:
            got = json.load(f)
        if all(got.get(k) == v for k, v in key.items()):
            return got
    out = subprocess.run([binary_path(), "size", path, "--repeats", "0", "--roles"],
                         capture_output=True, text=True, check=True).stdout
    res = json.loads(out.strip().splitlines()[-1])
    got = {**key, "roles": res["roles"], "labels": res.get("labels", {})}
    tmp = f"{cache}.{os.getpid()}.tmp"     # one tmp per writer: parallel callers each write a whole, equal table
    with open(tmp, "w") as f:
        json.dump(got, f)
    os.replace(tmp, cache)
    return got


def stale_key_layout(path):
    """(store's layout, the binary's) when the store at `path` was ingested under a key layout this
    binary does not read -- the session guard would refuse it at open; None when fresh, or when
    either side is unknown (no manifest, a format the binary does not list, an old binary)."""
    mp = os.path.join(path, "manifest.json")
    if not os.path.exists(mp):
        return None
    with open(mp) as f:
        m = json.load(f)
    want = key_layouts().get(m.get("format", "")) or engine_key_layouts().get(m.get("format", ""))
    # the nebula family's type-first arm carries its own stamp (Nebula::kKeyLayoutTypeFirst)
    if want and (m.get("value_codec") or {}).get("key_order") == "type_first":
        want += "-typefirst"
    have = m.get("key_codec")
    if want is None or have is None or have == want:
        return None
    return have, want


def check_key_layout(path, rebuild_hint):
    """Refuse a stale store loudly, naming the rebuild -- never re-ingest silently (an sf30 store
    is hours). `rebuild_hint` = the command that rebuilds this store."""
    stale = stale_key_layout(path)
    if stale:
        raise SystemExit(f"store {path} was ingested under key layout {stale[0]!r}; this binary reads "
                         f"{stale[1]!r} -- rebuild it: {rebuild_hint}")


def store_content_identity(manifest):
    """What a store holds beyond its codecs, from its manifest: the schema it was built with (content sha), the record
    stream it was ingested from (sha256), its key count and its ingest counts. A capture keyed on the codecs alone would
    be reused across a re-ingest from another schema (an index declared, a `@reverse` changed) or other data; the
    capture-reuse keys (grid_bodies.fresh, grid_writes.store_identity) compare this too."""
    return {"schema": (manifest.get("schema") or {}).get("content_sha"),
            "records": (manifest.get("records") or {}).get("sha256"),
            "keys": manifest.get("keys"), "ingest_counts": manifest.get("ingest_counts")}


# store_staleness's states: the store can be opened and measured as it is; it is not built (no dir, or a half-built
# one: store_built); this binary refuses it or
# would measure another layout than asked (re-ingest); the probe could not tell (another process holds its LOCK, an IO
# error): never a reason to move a store aside
USABLE, MISSING, STALE, BUSY = "usable", "missing", "stale", "busy"


def store_built(path):
    """(built, why): a C++ store is built only when its manifest.json is there, parses and says settled. The ingest
    writes the manifest last, after its key-count check (ingest.cc, sst_sink.cc), so a dir without one is an ingest
    that stopped midway: a half-built store. Every "reuse it or build it" decision asks this, never whether the dir
    exists (a stopped ingest left four corner stores that the next round took as built)."""
    if not os.path.isdir(path):
        return False, f"no store at {path}"
    try:
        with open(os.path.join(path, "manifest.json")) as f:
            m = json.load(f)
    except FileNotFoundError:
        return False, f"half-built store {path}: no manifest.json (its ingest did not finish)"
    except ValueError as e:                             # (an unreadable manifest, EACCES or EIO, raises: not half-built)
        return False, f"half-built store {path}: its manifest.json does not parse ({type(e).__name__})"
    if not isinstance(m, dict) or not m.get("settled", False):
        return False, f"half-built store {path}: not settled (its ingest did not finish)"
    return True, ""


def codec_suffix(codec=None):
    """what a store's block codec adds to its dir name: nothing for the decided codec (store_config.json's
    rocksdb.compression; None means it), -<codec> for any other (cli/ingest.cc names the dirs it writes the same way)"""
    return "" if codec is None or codec == store_build("compression") else f"-{codec}"


def name_holds_codec(path, codec):
    """True when a store's dir name says it holds `codec`: the name ends -<codec>, or it carries no codec and `codec` is
    the decided one"""
    return os.path.basename(os.path.normpath(path)).endswith(f"-{codec}") or codec_suffix(codec) == ""


def live_ingests(path):
    """pids of running ingest processes (any binary called as `<binary> ingest ...`: the gate's control builds have
    other names) that write the store at path: its --out, or a --multi target, each with every codec of --compression
    (cli/ingest.cc: <out> for the decided codec, <out>-<codec> for the others: codec_suffix). Read from /proc."""
    want = os.path.realpath(path)
    pids = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read().decode(errors="replace").split("\0")
        except OSError:                                     # gone
            continue
        if len(argv) < 2 or argv[1] != "ingest":
            continue
        opt = {argv[i]: argv[i + 1] for i in range(2, len(argv) - 1) if argv[i].startswith("--")}
        codecs = opt.get("--compression", store_build("compression")).split(",")
        outs = ([item.split("=", 1)[-1].split("@")[0] for item in opt["--multi"].split(",")] if "--multi" in opt
                else [opt["--out"]] if "--out" in opt else [])
        for out in outs:
            if not os.path.isabs(out):                      # relative to the process's own cwd
                try:
                    out = os.path.join(os.readlink(f"/proc/{pid}/cwd"), out)
                except OSError:                             # another user's process: its cwd is not readable
                    continue
            out = os.path.realpath(out)
            if want in [out + codec_suffix(c) for c in codecs]:
                pids.append(int(pid))
                break
    return pids


def ingest_needed(path, rebuild=False):
    """THE decision of every store build (the ensure_* here, ingest_multi, check_meta_vs_formats.ensure_store): ingest
    when asked to rebuild or when the store is not built (store_built). The build then runs inside
    store_aside.replacing, which moves a half-built dir into the dated aside dir: never reused, never removed. A dir
    a running ingest is still writing is refused (SystemExit), not moved under it."""
    built, why = store_built(path)
    if built and not rebuild:
        return False
    if os.path.isdir(path):
        pids = live_ingests(path)
        if pids:
            raise SystemExit(f"[store_identity] {path}: an ingest is still writing it (pid {', '.join(map(str, pids))}) -- "
                             f"wait for it, or stop it; it is not moved aside under a running ingest")
        if not built:
            print(f"[store_identity] {why}: to be moved aside and rebuilt", file=sys.stderr)
    return True


def rebuild_hint(sf, db, schema_name, codec, chunk):
    """The run/ingest-cpp-stores.sh line that rebuilds one (format, schema, codec, chunk) store (codec None: the decided)."""
    return (f"run/ingest-cpp-stores.sh --sf {sf} --formats {db} --schema {schema_name}"
            + (f" --codec {codec}" if codec_suffix(codec) else "")
            + (f" --chunk {chunk // 1024}k" if chunk else "") + " --rebuild")


def manifest_schema_path(manifest):
    """The schema file a store manifest names, in THIS tree: a relative path is under the repo root (what ingest records
    for a schema in the tree, so a store copied to another clone resolves there), an absolute one as recorded (a schema
    outside any tree); a relative one whose file moved since resolves through inputs/moved_paths.json. None when the
    manifest names none or the file is gone. The C++ rule is the same
    (schema/schema_path.cc); tests/unit/check_manifest_schema_path.py holds them equal."""
    path = (manifest.get("schema") or {}).get("path")
    if not path:
        return None
    if os.path.isabs(path):
        return path if os.path.exists(path) else None
    here = os.path.join(_EXP, path)
    if os.path.exists(here):
        return here
    table = os.path.join(_EXP, "inputs", "moved_paths.json")      # a file moved in the tree since the store recorded it
    moved = json.load(open(table))["moved"].get(path) if os.path.exists(table) else None
    return os.path.join(_EXP, moved) if moved and os.path.exists(os.path.join(_EXP, moved)) else None


# The PROPERTY codec (cpp/kvevaluator/src/codec/props_codec.h) of the formats that can store property NAMES per value
# (dgraph: posting-entry facets; arangodb: edge and vertex documents):
# schema = values only, the RowV2 row of the declared columns (the fair default: one codec for every
# layout); named = every property name per value, the engines' habit, kept only to reproduce them. The non-default
# rides in the store name (dgraph-named, dgraph-named-c512k), selected everywhere by
# KVEVAL_PROPS_CODEC; every ingest passes it explicitly. (meta's value.edge_props is the same choice for its
# Condensed lists, stated in its spec file -- meta never names vertex props; nebula / pertype_csr store no names.)
PROPS_CODEC_FORMATS = ("dgraph", "arangodb")


def props_codec_tag(db):
    """The store-name tag of the property codec: "" at the default, "-named" otherwise (formats with the knob)."""
    return f"-{props_codec()}" if db in PROPS_CODEC_FORMATS and props_codec() != PROPS_CODEC_DEFAULT else ""


def store_props_codec(path):
    """The edge-prop codec a store was ingested with (its manifest), None for a format without the knob (meta
    included: meta-v4 stores no property names in any layout)."""
    with open(os.path.join(path, "manifest.json")) as f:
        m = json.load(f)
    vc = m.get("value_codec") or {}
    if m.get("format") == "meta":   # meta-v4 has one encoding, never property names (its spec field value is retired)
        return None
    return vc.get("props_codec")


def check_props_codec_name(path, fmt):
    """A store's name and its manifest must agree on the property codec: "-named" in the dir name iff the
    manifest says named. Catches a store ingested by hand under the wrong name, for every caller that opens a
    store by path (CppServer runs it); the resolvers additionally hold the store to the knob (require_props_codec).
    A manifest without the field is left to the session's round-trip guard (a pre-knob store)."""
    if fmt not in PROPS_CODEC_FORMATS or not os.path.exists(os.path.join(path, "manifest.json")):
        return
    got = store_props_codec(path)
    if got is None:
        return
    named_by_name = "-named" in os.path.basename(os.path.normpath(path))
    if (got == "named") != named_by_name:
        raise SystemExit(f"store {path}: its manifest says props_codec={got!r} but its name says "
                         f"{'named' if named_by_name else 'schema'} -- a store under the wrong name; rebuild it "
                         f"(KVEVAL_PROPS_CODEC picks the codec and the name together)")


def require_props_codec(path, want):
    """Refuse a store not built with property codec `want` -- for callers whose numbers only mean something
    under one codec (reproducing an engine needs `named`). Never a silent default."""
    got = store_props_codec(path)
    if got is None:
        raise SystemExit(f"store {path} records no props_codec (built without the knob) -- rebuild it")
    if got != want:
        raise SystemExit(f"store {path} has props_codec={got!r}, this caller needs {want!r} "
                         f"(KVEVAL_PROPS_CODEC={want} selects the {want} store)")
    return path


def store_chunk_target(store_dir):
    """The chunk target (bytes) a C++ store was ingested with: its manifest's value_codec.chunk_target; None for a
    store without chunks (nebula, arangodb, plain dgraph). The STORE's fact, passed to caps_for(chunk=...): the format's
    pack_target decides whether it is a pack size (pertype_csr) or not (dgraph split) -- C++ reads the same field."""
    with open(os.path.join(store_dir, "manifest.json")) as f:
        return (json.load(f).get("value_codec") or {}).get("chunk_target")


def chunk_of_slug(slug):
    """The chunk (bytes) a store slug names (`-c<N>k`, cpp_store_name's tag), None for none: for a caller that has
    a store's name but no store (the optimizer's arm pseudo-formats)."""
    import re
    m = re.search(r"-c(\d+)k(?=-|$)", slug)
    return int(m.group(1)) * 1024 if m else None
