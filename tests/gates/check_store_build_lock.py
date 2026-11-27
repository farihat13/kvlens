"""One builder per store (store_aside.building): two processes that need one missing store at once ingest it once; the
second waits on the build lock, then finds it built, and both get the same store. A tiny LinkBench store (the
blob-bytes fixture's data) in a temp output dir, each ingest slowed by 2 s so the two overlap. The control replaces the
lock with a no-op and must see two ingests (or a refusal) for the same store. ~10 s; needs the built binary.

    python tests/gates/check_store_build_lock.py
"""
import os
import subprocess
import sys
import tempfile
import time

import check_linkbench_blob_bytes as fixture  # (its LinkBench data writer)

CHILD = r'''
import contextlib, sys, time
from kvevaluator.measure import store_aside, stores
if sys.argv[2] == "control":
    store_aside.building = lambda paths: contextlib.nullcontext()
slow = stores.run_ingest
def run_ingest(*a, **k):
    time.sleep(2)
    return slow(*a, **k)
stores.run_ingest = run_ingest
print("STORE", stores.ensure_store("linkbench", "locktest", "nebula", data=sys.argv[1]))
'''


def two_builders(tmp, mode):
    """(ingests, return codes, store paths) of two processes started together in one fresh output dir"""
    out = os.path.join(tmp, mode, "output")
    env = {**os.environ, "KVEVAL_OUTPUT_DIR": out}
    procs = [subprocess.Popen([sys.executable, "-c", CHILD, os.path.join(tmp, "data"), mode], env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    res = [p.communicate(timeout=300) for p in procs]
    ingests = sum(err.count("[stores] ingesting") for _, err in res)
    stores_got = sorted({ln.split(" ", 1)[1] for o, _ in res for ln in o.splitlines() if ln.startswith("STORE ")})
    waited = sum(err.count("waiting for it") for _, err in res)
    return ingests, [p.returncode for p in procs], stores_got, waited


def main():
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="store-build-lock-") as tmp:
        fixture.write_data(os.path.join(tmp, "data"), "high")
        n, rcs, got, waited = two_builders(tmp, "lock")
        ok = n == 1 and rcs == [0, 0] and len(got) == 1 and waited == 1
        print(f"  {'ok  ' if ok else 'FAIL'} two builders, the lock: {n} ingest(s), rc {rcs}, {len(got)} store path, "
              f"{waited} waited")
        cn, crcs, cgot, _ = two_builders(tmp, "control")
        caught = cn == 2 or crcs != [0, 0]
        print(f"  {'ok  ' if caught else 'FAIL'} control (no lock): {cn} ingest(s), rc {crcs} (want 2 ingests or a "
              f"failure)")
    good = ok and caught
    print(f"check_store_build_lock {'OK' if good else 'FAIL'}: 2 cases ({time.time() - t0:.0f} s)")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
