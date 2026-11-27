# latency: the shared readers of latency captures, and the grid latency driver

Several studies read the same latency captures (a cpplatency run's `steps.csv` per store and regime). The code they
share lives here, so no study imports another's. Imported as `kvcost.latency`. `grid_latency.py`'s
`picks_of` reads the pick through `kvcost.layout_cost.picks.choose_cell` (as meta's `plot_grid.load` does), skips a
query with no pick, and reads a module-level `CRITERION` that `--pick-by` sets.

| module | holds |
|---|---|
| `features.py` | a step row -> its features: S, G, N, the byte lanes (`GET_LANES`, `SCAN_LANES`, `MISS_LANES`, `BYTE_LANES`), the steady proxy's estimated blocks (`est_blocks` = G x ceil(GET bytes / G / 4 KB) + S x ceil(scan bytes / S / 4 KB); `BLOCK` = 4,096, the stores' data block size), bytes by component (`OP_CLASS`) and by declared layout (`LAYOUT`); a capture's and a regime's step rows (`load_capture`, `load_regime`, `regimes_of`) |
| `steps.py` | a capture's step records per request: the walls, the counts and the RocksDB timers (`PC_TIMERS`, also named `ROCKSDB_TIMERS`), per class and store (`per_request`, `store_totals`, `breakdown_of`), the KV timer's own cost per call (`timer_pair_ns`, `pair_ns`), the raw per-run medians (`raw_medians`), a regime's files (`step_files`, default regime `STEADY_REGIME` = `steady-307mb-p3`), the write records (`load_writes`, `writes_of`) |
| `steady_table.py` | the steady latency table's rule: per store and query the wall, KV time and combined cost of the body timed (`per_query`, its counts checked equal to the capture's), the writes' (`write_per_query`: one apply per param, a steady write capture), per query the mean over params (`OVER_PARAMS`), the equal-share weights (`equal_share`: 1 / the reference's cost) and the weighted ratios over the reference (`ratios`); read by `paper/derive/latency_steady_ldbc.py` and `experiments/job/steady_latency_table.py` |
| `steady_share.py` | the RocksDB share of steady latency per store, as a command (`python -m kvcost.latency.steady_share --timings <a workload's timings: Tree.timings> --weights <spec> --out <csv>`): steady_table's rule (`timer_query`, `mean_walls`, `shares`) over each store's KV-timer and breakdown captures beside its wall capture. The weights as the wall tables use them: LDBC Interactive `--workload interactive --weights frequencies:ldbc/3/i01` (the updates, not timed, left out), BI `--workload bi --weights equal-share:b01`, JOB `--workload job --weights equal-share:j01`. The writes' share (each write applied once per pass, its raw file the response itself: `raw_runs`) is `experiments/layout_choice/write_rocksdb_share.py`. It also holds the regime lookup by dir name, at any scale (307 MB at SF3, 3072 at SF30): `wall_regime` (the one `steady-<MB>mb-p2`), `write_regime` (the one `after-steady-warmup-<MB>mb-p1-noflush`), `cache_mb`; the paper's latency derives read through them |
| `i01.py` | I01's per-query class (IC, IS, IU: `qclass`) and its weights (`weights`): LDBC SNB Interactive's own mix |
| `grid_latency.py` | the grid latency driver: per cell and query, the plan the cell is priced at (`kvcost.layout_cost.picks.choose_cell`), timed with `measure/cpplatency.py` on the cell's store, then the body gate (each run's S / G / N, misses and 7 byte lanes equal the counts capture's row). Its usage is in its docstring |

Run the driver from the repo root: `python3 -m kvcost.latency.grid_latency --sf <SF> --counts <capture> ...`;
`--check` re-runs the gate on what is on disk, with no store. The drivers that call it: `experiments/layout_choice/run-w16-mix-latency.sh`, `experiments/meta/run-bi-cells.sh`.
Some snapshots name it by another path, `experiments/meta/grid_latency.py`.

**Its layer.** This package is part of `kvcost`: it imports `kvcost` and `kvevaluator` only, never a study; a study
imports it. `tests/lint/check_layers.py` checks that. Its gates:
`tests/gates/check_store_config.py` and `check_capture_kinds.py`.

## Expected run time

The paper's steady timing (`kvcost measure-latency`, the 12 uniform layouts and the timed mixes, 15 stores per
family; binary). From the paper's run records (`uv run --locked python -m kvevaluator.measure.run_costs --paper-results`, after `just fetch-results`; each record's `wall_s`, per store, with stores side by side); 28-core Intel Xeon Gold 5512U:

| family | cache | per store | all stores |
|---|---|---|---|
| Interactive (i01), LDBC SF3 | 307 MB | 299 s median, 881 s max (Sp-Sp) | 1.6 h |
| BI (b01), LDBC SF3 | 307 MB | 7,356 s median, 13,033 s max (Sp-Sp) | 34.8 h |
| JOB (j01), IMDb full | 370 MB | 8,120 s median, 13,186 s max (Sp-Sp) | 34.6 h, and the Sp-Co drift control 1,585 s |

The heavy step is BI's and JOB's timing: each takes about a day and a half, one store at a time.
