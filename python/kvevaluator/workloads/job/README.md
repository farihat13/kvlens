# The JOB workload: IMDb with the Join Order Benchmark, as a property graph

A sibling of `ldbc/`, `bi/` and `linkbench/`: its own data, schema, loader, queries and plans; nothing here imports
another workload.

**What it is.** The 113 queries of the Join Order Benchmark (Leis et al., PVLDB 2015; 33 templates) over the IMDb
snapshot that paper used, mapped to a property graph and written as fixed plans in the plan IR. It adds what LDBC,
LinkBench and BI do not have: many vertex types with small rows, filters no index serves (`LIKE`), star joins around
one vertex, and edges that carry several properties.

**The data is "IMDb with repeated credits merged", not plain JOB.** Our stores identify an edge by (source, edge
type, target), and parallel edges are ruled out. So the first row (smallest id) is kept per
(person, movie, role) in `cast_info` (714,797 of 36,244,344 rows dropped, 2.0%), per (movie, company, company type)
in `movie_companies` (60,020 of 2,609,129, 2.3%), and per (movie, keyword) in `movie_keyword` (3,484 exact
duplicates). The reference answers are computed on the same merged tables, so every answer is still checked. Against
the unmerged dump, 3 of the 113 answers differ (9d, 25b, 31b) and none becomes empty.

## Run

**The entry point is `experiments/job/run-job-counts-chain.sh <tag>`** (from the repo root): after the three dataset steps below it
captures every body on the stores at that tag and runs the grid check, the reference and the picks tables, about 25
min at `sample1pct` and 7-8 h at `full`. The Python oracle on the three named formats is `just cpp-diff 1 job`. The single commands it runs:

```
P="uv run --locked python"                    # the repo's venv: it has DuckDB, which the dataset scripts need
just fetch-dataset imdb=sample1pct     # all a tag needs, each step only if missing: ~3 min fresh (`just gate-prep` runs the same)
#   for sample1pct: fetch, build_tables full, build_tables sample1pct, reference sample1pct (11 s)
# the steps one by one, the full tag's reference included (the download: `just fetch-dataset imdb=full`, its first step):
$P python/kvevaluator/datasets/imdb/build_tables.py --tag full    # the merged tables, the vertex and edge files
$P python/kvevaluator/workloads/job/reference.py --tag full        # the 113 answers, the entry counts
$P python/kvevaluator/datasets/imdb/build_tables.py --tag sample1pct
$P python/kvevaluator/workloads/job/reference.py --tag sample1pct
# correctness: every body of every query on every store against the reference (stores are ingested on first use)
$PYTHON tests/gates/check_job_reference.py --tag sample1pct
# the Python oracle against C++ (rows and S / G / N per step), each query's first body, per named format
$PYTHON tests/gates/check_job_cpp_vs_python.py --format nebula          # also dgraph, arangodb
# counts, the nine cells: every body of every query, then the grid check and the picks table (in-sample)
$PYTHON -m kvevaluator.measure.grid_bodies --sf sample1pct --family job --nparam 1 --cells <cell>   # one call per cell
(cd experiments/meta && $PYTHON check_grid.py --sf sample1pct --family job --nparam 1 --in-sample)
$PYTHON experiments/meta/bi_cells_table.py --family job --sf sample1pct
# counts, the named formats: every body of every query, into the formats capture (captures/formats/job/... below the tag's root)
$PYTHON -m kvevaluator.measure.grid_bodies --sf sample1pct --family job --nparam 1 --format dgraph,nebula,arangodb
# the guard: the plan files are what the generator gives
$PYTHON -m kvevaluator.workloads.job.dump_plans --check
```

The drivers (`-h` on each; a long one runs in tmux with a log):

| driver | what |
|---|---|
| `experiments/job/run-job-counts-chain.sh <tag>` | a tag's counts, unattended: the first store alone (it dumps the records), the other ingests four at a time, the counts as one `kvcost measure` per store, `--parallel N` side by side (`run/in-parallel.sh`; default 6), the grid check, the reference from the capture, both picks tables. `--cells a,b` names the cells (fewer than nine is a slice: no tables), `--cells-only` drops the named formats, `--format F` measures one named format alone (after a change that touches only it: its store, every body, the reference, both tables); a dirty binary, uncommitted count code, or count code moved since the binary's commit is warned and marked by measure (`binary_unclean`, `binary_behind_head`: kvevaluator.stale) in each store's run record |

Before a merge, the branch also takes the experiment's full sf0.1 round, which this workload's files do not own:
`just gate full` (it builds the control binaries and runs `run/meta-gates.sh --sf 0.1 --controls <dir>`). The round runs a slice of this workload itself: `just cpp-diff`'s
lane `job` is the Python-oracle check on 16 queries (`check_job_cpp_vs_python.py --gate`), first body, on the three named
formats; `just gate-prep` makes the sample tag and its three stores when they are missing (a fresh machine: the pinned
download and ~6.5 min, 388 s on a 32-core machine). The sizing and control scripts behind the decisions below
are in `experiments/job/` (its README lists them).

Regenerating what is tracked (only after a change to the mapping, the queries or the generator):
`reference.py --tag full --witnesses` (the sample's forced titles), `python3 -m kvevaluator.workloads.job.sqlspec`
(the specs, from the fetched SQL), `schema_text.py` then `python3 -m kvevaluator.datasets write-schema imdb`,
`python3 -m kvevaluator.workloads.job.dump_plans`.

**Data tags** (the tag plays the role LDBC's scale factor plays). `full`: 30,812,768 vertices, 64,779,587 edges.
`sample1pct`, the gate scale: 1% of the titles by an arithmetic hash of the id, plus 973 forced titles (up to 20
result rows per query, so no query that has an answer loses it), their parent series, every row that points at a
kept title, the entities those rows point at, and those people's `person_info` and `aka_name`: 2,182,582 vertices,
2,404,627 edges. Data: `<data dir>/datasets/imdb/rows/<tag>/` (the download in `<data dir>/datasets/imdb/raw/`). Stores: `output/rocksdb-cpp/imdb-rows/<tag>/<store>` (`rows` is the
mapping's name: a second mapping gets its own root).

**The numbers of record** are the dated snapshot `imdb-rows/full/job-20261012` (`just fetch-results` puts it in
`<data dir>/results/`, 8 MB): the whole
full-tag capture of the 12 stores as one archive, both picks tables, the reference files, and which code measured
what. Its README has the commands that re-derive every table from it. A capture under `output/imdb-rows/<tag>/captures/grid/job/reported`
is a working copy.

## Files

| file | what |
|---|---|
| `mapping.py` | the one table of the mapping: labels, properties, edge types, merge keys, indexes |
| `job.schema`, `schema.json` | generated from `mapping.py` (`schema_text.py`, `python -m kvevaluator.datasets write-schema imdb`); `schema.py` compiles it |
| `loader.py` | the record stream from a tag's vertex and edge files |
| `dataset/` | `fetch.py` (the dataset's fetch step: the downloads, pinned by sha256 and commit in `inputs/data-sources/imdb/pins.json`, then a tag's tables), `build_tables.py` (merge, sample, export), `sample_forced_titles.csv` |
| `sqlspec.py`, `specs/` | the SQL grammar JOB uses, and the 113 parsed queries (tracked: the SQL texts are fetched, not redistributed) |
| `type_tables.json` | the six type tables' rows (4 to 113 rows each) |
| `pattern.py` | a spec as a graph pattern: variables and elements |
| `generator.py` | a pattern and an entry point give a plan |
| `ensure.py`, `reference.py` | JOB's data step on a tag (its manifest's `data_step`; `just fetch-dataset imdb=<tag>` runs it after the tables): the reference (DuckDB: the 113 answers, the alias and entry counts) |
| `entry_counts_full.csv` | per query and alias, the rows passing its tests in the full data (from the reference) |
| `queries/__init__.py` | the registry: which bodies a query has, their names, the bundle a plan file holds |
| `plans/` | the dumped plan IR, one bundle per query (1,255 bodies; 953 valid on a grid cell, see "The plans") |

## The mapping (`rows`)

One rule, stated before any count was seen: **a table row is a vertex, a foreign key from it to an entity is an edge,
and every other column is a property.** Three exceptions, each forced:

- **A table with two foreign keys to entities and no repeated pair is an edge.** `movie_keyword` (no property), and,
  after the merge above, `cast_info` (Name → Title; properties `note`, `nr_order`, `character`) and
  `movie_companies` (Title → Company; property `note`).
- **Repeated credits: the role is part of the edge's identity.** A person can have two roles in one movie, so the role
  is the EDGE TYPE (`cast_actor`, `cast_director`, ...; `company_production`, `company_distributor`), not a property.
  The declared edge types follow the FULL tag's data: 11 roles and 2 company types have rows there
  (`dataset/edge_type_rows_full.json`, written by the full build; `guest` and two company types have none and are
  not declared: a declared type with no edge would still be probed by every arm that names no type).
  A predicate on a role (`rt.role = 'actress'`, 19 queries) or on a company type (24 queries) therefore selects an edge
  type and reads nothing, on every store.
- **A credit points at three things.** The character (`cast_info.person_role_id`) stays a property of the edge that
  holds the Character's id. A plan reaches the Character by a point read; no plan can enter at a Character and walk
  to its credits (`generator.CannotBuild`, a declared limit).

**Every column is a property**, including the ones no query reads (`md5sum`, the phonetic codes): leaving them out
would make rows smaller for the layouts that return whole rows. One foreign key is neither edge nor dropped:
`aka_title.episode_of_id` (2,876 set, read by no query) stays an integer.

**Type tables.** The six type tables (kind, company type, role, info type, link type, complete-cast type; 4 to 113
rows) are stored as labels, and a row that refers to one holds the id as an integer property (`info_type_id`,
`kind_id`, ...). A predicate on a type table is resolved WHEN THE PLAN IS WRITTEN, from `type_tables.json`:
`it.info = 'rating'` becomes "`info_type_id` is 101" on the row that joins `it`; a `MIN` over a type's name is taken
from the same file. No plan reads a type label. This is ours, not a format's or an engine's: the same on every
store, and it leaves out a lookup of at most 113 tiny rows per query.

**Vid tags.** The workload has 16 labels and a vid has 16 tag values, so it uses all of them, the same numbers LDBC
and LinkBench use. A tag is read per schema (`schema.json`'s `vid_tags` holds this schema's labels only) and an IMDb
store holds IMDb labels only; a store opened under another workload's schema is refused by the schema hash in its
manifest (`stores.ensure_store`). The labels are JOB's table names (`Name`, not Person).

**Indexes.** A per-type membership index on every label, and an EQUALITY index on every vertex property a query
tests with `=` or `IN` against a constant, directly or through a type table (16 properties,
derived from the specs). No plan enters by a range. JOB's own configuration
has only key indexes: that reading is in the same capture: the picks table's "no index" column. The no-index plans read a label's rows in one
run on the stores whose layout keeps them together (the nine cells, ArangoDB, Dgraph per property); on NebulaGraph
they read per vertex.
Two declarations matter for the Dgraph format (the other stores index per label and exactly anyway):

- **One predicate per label (`predicates per_label`, and `type_scoped` on each index).** In Dgraph a predicate
  owns its data keys and its index keys, so the declaration covers both: a vertex property's data key is
  `<Label>.<prop>`, then the uid. A label's values of one property are then one whole predicate, one key run, and
  the label can be read with its properties in one pass per property (the engine's `has(predicate)` path): the
  label-run bodies are valid on Dgraph too.
- **Why per label.** Several labels share a property name (`info` on three, `note` on four, `name` on
  four), and Dgraph's index is per predicate: a lookup would return every label's matches, and the plan would have
  to drop the others by reading the label's whole type posting (14.8 M uids for MovieInfo) or a property per foreign
  vertex. The shared names are JOB's column names, a declaration habit, and Dgraph's own convention names a
  predicate `Type.field`; so the indexes are declared per label. The reported LDBC
  design declares bare predicates; its numbers under per-label predicates are not measured.
- **Equality on integers (`equality`).** A number's index is sorted by default, and our Dgraph format keys a sorted
  index by the value's day token (right for a date; for an integer code every value falls in day 0 and a lookup
  returns the whole label). The real engine indexes an integer exactly, and D4 asks for an equality index, so the
  eight integer indexes (`info_type_id` on three labels, `kind_id`, `production_year`, `subject_id`, `status_id`, `link_type_id`) are declared `equality`: an exact term on every store, the value still an integer.

**How wide an integer equality-index term is.** NebulaGraph, ArangoDB and the nine cells store one index entry
per vertex and write the integer as 8 bytes in it. Dgraph stores one posting list per distinct value and writes the
value in the list's key as its decimal text (1 to 4 bytes here): a packed 8-byte integer can hold a NUL byte, which
a chunked Dgraph store uses to mark a list's parts. So the term is narrower on Dgraph, on one key per distinct value
(113 info types, 7 kinds), not per vertex.

**The Dgraph store is the 512 KB one** (`dgraph-c512k`, the split the format is reported at): a JOB entry point sets
the knob, and a JOB Dgraph store without it is refused. The Python oracle compares against the unchunked store
(`dgraph`), set by name.

A second index arm (bare predicates, say) is a second schema file `job_<name>.schema`: its stores then need the
schema's name in their path (`stores.ensure_store` names every JOB store `canonical` today).

## The plans

A JOB query joins 4 to 17 tables, but once equal join columns are merged every one of the 113 is a tree, and 106
are stars around a movie. A plan is an entry point and a fixed rule for the rest (`generator.py`):

1. **Entry:** one alias with a test, read by an index (`enter_<alias>_by_index`), by one pass over the label's own key
   run with the properties (`enter_<alias>_by_label_run`: valid where a label's records are one key run, the
   capability `label_rows_one_run`: every cell and ArangoDB, and Dgraph as one run per wanted property; not run on
   NebulaGraph, whose reported key order (vertex id first) interleaves the labels' rows), or by its label's vid list
   and a read per vertex (`enter_<alias>_by_scan`, the plain scan). The plain scan is offered only where the entry
   label lacks `whole_label_scan_by_default` (ArangoDB, Dgraph, NebulaGraph; never a grid cell): where a whole-label
   read with properties runs as one scan of the label's rows, the label run is that read, and it is never dearer in
   the pick measure (`wops_bytes4k`). On the paper's IMDb full captures it is cheaper or equal on 16,308 of 16,308
   (layout, query, alias) pairs, 12 measured and 37 composed layouts, closest 0.999 (job10c, Ch256k-Sp). This is a
   cost result, not dominance in every lane: the run crosses every row of the label, so its NEXTs and bytes can be
   higher (up to 5.6x and 3.8x on Sp-Co). `tests/gates/check_job_scan_dominated.py` re-checks it on any tree.
   Its `_outputs_early` twin stays everywhere. A query is entered at the 3 aliases with the fewest
   matching rows in the full data, plus the smallest one an index serves when none of the three is. Bodies per
   store: 1,255 on ArangoDB and Dgraph, 953 on NebulaGraph (no label run) and on a grid cell (no plain scan).
2. **Outward:** at a vertex, the arms that hold a predicate first, then the smaller mean degree. After each arm the
   vertex set keeps only what that arm matched, so the next arm reads less.
3. **Back down, in memory:** each arm is cut to the vertices that survived all the others. No KV operation.
4. **Outputs:** each `MIN` over what is left. A property only an output needs is read here, for the survivors; the
   twin `..._outputs_early` reads it with the predicate's read of the same vertex (cheaper where a layout returns
   the whole row anyway).

A `MIN` needs the values that take part in a result row, not the rows, so no plan multiplies one arm with another.
A `LIKE` is the expression `like` (`%` only): 0 KV operations; its cost is the read of the property it tests.

**The pick is in-sample.** A JOB query has no parameters (its literals are in its text), so there are no held-out
rows and the plan rule's held-out split
(`python/kvevaluator/plan_choice/candidates.py`) cannot apply. Every valid body is measured on every store, the nine cells and the
named formats alike (`grid_bodies.py`, `--format` for a named format). The picks table
(`bi_cells_table.py --family job [--formats]`) prints three totals per store: the pick's, the first body's (each
query's default plan), and the cheapest body's that uses no property index; and how many picks are less than 10%
cheaper than the first body (inside the margin).

**What the reference check compares** (`tests/gates/check_job_reference.py`), per store, query and body: the answer; the
entry's row count against the reference's count of that alias's rows; and every alias's final row count against the
reference's `COUNT(DISTINCT alias.id)` over the join, so an alias seen only through a `MIN`, or through no output,
is checked too. Five queries return no row (2c, 5a, 5b, 10b, 32a), on the unmerged dump too: there every count must
be 0. At the full tag the check reads the counts capture itself (`--from-capture`): the runs that are reported are
the runs that are checked.

## Expected run time

The paper's run, IMDb full, the seven measured layouts (binary). From the paper's run records (`uv run --locked python -m kvevaluator.measure.run_costs --paper-results`, after `just fetch-results`; each record's `wall_s`, per store, with stores side by side); 28-core Intel Xeon Gold
5512U: ingest 458 s median, 520 s max (Sp-Sp), 54 min for the seven; the counts (j01, every body) 4,176 s median,
5,378 s max (Ch1k-Ch1k), 8.0 h for the seven. The heavy step is the counts.

Recorded on a 32-core EPYC 9354P; a second machine (32 cores, 125 GB) runs the same steps 1.5-2x
slower: fetch 84 s, build_tables 73 s (full) and 7 s (sample), the sample's records dump plus one ingest 95 s and an
ingest 57-70 s, the oracle check on 113 first bodies 219 s (NebulaGraph). Not investigated. The 113 queries have
1,255 bodies: 953 valid on a grid cell (no plain scan) and on NebulaGraph (no label run), 1,255 on ArangoDB and Dgraph.

| step | sample1pct | full |
|---|---|---|
| fetch (dump + queries) | - | 57 s |
| build_tables | 4 s | 55 s |
| reference (answers, entry counts, alias counts) | 4 s | 16 s |
| records dump (once per tag) | 24 s | 553-596 s (10.2 GB) |
| ingest, one cell of the configurable format | 24 s | 513-533 s (Split / Split: 8.2 GB, 280.8 M keys) |
| ingest, Dgraph (512 KB), one predicate per label | - | 536 s (9.5 GB, 181.4 M keys) |
| counts on a cell through the grid, every body | 256-472 s (five cells side by side; 951-1,454 s before the `parts` fix) | 7,543-11,667 s per cell with six stores side by side (pilot estimate: about 2.4 h per cell); 21-23 GB peak server memory each |
| counts on a named format through the grid, every valid body | 220-377 s | NebulaGraph 7,246 s, ArangoDB 11,404 s (six stores side by side; 23-25 GB peak); Dgraph (512 KB) 8,440 s alone, 22.8 GB peak (estimate: 2.5-3 h) |
| grid check over the nine cells | 356 s | 353 s |
| reference check from the capture (14,456 runs) | 1 s | 1 s |
| reference check by running, every body, per store | 215-450 s (three stores side by side) | not run there (the capture is checked instead) |
| Python oracle check, 113 first bodies, per named format | 101-147 s | not run there (a small-scale oracle) |
| the whole sample gate chain (captures, checks, oracle, tables) | 27-31 min | - |
| the round's slice (`just cpp-diff 1 job`: the oracle check, 16 queries x 3 named formats, one after another) | 218 s on the second machine, idle (66-73 s per format) | - |
| the whole full-tag chain (`experiments/job/run-job-counts-chain.sh full`: 12 ingests four at a time 1,701 s, two waves of six stores 11,667 s + 11,404 s, checks and tables 442 s) | - | 7 h 00 min (estimate: 7 h 10 min) |
| the nine cells alone on the second machine, 2026-10-24/25 (`run-job-counts-chain.sh full --cells-only --parallel 3`, binary 07cbb8f9cc): the first store with the records dump 1,869 s, eight ingests four at a time 2,009 s (858-1,011 s each), the counts three at a time 23,989 s (5,452-10,005 s per cell; Chunked properties 5,452-5,801 s, Condensed 8,263-8,386 s, Split 9,830-10,005 s; 11.9-18.7 GB peak each), grid check 638 s, tables 130 s | - | 7 h 57 min (estimate at launch: 11-19 h, from the 10-12 per-cell times at 1.5-2x for the second machine; three at a time ran each cell faster than six did) |

Full-tag pilot, one cell (Split / Split), with other runs on the machine: per entry label the heaviest
label-run body takes 1.9-25 s of plan time and its scan twin 1.8-59 s (MovieInfo 22 against 59 s, PersonInfo 5
against 14 s, Title 25 against 30 s); the heaviest index body 65-71 s (`job18c`, entered at Name by gender); the
heaviest default bodies 10 s. The server's peak memory over a session of such bodies: 21.3 GB.

A store is reused only under the schema hash it was ingested with. The line `predicates per_label` is part of that
hash, so the eleven full-tag stores ingested before it (the nine cells, NebulaGraph, ArangoDB; their captures of
stand, the line changes no byte outside Dgraph) are refused by the next run that opens them: move them
aside and let the run ingest them again, about 30 min for the eleven, four at a time.
