# inputs/latency-picks: the body each store times

One file per read family at a scale, `<dataset>/<scale>/<family>.json`, in the shape of the timing's own record
(`summary/<dataset>/<scale>/grid-latency/<family>/picks.json`): `criterion` (the pick rule), `counts_capture` (the
counts it was picked on, below their tree) and `cells`, `{store: {query: body}}`, for the latency figure's 12 uniform
layouts. `(pin)` is the plan file's own body.

| file | from |
|---|---|
| `ldbc/sf3/interactive.json`, `ldbc/sf3/bi.json`, `imdb/full/job.json` | the paper's counts (`--from paper-results`): the bodies its timing ran |
| `ldbc/sf0.1/interactive.json`, `ldbc/sf0.1/bi.json`, `imdb/sample1pct/job.json` | counts made at that scale |

**Why a file.** A pick is the cheapest body on the layout's counts (the plan rule,
`python/kvcost/latency/grid_latency.py` `picks_of`), and counts are the same on every machine. Keeping the pick here
lets the latency run time without counting: a reviewer's run builds the stores and times them. A layout or scale with
no file entry, or an entry naming a body the plan files no longer have, is counted first, as before
(`grid_latency.file_picks` decides, for the run and for the timing alike).

**Not the pick record.** `inputs/picks/` is the plan rule's input (each store's decided body, the anchor of every
pricing); this is the rule's output for the timed stores, read only by the timing.

**Writing one.** The same command as the timing, with `--save-picks`: it counts the named layouts (or reads the counts
in `--from TREE`), writes their picks here and times nothing.

```bash
kvcost measure-latency --dataset ldbc=3 --workload i01 Sp-Sp Co-Co Ch64k-Ch64k Sp-Co Co-Sp Ch1k-Ch1k Ch16k-Ch16k \
    Ch4k-Ch4k Ch256k-Ch256k Ch1k-Ch4k Ch1k-Ch16k Ch4k-Ch16k --save-picks --from paper-results
```

Then time without the flag (a store whose body moved since its timing is timed again; the others keep theirs);
`git diff inputs/latency-picks/` shows what moved. When counts are in the output dir and
their pick differs from the file's, the timing keeps the file's body, marks the store `body_moved` and prints these two
commands.
