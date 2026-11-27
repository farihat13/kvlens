# scale_transfer: does a layout chosen at a small scale hold at SF30?

For each of the 49 uniform layouts (props and adjacency each Split, Condensed, or Chunked at 1, 4, 16, 64 or 256 KB),
this study takes a workload's per-query costs from a small scale (SF3 or SF0.3: measured, or predicted at tier 1 or
tier 2 by predict mode from that scale's statistics) and compares them, layout by layout, with SF30's measured
costs. The question is a good pick, not a correct full ranking: each panel leads with the small scale's pick and its
cost at SF30 over SF30's best. It is not in `paper-results/`: the paper does not report SF30, so the run reads SF30's
captures from the output dir, measured there first (`--dataset ldbc=30`, below). Analysis only: it opens no store.

**Entry point:** `experiments/scale_transfer/run-scale-transfer.sh` (from the artifact's root; `-h` lists the flags).

```bash
# the binary named in every output's provenance.txt: KVEVAL_CPP_BIN, or the build's
experiments/scale_transfer/run-scale-transfer.sh --small 3 --workload i02 \
    --small-tier1 <SF3 tier-1 cost-layouts place> --small-tier2 <SF3 tier-2 cost-layouts place> \
    --large-tier1 <SF30 tier-1 cost-layouts place> --large-tier2 <SF30 tier-2 cost-layouts place>
experiments/scale_transfer/run-scale-transfer.sh --small 0.3 --workload i02 \
    --small-tier1 <SF0.3 tier-1 cost-layouts place> --small-tier2 <SF0.3 tier-2 cost-layouts place>
```

## Stages (`--stages`, in order; each prints its wall time)

| stage | what it does | code |
|---|---|---|
| measured | the 49 at each scale (`kvcost cost-layouts --dataset ldbc=<scale> --workload <w> Sp-Sp Sp-Co ... Ch256k-Ch256k`, from the tree `--from` names), then one row per (layout, query): frequency x one run's ops and bytes, checked against those totals (49 of 49). On the output dir (no `--from`), `kvcost compose --dataset ldbc=<scale>` first makes the nine from the measured diagonals; a tree given by `--from` is read as it is and must hold each scale's nine | `kvcost cost-layouts Sp-Sp Sp-Co ... Ch256k-Ch256k`, `scale_transfer.py measured` |
| predicted | the same rows from a `kvcost cost-layouts --count-source predicted-tier1` (or `predicted-tier2`) place (its `query_costs.csv`) | `scale_transfer.py predicted` |
| compare | each side's per-query costs weighted into totals (the workload's frequencies, or `--weights`' on every side), compared layout by layout with SF30's: `panels.csv` / `.md` (per panel and model: the pick, its cost at SF30 over SF30's best, its rank there, Spearman of the 49, the near-tie count), `points.csv` (x: SF30 over SF30's best; y: the source over its own best), `per_query.csv`, `query_ranks.csv`, `totals/<panel>.csv` | `scale_transfer.py compare` |
| plot | one figure per small-scale panel (wops_bytes4k), by `experiments/predict_statistics/plot_predicted_vs_measured.py --totals --mark-pick` | the driver |

Panels: `a<n>-sf<small>-measured`; `b<n>-sf<small>-tier1`, `-tier2` (each predicted table given); `ref-sf30-tier1`,
`-tier2` (predict on SF30's own statistics). Models: wops (G + 1.1 S + 0.01 N, a write's own reads, PUTs and DELETEs
included) and wops_bytes4k (wops + bytes / 4096).

## Inputs

From the output dir (or the tree `--root` names; `paper-results/` holds SF3 but no SF30): each scale's captures,
`captures/ldbc/sf<N>/` (the seven measured; the driver composes the rest), and each predicted tier's cost-layouts place
(`--small-tier1`, `--small-tier2`, `--large-tier1`, `--large-tier2`; no default: a tier not given is left out). SF30's
seven are measured as SF0.3's below, at `--dataset ldbc=30` (`just fetch-dataset ldbc=30` first; the SF30 BI counts took
24.8 h); each scale's predicted tables come from its statistics, as below.

```bash
# SF0.3's seven stores (Ch64k-Ch64k first, alone: it dumps the record stream)
uv run kvcost measure --dataset ldbc=0.3 --workload i01 Ch64k-Ch64k   # Ch64k-Ch64k first, alone: it dumps the record stream
uv run kvcost measure --dataset ldbc=0.3 --workload i01 Sp-Sp Co-Co Ch1k-Ch1k Ch4k-Ch4k Ch16k-Ch16k Ch64k-Ch64k Ch256k-Ch256k
# SF0.3's statistics, tallies, predict on the 49 and its cost, at tier 2 and at tier 1
for t in 2 1; do
  uv run kvcost predict --dataset ldbc=0.3 --workload i02 Sp-Sp Sp-Co ... Ch256k-Ch256k --tier $t --jobs 7
  uv run kvcost predict-accuracy --dataset ldbc=0.3 --workload i02 Sp-Sp Sp-Co ... Ch256k-Ch256k --count-source predicted-tier$t
done
```

**LDBC BI (`--workload b01`).** BI gives each query an equal share of the cost: each query weighted 1 / its measured
wops_bytes4k on Sp-Co at SF3, the one equal-share file for every scale and both axes, the driver's
`--weights` (default: `inputs/workloads/ldbc/equal-share/b01.json`, made by `python -m kvcost.layout_cost.query_weighting`). Its inputs: each scale's
`captures/ldbc/sf<N>/bi/` (three diagonals each, the six others composed by the driver; SF30's measured into the output
dir first) and each tier's predicted cost-layouts place, set `predicted-tier1` (tier 1) and `predicted-tier2` (tier 2), given
by flag. SF0.3's BI counts are measured on the same seven stores and its predicted tables made from the same statistics:

```bash
uv run kvcost measure --dataset ldbc=0.3 --workload b01 Sp-Sp Co-Co Ch1k-Ch1k Ch4k-Ch4k Ch16k-Ch16k Ch64k-Ch64k Ch256k-Ch256k
# B01's predicted 49 and its cost, beside I02's (summary/ldbc/sf0.3/predict-statistics/b01/<set>/: one dir per workload)
for t in 2 1; do
  uv run kvcost predict --dataset ldbc=0.3 --workload b01 Sp-Sp Sp-Co ... Ch256k-Ch256k --tier $t --jobs 7
  uv run kvcost predict-accuracy --dataset ldbc=0.3 --workload b01 Sp-Sp Sp-Co ... Ch256k-Ch256k --count-source predicted-tier$t
done
experiments/scale_transfer/run-scale-transfer.sh --small 3 --workload b01 \
    --small-tier1 <SF3 B01 tier-1 cost-layouts place> --small-tier2 <SF3 B01 tier-2 cost-layouts place>
experiments/scale_transfer/run-scale-transfer.sh --small 0.3 --workload b01 \
    --small-tier1 <SF0.3 B01 tier-1 cost-layouts place> --small-tier2 <SF0.3 B01 tier-2 cost-layouts place>
```

## Output

`<output dir>/summary/ldbc/sf30/scale-transfer/sf<small>/<workload>/` (or `--out`): `provenance.txt` (the binary, the commit,
every input), `costs/` (the per-query rows of each side), `logs/`, the compare stage's tables, `figures/`. The 49's
totals go to their own place, `<output dir>/summary/ldbc/<scale>/layout-costs/<workload>/measured/`; the nine a scale composes, to
`composed/ldbc/<scale>/`.

## Expected run time

| phase | SF3 against SF30 | SF0.3 against SF30 |
|---|---|---|
| SF0.3's seven stores: ingest and measure | | 3 min 47 s (the record stream 28 s; Ch64k-Ch64k alone 82 s, then four lanes 62-83 s each) |
| SF0.3's statistics, tallies, predict (both tiers) | (SF3's predict results, `predicted/ldbc/sf3/`) | 98 s (statistics 24 s; tallies 46 s; predict 13 s and 12 s) |
| measured, predicted, compare, plot (one workload) | 17 s (I02), 14 s (I01) | 25 s (I02), 15 s (I01) |
| B01: SF0.3's BI captures on the seven stores | | 1,945 s, one store at a time (243-320 s each; estimated ~35 min from the Sp-Sp pilot's 320 s) |
| B01: SF0.3's predict (each tier, 7 at once) | (SF3's predict results, `predicted/ldbc/sf3/`) | 39 s (tier 2), 33 s (tier 1) |
| B01: measured, predicted, compare, plot | 14 s | 15 s |

Measured (binary 40cc7bb5f4; the B01 rows binary 2b12ee15e0).

## Files

| file | what |
|---|---|
| `run-scale-transfer.sh` | the one entry point: one small scale and one workload against SF30 |
| `scale_transfer.py` | its stages `measured`, `predicted`, `compare` |
