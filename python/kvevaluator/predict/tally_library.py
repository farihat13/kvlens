"""Tallies per setting, and the tally of any meta spec by lookup, with no pass over the records per spec.

A tally row depends only on the block its store part is stored with: a props row of label L on L's props block, an
adjacency row of (edge type E, direction d) on (E, d)'s block, an index row on the index block. So a spec's tally is
assembled row by row from tallies whose spec gives that part the same block; a mix is assembled the same way.
A setting is one (side, block): side vertex_props or adjacency, the block as config.cc parses it.

The library holds one tally per setting spec (setting-<sha12>.json beside its .key.json), keyed by the setting spec,
the record stream's freshness key and the tally version (kTallyVersion, bumped by hand when a tally's rows can change;
tests/gates/check_tally_version.py fails a change without a bump). The building binary is provenance, not key. A dirty
binary reuses nothing, and an entry a dirty binary built is built again; an entry with no version is reused only by
the binary that built it, until --stamp adopts it. An entry from another record stream is refused (fresh=True builds
it again). A block no seed serves gets a one-sided setting spec, built on demand (that block on its side, Split on the
other). A library writes only below its run's output root, checked on the real path: a linked settings dir is read,
never rebuilt in place.

    python -m kvevaluator.predict.tally_library --spec S.json --records R.jsonl --library DIR [--fresh] \\
        (> tally.json | --out-dir D: D/<the spec's store name, meta-sha12>.json, what kvcost predict --tally-dir reads)
    python -m kvevaluator.predict.tally_library --stamp DIR     # adopt DIR's unversioned entries as version 1 (stamp())
"""
import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys

from kvevaluator.output_tree import stats_dir
from kvevaluator.paths import ROOT, cpp_bin, recorded_path, stats_dir as stats_root, store_dir
from kvevaluator.datasets import schema_path
from kvevaluator.predict import assemble_tally as A
from kvevaluator.output_tree import Tree

SCHEMA = schema_path("ldbc")
PROPS, ADJ = "vertex_props", "adjacency"
SHARED = ("version", "direction", "index")       # top-level blocks every setting spec copies from the spec it serves
SPLIT = {"group": "item", "owners": "one", "split": "never"}   # the other side of a one-sided setting spec
DIRS = ("out", "in")
PER_TYPE = ("vertex_props_by_label", "adjacency_by_etype", "adjacency_by_etype_dir")
# The commit whose C++ is tally version 1's counting code (the last one before the version existed). stamp() adopts an
# unversioned entry as version 1 only when the clean binary that built it has this commit's cpp/ tree.
V1_CPP_COMMIT = "1bcf72ca75c4d1774854b288f683fda45fd86fab"


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def canonical(x):
    return json.dumps(x, sort_keys=True, separators=(",", ":"))


def block(raw, side):
    """a component in one form (config.cc component_of's defaults filled; adjacency's default fields left out), so two
    specs that store a part the same way give equal blocks"""
    raw = raw or {}
    out = {"group": raw.get("group", "item"), "owners": raw.get("owners", "one"), "split": raw.get("split", "never")}
    if side == ADJ:
        if raw.get("edge_props", "adjacency") == "own":
            out["edge_props"] = "own"
        if raw.get("far_label", True) is False:
            out["far_label"] = False
    return out


def default_block(spec, side):
    raw = spec.get(side)
    if side == PROPS and isinstance(raw, dict) and "group" not in raw:    # only groups on top of the default layout
        raw = None
    return block(raw, side)


def props_of(spec, label):
    """the props block label is stored with (Config::props_of: the per-label override, else the default)"""
    by = spec.get("vertex_props_by_label", {})
    return block(by[label], PROPS) if label in by else default_block(spec, PROPS)


def adj_of(spec, etype, direction):
    """the block (etype, direction)'s lists are stored with (Config::adj_of: per direction, per edge type, default)"""
    d = spec.get("adjacency_by_etype_dir", {}).get(etype, {})
    if direction in d:
        return block(d[direction], ADJ)
    by = spec.get("adjacency_by_etype", {})
    return block(by[etype], ADJ) if etype in by else default_block(spec, ADJ)


def shared_of(spec):
    """what a setting spec must share with the spec it serves: version, direction, index, props groups"""
    out = {k: spec[k] for k in SHARED if k in spec}
    groups = spec.get(PROPS, {}).get("groups") if isinstance(spec.get(PROPS), dict) else None
    if groups is not None:
        out["groups"] = groups
    return out


def setting_spec(side, blk, shared):
    """the one-sided setting spec of (side, blk): blk on its side, Split on the other, the shared blocks copied"""
    spec = {k: copy.deepcopy(v) for k, v in shared.items() if k != "groups"}
    spec[PROPS] = dict(blk) if side == PROPS else dict(SPLIT)
    spec[ADJ] = dict(blk) if side == ADJ else dict(SPLIT)
    if "groups" in shared:
        spec[PROPS]["groups"] = copy.deepcopy(shared["groups"])
    return spec


def serves(spec):
    """the (side, block) pairs a uniform spec's tally serves: its default props and adjacency blocks"""
    for k in ("vertex_props_by_label", "adjacency_by_etype", "adjacency_by_etype_dir"):
        if spec.get(k):
            raise ValueError(f"a setting spec is uniform: {k} given")
    return [(PROPS, default_block(spec, PROPS)), (ADJ, default_block(spec, ADJ))]


def refuse(msg):
    raise SystemExit(f"tally_library: {msg}: refused")


def made_by(prov):
    """the commit and dirty flag of the binary a provenance names (--version's, or a tally's own "provenance"): a
    release archive's own binary counts as clean, a copy's with no commit as "unknown" (binary.binary_state)"""
    from kvevaluator.capture.binary import binary_state
    dirty = {"clean": False, "release": False, "unknown": "unknown"}.get(binary_state(prov), True)
    return {"git_sha": prov.get("binary_git_sha"), "dirty": dirty}


def binary_info():
    """the binary's commit, dirty flag (made_by) and tally version (None from a binary older than the version), from
    --version"""
    v = json.loads(subprocess.run([str(cpp_bin()), "--version"], capture_output=True, text=True, check=True).stdout)
    return made_by(v), v.get("tally_version")


def reusable(have_binary, have_version, binary, version):
    """may a tally made by have_binary (its commit and dirty flag) at have_version serve this binary at version? The
    same version from clean binaries; an unversioned tally only to the clean binary that made it"""
    if binary["dirty"] or have_binary.get("dirty", True):
        return False
    if have_version is not None:
        return have_version == version
    return have_binary.get("git_sha") == binary["git_sha"]


def made_from_stream(recorded, records):
    """whether a tally's recorded stream (its "records", record_stream_at_open's label) is the stream at `records` now:
    by its freshness key's sha256 when the stream has a key file, else by its bytes"""
    from kvevaluator.measure import records_key
    kp = records_key.key_path(records)
    if os.path.exists(kp) and recorded.get("key_sha256"):
        return recorded["key_sha256"] == records_key.stream_ids(kp)["key_sha256"]
    return bool(recorded.get("content_sha256")) and recorded["content_sha256"] == records_key.file_sha256(records)


def _ledger_key(k):
    """what a tally entry is made under, for the reuse ledger: the binary that tallied it and its tally version (the
    stream and schema are identity: an entry of another one is never adopted)"""
    from kvevaluator.adoptions import key_sha
    return key_sha({"binary": (k.get("binary") or {}).get("git_sha"), "tally_version": k.get("tally_version")})


# one setting's pass over the record stream, its peak memory over the stream's file size: it holds every edge until it
# writes the lists (Chunked/Chunked at 64 KB, the heaviest setting: 5.14 GB on a 6.17 GB stream, 54.70 GB on a
# 62.80 GB one; experiments/predict_statistics/README.md, "One diagonal")
PEAK_OVER_RECORDS = 0.9


def pass_peak_gb(records):
    """the peak memory of one setting's pass over the record stream `records`, in GB (PEAK_OVER_RECORDS)"""
    return PEAK_OVER_RECORDS * os.path.getsize(records) / 2**30


LIBRARY = "tallies"      # a library's dir name: the shared one in the stats dir, a study's own below its output dir


def library_dir(dataset, scale):
    """a dataset's tally library at a scale: output/stats/<dataset>/<scale>/tallies/"""
    return os.path.join(str(stats_dir(dataset, scale)), LIBRARY)


class Library:
    """one directory of setting tallies over one record stream, keyed by spec, stream and tally version"""

    def __init__(self, path, records, seeds=(), schema=SCHEMA, fresh=False, quiet=False, root=None, reuse_stale=False):
        from kvevaluator.measure import records_key
        self.path, self.records, self.schema, self.fresh, self.quiet = path, os.path.abspath(records), str(schema), fresh, quiet
        self.reuse_stale = reuse_stale   # --reuse-stale: keep an entry of another stream or binary (never another version)
        self.root = str(root if root is not None else Tree().root)
        kp = records_key.key_path(self.records)
        if os.path.exists(kp):
            with open(kp) as f:
                key = json.load(f)
        else:   # keyed by the stream's bytes alone: a tally made here matches only a stream of the same bytes
            from kvevaluator import stale
            key = {records_key.CONTENT: records_key.file_sha256(self.records),
                   records_key.KEYED_BY_CONTENT: "no freshness key beside the stream"}
            stale.note("stream_keyed_by_content", f"the record stream {self.records} has no freshness key ({kp}): the "
                       f"library keys its tallies on the stream's content sha")
        self.stream_key = sha256_bytes(canonical(key).encode())
        self.stream_sources = sources_sha256(key)
        self.stream_content = key.get(records_key.CONTENT)             # the stream's bytes (None: dumped before)
        self.stream_earlier = key.get(records_key.SAME_CONTENT, [])     # earlier dumps of the same bytes
        self.schema_sha = records_key.file_sha256(self.schema)        # what a tally encodes with (indexes, kinds)
        self.binary, self.version = binary_info()
        self.seeds = [json.loads(canonical(s)) for s in seeds]     # uniform specs, tried first, in this order
        if not os.path.isdir(path):
            self.check_writable()
            os.makedirs(path, exist_ok=True)
        for k in sorted(os.listdir(path)):     # then the uniform entries already here, from this stream
            if k.startswith("setting-") and k.endswith(".key.json"):
                with open(os.path.join(path, k)) as f:
                    have = json.load(f)
                if self.same_stream(have) and have["spec"] not in self.seeds:
                    try:
                        serves(have["spec"])
                    except ValueError:
                        continue
                    self.seeds.append(have["spec"])    # a one-sided entry serves both its sides too
        self.built = []                                           # (setting spec sha12, seconds) this process built
        self.reused = []                                          # setting spec sha12 this process reused
        self.loaded = {}                                          # tally path -> the tally, one object per entry

    def stream_check(self, have):
        """(same, via_earlier, why) of an entry's key against this stream and schema. An entry that names its schema
        must name this one. Then by the stream's bytes when both name them; else (an entry made before the bytes were
        recorded) by its sources when it names them (a re-dump of the same sources by another commit is the same
        stream), else by the whole freshness key: of this stream (whose key holds the schema), or of an earlier dump of
        the same bytes (same_content_as) made under this schema"""
        if have.get("schema_sha256") and have["schema_sha256"] != self.schema_sha:
            return False, False, f"schema {have['schema_sha256'][:12]}, now {self.schema_sha[:12]}: the schema changed"
        if have.get("records_content_sha256") and self.stream_content:
            if have["records_content_sha256"] == self.stream_content:
                return True, False, ""
            return False, False, (f"content {have['records_content_sha256'][:12]}, now {self.stream_content[:12]}: the "
                                  f"stream was dumped again with other bytes")
        if have.get("records_sources_sha256"):
            mine, own, field = have["records_sources_sha256"], self.stream_sources, "sources_sha256"
        else:
            mine, own, field = have.get("records_key_sha256"), self.stream_key, "records_key_sha256"
        if mine == own:
            return True, False, ""
        if any(e.get(field) == mine and e.get("schema_json_sha256") == self.schema_sha for e in self.stream_earlier):
            return True, True, ""
        return False, False, (f"key {str(have.get('records_key_sha256'))[:12]}, now {self.stream_key[:12]}: the stream was "
                              f"dumped again before its bytes were recorded, or under another schema")

    def same_stream(self, have):
        """an entry's key made from this record stream under this schema (stream_check)"""
        return self.stream_check(have)[0]

    def adopt(self, tally, key, have, t):
        """an entry kept through same_content_as: its key and its tally's records block get this stream's
        content_sha256 and the schema's sha, so every reader (same_records, the C++ predict) compares them by content.
        In memory always; on disk when the library is this run's own"""
        t["records"] = {**t.get("records", {}), "content_sha256": self.stream_content, "schema_sha256": self.schema_sha}
        real, root = os.path.realpath(self.path), os.path.realpath(self.root)
        if real != root and not real.startswith(root + os.sep):
            return
        for path, obj in ((tally, t), (key, {**have, "records_content_sha256": self.stream_content,
                                             "schema_sha256": self.schema_sha, "adopted": "same_content_as"})):
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w") as f:
                json.dump(obj, f, indent=1, sort_keys=path == key)
                f.write("\n")
            os.replace(tmp, path)

    def entry(self, spec):
        """(path, tally) of a setting spec: reused when its key matches (spec, record stream, tally version: reusable()),
        built when missing or not reusable, refused when made from another record stream (unless fresh)"""
        text = canonical(spec)
        sha = sha256_bytes(text.encode())
        stem = os.path.join(self.path, f"setting-{sha[:12]}")
        tally, key = f"{stem}.json", f"{stem}.key.json"
        if tally in self.loaded:
            return tally, self.loaded[tally]
        want = {"spec": json.loads(text), "spec_sha256": sha, "records": recorded_path(self.records, store_dir()),
                "records_key_sha256": self.stream_key,
                "records_sources_sha256": self.stream_sources, "records_content_sha256": self.stream_content,
                "schema_sha256": self.schema_sha, "tally_version": self.version, "binary": self.binary}
        if os.path.exists(tally) and os.path.exists(key):
            with open(key) as f:
                have = json.load(f)
            if have.get("spec_sha256") != sha:
                refuse(f"{key} holds another spec (sha {str(have.get('spec_sha256'))[:12]})")
            same, via_earlier, why = self.stream_check(have)
            moved = [] if same else [f"its record stream or schema ({why})"]
            if not reusable(have.get("binary", {}), have.get("tally_version"), self.binary, self.version):
                moved.append(f"its binary {str(have.get('binary', {}).get('git_sha'))[:10]} (dirty "
                             f"{have.get('binary', {}).get('dirty')})")
            other_version = None not in (have.get("tally_version"), self.version) and have["tally_version"] != self.version
            ledger = None
            if same and moved and not other_version and not self.fresh and not self.binary.get("dirty"):
                # only the (clean) binary moved: a reuse ledger row may declare the entry current
                from kvevaluator import adoptions
                from kvevaluator.measure import records_key
                ledger = ("tally", records_key.file_sha256(tally), _ledger_key(have), _ledger_key(want), {"spec": sha[:12]})
                adopted = adoptions.adopt(*ledger[:4], f"{tally}: {'; '.join(moved)} moved", ledger[4])
            else:
                adopted = False
            keep = adopted or self.reuse_stale and not self.fresh and moved and not other_version
            hint = f"; or {adoptions.add_hint(*ledger)}" if ledger else ""
            if keep and not adopted:
                from kvevaluator import stale
                stale.note("reused_stale", f"{tally}: kept although {'; '.join(moved)} moved (--reuse-stale{hint})")
            elif not keep and not same and not self.fresh:
                refuse(f"{tally} was made from another record stream or schema ({why}); fresh=True (--fresh) builds it "
                       f"again, --reuse-stale keeps it{hint}")
            if keep or same and reusable(have.get("binary", {}), have.get("tally_version"), self.binary, self.version):
                with open(tally) as f:
                    t = json.load(f)
                if via_earlier:
                    self.adopt(tally, key, have, t)
                if "tally_version" not in t and have.get("tally_version") is not None:
                    # a stamped entry (stamp() writes its key only): the tally is of its key's version, as one built now says
                    t = {"format": t.get("format"), "tally_version": have["tally_version"],
                         **{k: v for k, v in t.items() if k != "format"}}
                self.loaded[tally] = t
                self.reused.append(sha[:12])
                return tally, self.loaded[tally]
        self.check_writable()
        import time
        t0 = time.time()
        spec_path = f"{stem}.spec.json"
        with open(spec_path, "w") as f:
            f.write(text + "\n")
        tmp = f"{tally}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            subprocess.run([str(cpp_bin()), "tally", "--schema", self.schema, "--spec", spec_path, "--records", self.records],
                           stdout=f, check=True)
        os.replace(tmp, tally)
        with open(key, "w") as f:
            json.dump(want, f, indent=1, sort_keys=True)
            f.write("\n")
        self.built.append((sha[:12], time.time() - t0))
        try:
            serves(want["spec"])
            if want["spec"] not in self.seeds:
                self.seeds.append(want["spec"])     # a uniform entry built now serves the specs that follow
        except ValueError:
            pass
        if not self.quiet:
            print(f"tally_library: built setting-{sha[:12]} in {time.time() - t0:.1f} s", file=sys.stderr)
        with open(tally) as f:
            self.loaded[tally] = json.load(f)
        return tally, self.loaded[tally]

    def check_writable(self):
        """refuse to write the library when its real path is outside the run's output root (a linked settings dir)"""
        real, root = os.path.realpath(self.path), os.path.realpath(self.root)
        if real != root and not real.startswith(root + os.sep):
            refuse(f"the library {self.path} is {real}, outside this run's output root {root}: an entry there would be built "
                   f"in place in another tree; give the run its own library dir (a copy), or build it in that tree")

    def setting(self, side, blk, shared):
        """(path, tally) serving (side, blk) under shared: the first seed that does, else the one-sided setting spec"""
        for s in self.seeds:
            if shared_of(s) == shared and (side, blk) in serves(s):
                return self.entry(s)
        return self.entry(setting_spec(side, blk, shared))


def stream_moved(have_key, want_key):
    """what differs between two record-stream keys; [] when they are the same stream. By the bytes when both keys name
    them (content_sha256: an edit to the dump's code that writes the same bytes is the same stream), else by what the
    records depend on (records_key.moved), not by the binary that dumped them"""
    from kvevaluator.measure import records_key
    a, b = have_key.get(records_key.CONTENT), want_key.get(records_key.CONTENT)
    if a and b:
        return [] if a == b else [f"content: the stream's bytes {a[:8]} -> {b[:8]}"]
    return records_key.moved(have_key, want_key)


def sources_sha256(key):
    """a record stream's freshness key without the commit that dumped it (records_key.sources_sha256)"""
    from kvevaluator.measure import records_key
    return records_key.sources_sha256(key)


same_records = A.same_records          # the rule lives in assemble_tally (its differences() reads it too)


def tally_for_spec(spec, lib):
    """the tally of `spec`, each part's rows from the setting tally that stores that part the same way"""
    shared = shared_of(spec)
    used = {}                                  # setting tally path -> tally

    def of(side, blk):
        p, t = lib.setting(side, blk, shared)
        used[p] = t
        return t

    base = of(PROPS, default_block(spec, PROPS))
    of(ADJ, default_block(spec, ADJ))
    labels = sorted({g["type"] for g in base["groups"] if g["part"] == A.PROPS_PART})
    pairs = sorted({(g["type"], g["direction"]) for g in base["groups"] if g["part"] == A.ADJ_PART})
    props_t = {lab: of(PROPS, props_of(spec, lab)) for lab in labels}
    adj_t = {(e, d): of(ADJ, adj_of(spec, e, d)) for e, d in pairs}

    for p, t in used.items():
        if t.get("format") != "meta":
            refuse(f"{p}: format {t.get('format')!r}, not meta")
        if not same_records(t["records"], base["records"]):
            refuse(f"settings from other record streams ({t['records']} vs {base['records']})")
    index = None
    for p, t in sorted(used.items()):
        rows = {tuple(g[f] for f in A.GROUP_FIELDS): g for g in t["groups"] if g["part"] == A.INDEX_PART}
        if index is not None and rows != index:
            refuse(f"the index rows differ between settings ({p})")
        index = rows
        for g in t["groups"]:
            if g["part"] not in (A.PROPS_PART, A.ADJ_PART, A.INDEX_PART):
                refuse(f"{p}: a group of part {g['part']!r}")

    def fam_tally(fam):
        """the setting tally a chunk family's rows come from: props:<label>, or <etype>:<dir>"""
        if A.is_props_family(fam):
            lab = fam[len("props:"):]
            if lab not in props_t:
                refuse(f"chunk family {fam}: no props group row of label {lab}")
            return props_t[lab]
        e, _, d = fam.rpartition(":")
        if d not in DIRS or (e, d) not in adj_t:
            refuse(f"chunk family {fam}: not <etype>:<out|in> of an adjacency group row")
        return adj_t[(e, d)]

    groups = list(index.values())
    for lab, t in props_t.items():
        groups += [g for g in t["groups"] if g["part"] == A.PROPS_PART and g["type"] == lab]
    for (e, d), t in adj_t.items():
        groups += [g for g in t["groups"] if g["part"] == A.ADJ_PART and g["type"] == e and g["direction"] == d]
    groups.sort(key=lambda g: tuple(g[f] for f in A.GROUP_FIELDS))
    if len({("payload_bytes" in g) for g in groups}) > 1:
        refuse("some settings carry payload bytes and others do not")
    # a chunk family's rows, and a locality row over a family, come from the setting tally that family's part is from
    chunks = {f: c for t in used.values() for f, c in t["chunks"].items() if fam_tally(f) is t}
    chunks = dict(sorted(chunks.items()))
    keep = [r for t in used.values() for r in t["locality"] if fam_tally(r["family"]) is t]
    keep.sort(key=lambda r: (r["etype"], r["direction"], r["owner_label"], r["target_label"],
                             not A.is_props_family(r["family"]), r["family"]))
    totals = {k: sum(g[k] for g in groups) for k in ("keys", "key_bytes", "value_bytes")}
    provenance = dict(base["provenance"])
    provenance["assembled_from"] = {
        "tool": "kvevaluator.predict.tally_library.tally_for_spec",
        "settings": [{"path": recorded_path(os.path.abspath(p), stats_root()), "sha256": sha256_bytes(open(p, "rb").read())}
                     for p in sorted(used)]}
    # the binary's key order, and the spec as it echoes a spec file written with sorted keys (so a direct tally and
    # this one compare byte for byte); a copy: the rows are the library's cached ones, and a caller may edit what it gets
    out = {"format": "meta"}
    if lib.version is not None:   # the library's: every entry it serves is of that version (reusable()), as a direct tally says
        out["tally_version"] = lib.version
    out.update({"spec": json.loads(json.dumps(spec, sort_keys=True)), "records": base["records"], "groups": groups,
                "chunks": chunks, "locality": keep, "totals": totals, "wall_s": 0.0, "provenance": provenance})
    return copy.deepcopy(out)


def library_tallies(dataset, scale, spec_files, records, schema, jobs, fresh=False, reuse_stale=False, root=None):
    """(the dataset's tally library, {cell: tally}) of the layouts in spec_files ({cell: spec file}): each layout's
    tally assembled from the library (library_dir), whose missing entries are built first, the uniform layouts among
    them `jobs` at once (one pass over the record stream each). The one place a layout's library entries are chosen:
    kvcost predict, and the gates' preflight for the layouts a gate predicts from an output tree of its own
    (tests/gates/gate_preflight.py). `root`: the output root a build is allowed in (the library's own tree unless
    given: Library.check_writable)"""
    from concurrent.futures import ThreadPoolExecutor
    spec_of = {}
    for c, path in spec_files.items():
        with open(path) as f:
            spec_of[c] = json.load(f)
    seeds = [s for _, s in sorted(spec_of.items()) if not any(s.get(k) for k in PER_TYPE)
             and default_block(s, PROPS)["owners"] == default_block(s, ADJ)["owners"]
             and default_block(s, PROPS)["group"] == default_block(s, ADJ)["group"]]   # the diagonals
    lib = Library(library_dir(dataset, scale), records, seeds=seeds, schema=schema, fresh=fresh, quiet=True,
                  reuse_stale=reuse_stale, root=root)
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        list(ex.map(lib.entry, seeds))             # one stream pass each
    return lib, {c: tally_for_spec(spec_of[c], lib) for c in spec_files}


def write_named(tally, spec_path, out_dir, dataset=None):
    """write a tally as <out_dir>/<store name>.json, the name predict --plans finds it by (an own-data dataset's store
    name when `dataset` is given; stores.meta_store_name); returns the path"""
    from kvevaluator.measure import stores
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{stores.meta_store_name(spec_path, dataset)}.json")
    with open(path, "w") as f:
        json.dump(tally, f, indent=1)
        f.write("\n")
    return path


def stamp(path):
    """adopt path's unversioned entries as tally version 1: each one whose clean binary has V1_CPP_COMMIT's cpp/ tree
    (git diff, so its counting code is version 1's) gets tally_version 1 and a `stamped` note in its key; the tally files
    are not touched. Others are listed and left. Returns (stamped, left)"""
    import time
    stamped, left = [], []
    for k in sorted(os.listdir(path)):
        if not (k.startswith("setting-") and k.endswith(".key.json")):
            continue
        kp = os.path.join(path, k)
        with open(kp) as f:
            have = json.load(f)
        if have.get("tally_version") is not None:
            continue
        b = have.get("binary", {})
        if b.get("dirty", True) or not b.get("git_sha"):
            left.append((k, "made by a dirty binary (or no binary named)"))
            continue
        r = subprocess.run(["git", "-C", str(ROOT), "diff", "--quiet", b["git_sha"], V1_CPP_COMMIT, "--", "cpp/"])
        if r.returncode != 0:
            left.append((k, f"binary {b['git_sha'][:10]}: " + ("its cpp/ differs from version 1's" if r.returncode == 1
                                                                else "commit unknown to this checkout")))
            continue
        have["tally_version"] = 1
        have["stamped"] = {"tally_version": 1, "rule": f"the binary's cpp/ tree equals {V1_CPP_COMMIT[:10]}'s (git diff)",
                           "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        with open(f"{kp}.tmp", "w") as f:
            json.dump(have, f, indent=1, sort_keys=True)
            f.write("\n")
        os.replace(f"{kp}.tmp", kp)
        stamped.append(k)
    print(f"tally_library --stamp {os.path.realpath(path)}: {len(stamped)} entries stamped version 1, {len(left)} left"
          + "".join(f"\n   left {k}: {why}" for k, why in left))
    return stamped, left


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--stamp", metavar="DIR", help="adopt DIR's unversioned entries as tally version 1, then exit")
    ap.add_argument("--spec")
    ap.add_argument("--records", help="the record stream (its freshness key beside it)")
    ap.add_argument("--library", help="the settings directory")
    ap.add_argument("--fresh", action="store_true", help="build entries made from another record stream again")
    ap.add_argument("--out-dir", help="write <out-dir>/<the spec's store name>.json instead of stdout")
    a = ap.parse_args(argv)
    if a.stamp:
        stamp(a.stamp)
        return
    if not (a.spec and a.records and a.library):
        ap.error("--spec, --records and --library are required (or --stamp DIR)")
    with open(a.spec) as f:
        spec = json.load(f)
    t = tally_for_spec(spec, Library(a.library, a.records, fresh=a.fresh))
    if a.out_dir:
        print(write_named(t, a.spec, a.out_dir))
    else:
        json.dump(t, sys.stdout, indent=1)
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
