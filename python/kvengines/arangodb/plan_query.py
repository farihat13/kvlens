#!/usr/bin/env python3
"""
Interactive plan inspection for ArangoDB (AQL) queries.

The ArangoDB counterpart of kvengines.nebula.plan_query. Runs an AQL query
with profiling and produces:
  1. plan.json        — merged explain plan + per-node profile (the saved artifact)
  2. plan_dag.png     — Graphviz DAG, color-coded by per-node self time
  3. plan_breakdown.png — horizontal bar chart of self time per node
  4. Terminal summary table

Can also re-analyze a previously saved plan.json (--from-saved), no DB needed.

How the timing works (ArangoDB-specific):
  ArangoDB profile=2 reports a `runtime` per execution node, in SECONDS, that is
  CUMULATIVE up the pipeline — the final node's runtime is ~the whole query time.
  So the time actually spent IN a node is its self time:
      self = runtime - max(runtime of its dependencies)
  We color the DAG and size the bars by self time. The raw cumulative runtime is
  kept in the table and in plan.json.

  `explain` gives the static plan (node types, dependencies, estimatedCost,
  estimatedNrItems) without running the query. `--explain-only` stops there.

Usage:
  # Run + profile a query file with an LDBC param row
  python -m kvengines.arangodb.plan_query -f queries/interactive-complex-1.aql \
      --param-file data/ldbc/substitution_parameters-sf0.3/interactive_1_param.txt \
      --param-index 1

  # Inline query, explicit params
  python -m kvengines.arangodb.plan_query -s 'FOR p IN Person FILTER p._key == @personId RETURN p.firstName' \
      --params "personId=933"

  # Static plan only (no execution)
  python -m kvengines.arangodb.plan_query -f queries/interactive-complex-6.aql \
      --param-file data/ldbc/substitution_parameters-sf0.3/interactive_6_param.txt --explain-only

  # Re-analyze a saved plan.json (no DB connection needed)
  python -m kvengines.arangodb.plan_query --from-saved path/to/plan.json

  # Output to a specific directory
  python -m kvengines.arangodb.plan_query -f queries/interactive-complex-1.aql --outdir output/
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Add lib/ to path for kvengines imports


# ---------------------------------------------------------------------------
# Param loading (mirrors kvengines.nebula.plan_query's helpers)
# ---------------------------------------------------------------------------

def parse_params_string(params_str: str) -> dict:
    """Parse 'key1=val1,key2=val2' into a dict, coercing numeric values."""
    params = {}
    for pair in params_str.split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        key, val = pair.split("=", 1)
        key, val = key.strip(), val.strip()
        try:
            val = int(val)
        except ValueError:
            try:
                val = float(val)
            except ValueError:
                pass
        params[key] = val
    return params


def load_param_from_file(param_file: str, param_index: int) -> dict:
    """Load a single param set from an LDBC substitution_parameters file."""
    path = Path(param_file)
    with open(path) as f:
        reader = csv.reader(f, delimiter="|")
        headers = next(reader)
        for i, row in enumerate(reader, start=1):
            if i == param_index:
                params = {}
                for h, v in zip(headers, row):
                    h, v = h.strip(), v.strip()
                    try:
                        v = int(v)
                    except ValueError:
                        try:
                            v = float(v)
                        except ValueError:
                            pass
                    params[h] = v
                return params
    raise ValueError(f"Param index {param_index} not found in {param_file}")


# ---------------------------------------------------------------------------
# Explain / profile
# ---------------------------------------------------------------------------

def run_explain(query_text: str, params: dict | None, db_name: str,
                host: str, port: int) -> dict:
    """Static plan only — `db.aql.explain`. No query execution.

    Returns a normalized {nodes, estimated_cost, estimated_nr_items, rules,
    collections, executed: False} dict.
    """
    from arango import ArangoClient

    client = ArangoClient(hosts=f"http://{host}:{port}")
    db = client.db(db_name, username="root", password="")
    plan = db.aql.explain(query_text, bind_vars=params or None)
    client.close()

    return {
        "executed": False,
        "estimated_cost": plan.get("estimatedCost"),
        "estimated_nr_items": plan.get("estimatedNrItems"),
        "rules": plan.get("rules", []),
        "collections": plan.get("collections", []),
        "nodes": plan.get("nodes", []),
        "node_stats": {},
        "query_stats": {},
    }


def run_profile(query_text: str, params: dict | None, db_name: str,
                host: str, port: int) -> dict:
    """Execute with profile=2 and merge the per-node runtime into the plan.

    Returns the same normalized dict as run_explain plus per-node profile stats
    (keyed by node id) and the aggregate query statistics.
    """
    from arango import ArangoClient

    client = ArangoClient(hosts=f"http://{host}:{port}")
    db = client.db(db_name, username="root", password="")

    # We still call explain to get the static plan (node types, deps, estimates);
    # profile=2 returns per-node runtime but the richest node descriptions live in
    # the explain plan, so we keep both and key the profile stats by node id.
    plan = db.aql.explain(query_text, bind_vars=params or None)

    cursor = db.aql.execute(query_text, bind_vars=params or None, profile=2)
    rows = list(cursor)
    stats = cursor.statistics() or {}
    client.close()

    node_stats = {n["id"]: n for n in stats.get("nodes", [])}
    # statistics() carries both per-node ('nodes') and aggregate counters; keep the
    # aggregate ones (drop 'nodes' to avoid duplicating it).
    query_stats = {k: v for k, v in stats.items() if k != "nodes"}
    query_stats["result_rows"] = len(rows)

    return {
        "executed": True,
        "estimated_cost": plan.get("estimatedCost"),
        "estimated_nr_items": plan.get("estimatedNrItems"),
        "rules": plan.get("rules", []),
        "collections": plan.get("collections", []),
        "nodes": plan.get("nodes", []),
        "node_stats": node_stats,
        "query_stats": query_stats,
    }


# ---------------------------------------------------------------------------
# Self-time computation
# ---------------------------------------------------------------------------

def compute_self_times(plan: dict) -> dict:
    """Map node id -> self runtime in seconds.

    ArangoDB's per-node `runtime` is cumulative up the pipeline, so the time spent
    in a node alone is its runtime minus the largest runtime among its dependencies.
    Returns {} when the plan was not executed (no profile data).
    """
    node_stats = plan.get("node_stats", {})
    if not node_stats:
        return {}

    runtime = {nid: s.get("runtime", 0.0) for nid, s in node_stats.items()}
    deps = {n["id"]: n.get("dependencies", []) for n in plan["nodes"]}

    self_times = {}
    for nid, total in runtime.items():
        dep_max = max((runtime.get(d, 0.0) for d in deps.get(nid, [])), default=0.0)
        self_times[nid] = max(0.0, total - dep_max)
    return self_times


def node_label(node: dict) -> str:
    """Short operator label, e.g. 'IndexNode' -> 'Index'; keep collection if present."""
    name = node.get("type", "Node")
    if name.endswith("Node"):
        name = name[:-4]
    coll = node.get("collection")
    if coll:
        name = f"{name}[{coll}]"
    return name


# ---------------------------------------------------------------------------
# Visualization
# ---------------------------------------------------------------------------

def _color_for(self_ms: float) -> str:
    """Color scale by per-node self time (ms). Mirrors the Nebula tool's bands."""
    if self_ms > 500:
        return "red"
    if self_ms > 100:
        return "orange"
    if self_ms > 10:
        return "yellow"
    return "lightblue"


def visualize_plan_dag(plan: dict, output_file: str, verbose: bool = False) -> str | None:
    """Write a Graphviz DOT file and render it to PNG via the `dot` binary.

    We hand-write DOT (instead of the python `graphviz` package) because the dot
    binary is present in this environment but the package is not. Edges point from
    a node to its dependencies (data flows dependency -> node), matching the Nebula
    DAG's direction.
    """
    self_times = compute_self_times(plan)
    node_stats = plan.get("node_stats", {})

    lines = ["digraph plan {", '  rankdir=TB;', '  node [shape=box, fontsize=10];']
    for node in plan["nodes"]:
        nid = node["id"]
        label = node_label(node)
        parts = [f"{label} ({nid})"]

        st = node_stats.get(nid)
        if st is not None:
            self_ms = self_times.get(nid, 0.0) * 1000
            total_ms = st.get("runtime", 0.0) * 1000
            parts.append(f"Items: {st.get('items', 0):,}")
            parts.append(f"Self: {self_ms:.1f}ms")
            parts.append(f"Cumul: {total_ms:.1f}ms")
            fill = _color_for(self_ms)
            attrs = f'label="{chr(10).join(parts)}", style=filled, fillcolor={fill}'
        else:
            # explain-only: show static estimates instead of runtime
            parts.append(f"est.cost: {node.get('estimatedCost', 0):.0f}")
            parts.append(f"est.rows: {node.get('estimatedNrItems', 0)}")
            attrs = f'label="{chr(10).join(parts)}"'

        lines.append(f'  {nid} [{attrs}];')

    for node in plan["nodes"]:
        for dep in node.get("dependencies", []):
            lines.append(f"  {dep} -> {node['id']};")
    lines.append("}")

    dot_path = f"{output_file}.dot"
    Path(dot_path).write_text("\n".join(lines))

    png_path = f"{output_file}.png"
    try:
        subprocess.run(["dot", "-Tpng", dot_path, "-o", png_path],
                       check=True, capture_output=True)
        return png_path
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"Warning: `dot` render failed ({e}); DOT source kept at {dot_path}")
        return dot_path


def visualize_plan_barchart(plan: dict, output_file: str) -> str | None:
    """Horizontal bar chart of per-node self time (ms), sorted ascending."""
    self_times = compute_self_times(plan)
    if not self_times:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("Warning: matplotlib not available, skipping bar chart")
        return None

    by_id = {n["id"]: n for n in plan["nodes"]}
    items = [(nid, st * 1000) for nid, st in self_times.items() if st * 1000 >= 0.01]
    if not items:
        return None
    items.sort(key=lambda x: x[1])

    labels = [f"{node_label(by_id[nid])} ({nid})" for nid, _ in items]
    values = [ms for _, ms in items]

    fig, ax = plt.subplots(figsize=(10, max(3, len(items) * 0.4)))
    y = range(len(labels))
    ax.barh(y, values, color="#1f77b4")
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=8)
    ax.set_xlabel("Self time (ms)")
    total_ms = plan.get("query_stats", {}).get("execution_time", 0) * 1000
    ax.set_title(f"Plan node self-time breakdown (exec: {total_ms:.1f}ms)")
    plt.tight_layout()
    out_path = f"{output_file}.png"
    plt.savefig(out_path, dpi=150)
    plt.close()
    return out_path


def print_summary_table(plan: dict) -> None:
    """Terminal table: one row per node, sorted by self time descending."""
    self_times = compute_self_times(plan)
    node_stats = plan.get("node_stats", {})

    rows = []
    for node in plan["nodes"]:
        nid = node["id"]
        st = node_stats.get(nid, {})
        rows.append({
            "id": nid,
            "op": node_label(node),
            "items": st.get("items", node.get("estimatedNrItems", 0)),
            "self_ms": self_times.get(nid, 0.0) * 1000,
            "cumul_ms": st.get("runtime", 0.0) * 1000,
            "est_cost": node.get("estimatedCost", 0.0),
            "deps": ",".join(map(str, node.get("dependencies", []))),
        })
    rows.sort(key=lambda r: r["self_ms"], reverse=True)

    executed = plan.get("executed")
    print(f"\n  === Plan ({'profiled' if executed else 'explain-only'}) ===")
    if executed:
        hdr = f"  {'ID':>3}  {'Operator':<22} {'Items':>10} {'Self ms':>9} {'Cumul ms':>9}  Deps"
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for r in rows:
            print(f"  {r['id']:>3}  {r['op']:<22} {r['items']:>10,} "
                  f"{r['self_ms']:>9.2f} {r['cumul_ms']:>9.2f}  {r['deps']}")
        qs = plan.get("query_stats", {})
        print(f"\n  exec_time={qs.get('execution_time', 0)*1000:.1f}ms  "
              f"result_rows={qs.get('result_rows', 0)}  "
              f"scanned_index={qs.get('scanned_index', 0)}  "
              f"scanned_full={qs.get('scanned_full', 0)}  "
              f"filtered={qs.get('filtered', 0)}  "
              f"cacheHits={qs.get('cacheHits', 0)}  cacheMisses={qs.get('cacheMisses', 0)}")
    else:
        hdr = f"  {'ID':>3}  {'Operator':<22} {'est.rows':>10} {'est.cost':>10}  Deps"
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for r in rows:
            print(f"  {r['id']:>3}  {r['op']:<22} {r['items']:>10,} "
                  f"{r['est_cost']:>10.1f}  {r['deps']}")
        print(f"\n  estimatedCost={plan.get('estimated_cost')}  "
              f"estimatedNrItems={plan.get('estimated_nr_items')}")


def analyze_plan(plan: dict, outdir: Path, do_dag: bool, do_bar: bool) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    if do_dag:
        dag = visualize_plan_dag(plan, str(outdir / "plan_dag"))
        if dag:
            print(f"  DAG: {dag}")
    if do_bar:
        bar = visualize_plan_barchart(plan, str(outdir / "plan_breakdown"))
        if bar:
            print(f"  Bar chart: {bar}")
        elif plan.get("executed"):
            print("  Bar chart: skipped (no per-node timing)")
    print_summary_table(plan)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Interactive plan inspection for ArangoDB (AQL) queries.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("-f", "--file", help="Query file path (.aql)")
    source.add_argument("-s", "--string", help="Inline query string")
    source.add_argument("--from-saved", metavar="PLAN_JSON",
                        help="Re-analyze a saved plan.json (no DB needed)")

    parser.add_argument("--params", help='Query params as "key1=val1,key2=val2"')
    parser.add_argument("--param-file", help="LDBC substitution_parameters file")
    parser.add_argument("--param-index", type=int, default=1,
                        help="Row index in param file (default: 1)")

    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8529)
    parser.add_argument("--db", default="ldbc")

    parser.add_argument("--explain-only", action="store_true",
                        help="Static plan only — do not execute the query")
    parser.add_argument("--outdir", default="./logs/plan-query",
                        help="Output directory for plan.json + figures")
    parser.add_argument("--no-dag", action="store_true", help="Skip DAG visualization")
    parser.add_argument("--no-barchart", action="store_true", help="Skip bar chart")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()
    outdir = Path(args.outdir)

    # --- Re-analyze mode ---
    if args.from_saved:
        plan_path = Path(args.from_saved)
        if not plan_path.exists():
            print(f"Error: {plan_path} not found", file=sys.stderr)
            sys.exit(1)
        print(f"Loading plan from: {plan_path}")
        plan = json.loads(plan_path.read_text())
        # JSON object keys are always strings; node ids in `nodes[]` stay ints.
        # Re-key node_stats by int so it matches the plan node ids again.
        plan["node_stats"] = {int(k): v for k, v in plan.get("node_stats", {}).items()}
        if args.outdir == "./logs/plan-query":
            outdir = plan_path.parent
        analyze_plan(plan, outdir, do_dag=not args.no_dag, do_bar=not args.no_barchart)
        return

    # --- Live mode ---
    if args.file:
        query_path = Path(args.file)
        if not query_path.exists():
            print(f"Error: {query_path} not found", file=sys.stderr)
            sys.exit(1)
        query_text = query_path.read_text()
        if args.outdir == "./logs/plan-query":
            outdir = Path("./logs/plan-query") / query_path.stem
    else:
        query_text = args.string

    # Strip line comments (AQL uses //); python-arango handles the rest.
    query_text = "\n".join(
        line for line in query_text.splitlines()
        if not line.strip().startswith("//")
    )

    # Load + preprocess params
    params = None
    if args.params:
        params = parse_params_string(args.params)
    elif args.param_file:
        params = load_param_from_file(args.param_file, args.param_index)
        qnum_match = re.search(r"interactive_(\d+)_param", args.param_file)
        if qnum_match:
            from kvengines.ldbc.params import arangodb_param_preprocessor
            params = arangodb_param_preprocessor(params, int(qnum_match.group(1)))
    if params:
        print(f"Params: {params}")

    if args.explain_only:
        print("Running EXPLAIN (no execution)...")
        plan = run_explain(query_text, params, args.db, args.host, args.port)
    else:
        print("Running query with profile=2...")
        plan = run_profile(query_text, params, args.db, args.host, args.port)
        qs = plan["query_stats"]
        print(f"  exec_time: {qs.get('execution_time', 0)*1000:.1f}ms  "
              f"result_rows: {qs.get('result_rows', 0)}")

    plan["query_text"] = query_text
    plan["params"] = params

    outdir.mkdir(parents=True, exist_ok=True)
    plan_path = outdir / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=2, default=str))
    print(f"  Plan saved: {plan_path}")

    analyze_plan(plan, outdir, do_dag=not args.no_dag, do_bar=not args.no_barchart)


if __name__ == "__main__":
    main()
