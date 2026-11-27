# Workloads: what runs, and how often

One file per workload: the one copy, read by `benchmarks.workloads()` (python/kvcost/layout_cost/benchmarks.py). A
`--workload` id or name finds its file by one rule, `kvevaluator.capture.workload_file.resolve`: an id names one file per
dataset and scale (`tests/unit/check_workload_ids.py`).

| dir | workloads | written by |
|---|---|---|
| `ldbc/` | W01-W15, each chosen so that a different layout should win | hand (these files are the source) |
| `ldbc/` | I01 (LDBC SNB Interactive's own mix; the paper's W16) and I02 (its reads only; the paper's W17) | `make_workloads.py`, from `inputs/frequencies/ldbc/sf3/mix.json` (the scale is the recipe's `W16_FREQUENCIES`; another scale's frequencies are one more recipe under its own id) |
| `linkbench/<scale>/` | L01-L04 (LinkBench's request-log frequencies, and variants) | `make_workloads.py --dataset linkbench --scale <scale>`, from the tracked `inputs/frequencies/linkbench/<scale>/mix.json` |
| `imdb/` | J01 (the Join Order Benchmark's 113 queries, each once) | `make_workloads.py`, from the plan files' query list |
| `ldbc/` | B01 (LDBC BI's 17 queries, each once). BI is an optional family: a load of the LDBC suite leaves B01 out unless asked, or it is passed as a file (`--workload inputs/workloads/ldbc/b01.json`) | `make_workloads.py`, from the plan files' query list |
| `<dataset>/equal-share/` | `ldbc/equal-share/b01.json`, `ldbc/equal-share/i01.json`, `imdb/equal-share/j01.json`: the workload with each query's frequency 1 / its measured combined cost (wops_bytes4k) on Sp-Co at the deciding scale (LDBC SF3; IMDb full), so each query costs 1 on Sp-Co. BI's and JOB's weighting; beside I01's own frequencies, a second view. Each has an id of its own, `<id>-equal-share` (`b01-equal-share`, `i01-equal-share`, `j01-equal-share`), so `--workload b01-equal-share` names it and its runs write places of their own; `query_weighting.equal_share_path(id)` finds it from the workload's id | `python -m kvcost.layout_cost.query_weighting` |
| `ldbc/examples/` | `example` (two example queries, each once: the section below) and `ic7_ic2` (IC7 and IC2, each once: the layout-cost walkthrough's example). Passed as files; a subfolder, so no suite load reads them | hand |

BI's and JOB's own files (`ldbc/b01.json`, `imdb/j01.json`, every frequency 1) are their second weighting, each query once, beside
the equal share: read through `query_weighting.once(wid)`, which refuses a frequency other than 1; a reader names it
(the form `once`), so no copy of them lives elsewhere.

A file holds `id`, `name` (the workload's name in the CSVs), `title`, and `items`, one per line: `family` (the capture
the query was measured in), `query`, `frequency` (how often it runs), `params` (the 1-based param indexes the item
covers; `null` = the mean over all its params). Then what the workload says about itself: `expect` (the prediction,
written before measuring), `why` (what the measured per-part costs show; its `{name}`s are computed by
`experiments/layout_choice/why_numbers.py` from `why_numbers`), `sweep` (the queries `sweep.py` scales), and so on.

A new workload is a new file here: `kvcost search-mix --workload <path.json>` also takes one from anywhere.

The query names a read family offers are its plan files: `ls python/kvevaluator/workloads/<family>/plans/`
(interactive: `ic1`, `is1`, ...; bi: `bi1`, ...; job: `job1a`, ...).

```bash
python3 python/kvcost/layout_cost/workloads/make_workloads.py --check                               # I01, I02, J01, B01 and L01-L04 equal their recipe
python3 python/kvcost/layout_cost/workloads/make_workloads.py --dataset linkbench --scale n1m     # write L01-L04 at n1m
```

## Add your own workload

The example here is two queries on LDBC, a person's friends with their names and a person's first name, priced on the
nine uniform layouts from the data's statistics, with no store built. To add yours, copy its three parts and rename.

Needs: the evaluator built (`just build`; a few minutes the first time, since it builds RocksDB too) and LDBC's SF0.1 data (`just
fetch-dataset ldbc=0.1`; a 25 MB download). Step 2 needs neither.

1. **The queries**, in Python: `python/kvevaluator/workloads/interactive/queries/example.py`. Copy it to a new module in
   the same dir (it is found by name; nothing else registers it). Each function builds one query's plan from steps
   (`read_vertex_by_id`, `read_neighbors`, `top_k`, ...): the IC and IS modules beside it use every step a read query
   has, and `ic1.py` is a longer example. `question` says what the query answers. `params_like="ic1"` makes it run on
   IC1's parameter rows (a person id), so it needs no param file; a query whose parameters are not a person's borrows
   another query's rows the same way. End the module with `LOGICAL`, mapping each query's name to its function, as the
   example does; that plan is also the query's hand plan. A `SIM` entry is needed only for a hand plan that differs
   from the logical one (`ic1.py`).
2. **Their plan files**, generated from the Python: `uv run --locked python python/kvevaluator/plan_choice/dump_planir.py
   --family interactive` writes `python/kvevaluator/workloads/interactive/plans/example_friends.json` and
   `example_first_name.json` (and leaves the other 37 as they are).
3. **The workload**: `ldbc/examples/example.json`, one item per query: `family` is the family (the query set) whose dir holds the query
   (`interactive` for a module in `workloads/interactive/queries/`), `frequency` how many times it runs per run of the
   workload (the costs are summed with these weights). A workload file needs no registering: the commands below take
   its path.

Then predict each layout's cost and rank the layouts:

```bash
uv run --locked kvcost predict --workload inputs/workloads/ldbc/examples/example.json --dataset ldbc=0.1
uv run --locked kvcost cost-layouts --workload inputs/workloads/ldbc/examples/example.json --dataset ldbc=0.1 \
    --count-source predicted-tier1
```

The first `predict` builds the statistics and each layout's tally from the data (about 20 s); later runs take a
second or two. `cost-layouts` prints the nine layouts, cheapest first, in wops_bytes4k (G + 1.1 S + 0.01 N, plus bytes
read / 4096; `--cost-model` prints another), and writes every model into
`output/summary/ldbc/sf0.1/layout-costs/example/predicted/` (query_costs.csv, layouts.csv). A layout is named
properties-adjacency, each Sp (Split), Co (Condensed) or Ch<K>k (Chunked at K KB). On the example, Co-Co is the cheapest:

```
  Co-Co       27.2525
  Co-Sp       27.5776
  Sp-Co       49.4631
  ...
  Ch64k-Ch64k 108.763
```

To measure the same workload on built stores instead of predicting it: its two queries are not among the interactive
family's measured queries, so `kvcost measure` counts them as a slice of its own, in a tree of its own
(`KVEVAL_OUTPUT_DIR=<dir> kvcost measure --workload inputs/workloads/ldbc/examples/example.json --dataset ldbc=0.1
--queries example_friends,example_first_name Sp-Co Co-Co`); `kvcost measure-latency` on it does the same itself. A
workload of the family's own queries is measured in place: `kvcost measure` and `kvcost compose`
(`python/kvcost/cli/README.md`). `uv run --locked python tests/gates/check_example_workload.py` runs the two commands
above and checks their output (the sf0.1 round runs it).

## Run one query

To see what one query costs on one layout, step by step, and what it answers (after `just build` and
`just fetch-dataset ldbc=0.1`). This is not a `kvcost` command: `kvcost measure` writes whole captures for pricing (`--queries
is2` writes a slice of one, into a tree of its own) and prints neither the steps nor the answer; `run_one` prints both
and writes nothing.

    uv run --locked python -m kvevaluator.measure.run_one --sf 0.1 --family interactive --store nebula --query is2 --answer-rows 1

`--store` is a named format (`dgraph`, `nebula`, `arangodb`, `pertype_csr`) or a layout (`Sp-Co`, `Co-Ch4k`, a cell
id such as `props_condensed_adj_chunked`, or a spec file). A store that is not there yet is built first (seconds at
SF0.1). The output, trimmed where `...` stands:

    binary build/cpp/kvevaluator sha 502d32a74957 dirty False
    interactive is2 param 1 {"person": "30786325579101"}
    store <output>/stores/ldbc/sf0.1/nebula (nebula), body one_phase
    step             op                          S      G       N  get_value  scan_value   rows
    me               read_vertex_by_id           0      0       0          0           0      1
    my_msgs          read_neighbors              1      0      13          0          13     13
    ranked           read_vertex_props           0     16       0       1119           0     13
    ...
    roots            read_chase                 10      0       0          0           0     10
    ...
    out              project                     0      0       0          0           0     10
    total                                       21     17      23       1263          23
    answer: 10 rows (first 1)
      {"messageContent": null, "messageCreationDate": 1345226350002, "messageId": 1030792359243, ...}

The first line names the binary that ran: its commit, and whether its tree had uncommitted changes. Each step row gives
its KV operations (S = SEEK, G = GET, N = NEXT), the value bytes it read by point lookup and by scan, and the rows it
handed on; a step with no KV operation (a join, a sort) counts 0.

**Which plan ran.** A query has one or more bodies: plans that give the same answer. The store runs the body the pick
record names for it (`inputs/picks/README.md`); `--body two_phase` runs another. A store with no pick, such as a layout
you just wrote, is refused when the query has more than one body:

    no pick recorded for meta-add3282ade84 / interactive is2: name a body with --body (one of: index_walk, one_phase, two_phase)

Name a body to look at it, or record a pick for the store (`inputs/picks/README.md` gives the command).

**A latency run times one body per query:** the one the pick names for that layout. A steady run refuses a query with two bodies (`cpplatency.check_bodies`; test: `tests/unit/check_cpplatency_bodies.py`), and each timed run's counts must equal the capture's row for that body. The other bodies matter only in the count run, where the pick is made.
