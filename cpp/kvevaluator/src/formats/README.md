# Adding a format

A format decides how the graph is written into keys and values, and what each read operator costs on it. There are two
ways to add one.

## 1. A spec or a new format dir?

**Route A, a meta spec (no C++).** `meta` is the configurable format: a JSON spec places each part of the schema
(vertex properties, adjacency, index) in the design space, per label, per edge type or per direction. If your layout
can be written as a spec, write a spec. Test: can the fields at the top of `meta/config.h` express it, and does
`validate()` there accept it (it refuses every combination meta does not build)?

**Route B, a new format dir.** For a layout the spec cannot say: another key scheme, an engine's own encoding. This is
C++ work and comes with its own count checks (section 6).

## 2. Route A, step by step

1. Write the spec: copy one from `inputs/specs/grid/` (the nine uniform layouts); the fields are documented at the top
   of `meta/config.h`, and `inputs/specs/README.md` lists the spec sets.
2. Run one query on it: [inputs/workloads/README.md](../../../../inputs/workloads/README.md#run-one-query), "Run one query" (`run_one --family interactive --store my_layout.json`); its store is built first.
3. Put it in a grid run: `kvcost measure --dataset ldbc=0.1 --workload i01 <its cell id>` (or a spec dir of your own as the layout).

**The spec's field names and the paper's words.** The paper names three settings per part; a spec writes each as three
fields (`meta/config.h`, lines 8-15):

| paper | `group` | `owners` | `split` | an example |
|---|---|---|---|---|
| Split | `"item"`: one item per record, the item in the key | `"one"` | `"never"` | `inputs/specs/grid/props_split_adj_split.json` |
| Condensed | `"owner"`: all of one owner's items in one record | `"one"` | `"never"`, or a size (adjacency: 524288) | `props_condensed_adj_condensed.json` |
| Chunked | `"owner"` | `{"pack": bytes}`: consecutive owners packed into chunks of about `bytes` | `"never"` | `props_chunked_adj_chunked.json` |

The field `split` is not the setting Split: it is where ONE owner's value overflows into ordered parts.

## 3. Route B: the files

A format is one dir here, in the shape the others use (`nebula/` is a short one to copy):

| file | holds |
|---|---|
| `format.h` / `format.cc` | the class, its capabilities, the dispatch `run_kv_op` |
| `keys.h` / `keys.cc` | the key scheme |
| `ingest.cc` | how a vertex and an edge are written (`add_vertex`, `add_edge`, `finalize`) |
| `reads.cc`, `index.cc` | the read primitives |
| `writes.cc` | the write operators, if the format takes writes |

Each read operator's meaning is written once, in `common.cc` (`run_operator`); a format supplies the primitives it
calls (`common.h`, `class OperatorReads`), and only those. A format never includes a sibling (`registry.cc`'s header).

A format that packs many vertices into one value (Per-type CSR, `meta`'s Chunked parts) does not count its chunk reads
as they run: each task logs where its probes land, and `exec/replay.h` replays the logs in task order, so the counts
are the same at every thread count.

## 4. Route B: where its name must be listed

Each row is marked **required** when leaving it out is refused, with the code that refuses; **only for** when only one
use reads it; **unchecked** when neither was shown.

| where | what it lists | without your format |
|---|---|---|
| `registry.cc`, `build_format` | the formats the binary can build | **required**: "unknown format" (`registry.cc:38`) |
| `registry.cc`, `kFormatCounting` | each format's counting versions {read, write} | **required**: "no counting version for format" (`registry.cc`, `format_counting`) |
| `registry.cc`, `key_layouts` | the key layout a store's manifest must match | not refused: a store with no layout entry passes the stale-store check (`python/kvevaluator/capture/store_identity.py`: "no information is not a refusal") |
| `cli/ingest.cc`, `kPeakPerCsvGb` | peak ingest RAM per GB of CSV | only for `--ram-budget-gb`: an unlisted format is assumed at 1.6 (`ingest.cc`, the `kPeakPerCsvGb.find` line) |
| `python/kvevaluator/plan_choice/generate.py`, `NAMED` | the named stores each workload generates plan bodies for | unchecked |
| `python/kvevaluator/plan_choice/dump_planir.py`, `CANON_FORMATS` / `LDBC_FORMATS` / `MB_FORMATS` | the formats a workload's plan bundles serve | unchecked |
| `python/kvevaluator/plan_choice/pick_record.py`, `LDBC_NAMED` | the named stores with recorded picks | only for `tests/unit/check_pick_record.py` |
| `python/kvevaluator/plan_choice/pick_record.py`, `JOB_FORMATS` | JOB's named stores | only for JOB |
| `python/kvevaluator/layouts.py`, `NAMED` | the named formats kvcost accepts as a layout | **required** to name it in a kvcost command: "is not a layout" (`layouts.py`, `resolve`) |
| `python/kvevaluator/measure/kvstats_layout.py`, `FORMAT_ORDER` | the formats a capture's store slug can start with | **required** for the named formats' capture: "names no known format" (`kvstats_layout.py`, slug parsing) |
| `python/kvevaluator/formats/__init__.py`, `FORMATS` | the Python twins | only for the C++ against Python check: a format may be C++ only |

## 5. Capabilities

A format declares the plan capabilities it grants (`capabilities()`, `edge_capabilities_for()` in its `format.h`):
what its keys let a plan do, e.g. NebulaGraph's `whole_row_read` (one GET returns a vertex's whole row) and
`sorted_range_index` (a date index read in order; `nebula/format.cc`, `Nebula::capabilities`).
A query's bodies are selected by them, so a capability the format lacks removes the bodies that need it.
`just cpp-diff` checks that the Python side selects from the capabilities the binary grants
(`tests/unit/check_caps_parity.py`).

## 6. Which check proves its counts

The C++ against Python table in `cpp/kvevaluator/README.md`, "The invariant", says what is compared for each kind of
format. A Route A spec is checked with meta (`run/meta-gates.sh`). A Route B format is checked against Python only if
it has a Python twin; without one, no gate checks its counts until you add a twin or its cells to `just golden-counts`.

## 7. Picks and the grid

A new store has no pick: a query with more than one body is refused until one is recorded or named.
[inputs/workloads/README.md](../../../../inputs/workloads/README.md#run-one-query), "Run one query", says what you see
and the two ways out.

## 8. What a format is charged

Charge what the layout forces under the cheapest plan it allows, and set every deployment knob the same way for every
format: a format is charged the structural cost of encoding the graph, not a deployment setting.
