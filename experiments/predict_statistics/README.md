# predict_statistics: naming a layout from statistics

Predict mode (`kvevaluator predict --plans`) prices each plan on a `meta` layout from two inputs, with no store: the
graph statistics (`stats.json` and `anchor_stats.json`, from `graph-stats`) and the layout's tally (its layout statistics: keys and bytes per store
part, chunk counts, locality; `kvevaluator tally`). This study runs it on **all 49 uniform layouts**: props and
adjacency each Split, Condensed, or Chunked at 1, 4, 16, 64 or 256 KB, through `kvcost predict` (a predicted capture
per level). Its answer is every layout predicted within 5% of the predicted best (`kvcost cost-layouts --count-source
predicted`); the comparison with the measured best is `kvcost predict-accuracy` (and, with a check workload or an earlier table, `python -m kvcost.predict_accuracy.layouts`).

**The row estimates** (each step's rows, the C++ cardinality layer, `cpp/kvevaluator/src/predict/cardinality.cc`) are
checked by `tests/unit/check_predict_rules.py`: one tiny graph per rule switch, where the rule gives the true count and
the switch off misses it. No Python copy of that layer is kept: one could check only the
configuration with every rule off, which no prediction uses.

**Seven tallies give the 49, and any mix.** A tally's rows are per store part. A label's props rows depend only on the
block that label's props are stored with, and an edge type's adjacency rows (per direction) only on the block its lists
are stored with; the index rows are the same in every layout. So the tally of any layout is, part by part, the rows of a
tally that stores that part the same way. The tally library (`kvevaluator.predict.tally_library`) keeps one tally per
setting, made by one pass over the records, and assembles any spec by lookup (`tally_for_spec`): the 49 uniform layouts
from the seven diagonals, and a per-type mix too (a block none of the seven has gets its own setting tally, built on
demand). Its key is the setting's spec, the record stream's sources (its freshness key without the commit that dumped
it) and the tally version (`kTallyVersion` in
`cpp/kvevaluator/src/cli/tally_version.h`, bumped by hand when a tally's rows can change; `just tally-version` fails a
change without a bump): an entry of the same version is reused whatever commit built it, so a pricing change does not
cost the seven SF30 diagonals again (~4 h). A tally made from a stream whose sources changed is refused (a re-dump of
the same sources by another commit is the same stream); a library writes only
below its run's own output dir (a settings dir linked from another tree is read, never rebuilt in place). Entries made
before the version existed: `python -m kvevaluator.predict.tally_library --stamp <dir>` adopts each one whose clean
binary has version 1's `cpp/` tree. `check_assembled_tallies.py` tallies layouts directly and compares them with the lookup, row
for row: the off-diagonals, the diagonals, and the I01 search's best mix.

**Can the layout statistics come from a sample?** Our encoder cannot run over a 1 TB dataset. A sample of
owners, each with its whole list, carries the id gaps (the bytes of a Condensed or Chunked list) and the id order (how
many chunks one owner's targets span). `kvevaluator tally --owner-sample P --seed S` computes the tally from P% of the
owners, scaled to the whole; `run-predict-statistics-sampled.sh` runs it on the seven diagonals, assembles the 49,
predicts, and compares both the rows and the layouts named with the full tallies'. It tests the information in a
sample; the pass over the records is still made (later the sample comes from the dataset's own database). The
sampled locality is an estimate with a known bias (the targets' chunk boundaries are guessed from the sampled owners);
`tally_errors.csv` reports it per owner-degree bucket (`locality_bucket`). A 1% sample can miss a small vertex type
altogether (SF3 has 71 TagClasses), so `--owner-floor F` keeps at least F owners of every type (a type with fewer is
kept whole) and scales each count by its owner type's own rate; the driver's `--floors 100` runs this "1% + floor"
variant beside each sample, in `p<P>-floor<F>-s<S>/`. The sample reads nothing outside it: a sampled Chunked family's
first id is the sample's lowest owner, and its last chunk (where an insert of a new vertex lands) is assumed half full,
the pack target x `last_chunk_fill` of inputs/contract/capabilities.json (the half chunk view's rule); a family kept
whole, and a full tally, keep their own.

## Run

```bash
# I02's 19 reads on the 49: the statistics and the 49's tallies when missing (stats/ldbc/sf<SF>/{tallies,uniform-49}/),
# the prediction into predicted/ldbc/sf<SF>/<set>/ (--tier T: predicted-tier<T>; tier 1 the default), then the named best against the
# measured 49, with I01's measured totals checked against --check-reference, into
# summary/ldbc/sf<SF>/predict-statistics/i02/<set>/ (uniform.csv, uniform_totals.csv: the layouts within 5%; layout_choice/)
kvcost predict --dataset ldbc=0.1 --workload i02 Sp-Sp Sp-Co ... Ch256k-Ch256k --tier 2 --jobs 16
kvcost predict-accuracy --dataset ldbc=0.1 --workload i02 Sp-Sp Sp-Co ... Ch256k-Ch256k --count-source predicted-tier2
# the check workload (I01's composed totals equal its measured 49's, kvcost cost-layouts --dataset ldbc=0.1 --workload
# i01 Sp-Sp Sp-Co ... Ch256k-Ch256k): python -m kvcost.predict_accuracy.layouts --check-workload i01
# --check-reference output/summary/ldbc/sf0.1/layout-costs/i01/measured (the place holds both chunk views)
# the same at SF3 (--dataset ldbc=3), and LDBC BI with --workload b01 (its measured 49: kvcost cost-layouts --workload b01 Sp-Sp Sp-Co ... Ch256k-Ch256k)
# the assembly check (a tally assembled by lookup equals the one tallied directly; at sf0.1 on every off-diagonal):
uv run --locked python experiments/predict_statistics/check_assembled_tallies.py --sf 0.1 \
    --records output/stats/ldbc/sf0.1/records.jsonl --out <a scratch dir> --cells all
# the sampled tallies, after the line above at the same scale (it reads that run's statistics and predicted captures):
experiments/predict_statistics/run-predict-statistics-sampled.sh --sf 3 --percents 1,10 --seeds 1,2,3
# with the floor variant beside each sample (p1-floor100-s1/, ...)
experiments/predict_statistics/run-predict-statistics-sampled.sh --sf 3 --percents 1 --seeds 1,2,3 --floors 100
# Figure 15's workloads (I01, B01 at SF3; J01 at the full IMDb) from the full tallies (once), then from the 1% + floor
# tallies, the full tallies without locality and with the sample's last chunk (the run: a bound then,
# read from the top owners whole; assumed half full)
experiments/predict_statistics/run-predict-statistics-sampled.sh --sf 3 --workloads i01,b01,j01 --variants full
experiments/predict_statistics/run-predict-statistics-sampled.sh --sf 3 --workloads i01,b01,j01 --percents 1 \
    --floors-only 100 --seeds 1
```

**The paper's predict-49 figure and run-time table read tallies counted on a sample of the owners:** 1% of the
owners, at least 100 per vertex type, seed 1 (`kvcost.predict_accuracy.uniform_49_tallies.PAPER_SAMPLE`).
`just results predict-49` and `just results runtime` run, per dataset, `sampled.py tallies --dataset D
--sf S --out output/summary/<dataset>/<scale>/predict-statistics/sampled --percent 1 --seed 1 --floor 100` (the seven
diagonal layouts tallied on the sample, the 49 assembled from them, into `.../sampled/p1-floor100-s1/`; a diagonal
already tallied on the same record stream and sample is kept), then per workload `kvcost predict <the 49> --tier 1
--tally-dir <that dir>/tally --save-as predicted-tier1-p1-floor100-s1` and `kvcost predict-accuracy ...
--count-source predicted-tier1-p1-floor100-s1`. The statistics (`stats.json`) and the measured side are counted on
the whole data. With `--tally-dir` the 49's own tallies and the tally library are not built.

The same figure from the 49's own tallies, counted on every owner, is the optional item `predict-49-full-tallies`
(`just reproduce-all` leaves it out): its chain builds the tally library and predicts into the tier's own set,
`predicted-tier1`; the same derive (`paper/derive/predict_49.py --set predicted-tier1`) and the same draw script make
it, with the line "predictions from the full tallies" above its panels.

At the paper's settings (op-byte cost, the 49): the sample names the layout the full tallies name on every
workload, for seeds 1, 2 and 3, also in wops.

| workload | layout named, full tallies and each seed | its measured cost over the measured best | Spearman's rho: full | seed 1 | seed 2 | seed 3 |
|---|---|---|---|---|---|---|
| I01, LDBC SF3 | Ch1k-Ch4k | 1.00x (the measured best) | 0.961 | 0.956 | 0.956 | 0.957 |
| B01, LDBC SF3 | Ch4k-Ch16k | 1.00x (the measured best) | 0.989 | 0.985 | 0.987 | 0.986 |
| J01, IMDb full | Ch4k-Ch4k | 1.04x (the measured best is Ch1k-Ch4k) | 0.955 | 0.949 | 0.949 | 0.949 |

A sampled tally reads the whole record stream and encodes the sampled owners only: the seven tallies' times sum to
804 s against 1,457 s for the full ones at LDBC SF3 and 1,346 s against 2,589 s at IMDb full (the sampled seven ran
seven at once), and a sampled tally holds little memory.

**LinkBench (n1m) on the 49** takes kvcost predict's run mode (it builds the statistics and the tallies itself, and with
`--anchor` the run's anchor statistics) and its request log, `--params log`, costed in-sample (LinkBench has no held-out set). l01 is the
log (reads + writes; an edge insert reads `degree_by_far_label`, which tier 1 writes in its flat form:
before, tier 1 refused those writes); l02 is its 7 read types (68,994 of 100,000 requests), at tier 1 and tier 2. the layout report (`python -m kvcost.predict_accuracy.layouts`) has no write side, so l01's pick comes from `workload_pick.py`:

```bash
python -m kvcost.cli.cli cost-layouts --dataset linkbench=n1m --workload l01 Sp-Sp Sp-Co ... Ch256k-Ch256k --cost-model bill_exact   # <m49> (the measured view):
    # summary/linkbench/n1m/uniform-49/l01-log/ (LinkBench's whole log: --params log, its default)
python -m kvcost.predict_accuracy.uniform_49_tallies specs <specs>
python -m kvcost.cli.cli predict --workload l01 --dataset linkbench=n1m <specs>/*.json --tier 2 --jobs 4
python -m kvcost.cli.cli predict --workload l02 --dataset linkbench=n1m <specs>/*.json --tier 1 --jobs 4
    # the 49 as spec files: a workload file's run (the request log, --params log; the tallies in the set's work/)
python -m kvcost.cli.cli cost-layouts --dataset linkbench=n1m --workload l01 --cost-model wops_bytes4k --count-source predicted-tier2
    # reads the output tree's predicted/linkbench/n1m/predicted-tier2/, writes its summary/linkbench/n1m/layout-costs/l01/predicted-tier2/wops_bytes4k/
python -m kvcost.predict_accuracy.layouts --dataset linkbench=n1m --predicted <output>/predicted/linkbench/n1m/<set>/linkbench/all \
    --grid <captures>/grid --cost-workload l02 --check-workload l02 --check-reference <m49 l02 csv> --param-rows all --pick-on in-sample --expect-layouts 49 --out <dir>
python -m kvcost.predict_accuracy.workload_pick --predicted <output>/summary/linkbench/n1m/layout-costs/l01/predicted-tier2/wops_bytes4k/uniform.csv \
    --measured <m49 l01 csv> --out <dir>
```

The results in `output/linkbench/n1m/` were predicted at `40cc7bb5`, with the anchor level written too
(the sets `predicted-anchor` and `predicted-tier1-anchor`). That level takes `--anchor`; the
reports read the plain sets only.

The sampled driver's first stage is the control: `--owner-sample 100` must give the full tally byte for byte (wall time and provenance
aside), on the three uniform layouts of the predict study (`--control-cells all`: the seven diagonals).

`kvcost predict ... Sp-Sp Sp-Co ... Ch256k-Ch256k` makes, when missing, the statistics (stats.json and anchor_stats.json, one graph-stats
pass; again when stale: another dump of the record stream, another statistics contract or graph-stats version), the
dataset's tally library (`stats/<ds>/<sc>/tallies/`: one tally per setting, the seven diagonals) and the 49 assembled
from it (`stats/<ds>/<sc>/uniform-49/specs/` and `tally/`: inputs of every predicted set and tier at that scale, beside
the statistics they come from); then every legal body of the workload's queries on each layout, params 1-5, one
binary call per layout, into `predicted/<ds>/<sc>/<set>/` (the requests and a log per layout in `<set>/work/`).
`kvcost build-statistics` makes the statistics and the tallies alone. `kvcost predict-accuracy ... Sp-Sp Sp-Co ... Ch256k-Ch256k` costs the predicted 49
(`uniform.csv`, `uniform_totals.csv`: the layouts within 5% of the predicted best) and names the best against the
measured one: a read-only workload with the layout report (`layout_choice/`: per workload the pick, the
fixed Sp-Co and Co-Co, the median layout and the worst, each x over the measured best, and the gap share
(Sp-Co - pick) / (Sp-Co - best); with `--check-workload i01 --check-reference <its measured 49>` the composed I01
totals must equal the reference's, or it fails), a workload with writes with `workload_pick` (`pick.csv`,
`layouts.csv`). The assembly check (`check_assembled_tallies.py`, no kvcost line: a check, not a step of the
pipeline) tallies `--cells` directly and compares them row for row with the assembled ones.

A pass of the assembly check looks like this:

```
check_assembled_tallies: 50 layouts compared (42 off-diagonal, 7 diagonals, 1 mixes); settings built this run 7 (39.8 s); 14184 rows, 0 layouts differ: PASS
```

`--plant` is its control: one key added to one assembled tally; it must exit 1, with one layout differing.

## Uniform-random params

LDBC curates its params (its parameter generator picks rows for stable query runtimes), so the persons in its rows
need not look like the population. Level B prices from population statistics, so its error on LDBC's rows mixes the
statistics' own error with that choice. `run-predict-statistics-uniform-params.sh`
runs predict mode's accuracy check on two param sets, side by side, on the same stores and binary:

- **ldbc-params**: LDBC's hosted rows 1-5 (the workload's params);
- **uniform**: the same rows with the bound person (IC1-12, and IS1-3 through IC1's file) or message (IS4-7) drawn
  uniformly at random, each query its own seeded draw of 5 (`inputs/params/ldbc/sf<SF>/interactive/uniform/`, made by
  `python -m kvevaluator.workloads.interactive.gen_uniform_params --sf <SF>`; the seed and the draws in
  `uniform_params.meta.json`). Every other column (dates, names, tags, countries, tag classes) is LDBC's row i, chosen
  for LDBC's person: IC1's first name or IC3's countries may match nothing for the random one. Most messages are
  comments with no replies, so IS7 (a message's replies) is often empty on the uniform set, where LDBC's short-read
  rows are stratified by replies. `compare.md` gives each (query, param)'s answer rows on both sets.

A query reads the uniform set with `--params uniform` (the driver passes it to `kvcost measure` and `kvcost predict`,
and `--param-set uniform` to `run/predict-accuracy.sh`); its captures go to their own params dir,
`captures/ldbc/<scale>/interactive/uniform/`, beside `reported` and `held-out`. The plan pick still reads LDBC's held-out rows 11-15.
The driver measures into an output tree of its own (`<output>/studies/uniform-params/`, the output dir of its measure
calls: `in_tree` in `run/env.sh`, the stores shared), so the main captures are untouched.

```bash
experiments/predict_statistics/run-predict-statistics-uniform-params.sh --sf 0.1 --cells all \
    --i01-reference <I01's 49 measured at that scale: summary/ldbc/sf<SF>/layout-costs/i01/measured>
experiments/predict_statistics/run-predict-statistics-uniform-params.sh --sf 3 --P 16      # the three diagonals
experiments/predict_statistics/run-predict-statistics-uniform-params.sh --sf 3 --P 16 --cells all \
    --i01-reference <I01's 49 measured at SF3 (cost-layouts' place): summary/ldbc/sf3/layout-costs/i01/measured> \
    --snapshot paper-results/captures/ldbc/sf3
```

Stages: `params`, `measure` (`kvcost measure` per set: LDBC with its held-out rows, then the uniform set; the grid's
cells, and with `--cells all` the ChK-ChK cell at 1, 4, 16 and 256 KB, all the 49 layouts read), `accuracy`
(`run/predict-accuracy.sh --captures` on the three diagonals, I02's 19 queries: bar 1, bar 2, the ratios),
`layouts` (with `--cells all`: `kvcost predict` and `python -m kvcost.predict_accuracy.layouts` on the 49 per set; the LDBC
side's composed I01 must equal `--i01-reference`, the uniform side has none), `compare`
(`python -m kvcost.predict_accuracy.compare_params`: one table, both sets, each currency named; `--snapshot` adds the
LDBC counts' drift against another capture, such as the paper's, `paper-results/captures/ldbc/sf3`). Output: the run's own output tree `output/studies/uniform-params/` (its captures),
and `output/studies/uniform-params/summary/ldbc/sf<SF>/uniform-params/` (`compare.md` first).

**Re-scoring on both sets without measuring again** (stage `rescore`, not in the default list): do
the cardinality rules hold on params they were not found on? It predicts the 49 on each set from `--stats-file`, with the
rules on and, given `--rules-off`, with those switches off (`kvevaluator predict --rule-names` lists them), and scores
each with `python -m kvcost.predict_accuracy.layouts --measured-table`, the measured side an earlier run kept
(`<root>/layouts-<set>/layout_choice/`'s by_query.csv and by_param.csv); then `rescore_summary.py` adds the layouts
past 30x. No store is opened. Output: `<out>/rescore/` (`summary.md` first). The run (tier 1, the six
rules off and on, the measured side of the SF3 run; neither is in `paper-results/`):

```bash
experiments/predict_statistics/run-predict-statistics-uniform-params.sh --sf 3 --cells all --stages rescore \
    --stats <a run's statistics: stats-tier1.json> \
    --rules-off index_value_rows,filter_value_share,far_label_consumer,entry_value_degree,props_read_consumer,value_share_correlated \
    --measured-table-root <an earlier uniform-params run's root>
```

It reads the 49's specs and tallies at `<output>/stats/ldbc/sf3/uniform-49/` (kvcost build-statistics makes them).

## The predict results

`run-predict-statistics-results.sh` makes the LDBC parts of these results (`paper-results/` keeps I02's tables under
`studies/scale-transfer/runs/ldbc/<scale>/tier1-flat/` and I01's and B01's under `summary/ldbc/sf3/predict-statistics/`)
from the statistics and the 49's tallies (run `kvcost build-statistics --dataset ldbc=<SF>` first):

- **tier1**: tier 2 (the full statistics) against tier 1 (counts and means, the 10 most common values of each indexed
  property and the 10 values whose vertices own the most edges; `kvcost predict --tier 1` sets the distributions flat) on the 49 layouts. `kvcost predict ... Sp-Sp Sp-Co ... Ch256k-Ch256k` and
  `kvcost predict-accuracy ... Sp-Sp Sp-Co ... Ch256k-Ch256k` run once per tier (`--tier 2`, `--tier 1`) into one output root; `layout_reports_w17.py` reports I02's 19 queries and the 12 that tier 1 alone prices;
  `tier1_spread_rows.py` gives the spread table (`spread_rows.md`).
- **burstiness**: IC2's ranked walk on the Ch64k-Ch64k store, with burstiness as built and with b = 1
  (`run/predict-accuracy.sh`, a traced run that opens the store read-only); `burstiness_walk.py`.
- **writes**: I01 (reads and updates) on the nine grid layouts: `kvcost predict --workload i01`, `cost-layouts` and
  `search-mix` on the measured and the predicted side (wops, wops_bytes4k), the predicted mix priced on measured;
  `w16_over_measured.py`.

The anchor level (tier 2 plus the bound person's own statistics) is not re-made by this driver.
 The LDBC-vs-uniform-params part comes from
`run-predict-statistics-uniform-params.sh` (above).

```bash
experiments/predict_statistics/run-predict-statistics-results.sh --sf 3 \
    --grid paper-results/captures/ldbc/sf3 \
    --i01-reference output/summary/ldbc/sf3/layout-costs/i01/measured
```

Output: `output/summary/ldbc/sf<SF>/predict-statistics/results/` (`--work-dir` to move it): one output root per run (`tier1/`,
`burstiness-b-as-built/`, `burstiness-b-1/`, `writes/`), the tables beside them (`reports/`, `i01/`,
`spread_rows.md`, `burstiness_walk.md`, `w16_over_measured.md`), `runs.csv` (stage, wall seconds) and `DONE`.

## How accurate predict mode is (predict-formulas)

How accurate predict mode is on a workload, and where its error comes from. Every KV step of LDBC
Interactive (I01), its reads and its writes, on the seven measured layouts, params 1-5, is costed at a chain of levels
that hands one cause at a time from the step's truth card to the statistics (tier 1, the default): the chunks a read
touches, the sizes, the counts, then the tallies counted on the paper's 1% owner sample (at least 100 owners per type),
then each layout's own predicted plan, which on the read items is predict-49's prediction
([cpp/kvevaluator/src/predict/terms.h](../../cpp/kvevaluator/src/predict/terms.h) `kLevels`). Per layout and level, the
workload total weights each item by its frequency; its error is |level − measured| / measured, on KV operations
(S + G + N + P + D), bytes read, bytes written, and the op-byte cost (wops_bytes4k, a write's last-chunk touch under the
half chunk view), for the whole workload and for its read and write items alone; the signed steps add up exactly to
the last level's error. At SF3 (median / max over the seven, op-byte cost), the whole workload: the formulas given every
true term 9.2% / 42.4%, the statistics' counts 41.7% / 136.2%, the paper's prediction 26.3% / 75.1%; its read items
alone 9.5% / 45.6%, 42.1% / 146.1% and 29.1% / 79.2%; its write items (22% of the frequency) 0.0% / 1.1%, 40.3% /
67.6% and 46.0% / 71.0%. The exact gate (`run/predict-gate.sh`: each cost rule is exact on its truth card, and a planted
wrong rule fails it) is the check line: 0 of 154,050 lanes differ. `just results predict-formulas`, then
`just figure predict-formulas`: about 13 min at SF3 with the seven stores in place, 7 min once its statistics are made.
`just reproduce-all` leaves it out.

## Files

| file | what |
|---|---|
| `run-predict-statistics-results.sh` | the predict results' driver: stages `tier1`, `burstiness`, `writes` (the section above) |
| `layout_reports_w17.py` | stage `tier1`: the layout_choice reports on I02's 19 queries and on the 12 that tier 1 prices |
| `tier1_spread_rows.py` | stage `tier1`: the spread table, tier 2 against tier 1 (`spread_rows.md`) |
| `burstiness_walk.py` | stage `burstiness`: IC2's walk, burstiness as built against b = 1 (`burstiness_walk.md`) |
| `w16_over_measured.py` | stage `writes`: I01's predicted pick and mix over the measured best (`w16_over_measured.md`) |
| `python/kvcost/predict_accuracy/uniform_49_tallies.py` (kvcost predict uses it) | the 49 layouts: names (as in `uniform-49`'s tables), specs (P's diagonal spec with A's adjacency block), the tally runner |
| `layout_choice_accuracy.py` | I02's layout choice on the 49 in numbers (the queries named, I02's rank and cost over the best, the layouts within 5%): `--from <predict-accuracy uniform-49's dir for i02> --out <dir>`; a paper claim's derive once paper-results holds a run |
| `plot_predicted_vs_measured.py` | the figures of predicted against measured cost of the 49 layouts, from a layout_choice dir (the tier1 stage's `reports/full-q19/layout_choice/` under the predict results' output root): `--totals totals.csv` I02's total in wops_bytes4k, each side over its own best, one line above the panel naming predict's pick against the measured best (from the data), the line y = x and the layouts' legend (the predict deck's slide); `--totals-grid --series "<tier>=totals.csv" ...` the four tiers' panels in a 2x2 grid, one scale (or 2 or 4 series of any names, e.g. two workloads, each panel titled by its name, on its own scale); `--by-query-ratio by_query.csv --group ic|is` the same view per query (each layout over the query's best, both sides, one log range on every panel), titled with rho, the query's share of I02's runs and its share of I02's cost (median over the 49 layouts), both defined in the caption; `--by-query by_query.csv --group ic|is` one row per query, one column per model (wops, bytes, wops_bytes4k), absolute per run, with predicted = measured, Spearman's rho and the median predicted / measured per panel (`--same-body`: predict on the measured side's plan); `--strip --group ic|is --series "<tier>=by_query.csv" ...` every query of the group in one figure, three panels (wops, bytes, wops_bytes4k; `--model` draws one), in each query's column one strip per tier (tier 1, tier 1 + anchor, tier 2, tier 2 + anchor) of the 49 layouts' predicted / measured per run, log, points coloured by layout as in `--by-query`, Spearman's rho above each strip; `--sf 3|30` the scale named in every title; `--out <file>.png` |
| `python/kvcost/predict_accuracy/workload_pick.py` (kvcost predict-accuracy Sp-Sp Sp-Co ... Ch256k-Ch256k uses it) | a workload with a write family (I01, LinkBench's l01): the predicted pick from predict-accuracy's `uniform.csv` against the measured 49 (their cost-layouts place, `layout-costs/<workload>/measured`), in that table's currencies (wops; wops_bytes4k): its measured rank, x over the measured best, Spearman of the layouts (`pick.csv`, `layouts.csv`) |
| `check_assembled_tallies.py` | the assembly check: the library's seven diagonal tallies, direct tallies (off-diagonals, a mix), the row-for-row comparison with the assembled ones |
| (`write_requests.py`, retired) | the requests are `kvcost predict`'s (python/kvcost/predict_accuracy/requests.py) |
| `run-predict-statistics-uniform-params.sh` | predict accuracy on LDBC's params and on uniform-random ones, side by side |
| `rescore_summary.py` | its stage `rescore`: one row per (param set, rules on or off, model) from each run's layout_choice dir, and the layouts past 30x |
| `kvcost predict --dataset D=S --workload W Sp-Sp Sp-Co ... Ch256k-Ch256k --tier 1\|2` | the optional predict-49-full-tallies item's runs (I01, B01, J01 at tier 1; the paper's predict-49 adds `--tally-dir` and `--save-as`, above) and the latency figure's tier-2 sets (I02, B01, J01): the statistics, the tally library and the 49 tallies when missing (`stats/<ds>/<sc>/{tallies,uniform-49}/`; `kvcost build-statistics` makes them alone), then each family of W predicted on the 49 into `predicted/<ds>/<sc>/<predicted-tier1\|predicted>/` (requests and logs in its `work/`) |
| `kvcost predict-accuracy --dataset D=S --workload W Sp-Sp Sp-Co ... Ch256k-Ch256k --count-source predicted-tier1` | the predicted 49's costs (`uniform.csv`, `uniform_totals.csv`) and the predicted pick against the measured 49, into `summary/<ds>/<sc>/predict-statistics/<W>/<set>/`: I01 (it has writes) by its totals (`pick.csv`, `layouts.csv`, against `layout-costs/i01/measured`), B01 and J01 by the layout report (`layout_choice/`); `paper/derive/predict_49.py` reads them |
| `run-predict-statistics-sampled.sh` | the sampled tallies' driver (the study: several percents, seeds and floors against the full tallies) |
| `sampled.py` | its steps: the sampled tallies, the `--owner-sample 100` control, the row errors (`tally_errors*.csv`), the layouts named against the full tallies' (`picks_vs_full.csv`, per set: `--set predicted-tier1` for tier 1); for the paper's workloads the no-locality and full-bound variants, the provenance check and the pick table (`picks_by_workload.{csv,md}`) |
| `check_sampled_picks_missing_query.py` | a fast test (`tests/fast_tests.txt`): `sampled.py picks` names a query a sample cannot cost and leaves it out of both sides |
| `python/kvevaluator/predict/tally_library.py` | the tally library and `tally_for_spec` (shared: any caller with a spec) |
| `python/kvevaluator/predict/assemble_tally.py` | the merge of two tallies (the sampled tallies, which the library does not hold) |

## Expected run time

The paper's statistics and tallies. From the paper's run records (`uv run --locked python -m kvevaluator.measure.run_costs --paper-results`, after `just fetch-results`; each record's `wall_s`, per store, with stores side by side); 28-core Intel Xeon Gold 5512U:

| dataset | statistics | the seven settings' tallies (the tally library) |
|---|---|---|
| LDBC SF3 | 275 s | 243 s median, 300 s max; 29 min in all |
| LDBC SF30 (binary) | not recorded | 2,120 s median, 2,828 s max; 4.5 h in all (the heavy step) |
| IMDb full | 376 s | 404 s median, 457 s max; 47 min in all |
| LinkBench n1m (binary) | not recorded | 31 s median, 36 s max; 4 min in all |

The predict-49 chain at sf0.1 (`kvcost predict ... Sp-Sp Sp-Co ... Ch256k-Ch256k --tier 1` and `kvcost predict-accuracy ... Sp-Sp Sp-Co ... Ch256k-Ch256k` for
I01 and B01; binary d6c582a2, a 64-thread AMD EPYC 9354P): 123 s
(the statistics and tallies 80 s, I01's predict 11 s, its cost and pick 1 s, B01's predict 27 s, its report 3 s); it
needs the sf0.1 grids first (`kvcost measure --dataset ldbc=0.1 --workload i01 Sp-Sp Co-Co Ch1k-Ch1k Ch4k-Ch4k Ch16k-Ch16k Ch64k-Ch64k Ch256k-Ch256k` and `kvcost compose --dataset ldbc=0.1`; BI: `kvcost measure
--dataset ldbc=0.1 --workload b01 Sp-Sp Co-Co Ch1k-Ch1k Ch4k-Ch4k Ch16k-Ch16k Ch64k-Ch64k Ch256k-Ch256k`, 595 s on the same
machine under load).

Binary 9f212d9, 28-core (56-thread) Intel Xeon Gold 5512U:

| stage | sf0.1 | SF3 |
|---|---|---|
| stats: stats.json and anchor_stats.json (build_stats, one graph-stats pass) | ~6 s | ~150 s (graph-stats alone) |
| tallies: the 7 diagonals and the direct check tallies | 45 s for 7 + 42 at --jobs 16 | 488 s for 7 + 7 at --jobs 7 (each ~4 min, ~5 GB of memory) |
| predict (`kvcost predict`: the requests, 49 layouts x 235, and 49 binary calls) | 8 s at --jobs 16 | 38 s at --jobs 7 |
| cost and layouts (no store) | under 1 s | ~6 s |
| the whole driver | 39 s | 1054 s (estimate before the run: 25-30 min) |

The comparison (`kvcost predict-accuracy --dataset ldbc=3 ...`) takes ~5 s and opens no store.

`run-predict-statistics-uniform-params.sh --cells all` (the same machine; sf0.1: binary aa4188f's C++, P = 8,
a fresh output dir, the stores ingested on first use; SF3: binary cae102e, P = 16, the stores built, run as the slice
`--cells diagonals --fresh-stats` and then `--cells all`, summed; peak memory 36 GB of the machine's 125):

| stage | sf0.1 | SF3 |
|---|---|---|
| measure LDBC: 9 grid cells + 4 ChK-ChK cells, reported and held-out rows, writes, checks | 355 s | 3,476 s |
| measure uniform: the same cells, reported rows | 139 s | 1,590 s |
| accuracy, both sets (the statistics and the 3 diagonal tallies rebuilt with --fresh-stats at SF3) | 27 s | 888 s |
| the tally library, predict on the 49 and the layouts report, both sets | 110 s | 688 s |
| compare | under 1 s | under 1 s |
| rescore (SF3: 4 predict runs of the 49 and 4 scorings; binary 56693ed) | | 38 s (each predict 9 s, each scoring 1 s) |

One diagonal at SF30 (binary 67e62ae, the same machine; Ch64k-Ch64k, the heaviest diagonal: a Chunked
layout keeps both directions' lists until it writes them; one thread each):

| step | SF3 | SF30 | SF30 / SF3 |
|---|---|---|---|
| the record stream (`records.records_file`) | 306 s, 6.17 GB | 3,235 s, 62.80 GB | 10.6x time |
| one diagonal tally, alone | 218 s, 5.14 GB peak RSS | 2,141 s, 54.70 GB peak RSS | 9.8x time, 10.6x memory |
| the data: records lines (edges) | 62.0 M (52.7 M) | 629.7 M (540.9 M) | 10.2x (10.3x) |

Time and memory both grow with the data: about 3.4 us per records line and 100 B per edge. A tally holds every edge
until it writes the lists (the layout's lists in both directions, and an (owner, target) pair per stored direction
for the locality section), so its memory is the edge count, not the largest owner.

`run-predict-statistics-sampled.sh --sf 3 --percents 1,10 --seeds 1,2,3` (binary 299efb0, the same machine): the
`--owner-sample 100` control on three diagonals 369 s (three at once, ~10 GB each); the seven sampled diagonals 98-134 s
per (percent, seed), the parse of the record stream dominating; predict 23-25 s per (percent, seed); the comparison 5 s;
1,195 s in all (estimate before: ~27 min). At sf0.1: 50 s. With five seeds per percent (binary c9bebfb): 1,731 s.

`run-predict-statistics-sampled.sh --sf 3 --workloads i01,b01,j01` (binary a47e619a, the same machine): the full variant
673 s (predict I01 23 s, B01 87 s, J01 477 s; reports 85 s); then seed 1 of 1% + floor 100 with no-locality and
full-bound, about 2,640 s of stages against an estimate of ~45 min (the tallies 423 s, then 2,218 s from the controls on): the seven sampled tallies 160 s (LDBC SF3) and 263 s (IMDb; one
IMDb diagonal alone 213 s, 2.0 GB), the `--owner-sample 100` controls 459 s and 578 s (one diagonal each: at 100% the
range ends then still kept every line, about 4x a full tally), predict 23 + 52 + 413 s per variant (I01, B01, J01), reports
about 2 min per variant.

`just results predict-49` at the paper's settings (a 32-core AMD EPYC 9354P, the measured side and the
statistics in place): 507 s the first time: the sampled tallies 153 s (LDBC SF3) and 283 s (IMDb full; one diagonal
282 s), predict 4 s (I01), 5 s (B01) and 21 s (J01), the three reports 18 s, the measured side's checks 19 s. With the
tallies kept and the 49 predicted again: 67 s; with both kept: 47 s. `just figure predict-49`: 1 s. `just results
runtime` after it keeps the tallies and the predictions. At `--dataset ldbc=0.1,imdb=sample1pct` from an empty output
dir: predict-49 533 s (the seven stores and their counts 416 s, the sampled tallies 16 s and 33 s, predict and its
reports 68 s), then runtime 95 s, then the optional `predict-49-full-tallies` 89 s (it builds the tally library).

`run-predict-statistics-results.sh` (the predict results; the same machine), per stage: tier1 about 105 s (two
predicts of about 55 s, then the reports), burstiness about 500 s (one Ch64k-Ch64k tally per set, then 5 traced IC2
runs), writes about 55 s.

At SF30 the results hold the tier1 stage only: 7,158 s (3,558 s and 3,582 s per tier; peak 0.57 GB),
after the SF30 statistics (4,373 s, peak 59 GB) and the seven diagonal tallies (1,902-2,830 s each, 16,074 s in all, up
to 55 GB each; a library of stamped tallies is reused in 2 s); with the anchor level, 2,505 s, nearly all of it tier
2's anchor level (tier 1 predicts the 49 in 18 s). SF30's command: `--sf 30 --stages tier1 --grid
paper-results/captures/ldbc/sf30 --i01-reference paper-results/summary/ldbc/sf30/layout-costs/i01/measured` (the seven
measured layouts, ChK-ChK at each chunk size among them, are in `captures/ldbc/sf30/`; the layouts step composes the
rest from them, and refuses a grid without them).
