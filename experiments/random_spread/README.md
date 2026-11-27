# random_spread: which uniform layout wins as LDBC Interactive's mix of reads and updates moves

LDBC Interactive has three groups of queries: complex reads (IC1-IC12), short reads (IS1-IS7) and updates (IU1-IU8).
A point of a triangle is a split between them, e.g. 60% complex, 20% short, 20% updates. At each point we draw 100
random workloads with that split and ask which uniform layout is the cheapest for each. Analysis only: the costs are
composed from an SF3 grid capture's per-part rows; no store is opened.

## Run

```bash
kvcost cost-layouts --dataset ldbc=3 --workload i01 Sp-Sp Sp-Co ... Ch256k-Ch256k     # I01's per-query costs on the 49 (seconds)
uv run --locked python experiments/random_spread/random_spread.py --dataset ldbc=3 [--from paper-results]   # ~1 s
```

(`kvcost` is `uv run --locked python -m kvcost.cli.cli`.) The first line writes
`summary/ldbc/<scale>/layout-costs/i01/measured/query_costs.csv` (its half view) (27 queries x 49 layouts: the picked plan, wops, bytes,
wops_bytes4k, S G N P D and the byte lanes) beside I01's 49 totals. `random_spread.py` reads it from the tree `--from`
names (default the output dir) and writes `<output dir>/summary/ldbc/<scale>/random-spread/`. The paper's figure is
drawn from that output (`paper-results/summary/ldbc/sf3/random-spread/`) by `paper/derive/ldbc_random_workloads_triangle.py
[--from output --dataset ldbc=S]` and its draw script.

Another layout set, e.g. the nine cells (Sp, Co, Chunked at 64 KB): `random_spread.py --dataset ldbc=3 --sets
'sp-co-ch64k=split,condensed,chunked'`. On the 10-29 grid: Co-Co wins 277 tiles, Co-Sp 123 (no Chunked side wins a
tile); the paper draws the 49.

| file | what |
|---|---|
| `random_spread.py` | the random workloads, the tiles and the winners, from cost-layouts' per-query costs |

Output: `tiles.csv` (per layout set, column and tile: each layout that wins a draw, its wins out of 100),
`winners.csv` (per tile: the layout with the most wins, its wins, `clear` = it wins at least half and no tie),
`marks.csv` (I01's split), `groups.csv` (query -> group, from the query's id), `random_spread_run.json` (the constants,
the gates' counts, a summary per layout set).

## The algorithm

- **Cost per run of a query on a layout:** wops_bytes4k = wops + bytes / 4,096. wops = G + 1.1 S + 0.01 N + P + D (a
  write's own reads at the read weights); bytes = key and value bytes read, missed keys included, + bytes written.
  The plan is the one the pick record (`inputs/picks/picks.csv`) and the held-out rule choose
  (`kvcost.layout_cost.compose.query_pick`); the cost is the mean over the reported params. Counts are deterministic.
- **Layouts:** every uniform layout (one setting for all properties, one for all adjacency) over Split, Condensed and
  Chunked at 1, 4 and 16 KB (25 layouts), and the same plus Chunked at 64 and 256 KB (49 layouts). A Chunked side at
  another size than the grid's 64 KB is priced from that size's Chunked/Chunked capture.
- **A workload:** a split s = (s_complex, s_short, s_update), and inside each group random weights w_q, Dirichlet(1)
  over the group's queries (every mix of the group equally likely). Its frequency of query q in group g is
  f_q = s_g x w_q / cost_q(Sp-Co). So s_g is group g's share of the workload's **cost on Sp-Co** (Dgraph's layout, the
  reference of every ratio in the paper), and w_q is q's share of its group's cost there. The workload's cost on a
  layout L is sum_q f_q x cost_q(L); the winner is the cheapest L (a tie goes to the first in the set's order).
  A second column, `executions`, uses f_q = s_g x w_q: s_g is then the group's share of the runs.
- **Points:** the triangle is cut into 400 small triangles (5% steps); each is priced at its centre. The same 100
  draws (seed 20261026) are used at every point, in both columns and both layout sets.
- **I01** (LDBC's own frequencies) is marked by its split: on Sp-Co, complex reads are 95.7% of its cost, short reads
  3.8%, updates 0.5% (by runs: 6.5%, 71.5%, 22.0%).

What a tile's colour means: of the tile's 100 random workloads, this layout is the cheapest for the most. A tile where
no layout wins at least half (or two tie) has no clear winner.

## Gates (every run)

- The per-query costs and I01's 49 totals come from one pricing pass (`kvcost.layout_cost.uniform_49.cost_all`, as `kvcost cost-layouts`):
  each total is the sum of its queries' rows at I01's frequencies, and each measured layout is checked equal to its
  composed price there.
- The group-cost path equals pricing each workload's frequency vector directly (every draw at five points per column:
  25,000 cells for 25 layouts, 49,000 for 49); each group's share of the cost on Sp-Co (or of the runs) equals its split
  (3,000 per set); every tile's wins add up to 100 (800 (column, tile) per set).

## Expected run time

About 5 s at SF3 (the per-query costs 2 s in cost-layouts, random_spread 1 s, the rest Python start-up), on a 28-core Intel Xeon Gold 5512U.
The reproduction: the cost table at e381784 8 s, then the two stages 3 s.
At sf0.1 (the nine composed from the seven measured): 3 s on a 64-thread AMD EPYC 9354P.
