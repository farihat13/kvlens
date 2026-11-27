# TuGraph

The steps the databases share are in [`../README.md`](../README.md).

TuGraph v4.5.2 (the 4.x line, on LMDB), as its authors released it, with its KV operations counted per query (GET,
SEEK, NEXT and bytes) and reported at `http://127.0.0.1:7072/kvstats`. Ports: 7070 (REST: `POST /login`,
`POST /cypher`; user `admin`, the release's default password), 7072 (the counters). TuGraph has no kvformat in the
evaluator, so it is in Figure 2 only, as a live database.

## Fetch and build

TuGraph is not built and not patched. `get.sh` downloads the release's el8 package (47 MB, sha256-checked; its source
is the tag v4.5.2, commit `4a1c8fb6`) and unpacks it; the server runs on Ubuntu 24.04 as it is. Our counters are a
library of one C file, loaded before the server.

```bash
bash engines/tugraph/get.sh        # the release, unpacked into tugraph-4.5.2/ (gitignored); seconds
bash engines/tugraph/build.sh      # bin/libtugraph_kvstats.so from kvstats/tugraph_kvstats.c; under a second
```

**How the counting works.** TuGraph keeps LMDB inside `liblgraph.so` and calls it through the dynamic linker, so a
library loaded first (`LD_PRELOAD`) receives each call TuGraph makes to LMDB's API, counts it and passes it on.
`build.sh` checks on the fetched release that this holds. Calls LMDB makes to itself are not counted. What a call
counts as:

| LMDB call | counts as |
|---|---|
| `mdb_get`; `mdb_cursor_get` with `MDB_SET_KEY` (an exact-key lookup) | GET |
| `mdb_cursor_get` with `MDB_SET_RANGE`, `MDB_FIRST`, `MDB_LAST` | SEEK |
| `mdb_cursor_get` with `MDB_NEXT`, `MDB_PREV` | NEXT (`prev_count` is kept apart) |
| `mdb_cursor_get` with `MDB_GET_CURRENT` | its own column: the cursor does not move; in no total |
| `mdb_put`, `mdb_cursor_put`; `mdb_del`, `mdb_cursor_del`; `mdb_txn_commit` | PUT; DELETE; COMMIT |

Bytes are the key and value sizes at the call. A value's size is what LMDB stores: TuGraph puts an 8-byte version
before every value. `GET /kvstats` returns the totals per op and two raw channels, `by_call` (per LMDB call) and
`by_table` (per LMDB table and call: the graph table `_graph_`, each index, the schema and user tables);
`PUT /kvstats/reset` zeroes them.

## Data, load, start

```bash
source engines/env.sh
just fetch-dataset ldbc=0.3                          # from the repo root: LDBC's CSVs
bash engines/once.sh --db tugraph --sf 0.3           # get.sh and build.sh when missing, then the load and its backup
SF=0.3 bash engines/start-server.sh --db tugraph     # restore the backup, start with the counters, check the load
bash engines/stop-server.sh --db tugraph
```

The load is offline: `lgraph_import` writes an LMDB data dir from LDBC's CSV files, read in place, with the design
`schema/ldbc_import.json`: the labels, ids and properties the other databases load, LDBC's id as each label's
primary key, and four secondary indexes (Person.firstName, Place.name, Tag.name, TagClass.name). TuGraph has no list
property, so a person's emails and languages are one string each, joined by `;` (`scripts/prepare_import.py`). After
a start, `scripts/check_load.py` compares every label's row count with its CSV files and the indexes with the design.

**Settings.** The server is pinned to one CPU with `ENGINES_SETTING=equivalent`, as the other databases are. LMDB
reads its file through a memory map: it has no block cache, and TuGraph keeps no data cache above it, so there is no
cache to size or switch off: the warm regimes are one for it, and a regime with direct reads (`steady`) is refused. At a
cold start (`ENGINES_REGIME=cold-bounded`) the restored file is put out of the OS page cache before the launch, and
the start's check counts no rows, so the first query finds the store unread. `durable` is off (the release's default).

## Two configurations

TuGraph's per-label option `detach_property` decides where a vertex's properties are stored, and it changes what a
query reads. Both settings are loaded, each as a configuration with its own db key and its own backup:

| `--db` | `detach_property` | a vertex's properties | backup |
|---|---|---|---|
| `tugraph` | false | in its packed record, with its edges while the record is under 1,000 bytes (TuGraph's `src/core/graph_data_pack.h`) | `backup/runs-sf<SF>` |
| `tugraph-detached` | true | in a table of their own, each property read by a GET | `backup/runs-sf<SF>-detached` |

`lgraph_import` at v4.5.2 defaults to true, though its documentation says false; our import config writes the
setting for every label, so the tool's default never decides. The settings are written once, in `lifecycle.sh`
(`tugraph_configuration`). The two use the same ports, so one runs at a time; a start refuses a backup loaded as the
other. Every command above takes either key (`--db tugraph-detached`).

## Does TuGraph hide KV operations behind a cache?

No. `scripts/check_repeat_counts.py` runs one query twice and once more on a freshly started server, and compares
the counts per table and call:

```bash
SF=0.3 .venv/bin/python3 engines/tugraph/scripts/check_repeat_counts.py \
    --query engines/queries/microbench/tugraph/hop1p7_knows.cypher --param personId=1050
# tugraph hop1p7_knows personId=1050: counts equal on run 1, run 2 and a fresh server (63 rows; GET 1, SEEK 130, NEXT 36; ...)
```

It checks the configuration that is running; for the other, start it first and set `ENGINES_DB=tugraph-detached`.

The one GET is TuGraph's check of the request's token, on its user table; every request pays it.

## What is here

| path | what |
|---|---|
| `get.sh`, `build.sh` | fetch the release; build the counter library (checks the release's symbols first) |
| `kvstats/tugraph_kvstats.c` | the counter library: the call mapping, the bytes, the `/kvstats` listener |
| `schema/ldbc_import.json` | the physical design `lgraph_import` loads |
| `load-data.sh`, `start-server.sh`, `lifecycle.sh` | load and back up; restore and start (called by `../load-data.sh`, `../start-server.sh`) |
| `scripts/prepare_import.py`, `scripts/check_load.py`, `scripts/check_repeat_counts.py` | the import's inputs; the load check; the cache check |
| `scripts/stop.sh`, `scripts/status.sh`, `justfile` | stop, status |
| `.env` | paths and ports |

## Expected run time

On a 56-thread Intel Xeon Gold 5512U, LDBC sf0.3: `get.sh` 3 s, `build.sh` under 1 s, the load 6 s
(`lgraph_import` itself 2 s; the data dir is 234 MB; `tugraph-detached`: 10 s, 397 MB), a start with its check 15 s.
