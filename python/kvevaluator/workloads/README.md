# Workloads: one folder per workload

A workload is a set of queries on one dataset (`../datasets/<name>/`: its schema, loader and param files). Each folder
holds the workload's `manifest.json` (its dataset, kind, how a plan is picked, the formats it runs), its queries'
questions (`questions.json`), its queries (`queries/`), its plan bundles (`plans/*.json`) and, unless the dataset's
param files serve it, its parameter reader (`params.py`): `interactive` (LDBC SNB Interactive), `bi` (LDBC BI reads),
`microbench`, `writes`, `linkbench`, `job`. `__init__.py` here is the one table of workloads and queries, built from
the manifests. A manifest's `predict_gate` block says what predict mode's exact gate (`run/predict-gate.sh`) runs on
the workload beyond its queries, and the rules no request reaches, each with why. How a plan is chosen among a
bundle's bodies: `kvevaluator/plan_choice/candidates.py`.

## Known cheaper plans, not adopted

A plan is changed only when the change moves a reported conclusion (which layout is best for a workload, a headline
ratio, a mix's gain or picks), when it is about 4x or more on a query's own cost on some layout, or when correctness
needs it. This is the list of cheaper alternatives known and left, each with its size, so they are not rediscovered.
Add a line; it is not a work list.

Sizes are estimates from the SF3 data (each query's own counts reproduced exactly before the estimate;
nothing executed unless a row says "measured"). Ratio = alternative / today, weighted operations per run, on Split /
Condensed / Chunked adjacency.

| query | the alternative | ratio | why it is not adopted |
|---|---|---|---|
| IC12 | the tag class's lists replace the per-post tag read | 0.74 / 0.74 / 0.54 | changes no conclusion; reads one list per tag of the class, so it loses on a wide class |
| IC12 | start from the class's tagged posts | under 0.01 everywhere | wins only because every tag class in the parameter file is tiny (1-9 tags); loses badly on a wide class |
| IC11 | the country's list replaces a read per company | 0.82 / 0.80 / 1.00 | small; wrong for the 12 largest places in the data |
| IC4, BI7 | cut on the count first, read tie-break names after | at most 0.92-0.95 | small |
| IS7, microbench edge probe | other read orders of the edge-exists check | 0.99 on IS7 over 35 params (measured); 0.74-0.92 on the edge probe over 6 params (measured) | a few parameters decide it; symmetric by chance |
| BI10 | the tag class's lists, tags read only for the matches | 0.28 / 0.23 / ~1.0 (assumed) | at the bar (3.6x / 4.3x); two of five params gain only 1.1x; kept on this list, not adopted |
| BI14 | no creator read per parent / per liked message (the other side's lists already say whose it is) | 0.63 / 0.63 / not run | always valid, under 4x |
| BI18 | no second knows read | 0.62 / 0.65 / not run | always valid, under 4x |
| BI3 | start from the country's forums and walk the threads down | 0.80 / 0.83 / unknown | cheaper on 3 / 2 of 5 rows only |
| BI4 | creators from the top forums' members' lists | 0.87 / 0.80 / not run | small |
| BI8 | the date window from the index | 1.01 | loses on 4 of 5 rows |
| BI2, IC11 names, the chase on nebula / arangodb, LinkBench getlink on Split | stopping a scan early | ~1.0 | negligible |
| IC3 on NebulaGraph / dgneb (retired) | cut the friends' messages to the date index's window before the creationDate read: the window's messages come out of a per-tag index, so their tag is known, while the same messages reached through hasCreator (a Post or a Comment at the far end) are read with a wrong-tag miss first | at least 0.77 on NebulaGraph, 0.75 on dgneb (SF0.3, 5 params, measured misses: 33,257 miss GETs of 145,991 / 134,712 weighted ops; an upper bound of the saving, the alternative was not run) | under the bar; the exact-window twin (`ic3_exact_window_country_lists`) already drops this read where the index is exact |
| IC5 | the creator read per post of the candidate forums (the body the list join replaced, still measured as `ic5`), on a param whose `minDate` is after the last forum join: no forum qualifies, so that read costs nothing, while the list join still reads every friend's message list | 0.40 / 0.50 / 0.52-0.61 on such a param (estimated from the capture's own steps; the list join costs 1.3-2.5x there, in wops and in the op-byte cost); over the capture's ten SF3 params (measured) the per-post read is cheaper on none of the ten on any of the 41 layouts that hold IC5 rows: the list join is cheaper on nine, by 1.6-12.6x, and equal on one (a person without friends) | a choice per parameter value, cheaper for some values and dearer for the others: `minDate` is that late on 34 of the 243 hosted SF3 rows and on none of the capture's ten; choosing it needs a histogram of an edge property (`hasMember.joinDate`), which ordinary statistics do not hold, or a choice at run time. IC5 is 7-38% of I01's op-byte cost over the 49 layouts; with every late row costed by the cheaper body I01 would move under 2%, and its two cheapest layouts share the adjacency setting these reads depend on. Changes no conclusion |
| vertex delete on NebulaGraph (`w_del_v`) | with the keys in the engine's order (vid first) all of a vertex's edges sit under one `'e' vid` prefix: one scan finds them, instead of one prefix scan per (edge type, direction) | not sized | a write primitive no workload weights; the layout allows it, the plan does not use it |

Adopted (for the record, not part of this list): IC5's list join and IC3's two changes (they change
I01's headline ratio and picks); IC10's interest-tag lists, IC1's start from the name matches, IC9's index walk with a
creator check and IC6's start from the tag (a per-layout pick): each is a large factor on the query's own cost (4x to
150x).

BI was sized the same way (read-only; the 17 base plans' per-step counts reproduced on nebula and dgraph,
1,950 of 1,950 steps; estimates; every BI parameter family has only 5 rows, so nothing could be checked on held-out
parameters). Nothing found: BI5, BI7, BI8, BI11, BI17; BI16's alternative already exists as a measured twin
(`bi16_index`). The three most expensive queries (BI12, BI1, BI9) are whole-window scans with one read per message,
which no plan avoids on a one-key-per-vertex layout.

BI6, BI2, BI13 were sized on the nine uniform layouts (an estimate from the SF3 counts): with the cheaper
of today's pick and the alternative per layout, BI's best uniform layout does not move under any model
(Chunked/Chunked by weighted ops and by ops + bytes / 4 KB, Split/Condensed by bytes). The other layouts' ratio to the
best shrinks by weighted ops (Split/Split 233x -> 207x) and grows by ops + bytes / 4 KB (28.5x -> 32.4x), because
Chunked/Chunked gains most there (1.56 M -> 1.23 M). Not adopted: no reported conclusion changes.
