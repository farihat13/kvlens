"""Every body the selector gives a store uses only ops its format runs. Store-free.

For each format (the engine formats and their store arms; meta at every committed spec under inputs/specs/ and experiments/) and each
plan bundle of LDBC (every physical design, with its label_only plan family as ir_for picks it), BI,
microbench, writes, JOB and LinkBench, it selects the body as a session would (the binary's caps,
kvevaluator.plans.ir.bundle.select_variant at each scale a variant names, the fragments expanded), then asks the binary how it would
run each op (`kvevaluator caps --ops`: flow, kv or neither). An op that is neither fails at run time with "unknown
op"; here it fails with no store. A property-index read (read_index_eq / read_index_range / read_top_k_by_index /
index_range_buckets) on a (label, prop) the schema declares no index on fails too: there the read throws "schema: no
index on", or on dgraph returns no rows. A cell = (format arm, design, workload, query); a bundle with no body for the format
is counted apart ("no body"). The binary refusing an arm on the canonical LDBC schema fails (every arm must build
there); elsewhere a refusal means no such store exists, and is listed. Known gaps (plan_ops_known_gaps.json, each with
a reason) pass; a listed gap that no longer fails, among the arms that ran, is a failure (the list is stale).
Scope: the body the selector gives (a generated file, which has no selector: every body legal on the store, the bodies
the pick record may name); not the *_engine formats.
About 11 s.

    python3 tests/unit/check_plan_ops_supported.py [--designs all|canonical,...] [--no-known] [--formats a,b]
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
from kvevaluator.measure.plan_runs import PLAN_DIRS, ir_for
from kvevaluator.capture.binary import binary_path, binary_provenance
from kvevaluator.paths import ROOT
from kvevaluator.plans.ir.bundle import NoVariant, expand_fragments, require_bare, select_variant
from kvevaluator.schema.schema_json import to_json

_EXP = str(ROOT)

KNOWN = os.path.join(_HERE, "plan_ops_known_gaps.json")
PLANS = os.path.join(_EXP, "python", "kvevaluator", "workloads")
# the workloads by the schema their stores are built on
SCHEMA_WORKLOADS = {"ldbc": ["interactive", "bi", "microbench", "writes"], "job": ["job"], "linkbench": ["linkbench"]}
# the engine formats and their store arms (check_caps_parity's): the chunk arms either side of CHUNK_COARSE.
# Not covered: the engine-fidelity formats (*_engine).
ENGINE_ARMS = [(f, None) for f in ("dgraph", "nebula", "arangodb", "pertype_csr")]
ENGINE_ARMS += [(f, {"chunk_target": c}) for f in ("dgraph", "pertype_csr")
                for c in (65536, 524288)]


SPEC_ROOTS = ("inputs/specs", "experiments")   # the spec sets, and a study that keeps a spec of its own


def meta_specs():
    """every committed meta spec under inputs/specs/ (the corners, the grid dirs, the layout-choice mixes and groupings,
    the examples) and experiments/: a JSON object with an `adjacency` or `vertex_props` component. Prints the count per
    root: a root that moved shows as a drop there (the move left this scanning experiments/ alone)"""
    out, per_root = [], {}
    for rel in SPEC_ROOTS:
        root = os.path.join(_EXP, rel)
        per_root[rel] = 0
        for p in sorted(glob.glob(os.path.join(root, "**", "*.json"), recursive=True)):
            try:
                with open(p) as f:
                    spec = json.load(f)
            except (OSError, ValueError):
                continue
            if isinstance(spec, dict) and ("adjacency" in spec or "vertex_props" in spec):
                out.append((f"meta[{os.path.relpath(os.path.splitext(p)[0], root)}]", spec))
                per_root[rel] += 1
    print("meta specs: " + ", ".join(f"{r}/ {n}" for r, n in per_root.items()))
    if not per_root[SPEC_ROOTS[0]]:
        raise SystemExit(f"no meta spec under {SPEC_ROOTS[0]}/: the spec sets moved again? (every meta arm would be skipped)")
    return out


def ldbc_designs():
    """every LDBC design but the parked ones (datasets/ldbc/schema.py PARKED_SCHEMAS)"""
    from kvevaluator.datasets.ldbc.schema import ACTIVE_SCHEMAS
    return sorted(ACTIVE_SCHEMAS)


def schema_of(kind, design):
    if kind == "ldbc":
        from kvevaluator.datasets.ldbc.schema import get_schema
    elif kind == "job":
        from kvevaluator.datasets.imdb.schema import get_schema
    else:
        from kvevaluator.datasets.linkbench.schema import get_schema
    return get_schema(design)


def bundles(workload, label_only):
    """the stems of every bundle file a store of this workload may run (ir_for's directories)"""
    dirs = (["interactive/plans/label_only"] if label_only and workload == "interactive" else []) + PLAN_DIRS[workload]
    stems = set()
    for d in dirs:
        stems |= {os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(PLANS, *d.split("/"), "*.json"))}
    return sorted(stems)


# the ops that read a PROPERTY index (label + prop): valid only where the schema declares that index
from kvevaluator.plans.ir.bundle import PROPERTY_INDEX_OPS as INDEX_OPS


def body_steps(ir, caps, of_label, scale):
    """the steps of the body selected at `scale` (None: untagged), or None when no variant runs; a generated file (no
    selector: the pick record names the body) gives the steps of every body legal on the store, or None when none is"""
    if "plans" in ir and "variants" not in ir:
        from kvevaluator.plan_choice.pick_record import legal_bodies
        legal = legal_bodies(ir, caps, of_label)
        if not legal:
            return None
        return [st for b in legal for st in expand_fragments(ir["plans"][b], ir.get("fragments", [])).get("steps", [])]
    if "variants" not in ir:
        try:
            require_bare(ir, caps)                     # a bare plan's own store caps (property_index)
        except NoVariant:
            return None
        plan = ir
    else:
        try:
            plan = ir["plans"][select_variant(ir, caps, of_label, scale)]
        except NoVariant:
            return None
    return expand_fragments(plan, ir.get("fragments", [])).get("steps", [])


def undeclared_index_reads(step, declared):
    """the (label, prop) pairs an index op reads that the schema declares no index on"""
    if step["op"] not in INDEX_OPS:
        return []
    labels = step.get("labels", step.get("label"))
    labels = [labels] if isinstance(labels, str) else list(labels)
    return [(lb, step["prop"]) for lb in labels if (lb, step["prop"]) not in declared]


def scales_of(ir):
    """the scales to select at: each tag a variant names, then one no variant names (the untagged default)"""
    tags = sorted({t for v in ir.get("variants", []) for t in v.get("scales", [])})
    return tags + ["untagged"] if tags else [None]


def caps_and_ops(binary, schema_path, fmt, config, ops):
    cmd = [binary, "caps", "--schema", schema_path, "--format", fmt, "--ops", ",".join(sorted(ops))]
    if config:
        cmd += ["--codec-config", json.dumps(config)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return None, (r.stderr or r.stdout).strip().splitlines()[-1:] or ["exit %d" % r.returncode]
    return json.loads(r.stdout), None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--designs", default="all", help="LDBC physical designs, comma list (default: all); JOB and "
                    "LinkBench run their canonical design")
    ap.add_argument("--formats", default=None, help="only these arms (a format name, or meta)")
    ap.add_argument("--no-known", action="store_true", help="ignore the known-gaps list (every gap fails)")
    a = ap.parse_args(argv)
    binary = binary_path()
    prov = binary_provenance()
    print(f"binary {prov['path']}: {str(prov.get('binary_git_sha'))[:12]} dirty {prov.get('binary_dirty')}")
    known = [] if a.no_known else json.load(open(KNOWN))["gaps"]
    for k in known:
        if not k.get("reason"):
            raise SystemExit(f"known gap without a reason: {k}")
    arms = ENGINE_ARMS + [("meta", spec, label) for label, spec in meta_specs()]
    arms = [(x[0], x[1], x[2] if len(x) > 2 else
             x[0] + (f"[{','.join(f'{k}={v}' for k, v in sorted(x[1].items()))}]" if x[1] else "")) for x in arms]
    if a.formats:
        keep = set(a.formats.split(","))
        arms = [x for x in arms if x[0] in keep]
    n_cells = n_nobody = 0
    gaps, refused, refused_canonical, per_wl, ran_arms = [], [], [], defaultdict(int), set()
    designs = ldbc_designs() if a.designs == "all" else a.designs.split(",")
    with tempfile.TemporaryDirectory() as tmp:
        for kind, workloads in SCHEMA_WORKLOADS.items():
            for design in (designs if kind == "ldbc" else ["canonical"]):
                schema = schema_of(kind, design)
                spath = os.path.join(tmp, f"{kind}_{design}.json")
                with open(spath, "w") as f:
                    json.dump(to_json(schema, name=design), f)
                label_only = not (schema.indexed_cols or schema.date_cols)
                with open(spath) as f:   # the property indexes the schema declares (the file the binary reads)
                    declared = {(ix["label"], ix["prop"]) for ix in json.load(f)["indexes"] if ix["prop"]}
                # every bundle of these workloads, loaded once per schema
                irs = {(w, stem): ir_for(w, stem, label_only_family=label_only)
                       for w in workloads for stem in bundles(w, label_only)}
                all_ops = {s["op"] for ir in irs.values()
                           for body in list(ir.get("plans", {}).values()) + [ir] + ir.get("fragments", [])
                           for s in body.get("steps", []) if "op" in s}
                arms_ok = cells_d = 0
                gaps_before = len(gaps)
                for fmt, config, label in arms:
                    got, err = caps_and_ops(binary, spath, fmt, config, all_ops)
                    if got is None:                        # the binary refuses this arm on this schema: no store
                        r = f"{label} @{kind}/{design}: {err[0][:120]}"
                        (refused_canonical if (kind, design) == ("ldbc", "canonical") else refused).append(r)
                        continue
                    arms_ok += 1
                    ran_arms.add(label)
                    caps = set(got["capabilities"])
                    by = {**got["by_label"], **got["by_etype"]}
                    of_label = lambda lbl, by=by, caps=caps: set(by.get(lbl, caps))
                    for (w, stem), ir in sorted(irs.items()):
                        bad, ran = {}, False
                        for scale in scales_of(ir):
                            steps = body_steps(ir, caps, of_label, scale)
                            if steps is None:
                                continue
                            ran = True
                            for st in steps:
                                if got["ops"].get(st["op"]) not in ("flow", "kv"):   # neither, or no answer
                                    bad.setdefault(st["op"], []).append(scale or "any")
                                for lb, pr in undeclared_index_reads(st, declared):
                                    bad.setdefault(f"{st['op']} on undeclared index {lb}.{pr}", []).append(scale or "any")
                        if not ran:
                            n_nobody += 1
                            continue
                        cells_d += 1
                        per_wl[w] += 1
                        for op, scales in sorted(bad.items()):
                            gaps.append({"config": label, "design": design, "workload": w, "query": stem, "op": op,
                                         "scales": scales})
                n_cells += cells_d
                print(f"  {kind}/{design}: {arms_ok} arms, {cells_d} cells, {len(gaps) - gaps_before} use an op "
                      f"the format does not run")

    def matches(k, g):
        return all(k[f] == g[f] for f in ("config", "workload", "query", "op"))

    unknown = [g for g in gaps if not any(matches(k, g) for k in known)]
    # stale: a listed gap whose arm ran and did not fail (an arm left out by --formats says nothing)
    stale = [k for k in known if k["config"] in ran_arms and not any(matches(k, g) for g in gaps)]
    for g in gaps:
        tag = "known" if g not in unknown else "FAIL "
        reason = next((k["reason"] for k in known if matches(k, g)), "")
        print(f"  {tag} {g['config']} @{g['design']} {g['workload']}/{g['query']}: op '{g['op']}' is not run by "
              f"this format (scales {','.join(g['scales'])})" + (f" -- {reason}" if reason else ""))
    for k in stale:
        print(f"  FAIL listed as a known gap, but it no longer fails (remove it from {os.path.basename(KNOWN)}): {k}")
    # an arm the binary refuses on a schema (a mix naming LDBC types, on the JOB schema) has no store: one line per schema
    by_schema = defaultdict(list)
    for r in refused:
        by_schema[r.split(": ", 1)[0].split(" @", 1)[1]].append(r)
    for sch, rs in sorted(by_schema.items()):
        print(f"  refused on {sch} (no store can exist): {len(rs)} arms, e.g. {rs[0][:150]}")
    for r in refused_canonical:
        print(f"  FAIL refused on the canonical LDBC schema, where every arm must build: {r}")
    print(f"plan ops: {n_cells} cells over {len(arms)} format arms ("
          + ", ".join(f"{w} {n}" for w, n in sorted(per_wl.items())) + f"), {n_nobody} no body, "
          f"{len(refused)} (arm, schema) refused; {len(gaps)} cells use an op the format does not run, "
          f"{len(gaps) - len(unknown)} of them known")
    ok = n_cells > 0 and not unknown and not stale and not refused_canonical
    print("PLAN OPS OK" if ok else f"PLAN OPS FAIL: {len(unknown)} unknown gaps, {len(stale)} stale known entries, "
          f"{len(refused_canonical)} arms refused on ldbc/canonical" + (", 0 cells" if not n_cells else ""))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
