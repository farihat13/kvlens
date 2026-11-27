# inputs/: what every run reads, tracked

Each folder is one kind of input, then the dataset, then the scale, as in the output tree (`captures/ldbc/sf3/...`).
A new scale or dataset is new files here, no code: `kvevaluator.datasets` finds each folder (`python -m kvevaluator.datasets
params ldbc 3 interactive` prints one).

| folder | what | example |
|---|---|---|
| `schemas/<dataset>/` | the graph's schema, compiled once (both languages read it) | `schemas/ldbc/` |
| `params/<dataset>/<scale>/<query set>/` | the query parameters each query set runs on | `params/ldbc/sf3/interactive/interactive_5_param.txt` |
| `frequencies/<dataset>/<scale>/` | the benchmark's own query frequencies at that scale | `frequencies/ldbc/sf3/driver.properties` (LDBC's) |
| `workloads/<dataset>/[<scale>/]` | a workload: which queries run and how often (I01 is LDBC's own frequencies, sf3's); `equal-share/` and `examples/` beside | `workloads/ldbc/i01.json` |
| `data-sources/<dataset>/` | where the data comes from: download checksums, a generator's recorded clock, and `expected.json` (what a fetch's steps take on disk and in time, for `just fetch-dataset`'s plan line) | `data-sources/ldbc/archives.json`, `data-sources/imdb/pins.json` |
| `specs/` | the layouts of the configurable format `meta`, one JSON each | `specs/grid/props_split_adj_condensed.json` |
| `contract/` | the facts C++ and Python must agree on: KV ops, capture columns, capabilities, the statistics format, every RocksDB setting (`store_config.json`) | `contract/kv_ops.json` |
| `picks/` | the pick record: which plan each layout runs for each query, and why | `picks/picks.csv` |
| `latency-picks/<dataset>/<scale>/` | the body each store times per query in a latency run, one file per read family (`kvcost measure-latency --save-picks` writes it) | `latency-picks/ldbc/sf3/bi.json` |
| `paper_results.json` | the pin of the paper's results: the results repo's url, its commit, the file count and its file list's sha256, written by `just pin-paper-results` | |
| `adoptions.json`, `moved_paths.json` | two records from earlier layouts: earlier results declared current, and files moved in the tree | |

Each folder's README says how its files are made.
