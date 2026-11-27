# kvcost.layout_cost: the model layer for layouts and mixes

The shared code that turns measured KV counts into the cost of a layout or a mix. It opens no store and measures
nothing (the C++ evaluator measures; `python/kvevaluator/measure/` runs it). Imported as `kvcost.layout_cost`; the exact
search and the Pareto front are in its sibling `kvcost.layout_search` (`search.py`, `front.py`: their rows below are marked as theirs).

| module | holds |
|---|---|
| `settings.py` | the three layout settings (Split, Condensed, Chunked), the nine uniform layouts they make, their short names (`Co-Sp`, `Co-Ch64k`: `kvevaluator.layouts.name_of`), the reference layout every ratio is to (Sp-Co), the byte limit's unit (`B_UNIT`: Sp-Co's bytes, Co-Sp's before) and the named limits (`NAMED_BUDGETS`: B = 1, 2, 3, 5); the chunk sizes of the Chunked setting: the grid's (`GRID_CHUNK_KB`, `grid_size`), the swept ones (`CHUNK_SIZES_KB`), a size's option id and names (`size_option`, `size_name`) and the `--size 4k[=<root>[:<cell>]]` flags (`size_options`, `parse_sizes`, `sized_cell`: a bare size is found
where the caller's source keeps it) |
| `cost_models.py` | the three reported cost models: wops = G + 1.1 S + 0.01 N (+ PUTs and DELs), bytes = the 7 read lanes + bytes written, wops_bytes4k = wops + bytes / 4,096. The weights come from `python/kvevaluator/weights.py`, 4,096 from the pick rule (`python/kvevaluator/plan_choice/optimizer/criteria.py`), the lanes from `python/kvevaluator/capture/step_record.py` |
| `query_weighting.py` | how a benchmark's per-query costs add up to one score per layout: a frequency-weighted sum, a geomean or an additive mean |
| `picks.py` | THE pick of a grid cell's plan per query (`choose_cell`: the plan rule from `python/kvevaluator/plan_choice/candidates.py` on the held-out rows, and the pick record's decision when the capture's scale is not the deciding one); `PlanRule`, the plan rule in one value that `Composer` carries and `choose_cell` takes (no process-wide switch): the record (`record=None`: each grid by its own held-out rows, as the scale checks price), its anchor and pin, in-sample from the rows (a capture without held-out rows: in-sample for the rule's default families, or for any with `in_sample=True`), `historical`. The capture's rows are `python/kvevaluator/measure/grid_capture.py`; the record is `inputs/picks/picks.csv` (with `pick_stores.json`), read through `pick_record.load` |
| `compose.py` | a layout's cost from the measured cells (`load_reads`, `load_writes`, `load_measured`, `price`) and a mix's cost from the parts (`load`, `query_cost`, `cost_mix`) under one `Composer` (the criterion, the plan rule, the option set, the sizes, the families, the weighting; the criterion, the plan rule and the option set have no default: each caller names them): each Split or Condensed part's rows from the cell whose layout for that part's component is the mix's, each Chunked part's from its chunk size's Chunked/Chunked (the grid's 64 KB too: `chunk_sizes`). Its CLI checks every uniform layout priced from the parts against the measured cell (`python3 -m kvcost.layout_cost.compose --sf 0.1`). |
| `composed_vs_measured.py` | the one comparer of a BUILT mix's measured counts with its cost composed from the uniform captures (`compare`): every plan pick, every candidate set, every (query, param, plan, step, part) row in every raw channel, the workload sums against `compose.cost_mix`; Chunked at any chunk size (the sizes read from the mix's spec, `sizes_of`); the one miss-key rule (`MISS_RATIO`, `miss_gap_ok`: per (mix, workload), the miss-key bytes within 0.5x-2x of the composed, its reason in the docstring). Its callers: `kvcost check-mix`, `tests/gates/check_mix_vs_built.py`, `experiments/mix_benefit/composed_vs_measured.py` |
| `compose_cells.py` | the uniform-layout composer, the only one that writes a layout's capture: every uniform layout not measured itself, at any chunk size (Sp-Ch4k from Sp-Sp and Ch4k-Ch4k; default every pair of the measured diagonals), its capture files written from the two diagonals (property and index part rows from the P/P capture, adjacency part rows from the A/A one; the planner and measure-only flags from the cell's own spec), so a scale with only the diagonal measured is read by every nine-cell tool. Each part row's source is its layout unit's (`compose.unit_of`; an index part from the props source). `--check` compares with measured files (each composed cell: 0 rows differ on the 10-24 and 10-25 snapshots' SF3 LDBC and BI, LinkBench n1m and JOB full); `--dataset ldbc|linkbench|bi|job` (bi and job: reads only); `--cells` writes only the named layouts (any chunk size), from their own sources, and keeps the composed place's others; each composed run record holds `wall_s` and `composed_from_sha` (its source files by content: `note_if_stale` marks it stale when one was measured again); `--chunked-from` takes the Chunked diagonal from another spec set (the 4 KB cell). Each composed cell gets `exact/<cell>.csv` beside its runs: per runs row its query's mark, `yes`, `upper bound` (in-sample), `unknown` (held-out) or `no pick`, and why (the rules in the script's docstring). A row on one source only, two sources' different answers, or parts that do not sum to their run are PROBLEMs (exit 1). Reads a grid root with the diagonal cells (reads, held-out, writes; BI under `bi/`); writes a NEW grid root (never the measured one). Its check: `--check` |
| `layout_search/front.py` (the sibling) | a workload's complete Pareto front in wops and bytes, or in another axis pair (below) (`complete_front`: from the fewest-wops mix down in bytes, one exact solve per step) and a mix priced per part (`cost_by_part`, `mix_record`); the workload, its data and its problem are the caller's (I01's driver: `experiments/mix_benefit/w16_complete_front.py`); the byte limits of a sweep (`caps_of`, `CAPS`, the rule; a named limit is `reference_x<B>`), the built mixes measured beside a front (`built_points`), and one reader of a front file's named limits for every script (`ref_bytes_of`, `b_kind`, `b_of`, `x_of`: a file in Co-Sp units names them `nebula_x<B>`, `cap_x_nebula`, `bytes_x_nebula`, in Co-Sp's bytes) |
| `cost_table.py` | the one cost table `kvcost cost-layouts` writes for every source (measured, composed, predicted, a mix): one row per (layout, query), the picked body and its cost per model beside the raw channels |
| `compare.py` | two cost sets over the same layouts compared, the one definition (`kvcost predict-accuracy`, `kvcost.predict_accuracy.layouts`): Spearman, A's pick judged on B, top-5 overlap |
| `mixes.py` | a mix's assignment ({unit: setting}): its text (`assignment_label`, `parse_assignment`, `unit_name`; 'knows lists=chunked; Message props=condensed'), the configurable-format spec that builds it (`spec_of`) and the assignment a spec builds (`assignment`, matched against the grid's own components, `grid_settings`) |
| `workloads/` | the texts behind the workloads (`QUERIES`: what each query does; `PARAMS`: what its params are; designed.py, ldbc.py, linkbench.py, job.py) and `make_workloads.py`, which writes the workload files a recipe computes: I01 (LDBC SNB Interactive's own mix, from `gen_ldbc_mix.py`'s `mix.json`) and I02 (its reads only), L01-L04 (LinkBench's logged request mix at a scale), J01 (the Join Order Benchmark's 113 queries once each). The workloads themselves are files, `inputs/workloads/<benchmark>/<id>.json` (W01-W15 written by hand, each with its prediction written BEFORE measuring and a `why` written after), read by `benchmarks.workloads()`. Not the query plans: those are `python/kvevaluator/workloads/` |
| `benchmarks.py` | the datasets the pricer prices (`DATASETS`, the `--dataset` flags: `ldbc`, `linkbench`, `imdb`): each one's workloads (its manifest's `workload_files`; a workload of an optional family, LDBC's BI, only when named), axis (the scale) and families (read / write) (`families`, `workloads`, `select`; the grid's captures are `output_tree.capture_in`); also where `experiments/layout_choice` writes its analyses (`out_root`, `workload_dir`, `add_root_arg`), which is that study's output layout |
| `labels.py` | how figures and READMEs name these things: the combined cost (`SUM_LABEL`, `sum_text`), the pick rule (`rule_label`), a workload (`display_title`) and a suite's axis value (`axis_label`); the byte limits' names and the gain's text (`BUDGETS_NOTE`, `B_STYLE`, `gain_text`) and the pricer's figure colours (PUT, DEL, bytes written, the read of a read-modify-write, the two terms of the blend, the limit) |
| `layout_search/search.py` (the sibling) | the exact mixed-layout search (`Problem`, `solve`, branch and bound over per-query cost tables), over several workloads in one search order (`Joint`) and as per-query arrays (`Additive`); its tables built once per driver run or kept across runs (`problem`, `problems`, `set_cache`; the cache key covers `sources()` (the fill's import closure) and each extra table's code, the names it reads and its closure, `extra_key`) |

**A front's axis pair.** `kvcost search-mix --front` walks wops against bytes. With a bill model (`--front --cost-model
bill_exact`) it walks that form's read units against its write units: `layout_search/questions.py`'s `Axes` names the
two currencies and the two lanes of a priced vector they come from (`cost_models.BILL_UNITS`), the problem holds the
two lanes as extra tables, and `Problem.pair(x, y)` puts them where "ops" and "bytes" are, so the solver, the bounds,
the staircase and the walk run unchanged; each point is priced again from the captures on the same two lanes. The row's
model is `bill_exact:read,write`; a complete walk also names its cheapest point at each write price r in
`questions.BILL_R` (bill = read units + r x write units, `cost_models.bill_at`).

**Its layer.** This package is part of `kvcost`: it imports `kvcost` and `kvevaluator` only, never a study; a study
imports it. `tests/lint/check_layers.py` checks that.

The pricing code that studies still hold (the other
searches, the pick record's writer `python/kvcost/layout_cost/grid_pick_record.py`) moves here step by step (the artifact move).

## The chunk view

**What it decides.** What a write is charged when it touches a family's LAST chunk. In a Chunked layout a new vertex
lands in its label's last props chunk, and a new edge whose owner is the newest vertex lands in that direction's last
adjacency chunk. The write reads that chunk whole and writes it back, so its bytes are the chunk's bytes.

**Why the measured bytes are not used by default.** How full the last chunk is depends on where the earlier chunk
edges happened to fall, not on the layout. At SF3, Chunked at 64 KB, a change that moves the chunk counts by 0.1% (Comment
9,654 -> 9,663 chunks) can take the last Comment props chunk from 54,523 B (652 rows) to 905 B (13 rows),
and W11's Chunked Message props from 109 to 32 MB. A counts run makes 5 inserts per write
query, all on the same last chunk; 5 Comment rows are 320 B, about 0.6% of a 56 KB chunk, so the run cannot average
the fill out. Over time a last chunk is anywhere from empty to full: the long-run mean read is half a full chunk
(28,038 B for that family, against 1,033 B and 54,651 B from the two starting fills).

**The two views** (`compose.chunk_view`, `Composer.chunk_view`; in kvcost the cost model decides it, the bill models
measured and the others half; the study scripts' `--chunk-view` flag):
- `half` (the default): each last-chunk touch is charged the family's chunk target / 2 in place of the chunk's bytes,
  on the read (`scan_value`) and on the write's base (`write_value` keeps the new row's own bytes). Keys and
  S / G / N stay as measured.
- `measured`: the bytes the capture found.

**What the capture keeps.** The C++ evaluator records every last-chunk touch in a write's step and part rows and its
counts row, in seven columns (`inputs/contract/capture_columns.json`, group `last_chunk`): `last_chunk_reads` and
`last_chunk_writes` (the touches), `last_chunk_read_bytes`, `last_chunk_write_bytes` and `last_chunk_write_base_bytes`
(the chunk's value bytes read, written and before the write), `last_chunk_read_target_bytes` and
`last_chunk_write_target_bytes` (the chunk target per touch, summed). A chunk is the last when its key is the family's
final key in the store (one uncounted probe, audited as `raw_seeks`). Both views come from one capture.

**Old captures.** A writes capture made before the columns has none: under `half` it is refused on
every cell (a cell's name does not say whether a part is Chunked: a mix, Per-type CSR). Price it under the measured view (kvcost:
a bill model; a study script: `--chunk-view measured`) until it is re-measured. Every loader that prices a write takes the view with no default, so two views
never meet in one comparison; the cell view (`kvcost` views, the golden costs) and the predict-accuracy reports read
`measured`: they show the capture, or an estimate against the bytes its run found. A search result row records its view (`chunk_view`); a row without one was priced
measured. `bill_exact` prices each measured request rounded up alone, from the capture's exact unit columns (reads:
`get_fetch_4k`, `range_fetch_4k`; writes: `put_units_1k`, `del_units_1k`), so it runs under `measured` only. A writes
capture without the write units' columns (made before the binary counted them) is refused by name under `bill_exact`.
