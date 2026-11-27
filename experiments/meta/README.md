# meta: the configurable format's grid, its BI cells and its checks

`meta` is one C++ format that reads a co-location spec and lays out a shared RocksDB by it. Its nine uniform layouts
(vertex properties x adjacency, each Split, Condensed or Chunked) are **the grid**, measured by `kvcost measure` (and composed by `kvcost compose`)
(`inputs/specs/grid/`; the other spec sets in `inputs/specs/`). The paper's runs measure seven stores (named on the command line): the 3 diagonal
cells (Split/Split, Condensed/Condensed, Chunked/Chunked) and Chunked/Chunked at 1, 4, 16 and 256 KB. It then composes
the 6 other cells from the diagonals (`compose_cells`: a cell takes its property parts from one diagonal and its
adjacency parts from the other) into the composed captures `output/composed/ldbc/sf<SF>/<workload>/<params>/`. Every
tool reads each layout by one rule (`output_tree.layout_capture`: measured itself in `captures/`, else `composed/`);
naming the six to `kvcost measure` measures them too. This folder holds what runs on top of the grid: LDBC BI
on the nine cells, the recorded plan choice, and the checks that meta at an engine-shaped corner does the same work as
that engine's own format.

The code calls the format's current version **meta-v4**: one property encoding (columns,
`codec/columns.h`) and one list encoding (`codec/lists.h`) in every layout. The name is in its stores' key-layout stamp
(`meta-v4-chunk-bytes`, `formats/meta/format.h`), so a store of an earlier version is refused, never reused.

One plan rule everywhere: a query's plan on a cell is the candidate with the fewest ops + bytes / 4 KB on the held-out
params 11-15, kept only if it is more than 10% cheaper than the default plan; the cost is reported on params 1-5. BI has
no held-out params, so BI is picked in-sample, and every BI output says so.

## Run

```bash
uv run kvcost measure --dataset ldbc=0.1 --workload i01 Sp-Sp Co-Co Ch1k-Ch1k Ch4k-Ch4k Ch16k-Ch16k Ch64k-Ch64k Ch256k-Ch256k   # the Sp-Sp Co-Co Ch1k-Ch1k Ch4k-Ch4k Ch16k-Ch16k Ch64k-Ch64k Ch256k-Ch256k stores: LDBC reads (params 1-5 and 11-15) and writes
uv run kvcost compose --dataset ldbc=0.1                         # the nine from them
uv run kvcost measure --dataset ldbc=0.1 --workload i01 Sp-Sp Co-Co Ch64k-Ch64k Sp-Co Sp-Ch64k Co-Sp Co-Ch64k Ch64k-Sp Ch64k-Co    # all nine cells measured (the gates, timing every cell's store)
experiments/meta/run-bi-cells.sh --sf 0.1 --stage counts    # BI on the nine cells, then the grid check
experiments/meta/run-bi-cells.sh --sf 0.1 --stage table     # per (query, cell) the pick beside the planner's body
experiments/meta/run-bi-cells.sh --sf 3 --stage latency     # steady and cold BI latency (tmux; it measures)
run/meta-vs-formats.sh                                      # meta at each engine corner against that engine's format
```

An LDBC grid needs `--held-out` (it also measures params 11-15, the set the plan rule picks on) or `--in-sample` (it
prices on the reported params, and every output says so); with neither, the driver refuses at the start.

**The smallest run** is the first line. It took 126 s on a 16-core AMD EPYC 7302P with a SATA SSD with the sf0.1 stores
already built, and 5 min 27 s on a 40-core node on hard disk from a fresh checkout, ingest included. It
ends with the grid check (`tests/gates/check_grid.py`); a pass looks like this:

```
cells: 9  coverage holes (cell, query with no valid plan on every param): 0
pick rule in force: recorded (...)
validity: 171/171 (cell, query) chosen plans hold their validity caps
candidate drift vs HEAD's plan files: 9 cells, 0 (cell, query) with drift
answers: 95/95 (query, param) identical across all 9 cells
  cheaper than the planner's pick in props_chunked_adj_chunked: ic12 ...->default
RESULT: PASS (full coverage; same answers everywhere; every step's cost follows its own component)
```

- `pick rule in force: recorded` is expected below SF3, also with `--held-out`. The plan choice is decided once, at
  the largest reported scale (SF3), and recorded in `inputs/picks/picks.csv`. A smaller scale prices each query at
  that recorded plan. Its own held-out params check the plan: if it is more than 10% worse than the cheapest one there,
  the query takes this scale's own recorded pick, or the check fails. `held-out 11-15, margin 0.1` shows only where the
  record has no row (at SF3 itself, or a query it does not hold).
- The `cheaper than the planner's pick` lines are information, not warnings. Each names a (cell, query) where the plan
  rule chose another body than the one the evaluator's planner would select (`planner->chosen`). The cost is priced at
  the chosen body. Only the `RESULT` line decides.

BI output: `output/captures/ldbc/sf<SF>/bi/reported/{counts,steps,parts}/<cell>.csv`, and the pick table
`output/summary/ldbc/sf<SF>/bi-cells/bi_picks.csv`; latency (`--stage latency`) in `<root>/timings/ldbc/sf<SF>/bi/`. The archived grids are
`sf3/meta-grid-20260929`, `sf30/meta-grid-20261002` and the re-measure `sf3/remeasure-20261012` (`just fetch-results`).

## What is here

| file | what |
|---|---|
| `run-bi-cells.sh` | LDBC BI on the nine cells: `counts` (one evaluator server per cell, its memory capped, then the grid check), `table`, `latency` (steady with a bounded cache, then cold, direct I/O), `check`; `--chunked-size` and `--spec-dir` for other spec sets |
| `bi_cells_table.py` | per (query, cell) the plan BI is priced at, beside the planner's own body; a pick less than 10% cheaper is marked `inside_margin` |
| `check_bi_chunk_answers.py` | BI on a Chunked cell at another chunk size gives the same answers as at 64 KB: every run ok, the same bodies, the same result hash and rows |
| `check_candidate_drift.py` | does a grid capture still hold the candidate plans the plan files describe; fails on a candidate never run or a captured body now refused (the layout-choice driver runs it first) |
| `check_meta_vs_formats.py` | meta against the separate engine formats (LDBC or the writes workload): the same answers everywhere, and at each engine-shaped corner the same work per step unless a listed difference allows otherwise (`run/meta-vs-formats.sh` runs it) |
| `grid_pick_record.py` | the grid's recorded plan choice: per cell and query, the pick under the held-out rule at the largest scale, written to `inputs/picks/picks.csv`; `--check` compares a capture with the record |

For a change that renames, drops or merges candidates and must move no number: capture the grid before and after it
(`kvcost measure ... --fresh`, the same scale), then `tests/gates/meta/check_grid_same_picks.py --before DIR --after DIR`
compares the two by body: every run's counts, the valid bodies and the pick of every (family, cell, query);
`--dump-picks` in the tree before the change and `--picks-before` compare the picks across the two code states, and
`--plant` is its control. At sf0.1 the two grid captures take about 11 min each, the comparison under a minute.

## Expected run time

The paper's counts, the seven measured layouts (Sp-Sp, Co-Co, ChK-ChK at 1, 4, 16, 64, 256 KB). From the paper's run records (`uv run --locked python -m kvevaluator.measure.run_costs --paper-results`, after `just fetch-results`; each record's `wall_s`, per store, with stores side by side); on
28-core Intel Xeon Gold 5512U nodes (SF30 BI also a 32-core AMD EPYC 9354P), binaries to 11-05 (the
writes 11-14):

| run | SF3 | SF30 |
|---|---|---|
| store ingest, per layout | 294 s median, 408 s max (Sp-Sp); 36 min for the seven | 3,089 s median of the 2 recorded (Sp-Sp 3,686 s) |
| Interactive reads, reported params, per layout | 181 s median, 272 s max; 22 min for the seven | 583 s median, 1,021 s max (Ch1k-Ch1k) |
| Interactive reads, held-out params, per layout | 132 s median, 312 s max; 22 min for the seven | 619 s median, 1,053 s max |
| writes, per layout | 5 s median, 17 s max | 18 s median, 81 s max |
| microbench, per layout | 137 s median, 174 s max; 16 min for the seven | 913 s median, 1,168 s max (Sp-Sp) |
| BI (b01), per layout | 3,876 s median, 4,387 s max (Ch1k-Ch1k); 7.1 h for the seven | Ch1k-Ch1k 50,905 s (14.1 h), Ch256k-Ch256k 34,015 s; the other five records reused rows of an earlier run and hold only their last pass (543-1,555 s); that earlier run's whole cells took 28,475-35,655 s (7.9-9.9 h) each, for the four with a recorded time |

The heavy step is BI: at SF30 a cell takes 8-14 h (Ch1k-Ch1k the longest), most of it BI1 and BI12; the seven's
records sum to 24.8 h only because five of them hold a last pass.

BI at SF3 (32-core AMD EPYC 9354P CloudLab nodes, 2026-10-19/20, walls from each run's records): the counts on the nine cells once took
45,213 s (12.6 h; 4,759-5,343 s per cell); the latency on four cells, steady (307 MiB cache) and cold at params 1-2,
6,504 s (1.8 h, estimated 1.6 h).

Interactive at SF30, steady (3,072 MiB cache, 10 measured + 10 KV-timer + 1 breakdown passes, params 1-5; Intel Xeon
Gold 5512U c6620 nodes, snapshot `sf30/steady-latency-utah6620-20261112`), per host of six or seven stores:
the dataset fetch and check 412-645 s; the record stream 2,987-3,051 s; the ingest, 2 stores at a time, 8,313-10,434 s
(2,485-3,235 s per store); a counts capture (`kvcost measure --dataset ldbc=30 --workload i01 Sp-Sp Co-Co Ch1k-Ch1k Ch4k-Ch4k Ch16k-Ch16k Ch64k-Ch64k Ch256k-Ch256k`) 503-1,008 s per cell; the reads
1,059-1,153 s per store (Sp-Sp and Co-Sp 1,920-2,215 s), 2.3-2.5 h per host. Estimated beforehand 7-19 h per host (the
SF3 pass times the SF30/SF3 work ratio); the 10% cache holds these cells' blocks at SF30 (hit rate 1.0 on 10 of the 12
stores), so a pass took 3.2x SF3's, not 8-11x. The writes after them (A and B, 3 KV-timer passes, the share's A and B;
`run-w16-mix-latency.sh --stage writes`): 2.4-2.7 h per host, estimated 1.5-3 h.
