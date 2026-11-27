#!/usr/bin/env python3
"""The meta store cache (measure/stores.py: cpp_store_name, store_path, meta_build_guard): a meta store is named by the hash of
its expanded spec; a store named before (meta-<spec stem>) is found by the spec its manifest records and used in place;
a layout built before in another form is never re-ingested silently. Runs on a temporary store root (KVEVAL_STORE_DIR)
holding manifests only; the binary expands the specs. Also: the spec goes by argument (none: meta refuses). A few seconds.

Run:  python3 tests/unit/check_meta_store_cache.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile

results = []


def case(name, ok, detail=""):
    results.append(ok)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail and not ok else ""))


def fake_store(root, name, canonical, layout=None):
    """a store dir holding a manifest only (store_path and the guard read nothing else)"""
    os.makedirs(os.path.join(root, name))
    with open(os.path.join(root, name, "manifest.json"), "w") as f:
        json.dump({"format": "meta", "value_codec": canonical}, f)
    if layout:
        with open(os.path.join(root, name, "layout.json"), "w") as f:
            json.dump({"layout": layout}, f)


def stderr_of(fn):
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        out = fn()
    return out, buf.getvalue()


def refused(fn):
    try:
        fn()
    except SystemExit as e:
        return str(e)
    return None


def main():
    tmp = tempfile.mkdtemp(prefix="meta-store-cache-")
    os.environ["KVEVAL_STORE_DIR"] = os.path.join(tmp, "stores")
    os.environ.pop("KVEVAL_BUILD_STORE", None)
    from kvevaluator.paths import INPUTS_DIR
    from kvevaluator.measure import stores as B
    grid = os.path.join(str(INPUTS_DIR), "specs", "grid")
    spec_file = os.path.join(grid, "props_split_adj_split.json")
    spec = json.load(open(spec_file))
    canon = B.canonical_meta_spec(spec, B._schema_artifact("canonical"))
    sha = B.meta_spec_sha(canon)

    def name_of(path):
        return B.cpp_store_name("meta", "canonical", None, None, spec=path)

    name = name_of(spec_file)
    case("a meta store is named by its expanded spec's hash", name == f"meta-{sha}", name)
    # the same spec under another file name (short form or expanded) gets the same name
    other = os.path.join(tmp, "same_layout_other_name.json")
    json.dump(canon, open(other, "w"))
    case("the same layout in another file (expanded form) gets the same name", name_of(other) == name)
    root = os.path.join(tmp, "stores", "ldbc", "sf0.1")
    os.makedirs(root)
    case("no store: the new path meta-<sha12>", B.store_dir("ldbc", "0.1", name_of(spec_file)) == os.path.join(root, name))

    fake_store(root, "meta-props_split_adj_split", canon)
    got, said = stderr_of(lambda: B.store_dir("ldbc", "0.1", name_of(spec_file)))
    case("an old store with the same recorded spec is used in place, said once",
         got == os.path.join(root, "meta-props_split_adj_split") and said.count("reusing meta-props_split_adj_split") == 1
         and sha in said, said.strip())
    _, again = stderr_of(lambda: B.store_dir("ldbc", "0.1", name_of(spec_file)))
    case("... and not said again", again == "")
    case("another file of the same layout opens it too",
         B.store_dir("ldbc", "0.1", name_of(other)) == os.path.join(root, "meta-props_split_adj_split"))
    fake_store(root, f"meta-{sha}", canon)
    case("a store named by the hash wins over the old name", B.store_dir("ldbc", "0.1", name_of(spec_file)) == os.path.join(root, f"meta-{sha}"))

    # an edited spec under the old file name: another hash, and the old store of that name is named by the guard
    edited = json.load(open(os.path.join(grid, "props_condensed_adj_condensed.json")))   # another layout's spec
    ed_dir = os.path.join(tmp, "edited")
    os.makedirs(ed_dir)
    ed_file = os.path.join(ed_dir, "props_split_adj_split.json")
    json.dump(edited, open(ed_file, "w"))
    try:
        ed_name = name_of(ed_file)
    except SystemExit as e:
        case("the edited spec is a valid spec", False, str(e))
        return finish()
    ed_canon = B._META_NAMES[ed_name][2]
    case("an edited spec under the same file name gets another name", ed_name != name and ed_name.startswith("meta-"))
    case("... and opens no old store", B.store_dir("ldbc", "0.1", ed_name) == os.path.join(root, ed_name))
    big = os.path.join(tmp, "stores", "ldbc", "sf3")
    fake_store(big, "meta-props_split_adj_split", canon)
    why = refused(lambda: B.meta_build_guard(big, ed_name, "3"))
    case("guard at sf3: refused, naming the old store and the fields that differ",
         bool(why) and "meta-props_split_adj_split holds the layout props_split_adj_split in an older canonical form" in why
         and "vertex_props.group" in why and "kvcost build-store" in why, why or "not refused")
    os.environ["KVEVAL_BUILD_STORE"] = "1"
    _, said = stderr_of(lambda: B.meta_build_guard(big, ed_name, "3"))
    case("guard at sf3 under kvcost build-store: built, said", refused(lambda: None) is None and "older canonical form" in said)
    os.environ.pop("KVEVAL_BUILD_STORE")
    case("guard at a scale tag (n1m): refused like a large scale", bool(refused(lambda: B.meta_build_guard(big, ed_name, "n1m"))))
    B._SAID.clear()
    _, said = stderr_of(lambda: B.meta_build_guard(root, ed_name, "0.1"))
    case("guard at sf0.1: built, said once", "older canonical form" in said and said.count("\n") == 1, said.strip())
    case("guard: no store of that layout name, nothing to say", B.meta_build_guard(os.path.join(tmp, "empty"), ed_name, "3") is None)

    # an arm named meta-<layout name>: the one new store whose layout file names it
    arms = os.path.join(tmp, "stores", "ldbc", "sf1")
    fake_store(arms, "meta-aaaaaaaaaaaa", ed_canon, layout="my_mix")
    case("an arm meta-<layout> resolves through the layout file", B.store_path(arms, "meta-my_mix") == os.path.join(arms, "meta-aaaaaaaaaaaa"))
    fake_store(arms, "meta-bbbbbbbbbbbb", canon, layout="my_mix")
    why = refused(lambda: B.store_path(arms, "meta-my_mix"))
    case("two stores of that layout name: refused, naming both", bool(why) and "meta-aaaaaaaaaaaa" in why and "meta-bbbbbbbbbbbb" in why, why)
    case("an engine format's name is root/name", B.store_path(arms, "dgraph") == os.path.join(arms, "dgraph"))
    os.makedirs(os.path.join(arms, "meta-my_mix"))
    case("an existing dir of that name is itself", B.store_path(arms, "meta-my_mix") == os.path.join(arms, "meta-my_mix"))
    # an arm meta-<mix name> no layout file names (a mix equal to a built layout: check-mix measured it on that store):
    # the store of the spec passed, by its hash; without a spec, root/name as before
    fake_store(arms, name, canon, layout="props_split_adj_split")
    # the same layout as that store, under another file name
    got, said = stderr_of(lambda: B.store_path(arms, "meta-mix_0123456789", other))
    case("a mix no layout names: the store of its spec", got == os.path.join(arms, name) and name in said, (got, said))
    case("... without a spec: root/name, as before",
         B.store_path(arms, "meta-mix_0123456789") == os.path.join(arms, "meta-mix_0123456789"))
    spec_by_argument(B, spec_file)
    return finish()


def spec_by_argument(B, spec_file):
    """the spec goes by argument: without one, meta refuses (its store name, its caps); a CLI's --spec names its source"""
    from kvevaluator.formats.caps import caps_for
    from kvevaluator.datasets.ldbc.schema import LDBC_SCHEMA
    why = refused(lambda: B.cpp_store_name("meta", "canonical", None, None))
    case("a meta store name without a spec: refused", bool(why) and "spec" in why, why)
    why = refused(lambda: caps_for("meta", LDBC_SCHEMA))
    case("meta's caps without a spec: refused", bool(why) and "spec" in why, why)
    case("meta's caps with a spec: a set", bool(caps_for("meta", LDBC_SCHEMA, spec=spec_file)))
    held = os.environ.pop(B.SPEC_ENV, None)
    try:
        got, said = stderr_of(lambda: B.cli_spec(spec_file))
        case("--spec given: used, named as the flag's", got == spec_file and "from --spec" in said, said)
        os.environ[B.SPEC_ENV] = spec_file + ".env"
        got, said = stderr_of(lambda: B.cli_spec(None))
        case(f"no --spec: ${B.SPEC_ENV}, said so", got == spec_file + ".env" and B.SPEC_ENV in said, said)
        os.environ.pop(B.SPEC_ENV)
        case("neither: None", B.cli_spec(None) is None)
    finally:
        os.environ.pop(B.SPEC_ENV, None)
        if held is not None:
            os.environ[B.SPEC_ENV] = held


def finish():
    n_ok = sum(results)
    print(f"check_meta_store_cache {'OK' if results and n_ok == len(results) else 'FAIL'}: {n_ok}/{len(results)} cases")
    return 0 if results and n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
