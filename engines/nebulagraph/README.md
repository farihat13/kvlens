# NebulaGraph

The steps for all three databases at once are in [`../README.md`](../README.md).

NebulaGraph (the 3.x line, commit 5d43e44b) on RocksDB v7.5.3, patched to count the KV operations of each query (SEEK,
GET, NEXT and bytes) in storaged and report them at `http://127.0.0.1:19779/kvstats`. Ports: graphd 9669, metad 9559,
storaged 9779.

## Build

Builds inside NebulaGraph's dev image, so it needs docker (run with sudo): `../README.md`, "Needs". `build.sh` checks it
first.

```bash
bash engines/nebulagraph/get.sh                  # the NebulaGraph and RocksDB sources at pinned commits (gitignored)
bash engines/nebulagraph/build.sh                # apply patches/, build the measured binaries into bin/
bash engines/nebulagraph/build.sh histogram      # or: + latency percentiles, bin-histogram/
bash engines/nebulagraph/build.sh plan-debug     # or: storaged logs its storage plan, bin-plan-debug/
```

| patch | what it adds |
|---|---|
| `patches/nebula.unified.patch` | per-query KV counters in storaged: `/kvstats` (and `PUT /kvstats/reset`), per-operation traces at `/kvtrace`, optional latency histograms |
| `patches/nebula.plan-debug.patch` | logs storaged's execution plan per request (the `plan-debug` build) |

## Data, load, start

```bash
source engines/env.sh
just fetch-dataset ldbc=0.1                              # from the repo root: LDBC's CSVs, params and update streams, sha256-checked
(cd engines/nebulagraph && just ldbc-download 0.1)   # links them into data/ldbc/
SF=0.1 bash engines/load-data.sh --db nebula     # load with schema/schema.gql, back up to backup/runs-sf0.1/
SF=0.1 bash engines/start-server.sh --db nebula  # restore the backup, start, check the schema
```

A load is skipped when its backup exists. Each backup records the sha256 of the schema that built it
(`backup/load-sf<SF>.txt`, untracked; the tracked `backup/provenance-sf<SF>.txt` is the archived backups' record); a start
refuses a backup whose schema has changed since (`../provenance.sh`).

## What is here

| path | what |
|---|---|
| `get.sh`, `build.sh`, `patches/` | the sources, our patches and the build |
| `schema/schema.gql` | the nGQL schema the LDBC data is loaded with |
| `configs/` | the graphd, metad and storaged configs the measured servers ran with |
| `load-data.sh`, `start-server.sh`, `lifecycle.sh` | load and back up; restore and start (the bodies are in `lifecycle.sh`) |
| `backup/` | the loaded backups (gitignored) and their provenance files |
| `scripts/` | start, stop, reset, status, the loader (`scripts/ldbc/`), patch tools, plan sweeps, a container setup |
| `justfile` | the wrapper's recipes for working on NebulaGraph: start, stop, console, ad-hoc queries, plans, stats (`just --list`) |
| `nebula-binaries.conf` | the binary variants `python/kvengines/nebula/variant_build.py` can build |
