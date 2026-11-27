# mix_benefit: what a mix of layouts buys, in tables and figures

The layout choice (`../layout_choice/README.md`) prices every mixed layout from the nine measured uniform ones and
finds the best exactly. This folder shows what that buys, one view per script: at the same bytes, how many times fewer
ops a mix needs than the best uniform layout that fits; that no uniform layout is good on every benchmark; what each mix
on I01's front is made of; what the chunk size of the Chunked setting changes; and whether a layout chosen at SF3 holds
at SF30. It also checks that a composed price equals the measured cost of the built mix.

One plan rule everywhere: a query's plan is the one its capture picked (fewest ops + bytes / 4 KB on the held-out
params 11-15, 6-10 before, kept only if it is more than 10% cheaper than the default plan; cost reported on params 1-5). The three
models (wops, bytes, wops + bytes / 4 KB) only read that plan's cost; no plan is re-picked per model.

Analysis only: the chunk-size study's counts are `kvcost measure` lines (below). Working output goes under `output/summary/ldbc/sf3/` (untracked).
The results are in the paper's "Choosing the best KV Format" section and its Evaluation.

## Run

```bash
experiments/mix_benefit/run-mix-benefit-cross-benchmark.sh --grid <grid> --linkbench <dir> --job <job_picks.csv> --out <dir>
experiments/mix_benefit/run-mix-benefit-w16-front.sh [--scale 0.1]  # I01's complete front, all 3^21 mixes, figures (default the SF3 grid)
kvcost measure --dataset ldbc=3 --workload i01 Ch1k-Ch1k Ch4k-Ch4k Ch16k-Ch16k Ch128k-Ch128k Ch256k-Ch256k   # measures: one Chunked/Chunked store per size
kvcost measure --dataset ldbc=3 --family microbench Ch1k-Ch1k Ch4k-Ch4k Ch16k-Ch16k Ch128k-Ch128k Ch256k-Ch256k
#   (side by side: one line per size, `--no-check`, in a file for run/in-parallel.sh -j 2 --first-alone; then both
#   lines above with --check-only)
experiments/mix_benefit/run-mix-benefit-chunk-size.sh --options <I01 options.csv>       # the chunk sizes side by side
experiments/mix_benefit/run-mix-benefit-scale-check.sh --small <SF3 grid> --large paper-results/captures/ldbc/sf30 --out <dir>
kvcost search-mix --question i01-any-size-best --dataset ldbc=3 [--from paper-results]   # one mix question the paper reads
#   (the questions: python/kvcost/layout_search/mix_questions.py; the rows go under LDBC's scale; a JOB or LinkBench
#   question also takes its own pair, --dataset ldbc=3 --dataset imdb=full; a -larger one the larger LDBC scale, ldbc=30)
uv run --locked python experiments/mix_benefit/combined_other_benchmarks.py --dataset ldbc=3,imdb=full,linkbench=n1m --check-two-step
#   BI, JOB, LinkBench: the nine, the mixes from the questions bi, job, linkbench, the ceiling
# Figure 10(a) for LDBC BI and JOB (equal-share weights; not read by the paper yet): the searches, then the cost cache
kvcost search-mix --question bi-any-size --dataset ldbc=3           # B01: the staircase (default limits) and the 3-setting front
kvcost search-mix --question job-any-size --dataset ldbc=3 --dataset imdb=full   # J01: the staircase without the cost cache, best found per limit
uv run --locked python experiments/mix_benefit/front_by_part.py --workload b01 --dataset ldbc=3     # (and j01 --dataset ldbc=3,imdb=full)
```

The BI and JOB fronts (`front-by-part/<workload>/front-by-part-<workload>{,-points,-limits}.csv`, drawn by gs-paper's
`fig_front_by_part_49_layouts_w16.py --panel a`) use the same code as I01's (`kvcost.layout_search.front_by_part`). BI's
staircase is proven at every limit. JOB's cost cache does not fit, so each JOB limit is one solve without the cost cache capped at 30,000
nodes, started from `Bodies.fit_start` (the fewest-wops mix moved part by part to fewer bytes until it fits, then
improved within the limit). At SF3 the 30,000 nodes improve no limit's start, so each JOB point is the start
rule's mix (an upper bound on the best mix at that limit) with the search's bound beside it: the limits table gives
the bound and the gap (23-69%).

The drivers read the output dir by default (`output/captures/ldbc/sf3`, `output/captures/ldbc/sf30`); the paper's runs
are the same tree in `paper-results/` (`just fetch-results`): pass its dirs (`--grid paper-results/captures/ldbc/sf3`), or
`--from paper-results` to `kvcost search-mix --question`.

`combined_other_benchmarks.py` reads each benchmark's layouts by one rule (`output_tree.layout_capture`: measured itself in captures/, else composed/, by `kvcost compose`).

The smallest runs, at sf0.1 (the same pipelines; their numbers differ from the paper's). First the nine layouts and the
layout choice (`../layout_choice/README.md`): `kvcost measure --dataset ldbc=0.1 --workload i01 Sp-Sp Co-Co Ch1k-Ch1k Ch4k-Ch4k Ch16k-Ch16k Ch64k-Ch64k Ch256k-Ch256k`, `--family microbench` on the same seven, `kvcost compose --dataset ldbc=0.1`, then
`experiments/layout_choice/run-layout-choice.sh --sf 0.1`. With `OUT=$(uv run --locked python -m
kvevaluator.paths output_dir)`, `G=$OUT/captures/ldbc/sf0.1`, `S=$OUT/summary/ldbc/sf0.1` and
`LC=$S/layout-choice/reported/wops_bytes4k`:

```bash
uv run --locked python experiments/mix_benefit/fewest_mix_by_model.py --grid $G --options $LC --out $S/mix-benefit/fewest-by-model
experiments/mix_benefit/run-mix-benefit-w16-front.sh --grid $G \
    --front $LC/equal-bytes-front/i01-rw-ldbc-mix/equal_bytes_front.csv --out $S/mix-benefit/i01-front
uv run --locked kvcost measure --dataset ldbc=0.1 --workload i01 Ch4k-Ch4k          # measures one Chunked/Chunked store, then its check
uv run --locked kvcost measure --dataset ldbc=0.1 --family microbench Ch4k-Ch4k
for set in mixes grid_grouped groupings; do uv run --locked kvcost measure --dataset ldbc=0.1 --family interactive inputs/specs/$set; done
for set in grid_grouped groupings; do uv run --locked kvcost measure --dataset ldbc=0.1 --family microbench inputs/specs/$set; done
uv run --locked python experiments/mix_benefit/composed_vs_measured.py --sf 0.1 --grid $G
```

On a 16-core AMD EPYC 7302P with a SATA SSD, the sf0.1 stores already built: `fewest_mix_by_model.py`
639 s (pass: `check: 18 mixes equal options.csv's`); the I01 front 550 s the first time, 176 s with its all-mixes step
reused; the chunk counts 43 s and their check 3 s (9 LDBC + 9 microbench cells); the built and grouped cells 84, 153,
136, 59 and 73 s; `composed_vs_measured.py` 8 s (pass: `composed against measured (sf0.1): PASS`;, the
3 built mixes of `inputs/specs/mixes`, 7,356 step rows). `run-mix-benefit-chunk-size.sh` is for SF3: at sf0.1 its
compose, report, figure and 64 KB fronts run
(28 min), then it stops at the 4 KB front, which takes no `--sf` and prices with the SF3 pick record.

A run on archived captures prints one `[resolve_body] the capture's recorded commit <sha> is not in this repository`
line per capture commit. It is expected: each capture records the commit that measured it, in the source repository
this artifact was cut from, which the artifact's history does not hold. Its plan names are then resolved at HEAD. The candidate-drift check uses the body sha each run row records where it has one (the SF3 captures
 on); a capture without it (the SF30 grid) is compared by name.

## Paper outputs

Each is a table made by `paper/derive/<name>.py` and drawn by `paper/draw/fig_<name>.py` (`tab_<name>.py` for a
table). Each reads `paper-results/` (`just fetch-results`); the places below are in it. The two I01 figures read their
mixes' rows from `summary/ldbc/sf3/mix-search/i01/`.

- uniform_and_mixed_i01 (I01: the nine uniform layouts and three mixes under three models, from
  `summary/ldbc/sf3/layout-choice/reported/`, its `wops_bytes4k/` and `sum-optimum/`);
- pareto_and_fixed_i01 (I01's front and what it is made of, from `summary/ldbc/sf3/mix-benefit/i01-front/`);
- other_workloads_heatmap (the nine uniform layouts on the four benchmarks, from `cross_benchmark_frequencies.py`'s file
  in `summary/ldbc/sf3/mix-benefit/cross-benchmark-frequencies/`);
- composed_vs_measured (composed against measured: six uniform layouts priced from three, in
  `summary/ldbc/sf3/mix-benefit/section7/three-cells/`; the built mixes, in `studies/latency-mixes-sf3/` and
  `studies/latency-mixes-job/`);
- run_time (the run time of each step at SF3 and on IMDb full: the stores' manifests under `stores/`, the run records
  of the captures, statistics, tallies and predictions, and the mix searches' rows under `summary/<dataset>/<scale>/mix-search/`,
  partly from the run times below).

## What is here

| file | what |
|---|---|
| `kvcost search-mix --question NAME` | every best mix and front the paper reads, one question at a time, each question's calls written once in `python/kvcost/layout_search/mix_questions.py` (rows proven, but JOB's two-step mixes, `--search two-step`, and local searches, `--search local`): I01's per-type front (`i01-front`), its staircases per type and direction (`i01-direction`) and with Chunked at 1-256 KB (`i01-any-size`: at LDBC SF3 the paper's limits file, else the limits the rule gives on that scale's data), its fewest wops, bytes, wops + bytes / 4 KB and bill (`i01-best`, `i01-bill`, `i01-any-size-best`, `i01-any-size-bill`), BI, JOB and LinkBench (`bi`, `job`, `linkbench`, `l01-any-size`), B01 and J01 at equal share and once (`b01-spco-share`, `b01-once`, `j01-spco-share`, `j01-once`) (each row's times hold its two-step's two stages), the latency figure's mixes (`i02-any-size`, `w17-`, `b01-`, `j01-predicted`) and Figure 10(a)'s (`bi-any-size`, `job-any-size`); at a larger LDBC scale (`--dataset ldbc=30`): `i01-larger`, `b01-spco-share`, `b01-once`. The rows go to `summary/<dataset>/<scale>/mix-search/<rows>/` of the dataset the question searches |
| `limits/` | the byte limits of the staircases: I01's two (`i01_per_type_and_direction.json`, 75; `i01_per_type_any_size.json`, 128) as `{cap_bytes, cap_kind}`, J01's (`j01_per_type_any_size.json`, 16) as `{B, cap_kind}` (B x Sp-Co's bytes), kept as inputs (`kvcost search-mix --byte-limit file:<path>`); each file says the rule that made it |
| `run-mix-benefit-cross-benchmark.sh` | every table, figure and check of the cross-benchmark result: the solves (reused when present), the per-query costs and the regret tables, seven figures, the checks |
| `run-mix-benefit-w16-front.sh` | I01's complete per-type front at `--scale` (default 3): the front, all 3^21 mixes with the exhaustive check, six figures. |
| `run-mix-benefit-chunk-size.sh` | the Chunked setting at several chunk sizes side by side for I01 and I02: compose the nine cells per size, the report, the fronts, the figures |
| `run-mix-benefit-scale-check.sh` | does a layout chosen at a small scale hold at a larger one: compose, choose, the cost cache, the built mixes |
| `per_query_disagreement.py` | per workload family (I01, BI, IMDb / JOB, LinkBench), which uniform layout is cheapest per query -> `costs.csv` with the raw channels |
| `plot_per_query_disagreement.py` | its figure: each query's share of the workload's cost, above the best layout's cost on it over the query's own cheapest |
| `cross_benchmark_frequencies.py` | the nine uniform layouts on each benchmark under each way of counting its queries (once each, its frequencies, equal contribution); checks its ratios against the regret table |
| `plot_cross_benchmark_regret.py` | the figures: no uniform layout is good on every benchmark (heat map and bars) |
| `check_headline_weighting.py` | the check of the BI / JOB headline weighting: once each, additive and geomean recomputed with no shared code; `--perturb` must fail |
| `check_cross_benchmark_numbers.py` | the cross-benchmark result's tables and numbers re-derived from the captures, with no code shared with the pipeline; `--perturb` must fail |
| `random_mix_robustness.py` | random query mixes on BI and JOB: does the geomean's pick also win when the mix is drawn at random |
| `plot_random_mix_robustness.py` | its figure: per layout, the cost over each random mix's best |
| `combined_other_benchmarks.py` | LDBC BI, IMDb / JOB and LinkBench at SF3: the nine uniform layouts, the fewest-combined mix (per type and direction, no byte limit) and a ceiling, under wops + bytes / 4 KB; BI and JOB by the geomean (the mix solved on the additive form, w_q = 1 / c(q, geomean-best layout)), LinkBench by its log's counts. Each mix is read from a proven `kvcost search-mix` row (`--rows`; JOB by the solve without the cost cache, which does not fit); the row's score must equal the panel's own pricing. `--check-two-step`: the two-step search against the exact one on every workload the exact one solves. Input of the paper figure combined-bi-job-linkbench |
| `one_mix_per_dataset.py` | one mixed layout for the whole LDBC dataset (W01-W15, I01, I02 and BI), solved jointly and exactly under two objectives |
| `plot_one_mix_per_dataset.py` | its figure: per workload, the one mix, the best fixed uniform layout, the other eight, and the workload's own best mix |
| `fewest_mix_by_model.py` | the exact mix per type and direction that is fewest under each model (wops, bytes, wops + bytes / 4 KB), no byte limit, at SF3 |
| `plot_equal_bytes_curve.py` | the equal-bytes curve, I01 and I02: weighted ops against the byte limit for the mix and the best uniform layout that fits, and their ratio; `--grouped` adds the grouped-properties line |
| `composed_vs_measured.py` | composed against measured for every built mix a tree holds at SF3 (paper-results: studies/latency-mixes-sf3/), per channel and per step row, Chunked at any chunk size; exit 1 on any difference outside the miss-key bytes, or miss-key bytes outside 0.5x-2x of the composed in a (mix, workload) cell; `--plant` must fail. It finds the mixes and writes the files; the comparing is `python/kvcost/layout_cost/composed_vs_measured.py`'s, the one comparer `kvcost check-mix` uses too |
| `w16_complete_front.py` | I01's complete per-type front: every mix no other mix beats in both wops and bytes, each priced per part |
| `w16_all_mixes.py` | every per-type mix of I01 (3^21) priced from the per-query costs: a histogram, a sample, and the check that none beats the front |
| `plot_w16_front_makeup.py` | six figures of what the front's mixes are made of, coloured by each part's setting |
| `w16_front_common.py` | what the I01-front scripts share: the inputs, I01 loaded from a grid, its 21 parts |
| `front_by_part.py` | Figure 10(a)'s tables for B01 or J01 (`--workload`, `--dataset`): the front of every mix the `bi-any-size` / `job-any-size` questions' rows name, each mix by part, the points, and each limit's proven, bound and gap. I01's are made by the paper's derive (`paper/derive/front_by_part_49_layouts_i01.py`), the same code |
| `figure_common.py` | what the figures share: paths, the layouts' order, names and colours, the captions |
| `chunk_size_report.py` | what the chunk size of the Chunked setting does: per size the uniform layouts, owners served per chunk read, and the exact mix |
| `plot_chunk_size_single_vs_mix.py` | I01's best single layout against its best mix within each byte limit, per chunk size |
| `plot_equal_bytes_curve_chunk_sizes.py` | the equal-bytes curve with one mix line per chunk size |
| `plot_budget_gain_chunk_sizes.py` | the budget gain per chunk size: the best uniform layout that fits over the mix |
| `plot_budget_gain_3panel_chunk_sizes.py` | the three-panel budget-gain figure with one line per chunk size and one for any chunk size |
| `layout_scale_check.py` | I01 and I02: the layout chosen at the small scale (read from its mix-search result rows: `kvcost search-mix`, `kvcost search-mix --front`) priced at the large one, against the large scale's own choice; solves nothing |
| `scale_check_bytes_per_part.py` | where the bytes move between scales: I01's bytes on each adjacency part per list setting |
| `scale_check_owners_per_chunk.py` | why a Chunked list's bytes grow against Split with the scale: owners per chunk read and bytes per owner |
| `scale_check_record_vs_own_picks.py` | plan picks across scales: the recorded plan against the larger scale's own held-out choice |
| `scale_check_composed_vs_measured.py` | composed against measured for the built mixes at SF30, per chunk size |
| `scale_check_common.py` | what the scale-check scripts share |

## Expected run time

Analysis (32-core AMD EPYC 9354P, opens no store): `run-mix-benefit-cross-benchmark.sh` about 35 min the first time (its three solves),
30 s after; `run-mix-benefit-w16-front.sh` 15 min the first time (the front 3 min, all mixes 11.5 min on 32 cores), about
3.5 min after; `run-mix-benefit-chunk-size.sh` about 12 min with four sizes and the curve;
`run-mix-benefit-scale-check.sh` a few minutes (its
choices are 32 `kvcost search-mix` / `front` calls of seconds each). The questions together (28-core Intel Xeon Gold 5512U):
1,307 s against an estimate of 20 min (the any-size staircase 796 s, JOB's proof 173 s, the
per-direction staircase 98 s, the panel 81 s, the per-type front 68 s, LinkBench 64 s, the rest under 20 s each).
The opt-in questions (32-core AMD EPYC 9354P): `bi-any-size` 368 s with the mix cost cache cold (its 17 queries' costs 271 s,
the staircase 86 s, the front 17 s; 103 s with it warm), peak 9.1 GB; `job-any-size` 1,674 s (16 limits, 6 at a
time, each 0.5-15 min and ~1.7 GB; estimate 35-47 min); `fronts` about 30 s. `combined_other_benchmarks.py` alone: about 80 s for the panel (no solve; the two tars unpacked once),
about 10 min more with `--check-two-step` (38 exact solves; I01 and I02 per type and direction take most of it). The other scripts take seconds.

At sf0.1 (32-core AMD EPYC 9354P; the grid's seven measured, the nine composed): `run-mix-benefit-w16-front.sh
--scale 0.1` 169 s; the questions at sf0.1 (`kvcost search-mix --question <name> --dataset ldbc=0.1`), one by one: i01-front 42 s, i01-direction 14, i01-any-size 369 (45
limits by the rule; 213 s, in `just reproduce-all`), i01-best 41, i01-bill 6, i01-any-size-best 194, i01-any-size-bill 584 (the fewest bill and the bill's
front), bi 4, b01-spco-share with its bill call (since its own question, b01-spco-share-bill) 409, b01-once 412, two-step-times 15 (I01 and B01); JOB, LinkBench and the SF30 steps skipped (not in that tree). L01's 49 in the
measured chunk view on the n1m captures (`kvcost cost-layouts --dataset linkbench=n1m --workload l01 Sp-Sp Sp-Co ... Ch256k-Ch256k --cost-model
bill_exact`, which writes the measured view): 106 s.

The chunk-size counts, four sizes (1k, 4k, 16k, 256k) in three chains: the record stream 610 s once,
ingest 439-455 s per store, per store reads 104-123 s, held-out 98-116 s, writes 6-27 s, microbench 213-229 s; the run
23 min, the check 14 s.

### Expected run time, the scale check

The SF30 captures the scale check reads (a 32-core AMD EPYC 9354P CloudLab node with NVMe; estimate against actual):

| phase | at SF30 | estimate | actual |
|---|---|---|---|
| the pilot: the record stream (parse, once, 62.8 GB), one ingest, reads at 2 params | Chunked/Chunked at 4 KB | 1.2-2.7 h | 1 h 36 min (stream 3,053 s; ingest 2,469 s, peak 35.8 GB; reads 263 s) |
| ingest, two at a time | four cells; three mixes | - | 5,657 s; 5,401 s (largest peak per ingest 36.6 GB) |
| ingest, one store alone | the 4 KB mix | 41 min (the pilot's) | 2,554 s |
| LDBC reads, params 1-5, per store, two stores at a time | 315 runs | 479-638 s | 559-665 s |
| held-out params 6-10, per store | 315 runs | as the reads | 563-699 s |
| the writes workload, per store | 100 rows | - | 1-22 s |
| four cells, all phases | one chain | - | 2 h 40 min |
| the three 64 KB mixes, all phases | | 2 h 15 min | 2 h 11 min |
| the 4 KB mix, all phases | I01 within B = 1 (x Co-Sp's bytes), chosen at SF3 | 60-75 min | 65 min |
| the analysis, `run-mix-benefit-scale-check.sh` | opens no store | - | 957 s with the two layout solves side by side; 1,837 s one after another |
