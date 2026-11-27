# AGENTS.md

Set up first: [README.md](README.md#getting-started) (`just setup`, `just build`, then `just fetch-dataset ldbc=0.1`).
The words (layout, mix, family, workload, body, pick, the KV cost models): [python/README.md](python/README.md#concepts).

## Task: find the best layout for a workload

1. Write the workload: queries and how often each runs, as `inputs/workloads/ldbc/examples/ic7_ic2.json` does (its
   fields: [inputs/workloads/README.md](inputs/workloads/README.md)). Below, `my.json`.
2. Predict each uniform layout's cost from the graph's statistics, no store (40 s the first time at SF0.1):
   `uv run --locked kvcost predict --workload my.json --dataset ldbc=0.1`, then
   `uv run --locked kvcost cost-layouts --workload my.json --dataset ldbc=0.1 --count-source predicted-tier1`.
3. Measure three layouts and compose the rest (4 min at SF0.1 for the whole walkthrough):
   `uv run --locked kvcost measure --workload my.json --dataset ldbc=0.1 Sp-Sp Co-Co Ch64k-Ch64k`, then
   `uv run --locked kvcost compose --dataset ldbc=0.1`, then
   `uv run --locked kvcost cost-layouts --workload my.json --dataset ldbc=0.1 --cost-model wops_bytes4k`.
4. Find the best mix, then build it and check that measured = composed:
   `uv run --locked kvcost search-mix --workload my.json --dataset ldbc=0.1 --cost-model wops_bytes4k`, then
   `uv run --locked kvcost check-mix --workload my.json --dataset ldbc=0.1 mix:best`.

Each command's flags: `uv run --locked kvcost <command> -h`. Where each writes, and the paper's forms:
[python/kvcost/cli/README.md](python/kvcost/cli/README.md). A workload of new queries is measured as a slice, in a
tree of its own: [inputs/workloads/README.md](inputs/workloads/README.md).

## Task: add a query or a layout

- **A query:** copy `python/kvevaluator/workloads/interactive/queries/example.py` and build its logical plan from
  steps. Write its plan files with `uv run --locked python python/kvevaluator/plan_choice/dump_planir.py --family
  interactive`. Run it on one store, step by step: `uv run --locked python -m kvevaluator.measure.run_one --sf 0.1
  --family interactive --store Sp-Co --query <name>`. The details: [inputs/workloads/README.md](inputs/workloads/README.md#add-your-own-workload).
- **A layout:** copy a spec from `inputs/specs/grid/`, run one query on it (`run_one ... --store my_layout.json`), then
  measure it: `uv run --locked kvcost measure --dataset ldbc=0.1 --workload i01 my_layout.json`. A layout the spec
  cannot say is a new format dir, in C++: [cpp/kvevaluator/src/formats/README.md](cpp/kvevaluator/src/formats/README.md).

## Rules a change keeps

| rule | the check that enforces it |
|---|---|
| Plan files are generated from the query's Python, never edited by hand | `tests/unit/check_plan_files.py` (in `just tests-fast`); the write plans: `just write-gate` |
| The C++ counts equal the Python oracle's on every plan file | `just cpp-diff` (`tests/gates/check_cpp_vs_python.py`), in `just gate full` |
| A count that moves says why: the SF0.1 counts are frozen in the repo | `just golden-counts` |
| Paths come from `kvevaluator.paths` and `kvevaluator.output_tree`, never a hand-made join | `tests/lint/check_paths.py` (in `just lint`) |
| Imports go one way: `kvcost` and `kvengines` import `kvevaluator`, and its layers import down | `tests/lint/check_layers.py` (in `just lint`) |
| Only code and inputs are tracked; a doc is a README.md (or this file, or `docs/walkthrough.md`) | `tests/lint/check_code_only.py` (in `just lint`) |
| Every `kvcost` call written in the repo, docs included, parses | `tests/lint/check_kvcost_calls_parse.py` (in `just lint`) |
| Every check runs somewhere: a runner names it, or `tests/by_hand.txt` | `tests/lint/check_tests_reached.py` (in `just lint`) |

The checks, cheapest first: `just lint` (45 s), `just tests-fast` (1.5 to 5 min), and `just gate full` (13 min, 18
with no capture to reuse): every check a merge needs, on a clean commit. Adding a check: [tests/README.md](tests/README.md).

## What the numbers mean

- **Every format is charged fairly:** what its layout forces under the cheapest plan it allows, never a deployment
  knob; every knob is set the same for every format
  ([formats README](cpp/kvevaluator/src/formats/README.md#8-what-a-format-is-charged)).
- **Every result names the binary that made it;** a binary built from uncommitted changes marks its results stale.
- **The counts are KV work** (KV operations and bytes); latency is its own timed run (`kvcost measure-latency`).
- **A cost always names its KV cost model** (`--cost-model wops_bytes4k`): there is no default.

## Running

- One job at a time per RocksDB store: two runs that open one store collide on its LOCK.
