This directory is optional: the kvformat evaluator (`cpp/kvevaluator`, `python/kvevaluator`) needs none of it.

# engines: the three real databases, built with our KV counters

The paper's "engine" numbers come from the real databases, each patched to count its own KV operations (SEEK, GET,
NEXT and bytes) per query and report them at `/kvstats`. This directory fetches each database at an exact commit,
applies our patches, builds it, loads LDBC SNB into it and measures. The Python side (clients, the measurement, the
param loader) is the package `python/kvengines/`.

## Needs

Beyond the main README's Needs block, each database's build and server need their own tools: Go for Dgraph, docker
for NebulaGraph (it builds inside its dev image), clang-17 and a set of libraries for ArangoDB. They are listed once,
in `engines/needs.tsv` (what, for which database, and the check that says it is there). On Ubuntu 24.04, with sudo:

```bash
just setup-engines                  # every database; or: just setup-engines --db dgraph   (--dry-run installs nothing)
```

It prints a line per need (present / to install), installs the missing ones (apt packages, Go's tarball into
`/usr/local/go`, the docker daemon started) and checks them all again; a second run installs nothing. Each
`engines/<db>/build.sh` checks the same list first and names what is missing ("missing: go").

| database | disk (our build) | build time |
|---|---|---|
| Dgraph | sources 125 MB, Go's module cache 651 MB | 23 s to fetch, 2.7 min to build (a stock Ubuntu 24.04 container on a 16-core AMD EPYC 7302P) |
| NebulaGraph | the dev image 6.2 GB, its build 2.3 GB, its RocksDB 1.3 GB | 19.4 min from scratch, RocksDB included, on a 56-core Intel Xeon Gold 5512U. The build is capped at 10 jobs (more ran out of memory when the script was written; not re-checked). The time includes the dev image's `docker pull` (`scripts/build.sh` starts its clock before it) |
| ArangoDB | sources with submodules 1.9 GB, its build 2.0 GB | 48 s to fetch, 11.8 min to build from scratch (`build.sh kvstats` on 28 of the 56 cores of an Intel Xeon Gold 5512U; make alone 705 s). A rebuild after a patch change: 11 min |

On a 56-thread Intel Xeon Gold 5512U, the three builds and the evaluator's build ran at once: Dgraph 263 s,
ArangoDB 817 s, NebulaGraph 1,420 s (its dev image pulled first). The tools' apt install took 22.5 min there, all of it
the Ubuntu mirror's download (323 MB at 0.24 MB/s).

NebulaGraph and ArangoDB compile large C++ trees (ArangoDB with V8 and ICU), the long steps of "Run it".

**TuGraph** (v4.5.2, on LMDB) is a fourth database, in Figure 2 only: it has no kvformat in the evaluator. It is
not built: `engines/tugraph/get.sh` fetches its release (47 MB, seconds) and our counters are a small library loaded
before the server (`engines/tugraph/README.md`). `--db all` stays the three above; TuGraph is named: `--db tugraph`,
and `--db tugraph-detached` for its second configuration (its properties in a table of their own).

## Run it

From the repo root, after `just setup-engines` (Needs, above). First the Python side (kvengines and the database clients), the
environment (paths from this dir; SF, default 0.1; the Dgraph knobs; the LDBC data root), and LDBC's data
(CSVs, params and update streams, sha256-checked):

```bash
just setup && source engines/env.sh && just fetch-dataset ldbc=0.1
```

Then one command builds what is missing, loads LDBC into each database and backs it up:

```bash
bash engines/once.sh --db all          # or --db dgraph, --db dgraph,nebula; --sf 0.3 for another scale
```

For each database named (`dgraph`, `nebula`, `arangodb`) it fetches the sources at their pinned commits
(`engines/<db>/get.sh`), checks the build's tools, applies `patches/`, builds and stashes the binary in
`engines/<db>/bin/` (`engines/<db>/build.sh kvstats`; NebulaGraph also installs its console and importer, which its
start and load run), links the LDBC files in (Dgraph also converts them to RDF), loads and backs up. A binary and a
backup that are there are skipped, so a second run takes seconds; it stops at the first failed step. The builds are the
long part (the table above).

Then restore the backups, start the servers and measure every query; stop them when done:

```bash
bash engines/run-engine.sh --db all
bash engines/stop-server.sh --db all
```

`run-engine.sh` writes `engine_kvstats.csv` under the evaluator's output dir (`KVEVAL_OUTPUT_DIR`). Loading reads the
LDBC data from `$LDBC_SNB_DATA_DIR` (default `<data dir>/datasets/ldbc/`, where `just fetch-dataset` puts it).

## Cache regimes

`ENGINES_SETTING=equivalent` runs an engine as Figure 2 times it: every process on one CPU, the engine's own caches
off. `ENGINES_REGIME` then names its cache regime. The names and what each sets come from one registry,
`inputs/contract/store_config.json`, which the evaluator's timed runs read too
(`python -m kvevaluator.measure.store_config <regime> <SF>` prints one regime's settings).

| regime | Dgraph (Badger) | NebulaGraph (RocksDB) | ArangoDB (RocksDB) | TuGraph (LMDB) |
|---|---|---|---|---|
| `warm` | 8 GiB block cache | 8 GiB | 8 GiB | no cache to size: the file is mapped |
| `warm-bounded` (Figure 2) | the steady size (30 MiB at SF0.3) | the same | the same, index and filter blocks outside it | as warm |
| `steady` (direct reads) | refused: Badger maps its tables | set | set | refused: LMDB maps its file |
| `cold-bounded` | a cold start | a cold start | a cold start | a cold start |
| `cold` (direct reads) | refused, as steady | a cold start, direct reads | a cold start, direct reads | refused, as steady |

A cold start is one start of the server: the backup restored, the restored store's files put out of the OS page cache
(those files alone, with no root: the engine's own code stays in memory), the server launched, and no check that
reads the store. `experiments/engine_fidelity/time_queries.py --regime cold-bounded` makes one before every timed
run. A page-cache drop is not enough while a server runs: it does not reach the pages a process has mapped. On a live
TuGraph at SF0.3, 184 of 233 MiB of the store stayed cached through `echo 3 > /proc/sys/vm/drop_caches` (the cached
pages counted with `mincore`, as `time_queries.py` counts them).

A start reads the store itself: ArangoDB reads 376 of the 954 MiB of its tables at SF0.3 (39%) and NebulaGraph replays
its logs. So the tool puts the store out of the page cache once more after the start, and writes into each row what
was still cached just before the run (`store_cached_mib` in `settings`): at SF0.3, 0.0 MiB of ArangoDB's and
NebulaGraph's stores, 0.1 MiB of TuGraph's (LMDB's meta pages) and 48 of the 575 MiB of Dgraph's.

Dgraph's 48 MiB are Badger's table indexes with their filters, not data: 47.8 of the 48.1 MiB are one unbroken run
at the end of each of its 113 tables, which Badger reads when it opens a table and keeps mapped. NebulaGraph and
ArangoDB hold the same on the heap (RocksDB reads a table's index and filter blocks at its open), where no page-cache
count sees it; LMDB has no index apart from its data pages, so TuGraph starts the coldest. Dgraph's data is read
cold: one cold Q3 reads 62.2 MiB of its store from the disk. Its Q3 takes the same 11 s cold as warm because the run
is CPU work: its threads wait 71 ms for the disk.

## Layout

| path | what |
|---|---|
| `env.sh` | source first: every path from this dir, the measured operating point |
| `ldbc-params-dir.sh` | the LDBC substitution parameters each `just ldbc-download` links: LDBC's download, else the tracked hosted copy in `inputs/params/ldbc/sf<SF>` (`just fetch-dataset` downloads none for sf0.3 and sf1) |
| `<db>/get.sh` | fetch the sources at an exact commit (each its own shallow git checkout, gitignored) |
| `<db>/patches/`, `<db>/build.sh` | our instrumentation patches (the ones that built the measured binaries) and their build |
| `<db>/start-server.sh`, `<db>/load-data.sh`, `<db>/lifecycle.sh`, `lifecycle-common.sh` | start / load one DB: reset, start, restore or load, back up |
| `<db>/schema/` | the DDL each DB is loaded with |
| `<db>/backup/` | the loaded backups (gitignored) and their provenance (tracked) |
| `nebulagraph/configs/` | the NebulaGraph configs the measured servers ran with |
| `<db>/scripts/`, `<db>/justfile`, `<db>/data/` | each database's own wrapper (start, stop, load, its tools) |
| `queries/ldbc/<db>/`, `queries/microbench/<db>/` | the one copy of the query texts |
| `queries/writes/<db>/` | the write primitives' texts, run by the live write sweep (`experiments/engine_fidelity/measure.py --family writes`) |
| `queries/ldbc/cypher/`, `queries/microbench/cypher/` | a Cypher form of Figure 2's three queries, equivalent to the engines' texts (written from NebulaGraph's openCypher); TuGraph's texts (`queries/*/tugraph/`) start from them |
| `once.sh`, `load-data.sh`, `start-server.sh`, `run-engine.sh`, `provenance.sh` | the drivers over all three DBs |

`<db>` is `dgraph`, `nebulagraph` or `arangodb`; the code's key for NebulaGraph is `nebula`. `tugraph/` has the same
drivers, and `kvstats/` in place of `patches/` (its own README).

## Queries

`queries/` holds one text per query and database, the corrected set the engine-fidelity case studies measured. IC13
and IC14 are out of scope. cost_eval and the storage-latency-breakdown study were measured
with older texts of 6 queries; those texts are in the source repository (tag `artifact-freeze-20261026`).

## Developer tools

Kept for working on an engine, not used by the measurement: the wrappers' plan and query tools (`plan-sweep.sh`,
`storage-plan-sweep.sh`, `variant-run.sh`, `kvengines.<db>.plan_query`, `parse_plan`, `variant_build`), `dgraph/code/`.
