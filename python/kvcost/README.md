# kvcost

The analysis on top of the C++ evaluator's counts: it prices layouts and mixes, searches for the cheapest mix of a
workload, and draws the figures. It measures nothing itself (the evaluator does, through
`python/kvevaluator/measure/`). The words it uses: [Concepts](../README.md#concepts) in `python/README.md`.

**Entry point:** the `kvcost` command (`uv run kvcost -h`), one subcommand per step: [`cli/README.md`](cli/README.md).

| package | what it holds |
|---|---|
| [`cli/`](cli/README.md) | the `kvcost` command and its subcommands |
| [`layout_cost/`](layout_cost/README.md) | the model layer: the settings, the cost models, composing a mix from measured parts |
| `layout_search/` | the mix search and the Pareto front, over `layout_cost`'s prices |
| [`latency/`](latency/README.md) | the readers of measured latency captures |
| `predict_accuracy/` | predicted against measured counts, per step and per layout (`kvcost predict-accuracy`) |
| `plotting/` | the figures' shared style, statistics and formatting |
