# layout_choice: which layout fits which workload, mixed layouts included

Analysis only, except `run-w16-mix-latency.sh`: every other script reads grid captures and opens no store.

## The question

The configurable format `meta` has nine **uniform** layouts (vertex properties x adjacency, each Split, Condensed or
Chunked), measured by `kvcost measure` and composed by `kvcost compose` (`../meta/README.md`). Here we ask two things:

1. For a **workload** (read and write queries with frequencies: 15 designed ones, W01-W15, one file each in
   `inputs/workloads/ldbc/`; I01 is LDBC SNB Interactive's own mix and I02 its reads only;
   LinkBench's L01-L04) which uniform layout is cheapest, in total ops and in total bytes?
2. Does a **mixed** layout beat every uniform one? Per type: vertex properties chosen per vertex type, adjacency per
   edge type. Per type and direction: adjacency also per direction.

## How it works

- **Pricing by part.** The C++ evaluator charges each KV op to the store part its key names (one vertex type's
  properties, one edge type's lists in one direction, an index), and the parts sum exactly to the step. A step of a
  plan touches one part, so its cost depends only on that part's layout. A mixed layout is therefore priced without
  building it: each part's cost comes from a measured uniform cell that gives that part the same layout
  (`python/kvcost/layout_cost/compose.py`). A query keeps one plan across its parts, chosen by the same rule as on a
  uniform layout: fewest ops + bytes / 4 KB on the held-out params 11-15, kept only if it is more than 10% cheaper
  than the default plan; the cost is reported on params 1-5.
- **The search is exact.** A mix's cost is a sum over queries, and each query depends only on the parts it touches. The
  search prices each query once for every assignment of its own parts, then runs branch and bound over the parts
  (`python/kvcost/layout_search/search.py`).
- **The check.** `meta` can also build a mixed store. Built mixes are measured and compared with their composed cost
  (`tests/gates/check_mix_vs_built.py`, `kvcost check-mix`; the one comparer is `python/kvcost/layout_cost/composed_vs_measured.py`):
  equal in every pick and every step row, except the bytes of a missed seek's landing key, which can belong to a
  neighbouring part: per workload they stay within 0.5x-2x of the composed.

The currencies: ops are weighted G : S : N : P : D = 1 : 1.1 : 0.01 : 1 : 1; a write's own reads count as write cost.
Total bytes = bytes read + bytes the writes read first + bytes written.

The results are in the paper's "Choosing the best KV Format" section and its Evaluation.

## Run

```bash
kvcost measure --dataset ldbc=0.1 --workload i01 Sp-Sp Co-Co Ch1k-Ch1k Ch4k-Ch4k Ch16k-Ch16k Ch64k-Ch64k Ch256k-Ch256k             # measure the Sp-Sp Co-Co Ch1k-Ch1k Ch4k-Ch4k Ch16k-Ch16k Ch64k-Ch64k Ch256k-Ch256k first (tmux at sf3)
kvcost measure --dataset ldbc=0.1 --family microbench Sp-Sp Co-Co Ch1k-Ch1k Ch4k-Ch4k Ch16k-Ch16k Ch64k-Ch64k Ch256k-Ch256k      # ... and their microbench reads
kvcost compose --dataset ldbc=0.1                                   # the other six of the nine
experiments/layout_choice/run-layout-choice.sh --sf 0.1   # all the analysis -> output/summary/ldbc/sf0.1/layout-choice/
experiments/layout_choice/run-layout-choice.sh --sf 3 --grid paper-results/composed/ldbc/sf3    # the paper's SF3 (paper-results/summary/ldbc/sf3/layout-choice/)
experiments/layout_choice/run-layout-choice.sh --sf 30 --grid paper-results/composed/ldbc/sf30   # SF30 (below)
experiments/layout_choice/run-layout-choice.sh --dataset linkbench --scale n1m --params log
```

**The smallest run** is the first three lines (stores built on the first): the grid 126 s, its microbench reads 83 s,
the analysis 228 s on a 16-core AMD EPYC 7302P with a SATA SSD. On a 40-core node on hard disk the grid
took 5 min 27 s and the microbench 1 min 37 s from a fresh checkout. The analysis there stopped, after 1 min 55 s, at
the why-text check that has been fixed since; it was not timed again.

At SF3, the `--sf 3` line on the SF3 grid (since measured again; today's is in `paper-results/`) took 477 s
on the 16-core EPYC 7302P. The
built-mix check skips the one built mix whose vertex properties are grouped, by name
(`people_hot_cold_messages_w16_fewest_bytes`): its price needs the grouped props options, which the search does not offer. Its spec
is in `inputs/specs/retired/`, so a run builds the other three; the SF3 captures hold it. A pass at any scale ends with
`[run-layout-choice.sh] DONE (sf<SF>, <N>s)`, after these lines (SF3's counts; sf0.1's below):

```
candidate drift (<grid>): 48 (cell, workload) captures checked against HEAD's plan files; 0 (cell, workload, query) with drift -- PASS
uniform check (the 9 cells): PASS
measured vs composed: PASS                                                       # the built mixes
why texts: 15 checked, 60 rendered from committed snapshots, 0 problems
```

At sf0.1 the drift line counts 18 captures, and the built-mix line is `check_mix_vs_built SKIPPED (Result 3 is not
checked at sf0.1)` in place of `measured vs composed: PASS`.

**SF30.** The SF30 numbers (the paper reports none) are `paper-results/summary/ldbc/sf30/layout-choice/reported/wops_bytes4k/`:
the `--sf 30` line above, on the nine 64 KB uniform layouts composed from `paper-results/captures/ldbc/sf30/`. The older
SF30 analysis is not in `paper-results/`. Its grid is refused by this driver, with or without
`--allow-candidate-drift`. It predates the held-out rule (it has no params 6-10 and no `measure_only` column), and 12
queries have plan bodies at HEAD it never ran (108 (cell, workload, query) at step 0). Re-measuring all nine is
`kvcost measure --dataset ldbc=30 --workload i01 Sp-Sp Co-Co Ch64k-Ch64k Sp-Co Sp-Ch64k Co-Sp Co-Ch64k Ch64k-Sp Ch64k-Co` and the same with `--family microbench`, about
16-21 h on a 32-core AMD EPYC 9354P CloudLab node with NVMe, then the line above on that grid.

`run-layout-choice.sh` first checks that the capture holds the candidate plans the plan files describe
(`../meta/check_candidate_drift.py`), then runs: price every workload on the nine cells; check the built mixes; the write:read ratio sweep; one folder per workload with its story, options and figures;
the byte-budget picks; the bill (DynamoDB's billing rule) and the equal-bytes front as side chains; the figures across
workloads. It reads the captures in `output/captures/ldbc/sf<SF>/` (or `--grid`). Its output,
`output/summary/ldbc/sf<SF>/layout-choice/<params>/<rule>/`, has a `README.md` that indexes the workloads, and one
folder per workload (`w<NN>-<r|rw>-<name>/`). Every analysis step keeps the raw channels (S, G, N, P, D and each byte
lane) per option x query x part in `breakdown.csv`, so a new view needs no re-pricing.

The other drivers:

```bash
experiments/layout_choice/run-w16-mix-latency.sh --sf 0.1 --stage counts   # I01's picked mixes built and TIMED (measures)
```

The latency-vs-cost figure's runs: one `kvcost measure-latency` per workload (below), into the output tree's
`timings/<dataset>/<scale>/<family>/` and `summary/<dataset>/<scale>/grid-latency/<family>/`, where
`paper/derive/latency_steady_ldbc.py` and `latency_steady_job.py` read them (`--from output --dataset ldbc=S`): the 12
layouts timed steady at the bodies of `inputs/latency-picks/` (no counts; `--validate-count` counts them too), the
writes after each store's read warm-up, and the mixes the mix questions' rows hold, counted first (`--dry-run` prints
the steps):

```bash
# the latency figure, per workload (i01, b01; j01 with --dataset imdb=full): the timed mixes from their mix questions'
# rows, then the mixes counted (kvcost measure), the 12 layouts and the mixes timed steady, I01's writes
kvcost measure-latency --dataset ldbc=3 --workload i01 Sp-Sp Co-Co Ch64k-Ch64k Sp-Co Co-Sp Ch1k-Ch1k Ch16k-Ch16k Ch4k-Ch4k Ch256k-Ch256k Ch1k-Ch4k Ch1k-Ch16k Ch4k-Ch16k \
    mix:i02-any-size mix:i02-predicted     # I01 writes: its writes timed too; --dry-run: the steps and the body each store times per query
# BI: --workload b01 <the 12> mix:b01-spco-share mix:b01-predicted;
# JOB: --dataset imdb=full --workload j01 <the 12> mix:j01-spco-share mix:j01-predicted --drift-control Sp-Co
#   (Sp-Co again at the end: the drift control)
# mix:b01-spco-share and mix:j01-spco-share take their bill mix from the same rows: ask the bill question first
#   (kvcost search-mix --question b01-spco-share-bill, --question j01-spco-share-bill); without it the bill mix is
#   skipped, and named
# --rocksdb-share adds KV-timer passes and the RocksDB share per store (not in the paper)
```

The inputs and outputs the paper reads (`just fetch-results`), in `paper-results/`: the analyses
`summary/ldbc/sf3/layout-choice/reported/` (priced from `captures/ldbc/sf3/` and `composed/ldbc/sf3/`) and
`summary/ldbc/sf30/layout-choice/reported/` (from `captures/ldbc/sf30/` and `composed/ldbc/sf30/`)
(`../mix_benefit/README.md`). Earlier layout-choice runs are not in it.

## Paper outputs

`paper/derive/layout_choice.py` (the W14 table, the byte-budget table and the I01 sentences, from
`summary/ldbc/sf30/layout-choice/reported/wops_bytes4k/`; the SF3 budget rows from
`summary/ldbc/sf3/layout-choice/reported/wops_bytes4k/`), `paper/fig_parts_explain.py` (why a mix can pay, part by part,
from `summary/ldbc/sf3/layout-choice/reported/wops_bytes4k/`) and bill-i01 (I01 under the bill:
`paper/derive/bill_i01.py` makes its table from the bill stage's output in
`summary/ldbc/sf3/layout-choice/reported/wops_bytes4k/bill/`, and the mix's row from `summary/ldbc/sf3/mix-search/`;
`paper/draw/fig_bill_w16.py` draws it), each in `paper-results/`.

## What is here

| file | what |
|---|---|
| `run-layout-choice.sh` | the layout choice over a grid capture, every stage above; `--dataset ldbc\|linkbench`, `--workload`, `--grid`, `--jobs`, `--fresh` |
| `kvcost measure-latency ... Sp-Sp Co-Co Ch64k-Ch64k ... Ch4k-Ch16k` | the latency-vs-cost figure's runs of one query set: the 12 layouts (and the mixes the run's search picked), timed steady at the picks file's bodies (the mixes counted first; `--validate-count` counts the 12 too: the seven measured, the five others composed), and the writes, into the tree's timings/ and summary/<dataset>/<scale>/grid-latency/ the figure's derives read (python/kvcost/cli/latency.py) |
| `run-w16-mix-latency.sh` | I01's picked mixes built and timed beside the nine uniform layouts, one stage at a time: `specs`, `counts`, `latency`, `ic10-bodies`, `writes`, `score`, `plot`; its `writes` stage is the write step of `kvcost measure-latency --workload i01 Sp-Sp Co-Co Ch64k-Ch64k ... Ch4k-Ch16k` (the paper's latency figure; I01 writes) |
| `cost_workloads.py` | every workload on each of the nine uniform cells, from the grid captures -> `workload_costs.csv` |
| `workload_story.py` | per workload: every option (the nine uniform layouts and the exact solves of each option set) with every measure (`options.csv`), each query's cost split by store part (`breakdown.csv`), and the workload's README |
| `plot_workload_story.py` | the per-workload figures (parts, breakdowns, the options map and its choice matrix) from `workload_story.py`'s CSVs |
| `why_numbers.py` | fills the numbers in a workload's "why" text from that workload's own priced data |
| `check_why_numbers.py` | its check: no plain number in a why text unless declared scale-free, and every text renders from every committed snapshot and, with `--sf`, from that scale's fresh run (the driver passes it, after workload_story) |
| `sweep.py` | scale a workload's writes by r (0.01 to 100 writes per read): each uniform layout's totals and the per type mix solved at each ratio -> `sweep.csv` |
| `plot_sweep.py` | the ratio sweep's figures |
| `budget.py` | the byte-budget pick per workload: the fewest total ops among the layouts whose total bytes are within B x Sp-Co's, the reference (B = 1, 2, 3, 5) -> `budget_picks.csv` |
| `budget_sweep.py` | the byte-limit sweep: at about 40 limits per workload, the fewest ops of the best uniform layout and of the best per type mix -> `budget_sweep.csv` |
| `plot_budget_sweep.py` | the byte-limit figures (`budget-gain`) from `budget_sweep.csv` and `options.csv` |
| `equal_bytes_front.py` | the equal-bytes front: at each byte limit, the fewest ops of the uniform layouts and of the exact mix that fits, plus each built mix as a point |
| `plot_equal_bytes_front.py` | the front's figure per workload: weighted ops against the byte limit |
| `chunk_size_front.py` | the equal-bytes front when the chunk size is part of the choice: each part takes Split, Condensed, or Chunked at one of several sizes (`--must-equal`: its 64 KB line against an equal-bytes front taken against the same reference; another reference is refused) |
| `pick_margins.py` | per (layout, LDBC read query) of the nine cells: the plan the pick rule keeps, its challenger on the held-out params and how far it is from flipping (`pick_margins.csv`; `check_cross_benchmark_numbers.py` reads the pick) |
| `check_front_must_equal_reference.py` | `--must-equal`'s check: two fronts taken against different references are refused (synthetic rows, no captures; in `just tests-fast`) |
| `check_front_against_complete.py` | that front's control: its 64 KB line equals the complete 3-setting front's fewest-wops mix within each limit (`--plant` is the control) |
| `chunk_size_choice_by_part.py` | why a part takes its chunk size in that front's mix: per part and byte limit the setting, its share, and two rules scored against the exact mix |
| `check_chunk_size_mix.py` | a built mix whose parts take different chunk sizes, against its composition (`spec` writes the spec, `check` compares row by row; `--plant` is the control) |
| `plot_cross_workload.py` | the figure across workloads: each uniform layout's cost over the workload's best, in ops, bytes and ops + bytes / 4 KB |
| `plot_measured_cost_by_query.py` | the measured cost of every query of a workload on the uniform layouts, one strip per query, banded by the workload's own GROUPS (`--family`), in wops, bytes and wops_bytes4k, per run (request) or (`--over-best`) over the query's cheapest layout, with each query's share of the workload's runs and cost above the top panel; from a layout_choice `by_query.csv` (the 49 layouts at SF3 / SF30; `--same-measured`: another file's measured columns must equal) or per_query_disagreement's `costs.csv` with `--family` (LinkBench n1m: the nine grid cells); `--scale` and `--mix` name the data and the weights in the title and caption |
| `bill_search.py` | the exact best per type mix under DynamoDB's request units, beside the nine uniform layouts, in a lower, upper and exact form |
| `plot_bill.py` | the bill figure: the nine uniform layouts and the mix with the smallest bill, one panel per chunk size |
| `sum_search.py` | the exact best mix under ops + bytes / 4 KB, with no byte limit |
| `joint_search.py` | one per type mix for several workloads at once: an exact joint solve, and the regret table |
| `cross_benchmark_regret.py` | the nine uniform layouts across benchmarks (LDBC, BI, IMDb / JOB, LinkBench): each layout's cost over that benchmark's best |
| `search_steps.py` | the exact search timed step by step, one CSV row per step, for the paper's run-time table |
| `sensitivity_linkbench.py` | does the LinkBench ranking survive the modeling choices that favour one layout -> `sensitivity.csv` |
| `pick_specs.py` | one workload's byte-limit picks as buildable `meta` specs, generated from a layout-choice output and checked against its `budget_picks.csv` |
| `w16_latency_score.py` | the I01 mix-latency scorer: per byte limit, is the built mix faster than the best uniform layout within the limit (`noise`, `score`) |
| `write_rocksdb_share.py` | the RocksDB share of the steady writes per store, from the writes stage's KV-timer passes (`--write-kvtimer N`, `after-steady-warmup-<MB>mb-kvtimer-noflush/meta-<store>/run-<k>/`): the reads' rule (`kvcost.latency.steady_table`), IU1-8 at I01's counts with the standard error over the passes, the primitives pooled, the timer passes' wall over B's per query, and with `--reads` the share of I01's reads and updates together (the reads' row computed by `steady_share.store_row` from the read capture that warmed B, so store, scale and cache match); every pass must share B's binary, store, warm-up capture and read bodies; under 1 s, opens no store |
| `check_write_rocksdb_share.py` | its check on planted captures: hand-worked shares, the SE over passes, the I01 share from B's warm-up capture, an incomplete pass left out, an unsettled warm-up counted, a pass of other counts, binary, store, warm-up or bodies refused (`--plant no-timer-cost\|no-pass-identity\|reads-elsewhere` are the controls; in `just tests-fast`) |
| `write_latency_table.py` | the steady write timing per store: per write query (IU1-8, the primitives) the median over params of the apply wall after the steady read warm-up and in the four-leg run, and the IU total at I01's counts over Sp-Co's (the scorer's `class_totals`); under 1 s, opens no store |
| `plot_w16_mix_latency.py` | the I01 mix-latency figure: per byte limit, the mix, the best uniform layout within it and the picks file's reference (Co-Sp for the mixes picked in Co-Sp units), in time, ops and bytes |
| `plot_w16_engine_arms.py` | the named engine formats against their nearest uniform layouts, in the I01 timed run |
| `plot_w16_stores.py` | three store-level views of the I01 timed run, per regime, from the scorer's files |
| `plot_w16_reads_and_updates.py` | I01's time per store: the reads' steady time stacked with the updates' timed apply |

## Expected run time

`run-layout-choice.sh` at sf3 on the grid, `--jobs 24` (32-core AMD EPYC 9354P): 183 s with a cold
mix cost cache, 134 s with it warm. On the SF3 grid (16-core AMD EPYC 7302P):
477 s with `--jobs 12`, the largest steps the two equal-bytes fronts (186 s, 446 s) and the
workload stories (269 s) (a C++ build ran on the machine). At sf0.1: 228 s. (Both runs were before the grouped props
options' retirement, without them.)

The searches alone at sf3: `sum_search.py` on 17 workloads, 31 min (with the grouped options); `joint_search.py`,
217 s; `budget_sweep.py`, 5.4 min; `bill_search.py`, 91 s; `chunk_size_front.py` on I02 with three sizes, 26 min. The 49 uniform layouts of I01 at SF3 (`kvcost cost-layouts --dataset ldbc=3 --workload i01 Sp-Sp Sp-Co ... Ch256k-Ch256k`; python/kvcost/layout_cost/uniform_49.py), 4 s.

`run-w16-mix-latency.sh` measures. At sf3 (2026-10-18/19), 20 stores: counts and ingest 58 min, steady 89 min, cold
11 min, writes 5.5 min, scoring under a minute: about 2.8 h of stages, 55 GB of stores. At sf0.1 every stage together
takes about 20 min.
The latency runs at sf0.1 (Interactive and BI; a 64-thread AMD EPYC 9354P under other jobs, the
clean binary d6c582a2; a 10 MiB steady cache, 3 measured passes and 3 KV-timer passes): Interactive, 12 stores, 556 s
(counts 265 s, steady 167 s, share 3 s, writes 121 s); BI, 12 stores and 2 mixes, 2,192 s (counts 1,087 s with the
mixes' ingest, steady 1,097 s, share 8 s). Its JOB part (IMDb full, on at SF3) has not been run by this driver.

The writes stage's KV-timer passes (`--write-kvtimer N`): each pass is one more B run (its own arena, the read warm-up,
the 75 writes at perf level 2 + the KV timer). At sf0.1 on Sp-Co (Xeon Gold 5512U, load 7 from a build beside it):
8 s per pass (the warm-up 7 s). At SF3 a pass costs about one B run: the SF3 writes take 47-168 s per store
for A and B together, most of it B's warm-up (39-113 s), so N passes on 15 stores take about 15 x N x 1-2 min (N = 10:
about 3-4 h; an estimate, not yet measured).
