# job: the IMDb / JOB workload's counts

The Join Order Benchmark's 113 queries on the IMDb data, mapped to a property graph: what each layout of the
configurable format and each engine format (Dgraph at 512 KB, NebulaGraph, ArangoDB) costs on every valid plan body. The
workload itself (data, schema, mapping, queries, plans, its run commands and costs) is
[`python/kvevaluator/workloads/job/`](../../python/kvevaluator/workloads/job/README.md); this folder holds the
counts driver and the JOB latency table.

## Needs

Beyond the top README's Getting Started, the IMDb data. One command fetches it (pinned by sha256: a 1.26 GB download), builds the full
tag's tables and the 1% sample's, and the sample's reference answers, each step only if missing:

```bash
just fetch-dataset imdb=sample1pct
```

On a 16-core AMD EPYC 7302P: 178 s (fetch 86 s, the full tables 74 s, the sample 7 s, the reference 11 s),
11.6 GB in `<data dir>/datasets/imdb/` (raw 3.7 GB, full tables 6.0 GB, the sample 0.8 GB). The reference answers come
from DuckDB, which the repo's venv already has (a dependency of `kvevaluator`).

## Run

```bash
experiments/job/run-job-counts-chain.sh sample1pct          # the 1% sample: stores, counts, checks, the picks tables (~25 min)
experiments/job/run-job-counts-chain.sh full --parallel 6   # the full data (~7 h; 21-25 GB per server, ~150 GB at 6)
experiments/job/run-job-counts-chain.sh full --format dgraph   # one engine format alone, beside an existing capture (its measure:
#   kvcost measure --dataset imdb=full --workload j01 dgraph --nparam 1 --timeout 3600 --fresh; then the reference and tables)
FREEZE_NUMA_NODE=0 uv run --locked python -m kvcost.cli.cli measure-latency --dataset imdb=full --workload j01 \
    Sp-Sp Co-Co Ch64k-Ch64k Sp-Co Co-Sp Ch1k-Ch1k Ch16k-Ch16k Ch4k-Ch4k Ch256k-Ch256k Ch1k-Ch4k Ch1k-Ch16k Ch4k-Ch16k \
    --drift-control Sp-Co   # steady latency on the 12 layouts
uv run --locked python experiments/job/steady_latency_table.py   # the steady latency table of job-latency-utah6620-20261108 (a re-analysis, no store)
```

Steady latency is `kvcost measure-latency ... <the 12 layouts>` (`python/kvcost/cli/latency.py`), the command the paper's
latency figure runs (`run/reproduce.py`, its `LATENCY` table). It times each query's pick on the 12 uniform layouts
(the seven stores of the counts: Sp-Sp, Co-Co, Ch64k-Ch64k, ChK-ChK at K = 1, 4, 16, 256 KB; and Sp-Co, Co-Sp, Ch1k-Ch4k,
Ch1k-Ch16k, Ch4k-Ch16k), steady: direct I/O, the block cache of `inputs/contract/store_config.json` (10% of the IMDb CSV
bytes: 370 MiB at full). It measures the counts of the seven (`kvcost measure`), composes the five others' from them (`kvcost compose`),
builds those five stores (`kvcost build-store`) and runs each spec set's stores through
`run/latency-cells.sh` (the shared timed stage), alone on a quiet machine; `--control` times Sp-Co again at the end
(the drift control). A store outside the 12 is named after them, by name or as a spec file (or a dir of them). Output
(`python/kvevaluator/output_tree.py`): `timings/imdb-rows/<tag>/job/meta-<cell>/steady-<MB>mb-p<passes>/`, and the picks and
gates in `summary/imdb-rows/<tag>/grid-latency/job/`. `--dry-run` prints the steps.

The latency table (`steady_latency_table.py`) reads the pinned snapshot `imdb-rows/full/job-latency-utah6620-20261108` in place (`just
fetch-results`), or the one `--snapshot <id>` names (each snapshot's timing roots are listed in the script). Per store it gives the wall, the KV time and the combined cost (wops_bytes4k) over Sp-Co's, each query
weighted 1 / its combined cost on Sp-Co (the same frequencies as JOB's cost results), and the plain sum of the 113 walls
beside it. The rule is `kvcost.latency.steady_table` (the LDBC steady table's too). It checks every timed run's counts
against the capture and the weights against the frequencies JOB's costs used. Output: the table on stdout and
`output/summary/imdb-rows/full/job-latency-table/job-steady-latency.csv` (a named snapshot: `job-steady-latency--<its name>.csv`).

Run the full tag in tmux with a log. Both warn on a binary that is dirty or whose code differs from HEAD's, and mark the captures' run records (`binary_unclean`, `binary_behind_head`: kvevaluator.stale); the run goes on. Output:
`output/captures/imdb-rows/<tag>/job/reported/` (`counts/`, `steps/`, `parts/`, `row-hashes/`; the cells and the named
stores side by side), and the picks tables `job_picks.csv`, `job_picks_formats.csv` in `output/summary/imdb-rows/<tag>/job-cells/`. The archived capture of the full tag is `imdb-rows/full/job-20261012` (`just fetch-results`);
the cross-benchmark analysis that reads it is in `../mix_benefit/`.

## What is here

| file | what |
|---|---|
| `run-job-counts-chain.sh` | the counts at one data tag, unattended: ingest the stores, run every valid body of every query (one `kvcost measure` per store, side by side through `run/in-parallel.sh`), check the cells and the reference answers, write the picks tables; `--format F` one engine format alone (its store, its bodies, the reference, both tables) |
| `steady_latency_table.py` | the steady latency table of the snapshot: per store, the wall, KV time and combined cost over Sp-Co's, equal-share and equally weighted |

## Expected run time

The sample tag, stage by stage. A is a 16-core AMD EPYC 7302P with SATA SSDs; B a 40-core node whose data
was on a hard disk; the 25 min recorded earlier was on a 32-core AMD EPYC 9354P.

| stage | A | B |
|---|---|---|
| the data (`ensure.py --tag sample1pct`) | 178 s | 4 min 46 s |
| stores and counts (12 stores, at most 6 side by side) | 1,308 s | 31 min |
| the grid check (`check_grid.py`) | 21 s | - |
| the reference check | 0 s | - |
| the picks tables | 178 s | - |
| the chain in all | 41 min, measured with the grid check at 664 s; 31 min with it at 21 s | - |

The grid check reads each plan file and hashes its bodies once per run, not once per cell and name. It prints
a line per stage and per cell, and the chain prints a line each minute while a check runs. A pass ends with `CHAIN DONE <time>: every stage passed (...)`.

The full tag: 7 h 00 min with six servers side by side (21-25 GB peak each), on the 32-core machine. Dgraph (512 KB)
alone at the full tag: ingest 536 s, bodies 8,440 s, 22.8 GB peak. The workload's README has the per-stage table.

The latency table (`steady_latency_table.py`): 2 s (13 stores x 113 queries, 1,469 plan rows; estimate 10 s) on an Intel
Xeon Gold 5512U, the snapshot already unpacked.

Steady latency at the full tag (`kvcost measure-latency --dataset imdb=full --workload j01 Sp-Sp Co-Co Ch64k-Ch64k ... Ch4k-Ch16k ...`), per store
through `run/latency-cells.sh`, timed with 3 measured passes per store on a 28-core Intel Xeon Gold 5512U,
one store at a time with nothing else running: 11,878 s for the seven. At the full tag the command runs 5 measured
passes per store and no KV-timer passes (`steady_setting` in `python/kvcost/cli/latency.py`) on 13 stores (the 12 and
the control); the paper's run took 10 measured and 10 KV-timer passes per store, 35.0 h in all. Per store: Ch64k-Ch64k 963 s, Sp-Sp 2,871 s, Co-Co 2,153 s,
Ch1k-Ch1k 1,817 s, Ch4k-Ch4k 1,547 s, Ch16k-Ch16k 1,722 s, Ch256k-Ch256k 805 s; each 5 passes (2 warm, 3 measured; 6 on Ch16k-Ch16k) of
150-567 s, plus ~15 s for the page-cache drop and the start; 5.7-6.7 GB peak. Sp-Sp and Co-Co take 2.3-3.1x Ch64k-Ch64k's
time while reading no more bytes per pass (23.7-32.6 GB against 34.5 GB). The sample tag: 76-180 s per store.

Five more stores at the full tag (the same machine and settings): Sp-Co 2,671 s, Co-Sp 2,352 s,
Ch1k-Ch4k 1,768 s, Ch4k-Ch16k 1,525 s, the JOB mix 1,761 s, and Ch64k-Ch64k again 962 s; 5.8-7.0 GB peak. Their counts
(`grid_bodies`, 3 at a time): 3,682-5,221 s per store.
