"""The C++ evaluator's counts equal the freeze's golden counts, exactly: 50 read cells (is1, is2, is3, ic2, ic8 x params 1-2 x
dgraph, nebula, arangodb and the meta cells Sp-Co and Ch64k-Ch64k, sf0.1: S, G, N, P, D, the read byte lanes the golden file
lists (the 7 read lanes, and the used bytes summed over the run's steps), the rows and the result hash) and 3 write cells (iu2 param 1 per format, audited against RocksDB). The stores are built on first use
under the output dir (needs the sf0.1 LDBC CSVs: `just fetch-dataset ldbc=0.1`). A planted wrong value must be caught. IS2 is in for its chase hop (NebulaGraph and ArangoDB stop on the first edge, with no NEXT past it).

A count that moves needs a counting-version bump in C++ (measure/counting.py: the seam's in store/counting.h, the
format's pair in formats/registry.cc kFormatCounting): each golden file stores the versions its cells were counted under (`counting`, per
format), and a differing cell whose versions did not move fails as a forgotten bump. After a bump, --update re-records
the cells and versions in the bumping commit; --update refuses a moved count with no bump.

    python tests/gates/check_golden_counts.py                   # build stores if missing, measure, compare (~minutes at sf0.1)
    python tests/gates/check_golden_counts.py --update          # in the commit that bumps a counting version: re-record
    python tests/gates/check_golden_counts.py --plant           # the control: one measured count off by one, no bump: must FAIL
    python tests/gates/check_golden_counts.py --allow-dirty NAME   # a named control: the result is not a gate
"""
import argparse
import copy
import csv
import json
import os
import subprocess
import sys
import time

from kvevaluator.measure import counting
from kvevaluator.capture.binary import binary_dirty_verdict, binary_provenance
from kvevaluator.paths import OUTPUT_DIR, ROOT

GOLDEN = ROOT / "tests" / "fixtures" / "golden"
FIELDS = ("S", "G", "N", "P", "D")
UPDATE_FIELDS = FIELDS + ("write_key_bytes", "write_value_bytes", "rocks_keys_written", "rocks_bytes_written", "rows")


def run(cmd, log, env=None):
    t = time.time()
    with open(log, "w") as fh:
        rc = subprocess.run(cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT, env=env).returncode
    print(f"  rc={rc} {os.path.basename(log)} ({time.time() - t:.0f} s)", flush=True)
    if rc:
        sys.exit(f"check_golden_counts: {' '.join(cmd[1:4])} ... failed (rc {rc}); see {log}")


def measure_reads(g, work):
    """the read cells (stores x queries x params), as golden/counts.json writes them: grid_bodies --picks (the one body the pick record names
    for each store at this scale, as every runner runs it), the named formats in one run and each meta cell in its own"""
    import glob
    cells = []
    runs = [("formats", ["--format", ",".join(g["formats"])])] + [(name, ["--cells", spec])
                                                                  for name, spec in sorted(g["meta_cells"].items())]
    for tag, sel in runs:
        out = os.path.join(work, f"reads-{tag}")
        cmd = [sys.executable, "-m", "kvevaluator.measure.grid_bodies", "--sf", g["sf"], "--nparam", str(g["nparam"]),
               "--queries", ",".join(g["queries"]), "--picks", "--fresh", "--root", out] + sel
        run(cmd, os.path.join(work, f"reads-{tag}.log"))
        files = sorted(glob.glob(os.path.join(out, "**", "counts", "*.csv"), recursive=True))
        if not files:
            sys.exit(f"check_golden_counts: grid_bodies wrote no counts file under {out}")
        for path in files:
            name = os.path.basename(path)[:-len(".csv")]
            store = name if tag == "formats" else f"{tag} ({name})"
            # a lane the run row does not carry (the used bytes: per step only) is the sum over the run's step rows
            from_steps = {}
            for st in csv.DictReader(open(path.replace(os.sep + "counts" + os.sep, os.sep + "steps" + os.sep))):
                acc = from_steps.setdefault((st["query"], st["param_idx"], st["body"]), {})
                for k in g["lanes"]:
                    if k in st:
                        acc[k] = acc.get(k, 0) + int(float(st[k] or 0))
            for r in csv.DictReader(open(path)):
                if r["ok"] not in ("True", "true", "1"):
                    sys.exit(f"check_golden_counts: {store} {r['query']} p{r['param_idx']} failed: {r['error'][:200]}")
                cells.append({"store": store, "query": r["query"], "param": int(r["param_idx"]),
                              **{k: int(float(r.get(k) or 0)) for k in FIELDS},   # a read row has no P, D: 0
                              "lanes": {k: int(float(r[k])) if k in r else from_steps[(r["query"], r["param_idx"], r["body"])][k]
                                        for k in g["lanes"]},
                              "rows": int(r["rows"]), "result_hash": r["result_hash"]})
    return cells


def measure_updates(u, formats, work):
    """the 3 write cells, as golden/updates.json writes them"""
    out = os.path.join(work, "updates.csv")
    run([sys.executable, "tests/gates/check_cpp_write_counts.py", "--sf", u["sf"], "--formats", ",".join(formats),
         "--queries", u["query"], "--nparam", str(u["param"]), "--csv", out], os.path.join(work, "updates.log"))
    cells = []
    for r in csv.DictReader(open(out)):
        cells.append({"format": r["format"], "query": r["query"], "param": int(r["param_idx"]),
                      **{k: int(float(r[k])) for k in UPDATE_FIELDS[:-1]},
                      "rows": int(float(r["rows"])) if r["rows"] != "" else None,
                      "lanes": {k[len("bytes_"):]: int(float(v)) for k, v in r.items() if k.startswith("bytes_") and v != ""}})
    return cells


def compare(want, got, key):
    """[(cell, field, want, got)] over every field of every golden cell; a golden cell not measured, or a measured cell
    the golden file lacks, is one difference"""
    have = {key(c): c for c in got}
    diffs = []
    for w in want:
        g = have.pop(key(w), None)
        if g is None:
            diffs.append((key(w), "(cell)", "present", "not measured"))
            continue
        for f in sorted(set(w) | set(g)):
            if f == "lanes":
                for lane in sorted(set(w[f]) | set(g[f])):
                    if w[f].get(lane) != g[f].get(lane):
                        diffs.append((key(w), f"lanes.{lane}", w[f].get(lane), g[f].get(lane)))
            elif w.get(f) != g.get(f):
                diffs.append((key(w), f, w.get(f), g.get(f)))
    diffs += [(k, "(cell)", "absent", "measured") for k in sorted(have)]
    return diffs


def format_of(cell):
    """the format a golden cell ran on: a write cell names it; a read cell's store is a format or a meta cell ("Sp-Co (...)")"""
    return cell.get("format") or ("meta" if "(" in cell["store"] else cell["store"])


def stamps(cells, parts):
    """{format: the binary's versions on `parts`} for the formats of `cells` (what a golden file stores as `counting`)"""
    return {f: {k: counting.stamp(f)[k] for k in counting.PARTS[parts]} for f in sorted({format_of(c) for c in cells})}


def bumped(golden, parts, fmt):
    """None when the binary's versions for `fmt` equal the ones the golden file stores (a moved count is then a forgotten
    bump), else which moved; a golden file that stores none is refused by name"""
    stored = (golden.get("counting") or {}).get(fmt)
    if stored is None:
        sys.exit(f"check_golden_counts: the golden file stores no counting versions for {fmt!r}: re-record it with --update")
    now = {k: counting.stamp(fmt)[k] for k in counting.PARTS[parts]}
    moved = {k: (stored.get(k), now[k]) for k in now if stored.get(k) != now[k]}
    return moved or None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--allow-dirty", default=None, metavar="NAME",
                    help="run a dirty binary as the named control (not a gate result; binary.binary_dirty_verdict)")
    ap.add_argument("--work", default=str(OUTPUT_DIR / "golden-counts"), help="where the measured CSVs and logs go")
    ap.add_argument("--update", action="store_true", help="re-record the cells and versions (in a commit that bumps one)")
    ap.add_argument("--plant", action="store_true", help="the control: one measured count off by one, no bump (must exit 1)")
    a = ap.parse_args()
    prov = binary_provenance()
    print(f"[golden] binary {prov['path']}: git {prov['binary_git_sha']}, dirty {prov['binary_dirty']}", flush=True)
    ok, msg = binary_dirty_verdict(prov, a.allow_dirty)
    print(f"[golden] {msg}", flush=True)
    if not ok:
        sys.exit(f"check_golden_counts: {msg}")
    os.makedirs(a.work, exist_ok=True)
    counts = json.load(open(GOLDEN / "counts.json"))
    updates = json.load(open(GOLDEN / "updates.json"))
    read_key = lambda c: (c["store"], c["query"], c["param"])
    write_key = lambda c: (c["format"], c["query"], c["param"])

    got_reads = measure_reads(counts, a.work)
    got_writes = measure_updates(updates, [c["format"] for c in updates["cells"]], a.work)
    if a.plant:
        got_reads[-1]["N"] += 1   # a count that moved while no counting version did (the last cell: the self-test plants below use the first two)
        print(f"[golden] PLANT: {read_key(got_reads[-1])} N off by one, no version bumped [control: must FAIL]")
    d_reads = compare(counts["cells"], got_reads, read_key)
    d_writes = compare(updates["cells"], got_writes, write_key)
    fields = 5 + len(counts["lanes"]) + 2
    print(f"[golden] reads: {len(counts['cells'])} golden cells, {len(got_reads)} measured, {fields} fields each: "
          f"{len(d_reads)} differences")
    print(f"[golden] writes: {len(updates['cells'])} golden cells, {len(got_writes)} measured: {len(d_writes)} differences")
    for k, f, w, g in (d_reads + d_writes)[:30]:
        print(f"  DIFF {k} {f}: golden {w}, measured {g}")
    # a moved count with every counting version unmoved: a forgotten bump (else: a bump, to re-record with --update)
    fmt_of_key = {**{read_key(c): format_of(c) for c in counts["cells"] + got_reads},
                  **{write_key(c): format_of(c) for c in updates["cells"] + got_writes}}
    unbumped = sorted({(fmt_of_key[k], kind) for kind, diffs, gold, parts in (("read", d_reads, counts, "read"),
                                                                              ("write", d_writes, updates, "write"))
                       for k, f, w, _ in diffs if bumped(gold, parts, fmt_of_key[k]) is None
                       and not (f.startswith("lanes.") and w is None)     # a lane the golden file never stored: added, not moved
                       and not (f == "(cell)" and w == "absent")})        # a cell it never stored (a query added): the same
    for fmt, kind in unbumped:
        print(f"  NO BUMP: a {kind} count on {fmt} moved, and neither the seam's nor {fmt}'s {kind} counting version did "
              f"(bump it in C++: store/counting.h or the format's pair in formats/registry.cc)")
    if a.update:
        if unbumped:
            sys.exit(f"check_golden_counts --update: {len(unbumped)} (format, kind) moved with no counting-version bump: "
                     f"bump first, then --update")
        rewritten = []
        for name, gold, got, parts, diffs in (("counts.json", counts, got_reads, "read", d_reads),
                                              ("updates.json", updates, got_writes, "write", d_writes)):
            if not diffs and stamps(got, parts) == gold.get("counting"):
                continue                     # unchanged: its cells and the provenance that recorded them stay
            rewritten.append(name)
            gold.update(cells=got, counting=stamps(got, parts),
                        provenance={"binary_git_sha": prov["binary_git_sha"], "binary_dirty": prov["binary_dirty"],
                                    "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                    "commit": prov["binary_git_sha"]})
            with open(GOLDEN / name, "w") as f:
                json.dump(gold, f, indent=1, sort_keys=True)
        print(f"[golden] re-recorded {' and '.join(rewritten) or 'nothing (no cell changed)'} from binary "
              f"{str(prov['binary_git_sha'])[:10]}")
        return 0

    # the plants: a wrong count, a wrong hash and a missing cell must each be one difference
    planted = copy.deepcopy(counts["cells"])
    planted[0]["N"] += 1
    planted[1]["result_hash"] = "0" * 32
    plants = [len(compare(planted, got_reads, read_key)) - len(d_reads),
              len(compare(counts["cells"], got_reads[1:], read_key)) - len(d_reads),
              len(compare(updates["cells"][:1] + [dict(updates["cells"][1], P=updates["cells"][1]["P"] + 1)]
                          + updates["cells"][2:], got_writes, write_key)) - len(d_writes)]
    caught = plants == [2, 1, 1]
    print(f"[golden] plants (a wrong N + a wrong hash, a missing read cell, a wrong write P): {plants} new differences, "
          f"want [2, 1, 1]: {'caught' if caught else 'NOT CAUGHT'}")
    want_reads = (len(counts["formats"]) + len(counts["meta_cells"])) * len(counts["queries"]) * counts["nparam"]
    ok = not d_reads and not d_writes and caught and len(got_reads) == want_reads and len(got_writes) == 3
    print(f"check_golden_counts {'OK' if ok else 'FAIL'}: {len(got_reads)} read + {len(got_writes)} write cells "
          f"vs the freeze's golden counts (binary {str(prov['binary_git_sha'])[:10]})"
          + (f"; {len(unbumped)} (format, kind) moved with no counting-version bump" if unbumped else "")
          + (" [control: must FAIL]" if a.plant else ""))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
