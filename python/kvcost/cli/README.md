# kvcost: the KV cost of a workload on each layout and mix, one command per stage

`kvcost` takes a workload from its plans to a checked mix, one command per stage. The paper's figures run these same
commands (`run/reproduce.py`; the top README, "Reproduce the paper").

- **run it:** from the repo root as `uv run kvcost` (installed by `just setup`); `kvcost -h` lists the commands,
  `kvcost <command> -h` gives each one's flags, the code it calls and what it writes
- **every command takes** the workload (`--workload my.json`: its queries and how often each runs; or an id: i01) and
  the dataset at its scale (`--dataset ldbc=0.1`)
- **every command writes** into the output tree (`python/kvevaluator/output_tree.py`), into its kinds and nowhere else:
  `captures/`, `composed/`, `stores/`, `timings/`, `summary/<dataset>/<scale>/`

**On this page:** [The walkthrough](#the-walkthrough) ·
[The commands behind the paper's figures](#the-commands-behind-the-papers-figures) ·
[Where each command writes](#where-each-command-writes) ·
[Names and flags every command shares](#names-and-flags-every-command-shares) ·
[Try a benchmark small first](#try-a-benchmark-small-first) · [Fetch the data first](#fetch-the-data-first) ·
[What each command does](#what-each-command-does) · [The cost models](#the-cost-models) ·
[The checks behind the answers](#the-checks-behind-the-answers) · [Expected run time](#expected-run-time) ·
[Adding a benchmark](#adding-a-benchmark)

## The walkthrough

From a workload to its best mix, checked and timed: IC7 and IC2, each once, at SF0.1 (~4 min, the stores included).
The full walkthrough, with what each step shows: [docs/walkthrough.md](../../../docs/walkthrough.md).

```bash
just fetch-dataset ldbc=0.1      # once
source .venv/bin/activate        # puts kvcost on the PATH
W=inputs/workloads/ldbc/examples/ic7_ic2.json
kvcost generate-plans  --workload $W --dataset ldbc=0.1                              # each query's plans
kvcost measure         --workload $W --dataset ldbc=0.1 Sp-Sp Co-Co Ch64k-Ch64k      # build 3 stores, count their KV work
kvcost compose         --dataset ldbc=0.1                                            # the other 6 layouts, from the 3
kvcost cost-layouts    --workload $W --dataset ldbc=0.1 --cost-model wops_bytes4k    # the cheapest: Co-Ch64k, 396.59
kvcost search-mix      --workload $W --dataset ldbc=0.1 --cost-model wops_bytes4k    # the best mix: 312.245, PROVEN
kvcost check-mix       --workload $W --dataset ldbc=0.1 mix:best                     # build it, measure it: PASS
kvcost measure-latency --workload $W --dataset ldbc=0.1 Co-Co mix:best --cache-mb 1  # time the mix against Co-Co
```

- `--cache-mb 1`: at SF0.1 the default cache holds all of IC7 and IC2, and a timed run that reads nothing is refused
- a query measured once on a layout is reused by every later run, so adding a query measures that query alone
- `tests/gates/check_walkthrough.py` runs these commands and checks the answers above

**What each step wrote**, in the output tree:

```
captures/ldbc/sf0.1/interactive/{reported,held-out}/   measure: the counts of Sp-Sp, Co-Co, Ch64k-Ch64k (and check-mix's mix)
composed/ldbc/sf0.1/interactive/{reported,held-out}/   compose: the six other layouts (each run record: its wall_s
                                                       and the measured files it was made from)
stores/ldbc/sf0.1/                                     measure, check-mix: one store per layout
summary/ldbc/sf0.1/layout-costs/ic7_ic2/measured/      cost-layouts: query_costs.csv, query_parts.csv, layouts.csv
summary/ldbc/sf0.1/mix-search/ic7_ic2/                 search-mix: mixes-of-Sp-Co-Ch64k.json (the row), .csv, mixes/
summary/ldbc/sf0.1/built-mixes/mix_<sha>/              check-mix: the mix as built (named by its content), checks.jsonl
timings/ldbc/sf0.1/interactive/meta-<layout>/steady-<MB>mb-p2/  measure-latency: the raw timings
summary/ldbc/sf0.1/workload-latency/ic7_ic2/           measure-latency: summary.csv
```

**More on the same workload:**

```bash
kvcost cost-layouts --workload $W --dataset ldbc=0.1 --mix "knows lists=Ch64k" --mix-base Co-Sp \
    --mix-name knows-chunked                              # a hand-written mix priced from the parts: its rows, kind mix
kvcost list-parts   --workload $W --dataset ldbc=0.1 [--by-direction]       # the parts a mix string can name
kvcost plot-costs   --workload $W --dataset ldbc=0.1 [Sp-Sp Co-Ch64k knows-chunked]
                                                          # per query over Sp-Co, and the totals
```

- a query's capture is keyed by the store, the plan, the params and the counting versions the binary states
- `--cost-model` is required wherever a number is made; `--byte-limit` is a multiple of Sp-Co's bytes (`2x`) or a size
  (`68MB`)

## The commands behind the paper's figures

The figure chains (`run/reproduce.py`, one per figure) run the walkthrough's commands on the layouts written out, or on
a named question, at the paper's scale (`--dataset ldbc=3`, `imdb=full`, `linkbench=n1m`).

```bash
kvcost measure          --dataset ldbc=3 --workload i01 Sp-Sp Co-Co Ch1k-Ch1k Ch4k-Ch4k Ch16k-Ch16k Ch64k-Ch64k Ch256k-Ch256k
                                                                           # the seven measured layouts
kvcost compose          --dataset ldbc=3                                   # the other 42 of the 49, from them
kvcost cost-layouts     --dataset ldbc=3 --workload i01 Sp-Sp Sp-Co ... Ch256k-Ch256k   # every model, both chunk views
kvcost search-mix       --question i01-any-size-best --dataset ldbc=3      # one mix question
kvcost build-statistics --dataset ldbc=3                                   # the statistics and the 49's tallies
kvcost predict          --dataset ldbc=3 --workload i01 Sp-Sp Sp-Co ... Ch256k-Ch256k --tier 1|2
kvcost predict-accuracy --dataset ldbc=3 --workload i01 Sp-Sp Sp-Co ... Ch256k-Ch256k
kvcost measure-latency  --dataset ldbc=3 --workload i01 Sp-Sp Co-Co Ch64k-Ch64k Sp-Co Co-Sp Ch1k-Ch1k Ch16k-Ch16k \
    Ch4k-Ch4k Ch256k-Ch256k Ch1k-Ch4k Ch1k-Ch16k Ch4k-Ch16k mix:i02-any-size mix:i02-predicted \
    [--drift-control Sp-Co]
```

**The layouts**
- the seven measured: Sp-Sp, Co-Co and ChK-ChK at 1, 4, 16, 64 and 256 KB
- the 49: every (properties, adjacency) pair of those seven settings; `run/reproduce.py` writes all 49 out
- `compose` writes the ones not measured, `cost-layouts` costs them (each measured one checked against its own
  capture), `predict` predicts them from the statistics

**Mix questions**
- a question is one search the paper reads, written once in `experiments/mix_benefit/mix_questions.json`
- `search-mix --question NAME` runs its calls and writes to `summary/<its dataset>/<its scale>/mix-search/<rows>/`; a
  `-larger` question searches the dataset's second scale (`--dataset ldbc=3,ldbc=30`)

**Predict's two tiers**
- `--tier 1`, the default: a coarser summary of the statistics, into the set `predicted-tier1`; a reader that names no
  set reads it
- `--tier 2`: the full statistics, into `predicted-tier2`, read when named

**measure-latency: one timing rule, the paper's**
- each query runs at the body `inputs/latency-picks/<dataset>/<scale>/<family>.json` names for its layout; the cost axis
  comes from the timed runs' own counts
- a layout the file does not cover (a mix, another chunk size) is counted first; `--validate-count` counts every layout
- each store steady at `inputs/contract/store_config.json`'s cache (`--cache-mb`), the page cache dropped first, every
  server on one NUMA node (`run/latency-cells.sh`); without passwordless sudo the run says so and its records are marked
- after a plan change: the same command with `--save-picks` (counts and writes the file, times nothing), then without
  it; `git diff inputs/latency-picks/` shows what moved

## Where each command writes

`<ds>/<sc>` is the dataset and scale as the output tree names them (`ldbc/sf0.1`, `imdb-rows/full`, `linkbench/n1m`);
`<id>` is the workload's id (a workload file's `id`, else its `name`).

| command | writes |
|---|---|
| `generate-plans` | the query's plan file under `python/kvevaluator/workloads/<set>/plans/`; nothing in the tree |
| `measure` | `captures/<ds>/<sc>/<query set>/<params>/`; `stores/<ds>/<sc>/` |
| `build-store` | `stores/<ds>/<sc>/` |
| `compose` | `composed/<ds>/<sc>/<query set>/<params>/` |
| `cost-layouts` | `summary/<ds>/<sc>/layout-costs/<id>/<count source>/` |
| `list-parts` | nothing |
| `plot-costs` | `plots/` beside the cost-layouts rows it draws |
| `search-mix` | `summary/<ds>/<sc>/mix-search/<id>/` (`--count-source <set>`: `<id>-<set>/`) |
| `check-mix` | `summary/<ds>/<sc>/built-mixes/mix_<sha>/`; its store in `stores/`, its counts in `captures/` |
| `measure-latency` | `timings/<ds>/<sc>/<query set>/meta-<layout>/<regime>/`; `summary/<ds>/<sc>/workload-latency/<id>/summary.csv` |
| `predict` | `predicted/<ds>/<sc>/<set>/`; the statistics in `stats/<ds>/<sc>/` |
| `predict-accuracy` | `summary/<ds>/<sc>/predict-accuracy/<id>/<source>-vs-<against>/` |
| `record-picks` | `inputs/picks/picks.csv` |

- **the output dir:** `KVEVAL_OUTPUT_DIR`, default `output/`; `--from TREE` names the tree a command reads (default the
  output dir; `paper-results`)
- **another tree:** set `KVEVAL_OUTPUT_DIR` for that call; a driver uses `in_tree <dir> <command>` (`run/env.sh`),
  which keeps the stores, the statistics and the specs shared (`KVEVAL_STORE_DIR`, `KVEVAL_STATS_DIR`,
  `KVEVAL_SPECS_DIR`)

## Names and flags every command shares

- **`--dataset DATASET=SCALE`** is the one way to name a scale: the SF for ldbc and bi (`ldbc=3`), LinkBench's node tag
  (`linkbench=n1m`), IMDb's data tag (`imdb=full`); a workload file may also carry a `"scale"`
- **a layout:** properties-adjacency in short names, Sp Split, Co Condensed, Ch Chunked with its chunk size in KB, any
  size (`Co-Ch4k`, `Ch1k-Ch16k`; the grid's 64 KB is `Ch64k`); `Sp-Co` is Dgraph's layout, `Co-Sp` NebulaGraph's and
  ArangoDB's; a cell id (`props_condensed_adj_chunked`) or a spec file works too; on `job`, `dgraph`, `nebula` and
  `arangodb` are layouts as well (measured only)
- **a mix:** a spec json (`inputs/specs/mixes/*.json`) given as a layout, or a string, `--mix "knows lists=Ch4k; Person
  props=Co"`, its parts as `list-parts` spells them; `--mix-base` fills the parts it leaves out (without it, such a
  string is refused); a mix is named by its content (`mix_<sha>`), or `--mix-name`
- **`--pick-by`:** the rule that picks each read query's plan (default `wops_bytes4k`); **`--pick-on`:** the params it
  is picked on, `held-out`, the default (LDBC's reads: picked on params 11-15, reported on 1-5; bi, job and the
  microbench: on their reported params), or `in-sample` (the reported params; LinkBench's default, with no held-out set)
- **`--dry-run`** prints what a command would run and runs none (measure, build-store, compose, predict, check-mix,
  measure-latency, `search-mix --question`)
- **what a command prints:** its first line names the workload and the output tree; its last line, starting with `==>`,
  is its answer (the fewest layout, the search's verdict and total, PASS or FAIL); `-v` prints every line

**The benchmarks** (the adapters that price a load):

| adapter | what it prices |
|---|---|
| `ldbc` | LDBC SNB Interactive: the IC/IS reads, the writes, the microbench |
| `bi` | LDBC BI, an optional family of LDBC priced apart (a workload that mixes it with the others is refused) |
| `linkbench` | its reads and writes; `--params log`, the whole request log, by default |
| `job` | the Join Order Benchmark on IMDb |

## Try a benchmark small first

The times are one 32-core machine's, for one diagonal layout on the benchmark's whole query set, store ingest included.

| benchmark | smallest scale | one layout measured | a mix string, e.g. |
|---|---|---|---|
| ldbc | `--dataset ldbc=0.1` | 46-69 s | `"knows lists=chunked"` |
| bi | `--dataset ldbc=0.1` | about 2 min | `"knows lists=chunked"` |
| linkbench | `--dataset linkbench=tiny --params quick` on every command | 12-14 s (38 s with the ingest) | `"link1 lists=chunked"` |
| job | `--dataset imdb=sample1pct` (1% of IMDb's titles) | 8-12 min | `"cast_actor lists=chunked"` |

- **LinkBench tiny: keep `--params quick`** (each request type's first 5 requests) on every command: the default,
  `--params log`, runs past 14 min on one cell, because tiny's generator puts 962k of its links on one node; `--params
  log` is the published setting at `n1m`
- **the parts a mix string names:** a vertex group's properties (`Person props`) and an edge type's lists (`knows
  lists`, or per direction, `knows out-lists`); `kvcost list-parts --workload my.json --dataset D=S` prints them

| benchmark | parts |
|---|---|
| ldbc | props: Forum, Message, Organisation, Person, Place, Tag; lists: containerOf, hasCreator, hasInterest, hasMember, hasModerator, hasTag, hasType, isLocatedIn, isPartOf, isSubclassOf, knows, likes, replyOf, studyAt, workAt |
| bi | the ldbc parts less Organisation, isSubclassOf, studyAt and workAt |
| linkbench | props: Node; lists: link1, link2 |
| job | 12 props (Title, Name, Company, ...) and 22 lists (cast_actor, has_keyword, ...) |

## Fetch the data first

`measure` refuses without its data and names every step still missing. The C++ binary comes first of all (the top
README, "Getting Started").

| benchmark | command | time |
|---|---|---|
| ldbc, bi | `just fetch-dataset ldbc=0.1` | 2-10 s (a 25 MB download, 121 MB on disk) |
| linkbench | `just fetch-dataset linkbench=tiny` (builds LinkBench: network, java, mvn) | 18 s |
| job | `just fetch-dataset imdb=sample1pct` (a 1.26 GB fetch, the sample's reference answers by DuckDB) | about 3 min |

- several in one command: `just fetch-dataset ldbc=0.1,linkbench=tiny,imdb=sample1pct`; `--check` checks them by sha256

## What each command does

`kvcost <command> -h` gives each one's flags, the code it calls and what it writes.

| command | what it does |
|---|---|
| `generate-plans` | prints each query's plans and choice points; writes a builder's plan file when it differs |
| `measure` | counts the workload's query sets on each layout, one store per layout, built when missing; a capture is reused while fresh |
| `build-store` | builds each layout's store |
| `compose` | composes the uniform layouts not measured from the measured diagonals, at any chunk size |
| `cost-layouts` | each query's pick and cost on each layout and mix, every model, both chunk views |
| `list-parts` | the parts a mix string can name |
| `plot-costs` | per query, each layout's cost over a baseline (default Sp-Co), one row per model; the totals |
| `search-mix` | the fewest of one model over mixes, proven by branch and bound; `--byte-limit` within a size; `--front` the wops-bytes front |
| `check-mix` | builds the mix, measures it and compares it with its composed cost: exit 0 PASS, 1 FAIL, 2 not compared |
| `measure-latency` | times the workload on each layout's store: steady (default), `--regime cold` or `--regime warm` |
| `record-picks` | the pick record at the dataset's deciding scale (offline, not per workload) |
| `build-statistics` | the dataset's statistics and the 49's tallies, once per dataset |
| `predict` | predict mode's counts as captures, from the statistics and each layout's tally; no store built or opened |
| `predict-accuracy` | one count source against another (`--count-source predicted-tier1 --against measured`): pick hit, rank, Spearman, spread |

## The cost models

`cost-layouts` writes one place per identity, `summary/<ds>/<sc>/layout-costs/<id>/<count source>/` (`measured`, or a
predicted set): `query_costs.csv` (one row per query, layout and chunk view: the picked body, every model and raw
channel), `query_parts.csv` (per part kind), `layouts.csv` (the totals at the workload's frequencies), `runs.jsonl`. A
run upserts by layout. One writer and reader: `python/kvcost/layout_cost/layout_costs.py`; one definition of the
models: `python/kvcost/layout_cost/cost_models.py`.

| model | what it counts |
|---|---|
| **wops** | G + 1.1 S + 0.01 N of the reads (a write's own reads included) + PUTs + DELs |
| **bytes** | the 7 read lanes (missed keys included) + bytes written |
| **wops_bytes4k** | wops + bytes / 4,096 |
| the bill (`bill_lower`, `bill_upper`, `bill_exact`) | from the unit lanes |

## The checks behind the answers

| check | what it asserts | command |
|---|---|---|
| the walkthrough | the walkthrough's answers (396.59, 312.245, PASS, the mix timed); every file in one of the tree's kinds | `tests/gates/check_walkthrough.py` |
| the example workload | `kvcost predict` and `cost-layouts --count-source predicted-tier1` price and rank the nine | `tests/gates/check_example_workload.py` |
| mix from predicted parts | a mix composed from a predicted set's part rows equals it predicted whole | `tests/gates/check_predicted_mix.py` |
| golden pricing | Sp-Co priced from the frozen capture is byte-identical to the freeze (`just golden-costs`) | `tests/gates/check_golden_costs.py` |
| uniform identity | a mix with every part on one cell's settings prices exactly as that cell measured | `tests/gates/check_kvcost_uniform_identity.py --dataset ldbc --sf 0.1 [--control]` |
| mix from the diagonal | a mix priced from the three diagonal cells equals it from the nine measured | `tests/gates/check_kvcost_mix_from_diagonal.py --dataset ldbc --sf 0.1 --mix "knows lists=Ch64k" --base Co-Sp --grid <nine cells>` |
| no cost cache = cost cache | search-mix gives the same totals and score both ways, both proven | `tests/gates/check_no_cost_cache_equals_cost_cache.py --dataset ldbc --sf 3 --from <grid> --workload inputs/workloads/ldbc/b01.json --combine-queries sum --combine-queries geomean` |
| no cost cache = brute force | the solve equals a brute force over every assignment on planted problems | `tests/unit/check_no_cost_cache_solve.py --cases 300` |
| negative controls | six planted defects, each caught | `tests/gates/check_kvcost_controls.py --sf 0.1 --grid <nine cells>` |
| composing again | a composed capture is kept when byte-identical, replaced when today's code composes another | `tests/unit/check_compose_install.py` |

The composer's own check: `compose_cells.py --check` (composed against measured on every workload).

## Expected run time

| command | time |
|---|---|
| **measure** | at sf0.1: the nine LDBC cells with their held-out params 347 s, the microbench 82 s, BI about 115 s per cell; I01 on the seven, every capture reused: 4 s |
| **compose** | at sf0.1, about 3 s for LDBC's query sets |
| **cost-layouts --mix, plot-costs, list-parts** | at sf0.1 on I01: 3.6 s, 3.6 s and under a second |
| **search-mix** | at SF3 (I01, 16 cost cache jobs), 3-10 s per question |
| **search-mix --front** | at SF3, I01 per type: the complete front (1,588 points, each proven) and a 44-limit staircase in 79 s; per type at any size, the 104-limit staircase 572 s, all proven |

The measuring tools' own cost: `experiments/meta/README.md`, "Expected run time".

## Adding a benchmark

1. Write an adapter, `python/kvcost/cli/workloads/<name>.py`, beside the four existing ones, and add its name to
   `ADAPTERS`; the module's fields are listed in `workloads/__init__.py`'s docstring. The measuring is `kvcost
   measure`'s, the same for every dataset.
2. Add what any grid benchmark needs:
   - `grid_capture.WORKLOADS`, `SCHEMAS` and `query_params` (`python/kvevaluator/measure/grid_capture.py`);
   - `benchmarks.FAMILIES`, `workloads()` and `queries()` (`python/kvcost/layout_cost/benchmarks.py`);
   - `candidates.workload_queries` (`python/kvevaluator/plan_choice/candidates.py`);
   - for compose, the composer's `--dataset`.
