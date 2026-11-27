# kvevaluator: the evaluator in Python

It defines what runs (the schema, the datasets, the plan IR, the formats, the workloads), says what a capture and a
store are, chooses plans, and drives the C++ binary. It measures nothing itself: the Python store is an oracle for the
sf0.1 checks only.

## The layers

Each unit imports only its own layer and the layers below. `tests/lint/check_layers.py` holds the order (`LAYERS`) and
counts every import that goes up, also one inside a function (the `layer-up` ratchet, at 0: a new one fails `just lint`).

```mermaid
flowchart BT
  base["0 base: paths, contract, weights, cost, jsonbytes, stale, adoptions, layouts"]
  schema["1 schema/"] --> base
  datasets["2 datasets/: each dataset's loader and params"] --> schema
  sizes["2 sizes: a run's scales per dataset (--scale; inputs/sizes.json)"] --> datasets
  plans["3 plans/: the plan IR and its binding (ir/, plan.py, units.py)"] --> schema
  kvstore["4 kvstore/: the oracle's in-RAM KV store"] --> base
  formats["5 formats/: the Python formats, the store caps (caps.py)"] --> plans
  formats --> kvstore
  oracle["6 oracle/: run a plan on the Python store (runner, interp, run)"] --> formats
  workloads["7 workloads/: each query as plans, its params"] --> plans
  workloads --> datasets
  capture["8 capture/: a capture's columns, the step row, a store's identity, the binary"] --> workloads
  tree["8 output_tree: where every output lives (stores, stats, captures, composed, summary, figures)"] --> workloads
  fetch["8 fetch_dataset: `just fetch-dataset`, each dataset's fetch step and its workloads' data steps"] --> workloads
  choice["9 plan_choice/: candidates, the pick record and its lookups (resolve), the optimizer, the generators"] --> capture
  measure["10 measure/: the drivers (param_census among them)"] --> choice
  predict["11 predict/: predict mode's drivers"] --> measure
```

In words: a lower layer does not know a higher one. The plan IR (3) knows no format; a format (5) knows no workload;
`output_tree` (8) says where every output lives and `capture/` (8) what a capture's rows hold, for everyone above them;
`fetch_dataset` (8) gets a dataset's data and then what its workloads need beside it (JOB's reference answers), so a
dataset never imports a workload; plan choice (9) reads captures
to pick a body; the drivers (10, 11) run it all. A workload (7) binds the body it is handed (Interactive's
`bound_plan`, the microbench's `bound_body`); which body is plan choice's (`plan_choice/resolve.py`, from the pick
record).

Inside a layer the growth axes stay apart too: a dataset imports no workload, a workload no other workload, a format
only the shared format modules (`check_layers.UNITS`, `FORMAT_USES`).

## Where a new module goes

The lowest layer whose imports it needs. A helper that two layers both need goes to the lower one; never import up and
never re-export a name from a module it moved out of (move the callers).
