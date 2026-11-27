#!/usr/bin/env python3
"""build_cpp.py: build the C++ evaluator once per committed source state on a machine, in a shared cache, and
hard-link the binary into this tree (`just build`).

The cache (kvevaluator.paths.build_cache_dir(): <main clone>/build/cache, shared by every worktree of the repo):

    by-tree/<key>/src/                    a git archive of cpp/kvevaluator and the paths CMake's "repo paths" block names
    by-tree/<key>/src/build/cpp/          its build: the objects and the unit tests (ctest runs here)
    by-tree/<key>/src/build/at-<commit>/  the binary relinked for one commit (its stamp names that commit)
    by-tree/controls-<key>/               one commit's control binaries (below)
    rocksdb/<release>-<flags>-<toolchain>/   one RocksDB build, shared by every entry and every build in place

The key is a sha256 over the git trees of those paths (cpp/kvevaluator's .md files aside) and the build settings (the extra CMake arguments, the
compiler and the environment variables CMake reads, cmake, the system libraries' package versions), so a commit that
changes only other Python reuses the entry: its objects are compiled once, and each commit gets its own relinked binary
(seconds), so the binary always names the commit it is used at. One lock per key: a second build of the same key
waits, then reuses. The source is a snapshot from git, never the working tree, so an edit made during a build cannot
leave a stale object in an entry. The binary is HARD-linked into <build dir>/cpp/kvevaluator (copied
across filesystems): it finds the repo root from its own path (src/schema/schema_path.cc), so a symlink would make
it read the cache; the link is checked to resolve this tree before it replaces the old binary.

A tree with uncommitted changes under those paths builds IN PLACE in <build dir>/cpp, incrementally, linking the
shared RocksDB; its binary says dirty, so the gates refuse it. So does a build dir that is not
<root>/build (the compiled-in path to the root would be wrong for a cached binary) and a tree with no git.

The controls of a commit share one entry, controls-<key>, the cache's only build dir that changes: under its lock a
patch is applied, the binary built incrementally and copied to src/build/control-<patch>-<commit>/, the patch reversed.
Guards: the source must equal its archive before a control is built, and a control must differ from the unpatched
binary of the same commit. A control's (or a --sha binary's) repo root is its archive, which holds the LDBC schemas
only: it opens LDBC stores; a JOB or LinkBench store needs a binary linked into a tree.

    python run/build_cpp.py                      # this tree: the cache when clean, in place when dirty; print --version
    python run/build_cpp.py --sha <sha>          # that commit's binary in the cache; prints its path, links nothing
    python run/build_cpp.py --sha <sha> --patch <file>   # a control: the commit plus a patch (refused with fuzz)
    python run/build_cpp.py --ctest [BINARY] [-- ctest args]  # the unit tests of the build that made BINARY
    python run/build_cpp.py --entry [BINARY]     # that build's dir
    python run/build_cpp.py --list               # the cache's entries: size, commits, last use (for a removal list)
    python run/build_cpp.py --evict-list [--keep 6] [--days 2]   # the entries to remove (none linked from a tree, none locked)
    python run/build_cpp.py --evict              # remove them, each under its lock (the removal queue's owner only)
    python run/build_cpp.py --in-place           # build in place even when clean
    python run/build_cpp.py -- -DEVALUATOR_ALLOCATOR=glibc   # extra CMake arguments (part of the key)
"""
import argparse
import calendar
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from kvevaluator.paths import BUILD_DIR, ROOT, build_cache_dir, cpp_bin
from kvevaluator.datasets import schema_path

CPP = "cpp/kvevaluator"
TREE_BIN = BUILD_DIR / "cpp" / "kvevaluator"   # where the build puts the tree's binary (never KVEVAL_CPP_BIN's file)
LDBC_SCHEMA = os.path.relpath(schema_path("ldbc"), ROOT)   # what the root check resolves (repo-relative)
# environment variables CMake or the compiler read: part of the key, since they change the build
ENV_KEYS = ("CC", "CXX", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS", "CMAKE_BUILD_TYPE", "CMAKE_GENERATOR",
            "CMAKE_CXX_COMPILER_LAUNCHER", "CMAKE_PREFIX_PATH", "CMAKE_TOOLCHAIN_FILE")


def log(msg):
    print(f"[build-cpp] {time.strftime('%H:%M:%S', time.gmtime())} {msg}", flush=True)


def run(cmd, show_output=False, **kw):
    """run `cmd`; on failure exit naming it (and print its captured output when asked)"""
    r = subprocess.run(cmd, **kw)
    if r.returncode != 0:
        if show_output:
            print((r.stdout or "") + (r.stderr or ""), file=sys.stderr)
        sys.exit(f"[build-cpp] FAILED (rc {r.returncode}): {' '.join(map(str, cmd))}")
    return r


def git(*args, env=None):
    return run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, env={**os.environ, **(env or {})}).stdout


def cmakelists(commit):
    """CMakeLists.txt as `commit` has it (the paths it builds and the packages it links are that commit's)"""
    return git("show", f"{commit}:{CPP}/CMakeLists.txt")


def repo_paths(text):
    """cpp/kvevaluator plus every path set in CMakeLists.txt's "repo paths" block (the one place they are named)"""
    block = text[text.index("# --- repo paths"):]
    block = block[:block.index("\n# ---", 1)]
    named = [v for k, v in re.findall(r'set\((KVEVAL_\w+)\s+"([^"]+)"\)', block) if k != "KVEVAL_ROOT_REL"]
    if not named:
        sys.exit("[build-cpp] no paths in CMakeLists.txt's repo paths block")
    return [CPP, *named]


def normalize(cmake_args):
    """`-D X=Y` -> `-DX=Y`, so a setting has one spelling in the key and in the RocksDB release lookup"""
    out, i = [], 0
    while i < len(cmake_args):
        a = cmake_args[i]
        if a == "-D" and i + 1 < len(cmake_args):
            a, i = "-D" + cmake_args[i + 1], i + 1
        out.append(a)
        i += 1
    return out


def rocksdb_release(cmake_args):
    """the KVEVAL_ROCKSDB the arguments set (`-DKVEVAL_ROCKSDB[:TYPE]=x`), else None (CMake's default)"""
    got = [m.group(1) for a in cmake_args for m in [re.match(r"-DKVEVAL_ROCKSDB(?::\w+)?=(.*)$", a)] if m]
    return got[-1] if got else None


def toolchain(text):
    """what the binary depends on beyond its source: the compiler CMake will use, cmake, the environment variables
    CMake reads, and the system libraries CMake links (their package versions)"""
    def first(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True).stdout.splitlines()[:1]
        except OSError:
            return [f"missing: {cmd[0]}"]
    apt = re.search(r'set\(KVEVAL_APT_LINE "[^"]*install -y ([^"]+)"\)', text)
    pkgs = apt.group(1).split() if apt else []
    versions = []
    if pkgs and shutil.which("dpkg-query"):
        versions = subprocess.run(["dpkg-query", "-W", "-f", "${Package}=${Version}\n", *pkgs],
                                  capture_output=True, text=True).stdout.split()
    return {"c++": first([os.environ.get("CXX") or "c++", "--version"]), "cmake": first(["cmake", "--version"]),
            "env": {k: os.environ[k] for k in ENV_KEYS if k in os.environ}, "packages": sorted(versions)}


def short(d):
    return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()


def tree_ids(treeish, paths):
    """{path: the id of its content at `treeish`}; for cpp/kvevaluator, a sha over its files with the .md files aside
    (docs are not what the binary runs: the rule of capture/binary.py's binary_cpp_verdict), so a README edit reuses"""
    ids = {}
    for p in paths:
        if p == CPP:
            entries = [e for e in git("ls-tree", "-r", treeish, "--", p).splitlines() if not e.endswith(".md")]
            ids[p] = hashlib.sha256("\n".join(entries).encode()).hexdigest()
        else:
            ids[p] = git("rev-parse", f"{treeish}:{p}").strip()
    return ids


def worktree_dirty(paths):
    """the archived paths of the working tree differ from HEAD's (a temporary index: untracked files count)"""
    with tempfile.TemporaryDirectory() as d:
        env = {"GIT_INDEX_FILE": os.path.join(d, "index")}
        git("read-tree", "HEAD", env=env)
        git("add", "-A", "--", *paths, env=env)
        tree = git("write-tree", env=env).strip()
    return tree_ids(tree, paths) != tree_ids("HEAD", paths)


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log(f"waiting for {path.name} (another build holds it)")
            fcntl.flock(f, fcntl.LOCK_EX)
        yield


def touch(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n")


def rocksdb_dir(cache, src, cmake_args, tc, jobs):
    """the shared RocksDB build for these pins, flags and toolchain; built first, in a temporary dir renamed into place
    once checked, when no build has finished it. Its CMake dir goes once the library is in place, and an interrupted
    attempt's dirs at the next build (under the lock no other build of this name runs)"""
    release = rocksdb_release(cmake_args)
    r = run(["cmake", *([f"-DKVEVAL_ROCKSDB={release}"] if release else []), "-P",
             str(src / CPP / "cmake" / "rocksdb_pins.cmake")], capture_output=True, text=True, show_output=True)
    rid = (r.stdout + r.stderr).strip()
    name = f"{rid}-{short(tc)[:8]}"
    rdir = cache / "rocksdb" / name
    with locked(cache / "rocksdb" / f"{name}.lock"):
        for stale in sorted((cache / "rocksdb").glob(f"{name}.tmp-*")):
            log(f"RocksDB {name}: removing {stale.name}, left by an earlier attempt")
            shutil.rmtree(stale)
        if not (rdir / "kvevaluator-rocksdb.done").exists():
            tmp = cache / "rocksdb" / f"{name}.tmp-{os.getpid()}"
            log(f"RocksDB {name}: building once for this machine")
            t0 = time.time()
            # --jobs reaches RocksDB's own make too (its default is every core: rocksdb.cmake, KVEVAL_ROCKSDB_JOBS)
            run(["cmake", "-S", str(src / CPP), "-B", f"{tmp}-cmake", f"-DKVEVAL_ROCKSDB_DIR={tmp}",
                 f"-DKVEVAL_ROCKSDB_JOBS={jobs}", *cmake_args], stdout=subprocess.DEVNULL)
            run(["cmake", "--build", f"{tmp}-cmake", "--target", "rocksdb_build", "-j", str(jobs)])
            v = (tmp / "include" / "rocksdb" / "version.h").read_text()
            built = ".".join(re.search(rf"#define ROCKSDB_{p} (\d+)", v).group(1) for p in ("MAJOR", "MINOR", "PATCH"))
            if not rid.startswith(built + "-"):   # the library the step built must be the release the dir is named for
                sys.exit(f"[build-cpp] the RocksDB build in {tmp} is {built}, not {rid}: refused")
            (tmp / "kvevaluator-rocksdb.done").write_text(rid + "\n")
            os.rename(tmp, rdir)
            shutil.rmtree(f"{tmp}-cmake")   # the build's CMake dir: the library is in rdir
            log(f"RocksDB {name}: built in {time.time() - t0:.0f} s")
    return rdir


def entry_key(ids, cmake_args, tc):
    inputs = {"trees": ids, "cmake_args": cmake_args, "toolchain": tc}
    return inputs, short(inputs)[:20]


def source_manifest(src):
    """sha256 over every source file's path and bytes (the build dirs aside): what the archive extracted"""
    h = hashlib.sha256()
    for f in sorted(p for p in src.rglob("*") if p.is_file() and p.relative_to(src).parts[0] != "build"):
        h.update(str(f.relative_to(src)).encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def extract(entry, commit, paths, inputs):
    """the entry's source: the git archive, (re)extracted whole unless an earlier extraction finished"""
    src = entry / "src"
    if not (entry / "SRC_DONE").exists():   # an interrupted extraction is overwritten
        src.mkdir(parents=True, exist_ok=True)
        archive = run(["git", "-C", str(ROOT), "archive", "--format=tar", commit, "--", *paths], capture_output=True).stdout
        run(["tar", "-x", "-C", str(src)], input=archive)
        (entry / "SRC_DONE").write_text(json.dumps({**inputs, "manifest": source_manifest(src)},
                                                   sort_keys=True, indent=1) + "\n")
    return src


def configure(src, build, rdir, commit, dirty, key, cmake_args):
    run(["cmake", "-S", str(src / CPP), "-B", str(build), f"-DKVEVAL_ROCKSDB_DIR={rdir}",
         f"-DKVEVAL_BUILD_COMMIT={commit}", f"-DKVEVAL_BUILD_DIRTY={dirty}", f"-DKVEVAL_BUILD_KEY={key}",
         *cmake_args], stdout=subprocess.DEVNULL)


def keep_copy(binary, out):
    """`binary` at `out`, a hard link (the linker replaces its output by a new file, so the link keeps these bytes)"""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.{os.getpid()}")
    try:
        os.link(binary, tmp)
    except OSError:
        shutil.copy2(binary, tmp)
    os.replace(tmp, out)


def build_entry(cache, commit, cmake_args, jobs):
    """`commit`'s binary in the cache: the entry's objects built once, then the binary relinked for this commit"""
    text = cmakelists(commit)
    paths, tc = repo_paths(text), toolchain(text)
    inputs, key = entry_key(tree_ids(commit, paths), cmake_args, tc)
    entry = cache / "by-tree" / key
    build = entry / "src" / "build" / "cpp"
    out = entry / "src" / "build" / f"at-{commit[:12]}" / "kvevaluator"   # the build dir's depth: its root is src
    if (out.parent / "DONE").exists():
        log(f"entry {key} at {commit[:10]}: built before, reused")
        touch(entry / "LAST_USED")
        return out
    with locked(cache / "by-tree" / f"{key}.lock"):
        if (out.parent / "DONE").exists():
            log(f"entry {key} at {commit[:10]}: built while this build waited, reused")
            return out
        t0 = time.time()
        src = extract(entry, commit, paths, inputs)
        rdir = rocksdb_dir(cache, src, cmake_args, tc, jobs)
        first = not (entry / "DONE").exists()
        log(f"entry {key} at {commit[:10]}: {'configuring and building' if first else 'relinking'} (-j {jobs})")
        configure(src, build, rdir, commit, "false", key, cmake_args)   # the stamp names this commit
        run(["cmake", "--build", str(build), "-j", str(jobs)])
        run(["cmake", "--build", str(build), "--target", "unit_tests", "-j", str(jobs)])   # ctest then builds nothing
        if first:
            (entry / "DONE").write_text(json.dumps({"key": key, **inputs}, sort_keys=True, indent=1) + "\n")
        keep_copy(build / "kvevaluator", out)
        (out.parent / "DONE").write_text(json.dumps({"commit": commit, "wall_s": round(time.time() - t0, 1)}) + "\n")
        touch(entry / "LAST_USED")
        log(f"entry {key} at {commit[:10]}: done in {time.time() - t0:.0f} s")
    return out


def build_control(cache, commit, patch, cmake_args, jobs):
    """a control binary: `commit` plus `patch`, built in the commit's controls entry (controls-<key>), the cache's only
    build dir that changes: under its lock, apply the patch, build the binary incrementally, copy it out, reverse the
    patch. Guards: the source must equal its archive before a control is built (an interrupted one refuses by name),
    and the control must differ from the unpatched binary of the same commit (a patch that did not reach the build)."""
    text = cmakelists(commit)
    paths, tc = repo_paths(text), toolchain(text)
    inputs, key = entry_key(tree_ids(commit, paths), cmake_args, tc)
    name = f"controls-{key}"
    entry = cache / "by-tree" / name
    build = entry / "src" / "build" / "cpp"
    patch = Path(patch).resolve()
    psha = hashlib.sha256(patch.read_bytes()).hexdigest()[:16]
    out = entry / "src" / "build" / f"control-{psha}-{commit[:12]}" / "kvevaluator"   # the depth of the build dir
    with locked(cache / "by-tree" / f"{name}.lock"):
        if (out.parent / "DONE").exists():
            done = json.loads((out.parent / "DONE").read_text())
            print(done.get("patch_output", ""), end="")   # its offsets, as when it was built
            log(f"control {patch.name} at {commit[:10]}: built before in {name}, reused")
            touch(entry / "LAST_USED")
            return out
        t0 = time.time()
        src = extract(entry, commit, paths, inputs)
        want = json.loads((entry / "SRC_DONE").read_text())["manifest"]
        if source_manifest(src) != want:
            sys.exit(f"[build-cpp] REFUSED: {src} differs from its archive (an interrupted control left a patch "
                     f"applied?): list {entry} for removal and build again")
        rdir = rocksdb_dir(cache, src, cmake_args, tc, jobs)
        base = build / "kvevaluator"
        bases_file = entry / "BASE_SHA256.json"   # {commit: the unpatched binary's sha256}
        bases = json.loads(bases_file.read_text()) if bases_file.exists() else {}
        # every binary of one commit carries one stamp (dirty, this commit, this entry), so only a patch tells them apart
        configure(src, build, rdir, commit, "true", name, cmake_args)
        if commit not in bases:
            log(f"{name}: building the unpatched binary at {commit[:10]} (-j {jobs})")
            run(["cmake", "--build", str(build), "--target", "kvevaluator", "-j", str(jobs)])
            bases[commit] = hashlib.sha256(base.read_bytes()).hexdigest()
            bases_file.write_text(json.dumps(bases, sort_keys=True, indent=1) + "\n")
        dry = subprocess.run(["patch", "-p1", "--batch", "--dry-run", "-d", str(src), "-i", str(patch)],
                             capture_output=True, text=True)
        if dry.returncode != 0:
            sys.exit(f"[build-cpp] {patch.name} does not apply at {commit[:10]}:\n{dry.stdout}{dry.stderr}")
        if re.search("fuzz", dry.stdout, re.I):   # it matched other context than it was cut against: the control is VOID
            sys.exit(f"[build-cpp] {patch.name} applies only with fuzz at {commit[:10]}: re-cut it\n{dry.stdout}")
        applied = run(["patch", "-p1", "--batch", "--no-backup-if-mismatch", "-d", str(src), "-i", str(patch)],
                      capture_output=True, text=True, show_output=True).stdout
        print(applied, end="")
        try:
            log(f"control {patch.name} at {commit[:10]}: building in {name} (-j {jobs})")
            r = subprocess.run(["cmake", "--build", str(build), "--target", "kvevaluator", "-j", str(jobs)])
            if r.returncode == 0:
                got = hashlib.sha256(base.read_bytes()).hexdigest()
                if got == bases[commit]:
                    sys.exit(f"[build-cpp] REFUSED: the control {patch.name} built the unpatched binary "
                             f"(sha256 {got[:16]}): the patch did not reach the build")
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(base, out.with_name(".kvevaluator.tmp"))
                os.replace(out.with_name(".kvevaluator.tmp"), out)
        finally:
            run(["patch", "-R", "-p1", "--batch", "--no-backup-if-mismatch", "-d", str(src), "-i", str(patch)],
                capture_output=True, text=True, show_output=True)
            if source_manifest(src) != want:
                sys.exit(f"[build-cpp] REFUSED: after reversing {patch.name}, {src} differs from its archive: "
                         f"the next control would build on it; list {entry} for removal")
        if r.returncode != 0:
            sys.exit(f"[build-cpp] FAILED: the control {patch.name} did not build (rc {r.returncode})")
        (out.parent / "DONE").write_text(json.dumps({"patch": patch.name, "patch_sha256": psha, "commit": commit,
                                                     "sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
                                                     "base_sha256": bases[commit], "patch_output": applied,
                                                     "wall_s": round(time.time() - t0, 1)},
                                                    sort_keys=True, indent=1) + "\n")
        touch(entry / "LAST_USED")
        log(f"control {patch.name}: built in {time.time() - t0:.0f} s -> {out}")
    return out


def build_in_place(cache, cmake_args, jobs, why):
    """the old build, in <build dir>/cpp, incremental; it links the shared RocksDB when there is a cache"""
    b = TREE_BIN.parent
    log(f"building in place in {b} ({why})")
    extra = []
    if cache is not None:
        tc = toolchain((ROOT / CPP / "CMakeLists.txt").read_text())
        extra = [f"-DKVEVAL_ROCKSDB_DIR={rocksdb_dir(cache, ROOT, cmake_args, tc, jobs)}"]
    # the stamp comes from git at every build (gen_build_info.cmake): no commit is named here
    run(["cmake", "-S", str(ROOT / CPP), "-B", str(b), "-DKVEVAL_BUILD_COMMIT=", "-DKVEVAL_BUILD_DIRTY=",
         "-DKVEVAL_BUILD_KEY=", f"-DKVEVAL_ROCKSDB_JOBS={jobs}", *extra, *cmake_args], stdout=subprocess.DEVNULL)
    run(["cmake", "--build", str(b), "-j", str(jobs)])


def resolved_root(binary):
    out = subprocess.run([str(binary), "schema-path", "resolve", LDBC_SCHEMA], capture_output=True, text=True)
    try:
        return Path(json.loads(out.stdout)["root"]).resolve()
    except (ValueError, KeyError):
        return None


def link_into_tree(binary, target):
    """hard-link the binary to `target` (a copy across filesystems) once the link resolves this tree as its root"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}")
    try:
        os.link(binary, tmp)
        how = "hard link"
    except OSError:
        shutil.copy2(binary, tmp)
        how = "copy (another filesystem)"
    root = resolved_root(tmp)
    if root != ROOT.resolve():
        os.unlink(tmp)   # our own temporary link, never linked into the tree
        sys.exit(f"[build-cpp] REFUSED: {binary} linked at {target} resolves its root as {root}, not {ROOT}")
    os.replace(tmp, target)
    log(f"{target} -> {binary} ({how}; its root is this tree)")


def entry_build_dir(binary):
    """the build dir holding `binary`'s unit tests: its cache entry's (its --version build_key), or, for a binary built
    in place (no build_key), the dir it sits in"""
    if not Path(binary).exists():
        sys.exit(f"[build-cpp] no binary at {binary}: build it (just build)")
    v = json.loads(run([str(binary), "--version"], capture_output=True, text=True).stdout)
    key, cache = v.get("build_key", ""), build_cache_dir()
    if not key:
        return Path(binary).resolve().parent, False
    if cache is None:
        sys.exit(f"[build-cpp] {binary} names the cache entry {key}, but this tree has no build cache (no git)")
    return cache / "by-tree" / key / "src" / "build" / "cpp", True


def tree_size(p):
    """the bytes under `p`, each inode once (a commit's binary is a hard link to its build's)"""
    seen = {}
    for f in p.rglob("*"):
        if f.is_file() and not f.is_symlink():
            st = f.stat()
            seen[(st.st_dev, st.st_ino)] = st.st_size
    return sum(seen.values())


def list_cache(cache):
    rows = []
    for d in sorted((cache / "by-tree").glob("*")) + sorted((cache / "rocksdb").glob("*")):
        if d.is_dir():
            used = (d / "LAST_USED").read_text().strip() if (d / "LAST_USED").exists() else "-"
            commits = sorted(p.name[3:] for p in (d / "src" / "build").glob("at-*")) if (d / "src" / "build").is_dir() else []
            rows.append((d.relative_to(cache), tree_size(d) / 1e9, used, ",".join(commits) or "-"))
    print(f"{'entry':60} {'GB':>6}  {'last used':20}  commits")
    for name, gb, used, commits in rows:
        print(f"{str(name):60} {gb:6.2f}  {used:20}  {commits}")
    print(f"{len(rows)} entries, {sum(r[1] for r in rows):.1f} GB in {cache}")


def last_used(entry):
    """the entry's LAST_USED stamp as a UTC epoch (0 when it has none: oldest first)"""
    try:
        return calendar.timegm(time.strptime((entry / "LAST_USED").read_text().strip(), "%Y-%m-%dT%H:%M:%SZ"))
    except (OSError, ValueError):
        return 0


def linked_outside(entry):
    """a file of `entry` that is hard-linked from outside it (a tree's build/cpp/kvevaluator: a worktree uses it), or
    None. A link inside the entry (a commit's binary and its build's) does not count."""
    inside, nlink = {}, {}
    for f in entry.rglob("*"):
        if f.is_file() and not f.is_symlink():
            st = f.stat()
            inside[(st.st_dev, st.st_ino)] = inside.get((st.st_dev, st.st_ino), 0) + 1
            nlink[(st.st_dev, st.st_ino)] = (st.st_nlink, f)
    for ino, n in inside.items():
        if nlink[ino][0] > n:
            return nlink[ino][1]
    return None


@contextmanager
def held_or_none(path):
    """the lock at `path` held by this process, or None when another process holds it (never waits)"""
    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        yield f


def eviction_list(cache, keep, days, now=None):
    """the by-tree entries (controls entries too) to remove: beyond the `keep` most recently used AND unused for `days`
    days, none linked from a tree or locked by a build. Returns [(entry, why kept or None)] for every entry past
    `keep`, oldest last; the rocksdb/ builds are never listed."""
    now = time.time() if now is None else now
    entries = sorted((d for d in (cache / "by-tree").glob("*") if d.is_dir()), key=lambda d: (-last_used(d), d.name))
    out = []
    for d in entries[keep:]:
        why = None
        if now - last_used(d) < days * 86400:
            why = f"used within {days} days"
        elif (f := linked_outside(d)) is not None:
            why = f"linked from outside: {f.relative_to(d)} has {f.stat().st_nlink} links"
        else:
            with held_or_none(cache / "by-tree" / f"{d.name}.lock") as held:
                if held is None:
                    why = "its lock is held (a build)"
        out.append((d, why))
    return out


def evict(cache, keep, days, remove):
    """print the eviction list (path, GB, last use, commits); with `remove`, remove each listed entry under its lock,
    checking again under the lock, and print the GB freed"""
    rows = eviction_list(cache, keep, days)
    freed = 0.0
    print(f"{'entry':60} {'GB':>6}  {'last used':20}  {'commits':28} verdict")
    for d, why in rows:
        gb = tree_size(d) / 1e9
        used = (d / "LAST_USED").read_text().strip() if (d / "LAST_USED").exists() else "-"
        commits = ",".join(sorted(p.name[3:] for p in (d / "src" / "build").glob("at-*"))) or "-"
        verdict = f"kept: {why}" if why else "evict"
        if remove and not why:
            with held_or_none(cache / "by-tree" / f"{d.name}.lock") as held:
                if held is None:
                    verdict = "kept: its lock is held (a build started)"
                elif time.time() - last_used(d) < days * 86400:
                    verdict = "kept: used since the list"
                elif (f := linked_outside(d)) is not None:
                    verdict = f"kept: linked from outside since the list ({f.relative_to(d)})"
                else:
                    shutil.rmtree(d)
                    freed += gb
                    verdict = "removed"
        print(f"{str(d.relative_to(cache)):60} {gb:6.2f}  {used:20}  {commits:28} {verdict}")
    n = sum(1 for _, why in rows if not why)
    print(f"{n} of {len(rows)} entries past the {keep} most recent to evict (unused {days} days, unlinked, unlocked)"
          + (f"; {freed:.1f} GB freed" if remove else "; `--evict` removes them (the removal queue's owner only)"))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("--sha", help="build this commit's binary in the cache (no link)")
    ap.add_argument("--patch", help="a control: apply this patch to the archive (with --sha)")
    ap.add_argument("--entry", nargs="?", const="", metavar="BINARY", help="print the build dir of BINARY's tests")
    ap.add_argument("--ctest", nargs="?", const="", metavar="BINARY", help="run the unit tests of BINARY's build")
    ap.add_argument("--list", action="store_true", help="list the cache's entries")
    ap.add_argument("--evict-list", action="store_true",
                    help="list the by-tree entries to remove: past the --keep most recent, unused --days, unlinked, unlocked")
    ap.add_argument("--evict", action="store_true", help="remove what --evict-list lists (the removal queue's owner only)")
    ap.add_argument("--keep", type=int, default=6, help="the most recently used entries never evicted (default 6)")
    ap.add_argument("--days", type=float, default=2, help="an entry used within this many days is kept (default 2)")
    ap.add_argument("--in-place", action="store_true", help="build in <build dir>/cpp even when the tree is clean")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4,
                    help="parallel compile jobs, for RocksDB's make and the evaluator (default every core; about 0.5 GB each)")
    ap.add_argument("extra", nargs="*", help="after --: CMake arguments (a build) or ctest arguments (--ctest)")
    a = ap.parse_args()
    cache = build_cache_dir()
    if a.list:
        return list_cache(cache) if cache else print("no build cache (not a git checkout)")
    if a.evict_list or a.evict:
        return evict(cache, a.keep, a.days, remove=a.evict) if cache else print("no build cache (not a git checkout)")
    if a.entry is not None:
        print(entry_build_dir(Path(a.entry) if a.entry else cpp_bin())[0])
        return
    if a.ctest is not None:
        d, cached = entry_build_dir(Path(a.ctest) if a.ctest else cpp_bin())
        # in a cache entry the tests are built (under its lock): -FS skips ctest's own build step there. The run holds
        # the entry's lock too, so a build relinking the entry (build_entry) waits until the tests are done
        cmd = ["ctest", "--test-dir", str(d), "--output-on-failure", *(["-FS", "unit_tests_built"] if cached else []), *a.extra]
        if not cached:
            sys.exit(subprocess.run(cmd).returncode)
        with locked(d.parents[3] / f"{d.parents[2].name}.lock"):   # <cache>/by-tree/<key>.lock, as build_entry takes it
            sys.exit(subprocess.run(cmd).returncode)
    cmake_args = normalize(a.extra)
    if a.patch and not a.sha:
        sys.exit("[build-cpp] --patch needs --sha (a control is a commit plus a patch)")
    if a.sha:
        if cache is None:
            sys.exit("[build-cpp] --sha needs the build cache (a git checkout)")
        commit = git("rev-parse", "--verify", f"{a.sha}^{{commit}}").strip()
        print(build_control(cache, commit, a.patch, cmake_args, a.jobs) if a.patch
              else build_entry(cache, commit, cmake_args, a.jobs))
        return
    if cache is None:
        why = "not a git checkout: no build cache"
    elif a.in_place:
        why = "--in-place"
    elif BUILD_DIR.resolve().parent != ROOT.resolve():
        why = f"the build dir {BUILD_DIR} is not <root>/build: a cached binary would resolve another root"
    elif worktree_dirty(repo_paths((ROOT / CPP / "CMakeLists.txt").read_text())):
        why = "uncommitted changes under the archived paths"
    else:
        why = None
    if why:
        build_in_place(cache, cmake_args, a.jobs, why)
    else:
        link_into_tree(build_entry(cache, git("rev-parse", "--verify", "HEAD").strip(), cmake_args, a.jobs), TREE_BIN)
    version = run([str(TREE_BIN), "--version"], capture_output=True, text=True).stdout
    print(version, end="")                     # the binary's provenance, as it states it
    v = json.loads(version)                    # the pass line, from the same statement (no second source)
    where = TREE_BIN.relative_to(ROOT) if TREE_BIN.is_relative_to(ROOT) else TREE_BIN
    print(f"build OK: {where} (commit {v['binary_git_sha'][:10]}, dirty {str(v['binary_dirty']).lower()})")


if __name__ == "__main__":
    main()
