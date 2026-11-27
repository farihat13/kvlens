"""Bind a timed evaluator server to one NUMA node, and count the page faults the binding is there to stop.

With kernel.numa_balancing=1 the kernel's scanner unmaps a process's pages on a timer; the next touch of each is a
hint fault, charged to whatever plan runs then. `numactl --cpunodebind=N --preferred=N` gives the server a memory policy, and the scanner skips a
process with one. Only the timed drivers bind (cpplatency, cppwlatency, cppmixed): counts do not depend on placement.
"""
import argparse
import glob
import os
import re
import shutil


def add_argument(ap):
    """The drivers' --numa-node flag (default: EVALUATOR_NUMA_NODE, else auto)."""
    ap.add_argument("--numa-node", default=os.environ.get("EVALUATOR_NUMA_NODE", "auto"), metavar="auto|off|N",
                    help="bind the evaluator server to one NUMA node (numactl --cpunodebind=N --preferred=N); auto = "
                         "the node with the most free memory, off on a 1-node machine or with numa_balancing=0 "
                         "(default: EVALUATOR_NUMA_NODE, else auto)")


def _nodes():
    return sorted(int(m.group(1)) for p in glob.glob("/sys/devices/system/node/node*")
                  if (m := re.fullmatch(r".*/node(\d+)", p)))


def _free_mb(node):
    with open(f"/sys/devices/system/node/node{node}/meminfo") as f:
        for line in f:
            if "MemFree:" in line:
                return int(line.split()[-2]) // 1024
    raise SystemExit(f"node{node}: no MemFree in its meminfo")


def numa_balancing():
    try:
        with open("/proc/sys/kernel/numa_balancing") as f:
            return int(f.read().strip())
    except OSError:
        return None


def outer_binding():
    """The binding this process already runs under (an outer numactl or taskset: a shell driver's), or None. The
    server inherits it."""
    cpus = os.sched_getaffinity(0)
    with open("/proc/self/numa_maps") as f:
        policies = sorted({line.split()[1] for line in f if len(line.split()) > 1})
    if len(cpus) < os.cpu_count() or policies != ["default"]:
        return {"cpus": len(cpus), "of_cpus": os.cpu_count(), "memory_policies": policies}
    return None


def choose(arg):
    """The binding of one driver run, from --numa-node: {policy: bind | inherited | off, node, nodes, numa_balancing,
    node_free_mb_at_launch, outer, reason}. Chosen once per run, so every server of the run sits on one node."""
    nodes, bal, outer = _nodes(), numa_balancing(), outer_binding()
    free = {n: _free_mb(n) for n in nodes}
    info = {"nodes": len(nodes), "numa_balancing": bal, "node_free_mb_at_launch": free, "outer": outer}
    if arg == "off":
        return dict(info, policy="off", node=None, reason="--numa-node off")
    if arg == "auto":
        if outer:
            # a shell driver bound this process (a --numa flag): the server inherits that binding,
            # never a second one of ours on another node
            return dict(info, policy="inherited", node=None, reason=f"auto: the driver already runs bound ({outer})")
        if len(nodes) < 2:
            return dict(info, policy="off", node=None, reason=f"auto: {len(nodes)} NUMA node(s)")
        if not bal:
            return dict(info, policy="off", node=None, reason=f"auto: kernel.numa_balancing={bal}")
        node = max(nodes, key=lambda n: (free[n], -n))
        reason = "auto: the node with the most MemFree"
    else:
        try:
            node = int(arg)
        except ValueError:
            raise SystemExit(f"--numa-node {arg!r}: expected auto, off or a node number")
        if node not in nodes:
            raise SystemExit(f"--numa-node {node}: this machine has nodes {nodes}")
        reason = "--numa-node"
    if not shutil.which("numactl"):
        raise SystemExit(f"--numa-node {arg}: numactl is not installed (install it, or pass --numa-node off: the "
                         f"walls then carry the NUMA-balancing faults)")
    return dict(info, policy="bind", node=node, reason=reason)


def launch_prefix(numa):
    """The command prefix of a server under binding `numa` (from choose; None or off = no prefix). numactl execs the
    binary, so the server's pid is the binary's (perf -p attaches to it)."""
    if not numa or numa["policy"] != "bind":
        return []
    return ["numactl", f"--cpunodebind={numa['node']}", f"--preferred={numa['node']}"]


def minor_faults(pid):
    """The process's minor faults so far, every thread's (/proc/<pid>/stat field 10)."""
    with open(f"/proc/{pid}/stat") as f:
        return int(f.read().rsplit(")", 1)[1].split()[7])


def hint_faults():
    """The system's NUMA hint faults so far (/proc/vmstat numa_hint_faults; 0 on a kernel without the counter)."""
    with open("/proc/vmstat") as f:
        for line in f:
            if line.startswith("numa_hint_faults "):
                return int(line.split()[1])
    return 0


def numa_faults_level(pid):
    """The kernel's NUMA fault level over the server's threads (/proc/<pid>/task/*/sched total_numa_faults): a LAGGING,
    decaying level, not a count. The kernel folds a thread's hint faults into it once per full scan cycle of the address
    space (task_numa_placement), halving the old value: sf0.1, that machine, unbound, it read 0 through two passes of 26,132 and
    12,369 hint faults, then 37,720 for nine passes that took 10,000-30,000 more. Recorded for information; the refusal
    reads the policies (scannable_mappings). None when the kernel does not show it."""
    tot, seen = 0, False
    for f in glob.glob(f"/proc/{pid}/task/*/sched"):
        try:
            with open(f) as fh:
                for line in fh:
                    if line.startswith("total_numa_faults"):
                        tot += int(line.split(":")[1])
                        seen = True
        except OSError:                         # a thread that exited between the glob and the open
            pass
    return tot if seen else None


def server_policies(pid):
    """{memory policy: mappings} of the server's mappings (/proc/<pid>/numa_maps, the policy field: prefer:2, bind:0,
    default, ...). Reading it walks the server's pages: once per server, after its last measured request."""
    out = {}
    with open(f"/proc/{pid}/numa_maps") as f:
        for line in f:
            parts = line.split()
            if len(parts) > 1:
                out[parts[1]] = out.get(parts[1], 0) + 1
    return dict(sorted(out.items()))


def scannable_mappings(policies):
    """The policies the NUMA scanner scans: the kernel scans a mapping only under the default policy (its MOF flag;
    mm/mempolicy.c vma_policy_mof), or one set with MPOL_F_NUMA_BALANCING (numa_maps shows '=balancing'). An explicit
    prefer / bind / interleave without it is never scanned, so its pages take no hint fault, wherever they sit."""
    return {pol: n for pol, n in policies.items() if pol == "default" or "balancing" in pol}


def snapshot(pid):
    """(the server's minor faults, the system's hint faults) now: a pass's columns are the difference of two."""
    return minor_faults(pid), hint_faults()


def fault_columns(before, after, wall_ns, pid):
    """The fault columns of a pass between two snapshots: the server's minor faults (every cause: first touch, a cache's
    churn, the allocator, NUMA balancing), their rate per second of `wall_ns`, the system's hint faults over the pass
    (every process's), and the kernel's lagging NUMA fault level of the server at the pass end (numa_faults_level)."""
    minflt = after[0] - before[0]
    return {"server_minflt": minflt, "faults_per_s": round(minflt / (wall_ns / 1e9), 1) if wall_ns else 0.0,
            "numa_hint_faults": after[1] - before[1], "server_numa_faults_level": numa_faults_level(pid)}


def measured_rates(passes):
    """{arm: server minor faults per second of plan wall over its measured passes together} (steady-passes rows)"""
    tot = {}
    for p in passes:
        if p["phase"] == "measured":
            f, w = tot.get(p["arm"], (0, 0))
            tot[p["arm"]] = (f + int(p["server_minflt"]), w + int(p["wall_ns_sum"]))
    return {arm: round(f / (w / 1e9), 1) if w else 0.0 for arm, (f, w) in tot.items()}


def scannable_servers(policies, numa):
    """{server label: its mappings the NUMA scanner may scan} of a BOUND server (bind or inherited): its walls may carry
    hint faults. policies: {server label: server_policies}. Unbound runs: {} (they only record)."""
    if not numa or numa["policy"] == "off":
        return {}
    return {label: scannable_mappings(pol) for label, pol in policies.items() if scannable_mappings(pol)}


def refuse_scannable(policies, numa, what):
    """Refuse a bound server with a mapping the NUMA scanner scans (scannable_servers); a check's form (cpplatency
    records the finding in its meta instead, marked numa_scannable)"""
    bad = scannable_servers(policies, numa)
    if bad:
        raise SystemExit(f"{what}: bound ({numa['policy']}, node {numa['node']}), yet the NUMA scanner may scan these "
                         f"servers' mappings: {bad} (policy: mappings) -- the capture is refused")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="print the binding --numa-node would choose")
    add_argument(ap)
    a = ap.parse_args()
    n = choose(a.numa_node)
    print(n, launch_prefix(n))
