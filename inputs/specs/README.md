# inputs/specs: the layout spec sets

One directory per spec set, one JSON spec per layout: what each vertex label and edge type stores, and how (the
configurable format, `meta`).

```bash
kvcost measure --dataset ldbc=<SF> --workload i01 inputs/specs/<set>    # measure a whole set
```

- **names:** a capture is named by the spec's file stem and its set's directory, so a set keeps its name; a store is
  named by what it holds, `meta-<12 hex digits>` of the expanded spec, so two files holding one layout share a store
- **a layout name works too:** `kvcost measure ... Ch4k-Ch16k` makes the spec from the name (`kvevaluator.layouts`);
  the files here are the measured ones, equal to it
- **composed from one store:** a set with one diagonal cell (`grid_chunked_<K>`) is enough for every layout with a
  Chunked side at that size; the rest are composed from it and the grid's cells, exactly, since a part's rows depend
  on its own setting only
- **at SF0.1**, a set with no row in the pick record (decided at SF3) checks with `--in-sample`

## The uniform layouts

| set | what it holds |
|---|---|
| `grid/` | the 9 uniform cells: properties x adjacency, each Split, Condensed or Chunked (64 KB); read by every pricer |
| `grid_chunked_1k/`, `_16k/`, `_128k/`, `_256k/` | one diagonal cell each, Chunked / Chunked at that size (`pack` 1024 ... 262144); the chunk-size studies (mix_benefit, layout_choice) |
| `grid_chunked_4k/` | the five layouts with a Chunked side at 4 KB; with the grid's four unchunked cells, the nine at 4 KB |
| `grid_sized/` | uniform layouts at other sizes, each named by its canonical id: Ch4k-Ch4k, Ch4k-Ch16k, Ch1k-Ch4k, Ch1k-Ch16k, Ch4k-Ch64k, Ch4k-Ch128k, Ch256k-Ch256k (the latency figure's sized layouts are here) |
| `grid_added/` | eight more Chunked cells (Ch4k-Ch4k, Co-Ch4k, Sp-Ch4k, Ch4k-Sp, Ch4k-Co, Ch4k-Ch64k, Ch64k-Ch4k, Ch512k-Ch512k), as their own set |
| `bi_winner_20261020/` | Ch4k-Ch16k, BI's best single layout, measured and timed as one store (with a copy of Ch4k-Ch4k beside it) |

## One option changed on a grid cell

| set | what it holds |
|---|---|
| `grid_far_label_off/`, `grid_chunked_own/`, `grid_chunked_4k_edge_props/` | where edge properties go (in the adjacency chunks or their own) and whether the far end's label is stored, at 64 KB and at 4 KB |
| `grid_index/` | another secondary index on a grid cell: a posting per exact value, or per day (as the Per-type CSR and Dgraph corners use them); one index for every label |
| `grid_grouped/` | grouped vertex properties (`vertex_props.groups`): C++ fixtures, the caps parity check and the predict fixtures |
| `corners/` | the engine-shaped corners (Dgraph, NebulaGraph, ArangoDB, Per-type CSR) and their expected differences (`check_meta_vs_formats.py`) |
| `examples/` | a hand-written example |

## Mixed layouts (built and checked)

Each was picked by a search and built as one store to check that measured = composed. B is a byte limit: in Sp-Co's
bytes, except `mixes_w14/`, `mixes_w16/` and the `w16_b1*`, `w16_b15*`, `w16_b2_per_direction*` specs (Co-Sp's).

| set | what it holds |
|---|---|
| `mixes/`, `mixes_w14/`, `mixes_w16/`, `mixes_per_direction/` (+ `make_specs.py`) | the built mixes the layout-choice and mix-benefit studies read (`pick_specs.py`, `check_mix_vs_built.py`) |
| `mixes_w16_picks_20261012/` | I01's fewest-ops mixes within B = 1 and B = 2, per type and direction; read by `composed_vs_measured.py` |
| `mixes_w16_20261030/` | two I01 mixes: within B = 2 per type and direction, and within B = 1 with any chunk size per part; composed against measured on every row and lane |
| `mixes_w16_curve_20261013/` | I01's mix at B = 1.5 (with its solve record); measured = composed on 17 of 17 workloads, 19 of 19 picks equal |
| `mixes_chunk_sizes_20261014/` | I01's mix within B = 1 with each part Chunked at 4, 16 or 64 KB; at SF3 1,918 rows, 0 differ from composed (`check_chunk_size_mix.py`) |
| `mixes_one_for_ldbc_20261013/` | one layout for every LDBC workload (W01-W15, I01, I02, BI), the worst-case objective, at 64 KB and at 4 KB (`one_mix_per_dataset.py`) |
| `mixes_scale_check_20261013/` | the mixes built at SF30 to check measured = composed there |
| `mixes_joint_20261013/` | one layout for W01-W17 with the fewest summed cost (beside the worst-case one above) |
| `retired/` | a mix with grouped vertex properties that archived SF3 captures still name; kept so their spec can be read |

The owning studies' READMEs give each set's runs: [experiments/meta](../../experiments/meta/README.md),
[experiments/layout_choice](../../experiments/layout_choice/README.md),
[experiments/mix_benefit](../../experiments/mix_benefit/README.md).
