"""The time a live engine spends in its KV store per query, measured from outside the engine (one definition for
Dgraph/Badger, NebulaGraph/RocksDB, ArangoDB/RocksDB and TuGraph/LMDB), with the engine pinned to one CPU (ENGINES_SETTING=equivalent):

    kv_cpu_ns   on-CPU time inside the KV store: the engine's on-CPU time in the run (sum_exec_runtime of its threads,
                /proc/<pid>/task/<tid>/sched) x the share of perf samples on its CPU that the KV store owns (call stacks:
                frame pointers for Dgraph, Go's own; Intel LBR for the C++ engines, built without frame pointers. LBR
                splices Go stacks across goroutine switches, so it is not used for Go). A sample's owner is the frame nearest its leaf that is either the KV store's (Badger and
                its cache, or rocksdb::) or the engine's own code: so the KV store's callees (decompression, checksums,
                pread, the allocator) count as storage, while an engine callback the store runs (Dgraph's decode inside
                Badger's Item.Value) counts as the engine. A key comparator the store calls (ArangoDB's) is storage.
                Samples neither owns (the Go runtime's own threads, the kernel's idle) are neither.
    io_wait_ns  time the engine's threads were blocked on a disk read in the run (iowait_sum: io_schedule, so direct
                reads and mmap page faults both count; needs kernel.sched_schedstats=1)

A second count is kept raw beside it, perf_kv_any_samples: samples with a KV-store frame anywhere on the stack. The two
differ only where the store runs engine code as a callback (Dgraph's posting decode inside Badger's Item.Value).

What marks a sample as storage, per engine (KV_FRAME, STORE_PLUGIN); the engine's own code (ENGINE_FRAME):
    dgraph    github.com/dgraph-io/badger, github.com/dgraph-io/ristretto (Badger's block cache),  | github.com/hypermodeinc/dgraph
              github.com/klauspost/compress (Badger's block codec)
    nebula    rocksdb:: (its prefix extractor is RocksDB's own)                                    | nebula::
    arangodb  rocksdb::, and arangodb::RocksDB*Comparator / *PrefixExtractor (the store calls them) | arangodb::
    tugraph   mdb_*, midl_* (LMDB, in liblgraph.so), and lgraph::_detail::*CompareFunc              | lgraph::, cypher::,
              (its key comparators, which the store calls)                                          | fma_common::, geax::
TuGraph reads through LMDB's memory map: LMDB returns a pointer, so the page faults of a value's bytes happen in
TuGraph's code when it reads them. Their disk wait is io_wait_ns (the KV store's time); their CPU is the engine's.
NebulaGraph's RocksEngine and iterator wrappers and ArangoDB's TimedRocksDBIterator are engine code: they count only
through the rocksdb:: frames below them.

The engines' own nanosecond timers (kvstats storage_ns) are the cross-check. Needs passwordless sudo for perf.
"""
import os
import re
import signal
import subprocess
import time
from pathlib import Path

PROCESSES = {"dgraph": ("dgraph",), "nebula": ("nebula-metad", "nebula-graphd", "nebula-storaged"),
             "arangodb": ("arangod",), "tugraph": ("lgraph_server",)}
# Dgraph: klauspost/compress is Badger's block codec on a query path (Dgraph's own code calls it only in backup and
# restore). Its assembly decoder breaks the frame-pointer chain, so its stacks end at s2.Decode, with no Badger frame.
KV_FRAME = {"dgraph": re.compile(r"github\.com/(dgraph-io/(badger|ristretto)|klauspost/compress)"), "nebula": re.compile(r"rocksdb::"),
            "arangodb": re.compile(r"rocksdb::"),
            # LMDB is C, compiled into TuGraph's liblgraph.so: its functions are mdb_* and midl_* (a symbol's start)
            "tugraph": re.compile(r"^(mdb_|midl_)")}
ENGINE_FRAME = {"dgraph": re.compile(r"github\.com/(hypermodeinc|dgraph-io)/dgraph"), "nebula": re.compile(r"nebula::"),
                "arangodb": re.compile(r"arangodb::"),
                # kvstats_: our counter library's own functions (engines/tugraph/kvstats), between TuGraph and LMDB
                "tugraph": re.compile(r"lgraph::|cypher::|fma_common::|geax::|^kvstats_")}
# engine code the KV store calls (key order, prefixes): ArangoDB's comparators and prefix extractors, TuGraph's key
# comparators (src/core/kv_table_comparators.cpp, lmdb_table.cpp)
STORE_PLUGIN = re.compile(r"arangodb::RocksDB\w*(Comparator|PrefixExtractor)|lgraph::(_detail::\w+CompareFunc|DefaultCompareKey)")
CALL_GRAPH = {"dgraph": "fp", "nebula": "lbr", "arangodb": "lbr", "tugraph": "lbr"}
SCHED_FIELDS = ("se.sum_exec_runtime", "iowait_sum")


def engine_pids(db):
    """the pids of db's server processes (comm prefix match: the Dgraph binary is named dgraph-<mode>)"""
    out = []
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                if Path(f"/proc/{pid}/comm").read_text().strip().startswith(PROCESSES[db]):
                    out.append(int(pid))
            except OSError:
                continue
    return sorted(out)


def thread_sched_ns(pids):
    """{tid: {field: ns}} for every live thread of pids: on-CPU time and block-I/O wait (the file prints ms with 6
    decimals, so ns exact)"""
    out = {}
    for pid in pids:
        for task in Path(f"/proc/{pid}/task").glob("*"):
            try:
                text = (task / "sched").read_text()
            except OSError:
                continue          # a thread that ended
            vals = dict.fromkeys(SCHED_FIELDS, 0)
            for line in text.splitlines():
                key, _, val = line.partition(":")
                key = key.strip()
                if key in vals:
                    whole, _, frac = val.strip().partition(".")
                    vals[key] = int(whole) * 1_000_000 + int((frac + "000000")[:6])
            out[int(task.name)] = vals
    return out


class SchedClock:
    """The engine's on-CPU time and block-I/O wait, summed over every thread it has had since start(). A thread keeps
    its last polled values after it ends, so an engine that retires threads mid-run (ArangoDB's scheduler workers) does
    not lose them; a plain sum over the live threads would drop an ended thread's whole lifetime from the after-sum.
    A poller reads every `interval` s; what an ending thread did after its last poll is lost (a retiring worker has
    been idle first)."""

    def __init__(self, pids, interval=0.1):
        import threading
        self.pids, self.interval, self.last = pids, interval, {}
        self.lock, self.done = threading.Lock(), threading.Event()
        self.poll()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def poll(self):
        now = thread_sched_ns(self.pids)
        with self.lock:
            self.last.update(now)

    def _loop(self):
        while not self.done.wait(self.interval):
            self.poll()

    def read(self):
        """{field: ns} over every thread seen so far, polled now"""
        self.poll()
        with self.lock:
            return {f: sum(v[f] for v in self.last.values()) for f in SCHED_FIELDS}

    def stop(self):
        self.done.set()
        self.thread.join()


def schedstats_on():
    return Path("/proc/sys/kernel/sched_schedstats").read_text().strip() == "1"


class schedstats:
    """with schedstats(want): kernel.sched_schedstats is 1 inside (set with sudo when it is off) and back at its
    earlier value after; want False: nothing touched"""

    def __init__(self, want):
        self.want, self.was_on = want, True

    def __enter__(self):
        if self.want:
            self.was_on = schedstats_on()
            if not self.was_on:
                subprocess.run(["sudo", "-n", "sysctl", "-q", "-w", "kernel.sched_schedstats=1"], check=True)
                print("  kernel.sched_schedstats set to 1 for this run (it was 0; set back after)", flush=True)
        return self

    def __exit__(self, *exc):
        if self.want and not self.was_on:
            subprocess.run(["sudo", "-n", "sysctl", "-q", "-w", "kernel.sched_schedstats=0"], check=False)


_STACKS = {}
NO_STACKS = ("branch stack", "not supported", "PMU Hardware")     # perf's words for a CPU that cannot record a stack kind


def call_stacks_why(db):
    """'' when perf on this CPU records the call stacks db's profile needs (CALL_GRAPH: frame pointers for Dgraph,
    LBR for the C++ engines, which only some CPUs have), else perf's own reason, one line. Probed once per kind.
    A probe that fails for another reason (no perf, no passwordless sudo) stops the run and says which: that is a
    setup to fix (engines/needs.tsv, `just setup-engines`), not a CPU that cannot measure."""
    kind = CALL_GRAPH[db]
    if kind not in _STACKS:
        import shutil
        if shutil.which("perf") is None:
            raise SystemExit("--storage-profile needs perf, and it is not on PATH: just setup-engines (engines/needs.tsv)")
        if subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode != 0:
            raise SystemExit("--storage-profile runs perf with sudo -n, and sudo asks for a password here")
        r = subprocess.run(["sudo", "-n", "perf", "record", "-q", "-e", "cycles", "--call-graph", kind, "-o", "/dev/null",
                            "--", "true"], capture_output=True, text=True)
        lines = [l.strip() for l in (r.stderr or r.stdout).splitlines() if l.strip()]
        said = " ".join(l for l in lines if l != "Error:")[:200] or f"perf record exited {r.returncode}"
        if r.returncode != 0 and not any(w in said for w in NO_STACKS):
            raise SystemExit(f"--storage-profile: perf record --call-graph {kind} failed, and not for a CPU limit: {said}")
        _STACKS[kind] = "" if r.returncode == 0 else said
    return _STACKS[kind]


def call_stacks_work(db):
    return call_stacks_why(db) == ""


class PerfSession:
    """perf record on one CPU with call stacks (CALL_GRAPH) and the monotonic clock, for one cell"""

    def __init__(self, db, cpu, freq, path):
        self.path = Path(path)
        self.proc = subprocess.Popen(
            ["sudo", "-n", "perf", "record", "-q", "-C", str(cpu), "-e", "cycles", "-F", str(freq),
             "--call-graph", CALL_GRAPH[db], "-k", "mono", "-o", str(self.path), "--", "sleep", "86400"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        time.sleep(0.5)                                   # perf is recording before the first run
        if self.proc.poll() is not None:
            raise RuntimeError(f"perf record failed: {self.proc.stderr.read()}")

    def stop(self):
        subprocess.run(["sudo", "-n", "pkill", "-INT", "-f", f"perf record .* -o {self.path} "], check=False)
        try:
            self.proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.proc.send_signal(signal.SIGKILL)
        subprocess.run(["sudo", "-n", "chown", f"{os.getuid()}", str(self.path)], check=True)

    def samples(self, db):
        """[(t_ns, owner, any KV frame on the stack)] for every sample on the CPU"""
        return [(t, owner(db, frames), any(KV_FRAME[db].search(f) for f in frames)) for t, frames in stacks(self.path)]


def stacks(path):
    """[(t_ns, [symbol, leaf first])] from a perf.data file"""
    r = subprocess.run(["perf", "script", "-i", str(path), "-F", "time,ip,sym", "--ns"],
                       capture_output=True, text=True, check=True)
    out, t, frames = [], None, []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")):          # a sample's header line: its time
            if t is not None:
                out.append((t, frames))
            t, frames = int(round(float(line.strip().rstrip(":")) * 1e9)), []
        else:                                          # one frame: address, then symbol
            parts = line.split(None, 1)
            frames.append(parts[1].strip() if len(parts) > 1 else "")
    if t is not None:
        out.append((t, frames))
    return out


def owner(db, frames):
    """kv, engine or other: the frame nearest the leaf that is the KV store's or the engine's decides"""
    for f in frames:
        if KV_FRAME[db].search(f) or STORE_PLUGIN.search(f):
            return "kv"
        if ENGINE_FRAME[db].search(f):
            return "engine"
    return "other"


def attribute(samples, windows):
    """per run window (t0_ns, t1_ns): (samples, the KV store owns, the engine owns, a KV frame anywhere)"""
    res = []
    for t0, t1 in windows:
        n = k = e = a = 0
        for t, who, any_kv in samples:
            if t0 <= t <= t1:
                n += 1
                k += who == "kv"
                e += who == "engine"
                a += any_kv
        res.append((n, k, e, a))
    return res


def fold(path, out):
    """write a perf.data file's stacks folded (root first, frames joined by ';', then a space and the count), gzipped:
    the input any owner rule can be re-derived from, without the file"""
    import collections
    import gzip
    counts = collections.Counter(";".join(reversed(fr)) for _, fr in stacks(path))
    with gzip.open(out, "wt") as f:
        for stack, n in sorted(counts.items()):
            f.write(f"{stack} {n}\n")
    return sum(counts.values()), len(counts)


if __name__ == "__main__":
    # python storage_profile.py <db> <perf.data>...: each file's samples by owner
    # python storage_profile.py fold <perf.data> <out.folded.gz>: the file's stacks folded
    import collections
    import sys
    if sys.argv[1] == "fold":
        n, distinct = fold(sys.argv[2], sys.argv[3])
        print(f"{sys.argv[3]}: {n} samples, {distinct} distinct stacks")
        sys.exit(0)
    db = sys.argv[1]
    for path in sys.argv[2:]:
        st = stacks(path)
        by = collections.Counter(owner(db, fr) for _, fr in st)
        n = len(st) or 1
        print(f"{path}: {len(st)} samples; kv {by['kv'] / n:.3f}, engine {by['engine'] / n:.3f}, other {by['other'] / n:.3f}")
