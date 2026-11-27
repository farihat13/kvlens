# LDBC params

Every query reads LDBC's hosted param file, at every scale factor, with no exception: always hosted; the census reports
stop-early rows, it decides nothing (2026-10-19). Each pair's
history is in `param_sources.json`.

## What is here

| file | what |
|---|---|
| `param_sources.json` | the source each (SF, query) reads (hosted) and, per pair that was once ours, its history |
| `sf<SF>/interactive/interactive_<N>_param.txt` | LDBC's hosted file, copied verbatim (the data dir is not tracked); sha256 in `sf<SF>/interactive/hosted_params.meta.json` |
| `sf<SF>/interactive/short/interactive_short_param_<N>.txt` | IS4-7's anchors (LDBC ships no short-read params), from cost_eval; IS5 and IS6 reuse IS4's rows; IS7's `.meta.json` says how it was picked (its generator, `gen-short-params.py`, is in the source repository, tag artifact-freeze-20261026) |
| `sf<SF>/interactive/ours/interactive_<N>_param.txt` | records only, no query reads them: the params we used before 2026-10-19, from `data/gen_ic<N>_params.py --sf <SF>` (in the source repository, tag artifact-freeze-20261026; not shipped: no query reads these); how each was made is in its `.meta.json` |
| `sf<SF>/interactive/uniform/` | the uniform param set: LDBC's rows 1-5 with the bound person (IC) or message (IS4-7) drawn uniformly at random, every other column LDBC's; read with `--params uniform` (`kvcost measure`, `kvcost predict`; `run/predict-accuracy.sh --param-set uniform`; its captures in their own rows dir, `<set>/interactive/uniform/`) (`python/kvevaluator/workloads/interactive/gen_uniform_params.py`; `uniform_params.meta.json` holds the seed and the draw). A check of predict mode, not the workload's params |
| `sf<SF>/interactive/param_census.csv` | a report: per hosted row 1-10, the answer's row count, the work on the reference cell, and whether the plan stops early |
| `sf0.3/interactive/param_census_ic5_rows_1_35.csv` | the same for IC5 over all 35 hosted rows (which rows give IC5 a non-empty answer) |
| `sf<SF>/interactive/forum_param.txt` | the wide reads' forum anchors (`python/kvevaluator/datasets/ldbc/gen_forum_params.py`) |
| `sf<SF>/bi/`, `sf<SF>/microbench/`, `sf<SF>/writes/` | the other query sets' params, each written by its own `genparams.py` (`python/kvevaluator/workloads/<query set>/`); deterministic, so a missing scale regenerates byte-identically |

IS1-3 read IC1's file. IS4-7 read cost_eval's short-read params (LDBC ships none). One function finds every folder
here: `kvevaluator.datasets.params_in(dataset, scale, query_set)`. LDBC's operation mix (I01's source) is a frequency
file: `inputs/frequencies/ldbc/sf<SF>/mix.json`.

## How to use it

- **The loader:** `python/kvevaluator/datasets/ldbc/params.py`, `param_file(sf, query)`, is the one place that resolves a file.
  Every reader goes through it.
- **The census:** run `python3 python/kvevaluator/measure/param_census.py --sf <SF>` from the repo root. It ingests the reference
  cell's store on first use. At sf0.1 it takes about 12 s, at sf0.3 about 65 s with the ingest. The tracked file only
  ever holds a complete census of rows 1-10: a run with a failed row writes to `output/logs/param-census/` instead.
- **The gate:** `python3 tests/lint/check_param_sources.py --rerun 0.1`, run from the
  repo root. It checks that the loader opens the hosted file for every pair, that the hosted copies match LDBC's
  files, and that every source in the table is hosted. It prints each census's stop-early rows as a report, never a
  failure. With `--rerun 0.1` it also re-runs the sf0.1 census and compares.

## Stop-early rows, reported

Some hosted rows make a plan stop at its first lookup: an empty answer for almost no work. We keep them, since the
benchmark's params are the benchmark. The census lists them.

- **IC7 at sf3** (read ours until 2026-10-19): of the 10 hosted rows, 7 are empty, and rows 1, 4 and 10 stop early at
  about 3.3 weighted ops, against a median non-empty row of 35.8 (`sf3/interactive/param_census.csv`).
