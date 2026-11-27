"""Rebuild a store without removing the old one: the one move-aside rule every store rebuild uses (stores.py's ensure_*
and ingest_multi, measure/reingest_stores.py). The old store is renamed into one dated dir on the same filesystem,
<parent of the store's dir>/aside-<YYYYMMDD>/<the store's dir name>/<store> (output/stores/ldbc/aside-20261018/sf3/dgraph);
a build that fails puts it back. Nothing here removes a store: the aside dirs are removed by hand.
Also the one builder per store (building): two processes that need one missing store would ingest into one dir at once
(8 of 90 ingests lost when tests-fast ran beside a store build in one output dir)."""
import contextlib
import fcntl
import os
import shutil
import sys
import time

from kvevaluator.capture.store_identity import live_ingests

IGNORE_DISK_ENV = "KVEVAL_IGNORE_DISK"     # =1: run a rebuild even when the free space looks too small


def aside_dir_for(path):
    """the dated aside dir for a store at path: <parent's parent>/aside-<YYYYMMDD>/<parent's name>"""
    parent = os.path.dirname(os.path.abspath(path))
    return os.path.join(os.path.dirname(parent), f"aside-{time.strftime('%Y%m%d', time.gmtime())}",
                        os.path.basename(parent))


def dir_bytes(path):
    """bytes under a dir (a store: its SSTs, logs and manifest)"""
    return sum(os.path.getsize(os.path.join(d, f)) for d, _sub, files in os.walk(path) for f in files)


def free_bytes(path):
    return shutil.disk_usage(path).free


def check_aside(aside, root, names):
    """Refuse (SystemExit) an aside dir on another filesystem than the stores at root (a move there is a copy), or one
    that already holds one of names (an aside entry is never overwritten)."""
    probe = aside
    while not os.path.exists(probe):          # the dir may not exist yet: its nearest existing parent decides the device
        probe = os.path.dirname(probe)
    if os.stat(probe).st_dev != os.stat(root).st_dev:
        raise SystemExit(f"[store-aside] the aside dir {aside} is on another filesystem than the stores ({root}): "
                         f"a move there is a copy -- name a dir on the stores' filesystem")
    taken = sorted(n for n in names if os.path.exists(os.path.join(aside, n)))
    if taken:
        raise SystemExit(f"[store-aside] {aside} already holds {taken}: an aside entry is never overwritten -- "
                         f"another aside dir, or move those away first")


def check_free(root, paths, ignore_disk=False):
    """Refuse rebuilding stores whose new copies (about the old ones' size, + 10%) do not fit in the free space: old and
    new both stay until someone removes the aside dir. Returns (needed, free) bytes."""
    need = int(1.1 * sum(dir_bytes(p) for p in paths if os.path.isdir(p)))
    free = free_bytes(root)
    print(f"[store-aside] {len(paths)} stores, {need / 1e9:.2f} GB needed (their size + 10%), {free / 1e9:.2f} GB "
          f"free on {root}", file=sys.stderr)
    if need > free and not (ignore_disk or os.environ.get(IGNORE_DISK_ENV) == "1"):
        raise SystemExit(f"[store-aside] not enough free space: {need / 1e9:.2f} GB needed, {free / 1e9:.2f} GB free "
                         f"-- fewer stores per batch, room made, or --ignore-disk / {IGNORE_DISK_ENV}=1")
    return need, free


def lock_path(path):
    """the build lock of the store at path: a hidden file beside it, .<store>.build.lock (kept: an empty lock file)"""
    path = os.path.abspath(path)
    return os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.build.lock")


STALLED_S = 15 * 60   # a build whose store has not changed this long is not waited on (an sf30 ingest writes throughout)


def ingest_state(pid):
    """the state letter of a process from /proc (R, S, T, ...), or gone"""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return "gone"


def store_progress(path):
    """(file count, bytes, newest mtime) of the store dir: changes while a build writes it"""
    n = size = newest = 0
    for d, _, fs in os.walk(path):
        for name in fs:
            try:
                st = os.stat(os.path.join(d, name))
            except OSError:                        # removed under us (a compaction)
                continue
            n, size, newest = n + 1, size + st.st_size, max(newest, st.st_mtime_ns)
    return n, size, newest


def wait_for_build_lock(f, path):
    """Take the build lock of the store at path, waiting while another process builds it. Refuses (SystemExit, by
    name) when the build cannot finish while we wait: an ingest writing the store is stopped (state T), or the store
    has not changed for STALLED_S."""

    name = os.path.basename(path)
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    except BlockingIOError:
        pass
    f.seek(0)
    holder = f.read().strip() or "?"
    print(f"[store-aside] {name}: another process builds it ({holder}); waiting for it", file=sys.stderr)
    last, since = store_progress(path), time.monotonic()
    while True:
        time.sleep(1)
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)   # released when the file closes, also when the holder dies
            return
        except BlockingIOError:
            pass
        stopped = [pid for pid in live_ingests(path) if ingest_state(pid) in ("T", "t")]
        if stopped:
            raise SystemExit(f"[store-aside] {name}: an ingest is still writing it but is stopped (pid "
                             f"{', '.join(map(str, stopped))}, state T; the build lock's holder: {holder}); refusing "
                             f"rather than waiting on it")
        now = store_progress(path)
        if now != last:
            last, since = now, time.monotonic()
        elif time.monotonic() - since > STALLED_S:
            raise SystemExit(f"[store-aside] {name}: its build ({holder}) has not changed the store for "
                             f"{STALLED_S // 60} min; refusing rather than waiting on it")


@contextlib.contextmanager
def building(paths):
    """Hold the build lock of each store in paths (sorted: two callers never wait on each other's second lock). A
    second process that needs the same store waits here, naming the holder (wait_for_build_lock: it refuses a build
    that cannot finish); the caller then checks again whether the store is still missing (it usually is not). Stores
    of different names never wait on each other."""
    paths = sorted(os.path.abspath(p) for p in ([paths] if isinstance(paths, str) else paths))
    with contextlib.ExitStack() as stack:
        for p in paths:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            f = stack.enter_context(open(lock_path(p), "a+"))
            wait_for_build_lock(f, p)
            f.seek(0)
            f.truncate()
            f.write(f"pid {os.getpid()} since {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
            f.flush()
        yield


@contextlib.contextmanager
def replacing(paths, aside=None, ignore_disk=False):
    """Move each existing store in paths aside, then run the body (the build). If the body raises (SystemExit too),
    each partial new dir goes aside as <name>.failed-<HHMMSS> and the old store comes back, then the error goes on.
    aside: the one dir for all of them (default: aside_dir_for the first path; every path must share its parent)."""
    paths = [os.path.abspath(p) for p in ([paths] if isinstance(paths, str) else paths)]
    old = [p for p in paths if os.path.isdir(p)]
    if not old:
        yield None
        return
    root = os.path.dirname(old[0])
    if any(os.path.dirname(p) != root for p in old):
        raise SystemExit(f"[store-aside] stores of one rebuild must share a dir: {old}")
    aside = os.path.abspath(aside or aside_dir_for(old[0]))
    check_aside(aside, root, [os.path.basename(p) for p in old])
    check_free(root, old, ignore_disk)
    os.makedirs(aside, exist_ok=True)
    moved = []
    for p in old:
        os.rename(p, os.path.join(aside, os.path.basename(p)))      # a rename on one filesystem: free, instant
        moved.append(p)
    print(f"[store-aside] old {', '.join(os.path.basename(p) for p in moved)} -> {aside}", file=sys.stderr)
    try:
        yield aside
    except BaseException:
        stamp = time.strftime("%H%M%S", time.gmtime())
        for p in moved:
            if os.path.exists(p):
                os.rename(p, os.path.join(aside, f"{os.path.basename(p)}.failed-{stamp}"))
            os.rename(os.path.join(aside, os.path.basename(p)), p)
        print(f"[store-aside] the build failed: {', '.join(os.path.basename(p) for p in moved)} put back; any partial "
              f"dir kept in {aside} as <name>.failed-{stamp}", file=sys.stderr)
        raise
