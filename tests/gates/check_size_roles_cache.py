"""Backstop for the per-role size cache: every cached <store>/sizes.json at sf0.1 must equal a fresh `size --roles` walk.

store_identity.store_roles keys the cache by the manifest's sha256 and kKeyRolesVersion (formats/format.h), bumped by
hand when a key_role / key_label classifier changes. A classifier edit that forgets the bump serves stale tables;
this re-walks each cached store (all of sf0.1: about a minute) and fails on any difference.

  python3 tests/gates/check_size_roles_cache.py [--sf 0.1]
"""
import argparse
import glob
import json
import os
import subprocess
import sys

from kvevaluator.capture.binary import binary_path
from kvevaluator.measure.stores import store_dir
from kvevaluator.capture.store_identity import key_roles_version


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sf", default="0.1")
    a = ap.parse_args(argv)
    version = key_roles_version()
    checked, stale_version, bad = 0, 0, []
    for cache in sorted(glob.glob(os.path.join(store_dir("ldbc", a.sf, "*"), "sizes.json"))):
        store = os.path.dirname(cache)
        with open(cache) as f:
            got = json.load(f)
        if got.get("key_roles_version") != version:
            stale_version += 1          # store_roles recomputes it on next use: not served stale
            continue
        out = subprocess.run([binary_path(), "size", store, "--repeats", "0", "--roles"],
                             capture_output=True, text=True, check=True).stdout
        fresh = json.loads(out.strip().splitlines()[-1])
        checked += 1
        for field in ("roles", "labels"):
            if got.get(field, {}) != fresh.get(field, {}):
                bad.append(f"{os.path.basename(store)}: cached {field} != a fresh walk")
    print(f"[size-roles-cache] sf{a.sf}: {checked} cached tables re-walked at key_roles_version {version}; "
          f"{stale_version} older-version caches skipped (recomputed on use)")
    if bad or not checked:
        print("FAIL:\n  " + ("\n  ".join(bad) if bad else "0 caches checked -- run kvsize.py first")
              + ("\n  (a classifier changed without a kKeyRolesVersion bump in formats/format.h)" if bad else ""))
        return 1
    print("OK: every cached table equals a fresh walk")
    return 0


if __name__ == "__main__":
    sys.exit(main())
