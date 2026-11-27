# KVLens

## Getting started

**Needs:** 
- Ubuntu 24.04 on x86_64, sudo for apt, 30 GB of disk to start, 250 GB for the paper's scale. 
- Tested on an Intel Xeon Gold 5512U (28 cores), 128 GB RAM, 750 GB NVMe. 
- **For artifact evaluators**: Please contact the authors if you want to reproduce the expeirment in the exact machine

```bash
sudo apt-get update
sudo apt-get install -y build-essential git curl cmake python3 zstd \
    libgoogle-perftools-dev libsnappy-dev zlib1g-dev libbz2-dev liblz4-dev libzstd-dev
sudo apt-get install -y numactl openjdk-21-jdk-headless maven
curl -LsSf https://astral.sh/uv/install.sh | sh && export PATH=$HOME/.local/bin:$PATH
uv tool install rust-just
```

**Clone code and results repo**

```bash
git clone https://github.com/farihat13/kvlens.git kvlens && cd kvlens
just            # lists every command
just fetch-results   # ~1 min   the paper's measured runs, in paper-results/
just setup      # ~1 min
just build      # ~4 min
just smoke      # 10 s
```

**Draw the paper's figures and tables from its measured runs**, with no store and no dataset:

```bash
just figure complex-kv-reads --from paper-results        # Figure 2
just figure census-touched --from paper-results          # Table 4
just figure datasets --from paper-results                # Table 6
just figure uniform-49 --from paper-results              # Figure 7
just figure two-fronts --from paper-results              # Figure 8
just figure ldbc-triangle --from paper-results           # Figure 9
just figure uniform-49-workloads --from paper-results    # Figure 10
just figure front-mix --from paper-results               # Figure 11
just figure predict-49 --from paper-results              # Figure 12
just figure runtime --from paper-results                 # Table 7
just figure latency-vs-cost --from paper-results         # Section 5.4
```

- writes to `output/figures/<name>/paper-results/` (it prints the path)
    - `paper/`: what the paper prints
    - `figures/`, `tables/`: every view of the same data
    - `about.md`: what it shows, from which data
    
## Reproduce the paper

```bash
just setup-engines   # sudo, docker: the tools to build Figure 2's databases
just fetch-dataset   # ~4 min   the paper's datasets (17 GB)
just reproduce-all   # ~27 h    every figure and table at the paper's scale
```

- first, a quick check on small data: `just reproduce-all --dataset ldbc=0.1,imdb=sample1pct` (~70 min, no docker)
- run it in tmux, alone on the machine (the latency run drops the page cache)
- each step's time and exit code: `output/logs/reproduce-<dataset>-<time>/times.log`

Or one item at a time, in this order (each reuses the counts of the ones above it; alone, it measures them first):

```bash
just reproduce-all --only uniform-49             # ~24 min   Figure 7
just reproduce-all --only ldbc-triangle          # ~10 s     Figure 9
just reproduce-all --only two-fronts             # ~10 s     Figure 8
just reproduce-all --only front-mix              # ~12 min   Figure 11
just reproduce-all --only uniform-49-workloads   # ~4 h      Figure 10
just reproduce-all --only predict-49             # ~34 min   Figure 12
just reproduce-all --only runtime                # ~2 min    Table 7
just reproduce-all --only census-touched         # ~5 min    Table 4
just reproduce-all --only datasets               # ~2 min    Table 6
just reproduce-all --only complex-kv-reads       # ~17 min   Figure 2
just reproduce-all --only latency-vs-cost        # ~21 h     Section 5.4
```

- the KV counts are exact: the same code on the same data gives the paper's numbers; times are your machine's
- Figures 1, 3-6 and Tables 1-3, 5 are diagrams and text
- more (optional items, each chain alone, the paper's names): [run/README.md](run/README.md)

## Use it on your own workload

A workload is a file of queries and how often each runs. Price one on the nine uniform layouts, with no store built:

```bash
just fetch-dataset ldbc=0.1    # ~5 s    LDBC at SF0.1 (25 MB)
W=inputs/workloads/ldbc/examples/example.json
uv run --locked kvcost predict --workload $W --dataset ldbc=0.1                                   # ~40 s
uv run --locked kvcost cost-layouts --workload $W --dataset ldbc=0.1 --count-source predicted-tier1   # ~1 s
```

- write your own queries and workload: [inputs/workloads/README.md](inputs/workloads/README.md#add-your-own-workload)
- measure a workload, find its best mix and check it, step by step (~2 min): [the walkthrough](docs/walkthrough.md)
- change the code: [AGENTS.md](AGENTS.md)

## Directory structure

- [`cpp/kvevaluator/`](cpp/kvevaluator/README.md): the evaluator, KVlens: builds stores, runs plans, counts KV work
- [`python/`](python/README.md): `kvevaluator` (schemas, plans, formats), `kvcost` (the analysis, the `kvcost`
  command), `kvengines` (the live databases' clients)
- [`inputs/`](inputs/README.md): what every run reads: schemas, params, workloads, layout specs
- [`run/`](run/README.md): the drivers: build, fetch, the figure chains, the checks
- `paper/`: `derive/` turns results into the paper's tables and numbers; `draw/` turns tables into figures
- [`experiments/`](experiments/README.md): one folder per study behind the paper
- [`engines/`](engines/README.md): Dgraph, NebulaGraph, ArangoDB and TuGraph, with our KV counters (Figure 2)
- [`tests/`](tests/README.md): lints, unit tests, gates
- not tracked: `data/` (datasets), `build/`, `output/`, `paper-results/`

## More

- [the walkthrough](docs/walkthrough.md): from a workload to its best layout, step by step
- [python/README.md](python/README.md): the terms (layout, mix, workload, the KV cost models)
- [run/README.md](run/README.md): reproducing in detail, the optional items, the install in detail
- [python/kvcost/cli/README.md](python/kvcost/cli/README.md): every `kvcost` command
- [AGENTS.md](AGENTS.md): how to change the code
