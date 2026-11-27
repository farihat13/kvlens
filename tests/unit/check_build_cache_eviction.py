"""The C++ build cache's eviction (run/build_cpp.py --evict-list / --evict) on a fake cache, no build: the entries past
the --keep most recent and unused for --days are listed; one hard-linked from outside (a tree uses its binary), one
whose lock a build holds and one used recently are kept; a link inside the entry does not keep it; --evict removes
exactly the listed ones and never a rocksdb/ build.

    python tests/unit/check_build_cache_eviction.py
"""
import contextlib
import fcntl
import importlib.util
import io
import os
import sys
import tempfile
import time
from pathlib import Path

from kvevaluator.paths import ROOT

_spec = importlib.util.spec_from_file_location("build_cpp", ROOT / "run" / "build_cpp.py")
B = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(B)

DAY = 86400


def entry(cache, name, age_days, now):
    """a fake by-tree entry used `age_days` ago, with a binary and its commit's hard link inside it"""
    d = cache / "by-tree" / name
    (d / "src" / "build" / "cpp").mkdir(parents=True)
    (d / "src" / "build" / "cpp" / "kvevaluator").write_bytes(b"x" * 1000)
    (d / "src" / "build" / f"at-{name:0<12}").mkdir()
    os.link(d / "src" / "build" / "cpp" / "kvevaluator", d / "src" / "build" / f"at-{name:0<12}" / "kvevaluator")
    (d / "LAST_USED").write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - age_days * DAY)) + "\n")
    return d


def main():
    results = []

    def check(name, ok, detail=""):
        results.append(bool(ok))
        print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail and not ok else ""))

    with tempfile.TemporaryDirectory() as t:
        cache, now = Path(t) / "cache", time.time()
        for i in range(6):                                   # the 6 most recent: never listed
            entry(cache, f"recent{i}", 0.1 * i, now)
        entry(cache, "fresh", 1, now)                        # past the 6, used a day ago: kept
        entry(cache, "old", 5, now)                          # past the 6, unused 5 days: evicted
        linked = entry(cache, "linked", 6, now)              # a tree's binary links to it: kept
        (Path(t) / "tree" / "build" / "cpp").mkdir(parents=True)
        os.link(linked / "src" / "build" / "cpp" / "kvevaluator", Path(t) / "tree" / "build" / "cpp" / "kvevaluator")
        entry(cache, "locked", 7, now)                       # a build holds its lock: kept
        entry(cache, "controls-old", 8, now)                 # a controls entry counts the same: evicted
        (cache / "rocksdb" / "8.0-flags").mkdir(parents=True)   # never listed
        lock = open(cache / "by-tree" / "locked.lock", "w")
        fcntl.flock(lock, fcntl.LOCK_EX)

        rows = {d.name: why for d, why in B.eviction_list(cache, keep=6, days=2, now=now)}
        check("past the 6 most recent: the 5 older entries, no rocksdb build",
              sorted(rows) == ["controls-old", "fresh", "linked", "locked", "old"], sorted(rows))
        check("evicted: the old entry and the old controls entry (a link inside an entry does not keep it)",
              sorted(n for n, why in rows.items() if why is None) == ["controls-old", "old"], rows)
        check("kept: used within the days", "within" in str(rows.get("fresh")), rows.get("fresh"))
        check("kept: hard-linked from a tree", "linked from outside" in str(rows.get("linked")), rows.get("linked"))
        check("kept: its lock is held", "lock" in str(rows.get("locked")), rows.get("locked"))
        check("--keep 20: nothing listed", B.eviction_list(cache, keep=20, days=2, now=now) == [])

        with contextlib.redirect_stdout(io.StringIO()) as out:
            B.evict(cache, keep=6, days=2, remove=False)
        left = sorted(p.name for p in (cache / "by-tree").iterdir() if p.is_dir())
        check("--evict-list removes nothing", len(left) == 11, left)
        check("--evict-list names what --evict would remove", "2 of 5 entries" in out.getvalue(), out.getvalue()[-200:])

        with contextlib.redirect_stdout(io.StringIO()) as out:
            B.evict(cache, keep=6, days=2, remove=True)
        left = sorted(p.name for p in (cache / "by-tree").iterdir() if p.is_dir())
        check("--evict removes exactly the listed two", left == sorted(
            [f"recent{i}" for i in range(6)] + ["fresh", "linked", "locked"]), left)
        check("--evict keeps the rocksdb build", (cache / "rocksdb" / "8.0-flags").is_dir())
        check("--evict keeps the tree's binary", (Path(t) / "tree" / "build" / "cpp" / "kvevaluator").exists())
        lock.close()

    ok = all(results)
    print(f"build cache eviction {'PASS' if ok else 'FAIL'}: {len(results)} cases, {results.count(False)} failed")
    return 0 if ok and results else 1


if __name__ == "__main__":
    sys.exit(main())
