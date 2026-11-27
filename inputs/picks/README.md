# inputs/picks: the pick record

The pick record says which plan body each store runs for each query. It is three files, all read and written by
`python/kvevaluator/plan_choice/pick_record.py`:

| file | what it holds | who reads it |
|---|---|---|
| `picks.csv` | one row per pick: `dataset, workload, scale, store, query, body, body_sha, carried` | every reader, through `pick_record.load` |
| `pick_stores.json` | what a reader needs that is not a row: the rule (`criterion`, `margin`, `decided_at`), each mix's or chunk cell's `spec_sha`, each built mix's `assignment`, and `bound_scales` (the stores checked at each other scale) | every reader, through `pick_record.load` |
| `pick_decisions.json` | why each pick was made: its rule, `why`, ratios, the captures' fingerprints, `counting`, `by_reference`, `head`, `redecided` | the writers and their gates only, through `pick_record.load_full` |

A row at the deciding scale (SF3) says `3` in `scale`; JOB's rows say `3` too (JOB's dataset has one size). A scale's
own pick (an exception, the plan rule's per-scale check) is its own row at that scale. `carried` is `yes` for v1's
selection (below). `tests/lint/check_pick_files_read.py` keeps the reads in one module: a reader calls `load` (a
`Picks`: `entry`, `cell`, `stores`, `exceptions`, `bound_at`, `spec_sha`, `assignment`), and only the writers and
their gates call `load_full`, which joins the three files back into one record.

## Two rules read it

- **A runner** (kvbench, `plan_for`, a picks-only run, predict) takes a store's row at the scale it runs at, else the
  deciding scale's row, at any scale (`pick_record.entry`, `body_for`). A query whose plan file holds one legal body on
  a store needs no row; a query with a choice and no row is refused, and so is a row whose body is not legal there. A
  row whose body sha is no longer that body in the plan file runs today's body, marked.
- **A pricer** below the deciding scale (`python/kvcost/layout_cost/picks.py`, `choose_cell`) binds a store's rows
  only where the record checked that store at that scale (`bound_scales`; `picks.bound_store`);
  elsewhere it applies the held-out rule to its own capture. A carried row pins nothing and is no anchor.

## The rows

Per grid cell and mix, per LDBC read query: the plan the held-out rule picks at the deciding scale (the plan rule,
stated in `python/kvevaluator/plan_choice/candidates.py`; "CONVENTIONS 19" in the decisions is that rule). The named
stores (`dgraph`, `nebula`, `arangodb`, `pertype_csr-c64k`, `dgraph-c512k`, `pertype_csr-c512k`) are keyed by their
store dir's name (`pick_record.store_key`). BI, the microbench and JOB have their own rows, in-sample.

Rows with `carried` = `yes` are v1's selection: the body the plan file's selector took on that store, made
(`python3 -m kvevaluator.plan_choice.pick_record --carry-selection WORKLOAD --write`, from the plan files at
`pick_record.SELECTOR_TREE`; JOB's with `--carry-job`, from the job-20261012 capture). They are decided on no capture
of ours, so a new decision never keeps one as its anchor (`pick_record.anchor_of`); `python/kvcost/layout_cost/grid_pick_record.py`
decides them when it reads that store's capture of every body.

## Writing it

It is a tracked input, like `inputs/params/`, written only at the deciding scale:
`python -m kvcost.layout_cost.grid_pick_record --sf 3 --grid output/ldbc/sf3/captures/grid --mixes output/ldbc/sf3/captures/mixes_w16`
(and `--small-scale --record` at another scale for its exceptions). The writers build the record as one dict and
`pick_record.dump` writes the three files.

`pick_record_by_reference.json` (an input of the writer) lists the cells the last rewrite at the deciding scale did not
capture, each with the capture it was decided on. They are kept as decided (`grid_pick_record.py --repick
--keep-by-reference`), and the decisions' `by_reference` carries each one's held-out params, capture, date and binary.
The nine grid cells are decided on held-out params 11-15 and the 21 cells kept by reference on 6-10.
`tests/gates/meta/check_picks_by_reference.py --before <old record's picks.csv>` checks a rewrite keeps their rows at
every scale.

## Older shapes

A record in one file, `pick_record.json` or `grid_picks.json` (the body in a field `variant`), is refused by
`pick_record.load`. To read one (a `--before` of a commit that holds one, a snapshot's `freeze-record/`), convert it first with the one-shot script from the commit that added `picks.csv`:

    c=$(git log --diff-filter=A --format=%h -- inputs/picks/picks.csv)
    git show $c:experiments/meta/convert_pick_record.py > /tmp/convert_pick_record.py
    python3 /tmp/convert_pick_record.py --in OLD.json --out DIR      # writes DIR/picks.csv and the two JSON files

## candidate_aliases.json

It maps the candidate names that older captures and records use, and that no longer name a candidate (a twin's copy
of a base body, retired or left out; a bundle body renamed to one name per body), to the candidate with
that body today. Readers of captured rows go through `python/kvevaluator/plan_choice/candidates.py` `canonical_rows`,
which matches a row's own `body_sha` first and uses this table only for a row without one.
