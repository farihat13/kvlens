"""A store manifest's schema path resolves in THIS tree (store_identity.manifest_schema_path): relative (what ingest records,
so a copied store resolves; a file moved since resolves through inputs/moved_paths.json), absolute here, absolute into another tree (not rebased), outside any tree, gone. Also reingest's command and grid_capture's spec
check through it. The C++ side (schema/schema_path.h: the form ingest records, the resolution census / size use) runs
the same case table through `kvevaluator schema-path` and must give Python's answer, also for a binary whose build
tree is gone. ~1 s, no store opened."""
import json, os, subprocess, sys, tempfile

from kvevaluator.capture import store_identity
from kvevaluator.datasets import schema_path
from kvevaluator.capture.binary import binary_path, binary_provenance
from kvevaluator.capture.store_identity import _EXP, manifest_schema_path
from kvevaluator.measure import reingest_stores

REL = os.path.relpath(schema_path("ldbc", "label_only"), _EXP)
HERE = os.path.join(_EXP, REL)
OTHER_TREE = os.path.join("/nonexistent-tree", os.path.basename(_EXP), REL)   # another clone's absolute path
OLD_REL = os.path.join("python", "kvevaluator", "benchmarks", "ldbc", "schema_label_only.json")   # the path in older manifests
DATASETS_REL = os.path.join("python", "kvevaluator", "datasets", "ldbc", "schema_label_only.json")   # a later path in older manifests


GONE_ROOT = os.path.join("/nonexistent-build-tree", os.path.basename(_EXP))   # a copied binary's tree, removed


def m(path):
    return {"format": "nebula", "schema": {"name": "label_only", "path": path}}


def cpp(mode, path, root):
    """the binary's answer for one path under an experiment root (`kvevaluator schema-path`)"""
    out = subprocess.run([binary_path(), "schema-path", mode, path, "--root", root], capture_output=True, text=True)
    if out.returncode != 0:
        return f"<rc {out.returncode}: {out.stderr.strip()[:160]}>"
    return json.loads(out.stdout)["path"]


def python_under(root, man):
    """Python's resolver with `root` as its experiment dir (the reference for the gone-tree cases)"""
    keep = store_identity._EXP
    store_identity._EXP = root
    try:
        return manifest_schema_path(man)
    finally:
        store_identity._EXP = keep


def same(a, b):
    return a == b or (isinstance(a, str) and isinstance(b, str) and os.path.realpath(a) == os.path.realpath(b))


def main():
    prov = binary_provenance()
    print(f"[manifest-schema-path] binary {prov['path']} sha {(prov['binary_git_sha'] or '?')[:10]} dirty {prov['binary_dirty']}")
    fails, n = [], 0
    with tempfile.TemporaryDirectory() as tmp:
        outside = os.path.join(tmp, "schema.json")
        with open(outside, "w") as f:
            json.dump({}, f)
        cases = [("relative to the experiment root", m(REL), HERE),
                 ("relative, moved since (inputs/moved_paths.json)", m(OLD_REL), HERE),
                 ("relative, moved twice since: one step to its current place", m(DATASETS_REL), HERE),
                 ("relative, moved and not in the table", m(OLD_REL.replace("label_only", "nope")), None),
                 ("absolute, this tree", m(HERE), HERE),
                 ("absolute, another tree: as recorded, so gone here", m(OTHER_TREE), None),
                 ("absolute, outside any tree, present", m(outside), outside),
                 ("absolute, outside any tree, gone", m(os.path.join(tmp, "gone.json")), None),
                 ("another tree, file absent here too", m(OTHER_TREE.replace(REL, "nope.json")), None),
                 ("no schema block (an old manifest)", {"format": "nebula"}, None)]
        # a copied binary whose build tree is gone: the recorded path when it exists, else none (a loud stop at the
        # reader); never a path resolved against the working dir
        gone = [("gone build tree: relative", m(REL), None),
                ("gone build tree: absolute, this tree's file (the recorded path)", m(HERE), HERE),
                ("gone build tree: absolute, another tree", m(OTHER_TREE), None),
                ("gone build tree: outside any tree, present", m(outside), outside)]
        for what, man, want in cases:
            n += 1
            got = manifest_schema_path(man)
            if got != want:
                fails.append(f"{what}: got {got}, want {want}")
        # the C++ rule on the same table, Python as the reference; the gone-tree rows on both sides
        for root, table in ((_EXP, cases), (GONE_ROOT, gone)):
            for what, man, want in table:
                recorded = (man.get("schema") or {}).get("path")
                if recorded is None:
                    continue                    # no schema block: a C++ reader stops at manifest.at("schema")
                n += 1
                ref = manifest_schema_path(man) if root == _EXP else python_under(root, man)
                got = cpp("resolve", recorded, root)
                if not same(got, ref) or not same(ref, want):
                    fails.append(f"C++ resolve, {what}: C++ {got}, Python {ref}, want {want}")
        # the form ingest records, and that it resolves back here
        for what, given, want in [("a schema in this tree: relative to the experiment root", HERE, REL),
                                  ("a non-canonical path in this tree", os.path.join(_EXP, "python", "..", REL), REL),
                                  ("another tree's schema: absolute", OTHER_TREE, OTHER_TREE),
                                  ("outside any tree: absolute", outside, os.path.realpath(outside))]:
            n += 1
            got = cpp("record", given, _EXP)
            if got != want:
                fails.append(f"C++ record, {what}: got {got}, want {want}")
        n += 1
        back = cpp("resolve", cpp("record", HERE, _EXP), _EXP)
        if not same(back, HERE):
            fails.append(f"C++ record then resolve: got {back}, want {HERE}")
        # the binary's own root is this tree when it is this tree's binary (else every C++ row above used --root)
        own = json.loads(subprocess.run([binary_path(), "schema-path", "resolve", REL], capture_output=True,
                                        text=True).stdout)["root"]
        if os.path.realpath(binary_path()).startswith(os.path.realpath(_EXP) + os.sep):
            n += 1
            if not same(own, _EXP):
                fails.append(f"this tree's binary resolves against {own}, not {_EXP}")
        # reingest builds its command from this tree's file for a copied store (its manifest's relative path)
        n += 1
        cmd, why = reingest_stores.ingest_command(m(REL), os.path.join(tmp, "out"))
        if cmd is None or cmd[cmd.index("--schema") + 1] != HERE:
            fails.append(f"reingest command for a copied store: {cmd or why}")
        # grid_capture's spec check stops loudly on a gone schema (it returned True: fresh, unchecked)
        n += 1
        from kvevaluator.measure import grid_capture
        store = os.path.join(tmp, "store")
        os.makedirs(store)
        with open(os.path.join(store, "manifest.json"), "w") as f:
            json.dump(dict(m(os.path.join(tmp, "gone.json")), index_filter_blocks="partitioned", settled=True), f)
        spec = os.path.join(tmp, "spec.json")
        with open(spec, "w") as f:
            json.dump({"version": 1}, f)
        try:
            grid_capture.store_is_current(store, spec, "linkbench", layout=False)
            fails.append("grid_capture.store_is_current on a gone schema: returned instead of stopping")
        except SystemExit as e:
            if "neither" not in str(e):
                fails.append(f"grid_capture.store_is_current stopped for another reason: {e}")
    for x in fails:
        print("FAIL", x)
    print(f"check_manifest_schema_path: {n} cases, {len(fails)} failed")
    return 1 if fails or n == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
