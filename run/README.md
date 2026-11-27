# run: the shared drivers

Build the evaluator, fetch the data and the paper's results, run the figure chains and the checks. The top README's
[Reproduce the paper](../README.md#reproduce-the-paper) is the short form of this page.

**On this page:** [Reproduce a figure or table](#reproduce-a-figure-or-table) ·
[How long each figure takes](#how-long-each-figure-takes) ·
[Will my numbers match the paper's?](#will-my-numbers-match-the-papers) · [Optional runs](#optional-runs) ·
[Paper terms and their names here](#paper-terms-and-their-names-here) ·
[Install: versions, memory, disk](#install-versions-memory-disk) · [What each script does](#what-each-script-does)

## Reproduce a figure or table

Each figure or table of the paper (an *item*) is two commands:

```bash
just results <name>    # measure and analyse its data
just figure <name>     # derive its tables and draw it
```

- an item is named by its paper label (`fig:uniform-49` is `uniform-49`), never by its number
- the one list of items: `FIGURES` in `reproduce.py`, in the order a full run takes them; `just results` with no name
  prints it
- `just derive-all` (~3 min): every derive script on `paper-results/`, with how many ran and failed

### The paper's results (`paper-results/`)

The paper's runs are their own git repo (`kvlens-results`), placed at `paper-results/`.

```bash
just fetch-results            # clone it at the pinned commit, check every file
just fetch-results --check    # check the tree in place; writes nothing
just pin-paper-results        # (maintainers) write its file list, then pin its commit
```

- the pin: `inputs/paper_results.json`; the file list: `paper-results/files.sha256`
- a tree already in place is checked, never overwritten; `--source` takes another copy of the repo
- `just figure <name> --from paper-results` draws an item from it (seconds, no measuring) into
  `output/figures/<name>/paper-results/`: the figure, its tables, an `about.md`
- `paper/derive/` makes an item's tables; `paper/draw/` draws the figure from those tables only

### Re-measure one figure

```bash
just fetch-dataset                                         # the paper's scales: LDBC SF3, IMDb full, LinkBench n1m
just results uniform-49 --dry-run                          # the chain's commands; runs none
just results uniform-49                                    # the paper's settings
just results uniform-49 --dataset ldbc=0.1 && just figure uniform-49 --dataset ldbc=0.1   # the same chain, small
```

- every command is printed before it runs, with its time after
- `just figure` draws from `output/` into `output/figures/<name>/<run>/`, a dir named by the run's scales
- `--dataset ldbc=0.1`: LDBC at SF0.1 (numbers differ from the paper's); `--dataset ldbc=0.1,ldbc=0.3,imdb=sample1pct`
  adds a second LDBC scale and JOB
- `--jobs N` caps the stores each command runs at once, for a machine short of memory (its default is printed first)
- `just fetch-dataset` fetches only what is missing and prints what it will do first; `--check` checks by sha256;
  `kvcost measure` refuses without the data and names the command

### Re-measure everything

```bash
just reproduce-all --help     # its flags: --dataset, --jobs N, --only <name>, --skip <name>, --archive DIR, --dry-run
```

- each step's exit code and time: `output/logs/reproduce-<dataset>-<time>/times.log`, one log per step beside it
- not in the paper, kept with their studies: `plan-bytes`, `price-steps`, `parts-w11`, `composed-vs-measured`

### How long each figure takes

The top README's times are one run in order, each chain reusing the counts before it. Alone, from an empty `output/`:

| name | redraw (`--from paper-results`) | re-measure alone (summed run time) | heaviest step |
|---|---|---|---|
| `uniform-49` | 1 s | ~81 min | I01's counts on the seven layouts: 44 min |
| `ldbc-triangle` | 6 s | ~81 min | as uniform-49 |
| `two-fronts` | 1 s | ~81 min | as uniform-49 |
| `front-mix` | 34 s | ~1.8 h | as uniform-49, then two mix searches: ~28 min |
| `uniform-49-workloads` | 2 s | ~18.5 h | J01's counts 8.0 h, B01's 7.1 h |
| `predict-49` | 1 s | ~18.8 h | as uniform-49-workloads |
| `runtime` | 1 s | ~19.5 h | as uniform-49-workloads |
| `census-touched` | 5 s | ~18.5 h | as uniform-49-workloads |
| `datasets` | 0 s | ~18.5 h | as uniform-49-workloads |
| `complex-kv-reads` | 2 s | ~16 min | the databases' first build: Dgraph 2 min, ArangoDB 11 min, NebulaGraph 24 min |
| `latency-vs-cost` | 90 s | ~21 h | JOB's and BI's steady timing |
| `complex-kv-reads-cold` (optional) | - | ~54 min | ten cold starts a cell |
| `complex-kv-reads-other-configurations` (optional) | - | ~2 min | TuGraph's second configuration: load, counts, timing |
| `predict-formulas` (optional) | - | ~13 min | with the seven stores in place |

- **after another item, a chain adds only its own steps:** `kvcost measure` skips counts already in `output/`, and
  `kvcost search-mix` a search already answered (after `uniform-49-workloads`, `predict-49` adds only the statistics,
  30-50 min per dataset at SF3, and the predictions, 15 s to 5 min per workload)
- **where the times come from:** the top README's, one `just reproduce-all --jobs 4` on a fresh `output/`; this table's,
  the recorded run times of the chain's steps, summed
  (`uv run --locked python -m kvevaluator.measure.run_costs --paper-results`); several stores at once take a quarter
  to a third of the sum
- run a long chain in tmux, with a log

### The latency results: timing only, no counting

- each store times each query at the body `inputs/latency-picks/<dataset>/<scale>/<family>.json` names; the cost axis
  comes from the timed runs' own counts
- counts already in `output/` are compared with every timed run (free); `--validate-count` counts the 12 layouts first
- after a plan change, time the counts' new pick, per workload:

```bash
kvcost measure-latency --dataset ldbc=3 --workload i01 <the 12 layouts> --save-picks   # count, write the file, time nothing
kvcost measure-latency --dataset ldbc=3 --workload i01 <the 12 layouts>                # time the new picks
git diff inputs/latency-picks/                                                         # what moved
```

### Will my numbers match the paper's?

- **the counts: yes, exactly.** They are deterministic: at the paper's scale, the same code on the same data gives the
  same rows as `paper-results/`, and each run record names the binary that measured it
- **the times:** your machine's; what carries over is the ranking of the layouts
- **at another scale or workload:** a run of its own; compare the ranking (which layout is cheapest, which are close).
  A ranking can move with scale: IC7 at SF0.1 returns 20 rows a param and Co-Co (1,460) costs more than
  Ch64k-Ch64k (404); at SF3 four of its five params return none and Co-Co (12) costs less than Ch64k-Ch64k (64)
  (wops_bytes4k per run, the mean over the five)
- **to compare a run with `paper-results/`**, one family's captures, every column, the clocks left out (a layout one
  side lacks is a FAIL line):

```bash
uv run --locked python tests/gates/check_capture_columns_equal.py \
    --old paper-results/captures/ldbc/sf3/interactive/reported --new output/captures/ldbc/sf3/interactive/reported
```

### Optional runs

`just reproduce-all` leaves these out:

- `just results complex-kv-reads-cold` (~54 min, after `complex-kv-reads`): Figure 2's reads from a cold start;
  `just figure complex-kv-reads` then draws warm and cold side by side
  ([experiments/engine_fidelity/README.md](../experiments/engine_fidelity/README.md#warm-and-cold))
- `just results complex-kv-reads-other-configurations` (~2 min, after `complex-kv-reads`): the database configurations
  only Figure 2's options draw (TuGraph with its properties in a table of their own), added to the same run;
  `just figure complex-kv-reads` then draws those options too
- `just results engine-fidelity`, then `just figure engine-fidelity` (~15 min, no database, no docker): re-checks the
  paper's fidelity numbers from the saved live counters
  ([experiments/engine_fidelity/README.md](../experiments/engine_fidelity/README.md))
- `just results predict-formulas`, then `just figure predict-formulas` (~13 min with the seven stores): where predict
  mode's error comes from, one cause at a time
  ([experiments/predict_statistics/README.md](../experiments/predict_statistics/README.md#how-accurate-predict-mode-is-predict-formulas))

### Figure 2: the live databases

- op counts exact; bytes within 1% between two runs on one load (each Dgraph load numbers its nodes anew)
- times are your machine's; on a CPU without LBR call stacks (AMD), NebulaGraph's and ArangoDB's KV-time share is not
  measured
- the chain also measures TuGraph, with a vertex's properties packed and detached
- `just setup-engines` (sudo, docker) first; the databases: [engines/README.md](../engines/README.md); each study's own
  drivers: [experiments/README.md](../experiments/README.md)

### Paper terms and their names here

| in the paper | in the repo |
|---|---|
| W16 (LDBC SNB Interactive at SF3, LDBC's frequencies); W17 (its reads) | `i01`, `i02` ([inputs/workloads/](../inputs/workloads/README.md)) |
| the hand-written workloads; section 5's example | `w01`-`w15`; `w11` |
| LDBC BI, JOB, LinkBench as workloads | `b01`, `j01`, `l01`-`l04` |
| a query set (Interactive, BI, the microbenchmark, the writes, JOB, LinkBench) | a **family**: `--family interactive` |
| a uniform layout, e.g. Sp-Co | `Sp-Co`, or its **cell id** `props_split_adj_condensed` (`Ch4k-Ch4k` = `props_chunked_c4k_adj_chunked_c4k`) |
| the 49 uniform layouts | every pair of seven settings (Split, Condensed, Chunked at 1, 4, 16, 64, 256 KB); 7 measured, 42 composed |
| a mixed layout | a **mix**: a spec with per-type settings, named by its content (`mix_<sha>`, `inputs/specs/mixes/`) |
| Dgraph's, NebulaGraph's, ArangoDB's format; Per-type CSR | `dgraph` (reported as `dgraph-c512k`), `nebula`, `arangodb`; `pertype_csr` |
| a query's plan; its alternatives | a **body**: one plan in a query's plan file |
| the plan a layout runs | the **pick**, in `inputs/picks/picks.csv`, chosen at SF3 and used at every scale |
| the counts a check compares with | the **golden counts**: SF0.1 counts kept in the repo (`tests/gates/check_golden_counts.py`) |
| bytes read, by kind | the **byte lanes**: get key and value, scan key and value, miss keys (write key and value) |
| wops; the op-byte cost | `wops`, `wops_bytes4k` ([Concepts](../python/README.md#concepts)) |

## Install: versions, memory, disk

The top README's [Getting started](../README.md#getting-started) is the short form.

- **versions:** Ubuntu 24.04 (x86_64, Python 3.12), uv 0.13.6, just 1.59.0 (what CI pins); Python packages pinned by
  `uv.lock`; `just build` runs `check-build-tools.sh` first and names any missing tool; the latency runs also need
  `numactl`
- **memory:** the C++ build runs one job per core, up to ~0.5 GB each (RocksDB's build reached ~13.5 GB on a 28-core
  Xeon Gold 5512U); short of memory: `just build --jobs 4` (at most 4.8 GB, ~8 min); every other step of Getting
  started fits in 6 GB
- **disk:** ~30 GB to start: the C++ build 1.8 GB (2.7 GB with its unit tests, in `<main clone>/build/cache`, shared by
  every worktree), the paper's results 2.4 GB, LDBC SF0.1 121 MB and its stores 0.8 GB; to put data, output or build on
  another disk: [python/README.md](../python/README.md#where-files-go)
- **build warnings, harmless:** `-Wmissing-field-initializers`, `-Wmaybe-uninitialized`, `-Wunused-function`
  (`tests/test_schema.cc`), `-Wunused-result` (`tests/test_counting_miss.cc`), uv's `Failed to hardlink files`
- **from a release archive** (a zip, no `.git`): every command works; the build stamps `RELEASE_COMMIT` with the dirty
  flag `release`; the fast tests that read git history are skipped. A plain tar or `cp` copy runs too, records
  `unknown` and prints one warning line

**The first checks**, on a fresh clone:

```bash
just fetch-dataset ldbc=0.1   # ~10 s     LDBC SNB at SF0.1, every file checked by sha256
just golden-counts            # ~2 min    builds the SF0.1 stores, runs every plan, compares with the frozen counts
just check                    # ~3-8 min  what every change passes: lint, imports, a relocated clone, C++ and fast tests
```

Each push to main also runs `just setup`, `just lint` and `just tests-fast-no-binary` on GitHub Actions
(`.github/workflows/check.yml`); the tests tagged `binary:` in `tests/fast_tests.txt` run locally only.

## What each script does

**Reproduce**
- `reproduce.py`: `just results` and `just figure`: one item's chain, its derive and draw (`FIGURES`)
- `reproduce-all.sh`: `just reproduce-all`: every item in order, each step timed
- `fetch_results.py`: `just fetch-results`: the results repo at the pinned commit, every file checked; `--pin`
  (`just pin-paper-results`) writes its list and the pin
- `convert-paper-results.py`: a copy of `paper-results/` converted in place to the file shapes today's code reads

**Build and fetch**
- `build_cpp.py`, `build-cpp.sh`: `just build`: the C++ evaluator, once per committed source state, in a shared cache
- `check-build-tools.sh`: the tools and libraries `just build` and `just fetch-dataset` need
- `download.py`: one pinned file over http(s) or file://, its size and sha256 checked as it arrives
- `env.sh`: the shell drivers' one setup, sourced first

**Checks**
- `gate-full.sh`: `just gate full`: every check a merge needs, in one fixed order; `gate_levels.py`: which gate a
  branch needs
- `meta-gates.sh`: the configurable format's per-step gates on one binary (the sf0.1 round)
- `chain-stages.sh`, `gate-lock.sh`: a round's stages, each with its own log and verdict; one round per store root at a
  time
- `cpp-diff.sh`: `just cpp-diff`: the C++ counts against the Python oracle on every plan file
- `write-gate.sh`: `just write-gate`: the write plans, their effects and counts against the oracle
- `predict-gate.sh`, `predict-accuracy.sh`: predict mode's exact gate; its accuracy per KV step, level by level (the
  `predict-formulas` item)
- `meta-vs-formats.sh`: the configurable format against the engine formats
- `meta-build-controls.sh`: the known-bad control binaries a round's `--controls` needs

**Stores and timing**
- `ingest-cpp-stores.sh`: the stores of one (schema, codec, chunk) combination, one per format
- `kvstats.sh`: every count the evaluator measures for one workload
- `latency-cells.sh`, `measure-preflight.sh`, `machine-for-timing.sh`: the timed stage of a study's cells, its stale
  marks and binary preflight, the machine's setup for timing
- `in-parallel.sh`, `watch-memory.sh`: a command file's lines N at a time; the evaluator servers' peak memory
