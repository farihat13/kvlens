#!/usr/bin/env python3
"""Times the exact mixed-layout search step by step, one CSV row per step (owner: experiments/layout_choice; for the
paper's run-time table). Opens no store: it calls the search code the way the driver does, unchanged.

Steps: load the grid capture (once per option set: compose.load); build a workload's mix cost cache (every query priced under
every assignment of its own parts: cost_cache.problems, `--jobs` slices at a time, a fresh build); solve each objective
exactly (exact_search.solve: fewest weighted ops, fewest bytes, fewest ops + bytes / 4 KB, and the fewest ops within B x
Sp-Co's bytes, settings.B_UNIT, for B = 1, 2, 3, 5); price the solved mix (compose.cost_mix; it must equal the solver's total).
Each row carries the machine's load (os.getloadavg) and the count of other running processes at its step's start and end.

    python3 experiments/layout_choice/search_steps.py --sf 3 --grid <grid capture> \\
        --workload w01,w02,w03,w04,w05,w06,w07,i01,i02 [--by-direction] --jobs 1 --out <file.csv>
The mixes are per type over the settings Sp, Co and Ch64k (search-mix's default --settings), per type and direction
with --by-direction (search-mix's word); both: run it twice, with and without --by-direction, into one --out.
Rows are appended to --out; a row already there for (machine, commit, jobs, workload, option set, step, objective) stops
the run before anything is timed.
"""
import argparse
import csv
import math
import os
import resource
import socket
import subprocess
import sys
import time
from kvevaluator.paths import ROOT

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvcost.layout_cost import compose
from kvcost.layout_cost import benchmarks
from kvcost.layout_search import search as cost_cache
from kvcost.layout_search.search import BUDGETS, b_unit_layout, solve, uniform
from kvcost.layout_cost.compose import LAYOUTS, add_pick_by_arg
from kvcost.layout_cost import cost_models as CM

def the_sum(t):
    """ops + bytes / 4,096 of a cost vector (wops_bytes4k at the workload level)"""
    return CM.cost(t, "wops_bytes4k")


OPTION_SETS = {"per type": False, "per type and direction": True}
COLUMNS = ["workload", "workload_name", "option_set", "step", "objective", "wall_s", "cpu_s", "jobs", "step_uses_pool",
           "load1_start", "load1_end", "busy_other_start", "busy_other_end", "nodes", "proven", "parts",
           "settings_per_part", "space_size", "log10_space", "queries", "cost_cache_entries", "total_ops", "total_bytes",
           "fits", "machine", "started_utc", "commit", "dirty", "grid", "grouped"]


def busy_others():
    """processes in state R other than this one and its own children (the cost cache pool's workers)"""
    me, n = os.getpid(), 0
    for p in os.listdir("/proc"):
        if not p.isdigit() or int(p) == me:
            continue
        try:
            with open(f"/proc/{p}/stat") as f:
                s = f.read()
        except OSError:
            continue
        state, ppid = s[s.rindex(")") + 2:].split()[:2]
        n += state == "R" and int(ppid) != me
    return n


class Step:
    """times one step: wall, CPU of this process and its waited-for children, the load and other busy processes"""

    def __enter__(self):
        self.load0, self.busy0 = os.getloadavg()[0], busy_others()
        self.started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.r0 = [resource.getrusage(w) for w in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)]
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.wall = time.perf_counter() - self.t0
        r1 = [resource.getrusage(w) for w in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)]
        self.cpu = sum((b.ru_utime - a.ru_utime) + (b.ru_stime - a.ru_stime) for a, b in zip(self.r0, r1))
        self.load1, self.busy1 = os.getloadavg()[0], busy_others()
        return False

    def cols(self):
        return {"wall_s": round(self.wall, 3), "cpu_s": round(self.cpu, 3), "load1_start": round(self.load0, 2),
                "load1_end": round(self.load1, 2), "busy_other_start": self.busy0, "busy_other_end": self.busy1,
                "started_utc": self.started}


def provenance():
    """(commit, dirty) of the code that runs: git when this is a work tree, else the .source_commit file a copy carries"""
    git = subprocess.run(["git", "-C", EXP, "rev-parse", "HEAD"], capture_output=True, text=True)
    if git.returncode == 0:
        dirty = subprocess.run(["git", "-C", EXP, "status", "--porcelain", "--", "experiments", "python", "inputs"],
                               capture_output=True, text=True).stdout.strip()
        return git.stdout.strip()[:9], bool(dirty)
    with open(os.path.join(EXP, ".source_commit")) as f:          # "<sha> clean", written when the copy was made
        sha, state = f.read().split()
    return sha[:9], state != "clean"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", default="3")
    ap.add_argument("--grid", required=True, help="the grid capture the search reads (a committed one)")
    ap.add_argument("--workload", required=True, help="a comma list of ids, e.g. w01,i01")
    ap.add_argument("--by-direction", action="store_true", help="the mixes per type and direction (search-mix "
                    "--by-direction); default per type. Both: run twice into one --out")
    ap.add_argument("--jobs", type=int, required=True, help="cost cache slices at a time (1 = one process)")
    ap.add_argument("--out", required=True, help="the CSV the rows are appended to")
    add_pick_by_arg(ap)
    a = ap.parse_args()
    composer = compose.Composer(criterion=a.criterion, plan_rule=compose.PlanRule())
    commit, dirty = provenance()
    machine = socket.gethostname()
    sets = ["per type and direction" if a.by_direction else "per type"]      # the rows' option_set names it so
    ws = benchmarks.select("ldbc", a.sf, a.workload)
    done = set()
    if os.path.exists(a.out):
        with open(a.out) as f:
            done = {(r["machine"], r["commit"], r["jobs"], r["workload"], r["option_set"], r["step"], r["objective"])
                    for r in csv.DictReader(f)}
    if any((machine, commit, str(a.jobs), w["id"], s, "cost cache", "") in done for w in ws.values() for s in sets):
        raise SystemExit(f"{a.out} already holds rows of this machine, commit and --jobs for these workloads")
    new = not os.path.exists(a.out)
    out = open(a.out, "a", newline="")
    wr = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\n")
    if new:
        wr.writeheader()
    fixed = {"jobs": a.jobs, "machine": machine, "commit": commit, "dirty": dirty, "grid": a.grid,
             "grouped": False}     # no grouped props options; the column keeps the file's shape

    def emit(row):
        wr.writerow({c: row.get(c, "") for c in COLUMNS})
        out.flush()
        print(f"  {row['workload']:4s} {row['option_set']:24s} {row['step']:8s} {row.get('objective', ''):26s} "
              f"{row['wall_s']:9.2f} s  cpu {row['cpu_s']:9.2f} s  load {row['load1_start']}-{row['load1_end']}"
              + (f"  nodes {row['nodes']:,}" if row.get("nodes") != "" and "nodes" in row else ""), flush=True)

    for oset in sets:
        with Step() as st:
            data = compose.load(a.grid, OPTION_SETS[oset], "ldbc", composer=composer)
        emit({**fixed, **st.cols(), "workload": "all", "option_set": oset, "step": "load", "step_uses_pool": False,
              "queries": len(data)})
        for wid, w in sorted(ws.items(), key=lambda kv: kv[1]["id"]):
            units = compose.workload_units(w, data)
            n, txt = compose.space_of(data, units)
            shape = {"workload": w["id"], "workload_name": wid, "option_set": oset, "parts": len(units),
                     "settings_per_part": txt, "space_size": n, "log10_space": round(math.log10(n), 2), "queries": len(w["items"])}
            with Step() as st:
                pb = cost_cache.problems([(w, data, units, {"sum": the_sum})], a.jobs)[0]
            emit({**fixed, **shape, **st.cols(), "step": "cost cache", "step_uses_pool": a.jobs > 1, "cost_cache_entries": pb.cost_cache_entries})
            ref_bytes = compose.currencies(compose.cost_mix(w, data, b_unit_layout(units)))["bytes"]
            singles = [uniform(units, p, ad) for p in LAYOUTS for ad in LAYOUTS]
            for objective, measure, cap in ([("fewest wops", "ops", None), ("fewest bytes", "bytes", None),
                                             ("fewest ops + bytes / 4 KB", "sum", None)]
                                            + [(f"fewest wops within B = {b:g}", "ops", b * ref_bytes) for b in BUDGETS]):
                info = {}
                with Step() as st:
                    view = pb.under("sum") if measure == "sum" else pb
                    primary = "bytes" if measure == "bytes" else "ops"
                    starts = [view.from_assign(s) for s in singles]
                    fits = [s for s in starts if cap is None or view.total(s)[1] <= cap * (1 + 1e-9)]
                    key = (lambda s: view.total(s)) if primary == "ops" else (lambda s: view.total(s)[::-1])
                    assign, o, b, nodes, _ = solve(view, primary, cap, min(fits, key=key) if fits else None,
                                                   plain=True, info=info)
                emit({**fixed, **shape, **st.cols(), "step": "solve", "objective": objective, "step_uses_pool": False,
                      "nodes": nodes, "proven": "yes" if info.get("proven", True) else "no", "fits": assign is not None})
                if assign is None:
                    continue
                with Step() as st:
                    t = compose.cost_mix(w, data, assign)
                c = compose.currencies(t)
                got = the_sum(t) if measure == "sum" else c[primary]
                want = o if primary == "ops" else b
                if abs(got - want) > 1e-6 * max(1.0, abs(want)):
                    raise SystemExit(f"{wid} {oset} {objective}: the priced mix {got} != the solver's {want}")
                emit({**fixed, **shape, **st.cols(), "step": "compose", "objective": objective, "step_uses_pool": False,
                      "total_ops": round(c["ops"], 3), "total_bytes": round(c["bytes"], 1)})
    out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
