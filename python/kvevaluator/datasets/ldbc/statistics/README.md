# LDBC's statistics

The statistics predict mode reads for LDBC SNB, one file per scale: `sf0.1.json`, `sf0.3.json`, `sf3.json`. Each is
`kvevaluator graph-stats` over that scale's record stream, as `python -m kvevaluator.predict.build_stats` makes it.
The fields are the contract's (`inputs/contract/stats.schema.json`); none of them names an LDBC label or edge type.

`build.json` is what LDBC declares to the build: its anchor label (Person: the vertex a query's parameter binds) and its
date axis (Post and Comment by creationDate: the default for a date rule whose step names no labels). Another dataset
gets its own `statistics/build.json`, or none.

The anchor statistics (each Person's own degrees and recency rows) are not committed here: they are per vertex, so
they are rebuilt beside the output (`output/stats/ldbc/sf<SF>/anchor_stats.json`) by the same command.

**Check:** `python3 tests/gates/check_statistics_rebuild.py --sf 0.1` rebuilds the file from the record stream with the
binary under test and compares every entry but the file's identity (provenance, wall time, the stream's path). It runs
in the sf0.1 round (`run/meta-gates.sh`). After a change to graph-stats, regenerate with `--write` at each scale and
commit the files with the change.

**Expected run time** (graph-stats, one pass over the stream, with burstiness): sf0.1 6 s, sf0.3 17 s
(45 s with the stream's dump from the CSVs), SF3 258 s (291-378 s on a busy machine). sf30 is not
committed yet: it is generated when its record stream is current on the machine that builds it.
