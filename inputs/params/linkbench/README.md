# LinkBench's params: what is tracked

LinkBench's params are drawn from its own seeded request log, one dir per scale (`n1m`). They are 26 MB at n1m, so
they are regenerated, not tracked. What is tracked here proves the regeneration:

| file | what | read by |
|---|---|---|
| `inputs/frequencies/linkbench/<scale>/mix.json` | the request log's count and share per query, and the sha256 of the `requests.jsonl` it came from (a frequency file, so it lives there) | `make_workloads.py` (L01-L04's frequencies) |
| `<scale>/linkbench/params.sha256.txt` | the sha256 of every file `gen_linkbench_params.py` writes: `mix.json`, `request_log.jsonl`, `A/`, `B/`, `all/` | `gen_linkbench_params.py`, which checks each run against it (rc 1 on a difference) |

Re-make the params at n1m (about 30 s of data generation, then 11 s):

```bash
just fetch-dataset linkbench=n1m     # the data (the recorded clock replayed, each file's sha256 checked), then the params: "params: 50 files equal to ..."
```

The params archived with the measured run (`linkbench/n1m/remeasure-20261012`, `params.tar.zst`) predate getlink's
id2s list (2026-10-24) and today's write params: 24 of their 49 files differ from a regeneration on the same data, and
today's code refuses them. Regenerate them; the sha256 list proves the result. Their `mix.json` counts are the same.

A new scale, or a deliberate change of the params' format: `gen_linkbench_params.py --scale <scale> --track` writes both
files from that run; then `make_workloads.py --dataset linkbench --scale <scale>` writes its L01-L04.
