"""The record stream of a dataset: the one-time dump every store of the dataset is ingested from, cached per scale
(and per LDBC data variant), reused under one freshness key (records_key.py) for every dataset.
"""
import json
import os
import subprocess

from kvevaluator.paths import ROOT

_EXP = str(ROOT)


def records_cache_path(dataset, scale, variant=None):
    """where the cache lives: <output>/stats/<dataset>/<scale>/records[-<variant>].jsonl (output_tree.stats_dir), also
    when KVEVAL_STORE_DIR names another store root (a second store set shares the stream)"""
    from kvevaluator.output_tree import stats_dir
    return os.path.join(str(stats_dir(dataset, scale)), "records.jsonl" if variant is None else f"records-{variant}.jsonl")


def fresh_records_command(dataset, scale, variant=None):
    """the one line that dumps a record cache again (ensure_stores.py --fresh-records skips it when every store exists)"""
    from kvevaluator.measure import records_key
    arg = f"{dataset!r}, {scale!r}" + ("" if variant is None else f", {variant!r}")
    return (f"{records_key.FRESH_ENV}=1 uv run --locked python -c "
            f"\"from kvevaluator.measure import records; records.records_file({arg})\"")


def _adopt(dataset, path, want):
    """a cache made before the freshness key (its .src.json: the stream's sha256, its data, its format version) gets its
    key when it is what the key asks for now: the same format version and the same data. The cache is not dumped again
    (content_sha256 is the .src.json's sha256, the same hash of the same bytes). False when there is nothing to adopt"""
    import sys
    from kvevaluator import datasets
    from kvevaluator.measure import records_key
    src = path + ".src.json"
    rec = datasets.get(dataset).records()
    if not os.path.exists(src) or not hasattr(rec, "RECORDS_VERSION_BEFORE_KEY"):
        return False
    with open(src) as f:
        was = json.load(f)
    if was.get("records_version", 1) != rec.RECORDS_VERSION_BEFORE_KEY:
        raise SystemExit(f"[records] {dataset}: the cached records {path} are format version "
                         f"{was.get('records_version', 1)}, this code writes {rec.RECORDS_VERSION_BEFORE_KEY} -- remove the "
                         f"scale's store dir {os.path.dirname(path)} (records + stores) and re-run")
    if want["raw"] is not None and any(was.get(k) != v for k, v in want["raw"].items()):
        raise SystemExit(f"[records] {dataset}: the cached records {path} came from other data "
                         f"({ {k: was.get(k) for k in want['raw']} } vs now {want['raw']}) -- remove the scale's store "
                         f"dir {os.path.dirname(path)} (records + stores) and re-run")
    records_key.write(path, {**want, records_key.CONTENT: was["sha256"]})
    print(f"[records] {dataset}: the record cache {path} made before its freshness key is adopted (its .src.json: the "
          f"same format, {'the same data' if want['raw'] is not None else 'its data not here to compare'})", file=sys.stderr)
    return True


def _existing(dataset, scale, variant, path, rec, want, probe):
    """an existing cache, checked against `want`: used (marked when only its dumping code moved, or when keyed by
    its content), or refused (a moved dataset, scale, schema, key version or data; the probe stream on any move)"""
    import sys
    from kvevaluator.measure import records_key
    kp = records_key.key_path(path)
    if not os.path.exists(kp) and not _adopt(dataset, path, want):
        how = (f"dump it again ({fresh_records_command(dataset, scale, variant)}), or check it and adopt it: python3 "
               f"-m kvevaluator.measure.records_key --dataset {dataset} --sf {scale} --adopt")
        if probe:
            raise SystemExit(f"[records] the {dataset} {scale} record cache {path} has no freshness key (made before "
                             f"keys, or by hand): {how}")
        # keyed by its content: what is made from it is keyed on those bytes; every use is marked
        records_key.write(path, {**want, records_key.CONTENT: records_key.file_sha256(path),
                                 records_key.KEYED_BY_CONTENT: "no freshness key (made before keys, or by hand)"})
        print(f"[records] the {dataset} {scale} record cache {path} had no freshness key: keyed by its content, "
              f"used, marked ({how})", file=sys.stderr)
    with open(kp) as f:
        have = json.load(f)
    if records_key.upgraded(have, want):
        records_key.upgrade_key(path, want)
        print(f"[records] the sf{scale} record cache's key rewritten in its current form (it was fresh under the "
              f"earlier key, by its files; the old ids kept in {records_key.SAME_CONTENT})", file=sys.stderr)
        have = want
    moved = records_key.moved(have, want)
    if moved and (probe or not records_key.code_only(moved)):
        raise SystemExit(_stale(dataset, scale, variant, path, rec, want, moved))
    from kvevaluator import adoptions, stale
    if moved:   # only the dumping code moved: the stream is used as it is, adopted by a ledger row, else marked
        by_probe = (want.get("code") or {}).get("by") == "probe"
        detail = (f"{path}: the dumping code moved ({'; '.join(moved)}); used as it is. "
                  f"To clear it: {fresh_records_command(dataset, scale, variant)}"
                  + (f", or, when its dumping code writes today's probe bytes, python -m kvevaluator.measure.records_key "
                     f"--dataset {dataset} --sf {scale}" + ("" if variant is None else f" --variant {variant}")
                     + " --adopt-by-probe" if by_probe else ""))
        if have.get(records_key.CONTENT) and not records_key._dirty(want):   # a dirty binary's reader: never adopted
            adoptions.adopted_or_stale("records_stream", have[records_key.CONTENT], records_key.code_key(have),
                                       records_key.code_key(want), "stream_keyed_by_content", detail,
                                       records_key.ledger_scope(dataset, scale, variant))
        else:
            stale.note("stream_keyed_by_content", detail)
    if have.get(records_key.KEYED_BY_CONTENT):
        stale.note("stream_keyed_by_content", f"{path}: {have[records_key.KEYED_BY_CONTENT]}")


def records_file(dataset, scale, variant=None, data=None, fresh=None):
    """One cached record stream per (dataset, scale, LDBC data variant), gitignored beside the C++ stores, dumped on
    first use by the dataset's records module (datasets/<name>/records.py: its data, its dump arguments). `variant`
    names a data VARIANT of LDBC's CSVs (loader._root; e.g. fat128fixed) cached apart; `data` an own-data dataset's
    data dir (else its default). The cache is reused only under its freshness key (records_key.py: the dump's code, the
    schema, LDBC's C++ reader or the probe stream's bytes, the data); what is refused or marked: below. And
    KVEVAL_RECORDS_FRESH=1 (or fresh=True) moves the old cache aside and dumps it again. fresh=False ignores the
    variable: the probe stream a larger scale's key reads (records_key.code_part) is not set aside by the larger
    stream's re-dump; a stale probe is refused (its re-dump takes seconds). Any other stream whose dumping code alone
    moved, that has no key, or that a dirty binary dumps is used and marked stream_keyed_by_content (kvevaluator.stale;
    the last two keep the reason in the key, keyed_by_content, so every later use is marked); a moved dataset, scale,
    schema, key version or data is refused."""
    import sys
    import time
    from kvevaluator import datasets
    from kvevaluator.measure import records_key
    rec = datasets.get(dataset).records()
    path = records_cache_path(dataset, scale, variant)
    want = records_key.compute(dataset, scale, variant, data)   # first: a missing dataset stops before any dir is made
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data_dir = rec.source(scale, data, variant)[0]          # where a dump reads from
    aside = None
    if fresh is None:
        fresh = bool(os.environ.get(records_key.FRESH_ENV))
    if os.path.exists(path) and fresh:
        aside = f"{path}.aside-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"
        os.replace(path, aside)
        if os.path.exists(records_key.key_path(path)):
            os.replace(records_key.key_path(path), records_key.key_path(aside))
        print(f"[records] {records_key.FRESH_ENV}: the old record cache moved aside to {aside} (not removed)",
              file=sys.stderr)
    probe = records_key.is_probe(rec, scale, variant)    # never used marked: every larger stream's key holds its bytes
    if os.path.exists(path):
        _existing(dataset, scale, variant, path, rec, want, probe)
        return path
    rec.check_dumpable(scale, data_dir, variant)
    from kvevaluator.capture.binary import may_run
    unclean = "binary_dirty" in (want["cpp"] or {}) and not may_run(want["cpp"])
    if unclean and probe:
        raise SystemExit("[records] the binary is dirty: the probe stream (every larger stream's key holds its bytes) "
                         "is dumped with a clean binary only")

    def dump(tmp):
        import hashlib
        print(f"[records] dumping the {dataset} {scale} record stream{'' if variant is None else ' (' + variant + ')'} "
              f"(one-time cache)", file=sys.stderr)
        t0 = time.time()
        h = hashlib.sha256()                   # the bytes written, hashed as they pass: the stream's content_sha256
        cmd = [sys.executable, "-m", "kvevaluator.measure.records_dump"] + rec.dump_argv(scale, data_dir, variant)
        with open(tmp, "wb") as f, subprocess.Popen(cmd, stdout=subprocess.PIPE, cwd=_EXP) as p:
            for b in iter(lambda: p.stdout.read(1 << 20), b""):
                h.update(b)
                f.write(b)
        if p.returncode != 0:
            raise subprocess.CalledProcessError(p.returncode, cmd)
        key = {**want, records_key.CONTENT: h.hexdigest()}
        if unclean:   # its reader is not named by a commit: keyed by its bytes, every use marked
            key[records_key.KEYED_BY_CONTENT] = "dumped by a dirty binary: its reader is not named by a commit"
            from kvevaluator import stale
            stale.note("stream_keyed_by_content", f"{path}: {key[records_key.KEYED_BY_CONTENT]}")
        same = records_key.same_content_ids(aside, key[records_key.CONTENT]) if aside else []
        if same:
            key[records_key.SAME_CONTENT] = same
            print(f"[records] the new {dataset} {scale} record stream has the same bytes as the one set aside: what "
                  f"was made from that one is kept", file=sys.stderr)
        elif aside and os.path.exists(records_key.key_path(aside)):
            with open(records_key.key_path(aside)) as f:
                why = records_key.files_moved(json.load(f), want) or ["no file of the dump's code changed"]
            print(f"[records] the new {dataset} {scale} record stream's bytes differ from the one set aside ("
                  + "; ".join(why) + ")" + (": every larger stream keyed by it is now stale"
                                            if getattr(rec, "PROBE_SCALE", None) == str(scale) else ""), file=sys.stderr)
        records_key.write(path, key)           # under the lock, before the cache appears: a reader sees both
        # the dump's time beside it, not in the key (the key file's bytes are an id its readers record): the run-time
        # table's "read the dataset"
        with open(records_key.dump_record_path(path), "w") as f:
            json.dump({"wall_s": round(time.time() - t0, 3), "content_sha256": key[records_key.CONTENT],
                       "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f, indent=1, sort_keys=True)
            f.write("\n")
    _build_once(path, dump)
    return path


def _stale(dataset, scale, variant, path, rec, want, moved):
    """the refusal of a stale cache: what moved, and the commands that make it fresh"""
    probe = getattr(rec, "PROBE_SCALE", None) == str(scale) and variant is None
    adopt = (want.get("code") or {}).get("by") == "probe" and all(m.startswith(("code:", "version:")) for m in moved)
    return (f"[records] the {dataset} {scale} record cache {path} is STALE: " + "; ".join(moved)
            + (" (the probe stream the larger scales' keys read; its dump takes seconds)" if probe else "")
            + f" -- dump it again: {fresh_records_command(dataset, scale, variant)}"
            + (f"; or, with no re-dump when its dumping code writes today's probe bytes: python -m "
               f"kvevaluator.measure.records_key --dataset {dataset} --sf {scale}"
               + ("" if variant is None else f" --variant {variant}") + " --adopt-by-probe" if adopt else ""))


def _build_once(path, build):
    """Make the cache file `path` once, also when parallel processes all ask for it first (the sf3 pass's units do):
    the first takes a lock on <path>.lock, calls build(tmp) into its own tmp file and renames that into place; the
    others wait on the lock, then find `path` made. A reader that does not take the lock sees no file or a whole one.
    A build that fails removes its tmp file and raises."""
    import fcntl
    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)          # released when the file closes, also when the process dies
        if os.path.exists(path):
            return
        tmp = f"{path}.{os.getpid()}.tmp"
        try:
            build(tmp)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
