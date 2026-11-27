# ArangoDB

The steps for all three databases at once are in [`../README.md`](../README.md).

ArangoDB v3.12.8 (RocksDB engine), patched to count the KV operations of each query (SEEK, GET, NEXT and bytes) and
report them at `http://127.0.0.1:8529/_admin/kvstats`. Port: 8529 (HTTP; the client logs in as root with an empty password).

## Build

Needs clang-17, llvm-17 and OpenSSL's headers: `../README.md`, "Needs". `build.sh` checks them first. The sources with
their submodules take 1.5 GB.

```bash
bash engines/arangodb/get.sh                     # the ArangoDB sources at a pinned commit (gitignored)
bash engines/arangodb/build.sh                   # apply patches/, build the measured bin/arangod
bash engines/arangodb/build.sh histogram         # or: + latency histograms, bin-histogram/
bash engines/arangodb/build.sh tickers           # or: + RocksDB's own statistics, bin-tickers/ (a cross-check)
```

| patch | what it adds |
|---|---|
| `patches/arangodb.unified.patch` | per-query KV counters around ArangoDB's RocksDB calls: `/_admin/kvstats`, per-key traces at `/_admin/kvtrace`, optional latency histograms and RocksDB statistics |
| `patches/arangodb-community-build.patch` | lets the community edition build without the enterprise sources |

## Data, load, start

```bash
source engines/env.sh
just fetch-dataset ldbc=0.1                              # from the repo root: LDBC's CSVs, params and update streams, sha256-checked
(cd engines/arangodb && just ldbc-download 0.1)  # links them into data/ldbc/
SF=0.1 bash engines/load-data.sh --db arangodb   # import with schema/ldbc_schema.json, back up to backup/runs-sf0.1/
SF=0.1 bash engines/start-server.sh --db arangodb    # restore the backup, start, check the schema and indexes
```

A load is skipped when its backup exists. Each backup records the sha256 of the schema that built it
(`backup/load-sf<SF>.txt`, untracked; the tracked `backup/provenance-sf<SF>.txt` is the archived backups' record); a start
refuses a backup whose schema has changed since (`../provenance.sh`).

## What is here

| path | what |
|---|---|
| `get.sh`, `build.sh`, `patches/` | the sources, our patches and the build |
| `schema/ldbc_schema.json` | the collections and indexes the LDBC data is loaded with |
| `load-data.sh`, `start-server.sh`, `lifecycle.sh` | load and back up; restore and start (the bodies are in `lifecycle.sh`) |
| `backup/` | the loaded backups (gitignored) and their provenance files |
| `scripts/` | start, stop, reset, status, install, the loader (`scripts/ldbc/`), patch tools, a plan sweep |
| `justfile` | the wrapper's recipes for working on ArangoDB: start, stop, shell, ad-hoc queries, plans (`just --list`) |
