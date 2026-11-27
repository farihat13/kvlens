# kvengines

The evaluator (`kvevaluator`) and the cost analysis (`kvcost`) do not need this package.

The Python side of the live databases: a client for each, a profiler that reads the KV counters of our patched builds,
the LDBC Interactive runners and result parsers, and the driver that measures every (database, query, param) cell.
Building, loading and starting the servers is in [`engines/README.md`](../../engines/README.md).

**mb2**, in comments and snapshot names (`kvstats-mb2/...`), is the earlier live-engine experiment on these patched
builds: the microbench primitives (the MB queries, e.g. MB81-89 for writes) and the cost_eval cells. Its snapshots are
not in the artifact; this package carries its client, profiler and measurement code.

## Install

```bash
just setup                                           # uv sync --extra engines: the database drivers, pandas, graphviz
uv run python -I tests/lint/check_import_all.py --engines # every module imports
```

## Run

```bash
bash engines/run-engine.sh --sf 0.1                  # start the servers, measure every cell (resumes a stopped run)
bash engines/run-engine.sh --sf 0.1 --db dgraph --query ic2 --no-start   # one database and query, servers already up
```

`run-engine.sh` starts the servers and runs `measure.py`. Each cell writes `output/summary/ldbc/sf<SF>/engine/<db>/<query>/p<param>/`
(`kvstats.csv`: S / G / N, bytes, latency and the server's settings; `answer.json`: the result rows), and
`output/summary/ldbc/sf<SF>/engine/engine_kvstats.csv` is rebuilt from the cells after every run. No study reads that file
today: engine fidelity (`experiments/engine_fidelity/`) measures the live engines with its own `measure.py`, into
`live_kvstats.csv`.

## What is here

| path | what |
|---|---|
| `client.py`, `clients/` | `DatabaseClient` and `QueryResult`; `DgraphClient` (HTTP), `NebulaClient` (nebula3-python), `ArangoDBClient` (python-arango); `connect(db)` builds one with its local defaults |
| `kv.py` | `KVProfiler`: reset, run, snapshot of a server's KV counters (S / G / N and bytes), per-operation traces, and key decoders for the three stores |
| `measure.py` | the live measurement of every (db, query, param) cell (above) |
| `live_workloads.py` | where a live query and its params come from, per workload (LDBC, microbench) |
| `verify_schema.py` | checks a running server's schema and indexes against the schema file that built it |
| `cost_eval/` | loads LDBC query text and params with each database's param rules, and measures one execution (`measured/measure.py`) |
| `ldbc/` | LDBC Interactive: the query runner, param files, result dataclasses, one result parser per database, a CLI |
| `runner.py`, `results.py`, `utils.py` | a general query runner, result saving and loading, parsing helpers |
| `dgraph/`, `nebula/`, `arangodb/` | per database: the LDBC data converters and loaders, an LDBC runner, a query runner, plan inspection, and (Dgraph, NebulaGraph) `variant_build.py`, which builds a binary with our patches |

## In Python

```python
from kvengines import connect, KVProfiler

client = connect("nebula")                           # also "dgraph", "arangodb"
prof = KVProfiler()                                  # the default local counter endpoints
stats = prof.nebula_measure(lambda: client.execute(query))   # S / G / N and bytes for one run
```
