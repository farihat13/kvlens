# Dgraph

The steps for all three databases at once are in [`../README.md`](../README.md).

Dgraph v25.0.0 on Badger v4.8.0, patched to count the KV operations of each query (SEEK, GET, NEXT and bytes) and
report them at `http://localhost:8080/kvstats`. Ports: Zero 5080 / 6080, Alpha 7080 / 8080 / 9080.

## Build

Needs Go 1.24.3 and bzip2: `../README.md`, "Needs". `build.sh` checks them first.

```bash
bash engines/dgraph/get.sh                 # the Dgraph and Badger sources at pinned commits, and data/ldbc (gitignored)
bash engines/dgraph/build.sh               # apply patches/, build bin/dgraph-kvstats (the measured binary)
bash engines/dgraph/build.sh histogram     # or: + latency histograms, bin/dgraph-histogram
```

| patch | what it adds |
|---|---|
| `patches/dgraph-profile.patch` | per-query KV counters and `/kvstats`, per-operation traces, rollup pause and force, the plan as JSON (`?explain=`) |
| `patches/badger-profile.patch` | the counters inside Badger's reads |
| `patches/dgraph-use-local-badger.patch` | builds Dgraph against the patched Badger checkout |

## Data, load, start

```bash
source engines/env.sh
just fetch-dataset ldbc=0.1                        # from the repo root: LDBC's CSVs, params and update streams, sha256-checked
(cd engines/dgraph && just ldbc-download 0.1)   # converts the CSVs to RDF and links the params; an RDF dir of Git LFS placeholders counts as missing
SF=0.1 bash engines/load-data.sh --db dgraph         # load with schema/ldbcTypes.schema, back up to backup/runs-sf0.1/
SF=0.1 bash engines/start-server.sh --db dgraph      # restore the backup, start, check the schema
```

A load is skipped when its backup exists. Each backup records the sha256 of the schema that built it
(`backup/load-sf<SF>.txt`, untracked; the tracked `backup/provenance-sf<SF>.txt` is the archived backups' record); a start
refuses a backup whose schema has changed since (`../provenance.sh`).

## What is here

| path | what |
|---|---|
| `get.sh`, `build.sh`, `patches/` | the sources, our patches and the build |
| `schema/ldbcTypes.schema` | the DQL schema the LDBC data is loaded with |
| `load-data.sh`, `start-server.sh`, `lifecycle.sh` | load and back up; restore and start (the bodies are in `lifecycle.sh`) |
| `backup/` | the loaded backups (gitignored) and their provenance files |
| `scripts/` | start, stop, reset, status, the loaders (`scripts/ldbc/`), patch tools |
| `justfile` | the wrapper's recipes for working on Dgraph: start, stop, ad-hoc queries, plans, metrics (`just --list`) |
| `dgraph-binaries.conf` | the binary variants `python/kvengines/dgraph/variant_build.py` can build |
| `code/dgraph-key/` | a tool that decodes Dgraph's keys |
