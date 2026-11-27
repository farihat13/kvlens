# linkbench: LinkBench's data and request log, and its whole-log figures

LinkBench (Facebook's social-graph benchmark) as a second workload beside LDBC: its LOAD phase gives the graph, its
seeded REQUEST phase gives a request log (100,000 requests at n1m: 68,994 reads, 31,006 writes) whose request types and
frequencies are the workload. The workload itself (schema, loader, params, plans, the log model) is
`python/kvevaluator/workloads/linkbench/` (its data side: `python/kvevaluator/datasets/linkbench/`). LinkBench scales by node count (`tiny`, `n1m`, `n10m`), not by SF.

## Needs

Beyond the top README's Getting Started, a JDK and Maven, to build the LinkBench tool once. Any JDK from 8 on works: tested with
OpenJDK 21 and with JDK 8, Maven 3.8. On Ubuntu 24.04:

    sudo apt-get update && sudo apt-get install -y openjdk-21-jdk-headless maven

"Patched to Java 8" below means the tool's source and target level (raised from 7, which JDK 21's compiler refuses),
not the JDK it needs.

The smallest run is `run-linkbench.sh --scale tiny`: 134 s on a 16-core AMD EPYC 7302P, with
an empty Maven cache (46 s of it the tool's one-time download and build). A pass prints
`ALL CHECKS PASS (all formats agree)` (its `cpp.csv` holds 630 rows), then `[run-linkbench] DONE (scale tiny)`.

The anchors step prints `degree 961964..0, 1000 nodes total`: one node has 961,964 out-links in a graph of 1,000 nodes.
That is LinkBench's design, not a fault. Its `real` out-degree distribution (`nlinks_func = real`, from Facebook's
measured data in `config/Distribution.dat`) gives each node the degree at its (shuffled) rank, without scaling to the
graph's size, so one node always gets the top value, 961,963, at any scale (node 1 here); the ids its links point to
need not be nodes of the graph.
LinkBench's split over the two link types adds one (`ID2Chooser.calcLinkCount` gives each type `total / 2 + 1`). At
1,000 nodes that one node holds 99.8% of the 963,868 links; at 1M nodes it is the largest hub among 5.25M.

## Run

```bash
just fetch-dataset linkbench=n1m                                 # build the tool, generate the data, log the requests, anchors; then the params: the samples quick, quick-next and log (the whole log), and mix.json, checked against inputs/params/linkbench/n1m/linkbench/params.sha256.txt
experiments/linkbench/run-linkbench.sh --scale n1m               # the data as above, then ingest the three engine formats and count, then the figures
experiments/linkbench/run-linkbench.sh --scale tiny              # a quick smoke run
uv run kvcost measure --dataset linkbench=n1m --workload l01 Sp-Sp Co-Co Ch1k-Ch1k Ch4k-Ch4k Ch16k-Ch16k Ch64k-Ch64k Ch256k-Ch256k  # the Sp-Sp Co-Co Ch1k-Ch1k Ch4k-Ch4k Ch16k-Ch16k Ch64k-Ch64k Ch256k-Ch256k on the whole request log (--params log, the default)
uv run kvcost compose --dataset linkbench=n1m                      # the nine from them
experiments/layout_choice/run-layout-choice.sh --dataset linkbench --scale n1m --params log   # which layout fits LinkBench
```

The tool (facebookarchive/linkbench, patched to Java 8, with LinkBench's wall-clock reads replaced by one fixed clock so
two generations give the same bytes) is built once into `build/linkbench/` (or `$LINKBENCH_HOME`); it needs Java
and Maven. Data: `<data dir>/datasets/linkbench/<scale>/data/` with its `manifest.json` (tool commit, seeds, clock, per file lines
and sha256); stores under `output/stores/linkbench/<scale>/`. The wide-read capture (`run-linkbench.sh`) is its own output
tree, `output/studies/linkbench-wide-read/captures/linkbench/<scale>/`, its figures in `output/figures/linkbench-wide-read/<date>/`;
the whole-log readers take `--scale` and the timings' `--regime` (`output/timings/linkbench/<scale>/linkbench/meta-<cell>/<regime>/`).
The archived n1m capture on the nine layouts is
`linkbench/n1m/remeasure-20261012` (`just fetch-results`).

## What is here

| file | what |
|---|---|
| `run-linkbench.sh` | the pipeline at one scale: the data (`just fetch-dataset linkbench=<scale>`), the C++ capture of getNode / getLinkList on Dgraph, NebulaGraph and ArangoDB, the figures |
| `../../python/kvevaluator/datasets/linkbench/` | the data's own code: `generate.sh` (build the tool, generate the data, the logged request phase, anchors), `data_manifest.py` (what made a data dir, and its checksums), `FileDumpGraphStore.java` and `LinkBenchClock.java` (copied into the tool's source before the build), `fetch.py` (the dataset's fetch step) |
| `extract_clock_trace.py` | recovers a generation's clock from its data: a wall-clock run against a fixed-clock run of the same seeds, line by line; writes the trace and its `.json` (lines and sha256 per file) |
| `../../inputs/data-sources/linkbench/n1m/clock-20261012.csv` (+ `.json`) | the recorded clock of the n1m run the results were measured on; the `.json` holds that run's lines and sha256 per file |
| `plot.py` | the engine formats per access pattern: weighted KV ops and bytes, one panel per scale |
| `whole_log_counts_and_time.py` | the whole request log per layout and request type, as two CSVs: the counts (every raw lane, reads and writes) and the steady time of the reads |
| `plot_whole_log_summary.py` | the whole log on the nine layouts in counts (three rows) and in steady time (the last row), drawn like I01's summary figure |
| `steady_whole_log_table.py` | the steady run on the whole log, as a table: per layout the median wall per request type, the total per pass, hit rate and MB read, beside the counts on the same requests |
| `plot_whole_log_time_vs_models.py` | the measured steady time of a pass against the two count models (wops, ops + bytes / 4 KB), relative to Split/Split; and the Chunked layouts at 64 KB against 4 KB |

## How the data is made the same every time

Everything LinkBench draws is seeded (`load_random_seed` and `request_random_seed` = 12345, written into the tool's
config by `generate.sh`) except the times it stamps on links and nodes, which it reads from the wall clock.
`generate.sh` patches those reads to `LinkBenchClock.now()`, so the times come from `--clock MS` (one fixed value)
or from `--clock-trace FILE` (a recorded clock, one step per output line). At n1m with the default size, it
replays `inputs/data-sources/linkbench/n1m/clock-20261012.csv`, the clock of the measured run, and `data_manifest.py --write` then
requires each generated file to equal that run's lines and sha256 (the trace's `.json`), else rc 1. Any other setting
records its clock and the files' sha256 in `<data>/manifest.json`, and a later run on the same dir must name the same
clock (`--same-settings`).

The params follow from the data: `gen_linkbench_params.py` checks every file it writes against the tracked
`inputs/params/linkbench/<scale>/linkbench/params.sha256.txt` (rc 1 on a difference). The archived params of the 10-12 run predate
getlink's id2s list: regenerate them, the sha256 list proves the result (`inputs/params/linkbench/README.md`).

## Expected run time

The paper's run at n1m, the seven measured layouts. From the paper's run records (`uv run --locked python -m kvevaluator.measure.run_costs --paper-results`, after `just fetch-results`; each record's `wall_s`, per store, with stores side by side); 28-core Intel Xeon Gold 5512U: the reads
(`linkbench`, the whole request log) 88 s median, 95 s max (Ch1k-Ch1k), 10 min for the seven (binaries of 2026-11-03/04); the
writes 30 s median, 134 s max (Ch256k-Ch256k), 5.5 min for the seven (binary). The store ingests are not
recorded there.

The pipeline at n1m (1M nodes, 5.25M links) end to end, about 4 min: data generation 27 s, records about 40 s, ingest of
the three stores 1.7 min, the capture 55 s. At n10m (44.5M links) about 29 min: data generation 3.7 min, records
8.6 min, ingest 15.5 min, the capture 58 s. The nine layouts at n1m (64 cores): stores 42-50 s each, reads
371-385 s and writes 116-149 s per layout with nine side by side, the grid check 14 s.
