# measure: the capture files and their columns

A **capture** is one run of `grid_bodies.py` (or `grid_writes.py` for the writes) over a set of stores: every legal
body of every query, on each store, on the same params. `output_tree.py` names every output path; ask it, never join one
by hand (`python -m kvevaluator.output_tree -h`).

A capture lives at `output/captures/<dataset>/<scale>/<workload>/<params>/`: `<dataset>` is `ldbc`, `linkbench` or
`imdb-<mapping>`; `<workload>` is `interactive`, `microbench`, `bi`, `writes`, `linkbench` or `job`; `<params>` is
`reported` (the workload's default param set), `held-out`, or another param set's name (`uniform`, LinkBench's `A`).
Every layout and every named format measured at that place sits side by side in it, one file per kind:

| file | kind (`output_tree.KINDS`) | one row per |
|---|---|---|
| `counts/<layout>.csv` (+ `counts/<layout>.json`, the run record) | counts | run: (query, param, body) |
| `steps/<layout>.csv` | steps | step of a run |
| `parts/<layout>.csv` | parts | (step, part of the schema it read) |
| `row-hashes/<layout>.csv` | row-hashes | step: a hash of its output rows |

**A writes capture** (`grid_writes.py`, the `writes` and LinkBench writes workloads) has the same columns as a predicted
write cell: `capture_columns.WRITE_RUN_COLS` plus `payload_bytes` (the write's logical payload: 8 B
per id plus its props), `STEP_COLS`, `WRITE_PART_COLS`, and no `row-hashes/`. A write plan has one body (its name);
`answer_key` and `run_key` are empty. Read it through `capture_layout.write_rows`: it also reads the older shape (the
counts driver's columns: `value_bytes`, `get_bytes`, `total_bytes`, `db`, the lanes as `<lane>_bytes`), which the
frozen snapshots keep. `grid_writes` rewrites a live capture of the older shape in place on its first run
(`grid_writes.upgrade`: a re-parse; the record says `upgraded_from`). Its steps and parts files end with the exact write units, `put_units_1k` and
`del_units_1k` (`WRITE_STEP_COLS`, `WRITE_PART_COLS`; table below); a file written before them reads them as empty, and
`bill_exact` refuses it by name.

**A predicted capture** has the same layout and columns, without `row-hashes/`. It is written by `kvevaluator predict
--plans` (predict mode: every body priced from the statistics, no store opened), one dir per level (`--out-predicted
DIR`, and `--out-predicted-anchor DIR` with `--anchor-stats`, each from `python -m kvevaluator.output_tree`). The caller
names the set: `kvcost predict` names it by the statistics tier, `predicted-tier1` (the default) or `predicted-tier2`
(the population statistics), and, when asked, its `-anchor` twin (with the anchor statistics: off by default since
`kvcost predict --anchor`). Its rows say `measured_as` =
`predicted`. Only KV steps have a step row, numbered as in a measured capture; a write step is one (its PUTs, DELs, the
bytes they write and its own reads, `rows` 1). A cell that holds a write request (the writes family) adds `P`, `D`,
`write_key` and `write_value` to its counts and parts columns (`capture_columns.WRITE_RUN_COLS`, `WRITE_PART_COLS`); a
read-only cell's columns do not change. A column predict does not price is empty:
the histograms, the fetch, write-unit, codec, chunk and clock columns, `result_hash`, `answer_key`. A step has one part row per
store part it reads or writes, under a measured capture's names (`props:<Label>`, `adj:<group>:<etype>:<dir>`,
`index:<Label>[.<prop>]`); the parts add up to the step row on every lane (the binary stops with exit 2 if not). Two
names can differ from a measured capture's: a probe that finds no list is filed under the owners' group, where a
measured capture files it under the probed vertex's group, and a read over several labels is split over them by the
statistics. The composer's units (`compose.unit_of`) do not depend on the group. Its record (`counts/<cell>.json`) names
the statistics and tally files with their sha256 (the anchor level's record the anchor statistics too: the
population level never reads them), the rules turned off, the spec, and the request set (its
count, queries and a sha256 over every request's name, plan and params); `kvcost predict` adds the workload, the plan
files' commit and pipeline, and the bodies it left out with why (measure-only, or not legal on the layout: a measured
capture runs every candidate) through `--record-fields`. A capture is one identity, as a measured cell is: a re-run
that moves any of these (another query set, param count, body set, tally, spec, statistics or rules) into the same dir
is refused unless `--fresh`, which replaces the cell whole. One dir for both levels is refused.


## Read this first

- **`answer_key` vs `result_hash`.** `answer_key` is the run's answer as every legal body must give it: the rows in the
  query's order, with ties and unordered output compared as sets (`candidates.result_key`). The answer vote compares
  it: two bodies of one query on one param must have the same `answer_key`. `result_hash` is a hash of the rows exactly
  as the body returned them, order included (raw). Two correct bodies can differ in `result_hash`, never in
  `answer_key`.
- **`cell` names a store,** not only a grid cell: a meta cell (`props_split_adj_split`), a mix, or a named format
  (`dgraph-c512k`, `nebula`).
- **Byte columns are bytes.** In these files a byte lane has no `_bytes` suffix (`get_key`, `scan_value`); every lane is
  a byte count.
- **Counts are deterministic.** The same store, plan and params give the same counts. `wall_ns` is the only clock in
  these files: the counts run's own clock, never a latency of record (latency comes from the latency drivers).

## counts: one row per run (`capture_columns.RUN_COLS`)

| column | what it holds | unit |
|---|---|---|
| `sf` | the scale (a LinkBench or JOB scale tag for those datasets) | |
| `cell` | the store (see above) | |
| `query` | the query (`ic3`, `bi7`, ...) | |
| `param_idx` | the param row the run used, 1-based in the query's param file | |
| `body` | the plan body that ran (a twin's body is `<twin>:<body>`) | |
| `measure_only` | 1 = measured and reported, never picked (e.g. a body whose gain depends on the params, not the layout) | 0/1 |
| `ok` | 1 = the run finished | 0/1 |
| `error` | why a run did not finish or did not run (`not run at SF30: ...` = left out by the SF30 rule; `invalid on this cell: ...` = the store's capabilities do not allow the body) | text |
| `S`, `G`, `N` | SEEKs, GETs, NEXTs | ops |
| `S_miss`, `G_miss`, `N_miss` | the part of S, G, N whose landing was not the key or range asked for (a miss never reads a value) | ops |
| `get_key`, `get_value` | key and value bytes of the GETs that hit | bytes |
| `scan_key`, `scan_value` | key and value bytes of the range reads (SEEK + NEXT landings that were used) | bytes |
| `seek_miss_key`, `next_miss_key`, `get_miss_key` | key bytes of the missed landings | bytes |
| `read_bytes` | the sum of the seven byte lanes above: the reported "bytes read" | bytes |
| `rows` | the rows the run returned | rows |
| `result_hash`, `answer_key` | see "Read this first" | hash |
| `body_sha` | the body's identity: a hash of its steps and output after fragment expansion, the same on every store | hash |
| `run_key` | the reuse key: the body, the params and `CAPTURE_SHAPE` (the counting versions are compared per record: `measure/counting.py`); a later capture reuses a row only on an equal key | hash |
| `wall_ns` | the counts run's own clock (see above) | ns |
| `measured_as` | empty = measured in this row; else the run it was copied from (`<body>@p<param>`: the same body under another name, or a query with no params) | |

## steps: one row per step (`step_record.columns`)

The steps of each run, in order. `query`, `param_idx`, `body` match the counts row; `step_idx` (0-based), `step` and
`op` name the step.

| column | what it holds | unit |
|---|---|---|
| `kind` | `kv` = the step calls the store; `flow` = it only moves rows | |
| `rows`, `out_bytes` | the step's output: its rows, and their size in memory, neutral to the format (`value::neutral_bytes`) | rows, bytes |
| `S`, `G`, `N`, `P`, `D`, `S_miss`, `G_miss`, `N_miss` | the counts row's ops for this step, plus PUTs (`P`) and DELETEs (`D`) | ops |
| `get_key` ... `get_miss_key` | the seven read byte lanes, as in counts | bytes |
| `write_key`, `write_value` | key and value bytes a PUT or DELETE wrote | bytes |
| `used_key`, `used_value`, `used_write_key`, `used_write_value` | the part of the bytes read (written) that the plan asked for (a hop that wants neighbour ids on a Split adjacency reads the edge property values and uses none) | bytes |
| `decode_ns`, `decompress_ns` | time spent decoding and decompressing values | ns |
| `chunks_decoded` | chunks decoded | chunks |
| `decode_requests` | decode requests, by kind (`row:3 posting:1`; kinds `row cell posting chunk doc`) | text |
| `fields_decoded` | fields materialized | fields |
| `decoded_kvbytes` | the stored bytes of the values parsed (never their size in memory) | bytes |
| `keys_crossed` | cursor landings on a key whose value was never read | keys |
| `rows_in`, `rows_in_labelled` | the rows the step took in, and how many of them carry a label | rows |
| `get_kvpair_hist`, `range_read_hist`, `keys_per_range_read`, `put_kvpair_hist`, `del_kvpair_hist` | one entry per request, as `bucket:count` pairs: bucket 0 = size 0, bucket i = [2^(i-1), 2^i) bytes (keys for `keys_per_range_read`); `u` = a deleted item of unknown size; empty = not measured | text |
| `get_fetch_4k`, `range_fetch_4k` | per request, ceil(its bytes / 4096), summed | 4 KB units |
| `get_store_blocks`, `range_store_blocks` | the same requests in the store's unit: an item of 4 KB or more is one block, smaller items of one request share 4 KB blocks | blocks |
| `put_units_1k`, `del_units_1k` | a write capture's steps and parts only: per PUT and per DELETE of a known size, ceil(its key + value bytes / 1024), at least 1, summed; a DELETE of unknown size 1 (the bill's exact write units) | 1 KB units |
| `chunk_probes`, `chunks_charged`, `chunk_batches`, `chunks_distinct_max_batch`, `held_chunk_bytes_peak` | the chunk statistics from the landing replay (the same at every P) | chunks, bytes |
| `round_trips`, `round_trips_critical`, `range_pages_extra` | round trips against a remote store: per level, 1 for its GETs and 1 per range read or page; the levels a client waits through; the pages after a range read's first | requests |
| `wall_ns` | the step's clock in the counts run | ns |

## parts: one row per (step, part)

The read columns of a step split by the part of the schema each read touched (`part`: `props:<label>`,
`adj:<label>:<etype>:<in|out>`, `eprops:...`, `index:<label>.<property>`), so a layout can be priced part by part (`kvcost.layout_cost`).
Columns (`capture_columns.PART_COLS`): the step's keys, `part`, the six ops, the seven read byte lanes, the five histograms,
the four fetch columns and the four decode columns, each as in steps.

## row-hashes: one row per step

`rows_digest` is a hash of the step's output rows in order, `rows_set_digest` the same with the order ignored; `rows`
is their count. `tests/gates/meta/check_row_order.py` compares them across stores: what flows between steps must not
depend on the layout. A check, not a statistic.

## stale: a result made from an input that moved

When a step meets an input that moved since it was made (a body whose sha changed, a dirty binary, another process on
the machine while timing, a records stream whose dumping code changed), it warns once and goes on, and what it writes
carries the mark (`python/kvevaluator/stale.py`; these were refusals). A result with no mark is clean.

- **A run record or meta** (`counts/<store>.json`, a latency cell's record, a summary.json) gets a `stale` list, one
  entry per (kind, detail): `{"kind": "body_moved", "detail": "bi3 hand: 2bb1f0 -> a4eca1", "at": "<module.function>"}`.
- **A table whose unit is a row** (a gate CSV, `workload_costs.csv`) gets a `stale` column: the row's kinds, joined by
  `;`, empty when clean.
- **A dir** holds a shell step's notes, folded into the records written there: `<dir>/stale.jsonl` for every record of
  the dir, `<dir>/stale-<unit>.jsonl` for one unit's (a cell, a store). An analysis step whose outputs are tables
  writes its own `stale-<step>.jsonl` beside them, rewritten each run. A store holds the notes of the stream it was
  built from in `<store>/stale.jsonl`.

The kinds and what each means: `python -m kvevaluator.stale kinds`. A mark is cleared by making the result again from
today's inputs (the detail names what moved; most name the command). `--reuse-stale` (kvcost measure,
kvcost predict, run/predict-accuracy.sh) keeps a result whose freshness key moved, marked `reused_stale` with the
parts. The gates still refuse: a check stays a check.
