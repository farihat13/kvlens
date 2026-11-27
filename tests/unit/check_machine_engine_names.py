#!/usr/bin/env python3
"""run/machine-for-timing.sh lists a live engine by its process name: dgraph and its builds (dgraph-kvstats, ...), the
Nebula daemons and arangod; not a process that only has such a name inside it. Stand-in processes (a copy of sleep under
each name), the script's `check` action on a stand-in sysctl dir; no engine is touched, no sudo, no binary.

Run:  python3 tests/unit/check_machine_engine_names.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

from kvevaluator.paths import ROOT

results = []


def case(name, ok, detail=""):
    results.append(ok)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail and not ok else ""))


def live_engines(tmp, names):
    """the `live engines [...]` text of the script's check, with a stand-in process running under each of `names`"""
    procs = []
    try:
        for n in names:
            exe = os.path.join(tmp, "bin", n)
            if not os.path.exists(exe):
                shutil.copy("/bin/sleep", exe)
            procs.append(subprocess.Popen([exe, "30"]))
        for p in procs:           # a name is visible to ps only once the exec has happened
            for _ in range(100):
                if subprocess.run(["ps", "-o", "comm=", "-p", str(p.pid)], capture_output=True, text=True).stdout.strip() == os.path.basename(p.args[0])[:15]:
                    break
        env = dict(os.environ, MACHINE_TEST_DIR=os.path.join(tmp, "sys"))
        out = subprocess.run(["bash", os.path.join(ROOT, "run", "machine-for-timing.sh"), "check", "--state", os.path.join(tmp, "state")],
                             capture_output=True, text=True, env=env).stdout
        return re.search(r"live engines \[([^\]]*)\]", out).group(1)
    finally:
        for p in procs:
            p.terminate()
            p.wait()


def main():
    tmp = tempfile.mkdtemp(prefix="machine-engines-")
    os.makedirs(os.path.join(tmp, "bin"))
    os.makedirs(os.path.join(tmp, "sys"))
    for f, v in (("numa_balancing", "0"), ("swappiness", "1"), ("drop_caches", "0")):
        open(os.path.join(tmp, "sys", f), "w").write(v + "\n")
    case("dgraph is listed", live_engines(tmp, ["dgraph"]) == "dgraph x1")
    got = live_engines(tmp, ["dgraph-kvstats", "dgraph-kvstats"])
    case("dgraph-kvstats (a kvstats build, zero and alpha) is listed", got == "dgraph-kvstats x2", got)
    got = live_engines(tmp, ["dgraph-histogram"])
    case("dgraph-histogram (its name is cut to 15 characters by ps) is listed", got == "dgraph-histogra x1", got)
    got = live_engines(tmp, ["nebula-graphd", "arangod"])
    case("nebula-graphd and arangod are listed", got == "arangod x1;nebula-graphd x1", got)
    for ctl in ("kvstats-dgraph", "dgraphviz", "dgraph_helper", "mydgraph"):
        got = live_engines(tmp, [ctl])
        case(f"control: {ctl} is not an engine", got == "", got)
    n_ok = sum(results)
    print(f"check_machine_engine_names {'OK' if n_ok == len(results) else 'FAIL'}: {n_ok}/{len(results)} cases")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
