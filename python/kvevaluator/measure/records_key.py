"""The freshness key of a dataset's record cache (measure/records.records_file): one rule for every dataset.

The cache is right only while everything the dump reads is the same. The key's parts:
  code    at the probe scale (LDBC's PROBE_SCALE), or for a dataset with no probe: the python and cpp files below
          ("by": "files"). At every other scale: the bytes the code dumps for the probe ("by": "probe"), i.e. the probe
          stream's content_sha256 and the dump of PROBE_TREE (a committed edge-case CSV tree). So an edit that does
          not change what the dump writes re-dumps the small probe only, and the larger streams stay fresh. Beside
          either: records_dump.DUMP_FORMAT_VERSION, bumped by hand for a change neither probe can show.
  python  the dump's modules and the dataset's schema.json, by content (sha256) only
  schema  the compiled canonical schema, not the .schema text (a comment edit keeps the key)
  cpp     LDBC only: the source files of the C++ CSV reader, read at the binary's own commit
  raw     what identifies the data (LDBC: each CSV's size and mtime; LinkBench: the JSONL sizes; IMDb: its manifest's
          sha256); none when the data is not here
The key is written beside the cache (<cache>.key.json); the dump's wall_s goes to <cache>.dump.json (not in the key: the
key file's bytes are an id its readers record). A cache whose key moved, or that has none, is refused with the
reason (a key too narrow serves stale data silently). An older key form still fresh by its files is rewritten in this
form on first use. KVEVAL_RECORDS_FRESH=1 moves the old cache aside and dumps it again.

content_sha256 names the stream's bytes (an output: moved() never compares it); what is made from the stream is keyed
on it. --adopt keys a cache checked by hand; --adopt-by-probe keys a stale cache whose dumping code is shown (by its old
key, a --witness probe stream, or an --old-tree checkout) to write today's probe bytes, with no re-dump.

    python -m kvevaluator.measure.records_key [--dataset D] --sf 30 [--variant V] [--show | --check | --adopt]
    python -m kvevaluator.measure.records_key --sf 30 --adopt-by-probe [--witness <probe stream> ...] [--old-tree D]
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from kvevaluator.datasets import schema_path
from kvevaluator.paths import ROOT

_PY = str(ROOT / "python")                                              # the packages' root: the modules hashed live here
_EXP = str(ROOT)
KEY_VERSION = 5
FRESH_ENV = "KVEVAL_RECORDS_FRESH"
KEYS_BEFORE_DATASET = "ldbc"   # the dataset whose keys (and this command's --sf form) predate the `dataset` field
_INCLUDE = re.compile(r'^\s*#\s*include\s+"([^"]+)"', re.M)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


_SCHEMA_JSON = os.path.relpath(schema_path("ldbc"), str(ROOT))   # the compiled schema the
# C++ reader parses (in the key's python part) and a tally encodes with
CONTENT = "content_sha256"           # the stream's bytes (records_file writes it at dump time)
SAME_CONTENT = "same_content_as"     # the ids of earlier dumps of the same bytes
KEYED_BY_CONTENT = "keyed_by_content"   # why a stream's key names only its content (no key file, a dirty binary): every use is marked
CODE_PARTS = ("code:", "python:", "cpp:")   # the moves of the dumping code (moved()'s words); the others: dataset, version, sf, schema, data


def is_probe(rec, sf, variant=None):
    """the dataset's probe stream (LDBC's sf0.1): every larger stream's key holds its bytes, so it is never used marked"""
    return getattr(rec, "PROBE_SCALE", None) == str(sf) and variant is None


def code_only(moves):
    """True when every move (moved()'s words) is of the dumping code: a stream that is used, marked, not refused"""
    return bool(moves) and all(m.startswith(CODE_PARTS) for m in moves)


def file_sha256(path):
    """sha256 of a file's bytes, read in 1 MB blocks"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def canonical(x):
    """the one JSON text of a key that its ids hash (sorted keys, no spaces)"""
    return json.dumps(x, sort_keys=True, separators=(",", ":"))


def cpp_sources(part):
    """a key's cpp part without the commit that built the binary: the reader's sources (and binary_dirty), the same
    stream whichever commit of those sources dumped it (util/record_stream.cc's sources_sha256 rule)"""
    return {k: v for k, v in part.items() if k != "binary_git_sha"} if isinstance(part, dict) else part


def sources_sha256(key):
    """a stream's freshness key without the commit that dumped it (cpp_sources; binary_dirty is kept)"""
    return _sha(canonical({**key, **({"cpp": cpp_sources(key["cpp"])} if isinstance(key.get("cpp"), dict) else {})})
                .encode())




def stream_ids(key_file):
    """the schema.json the stream was dumped beside (a tally made then encoded with it), and the three ids a reader of a stream recorded before content_sha256: the key file's bytes (key_sha256, what
    graph-stats and the tally write), the key's canonical text (a tally library entry's records_key_sha256) and its
    sources (sources_sha256)"""
    with open(key_file, "rb") as f:
        raw = f.read()
    key = json.loads(raw)
    schema = [h for h, paths in key.get("python", {}).items() if any(p.endswith(_SCHEMA_JSON) for p in paths)]
    return {"key_sha256": _sha(raw), "records_key_sha256": _sha(canonical(key).encode()),
            "sources_sha256": sources_sha256(key), "schema_json_sha256": schema[0] if schema else None}


def same_content_ids(aside, content):
    """same_content_as for a new dump of `content` that replaces the cache set aside at `aside`: the old stream's ids
    (and the ones it carried) when its bytes are the same, else []. The old bytes are hashed when its key does not
    name them"""
    kp = key_path(aside)
    if not (os.path.exists(aside) and os.path.exists(kp)):
        return []
    with open(kp) as f:
        old = json.load(f)
    old_content = old.get(CONTENT) or file_sha256(aside)
    if old_content != content:
        return []
    return [stream_ids(kp)] + list(old.get(SAME_CONTENT, []))


def python_part(dataset):
    """{sha256: [relative paths]} of the modules the dump imports, plus the dataset's schema.json (the compiled schema
    the reader parses): keyed by content, the paths only name it"""
    from kvevaluator import datasets
    ds = datasets.get(dataset)
    code = ("import sys, os, json\n"
            "import kvevaluator.measure.records_dump\n"
            + ds.records().DUMP_IMPORTS +
            "base = os.path.abspath('.')\n"
            "print(json.dumps(sorted(os.path.abspath(m.__file__) for m in list(sys.modules.values())\n"
            "      if getattr(m, '__file__', None) and os.path.abspath(m.__file__).startswith(base + os.sep))))\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=_PY, capture_output=True, text=True, check=True)
    files = json.loads(r.stdout.strip().splitlines()[-1])
    files.append(os.path.join(str(ROOT), ds.schema))
    out = {}
    for f in sorted(set(files)):
        out.setdefault(_sha(open(f, "rb").read()), []).append(os.path.relpath(f, _EXP))
    return dict(sorted(out.items()))


def schema_part(dataset, path=None):
    """sha256 of the dataset's compiled schema the dump reads (the canonical design, or, LDBC, the .schema file at
    `path`)"""
    from kvevaluator.schema.schema_json import to_json
    if path is None:
        from kvevaluator import datasets
        schema = datasets.schema(dataset)
    else:
        from kvevaluator.datasets.ldbc.schema import _schema_path
        from kvevaluator.schema.schemadef import compile_schema
        from kvevaluator.schema.schema_txt import parse_schema
        schema = compile_schema(*parse_schema(path, resolve=_schema_path), variant="canonical")
    return _sha(json.dumps(to_json(schema), sort_keys=True).encode())


def _git(*args):
    return subprocess.run(["git", "-C", _EXP, *args], capture_output=True, text=True)


def _blob_id(data):
    """the id git gives a file's bytes (a blob's sha1), with no repo"""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def cpp_part(binary):
    """the C++ reader's source files at the binary's commit: {path: git blob id}, with the binary's sha. In a tree with
    no .git (a release archive's binary, or an "unknown" one built in a copy: binary.binary_state) the files are read
    from the tree, with the ids git would give them, and the binary's own bytes are keyed too: nothing ties the tree's
    files to the binary there, so a rebuild after an edit must not reuse a stream the old binary dumped."""
    if binary is None:
        return {"parser": "python (frozen; no binary)"}
    from kvevaluator.capture.binary import binary_state
    v = json.loads(subprocess.run([binary, "--version"], capture_output=True, text=True, check=True).stdout)
    sha, dirty = v.get("binary_git_sha"), v.get("binary_dirty")
    src = "cpp/kvevaluator/src"
    state = binary_state(v)
    in_tree = state in ("release", "unknown")

    def read(f):   # (text, blob id) of src/f at the binary's commit, or None
        if in_tree:
            path = os.path.join(_EXP, src, f)
            if not os.path.isfile(path):
                return None
            data = open(path, "rb").read()
            return data.decode(), _blob_id(data)
        r = _git("show", f"{sha}:./{src}/{f}")
        return None if r.returncode != 0 else (r.stdout, _git("rev-parse", f"{sha}:./{src}/{f}").stdout.strip())

    seen, todo = {}, ["cli/records.cc"]
    while todo:
        f = os.path.normpath(todo.pop())
        if f in seen:
            continue
        got = read(f)
        if got is None:
            continue
        text, seen[f] = got
        for h in _INCLUDE.findall(text):
            for c in (h, os.path.join(os.path.dirname(f), h)):
                c = os.path.normpath(c)
                if (os.path.isfile(os.path.join(_EXP, src, c)) if in_tree
                        else _git("cat-file", "-e", f"{sha}:./{src}/{c}").returncode == 0):
                    stem = os.path.splitext(c)[0]
                    todo += [c] + [stem + e for e in (".cc", ".cpp")]
                    break
    if "cli/records.cc" not in seen:
        raise SystemExit(f"[records_key] cannot read the C++ reader at the binary's commit {sha} (is it in this repo?)")
    part = {"binary_git_sha": sha, "binary_dirty": False if state == "release" else dirty, "files": dict(sorted(seen.items()))}
    if in_tree:
        part["binary_sha256"] = file_sha256(binary)
    return part


def code_part(dataset, sf, variant=None):
    """the key's code part (module docstring): by the probe's bytes (the probe scale's stream, and the probe tree's dump),
    or by the files when this is the probe scale, the dataset has none, or its data is not here. A stale probe stream
    is refused (records_file names the command that dumps it again, in seconds), which stops the caller"""
    from kvevaluator import datasets
    from kvevaluator.measure.records_dump import DUMP_FORMAT_VERSION
    rec = datasets.get(dataset).records()
    probe = getattr(rec, "PROBE_SCALE", None)
    files = {"dump_format": DUMP_FORMAT_VERSION, "by": "files"}
    if probe is None or (str(sf) == probe and variant is None):
        return files
    try:
        if rec.source(probe, None, None)[1] is None:
            return files
    except SystemExit:                       # no probe data here
        return files
    from kvevaluator.measure.records import records_file
    path = records_file(dataset, probe, fresh=False)
    with open(key_path(path)) as f:
        content = json.load(f).get(CONTENT) or file_sha256(path)
    return {"dump_format": DUMP_FORMAT_VERSION, "by": "probe", "probe_sf": probe, "probe_content_sha256": content,
            "probe_tree": rec.PROBE_TREE, "probe_tree_sha256": probe_tree_sha256(dataset)}


_TREE_SHA = {}   # (tree, code root, interpreter) -> the dump's sha256, once per process


def probe_tree_sha256(dataset, code_root=None, python=None):
    """sha256 of the record stream the dump writes for the dataset's probe tree (its records module's PROBE_TREE, in
    this repo): today's code, or the checkout at `code_root`, run by `python` (default: that checkout's own .venv, which
    imports its code) with its build's binary. The dump reads the tree as the probe scale's under a temporary data dir
    (the dataset's place_probe_tree), so the code takes no argument for it"""
    import tempfile
    from kvevaluator import datasets
    rec = datasets.get(dataset).records()
    tree = rec.PROBE_TREE
    root = str(ROOT) if code_root is None else os.path.abspath(code_root)
    if python is None:
        python = sys.executable if code_root is None else os.path.join(root, ".venv", "bin", "python")
    if (tree, root, python) in _TREE_SHA:
        return _TREE_SHA[(tree, root, python)]
    with tempfile.TemporaryDirectory() as data:
        rec.place_probe_tree(data)
        env = dict(os.environ, KVEVAL_DATA_DIR=data)
        if code_root is not None:            # the checkout's own build and binary, not this process's overrides
            env.pop("KVEVAL_BUILD_DIR", None)
            env.pop("KVEVAL_CPP_BIN", None)
        r = subprocess.run([python, "-m", "kvevaluator.measure.records_dump",
                            *rec.dump_argv(rec.PROBE_SCALE, None, None)], cwd=root, env=env, capture_output=True,
                           check=False)
    if r.returncode != 0:
        raise SystemExit(f"[records_key] the dump of the probe tree {tree} failed (code at {root}): "
                         f"{r.stderr.decode(errors='replace')[-600:]}")
    _TREE_SHA[(tree, root, python)] = _sha(r.stdout)
    return _TREE_SHA[(tree, root, python)]


def compute(dataset, sf, variant=None, data=None):
    """the key the cache of (dataset, scale `sf`, variant) has to have now; `data` an own-data dataset's data dir"""
    from kvevaluator import datasets
    rec = datasets.get(dataset).records()
    cpp = None
    if rec.USES_CPP_READER:
        from kvevaluator.datasets.ldbc.loader import _cpp_binary
        cpp = cpp_part(_cpp_binary())
    return {"version": KEY_VERSION, "dataset": dataset, "sf": str(sf), "variant": variant,
            "code": code_part(dataset, sf, variant),
            "python": python_part(dataset), "schema": schema_part(dataset), "cpp": cpp,
            "raw": rec.source(sf, data, variant)[1]}


def code_key(key):
    """the dumping code's parts of a key as one sha (kvevaluator.adoptions.key_sha): what a records_stream row of the
    reuse ledger is made under and current for; the parts moved() compares for the code, nothing else (a key by the
    probe: its code part alone). A dirty binary is never adopted (records._existing)"""
    from kvevaluator.adoptions import key_sha
    if (key.get("code") or {}).get("by") == "probe":     # moved() compares the probe's bytes only, not the files
        return key_sha({"code": key["code"]})
    cpp = key.get("cpp") or {}
    return key_sha({"code": key.get("code") or {}, "python": key.get("python") or {},
                    "cpp": {k: cpp.get(k) for k in ("files", "parser", "binary_sha256")}})


def ledger_scope(dataset, sf, variant=None):
    """a records_stream row's scope"""
    return {"dataset": dataset, "sf": str(sf), **({} if variant is None else {"variant": variant})}


def key_path(cache):
    return cache + ".key.json"


def dump_record_path(cache):
    """the dump's own record beside the cache: its wall_s, its content and when"""
    return cache + ".dump.json"


def write(cache, key):
    tmp = f"{key_path(cache)}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(key, f, indent=1, sort_keys=True)
    os.replace(tmp, key_path(cache))


def moved(have, want):
    """the parts of the key that differ, in words; [] when the cache is fresh"""
    out = []
    was, now = have.get("dataset", KEYS_BEFORE_DATASET), want.get("dataset", KEYS_BEFORE_DATASET)
    if was != now:
        out.append(f"dataset: {was!r} -> {now!r}")
    for part in ("version", "sf", "variant"):
        if have.get(part) != want.get(part):
            out.append(f"{part}: {have.get(part)!r} -> {want.get(part)!r}")
    if have.get("schema") != want.get("schema"):
        out.append("schema: the dataset's compiled schema changed")
    out += _code_moved(have.get("code") or {}, want.get("code") or {})
    a, b = have.get("raw") or {}, want.get("raw")
    if b is not None:      # None: the data is not here to compare (an own-data cache is then reused as it is)
        diff = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
        if diff:
            out.append(f"raw: {len(diff)} parts of the data changed (e.g. {', '.join(diff[:3])})")
    if (want.get("code") or {}).get("by") == "probe":
        return out + _dirty(want)
    return out + files_moved(have, want) + _dirty(want)


def _code_moved(ca, cb):
    """what differs between two code parts, in words: the format version, the keying, and, both by the probe, the
    bytes of each probe"""
    out = []
    if ca.get("dump_format") != cb.get("dump_format"):
        out.append(f"code: the dump format version {ca.get('dump_format')!r} -> {cb.get('dump_format')!r}")
    if ca.get("by") != cb.get("by"):
        out.append(f"code: keyed by {ca.get('by')!r}, now by {cb.get('by')!r}"
                   + (" (a stream whose dumping code writes today's probe bytes: records_key --adopt-by-probe)"
                      if cb.get("by") == "probe" else ""))
        return out
    if cb.get("by") != "probe":
        return out
    for field, what in (("probe_content_sha256", f"at sf{cb.get('probe_sf')} (the probe stream"),
                        ("probe_tree_sha256", f"for the probe tree {cb.get('probe_tree')} (")):
        if ca.get(field) != cb.get(field):
            out.append(f"code: the dump writes other bytes {what}{' ' if what.endswith('stream') else ''}"
                       f"{str(ca.get(field))[:8]} -> {str(cb.get(field))[:8]})")
    return out


def _dirty(want):
    from kvevaluator.capture.binary import may_run
    b = want.get("cpp") or {}
    if "binary_dirty" in b and not may_run(b):
        return ["cpp: the binary is dirty: its reader cannot be named by a commit, so its records are not reused"]
    return []


def files_moved(have, want):
    """the code's files that differ (the python and cpp parts), in words; [] when the same code dumped both"""
    out = []
    a, b = have.get("python", {}), want.get("python", {})
    diff = sorted({p for k in set(a) ^ set(b) for p in a.get(k, b.get(k, []))})
    if diff:
        out.append(f"python: {len(diff)} files changed in content (e.g. {', '.join(diff[:3])})")
    a, b = have.get("cpp") or {}, want.get("cpp") or {}
    if a.get("files") != b.get("files") or a.get("parser") != b.get("parser"):
        fa, fb = a.get("files", {}), b.get("files", {})
        diff = sorted(k for k in set(fa) | set(fb) if fa.get(k) != fb.get(k))
        out.append(f"cpp: the C++ reader's sources differ ({', '.join(diff[:3]) or a.get('parser') or b.get('parser')}; "
                   f"binary {a.get('binary_git_sha', '?')[:10]} -> {b.get('binary_git_sha', '?')[:10]})")
    if a.get("binary_sha256") != b.get("binary_sha256"):   # a tree with no .git keys the binary's own bytes (cpp_part)
        out.append("cpp: another binary (no .git here: the binary's bytes are its key)")
    return out


def as_files(key):
    """`key` with its code part by files at the current format version (how a version-3 key, which had no code part,
    keyed the code)"""
    from kvevaluator.measure.records_dump import DUMP_FORMAT_VERSION
    return {**key, "version": KEY_VERSION, "code": {"dump_format": DUMP_FORMAT_VERSION, "by": "files"}}


def upgraded(have, want):
    """`want` when `have` is a key of an earlier version (3 or 4) whose python and cpp parts name the code that dumped
    it (not adopted by probe) and that is fresh by those files (the same module texts and C++ reader, every other part
    equal), else None: today's code is then the dumping code, and the key is rewritten in this form (upgrade_key).
    Version 1 and 2 keys, and keys adopted by probe, are not upgraded (refused: --adopt-by-probe, or dump again)."""
    if have.get("version") not in (3, 4) or want.get("version") != KEY_VERSION or "adopted_by_probe" in have:
        return None
    return want if not moved(as_files(have), as_files(want)) else None


def upgrade_key(cache, want):
    """write `want` as the cache's key, keeping its content_sha256 (the bytes hashed when the old key names none) and
    adding the old key's ids to same_content_as, so what was made from the stream under the old key still matches it"""
    kp = key_path(cache)
    with open(kp) as f:
        have = json.load(f)
    content = have.get(CONTENT) or file_sha256(cache)
    write(cache, {**want, CONTENT: content, SAME_CONTENT: [stream_ids(kp)] + list(have.get(SAME_CONTENT, []))})


WITNESS_PARTS = ("python", "cpp", "schema")   # the dumping code's parts a witness or an old checkout must have (not
#                                               raw: a witness is another scale's data)


def same_code(code, witness):
    """{part: equal} for WITNESS_PARTS between the dumping code's parts and a probe-scale stream's key: python by its
    files' content (a move keeps it, as in moved()), cpp by the reader's sources (cpp_sources)"""
    def part(k, name):
        v = k.get(name)
        if name == "python":
            return sorted(v or {})
        return cpp_sources(v) if name == "cpp" else v
    return {name: part(code, name) == part(witness, name) for name in WITNESS_PARTS}


def dumped_by(have):
    """the python, cpp and schema parts of the code that dumped the stream: the key's own, or, for a key adopted by
    probe, adopted_by_probe.dumped_by (a key adopted before that field was kept: its witness's key, while it is there)"""
    a = have.get("adopted_by_probe")
    if not a:
        return {k: have.get(k) for k in WITNESS_PARTS}
    if a.get("dumped_by"):
        return a["dumped_by"]
    w = a.get("witness")
    kp = key_path(w if os.path.isabs(w) else os.path.join(_EXP, w)) if w else None
    if kp and os.path.exists(kp):
        with open(kp) as f:
            wk = json.load(f)
        print(f"[records_key] the dumping code: the parts of the witness {w} (the key was adopted before "
              f"adopted_by_probe.dumped_by was kept)")
        return {k: wk.get(k) for k in WITNESS_PARTS}
    raise SystemExit("[records_key] the key was adopted by probe and names no dumping code (no adopted_by_probe."
                     "dumped_by, and its witness's key is gone): dump it again")


def check_old_tree(tree, code):
    """{commit, binary_git_sha} when the checkout `tree` is the dumping code `code`: every python file it names has
    its content there, and the binary built there (build/cpp/kvevaluator) is clean, from the same reader sources;
    else SystemExit naming what differs"""
    tree = os.path.abspath(tree)
    bad = [p for h, paths in (code.get("python") or {}).items() for p in paths
           if not os.path.isfile(os.path.join(tree, p)) or file_sha256(os.path.join(tree, p)) != h]
    if bad:
        raise SystemExit(f"[records_key] the old tree {tree} is not the dumping code: {len(bad)} python files differ "
                         f"(e.g. {', '.join(bad[:3])})")
    binary = os.path.join(tree, "build", "cpp", "kvevaluator")
    if not os.path.exists(binary) or not os.path.exists(os.path.join(tree, ".venv", "bin", "python")):
        raise SystemExit(f"[records_key] no binary or venv in the old tree {tree}: run just setup and just build there")
    part = cpp_part(binary)
    from kvevaluator.capture.binary import may_run
    if not may_run(part) or cpp_sources(part) != cpp_sources(code.get("cpp")):
        raise SystemExit(f"[records_key] the old tree's binary {part.get('binary_git_sha', '?')[:10]} is not the "
                         f"dumping code's reader (dirty {part.get('binary_dirty')}, or other sources)")
    commit = subprocess.run(["git", "-C", tree, "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    return {"commit": commit.stdout.strip() or None, "binary_git_sha": part.get("binary_git_sha")}


def probe_adoption(have, want, witnesses, old_tree=None):
    """adopted_by_probe for a stale cache whose dumping code writes today's probe bytes, else SystemExit naming what
    was compared. The dumping code's bytes: at the probe scale from the old key's code part or a witness
    (`witnesses`: [(path, key)]); for the probe tree from the old key's code part or a run of `old_tree`"""
    code = want.get("code") or {}
    if code.get("by") != "probe":
        raise SystemExit(f"[records_key] this key is not keyed by the probe here (code: {code}): nothing to adopt by")
    rest = moved({**have, "version": want["version"], "code": code}, want)
    if rest:
        raise SystemExit("[records_key] the cache differs in more than its code: " + "; ".join(rest))
    dumping = dumped_by(have)
    if dumping.get("schema") != want.get("schema"):
        raise SystemExit("[records_key] the dumping code read another schema: dump it again")
    old = have.get("code") or {}
    out = {"dumped_by": dumping, **_scale_bytes(dumping, old, code, witnesses)}
    if out["probe_content_sha256"] != code["probe_content_sha256"]:
        raise SystemExit(f"[records_key] the dumping code wrote other bytes at sf{code['probe_sf']} "
                         f"({out['probe_content_from']}: {out['probe_content_sha256'][:8]}, today "
                         f"{code['probe_content_sha256'][:8]}): dump it again")
    out.update(_tree_bytes(dumping, old, code, want.get("dataset", KEYS_BEFORE_DATASET), old_tree))
    if out["probe_tree_sha256"] != code["probe_tree_sha256"]:
        raise SystemExit(f"[records_key] the dumping code writes other bytes for the probe tree ("
                         f"{out['probe_tree_sha256'][:8]}, today {code['probe_tree_sha256'][:8]}): dump it again")
    if have.get("adopted_by_probe"):
        out["earlier"] = {k: v for k, v in have["adopted_by_probe"].items() if k != "earlier"}
    return out


def _scale_bytes(dumping, old, code, witnesses):
    """{probe_content_sha256, probe_content_from}: the dumping code's bytes at the probe scale, from the old key's code
    part, else from the first witness with the dumping code's parts (each one tried is printed)"""
    if old.get("by") == "probe" and old.get("probe_content_sha256"):
        return {"probe_content_sha256": old["probe_content_sha256"], "probe_content_from": "the old key"}
    seen, found = [], None
    for path, wkey in witnesses:
        eq = same_code(dumping, wkey)
        seen.append(f"{path}: " + ", ".join(f"{k} {'equal' if v else 'differs'}" for k, v in eq.items()))
        if all(eq.values()):
            found = {"probe_content_sha256": wkey.get(CONTENT) or file_sha256(path),
                     "probe_content_from": os.path.relpath(path, _EXP) if path.startswith(_EXP) else path}
            break
    for line in seen:
        print("[records_key] witness tried: " + line)
    if found is None:
        raise SystemExit("[records_key] no witness has the dumping code (" + ("; ".join(seen) or "none found")
                         + f"). Make one: dump sf{code['probe_sf']} at the dumping commit "
                         f"{str((dumping.get('cpp') or {}).get('binary_git_sha'))[:10]} into another output dir "
                         "(a worktree, its own build) and pass its stream with --witness")
    return found


def _tree_bytes(dumping, old, code, dataset, old_tree):
    """{probe_tree_sha256, probe_tree_from[, old_tree]}: the dumping code's bytes for the probe tree, from the old key's
    code part, else from a run of the checkout `old_tree` once it is shown to be the dumping code"""
    if old.get("by") == "probe" and old.get("probe_tree_sha256") and old.get("probe_tree") == code["probe_tree"]:
        return {"probe_tree_sha256": old["probe_tree_sha256"], "probe_tree_from": "the old key"}
    if not old_tree:
        raise SystemExit(f"[records_key] the dumping code's bytes for the probe tree {code['probe_tree']} are not "
                         f"known: pass --old-tree <a checkout at the dumping commit "
                         f"{str((dumping.get('cpp') or {}).get('binary_git_sha'))[:10]}, with just setup and just build>")
    tree = check_old_tree(old_tree, dumping)
    sha = probe_tree_sha256(dataset, old_tree)
    print(f"[records_key] the old tree {old_tree} is the dumping code (commit {str(tree['commit'])[:10]}, binary "
          f"{str(tree['binary_git_sha'])[:10]}): its dump of the probe tree {sha[:8]}")
    return {"probe_tree_sha256": sha, "probe_tree_from": "the old tree", "old_tree": tree}


def witnesses_beside(dataset, probe, given=()):
    """[(path, key)] of the probe-scale streams to try: `given`, else the probe cache and its set-aside copies"""
    import glob
    from kvevaluator.measure.records import records_cache_path
    paths = list(given) or sorted(p[:-len(".key.json")] for p in glob.glob(records_cache_path(dataset, probe) + "*.key.json"))
    out = []
    for p in paths:
        if os.path.exists(p) and os.path.exists(key_path(p)):
            with open(key_path(p)) as f:
                out.append((p, json.load(f)))
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", default=KEYS_BEFORE_DATASET)
    ap.add_argument("--sf", required=True, help="the scale (LDBC's sf, or a data tag)")
    ap.add_argument("--variant", default=None)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--show", action="store_true", help="print the key computed now (above the probe scale: it reads the "
                   "probe stream, and dumps it when there is none; a stale one is refused)")
    g.add_argument("--check", action="store_true", help="the cache's key against now: exit 1 when it moved or is missing "
                   "(above the probe scale, as --show)")
    g.add_argument("--adopt", action="store_true", help="write the key computed now for the existing cache (checked by hand)")
    g.add_argument("--adopt-by-probe", action="store_true",
                   help="write the key computed now for a stale cache whose code a witness shows writes today's probe bytes")
    ap.add_argument("--witness", action="append", default=[],
                    help="with --adopt-by-probe: a probe-scale stream (its .key.json beside it) to try; default: the probe "
                         "cache and its set-aside copies")
    ap.add_argument("--old-tree", help="with --adopt-by-probe: a checkout of the dumping code, built (just build): its "
                                       "dump of the probe tree, when the old key does not name it")
    a = ap.parse_args(argv)
    from kvevaluator.measure.records import records_cache_path
    cache = records_cache_path(a.dataset, a.sf, a.variant)
    want = compute(a.dataset, a.sf, a.variant)
    if a.show:
        print(json.dumps(want, indent=1, sort_keys=True))
        return 0
    if a.adopt:
        if not os.path.exists(cache):
            raise SystemExit(f"[records_key] no cache at {cache}")
        from kvevaluator import datasets, stale
        from kvevaluator.capture.binary import may_run
        key = {**want, CONTENT: file_sha256(cache)}
        if "binary_dirty" in (want["cpp"] or {}) and not may_run(want["cpp"]):
            if is_probe(datasets.get(a.dataset).records(), a.sf, a.variant):
                raise SystemExit("[records_key] the binary is dirty: no key is written for the probe stream (every "
                                 "larger stream's key holds its bytes)")
            key[KEYED_BY_CONTENT] = "adopted under a dirty binary: its reader is not named by a commit"
            stale.note("stream_keyed_by_content", f"{cache}: {key[KEYED_BY_CONTENT]}")
        write(cache, key)
        print(f"[records_key] key written for {cache}: python {len(want['python'])} files, C++ reader "
              f"{len((want['cpp'] or {}).get('files', {}))} files at {(want['cpp'] or {}).get('binary_git_sha', '-')[:10]}, raw {len(want['raw'] or {})} parts")
        return 0
    if not os.path.exists(key_path(cache)):
        print(f"[records_key] {cache}: NO KEY")
        return 1
    have = json.load(open(key_path(cache)))
    if a.adopt_by_probe:
        code = want.get("code") or {}
        found = witnesses_beside(a.dataset, code.get("probe_sf"), a.witness) if code.get("by") == "probe" else []
        evidence = probe_adoption(have, want, found, a.old_tree)
        print(f"[records_key] hashing {cache} ..." if not have.get(CONTENT) else f"[records_key] {cache}: content "
              f"{have[CONTENT][:8]} (from its key)", flush=True)
        upgrade_key(cache, {**want, "adopted_by_probe": evidence})
        with open(key_path(cache)) as f:
            content = json.load(f)[CONTENT]
        print(f"[records_key] key written for {cache}: by the sf{code['probe_sf']} probe "
              f"{code['probe_content_sha256'][:8]} and the probe tree {code['probe_tree_sha256'][:8]}; the stream's "
              f"content {content[:8]}; its old ids in {SAME_CONTENT}; the dumping code under adopted_by_probe.dumped_by")
        from kvevaluator import adoptions
        made, now = code_key(have), code_key(want)
        if made != now and not adoptions.covering("records_stream", content, made, now, ledger_scope(a.dataset, a.sf, a.variant)):
            r = adoptions.add("records_stream", content, made, now,
                              {"proof": "records_key --adopt-by-probe", "probe_sf": code["probe_sf"]}, "agent",
                              ledger_scope(a.dataset, a.sf, a.variant),
                              evidence={k: evidence.get(k) for k in ("probe_content_sha256", "probe_content_from",
                                                                     "probe_tree_sha256") if evidence.get(k)})
            print(f"[records_key] the ledger row {r['id']} (inputs/adoptions.json: commit it with the stream's use)")
        return 0
    if upgraded(have, want):
        print(f"[records_key] {cache}: FRESH (a key of version {have.get('version')}, fresh by its files; its next use rewrites it in this form)")
        return 0
    m = moved(have, want)
    print(f"[records_key] {cache}: " + ("FRESH" if not m else "MOVED: " + "; ".join(m)))
    return 1 if m else 0


if __name__ == "__main__":
    sys.exit(main())
