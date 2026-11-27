"""Arenas for runs that write: a link copy of a base store (hard links to its SSTs, copies of its small files), made,
checked and removed by the rules below. The arena helper of the drivers that apply writes for a measurement
(measure/cppwrite.py's Arena for cppwlatency and the write-count gate). The gates that copy a store with the binary's
`checkpoint` (tests/gates/meta/check_write_effects.py, tests/gates/store_copies.py) get the same copy:

- an arena lives in <stores root>/arenas/<store>-<run id>/ (stores.cpp_stores_root()), never under a store tree
  another driver scans, and on the base's filesystem (a hard link cannot cross one: refused);
- the base store is never opened: create() links its SSTs and copies CURRENT, the MANIFEST that CURRENT names, the
  latest OPTIONS and any WAL, as RocksDB's Checkpoint would, but without a RocksDB open (which writes a new MANIFEST,
  OPTIONS and LOG into the base). It refuses while any process holds the base's db/LOCK open, and prints who;
- the base store is checked unchanged (base_state before and after; any difference fails the run);
- remove() deletes only a dir holding this module's marker (arena-marker.json) with the caller's run id, and prints
  the path and its size; anything else is never removed. A leftover arena is refused, never removed.
"""
import hashlib
import json
import os
import shutil
import subprocess
import time

MARKER = "arena-marker.json"
_NOTED = set()          # the arenas roots whose leftovers were already named in this process (once, not per cell)


def arenas_root(stores_root):
    """<stores root>/arenas: beside the scale dirs, never inside one"""
    return os.path.join(stores_root, "arenas")


def lock_holder(base):
    """who holds the base store's db/LOCK open: lsof's lines, "" when no process does. Refuses when lsof cannot say."""
    lock = os.path.join(base, "db", "LOCK")
    if not os.path.exists(lock):
        return ""
    try:
        p = subprocess.run(["lsof", "-w", lock], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SystemExit(f"cannot tell who holds {lock}: lsof failed ({e})")
    if p.returncode not in (0, 1) or (p.returncode == 1 and p.stderr.strip()):
        raise SystemExit(f"cannot tell who holds {lock}: lsof rc {p.returncode}: {p.stderr.strip()[:500]}")
    return p.stdout.strip() if p.returncode == 0 else ""


def base_state(base):
    """The base store's identity on disk: every file of db/ with its size, and for an SST also its inode and mtime;
    the sha256 of MANIFEST-*, OPTIONS-*, CURRENT and of the store's manifest.json. LOG files are left out (RocksDB
    rotates its INFO log on every open)."""
    db = os.path.join(base, "db")
    files = {}
    for name in sorted(os.listdir(db)):
        if name.startswith("LOG"):
            continue
        st = os.stat(os.path.join(db, name))
        e = {"bytes": st.st_size}
        if name.endswith(".sst"):
            e.update(inode=st.st_ino, mtime_ns=st.st_mtime_ns)
        if name.startswith(("MANIFEST-", "OPTIONS-")) or name == "CURRENT":
            e["sha256"] = hashlib.sha256(open(os.path.join(db, name), "rb").read()).hexdigest()
        files[name] = e
    m = os.path.join(base, "manifest.json")
    return {"files": files, "manifest_json_sha256": hashlib.sha256(open(m, "rb").read()).hexdigest()}


def state_diff(a, b, ssts_only=False):
    """[differences] between two base_state()s; ssts_only: compare the SST set alone (names, sizes, inodes, mtimes)"""
    fa = {k: v for k, v in a["files"].items() if not ssts_only or k.endswith(".sst")}
    fb = {k: v for k, v in b["files"].items() if not ssts_only or k.endswith(".sst")}
    out = [f"only before: {k}" for k in sorted(set(fa) - set(fb))] + [f"only after: {k}" for k in sorted(set(fb) - set(fa))]
    out += [f"changed: {k} {fa[k]} -> {fb[k]}" for k in sorted(set(fa) & set(fb)) if fa[k] != fb[k]]
    if not ssts_only and a["manifest_json_sha256"] != b["manifest_json_sha256"]:
        out.append("changed: manifest.json")
    return out


def du_bytes(path):
    n = 0
    for d, _subdirs, files in os.walk(path):
        for f in files:
            try:
                n += os.lstat(os.path.join(d, f)).st_size
            except OSError:
                pass
    return n


def _current_manifest(db):
    name = open(os.path.join(db, "CURRENT")).read().strip()
    if not name.startswith("MANIFEST-") or not os.path.isfile(os.path.join(db, name)):
        raise SystemExit(f"{db}/CURRENT names {name!r}, which is no MANIFEST there")
    return name


def _copy_plan(db):
    """(files to hard-link, files to copy) of a closed store's db/: the SSTs and blobs are linked; CURRENT, the MANIFEST it
    names, the latest OPTIONS and every WAL are copied; LOCK, LOG*, IDENTITY and older MANIFEST / OPTIONS are left out
    (RocksDB writes a new IDENTITY on open). Any other file is refused."""
    names = sorted(os.listdir(db))
    manifest = _current_manifest(db)
    options = sorted((n for n in names if n.startswith("OPTIONS-")), key=lambda n: int(n.split("-")[1]))
    link = [n for n in names if n.endswith((".sst", ".blob"))]
    copy = ["CURRENT", manifest] + options[-1:] + [n for n in names if n.endswith(".log")]
    skip = {"LOCK", "IDENTITY"} | {n for n in names if n.startswith(("LOG", "MANIFEST-", "OPTIONS-"))}
    other = sorted(set(names) - set(link) - set(copy) - skip)
    if other:
        raise SystemExit(f"{db} holds files a link copy does not know: {other[:10]}")
    if not options:
        raise SystemExit(f"{db} has no OPTIONS file")
    return link, copy


def create(base, stores_root, run_id, driver=None, omit_sst=False):
    """A new arena of `base` for run `run_id`: (arena dir, {before_copy, after_copy, changed_by_copy}). The base is never
    opened; it is refused while a process holds its LOCK, and its state (SSTs, MANIFEST, OPTIONS, CURRENT,
    manifest.json) must be the same after the copy. omit_sst: the test's control, a copy missing its first SST."""
    root = arenas_root(stores_root)
    os.makedirs(root, exist_ok=True)
    if os.stat(base).st_dev != os.stat(root).st_dev:
        raise SystemExit(f"{base} and {root} are on different filesystems: an arena needs hard links")
    arena = os.path.join(root, f"{os.path.basename(os.path.normpath(base))}-{run_id}")
    left = sorted(n for n in os.listdir(root) if os.path.isfile(os.path.join(root, n, MARKER)))
    if left and root not in _NOTED:              # a killed run's arena: never removed here, but never silent either
        _NOTED.add(root)
        print(f"  note: {len(left)} arena(s) of other runs in {root} (a killed run leaves one; its hard links keep old "
              f"SSTs on disk): {left[:5]}{' ...' if len(left) > 5 else ''}", flush=True)
    if os.path.exists(arena):
        raise SystemExit(f"arena {arena} already exists: not made again and not removed (an earlier run's?)")
    if os.path.exists(os.path.join(base, "arena.json")):
        raise SystemExit(f"{base} is itself an arena: copy the pristine store instead")
    if not json.load(open(os.path.join(base, "manifest.json"))).get("settled", False):
        raise SystemExit(f"{base} is not settled: no copy")
    db = os.path.join(base, "db")
    holder = lock_holder(base)
    if holder:
        raise SystemExit(f"{base} is open in another process; no copy. Holder of {db}/LOCK:\n{holder}")
    before = base_state(base)
    link, copy = _copy_plan(db)
    if omit_sst:
        link = link[1:]
    os.makedirs(os.path.join(arena, "db"))
    json.dump({"run_id": run_id, "base": os.path.abspath(base), "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "driver": driver, "method": "link copy", "omit_sst": omit_sst}, open(os.path.join(arena, MARKER), "w"), indent=1)
    for n in link:
        os.link(os.path.join(db, n), os.path.join(arena, "db", n))
    for n in copy:
        shutil.copyfile(os.path.join(db, n), os.path.join(arena, "db", n))
    shutil.copyfile(os.path.join(base, "manifest.json"), os.path.join(arena, "manifest.json"))
    # the C++ session's own marker: apply_writes is refused on a dir without arena.json (cli/checkpoint.cc's shape)
    json.dump({"source": os.path.abspath(base), "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "method": "link copy",
               "driver": driver}, open(os.path.join(arena, "arena.json"), "w"), indent=1)
    after = base_state(base)
    changed = state_diff(before, after)
    if changed or lock_holder(base):
        raise SystemExit(f"{base} changed or was opened during its copy: {changed[:5]}; the arena {arena} is left in place")
    return arena, {"before_copy": before, "after_copy": after, "changed_by_copy": changed}


def check_unchanged(base, expect, where):
    """the base store must be as it was when its arena was made"""
    d = state_diff(expect, base_state(base))
    if d:
        raise SystemExit(f"{where}: the base store {base} changed: {d[:10]}")


def remove(arena, run_id, stores_root):
    """Remove an arena this run made: only a dir under the arenas root holding the marker with this run id. Prints the
    path and its size; returns the bytes removed."""
    root = os.path.realpath(arenas_root(stores_root))
    real = os.path.realpath(arena)
    if os.path.dirname(real) != root:
        raise SystemExit(f"refusing to remove {arena}: not directly under {root}")
    mpath = os.path.join(real, MARKER)
    if not os.path.isfile(mpath) or json.load(open(mpath)).get("run_id") != run_id:
        raise SystemExit(f"refusing to remove {arena}: no {MARKER} with run id {run_id}")
    n = du_bytes(real)
    shutil.rmtree(real)
    print(f"  removed arena {real} ({n / 1e6:.1f} MB, hard links included)", flush=True)
    return n


class Arena:
    """An arena as a context manager: made on enter, the base checked unchanged and the arena removed (by its marker)
    on exit; .keep = True keeps it. run_id must be unique among the runs that share the stores root."""

    def __init__(self, base, run_id, driver, stores_root=None):
        from kvevaluator.measure.stores import cpp_stores_root
        self.src, self.run_id, self.driver = base, run_id, driver
        self.stores_root = stores_root or cpp_stores_root()
        self.dir, self.state, self.keep = None, None, False

    def __enter__(self):
        self.dir, self.state = create(self.src, self.stores_root, self.run_id, driver=self.driver)
        return self

    def __exit__(self, exc_type, *exc):
        try:
            if exc_type is None:
                check_unchanged(self.src, self.state["after_copy"], self.dir)
        finally:
            if not self.keep and self.dir and os.path.isdir(self.dir):
                remove(self.dir, self.run_id, self.stores_root)
