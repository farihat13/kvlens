#!/usr/bin/env python3
"""The NUMA binding of the timed servers holds (python/kvevaluator/measure/numa_binding.py): bound, the kernel's NUMA balancing cannot
scan the server (every mapping under an explicit policy), so it takes no hint fault.

  1. A CppServer started with the auto binding runs on one node: its CPUs are that node's, its memory policy prefer:N;
     one started unbound has the default policy.
  2. refuse_scannable refuses a bound server with a mapping under the default policy or a balancing flag, keeps one
     whose mappings are all prefer / bind, and never refuses an unbound run; scannable_servers (cpplatency's mark) names
     the same servers.
  3. cpplatency itself, steady, on the sf0.1 Chunked/Chunked store over the microbench suite, 5 params, a 1 GB cache
     (passes until the hit rate settles, then 10 measured, ~9 s): rc 0, the meta's server_policies (the server's
     numa_maps at its end) hold no scannable mapping, and the meta has no numa_scannable mark. Then the same with a 10 MB cache on
     LDBC: the cache's churn takes thousands of minor faults per second, none of them hint faults; it must pass too.
     Printed beside: per pass the server's minor faults, the system's hint faults and the kernel's lagging NUMA level.

`--control` runs part 3 unbound (--numa-node off): every mapping is under the default policy, which the scanner scans
(sf0.1, a 4-NUMA-node EPYC 9354P: ~35,000 hint faults per scan, a scan every 1-60 s), so the check must fail. On a 1-node machine or with kernel.numa_balancing=0 the check
prints n/a and exits 0.

Opens the sf0.1 meta-props_chunked_adj_chunked store (read-only): run it with no other store-toucher on it. ~1 min.

    python3 tests/gates/check_numa_binding.py [--control]
"""
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from kvevaluator.paths import OUTPUT_DIR, ROOT

_EXP = str(ROOT)
from kvevaluator.capture import binary
from kvevaluator.measure import server, stores, numa_binding

CELL = "props_chunked_adj_chunked"
SPEC = os.path.join(_EXP, "inputs", "specs", "grid", f"{CELL}.json")
MEASURED = 10
fails = 0


def check(cond, what):
    global fails
    print(("ok   " if cond else "FAIL ") + what, flush=True)
    if not cond:
        fails += 1


def node_cpus(node):
    """the CPU ids of a node, from its cpulist (0-15,64-79)"""
    out = set()
    for part in open(f"/sys/devices/system/node/node{node}/cpulist").read().strip().split(","):
        lo, _, hi = part.partition("-")
        out |= set(range(int(lo), int(hi or lo) + 1))
    return out


def server_binding(pid):
    """(the server's allowed CPUs, its mappings' memory policies), as the kernel says, once numactl has exec'd it"""
    t0 = time.time()
    while os.path.basename(os.readlink(f"/proc/{pid}/exe")) != "kvevaluator" and time.time() - t0 < 10:
        time.sleep(0.05)
    cpus = os.sched_getaffinity(pid)
    policies = sorted({ln.split()[1] for ln in open(f"/proc/{pid}/numa_maps") if len(ln.split()) > 1})
    return cpus, policies


def steady(out, workload, cache_mb, numa_node, cell):
    """one cpplatency steady run: (rc, its steady-passes rows, its meta or {})"""
    cmd = [sys.executable, "-m", "kvevaluator.measure.cpplatency", "--sf", "0.1", "--family", workload, "--format", "meta",
           "--spec", os.path.join(_EXP, "inputs", "specs", "grid", f"{cell}.json"), "--arms", f"meta-{cell}", "--nparam", "5", "--warm", "", "--cold", "", "--steady", f"{cache_mb},{MEASURED}",
           "--numa-node", numa_node, "--out", out]
    r = subprocess.run(cmd, cwd=_EXP, capture_output=True, text=True)
    if r.returncode:
        print(r.stdout[-3000:] + r.stderr[-3000:])
    path, mpath = os.path.join(out, "steady-passes.csv"), os.path.join(out, "steps.csv.meta.json")
    passes = list(csv.DictReader(open(path))) if os.path.exists(path) else []
    for p in passes:
        print(f"     pass {p['pass']} ({p['phase']}): {int(p['wall_ns_sum']) / 1e6:.0f} ms, {p['server_minflt']} minor "
              f"faults ({float(p['faults_per_s']):.0f}/s), system hint faults {p['numa_hint_faults']}, server NUMA "
              f"faults level {p['server_numa_faults_level']}")
    return r.returncode, passes, (json.load(open(mpath)) if os.path.exists(mpath) else {})


def main():
    control = "--control" in sys.argv[1:]
    prov = binary.binary_provenance()
    print(f"binary {binary.binary_path()} commit {prov.get('binary_git_sha', '?')[:12]} dirty {prov.get('binary_dirty')}")
    numa = numa_binding.choose("auto")
    if numa["policy"] != "bind":
        print(f"n/a: the auto binding is {numa['policy']} here ({numa['reason']}): no NUMA balancing to stop")
        return 0
    store = stores.store_dir("ldbc", "0.1", f"meta-{CELL}", SPEC)
    if not os.path.isdir(store):
        raise SystemExit(f"{store}: missing (build it: kvcost build-store --dataset ldbc=0.1 {CELL})")
    node = numa["node"]

    # 1. the binding takes, and only when asked
    with server.CppServer(store, "meta", numa=numa) as srv:
        cpus, pol = server_binding(srv.proc.pid)
    check(cpus == node_cpus(node) and pol == [f"prefer:{node}"],
          f"bound server: CPUs {min(cpus)}-{max(cpus)} ({len(cpus)}) = node {node}'s, policies {pol} = ['prefer:{node}']")
    with server.CppServer(store, "meta") as srv:
        cpus, pol = server_binding(srv.proc.pid)
    check(len(cpus) == os.cpu_count() and pol == ["default"], f"unbound server: {len(cpus)} CPUs, policies {pol}")

    # 2. the refusal rule
    def refused(pols, policy):
        try:
            numa_binding.refuse_scannable({"a/steady": pols}, dict(numa, policy=policy), "check")
        except SystemExit:
            return True
        return False
    bound_pols, default_pols = {f"prefer:{node}": 40}, {f"prefer:{node}": 39, "default": 1}
    check(not refused(bound_pols, "bind") and not refused({"bind:0": 40}, "inherited") and refused(default_pols, "bind")
          and refused({"bind=balancing:0": 3}, "bind") and refused(default_pols, "inherited")
          and not refused({"default": 40}, "off"),
          "refuse_scannable: refuses a bound server with a default or balancing mapping, only")
    found = numa_binding.scannable_servers({"a/steady": default_pols, "b/steady": bound_pols}, dict(numa, policy="bind"))
    check(list(found) == ["a/steady"] and not numa_binding.scannable_servers({"a/steady": default_pols}, dict(numa, policy="off")),
          f"scannable_servers (cpplatency's mark, numa_scannable): the bound server with a default mapping only {found}; "
          f"none for an unbound run")

    # 3. the driver, steady, bound (or unbound: the control); then a 10 MB cache's churn, bound
    tmp = tempfile.mkdtemp(prefix="check-numa-binding-", dir=os.path.join(str(OUTPUT_DIR)))
    runs = [("microbench", 1024, CELL)] + ([] if control else [("interactive", 10, "props_condensed_adj_condensed")])
    checked = 0
    for workload, cache_mb, cell in runs:
        what = f"{workload} {cell} {cache_mb} MB {'unbound' if control else 'bound'}"
        rc, passes, meta = steady(os.path.join(tmp, f"{workload}-{cache_mb}"), workload, cache_mb,
                                  "off" if control else "auto", cell)
        check(rc == 0, f"cpplatency steady, {what}: rc {rc}")
        measured = [p for p in passes if p["phase"] == "measured"]
        check(len(measured) == MEASURED, f"{what}: {len(measured)} measured passes of "
                                         f"{int(measured[0]['cells']) if measured else 0} cells (expected {MEASURED})")
        if not control:
            check(meta.get("numa", {}).get("policy") == "bind", f"{what}: the meta records the binding "
                                                                f"{meta.get('numa', {}).get('policy')} node {meta.get('numa', {}).get('node')}")
        pols = meta.get("server_policies") or {}
        scan = {k: numa_binding.scannable_mappings(v) for k, v in pols.items()}
        check(bool(pols) and not any(scan.values()),
              f"{what}: no server mapping the scanner may scan -- expected none, found {scan} (policies {pols})")
        if not control:   # a bound run with a scannable server is marked numa_scannable in its meta (kvevaluator.stale)
            check("numa_scannable" not in meta and all(n["kind"] != "numa_scannable" for n in meta.get("stale", [])),
                  f"{what}: the meta carries no numa_scannable mark")
        print(f"     {what}: over the measured passes, system hint faults "
              f"{sum(int(p['numa_hint_faults']) for p in measured)}, the server's NUMA level at the end "
              f"{measured[-1]['server_numa_faults_level'] if measured else None}")
        print(f"     {what}: minor faults over the measured passes {numa_binding.measured_rates(passes)} per s of plan wall")
        checked += len(measured)
    print(f"{'FAIL' if fails else 'ok'}: {fails} failed; {checked} measured passes checked over {len(runs)} runs"
          f"{' (control: unbound, must fail)' if control else ''}")
    if not fails or control:
        shutil.rmtree(tmp)                   # kept on a failure, for its steady-passes.csv (the control always fails)
    else:
        print(f"     the runs' output is kept in {tmp}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
