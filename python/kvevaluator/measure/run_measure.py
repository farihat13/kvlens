"""The measuring calls of `kvcost measure` and `kvcost compose`: whole query sets on a set of layouts, into the output
tree's captures/<dataset>/<scale>/ (and composed/<dataset>/<scale>/), each call a module the C++ binary runs under.

A capture is always a whole query set: per store (a layout of a spec set, or a named format), the
read query sets' bodies on the reported params (grid_bodies), on the held-out params when the set picks its plans
there (the manifest's pick_params), the write query set (grid_writes) when the workload reads it; then each spec
set's grid check (tests/gates/check_grid.py). Each call reuses a layout's capture while its key holds (grid_bodies'
and grid_writes' own reuse). At LDBC SF30 the held-out capture goes first, and the SF30 rule reads SF3's capture from
the same tree. One store's calls run one after another; different stores run side by side (run_calls, `jobs` at once).
"""
import concurrent.futures
import contextlib
import csv
import functools
import glob
import json
import multiprocessing
import os
import re
import subprocess
import sys
import threading
import time

from kvevaluator import datasets
from kvevaluator import layouts as L
from kvevaluator import workloads
from kvevaluator.capture.spec_sets import spec_cells
from kvevaluator.output_tree import HELD_OUT, Tree, capture_in, checked_param_set
from kvevaluator.paths import ROOT
from kvevaluator.plan_choice.candidates import failed_run

SF30 = "30"


class Pass:
    """one spec set's call group: its dir, the cells named (None: every spec of the dir), the check's reference cells"""

    def __init__(self, spec_dir, cells, ref=None):
        self.spec_dir, self.cells, self.ref = spec_dir, cells, ref

    def sel(self):
        return ["--spec-dir", self.spec_dir] + (["--cells", ",".join(self.cells)] if self.cells else [])

    def __repr__(self):
        return f"Pass({os.path.relpath(self.spec_dir, str(ROOT))}: {','.join(self.cells) if self.cells else 'all'})"


def passes(names):
    """(named formats, [Pass]) of the layouts named: a cell id or short name (Sp-Co, Co-Ch4k, Ch1k-Ch4k, any chunk size), a
    spec file (full form), a spec dir (every spec in it), or a named format (dgraph, a chunk arm: dgraph-c512k). The grid's
    cells and ChK-ChK at each size go through layouts.cell_runs (the grid's set first, then each size's set); any other
    spec set is a pass of its own"""
    if not names:
        raise SystemExit("name the layouts: a layout (Sp-Co, Co-Ch4k, Ch1k-Ch4k, a cell id), a spec file or a spec dir; "
                         "the paper's seven: Sp-Sp Co-Co Ch1k-Ch1k Ch4k-Ch4k Ch16k-Ch16k Ch64k-Ch64k Ch256k-Ch256k")
    grid_ids, others, formats = [], {}, []
    sized = {os.path.join(L.SPECS, d) for d in ("grid",) + tuple(L.SIZE_DIRS.values())}
    for n in names:
        if re.fullmatch(rf"({'|'.join(L.NAMED + ('pertype_csr',))})(-c\d+k)?", n):   # a named format, or its chunk arm
            formats.append(n)
            continue
        if os.path.isdir(n):
            d = os.path.realpath(n)
            if not spec_cells(d):
                raise SystemExit(f"{n}: no *.json spec in the dir")
            others[d] = None
            continue
        if n.endswith(".json") and os.path.isfile(n):
            p = os.path.realpath(n)
            spec = json.load(open(p))
            if spec != L.expand(spec):
                raise SystemExit(f"{n}: a spec in the short form; the stores are built from the full form (write it with "
                                 f"kvevaluator.layouts.expand, in a dir of its own)")
            lay = L.from_spec(p)
        else:
            try:
                lay = L.resolve(n)
            except (ValueError, KeyError) as e:
                raise SystemExit(f"{n}: not a layout ({e})")
        if lay.named:
            formats.append(lay.named)
            continue
        d = os.path.realpath(os.path.dirname(lay.spec))
        in_runs = d in sized and (d == os.path.join(L.SPECS, "grid") or lay.cell in spec_cells(d))
        if in_runs and d != os.path.join(L.SPECS, "grid") and not re.fullmatch(r"props_chunked_c(\d+)k_adj_chunked_c\1k",
                                                                                lay.cell):
            in_runs = False                     # a sized set's other cells: their own pass (cell_runs files them under grid)
        if in_runs:
            grid_ids += [lay.cell] if lay.cell not in grid_ids else []
        elif others.get(d, []) is not None:
            others[d] = others.get(d, []) + ([lay.cell] if lay.cell not in others.get(d, []) else [])
    out = []
    if grid_ids:
        runs = L.cell_runs(",".join(grid_ids))
        ref = next((c for d, c in runs if d == "grid"), None)   # none: check_grid's own (every grid cell measured)
        for d, cells in runs:                    # a chunk-size set is checked against the grid cells of this run
            whole = sorted(cells) == sorted(spec_cells(os.path.join(L.SPECS, d)))
            out.append(Pass(os.path.realpath(os.path.join(L.SPECS, d)), None if whole else cells, ref))
    out += [Pass(d, cells) for d, cells in others.items()]
    return formats, out


def query_sets(dataset, families):
    """(read query sets, the write query set or None) of a workload's families, each checked to be the dataset's"""
    bad = [f for f in families if workloads.get(f).dataset != dataset]
    if bad:
        raise SystemExit(f"{', '.join(bad)}: not query sets of {dataset}")
    reads = [f for f in families if workloads.get(f).kind == "read"]
    writes = [f for f in families if workloads.get(f).kind == "write"]
    return reads, (writes[0] if writes else None)


def cores():
    """the machine's physical cores (Linux: the distinct physical id and core id pairs of /proc/cpuinfo), else the
    logical CPUs Python counts"""
    try:
        with open("/proc/cpuinfo") as f:
            blocks = [dict(l.split(":", 1) for l in b.splitlines() if ":" in l) for b in f.read().split("\n\n")]
        found = {(b.get("physical id\t"), b["core id\t\t"]) for b in blocks if "core id\t\t" in b}
    except OSError:
        found = ()
    return len(found) or os.cpu_count() or 1


THREADS = 8      # the worker threads of one measuring call when --threads names none


def available_gb():
    """the memory a new process can take now, in GB (Linux: /proc/meminfo's MemAvailable), else None"""
    try:
        with open("/proc/meminfo") as f:
            kb = next(int(l.split()[1]) for l in f if l.startswith("MemAvailable:"))
    except (OSError, StopIteration):
        return None
    return kb / 2**20


def own_gb():
    """the memory this process holds now, in GB (Linux: /proc/self/status's VmRSS), else None"""
    try:
        with open("/proc/self/status") as f:
            kb = next(int(l.split()[1]) for l in f if l.startswith("VmRSS:"))
    except (OSError, StopIteration):
        return None
    return kb / 2**20


def at_once(jobs=None, threads=1, peak_gb=None, peak_of="a store", no_peak="no peak known"):
    """(how many units a command runs at once; why, in a few words): --jobs when given; else the machine's cores over
    the threads of one unit, and no more than the available memory over one unit's peak (peak_gb, said as `peak_of`;
    None: not capped, said as `no_peak`); never below 1. The one place that decides it: every command's --jobs asks
    here (kvcost measure's stores: stores_at_once; the forked processes of kvcost search-mix, compose and predict:
    at_a_time)"""
    if jobs is not None:
        return max(1, jobs), f"{max(1, jobs)} at once: --jobs"
    n = max(1, cores() // threads)
    why = f"{cores()} cores" + (f" / {threads} threads" if threads != 1 else "")
    have = available_gb()
    if peak_gb is None or have is None:
        return n, f"{n} at once: {why}; memory: {no_peak}, not capped (--jobs lowers it)"
    fit = max(1, int(have // peak_gb))
    return min(n, fit), f"{min(n, fit)} at once: {why}; memory allows {fit} ({have:.0f} GB available / {peak_gb:.3g} GB {peak_of})"


class WorkerExit(Exception):
    """a forked worker's SystemExit, carried to the caller with its code or message (a pool hands on no SystemExit)"""


def _in_worker(fn, task):
    """(fn(task), the stale notes made meanwhile), a refusal as WorkerExit: what at_a_time takes back from a worker"""
    from kvevaluator import stale
    before = len(stale.notes())
    try:
        return fn(task), stale.notes()[before:]
    except SystemExit as e:
        raise WorkerExit(e.code) from None


def at_a_time(fn, tasks, jobs, notes=True):
    """[fn(t) for t in tasks], `jobs` of them at a time, each in a forked process (it reads what this process holds:
    set a worker's inputs before the call); jobs <= 1, or one task: in this process. The results in the tasks' order.
    What a worker does beside its result reaches this process as it would from a call made here: a SystemExit is
    raised here with its code or message, the stale notes it made are kept here, in the tasks' order (stale.keep;
    notes=False: not kept, for a caller that does a worker's noted work again in this process), and a worker that
    dies (killed for memory) is an error, never a wait"""
    if jobs <= 1 or len(tasks) <= 1:
        return [fn(t) for t in tasks]
    from kvevaluator import stale
    fork = multiprocessing.get_context("fork")
    with concurrent.futures.ProcessPoolExecutor(min(jobs, len(tasks)), mp_context=fork) as pool:
        try:
            done = list(pool.map(functools.partial(_in_worker, fn), tasks, chunksize=1))
        except WorkerExit as e:
            pool.shutdown(wait=False, cancel_futures=True)      # the tasks not started yet are dropped
            raise SystemExit(e.args[0]) from None
    for _result, made in done if notes else ():
        stale.keep(made)
    return [result for result, _made in done]


def stores_at_once(jobs=None, P=THREADS, dataset=None, scale=None):
    """at_once for stores (or other units of P worker threads) of a dataset at a scale: one unit's peak is the one
    recorded for a store there (its manifest's measure_peak_gb; none recorded: not capped). kvcost measure asks
    here through measure_at_once"""
    peak = datasets.get(dataset).measure_peak_gb.get(str(scale)) if dataset else None
    return at_once(jobs, P, peak, "a store", f"no peak recorded for {dataset} {scale}")


def measure_at_once(jobs=None, threads=None, dataset=None, scale=None, pass_list=(), formats=()):
    """(the stores a measure runs at once, the worker threads of each call, why) for the stores of `pass_list` and
    `formats` on a dataset at a scale. --threads given, or neither flag and no peak recorded for a store there
    (its memory is not known: no more at once than before): stores_at_once, the cores over the threads (THREADS).
    Else every store at once, as many as the cores (one each) and the memory (the recorded peak) allow, or --jobs;
    and each call takes the cores shared out, cores // the stores at once, at most THREADS. One call measured at 8, 4
    and 1 threads took 344, 357 and 486 s on 2,148, 1,192 and 503 CPU-seconds (a store of the join benchmark's 1%
    sample): a call's threads buy little, so the cores go to more stores first"""
    stores = len(formats) + sum(len(ps.cells or spec_cells(ps.spec_dir)) for ps in pass_list)
    recorded = dataset is not None and str(scale) in datasets.get(dataset).measure_peak_gb
    if threads is not None or (jobs is None and not recorded):
        n, why = stores_at_once(jobs, threads or THREADS, dataset, scale)
        return n, threads or THREADS, why
    fit, why = stores_at_once(jobs, 1, dataset, scale)
    n = max(1, min(fit, stores))
    t = max(1, min(THREADS, cores() // n))
    return n, t, f"{n} at once, {t} threads each: {stores} stores; {why.split(': ', 1)[1]}"


def failed_calls(times):
    """run_calls' failed calls in a few words: '<stage> exited <rc>, ...'"""
    return ", ".join(f"{stage} exited {rc}" for stage, _t, rc, _tally in times if rc)


def calls(dataset, scale, reads, write, formats, pass_list, *, root=None, nparam=5, P=8, fresh=False, reuse_stale=False,
          param_set=None, in_sample=False, jobs=1, check_only=False, timeout=None, held_out=True, queries=None):
    """[(stage, argv)]: every call of one measure. The measuring calls first, one store per call: each named format
    (its reads; its writes only for a write query set measured alone), then each layout of each spec set (its reads,
    its held-out reads, its writes); then each spec set's grid check. run_calls runs the stores side by side and the
    checks after them. At SF30 each spec set's missing stores are built before any (grid_bodies builds none there),
    `jobs` at a time. held_out=False: the reported params only (no held-out capture; the check then in-sample);
    timeout: grid_bodies' seconds per run; queries: a comma list, the reads of these queries only (a slice, into
    `root`; no check)"""
    if queries and not root:
        raise SystemExit("--queries: a slice of a query set goes to a tree of its own: KVEVAL_OUTPUT_DIR=<dir>")
    if formats and check_only:
        raise SystemExit("--check-only: named formats have no grid check")
    c = _Calls(scale, reads, write, root=root, nparam=nparam, P=P, fresh=fresh, reuse_stale=reuse_stale,
               param_set=param_set, in_sample=in_sample, timeout=timeout, held_out=held_out, queries=queries)
    tag = lambda ps: f" {os.path.basename(ps.spec_dir)}" if len(pass_list) > 1 else ""
    out = []
    if not check_only:
        if scale == SF30:
            out += [(f"ingest{tag(ps)}", [sys.executable, "-m", "kvevaluator.measure.ensure_stores", "--sf", scale,
                                           "--dataset", dataset, "--jobs", str(jobs)] + ps.sel()) for ps in pass_list]
        for f in formats:
            out += c.stores(["--format", f, "--spec-dir", os.path.realpath(L.GRID)], f" {f}", fmt=True)
        for ps in pass_list:
            for cell in ps.cells or spec_cells(ps.spec_dir):
                out += c.stores(["--spec-dir", ps.spec_dir, "--cells", cell], f" {L.name_of(cell) if L.is_cell(cell) else cell}")
    for ps in pass_list:
        out += c.checks(ps, tag(ps))
    return out


class _Calls:
    """the options of one measure, and the calls they give for a store selection (stores) or a pass's check (checks)"""

    def __init__(self, scale, reads, write, *, root, nparam, P, fresh, reuse_stale, param_set, in_sample, timeout,
                 held_out, queries):
        self.scale, self.reads, self.write, self.nparam, self.in_sample = scale, reads, write, str(nparam), in_sample
        self.queries, self.held_out, self.fresh = queries, held_out, ["--fresh"] if fresh else []
        self.pset = checked_param_set((reads or [write])[0], param_set) if param_set else None
        self.rt = ["--root", os.path.realpath(root)] if root else []
        self.tail = (["--timeout", str(timeout)] if timeout else []) + (["--queries", queries] if queries else [])
        self.common = ["--P", str(P)] + self.fresh + (["--reuse-stale"] if reuse_stale else []) + self.rt
        self.pset_arg = ["--param-set", self.pset] if self.pset else []
        lists = write and self.pset and getattr(workloads.params_module(write), "PARAM_SETS", ())
        self.wset = ["--param-set", self.pset] if lists else []   # the writes take the reads' set when their workload lists sets

    def held_of(self, w):
        """True when query set w has a held-out capture here (a non-default param set has no held-out rows)"""
        return self.held_out and workloads.get(w).pick_params == HELD_OUT and self.pset is None

    def stores(self, sel, tag, fmt=False):
        """the stores of `sel`: reads, held-out (first at SF30: the SF30 rule decides on its rows), writes (a named
        format's only for a write query set measured alone)"""
        out, bodies = [], [sys.executable, "-m", "kvevaluator.measure.grid_bodies", "--sf", self.scale, "--family"]
        for w in self.reads:
            held = [(f"held-out {w}{tag}", bodies + [w] + self.common + sel + ["--held-out"] + self.tail)] \
                if self.held_of(w) else []
            out += held if self.scale == SF30 else []
            out.append((f"reads {w}{tag}", bodies + [w] + self.common + ["--nparam", self.nparam] + sel + self.pset_arg
                        + self.tail))
            out += held if self.scale != SF30 else []
        if self.write and not (fmt and self.reads):
            out.append((f"writes{tag}", [sys.executable, "-m", "kvevaluator.measure.grid_writes", "--sf", self.scale,
                                         "--family", self.write, "--nparam", self.nparam] + sel + self.fresh + self.rt
                        + self.wset))
        return out

    def checks(self, ps, tag):
        """a pass's grid check per read query set, in-sample where the set picks in-sample (a slice: no check)"""
        return [(f"check {w}{tag}", [sys.executable, "tests/gates/check_grid.py", "--sf", self.scale, "--family", w,
                                     "--nparam", self.nparam] + ps.sel()
                 + (["--in-sample"] if self.in_sample or not self.held_of(w) else []) + self.pset_arg + self.rt
                 + (["--grid-cells", ",".join(ps.ref)] if ps.ref else []))
                for w in (self.reads if not self.queries else [])]


def capture_units(cs):
    """[(capture dir, unit)]: each store the grid_bodies and grid_writes calls of `cs` write a record of, in its capture
    dir; the unit is the record's own name (a cell id, a named format's store dir), as the tools fold notes by it"""
    from kvevaluator.measure.grid_capture import format_store
    out = []
    for _stage, argv in cs:
        tool = next((t for t in ("grid_bodies", "grid_writes") if f"kvevaluator.measure.{t}" in argv), None)
        if tool is None:
            continue
        opt = lambda k: argv[argv.index(k) + 1] if k in argv else None
        w, sf = opt("--family"), opt("--sf")
        d = Tree(opt("--root")).capture(w, sf, HELD_OUT if "--held-out" in argv else opt("--param-set")).path
        if opt("--format"):
            read = w if workloads.get(w).kind == "read" else workloads.names(kind="read", dataset=workloads.get(w).dataset)[0]
            units = [os.path.basename(format_store(sf, read, f, build=False)) for f in opt("--format").split(",")]
        else:
            units = opt("--cells").split(",") if opt("--cells") else spec_cells(opt("--spec-dir"))
        out += [(d, u) for u in units if (d, u) not in out]
    return out


COUNT_PATHS = ("cpp/kvevaluator", "python/kvevaluator", "python/kvcost", "experiments/meta", ":(exclude)*.md")   # what decides a count


def binary_notes(binary_version):
    """[(kind, detail)]: the stale notes of a measure's binary against this tree, every measuring driver's rule in one
    place: binary_unclean when the binary is dirty or unstamped, when the count code has
    uncommitted changes, or when its C++ build inputs differ from HEAD's (cpp_diff_files); binary_behind_head when HEAD
    changed the count code since the binary's commit, or the commit is not HEAD's ancestor. A note warns and marks; it
    never refuses"""
    git = lambda *x: subprocess.run(["git", *x], cwd=str(ROOT), capture_output=True, text=True)
    sha, dirty = binary_version.get("binary_git_sha") or "", binary_version.get("binary_dirty")
    out = []
    if dirty is not False:
        out.append(("binary_unclean", f"the binary {sha[:10]} is dirty, or its build had no git to say (binary_dirty={dirty})"))
    wt = git("status", "--porcelain", "--", *COUNT_PATHS).stdout.strip()
    if wt:
        out.append(("binary_unclean", f"uncommitted changes under the count code ({' '.join(COUNT_PATHS)}), git status: "
                                      + "; ".join(wt.splitlines())))
    if sha and sha not in ("unknown", "HEAD"):        # the binary's C++ build inputs against HEAD's (binary_cpp_verdict)
        from kvevaluator.capture.binary import binary_cpp_verdict, build_input_paths
        ok, msg = binary_cpp_verdict(sha, allow="")
        if not ok:
            r = git("diff", "--name-only", sha, "HEAD", "--", *build_input_paths(), ":(exclude,glob)**/*.md")
            out.append(("binary_unclean", f"binary {sha[:10]}: its C++ build inputs differ from HEAD's; cpp_diff_files "
                                          f"{' '.join(r.stdout.split())}" if r.returncode == 0 else
                                          msg.removeprefix("REFUSED: ").split(". Rebuild")[0]))
    head = git("rev-parse", "HEAD").stdout.strip()
    if sha and sha != head:
        if git("merge-base", "--is-ancestor", sha, "HEAD").returncode != 0:
            out.append(("binary_behind_head", f"the binary {sha[:10]} is not an ancestor of HEAD {head[:10]}"))
        else:
            moved = git("diff", "--name-only", sha, "HEAD", "--", *COUNT_PATHS).stdout.split()
            if moved:
                out.append(("binary_behind_head", f"since the binary {sha[:10]}, HEAD {head[:10]} changed code that "
                                                  f"decides counts: {' '.join(moved)}"))
    return out


def mark_captures(cs, notes):
    """at a measure's start, for each store its calls write a record of: an earlier run's notes of that store moved
    aside (stale.start), then this run's `notes`, which the store's record folds in (its own stale-<unit>.jsonl, so
    measures of other stores of the same capture, side by side, neither move nor mark each other's)"""
    from kvevaluator import stale
    for d, unit in capture_units(cs):
        os.makedirs(d, exist_ok=True)
        stale.start(d, unit)
        for kind, detail in notes:
            stale.note_into(d, kind, detail, unit=unit)


def _counts_files(call):
    """{unit: its counts file's (inode, size, mtime_ns), or None when there is none} of the stores a measuring call
    writes (capture_units); a reused capture's counts file is left as it was, a measured one is written new"""
    out = {}
    for d, unit in capture_units([call]):
        try:
            st = os.stat(os.path.join(d, "counts", f"{unit}.csv"))
            out[unit] = (st.st_ino, st.st_size, st.st_mtime_ns)
        except FileNotFoundError:
            out[unit] = None
    return out


def _nothing_measured(call, unit):
    """a store's capture the call wrote again with every run taken from its prior capture (its record's reused_runs:
    measured 0, grid_bodies.reused_record)"""
    for d, u in capture_units([call]):
        if u == unit:
            try:
                with open(os.path.join(d, "counts", f"{unit}.json")) as f:
                    took = json.load(f).get("reused_runs") or {}
            except (FileNotFoundError, ValueError):
                return False
            return bool(took.get("runs")) and took.get("measured") == 0
    return False


PER_QUERY = re.compile(r"^  \S+ \S+ done (\d+)/(\d+), ")     # grid_bodies' line when a query's runs end: <cell> <query> done i/n


class Progress:
    """what the calls running side by side have done, as one line per kind of call: `reads job: Ch64k-Ch64k 47/113 ·
    Ch1k-Ch1k 12/113 · Co-Co done`. A stage is `<kind> [<query set>] <layout>` (calls()); its state is its queries done
    of its count (PER_QUERY), then done or FAILED, said once"""

    def __init__(self):
        self.state, self.said, self.lock = {}, {}, threading.Lock()

    def note(self, stage, line):
        """True when `line` is a per-query line (kept for the stage's state, not for the screen)"""
        m = PER_QUERY.match(line)
        if m:
            with self.lock:
                self.state[stage] = f"{m.group(1)}/{m.group(2)}"
        return bool(m)

    def end(self, stage, rc):
        with self.lock:
            self.state[stage] = "FAILED" if rc else "done"

    def lines(self):
        """the lines to print now: per kind, the stages whose state moved since they were last said"""
        with self.lock:
            moved = [(s, v) for s, v in self.state.items() if self.said.get(s) != v]
            self.said.update(moved)
        by = {}
        for stage, v in moved:
            head, _, item = stage.rpartition(" ")
            by.setdefault(head or stage, []).append(f"{item} {v}" if head else v)
        return [f"{head}: {' · '.join(items)}" for head, items in by.items()]


def _run_call(stage, argv, path, quiet, progress, said, log):
    """one call of run_calls, its lines as it prints them: every line into the log at `path` (when given), and on the
    screen under the lock `said`, but a per-query line when `quiet` (progress keeps it); a failed quiet call's last
    lines are printed with its log's place. -> its exit code"""
    start = f"[kvcost measure] {stage}: {' '.join(argv[1:] if argv[0] == sys.executable else argv)}"
    if not quiet:
        with said:
            log(start)
            sys.stdout.flush()
    last = []
    with (open(path, "w") if path else contextlib.nullcontext()) as out:
        if out:
            out.write(start + "\n")
        with subprocess.Popen(argv, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              errors="replace", env=dict(os.environ, PYTHONUNBUFFERED="1")) as p:
            for line in p.stdout:
                if out:
                    out.write(line)
                    out.flush()
                last = (last + [line])[-30:]
                if progress.note(stage, line) and quiet:
                    continue
                with said:
                    print(f"[{stage}] {line}", end="", flush=True)
    progress.end(stage, p.returncode)
    if p.returncode and quiet:                     # a failed call: its last lines, the per-query ones too
        with said:
            print(f"[kvcost measure] {stage} FAILED (rc {p.returncode}); its last lines ({runs_rel(path)}):\n"
                  + "".join(f"[{stage}] {x}" for x in last), end="", flush=True)
    return p.returncode


def run_calls(cs, jobs=1, log=print, log_dir=None, verbose=False, every=30.0):
    """run the calls from the repo root: a measuring call (one that names a store: capture_units) in its store's lane,
    the lanes `jobs` at once, a store's own calls one after another (one job per store); a call that names no store
    after every call before it has ended: an ingest alone, the checks that follow one another (each reads captures and
    writes nothing) `jobs` at once. Each call's output is printed line by line, with
    its stage in front. With `log_dir` (kvcost measure), every line of a call goes to <log_dir>/<stage>.log, and the
    screen gets no per-query line (stores run side by side, so those interleave): one progress line per kind of call
    about every `every` seconds instead (Progress), each call's other lines, and a failed call's last lines; `verbose`
    prints every line as without it. Once a call fails no other starts, the ones running end, and the code returned is the first
    failed call's in the calls' order. Returns (rc, [(stage, seconds, rc, tally)]) in the calls' order, the calls that ran: a measuring call's
    tally is {"reused": [unit], "measured": [unit], "missing": [unit]} from its stores' counts files before and after
    the call (reused: the file there before and not rewritten, or written again with every run taken from it), else
    None"""
    done, failed, said = {}, [], threading.Lock()
    progress, stop = Progress(), threading.Event()
    quiet = log_dir is not None and not verbose        # the per-query lines to the call's log only

    def say_progress():
        for line in progress.lines():
            with said:
                print(f"[kvcost measure] {line}", flush=True)

    def tick():
        while not stop.wait(every):
            say_progress()

    def one(i):
        stage, argv = cs[i]
        t = time.time()
        before = _counts_files(cs[i])
        path = os.path.join(log_dir, stage.replace(" ", "-") + ".log") if log_dir else None
        rc = _run_call(stage, argv, path, quiet, progress, said, log)
        after = _counts_files(cs[i])
        tally = None
        if before:
            same = {u for u in after if after[u] is not None
                    and (after[u] == before[u] or _nothing_measured(cs[i], u))}
            tally = {"reused": sorted(same),
                     "measured": sorted(u for u in after if after[u] is not None and u not in same),
                     "missing": sorted(u for u in after if after[u] is None)}
        done[i] = (stage, time.time() - t, rc, tally)
        if rc != 0:
            failed.append(rc)

    def lane(ids):
        for i in ids:
            if failed:
                return
            one(i)

    def side_by_side(lanes):
        with concurrent.futures.ThreadPoolExecutor(max(1, jobs)) as pool:
            for job in [pool.submit(lane, ids) for ids in lanes]:
                job.result()

    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    if quiet:
        print(f"[kvcost measure] one progress line per kind of call about every {every:.0f} s; each call's every line in "
              f"{runs_rel(log_dir)}/<call>.log (-v prints them)", flush=True)
    ticker = threading.Thread(target=tick, daemon=True) if quiet else None
    if ticker:
        ticker.start()
    try:
        _run_lanes(cs, lane, side_by_side)
    finally:
        stop.set()
        if ticker:
            ticker.join()
            say_progress()                        # the states not yet said
    times = [done[i] for i in sorted(done)]
    return next((rc for _stage, _t, rc, _tally in times if rc), 0), times


def runs_rel(path):
    """a log's path as said: below the output tree when it is there"""
    root = str(Tree().root)
    return os.path.relpath(path, root) if os.path.abspath(path).startswith(os.path.abspath(root) + os.sep) else path


def _run_lanes(cs, lane, side_by_side):
    """run_calls' order: each store's measuring calls in its lane, the lanes side by side; a call that names no store
    after every call before it, the checks that follow one another side by side"""
    lanes, checks = {}, []                    # store -> its measuring calls, in order; the checks that follow one another
    for i, call in enumerate(cs):
        stores = sorted({unit for _d, unit in capture_units([call])})
        if len(stores) > 1:
            raise SystemExit(f"{call[0]}: a measuring call names one store, not {stores} (run_measure.calls)")
        if stores or not call[0].startswith("check "):
            side_by_side([[c] for c in checks])
            checks = []
        if stores:
            lanes.setdefault(stores[0], []).append(i)
            continue
        side_by_side(lanes.values())
        lanes = {}
        if call[0].startswith("check "):
            checks.append(i)
        else:
            lane([i])
    side_by_side(lanes.values())
    side_by_side([[c] for c in checks])


def say_reuse(times):
    """one line per measuring call of run_calls' times: how many stores' captures it reused and how many it measured,
    and why a capture is reused"""
    out = []
    for stage, _t, _rc, tally in times:
        if tally is None:
            continue
        n = sum(len(v) for v in tally.values())
        line = f"{stage}: {len(tally['reused'])} of {n} reused, {len(tally['measured'])} measured"
        if tally["missing"]:
            line += f", {len(tally['missing'])} with no capture ({', '.join(tally['missing'])})"
        out.append(line)
    if any(tally and tally["reused"] for *_x, tally in times):
        out.append("a reused capture was measured before on the same store with the same params and plans, or "
                   "written again with every run taken from it by its run key; --fresh measures it again")
    return out


def compose_call(dataset, scale, root=None, param_set=None, cells=None, jobs=None):
    """the compose call over the dataset's captures at a scale (compose_cells, every query set of the dataset it holds),
    or (None, why) when a layout asked for (`cells`, cell ids at any chunk size; default every pair of the measured
    diagonals) has a side whose diagonal is not measured, the measure to run named; --check when a layout composed was
    measured itself; `jobs`: the layouts composed at once (None: compose_cells' own default)"""
    tree = Tree(root)
    measured, composed = tree.place_dir("captures", dataset, scale), tree.place_dir("composed", dataset, scale)
    reads = workloads.names(kind="read", dataset=dataset)
    pset = checked_param_set(reads[0], param_set) if param_set else None
    has = lambda c: any(os.path.exists(capture_in(measured, w, pset).record(c)) for w in reads)
    diagonals = [s for s in L.DIAGONALS if has(L.cell_id(s, s))]
    named = cells is not None          # named: those only (the composed place keeps its other layouts); else all, replaced
    if cells is None:
        if not diagonals:
            return None, f"no measured diagonal under {measured} (kvcost measure ... Sp-Sp Co-Co Ch64k-Ch64k)"
        cells = [L.cell_id(p, a) for p in diagonals for a in diagonals]
    sides = sorted({s for c in cells for s in L.cell_settings(c)}, key=lambda s: (s not in L.DIAGONALS, s))
    missing = [L.cell_id(s, s) for s in sides if s not in diagonals]
    if missing:
        names = " ".join(L.name_of(c) for c in missing)
        return None, (f"no measured capture of {', '.join(L.name_of(c) for c in missing)} under {measured}: measure "
                      f"them first (kvcost measure --dataset {dataset}={scale} --workload <w> {names})")
    off = [c for c in cells if L.cell_settings(c)[0] != L.cell_settings(c)[1] and has(c)]
    argv = [sys.executable, "-m", "kvcost.layout_cost.compose_cells", "--grid", str(measured), "--out", str(composed),
            "--dataset", dataset, "--families", ",".join(workloads.names(dataset=dataset))] \
        + (["--cells", ",".join(cells)] if named else []) + (["--param-set", pset] if pset else []) + ["--replace"] + (["--check"] if off else []) \
        + (["--jobs", str(jobs)] if jobs else [])
    return argv, None


# -- the walkthrough's pricing steps read these: a query of a workload file whose runs failed is refused --------------
def joined_root(run):
    """the capture place a run's queries are read from: the tree's captures/<dataset>/<scale>/"""
    return str(Tree().place_dir("captures", run.dataset, run.scale))


def failed_runs(run):
    """{(layout cell, query): (failed runs, the first error)} over the run's queries in the tree's captures: a read run
    that failed (candidates.failed_run: the C++ error is in the counts row's `error` column; a body the store may not
    run is a result, not a failure). A healthy capture has none, so a query
    with a failed run is refused by every later step rather than priced from nothing"""
    out = {}
    root = joined_root(run)
    for fam in run.families():
        if workloads.get(fam).kind != "read":
            continue
        qs = set(run.queries(fam))
        for rows in (("reported", "held-out") if workloads.get(fam).pick_params == HELD_OUT else ("reported",)):
            for path in sorted(glob.glob(os.path.join(root, fam, rows, "counts", "*.csv"))):
                cell = os.path.basename(path)[:-len(".csv")]
                with open(path, newline="") as f:
                    for r in csv.DictReader(f):
                        if r["query"] in qs and failed_run(r):
                            n, err = out.get((cell, r["query"]), (0, r.get("error") or ""))
                            out[(cell, r["query"])] = (n + 1, err)
    return out


def failed_in(counts_files):
    """{(layout cell, query): (failed runs, the first error)} over counts csv files (a read run that failed:
    candidates.failed_run),
    the cell from each file's name"""
    out = {}
    for path in sorted(counts_files):
        if not os.path.exists(path):
            continue
        cell = os.path.basename(path)[:-len(".csv")]
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                if failed_run(r):
                    n, err = out.get((cell, r["query"]), (0, r.get("error") or ""))
                    out[(cell, r["query"])] = (n + 1, err)
    return out


def measured_counts(cs):
    """the counts csv of every store the grid_bodies calls of `cs` write (its reads: the runs that can fail)"""
    return [os.path.join(d, "counts", f"{unit}.csv") for d, unit in capture_units(
        [c for c in cs if "kvevaluator.measure.grid_bodies" in c[1]])]


def say_failed_runs(what, failed, then):
    """the failed runs per query (the stores, the first error each) and what to do; -> the message"""
    by_q = {}
    for (cell, q), (n, err) in sorted(failed.items()):
        by_q.setdefault(q, []).append((cell, n, err))
    lines = [f"{what}: {sum(n for n, _ in failed.values())} runs failed, in {len(by_q)} queries (each counts row's "
             f"`error` column):"]
    for q, items in by_q.items():
        lines.append(f"  {q}: {', '.join(f'{c} {n}' for c, n, _ in items)} failed runs; the first error: "
                     f"{items[0][2][:300]}")
    lines.append(then)
    return "\n".join(lines)


def say_failed(run, failed, then):
    """the failed runs per query, the first error each, and what to do; -> the message for SystemExit"""
    by_q = {}
    for (cell, q), (n, err) in sorted(failed.items()):
        by_q.setdefault(q, []).append((cell, n, err))
    lines = [f"{run.name}: {sum(n for n, _ in failed.values())} runs failed, in {len(by_q)} of the run's queries "
             f"(the `error` column of {joined_root(run)}/<query set>/<params>/counts/<layout>.csv):"]
    for q, items in by_q.items():
        cells = ", ".join(f"{c} {n}" for c, n, _ in items)
        lines.append(f"  {q}: {cells} failed runs; the first error: {items[0][2][:300]}")
    lines.append(then)
    return "\n".join(lines)
