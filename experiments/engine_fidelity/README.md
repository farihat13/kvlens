# engine_fidelity: does the evaluator reproduce a real engine's KV cost on that engine's own plan?

The evaluator charges each format the cheapest plan its layout allows. To show that its counting is faithful, we also
write each engine's **own** plan for every query (the engine-faithful plans, `python/kvevaluator/workloads/*/plans/`),
run it on the evaluator, and compare the SEEK / GET / NEXT counts and the answer with the live engine's `/kvstats` for
the same query and param. The LDBC holdout params (`inputs/params/ldbc/ldbc_holdout.json`) were not used to write the
plans. The write side compares the formats' write counts with the live engines' write sweep.

The results are in the paper's Evaluation (engine fidelity).

## Run

The smallest run is the write side: the archived live sweep (`just fetch-results` first, as the main README says)
against the canonical formats' write counts. paper-results/ holds no such counts (the paper's were measured at SF30,
which the paper does not report), and a write's op counts do not depend on the scale, so `just results
engine-fidelity` measures them at sf0.1 (24 s) and compares (under 1 s), after the read side. The two steps alone:

```bash
kvcost measure --dataset ldbc=0.1 --family writes dgraph-c512k nebula arangodb
uv run --locked python experiments/engine_fidelity/write_fidelity.py \
    --sweep paper-results/engines/ldbc/sf0.3/write-sweep/write_sweep.csv \
    --capture output/captures/ldbc/sf0.1/writes/reported/counts
```

A pass ends with `23 compared cells: equal 8, equal after the named differences 15, unexplained residual 0` (the same
at sf0.1 and SF30) and exits 0; an unexplained residual exits 1. The compare
(`output/engines/ldbc/sf0.3/write-fidelity/write_fidelity.csv`) gives the paper's `fidelity.writes.*` keys
(`paper/derive/engine_fidelity.py`).

Every other script here needs the engines extra (`just setup`; without it they stop at `No module named
'requests'`).

### The read side, offline: the paper's numbers from its saved live capture

paper-results/ (`just fetch-results` places it) holds the live databases' side of the check under
`paper-results/engines/ldbc/sf<SF>/fidelity/<family>/`: their counters per (query, database, param)
(`live_kvstats.csv`, and each cell's raw `kvstats.json`), each cell's answer (`answer.json`) and the live Dgraph's uids
that IC7's plan follows (`dgraph/uids_*.csv`). Two commands re-check the paper's paragraph against it with no database
running:

```bash
just results engine-fidelity     # 14 min 39 s the first time, 9 min 40 s after: each engine's own plan on the evaluator, against the saved counters and answers
just figure engine-fidelity      # under 1 s: the fidelity.* keys from that run
```

`just results engine-fidelity` runs `validate.py --live --from paper-results` for LDBC Interactive at sf0.3 and sf0.1
and for the microbench at sf0.3 (it prints each command) and writes
`output/engines/ldbc/sf<SF>/fidelity/<family>/engine_validation.csv`.

It needs the LDBC CSVs at sf0.1 and sf0.3 (its first step per scale is `fetch-dataset`, which downloads what is
missing) and the evaluator's binary (`just build`). **It builds its own stores the first time, no command of yours:**
per scale and database, the store of the database's engine plan (`dgraph_engine`, `nebula_engine`, `arangodb_engine`)
and the store of its format, where our cheapest plan runs (`dgraph`, `nebula`, `arangodb`): 12 evaluator stores under
`output/stores/ldbc/` (1.1 GB at sf0.3, 0.4 GB at sf0.1; 26 to 39 s each at sf0.3, 9 to 13 s at sf0.1), from one record
stream per scale (0.7 GB under `output/stats/`). A later run reuses them. No live database is involved.

A pass (a 32-core AMD EPYC 9354P; the first run on an empty output dir with the CSVs in place, and a run
with the stores built):

| run | first run | stores built | ends with |
|---|---|---|---|
| LDBC Interactive, sf0.3 | 673 s (6 stores: 198 s) | 441 s | `count fit    : 628/684 suite cells` and `vs LIVE engine: 684/684` |
| the microbench, sf0.3 | 46 s (the same stores) | 46 s | `count fit    : 200/207 suite cells` and `vs LIVE engine: 207/207` |
| LDBC Interactive, sf0.1 | 159 s (6 stores: 65 s) | 93 s | `count fit    : 414/414 suite cells` and `vs LIVE engine: 414/414` |
| the whole chain | 879 s | 580 s | `results OK: engine-fidelity` |

`just figure engine-fidelity` then prints, among its 91 keys, `fidelity.reads.cases = 891`, `fidelity.reads.exact = 722`,
`fidelity.reads.split = 141`, `fidelity.reads.rest = 28`, `fidelity.reads.rest.maxdev = 1.8`, `fidelity.sf03.fit = 628`
and `fidelity.sf03.answers = 684` with `fidelity.sf03.answers.differ = 0`: the keys `paper/derive/engine_fidelity.py`
gives on paper-results/ itself.

What the offline check reads, and what it does not re-make:
- **An answer counts as checked** (`answer_source` = `stored`) when it was stored for today's query text and params and
  comes from the load the capture's counts were measured on (the `data_created` stamp in `live_kvstats.csv`). An answer
  that does not match is an error naming what differs; no database is asked. `--stored-answers` skips the query-text
  and load checks (`stored-unchecked`), and the derive refuses such a run.
- **`holdout_visibility.csv` is shipped, not re-made.** It records which holdout counts a committed capture already
  held before the plans were frozen: a record of history, made by `holdout_visibility.py` from the source repository's
  history (`--source-repo`), which the artifact does not carry. The derive reads it from paper-results/ always, with
  the previous load's counts and the 08-11 run.
- One scale alone: `just results engine-fidelity --dataset ldbc=0.1` (93 s with its stores built, 159 s without); `just figure engine-fidelity --dataset
  ldbc=0.1` then reads sf0.1 from the run and the rest from paper-results/, and says so. One query:
  `uv run --locked python experiments/engine_fidelity/validate.py --sf 0.1 --live --from paper-results --query ic7`
  (5 s with the stores built; `count fit    : 18/18 suite cells`).

### The read side, live

`measure.py` and `validate.py --live` need the three servers up on the engine backups
(`engines/README.md`; `bash engines/once.sh --db all --sf <SF>` loads and backs up the three at that SF). `--param` is
the paper's capture's 12 params: the 6 seen ones and the 6 holdout ones (`inputs/params/ldbc/ldbc_holdout.json`);
without it, `measure.py` measures the 6 seen ones only. The microbench has its own live counts, measured first:

```bash
SF=0.1 bash engines/start-server.sh --db all
uv run --locked python experiments/engine_fidelity/measure.py --sf 0.1 --param 1,2,7,8,13,14,19,20,25,26,31,32   # live /kvstats per (query, db, param)
uv run --locked python experiments/engine_fidelity/validate.py --sf 0.1 --live          # the evaluator against it, with the live answers
SF=0.3 bash engines/start-server.sh --db all
uv run --locked python experiments/engine_fidelity/measure.py --sf 0.3 --param 1,2,7,8,13,14,19,20,25,26,31,32
uv run --locked python experiments/engine_fidelity/validate.py --sf 0.3 --live
uv run --locked python experiments/engine_fidelity/measure.py --sf 0.3 --family microbench   # every microbench param
uv run --locked python experiments/engine_fidelity/validate.py --sf 0.3 --family microbench --live
```

Each `validate.py` ends with its counts. The passes:
- sf0.1: `count fit    : 414/414 suite cells` and `vs LIVE engine: 414/414`;
- sf0.3: `count fit    : 628/684 suite cells` and `vs LIVE engine: 684/684`;
- the microbench: `count fit    : 200/207 suite cells` and `vs LIVE engine: 207/207`.

The cells outside 10% are Dgraph's: it reads a posting list it split in parts at 512 KB with one GET more per part,
and the evaluator's count is short by exactly those GETs (`engine_validation.csv`: `split_residual_G`, `residual_exact`
YES; the microbench's 7 are chain). A new load's live counts equal an earlier load's on S, G and N, except Dgraph's
IC7 (`first: 200` takes the first 200 likers in uid order, and a load assigns uids anew); Dgraph's byte counts move
with its uids too. The capture in paper-results/ is the load's (LDBC Interactive and the microbench).

NebulaGraph's cells that are not exact (28 of 228 at sf0.3, each count within 1.8% of the engine's) come from one
rule of its variable-length Traverse: the next step's sources are the destinations it has not expanded yet *when their
edge is read*, and a source of the current step counts as expanded only once the response iteration has passed it. So
a vertex that is a source and a destination of the same step is read again at the next step, or not, by the order of
the storage response: the engine's order, which the evaluator does not model.
- **IC1 (above at every param, by 2 to 10 SEEKs):** the twin reads every first-hop friend with a friend among the
  friends again at step 3 (an upper bound); the engine leaves out 1 to 5 of them per param. Leaving all of them out
  would undershoot by more.
- **IS2 (above at 5 params by 1 or 2 of each):** the same rule on its `REPLY_OF*1..3` walk; the twin skips none.
- **IC12 (exact at p14, below at 11 params by 1 to 3 of each):** the twin drops every class that is a source and a
  parent in one step of the class walk (a lower bound); the engine reads `Person` and `Agent` again unless their rows
  came first.

`engine_validation.csv`'s `bound` column asks whether our cheapest plan's weighted operations are at most the engine's
own plan's. On LDBC's hosted params at sf0.3 it fails on 6 of the 633 cells it applies to: IC5 at p19 and p26 on the
three databases, with equal answers. Both params have a `minDate` after the last forum join: no forum
qualifies, the engines' plans read no post, and our pick (the list join) still reads every friend's message list (1.9x
to 2.0x the engine's plan on Dgraph and NebulaGraph, 1.003x on ArangoDB). At the ten other params the pick is 4x to
200x below the engine's plan. It is a known cheaper plan, not adopted: `python/kvevaluator/workloads/README.md`.

### The write side, live

The paper's live write sweep can be made again: the write primitives (MB81-MB88 of the mb2 microbenchmark) on the three
live databases at sf0.3, 5 params each (the evaluator's writes params, the rows `kvcost measure --family writes` binds),
read back by `write_fidelity.py` in place of the archived sweep. The writes change the store, so each database starts
from a clean restore of its backup (`start-server.sh` restores it), and is restored again before any later read run:

```bash
SF=0.3 bash engines/start-server.sh --db all
uv run --locked python experiments/engine_fidelity/measure.py --family writes --sf 0.3
kvcost measure --dataset ldbc=0.1 --family writes dgraph-c512k nebula arangodb
uv run --locked python experiments/engine_fidelity/write_fidelity.py \
    --sweep output/engines/ldbc/sf0.3/write-sweep/write_sweep.csv \
    --capture output/captures/ldbc/sf0.1/writes/reported/counts
bash engines/stop-server.sh --db all
```

`measure.py` prints one line per cell, then `120 rows -> .../write-sweep/write_sweep.csv` and `cells ok per engine:
dgraph 40, nebula 40, arangodb 40`; it exits 1 on a cell that errs, on a dump without the engine's write counters, or
on no cell. Each cell's raw `/kvstats` dump is kept beside it (`write-sweep/writes/<db>/<primitive>/p<N>/kvstats.json`).
The compare ends with `23 compared cells: equal 8, equal after the named differences 15, unexplained residual 0`, as
on the archived sweep. Run time (a 56-thread Intel Xeon Gold 5512U, two runs): the restores 29 to 33 s
(Dgraph), 26 to 27 s (NebulaGraph) and 9 to 10 s (ArangoDB), the sweep 2 to 3 s, the writes counts 24 s (skip it when
the capture is there), the compare under 1 s.

The texts are `engines/queries/writes/<db>/<primitive>.<ext>`, one per primitive `write_fidelity.py` maps to an MB
number; their own dir, so the read microbenchmark does not pick them up. MB89 (a hasInterest edge insert) is not
ported: no evaluator primitive measures it and the compare does not read it. `measure.py`'s docstring has the binding
rule, the cell order and why the order changes no count.

Against the archived sweep, cell by cell on the params both hold (the anchors differ: ours are the evaluator's), D and G
are equal everywhere and P and COMMIT everywhere but these: Dgraph's vertex delete (its count grows with the person's
degree), one NebulaGraph edge delete whose two persons sit in two partitions (a commit key per partition written), and
now and then a NebulaGraph cell with one or two commit keys more. NebulaGraph commits each of its 100 partitions every
10 s with no client write (an idle store counts 100 commits and 100 PUTs per 10 s), and such a commit can land in a
cell's counter window; the archived sweep has it too (an MB89 cell with three commits, one more than an edge's two
partitions). The compare takes each primitive's median over its params, which these leave as it is.
Dgraph reads one SEEK more per anchor (the person is found by `eq(id, ...)` with `type(person)`, as its read texts find
it; mb2 found it by `fqid`), and ArangoDB's edge delete reads the source's edges in the edge index (its N follows the
source's degree).

paper-results/ holds, beside the read capture above: the previous load's live counts
(`engines/ldbc/sf0.3/fidelity-repeat/`), the 08-11 run (`fidelity-legacy/`), the complex-kv-reads figure's run
(`complex-kv-reads/`; it stores its IC1 variants in an earlier shape until the archive's next re-pack) and the live write sweep (`write-sweep/write_sweep.csv`).

## What is here

| file | what |
|---|---|
| `measure.py` | the live engines' `/kvstats` per (query, db, param) -> `live_kvstats.csv`, the baseline `validate.py` compares against (LDBC or microbench); `--family writes`: the live write sweep -> `write-sweep/write_sweep.csv`, which `write_fidelity.py --sweep` reads |
| `validate.py` | runs every engine-faithful plan on the evaluator and compares its counts and answer with the live engine's, per cell; `--live` checks the answers too (stored ones, else it asks the engines); `--from paper-results` reads the paper's saved capture, with no engine; `--stored-answers` reuses stored answers unchecked |
| `live_answers.py` | the live engines' answers, fetched once and stored raw: an engine is asked again only when what decides its answer changed, and a parser fix is a re-parse |
| `answers.py` | how two answers are compared: key normalisation and row checks |
| `deviations.py` | the documented engine deviations: which answer differences are excused, and why (an allow-list, each entry with its reason) |
| `holdout_visibility.py` | were a holdout cell's live counts readable before the plans froze: a read-only report over `engine_validation.csv` |
| `write_fidelity.py` | the canonical formats' write counts against the live engines' write sweep, per write primitive; reads saved data only |
| `time_queries.py` | the complex-kv-reads figure's three queries timed at a cache regime (`--regime`: a name of the registry, `inputs/contract/store_config.json`; the figure's is `warm-bounded`), one raw row per run, into the figure's run: `live` (each engine configuration on the cells it is measured on, at the counting operating point, its defaults, or the equivalent setting: one CPU per engine, engine caches off, equal block caches, `ENGINES_SETTING=equivalent` in `engines/env.sh`; per run its S / G / N, which must equal the counted row, and its time in storage; at a cold-start regime, `cold-bounded`, it starts the server anew before every run and writes how much of the store was in the OS page cache) and `evaluator` (each engine's plan and, once per engine, our cheapest plan on our evaluator; an engine plan's counts must fit this run's live counts, `validate.py`'s rule) |
| `storage_profile.py` | A live engine's time in its KV store per run, measured from outside the engine (`time_queries.py live --storage-profile`): perf call stacks on the engine's CPU, each sample owned by the KV store or by the engine, times the engine's on-CPU time, plus the time its threads waited on a disk read. The rule per engine is in its docstring. Dgraph's stacks are frame pointers; NebulaGraph's and ArangoDB's need Intel LBR: on a CPU without it their KV CPU time is not measured, and the rows say so |
| `compare_complex_kv_reads.py` | a fresh complex-kv-reads run against the paper's, cell by cell: every S, G and N equal and every byte lane within 1%; one verdict line, then the fresh run's times as they are (a time decides nothing). `just figure complex-kv-reads` runs it last |

The figure's cells, its engine configurations (a database with its settings: Dgraph with and without its first-name
index on IC1, TuGraph with detach_property off and on), its query-text variants (NebulaGraph's IC1 binding the walk's
edge list), its run's files and the rule that makes a timed cell are in one module,
`python/kvengines/complex_kv_reads.py`. A configuration is measured like a database: `measure.py --run complex-kv-reads
--db dgraph-no-index --query ic1` counts it, `live_answers.py` checks its answer (the answer must not change with the
configuration), `time_queries.py live --db dgraph,dgraph-no-index` times both after one start; a query-text variant is
a query of its own name (`measure.py --run complex-kv-reads --db nebula --query ic1-edge-list --param 1 --append`), its
text the base query's file with `-edge-list` after the stem.
`just results complex-kv-reads` runs the whole chain from fresh databases (the main README's table) and prints each command.

## A worked case: why the engines read more than the cheapest plan on IC2

IC2 (a person's friends' 20 newest messages) at sf0.1, param 1. The cheapest plan our layouts allow walks the date
index and reads only the ~20 winners: 25 GETs. The engines' own plans read every message the friends wrote (3,811).

- **NebulaGraph** runs the query as a `UNION ALL` of a Post branch and a Comment branch. Each branch looks up its tag
  on all 3,811 messages: 2 x 3,811 = 7,622 GETs predicted, 7,640 measured live. After a failed tag lookup, storaged
  also runs a prefix scan; that is the engine's choice, not the key layout's, so it is charged only in the engine twin
  (`python/kvevaluator/formats/nebula_engine.py`, `read_vertex_props`).
- **ArangoDB** does the same in its plan: both subqueries expand the friends again, and each walks all 3,820 messages
  before it drops the other type (1,013 + 2,807 = 2,800 + 1,020). The message hop has no projection, so each message
  costs its primary-key lookup plus the whole document. The optimizer does not use the `creationDate` index, though it
  exists.

So most of the gap on IC2 is the engine's plan, not its key layout: the formats charge the same per access as the
engine twins, and the plans differ.

## Warm and cold

Figure 2's three reads at its own cache regime, `warm-bounded`, and from a cold start, `cold-bounded` (the regimes:
`engines/README.md`, "Cache regimes"): the latency of one read on a live database held to one CPU, LDBC SNB SF0.3, the
median of 10 timed runs. A cold run is on a database started anew with its store out of the OS page cache; a warm run
follows 3 warm-up runs. The disk wait is the time the server's threads were blocked on a disk read during the run,
summed over its threads: threads that wait at once add up, so it can pass the run's own time (Dgraph's Q1).

| query | database | warm, ms | cold, ms (min to max) | cold / warm | cold: disk wait, ms |
|---|---|---|---|---|---|
| Q1 (one hop) | Dgraph | 4.1 | 25.9 (24.5 to 26.8) | 6.3 | 37.9 |
| Q1 (one hop) | NebulaGraph | 7.7 | 17.3 (16.7 to 18.0) | 2.2 | 6.8 |
| Q1 (one hop) | ArangoDB | 4.3 | 14.9 (14.3 to 16.9) | 3.4 | 8.8 |
| Q1 (one hop) | TuGraph | 4.1 | 20.1 (18.5 to 20.7) | 4.9 | 7.9 |
| Q1 (one hop) | TuGraph, detached | 3.6 | 23.9 (22.7 to 24.9) | 6.6 | 11.4 |
| Q2 (IC1) | Dgraph | 76.7 | 101 (83.9 to 112) | 1.3 | 15.6 |
| Q2 (IC1) | Dgraph, no index | 110 | 121 (113 to 140) | 1.1 | 17.5 |
| Q2 (IC1) | NebulaGraph | 324 | 689 (682 to 693) | 2.1 | 312 |
| Q2 (IC1) | ArangoDB | 682 | 782 (722 to 822) | 1.1 | 45.0 |
| Q2 (IC1) | TuGraph | 149 | 590 (576 to 609) | 4.0 | 382 |
| Q2 (IC1) | TuGraph, detached | 190 | 583 (576 to 604) | 3.1 | 331 |
| Q3 (IC3) | Dgraph | 11,032 | 11,016 (10,912 to 11,167) | 1.0 | 71.3 |
| Q3 (IC3) | NebulaGraph | 5,077 | 10,113 (10,071 to 10,151) | 2.0 | 5,085 |
| Q3 (IC3) | ArangoDB | 3,477 | 5,342 (5,317 to 5,368) | 1.5 | 1,688 |
| Q3 (IC3) | TuGraph | 1,170 | 3,144 (3,099 to 3,232) | 2.7 | 1,776 |
| Q3 (IC3) | TuGraph, detached | 1,434 | 3,808 (3,803 to 4,005) | 2.7 | 2,079 |

A cold start costs the short read most (2.2 to 6.6 times its warm time on Q1) and the long reads 1.0 to 4.0 times.
TuGraph pays the most on Q2 and Q3 (4.0 and 2.7 times) and is still the fastest on Q3 (3.1 s cold); NebulaGraph
doubles on all three; Dgraph's Q3 does not move, its 11 s being CPU work (`engines/README.md`). Made by
`just results complex-kv-reads-cold --dataset ldbc=0.3` after `just results complex-kv-reads --dataset ldbc=0.3`;
`just figure complex-kv-reads --dataset ldbc=0.3` draws `complex_kv_reads_latency-cold` from it.

## Expected run time

A 32-core AMD EPYC 9354P (the drivers' timing lines). Starting the servers from their backups takes about
1 min; building all three backups about 11 min.

| step | sf0.1 | sf0.3 |
|---|---|---|
| `measure.py`, the LDBC suite at 12 params | 1.7 min | 10.3 min |
| `validate.py --live`, asking every engine for its answer | 7 min | 41 min |
| `validate.py --live`, all answers stored, every suite cell (414 / 684 cells) | 2.6 min | 17.4 min |
| `validate.py --family microbench --live` (207 cells) | - | 1.4 min |
| `just results engine-fidelity`, offline on the paper's capture, the stores built: LDBC / the microbench | 93 s | 441 s / 46 s |
| the same on an empty output dir (it builds its 6 stores per scale: 65 s and 198 s of ingest) | 159 s | 673 s / 46 s |
| `measure.py` at 12 params, then `validate.py --live` asking every engine | 155 s, then 252 s | 11 min 19 s, then 19 min 5 s |

At sf0.3 ArangoDB dominates the live time.

The same steps on a 56-thread Intel Xeon Gold 5512U, at artifact 4991d35b: loading and backing up the three
at sf0.1 3.8 min; starting them 1.1 min; `measure.py` at 12 params 1.8 min (sf0.1) and 7.3 min (sf0.3); `validate.py
--live`, asking every engine, 5.4 min (sf0.1) and 14.2 min (sf0.3); the microbench's `measure.py` 2.3 min and its
`validate.py --live` 52 s; offline, the ic7 line 81 s and `--stored-answers` at sf0.3 11.1 min.

From scratch on a fresh machine of that type (56-thread Intel Xeon Gold 5512U, artifact 5b9a5d7b), the
"read side, live" commands as written, every pass equal to the ones above: `just setup-engines` 38 s; `engines/once.sh
--db all --sf 0.1` 37.9 min (the three builds, one after another, then the loads and backups); `--sf 0.3` 6.9 min;
starting the three 1.1 min per scale; `measure.py` at 12 params 1.8 min (sf0.1) and 7.8 min (sf0.3); `validate.py
--live`, asking every engine, 4.0 min (sf0.1) and 18.3 min (sf0.3); the microbench's `measure.py` 2.1 min and its
`validate.py --live` 43 s. In all 1 h 23 min.

`just results complex-kv-reads` (the whole chain of the complex-kv-reads figure; a 32-core AMD EPYC 9354P,
3 warm-up and 10 timed runs per cell), with the three databases built:

| step | sf0.1 | sf0.3 |
|---|---|---|
| `engines/once.sh --db all` (loading and backing up what has no backup) | 252 s (all three) | 93 s (NebulaGraph; Dgraph's alone 5 min 48 s, ArangoDB's 63 s) |
| `engines/start-server.sh --db all` | 68 s | 66 s |
| the counts (with Dgraph without its index and NebulaGraph's edge-list text) | 9 s | 20 s |
| `time_queries.py live`, Dgraph / NebulaGraph / ArangoDB (each after its own start, 8 to 29 s) | 156 / 54 / 54 s | 219 / 76 / 87 s |
| `time_queries.py evaluator` (its six stores built the first time) | 74 s | 345 s |
| the whole chain | 12 min 27 s | 16 min 28 s |

`just results complex-kv-reads-cold` (the same three queries at `cold-bounded`, on the run the chain above made; a
28-core Xeon Gold 5512U, sf0.3, 10 timed runs per cell, each on a database started anew): 53 min 49 s.
`time_queries.py live` with its starts: Dgraph's two configurations 1,376 s (about 34 s a run), NebulaGraph 991 s
(33 s), ArangoDB 466 s (16 s), TuGraph 138 s for each of its two configurations (5 s); `time_queries.py evaluator`
112 s. Its perf samples and start logs (`times/equivalent/cold-bounded/perf/`, `starts/`) take 1.2 GB.

A first run also builds the databases (`engines/README.md`: side by side here, Dgraph 1 min 56 s, ArangoDB 10 min 40 s,
NebulaGraph 23 min 30 s; `once.sh` builds them one after another). `just setup-engines` installed 11 missing needs in
39 s. perf's raw samples stay in the run (`times/equivalent/warm-bounded/perf/`, about 0.3 GB at sf0.3).

The chain also measures TuGraph, the figure's fourth database (`engines/tugraph/README.md`: its release is fetched,
not built), in the figure's configuration: a vertex's properties in its packed record (`tugraph`). Its second, the
properties in a table of their own (`tugraph-detached`), only an option of the draw shows: `just results
complex-kv-reads-other-configurations` adds it to the run (the configurations the figure does not read:
`complex_kv_reads.other_configurations`). With every backup in place, the chain and that item took
12 min 58 s at sf0.3 for the six configurations (a 56-thread Intel Xeon Gold 5512U): the two counting
rounds 83 s and 15 s to start and 23 s and 4 s to count; the timing, Dgraph's two configurations 180 s, NebulaGraph
98 s, ArangoDB 83 s, TuGraph 27 s and 33 s; the evaluator 107 s. TuGraph's loads take 6 s and 10 s.
