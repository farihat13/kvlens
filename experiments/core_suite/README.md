# core_suite: the per-format figures of a kvstats capture

What each format costs per query on one workload: the KV operations (SEEK, GET, NEXT), the bytes read by lane, and the
warm and cold latency, side by side for every format captured. The measurement is `run/kvstats.sh`; this folder holds
its figures.

## Run

```bash
run/kvstats.sh --sf 0.1                                   # measure LDBC on dgraph and nebula, then draw
run/kvstats.sh --sf 0.1 --plots-only --show dgraph,nebula,arangodb   # draw a selection from the captured CSVs
uv run --locked python experiments/core_suite/plot_kvstats.py --sf 0.1 --family interactive   # the figures alone
```

`run/kvstats.sh --sf 0.1` is the smallest run. It needs the LDBC SF0.1 data (`just fetch-dataset ldbc=0.1`) and builds the two
stores on first use. A pass prints `ALL CHECKS PASS (all formats agree)`, then the run record's status `(ok)`, and
draws its figures into `<output dir>/summary/ldbc/sf0.1/named-formats/interactive/plots-all/`.

It also prints a warning:

    [warn] 24 empty (query,param,db) cells across 4 queries -- check these are genuinely empty, not a filter silently matching nothing:
        ic11     8 cells empty  (params 1,3-5)
        ic3     10 cells empty  (params 1-5)
        ic4      4 cells empty  (params 3,5)
        is7      2 cells empty  (param 1)

This is not a failure. These (query, param) pairs return no rows on both formats, and the Python oracle gives the same
rows for them (`just cpp-diff` compares the rows of every plan at params 1-5). At SF0.1 some of LDBC's params match nothing (IC3 asks for a
friend's messages in two given countries within a 30-day window, IS7 for the replies to message 3). The warning stays
because an empty answer on every format at once is the one case the cross-format check cannot catch: a filter that
silently matches nothing.

It reads the capture `output/captures/ldbc/sf<SF>/<workload>/reported/` (counts and steps; one file per store) and the
timings `output/timings/ldbc/sf<SF>/<workload>/<store>/<regime>/` (the warm, cold and write-latency passes), and writes
the figures to `output/summary/ldbc/sf<SF>/named-formats/<workload>/plots/` (or `plots-<formats>/` for another
selection). It opens no store.
The archived kvstats captures (`sf3/kvstats-20261001`, `sf30/kvstats-20261001`, `sf30/kvstats-latency-20261007`) come
from `just fetch-results`.

## What is here

| file | what |
|---|---|
| `plot_kvstats.py` | the figures of one (scale, workload) capture: a grid per query tier (weighted ops by operation, read bytes by lane, warm and cold latency), read amplification and cold block reads per query and format |

## Expected run time

`run/kvstats.sh --sf 0.1` (measure LDBC on the Dgraph and NebulaGraph formats, then draw), on a 16-core AMD EPYC 7302P
with a SATA SSD: 151 s the first time (it builds the two stores), 133 s with them built. On a 40-core node
whose data was on a hard disk: 3 min 8 s. The figures alone (`--plots-only`) open no store.
