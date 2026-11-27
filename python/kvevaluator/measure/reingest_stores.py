"""Re-ingest C++ stores from their own manifests, after a key-layout change (a key_codec bump) makes the binary refuse
the stores it built before.

A store's manifest.json records what built it (format, compression, value codec, schema). This moves the old store
aside (a rename on the same filesystem; nothing is removed) and runs `kvevaluator ingest` again with the same options
from the cached record stream, so the rebuilt store differs only by the layout change.

    python3 -m kvevaluator.measure.reingest_stores --sf 0.1 nebula nebula-zstd pertype_csr-c64k ...
    python3 -m kvevaluator.measure.reingest_stores --sf 0.1 --stale        # every store the binary refuses
    python3 -m kvevaluator.measure.reingest_stores --sf 0.1 --stale --to-default-layout   # + every whole store
    python3 -m kvevaluator.measure.reingest_stores --sf 0.1 --all          # every store under sf0.1

--stale opens each store as a run would and re-ingests the ones that fail the key-layout check; --all re-ingests
every one. A rebuilt store keeps its manifest's index + filter layout; --to-default-layout rebuilds into the binary's
default instead, and with --stale also selects the stores of another layout, codec or filter than inputs/contract/
store_config.json gives their names (measure/store_layout.py). A rebuilt store takes the codec its name says: a name with
no codec suffix gets the decided one. A store the
manifest cannot describe is reported and skipped, never guessed.

The old stores go to <store root>/../aside-<YYYYMMDD>/sf<SF>/<name> (--keep-aside-dir: another dir on the same
filesystem). An entry already there is refused, never overwritten. A failed ingest renames the old store back and
moves the partial new dir aside as <name>.failed-<HHMMSS>. The selected stores' size + 10% must fit in the free space
(--ignore-disk runs anyway).
"""
import argparse
import json
import os
import sys
import tempfile


from kvevaluator.capture.store_identity import BUSY, STALE, codec_suffix, manifest_schema_path, name_holds_codec
from kvevaluator.contract import store_build
from kvevaluator.capture.binary import binary_path
from kvevaluator.measure.stores import META_LAYOUT_FILE, store_dir, run_ingest, store_staleness
from kvevaluator.paths import ROOT
from kvevaluator.measure import store_aside, store_layout

_EXP = str(ROOT)

def ingest_command(manifest, out_dir, index_filter_blocks=None):
    """The `kvevaluator ingest` argv that rebuilds this store, or (None, why). index_filter_blocks: the layout to
    build (None: the manifest's own)."""
    fmt = manifest.get("format")
    named = (manifest.get("schema") or {}).get("path")
    if not fmt or not named:
        return None, "manifest names no format / schema path"
    schema = manifest_schema_path(manifest)
    if schema is None:
        return None, f"schema artifact {named} is gone (not in this tree either)"
    # the codec the store's NAME says (store_identity.name_holds_codec): a -<codec> name keeps its codec, a name with
    # none is rebuilt with the decided one (inputs/contract/store_config.json), whatever its manifest held
    comp = manifest.get("compression")
    comp = comp if comp and name_holds_codec(out_dir, comp) else store_build("compression")
    base = out_dir[:-len(codec_suffix(comp))] if codec_suffix(comp) else out_dir
    cmd = [binary_path(), "ingest", "--schema", schema, "--format", fmt, "--out", base,
           "--compression", comp]
    if manifest.get("scale"):                     # keep the store's scale stamp (the per-SF pins select by it)
        cmd += ["--scale", str(manifest["scale"])]
    cmd += ["--index-filter-blocks", index_filter_blocks or store_layout.of_manifest(manifest)]
    if manifest.get("block_size"):               # a store built at another block size (an arm, a control) stays at it
        cmd += ["--block-size", str(manifest["block_size"])]
    vc = manifest.get("value_codec") or {}
    if fmt == "meta":
        # meta's full spec IS its value codec: write it back out as the --spec file
        # (without it a meta store would be rebuilt as meta's default layout under its old name)
        return cmd + ["--spec", "@spec"], None
    for k, flag in (("chunk_target", "--chunk"), ("uids", "--uids"), ("strings", "--strings"),
                    ("layout", "--layout"), ("index_layout", "--index-layout"),
                    ("props_codec", "--props-codec")):
        if k in vc:
            cmd += [flag, str(vc[k])]
    # the arms a store's NAME carries must be rebuilt as what they are: nebula's key order, a far label
    if "key_order" in vc:
        cmd += ["--key-order", str(vc["key_order"])]
    if "far_label" in vc:
        cmd += ["--far-label", "on" if vc["far_label"] else "off"]
    return cmd, None


def reingest(sf, name, aside, dry_run=False, index_filter_blocks=None):
    path = store_dir("ldbc", sf, name)
    mpath = os.path.join(path, "manifest.json")
    if not os.path.exists(mpath):
        print(f"  {name}: no manifest.json -- skipped", file=sys.stderr)
        return False
    manifest = json.load(open(mpath))
    cmd, why = ingest_command(manifest, path, index_filter_blocks)
    if cmd is None:
        print(f"  {name}: {why} -- skipped", file=sys.stderr)
        return False
    spec_file = None
    if "@spec" in cmd:
        spec_file = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(manifest["value_codec"], spec_file)
        spec_file.close()
        cmd[cmd.index("@spec")] = spec_file.name
    print(f"  {name}: {' '.join(os.path.relpath(c, _EXP) if c.startswith(_EXP) else c for c in cmd)}",
          file=sys.stderr)
    if dry_run:
        return True
    # a meta store's layout name (stores.note_meta_layout) lives beside its manifest: the rebuilt store keeps it, or the
    # stores named by layout (store_path's lookup) no longer find it
    layout_file = os.path.join(path, META_LAYOUT_FILE)
    layout = open(layout_file).read() if os.path.isfile(layout_file) else None
    try:
        # the old store moves aside (the batch's space was checked in main); a failed ingest puts it back
        with store_aside.replacing(path, aside, ignore_disk=True):
            run_ingest("ldbc", sf, cmd)              # quiet, but a failed ingest prints its stderr
            if not os.path.exists(os.path.join(path, "manifest.json")):
                raise SystemExit(f"ingest did not produce {path}")
            if layout is not None:
                with open(layout_file, "w") as f:
                    f.write(layout)
    except SystemExit as e:
        raise SystemExit(f"[reingest] {name}: the ingest failed ({e}); the old store is back at {path}")
    finally:
        if spec_file is not None:
            os.unlink(spec_file.name)
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sf", default=os.environ.get("SF", "0.1"))
    ap.add_argument("names", nargs="*", help="store dir names under output/stores/ldbc/sf<SF>/")
    ap.add_argument("--stale", action="store_true", help="every store the binary refuses to open (a key-layout bump)")
    ap.add_argument("--to-default-layout", action="store_true",
                    help="rebuild into the binary's index + filter layout (default: keep the manifest's); with --stale, "
                         "also every store of another layout")
    ap.add_argument("--all", action="store_true", help="every store under the SF")
    ap.add_argument("--dry-run", action="store_true", help="print the ingest commands only")
    ap.add_argument("--keep-aside-dir", default=None,
                    help="where the old stores go (default <store root>/../aside-<YYYYMMDD>/sf<SF>; same filesystem)")
    ap.add_argument("--ignore-disk", action="store_true", help="run even if the free space looks too small")
    a = ap.parse_args(argv)
    root = os.path.abspath(store_dir("ldbc", a.sf, ""))
    names = list(a.names)
    if a.all or a.stale:
        names = sorted(d for d in os.listdir(root)
                       if os.path.exists(os.path.join(root, d, "manifest.json")))
    if a.stale:
        keep, busy = [], []
        for n in names:
            m = json.load(open(os.path.join(root, n, "manifest.json")))
            if not m.get("format"):
                continue
            # the one check (stores.store_staleness), probing the open too: refused (a key-layout bump, a schema or
            # codec mismatch); with --to-default-layout also another index + filter layout. BUSY (a LOCK another
            # process holds, an IO error) is no verdict: never moved aside
            state, why = store_staleness(os.path.join(root, n), layout=a.to_default_layout, probe=True)
            if state == STALE:
                keep.append(n)
            elif state == BUSY:
                busy.append(n)
                print(f"  {n}: BUSY, left as it is ({why})", file=sys.stderr)
        print(f"[reingest] {len(keep)} of {len(names)} stores are stale"
              + (f", {len(busy)} busy (no verdict: re-run when they are free)" if busy else ""), file=sys.stderr)
        names = keep
    if not names:
        sys.exit("nothing to re-ingest (name stores, or --stale / --all)")
    layout = store_layout.ingest_default() if a.to_default_layout else None
    aside = os.path.abspath(a.keep_aside_dir) if a.keep_aside_dir else store_aside.aside_dir_for(os.path.join(root, "x"))
    store_aside.check_aside(aside, root, names)
    store_aside.check_free(root, [os.path.join(root, n) for n in names], a.ignore_disk)
    print(f"[reingest] old stores {'would go' if a.dry_run else 'go'} to {aside}", file=sys.stderr)
    done = sum(1 for n in names if reingest(a.sf, n, aside, a.dry_run, layout))
    print(f"[reingest] {'would rebuild' if a.dry_run else 'rebuilt'} {done} of {len(names)} stores at sf{a.sf}; "
          f"old stores in {aside}; {store_aside.free_bytes(root) / 1e9:.2f} GB free on {root}")


if __name__ == "__main__":
    main()
