# logical_census: which part of the graph does a workload touch, the same on every layout?

Per query we count the elements its plan reads or writes: edges and edge properties, each by access path (a list
expansion, or a direct probe of one edge), label checks, vertex properties, index entries, and items written. On the
nine uniform layouts these counts are identical, so a statement like "adjacency is most of what LDBC Interactive reads"
rests on the workload and the plan, not on a layout's KV costs. The counts are those of the plan bodies we wrote, not a
lower bound over every possible plan. Analysis only: the driver reads an SF3 grid's step rows and the plans, and opens
no store.

## Run

```bash
experiments/logical_census/run-logical-census.sh               # the census on output/'s SF3 captures (~5 s)
experiments/logical_census/run-logical-census.sh --root paper-results   # on the paper's runs (just fetch-results)
```

`just results census-touched` runs it after the workloads' counts, at the run's LDBC scale.

Flags: `--sf SF` (the LDBC scale, default 3), `--root DIR` (the output tree the captures are read from: default
`output/`, `paper-results` for the paper's runs), `--ldbc-grid DIR` (a `captures/ldbc/<scale>` dir; default the
tree's at `--sf`), `--bi-grid DIR` (the place holding BI's capture; default the LDBC place), `--out DIR` (default `<output dir>/summary/ldbc/sf<SF>/logical-census`). On the archived grid
the three CSVs are byte-identical to the snapshot's `sf3/logical-census/`, except the `vertices` column: since
it counts a vertex only when its step reads a stored property (a step that asks only `id`, the key, reads
nothing), so it is the owners of the vertex properties read; no derive reads it. The paper's figure is drawn from those by
`paper/derive/census_workloads.py` and its draw script.

The check against NebulaGraph's own plan (below) needs the engines extra (`just setup`). It opens sf0.1 stores
and starts no engine:

```bash
uv run --locked python -m kvevaluator.measure.grid_bodies --sf 0.1 --queries ic4,ic5,ic9,ic12,is1 \
    --params 1,2,3,4,5,7 --out <slice dir>
uv run --locked python experiments/logical_census/engine_profile_check.py --grid <slice dir> \
    --profile <data dir>/results/sf0.1/engine-fidelity-20261002/ldbc \
    --sf3-census <data dir>/results/sf3/mix-benefit-20261103/sf3/logical-census/census.csv --sf 0.1 --out <out dir>
```

## The algorithm

Each plan step is one operator call. Its step row records the rows it took in and gave out, and the plan says which
properties the step asks for. So:

| operator | counted as |
|---|---|
| read_neighbors (and the sorted / top-k / window adjacency reads) | edges via list expansion: an edge per output row; with edge props, also rows x props edge properties |
| read_edge_exists | an edge via a direct probe, one per input pair. No plan asks it for edge properties, so that column is 0 everywhere (it is kept) |
| read_closure, read_chase | edges via list expansion, an edge per output row (approximate, flagged `approx`) |
| read_vertex_props | input rows x props (per input row, not per distinct vertex) |
| read_vertex_by_id | one element per vertex (its label) |
| read_anchor_props | vertices x props (at least 1) |
| read_filter_by_label_probe / _index | one label check per input vertex |
| read_index_eq, read_index_range | the index entries the scan crosses (`keys_crossed`) |
| read_top_k_by_index, read_vertices_by_label | the index entries the walk crosses, then the kept rows x props |
| IU1-IU8 (insert_vertex, insert_edge) | what the schema stores: an edge entry per declared direction (its props inside it), the vertex's non-null prop values, and the index entries the vertex adds (`cpp/kvevaluator/src/index/sorted_index.cc`, `vertex_write_keys`) |
| every in-memory step | nothing |

A vertex's `id` is its key, not a stored property, so it is not counted.

**One body per query.** A query can have several plan bodies (up to 16 for IC10). The census takes, of the bodies
valid on both Dgraph and NebulaGraph, the one with the fewest elements: a body counts when its validity caps
(`kvevaluator.plans.ir.capabilities.validity_caps`) are within what both formats give on every LDBC label, Dgraph at its
512 KB chunk (`census.py`'s `engine_caps`, computed from the formats' code at run time; `{property_index, type_index}` on
the code). So the census counts plans both engines can run. Every body's census
stays in `census.csv`, and the per-query figure draws the spread over bodies as a whisker.

**The workload bars** (`census_totals.csv`): I01 = the sum over queries of LDBC's frequency x the query's elements;
"every query once" = the plain sum (the big queries dominate); "every query equal" = each query's own shares,
averaged. Each bar is each kind's share of its total. Weights: I01 and I02 from `kvcost.layout_cost.workloads.ldbc`
(runs per 100,000 updates); BI runs each query once.

**Gates** (every run): every (query, param, body, step) key is on every layout the grid measured with the same
operator, rows, rows in and keys crossed: the grid's cells and the Chunked-Chunked cells at other chunk sizes in the
same captures dir; the three diagonals are required, and a grid measured on the diagonals
alone composes the six others, so it has no step rows for them (LDBC 2,420 keys on 17 cells, BI 3,310 on 7, 0 differ); the updates' stored items equal the Split-Split
layout's items written (24 (update, part) pairs, 0 differ); every operator the plans cost maps to a kind; a body whose
sha changed under the same name stops the run (except bi10, `kvcost.layout_cost.compose.BI_DRIFT_OK`).

## Against a real engine: NebulaGraph's own plan

`engine_profile_check.py` asks whether a real engine's plan touches the same kinds as ours, on IC4, IC5, IC9, IC12 and
IS1 at sf0.1 (engine-fidelity params 1 and 7). NebulaGraph's plan is counted two ways: its engine twin (the
evaluator's copy of its plan, whose S / G / N equal the live engine's; gated on every cell) and its PROFILE rows, read
in place from the engine-fidelity capture (the plan's shape and rough counts only).

What it shows:
- the dominant kind is the same on IC12 (edges) and IS1 (vertex props);
- on IC4, IC5 and IC9 NebulaGraph's plan is dominated by tag work (label checks or vertex props): it fetches every tag
  of a message (IC4, IC5) or the props of every candidate before its TopN (IC9);
- where the two plans walk the same edges, the counts agree: IC9 reads 121,482 edges in both;
- the census's own shares at sf0.1 (params 1-5) are within 5.3 points of sf3's.

So the census describes the cheapest plan we wrote; an engine's plan can move the shares a long way.

## What is here

| file | what |
|---|---|
| `run-logical-census.sh` | the driver: `census.py`, then `plot_census.py` |
| `census.py` | step rows + plans -> `census_steps.csv` (per suite, query, body, param, step), `census.csv` (per query and body, the mean over params 1-5; `chosen` marks the body drawn), `census_totals.csv` (the workload bars) |
| `plot_census.py` | `census_per_query.{png,pdf}` and `census_workloads.{png,pdf}` |
| `engine_profile_check.py` | the census against NebulaGraph's own plan: `shares.csv`, `twin_steps.csv`, `profile_nodes.csv`, `shares_by_scale.csv`. Needs the engines extra |

The shared code it uses: `kvevaluator.plans.units` (which steps cost), `kvevaluator.plan_choice.candidates` (the plan
bodies), `kvcost.layout_cost` (the weights).

## Expected run time

On a 16-core AMD EPYC 7302P with a SATA SSD, sf0.1 stores already built:

| step | time |
|---|---|
| `run-logical-census.sh` at SF3 (analysis only) | 5 s |
| the sf0.1 slice (`grid_bodies`, 5 queries, 9 cells) | 40 s |
| `engine_profile_check.py` at sf0.1 (opens the nebula_engine store) | 29 s |
