# kvevaluator — the C++ measurement engine of the KV-format evaluator

A second interpreter of the neutral plan IR, written over RocksDB (stock v7.5.3 by default, pinned by commit). Python
hands it one request (IR + params + store + schema); the binary runs the whole plan — KV ops, in-memory flow ops, chunk
codecs, counting — and returns the rows, S/G/N, real bytes, per-step latency with its RocksDB breakdown, and
span/work/wall (critical-path, summed-task, and wall-clock time).

Why it exists: the Python path cannot give a latency number (rocksdict has no PerfContext, and
Python sits in the loop of every op). The C++ path measures the format.

Python keeps everything that is not a timed run: plan authoring → IR files, param generation,
the in-RAM AbstractKV oracle (the answer key), orchestration and analysis.

## The invariant

For the same (IR, params, store), the C++ counts and answer rows equal the Python oracle's wherever the oracle models
the format. `just cpp-diff` (`run/cpp-diff.sh`, over `tests/gates/check_cpp_vs_python.py`) checks it on the sf0.1
stores and must stay at 0 mismatches. What is compared depends on the format:

| format | compared against the Python twin | where |
|---|---|---|
| Dgraph, NebulaGraph, ArangoDB | per step S / G / N / P / D and the miss counts; the rows as a set and in order. Not the bytes (`--skip-bytes`: the Python value model is not the C++ codec) | `just cpp-diff`: every LDBC and microbench plan file (parts `counts_dgraph`, `counts_nebula`, `counts_arangodb`), BI on Dgraph and NebulaGraph (`bi_dgraph`, `bi_nebula`), JOB's 16-query sample (`job`); BI on ArangoDB in `run/meta-gates.sh` (step 14) |
| Per-type CSR | the rows and their order on every plan file; S / G / N, the misses and the value bytes only on the steps its twin does not disown (the twin is a rows-and-order oracle: `python/kvevaluator/formats/pertype_csr.py`, its docstring) | `just cpp-diff`: `pertype_rows`, `pertype_counts`, `probe_counts` (the chunk reader's S, S_miss and scan bytes, replayed) |
| meta | nothing: its Python side is size-only (no read operators) | `run/meta-gates.sh`: each engine-shaped corner spec against its named format: the same answers and per-step work outside the differences it lists, and every grid cell's answers against NebulaGraph (`run/meta-vs-formats.sh`) |

`just golden-counts` also pins the counts of 50 read cells (Dgraph, NebulaGraph, ArangoDB and two meta cells) and 3
write cells against a recorded run. Never compared with Python: the key bytes (Python's key bytes are a structural
model) and the used bytes (`count_used`, C++ only; the executor refuses a step whose used bytes exceed what it read,
and golden-counts pins them on its read cells).
Bytes are *realistic*: engine-width keys, physical chunk bodies.

## Build

`just build` (`run/build_cpp.py`) builds this dir in the machine's build cache and hard-links the binary into the
tree; a tree with uncommitted changes to this dir (or to the schema and spec dirs it reads) builds in place,
incrementally, linking the cache's RocksDB. `run/build_cpp.py --list` lists the cache's entries and sizes. This dir also builds on its own, in place: CMake fetches RocksDB at a pinned commit and builds it with RocksDB's own Makefile, then
builds the binary `kvevaluator` (~2 min + ~2 min at -j16). Once per machine, the -dev packages the library links
against (Ubuntu): `libsnappy-dev libzstd-dev liblz4-dev libbz2-dev zlib1g-dev libgoogle-perftools-dev cmake`.

    cmake -S cpp/kvevaluator -B cpp/kvevaluator/build && cmake --build cpp/kvevaluator/build -j 16
    cpp/kvevaluator/build/kvevaluator --version    # provenance: git sha + dirty, RocksDB release@commit, compiler

| CMake option | default | what |
|---|---|---|
| `KVEVAL_ROCKSDB` | `7.5.3` | the RocksDB release: `7.5.3` (commit 540d5aae51, the paper's measurements) or `11.8.1` (abeebd9630; C++20) |
| `KVEVAL_ROCKSDB_SOURCE` | (GitHub) | an offline source: a local git clone, or a `.tar.gz` with `KVEVAL_ROCKSDB_TARBALL_SHA256` |
| `KVEVAL_ROCKSDB_JOBS` | all cores | `make -j` for the RocksDB build |
| `KVEVAL_ROCKSDB_DIR` | (this build dir) | a shared RocksDB build, linked as it is when it holds its done-marker (`run/build_cpp.py` builds one per release and flags, `cmake/rocksdb_pins.cmake`) |
| `KVEVAL_BUILD_COMMIT`, `_DIRTY`, `_KEY` | (ask git) | the commit, dirty flag and cache key a build of a git archive stamps (`run/build_cpp.py`) |

The fetched source must sit at the pinned commit and its `version.h` must name the release (`cmake/verify_rocksdb.cmake`,
run between download and build); a tarball is also checked against its sha256. Two gcc 13 flags are passed the way
`cmake/rocksdb.cmake` says (`DISABLE_WARNING_AS_ERROR=1`, `-include cstdint` in the environment, so the Makefile keeps
its RTTI flag).

**Provenance.** `--version` and every response name `rocksdb_source` (release@commit): that is the library's identity.
`librocksdb_sha256` is information only: two builds of one commit differ (RocksDB's `build_version.cc` embeds the build
date). `binary_git_sha` / `binary_dirty` come from the git tree that holds this dir; a release archive with no git says
its commit (RELEASE_COMMIT) and `release`; any other copy with no git says `unknown` for both. Which states may run a
check and which may be cited: `python/kvevaluator/capture/binary.py` (binary_state).

**Repo paths.** The binary finds the repo root at run time from its own location (`/proc/self/exe` plus the relative
path CMake writes), so a moved or renamed clone still works; a store manifest records its schema path relative to that
root. The repo-relative dirs the tests read (the LDBC schema JSONs, the layout specs) are named once, at the top of
`CMakeLists.txt`.

Facts about the link: the `.a` is non-PIC (we link `-no-pie`); stdout stays a clean JSON channel (messages go to
stderr). The default build makes kvevaluator only; `ctest --test-dir build` builds the unit tests first (its
`build_unit_tests` step, also for `-R <name>`; or `cmake --build build --target unit_tests`), then
runs them (`tests/test_expr.cc`: the value model + the expression grammar table). The tests share one harness,
`tests/harness.h`: a file's `TEST_CASE(test_x)` is one ctest test, run as `kvevaluator_tests test_x [--keep-tmp]`
(test_value is its own binary: it replaces the global operator new); each prints `checks=N fails=M`, and a case that
runs no check fails.

**The allocator.** The binary links tcmalloc (gperftools, `libgoogle-perftools-dev`) statically, not glibc's malloc:
glibc's malloc gets slower in a heap the process has used, so a storage step's warm latency grew with what ran
before it. At start the binary
sets tcmalloc's release rate to 0 (memory is not returned to the OS mid-run) and refuses to run if malloc is not
tcmalloc's. Every response's `provenance.allocator` names the allocator, its version, the release rate and any
allocator environment variables set. **A capture with no `provenance.allocator` was measured on glibc** (every one
measured before the switch to tcmalloc): its latencies are not comparable with tcmalloc runs (op counts are: identical on all 417
cpp-diff cells), and `cpplatency.py --accumulate` refuses to mix the two. The block cache charges a block at its data
bytes under either allocator (`make_block_cache`; tcmalloc's size classes would otherwise cost a bounded cache 10-25%
of its blocks, by an amount that depends on the format's block sizes).
`test_latency_stability` (ctest) is the check: a SEEK on a 59 KB value and its copy must cost the same (within 10%)
fresh and after a 600 MB allocation history, and a cached 59 KB block must be charged at its data bytes. Fresh and
after are two processes on one CPU taking alternate passes (the median round decides), so a load on the machine that
comes or goes mid-test does not fail it
(`tests/run-latency-stability-under-load.sh BINARY step13|square|steady` runs it under a load). Bursts of many short tasks
still can: beside the first minutes of a gate round it failed 3 of 19 runs (0 of 40 under 7 to 64 CPU spinners),
so `run/meta-gates.sh` runs it alone, after its other streams (stream zt). It fails on
a glibc build (by design: that build exists only for before / after runs). The old build, for before / after runs only:

    cmake -S cpp/kvevaluator -B <dir> -DEVALUATOR_ALLOCATOR=glibc

Never link an allocator by `-l` search here: a `/usr/local/lib/libjemalloc` built with the `je_`
prefix (it exports `je_malloc`, not `malloc`) is found first, so `-ljemalloc` or `LD_PRELOAD` of it silently keeps glibc.

## Use

The binary is `build/cpp/kvevaluator` after `just build`; run the Python commands from the repo root with the repo's
environment (`uv run --locked python -m ...`). To run one query on one store and read its counts and answer, the
short way: [inputs/workloads/README.md](../../inputs/workloads/README.md#run-one-query), "Run one query"
(`kvevaluator.measure.run_one`).
To add a format (a meta spec or a new format dir): `src/formats/README.md`.

    # a store's keys and bytes per (part, type, direction), read-only; exit 1 if the totals differ from the manifest:
    kvevaluator census output/stores/ldbc/sf3/meta-<hash>
    # 1. the schema artifact (once per schema; hashed into every store)
    uv run --locked python -m kvevaluator.datasets write-schema ldbc   # -> inputs/schemas/ldbc/schema.json
    # 2. the record stream -> stores (one pass, several compressions)
    # native path: the C++ CSV reader IS the producer (no Python, no pipe)
    kvevaluator ingest --schema .../schema.json --csv-root .../sfN/CsvBasic-LongDateFormatter \
        --out output/stores/ldbc/sf0.1/pertype_csr-c64k --compression snappy,zstd --chunk 65536   # snappy: the decided codec, no suffix
    # one pass, many formats (and a RAM budget that batches formats, byte-identically).
    # the default sweep is these four:
    #   --multi "pertype_csr=<p1>,nebula=<p2>,dgraph=<p3>,arangodb=<p4>" [--ram-budget-gb 8]
    # the neutral record stream itself (feeds the Python oracle; the record-hash gate):
    kvevaluator records --schema .../schema.json --root .../CsvBasic-LongDateFormatter
    # legacy pipe (the frozen Python parser is test-reference only -- KVEVAL_PY_PARSER=1):
    uv run --locked python -m kvevaluator.measure.records_dump --sf 0.1 > records.jsonl
    kvevaluator ingest --schema .../schema.json --out ... < records.jsonl
    #    --format nebula | dgraph | arangodb | meta   the other formats (same stream)
    #    --uids / --strings     swap one column encoder inside the plain layout
    # 3. run a plan (one request) or serve many over one open store
    kvevaluator run request.json          # response JSON on stdout
    kvevaluator serve < requests.jsonl    # one request per line, one response per line
    # 4. the differential test (at any P), a store's byte digest
    uv run --locked python tests/gates/check_cpp_vs_python.py --nparam 5 [--P 8] [--format nebula --skip-bytes]
    kvevaluator dump <store-dir>          # sha256 over every (key, value): two ingests must agree

`python/kvevaluator/measure/server.py` builds requests and drives `serve` from Python; `plan_runs.py` runs plans through it.

Request and response shapes: Python builds a request in `server.build_request`; the binary reads `run` in
`src/exec/executor.cc` (`RunConfig::from_json`). In short, a request is
`{store: {dir, format, schema}, plan: <IR>, params: {...}, run: {P, task_batch, pin_cpus,
iterations, cache, perf_level}}` (perf_level defaults to 2, RocksDB counts without its per-call timers; 3 adds the
timers to `kv`) and the response carries `rows`, per-step `steps[]` (logical
S/G/N and bytes, `physical` = what the workers did, `wall_ns`, `work_ns`, `span_ns`, `tasks`,
`kv` = PerfContext/IOStats, `codec` = decode timers, `rows` / `out_bytes` = the step's output
row count and neutral byte size, `per_iteration`), `totals` (span, work,
wall, per-worker busy time), the plan-level `rocksdb.statistics_delta` (every ticker that
moved in one iteration), `run`, `provenance`, and `error`.

**Parallel runs (M2).** Every KV op fans out into independent tasks: one per vid, per source
vertex, or per index walk (a closure per level). `run.P` workers execute them; the results
merge back in task order, so the rows are the same at every P.

The logical S/G/N are *defined* as the P=1 rule replayed over the tasks' probe landings
(pertype_csr's chunk reader charges a SEEK per probe that lands outside an already-decoded
chunk's id range, so the count depends on the order the probes arrive in). `physical` reports
what the worker threads actually did: it equals the logical counts at P=1 (asserted) and can
differ at P>1 (reported, never hidden). `pin_cpus: "physical"` pins one worker per physical
core; `task_batch` (64) is the run of contiguous tasks a worker takes at once.

**Round trips and the remote regime.** Every step also reports what its requests would cost a client whose
store is one network hop away (`src/store/round_trips.h`). A request is one GET, one range read, or one further
page of a range read (`run.remote.page_bytes`, 1 MiB). Requests whose keys are known before any of them is
answered form a level: they can be sent together. `round_trips` counts, per level, 1 for all its GETs plus 1 per
range read or page, and 1 for a job's writes; `round_trips_critical` counts the levels, the chain a client waits
through; `range_pages_extra` the pages after a range read's first. Inside one source of a fan-out a request is
taken to need the one before it, unless the operator marks the reads as independent (`RtParallel` in
`src/store/counting.h`). The three numbers are the same at every P and move no other count.

`run.remote.delay_us` (off by default) makes the wait real: a sleep per round trip, inside the step's clock,
reported as `remote_wait_ns`. `run.remote.mode` says where the network sits: `operation` (every request waits,
in the worker that makes it), `batch` (a step waits `round_trips_critical` times the delay) or `step` (a step
that reads or writes waits once). `run.remote.bandwidth_mbps` adds fetched bytes / bandwidth. The latency
driver's flags are `--remote-delay-us`, `--remote-mode`, `--remote-page-bytes`, `--remote-bandwidth-mbps`
(`python/kvevaluator/measure/cpplatency.py`); a capture holds one setting, written on every row and in its meta.

Two constraints worth knowing:
- One process per store directory at a time: runs open the store read-write (RocksDB's read-only open of a fully
  compacted store records no GET ticker, in 7.5.3 and 11.8.1 alike), so a second opener sees the RocksDB `LOCK`.
- A rocksdict store (rocksdict 0.3.29 is RocksDB 9.8.4, SST format_version 6) cannot be opened by the 7.5.3 build;
  the 11.8.1 build opens it. C++ stores and Python stores are never shared.

## Layout

    src/schema/    the dataset schema (from Python's schema.json), the far-end label rule, the manifest's schema path
    src/ir/        plan IR: the body to run (one body, or the parked files' selector), fragments, bind + coerce, the expression grammar
    src/contract/  the facts C++ and Python share (inputs/contract/*.json: the KV-op table, the capability vocabulary,
               the capture columns), compiled in at build time; its README says what each field means
    src/value/     the row/value model with Python's equality, ordering, hashing
    src/ops/       the in-memory flow ops (shared by every format)
    src/exec/      the task fan-out executor (worker pool, landing replay) and the instrument layers
    src/store/     RocksDB backend: fixed options, compression registry, counting seam, timers; the
               multi-part unit writes (unit_value, unit_posting, and unit_list = meta-v4's list RMW;
               meta-v4 = the configurable format's current version, experiments/meta/README.md)
    src/index/     the sorted KV index component (nebula's layout; pertype_csr borrows it)
    src/codec/     varints, codec timers, the swappable column encoders; meta-v4's
               one property encoding (columns) and one list encoding (lists)
    src/formats/   one directory per format (peers), nothing shared between them: pertype_csr/
               {keys,chunks,format}, nebula/{keys,rowv2,format,engine}, dgraph/{keys,uidpack,format,engine},
               arangodb/{keys,vpack,format,engine} (engine = the engine-fidelity twin, never a compared
               format), meta/ (the configurable format: config, parts, keys, index, reads, writes, ingest, and its
               cost rules for predict mode in predict.cc); the shared row surface and op helpers in rows.h / common.h, the name registry
               in registry.cc
    src/ingest/    record stream -> flat buffers (column arenas, edge pairs) -> one SST file per
               key-prefix run -> ingest -> settle -> manifest (sf30 fits in RAM)
    src/predict/   predict mode (counts without a store): the statistics file, the per-step cardinality layer and
               the chunk-landing closed form; format-free, the format's own rules are in formats/meta/predict.cc
    src/util/      small helpers: the record stream reader, sha256, base64, vids and the vid hash set, the cycle counter
    src/cli/       the commands (`kvevaluator --help` lists them: ingest, run, serve, predict, tally, census, ...)
    tests/         unit tests

**NebulaGraph's key order.** The `nebula` format (and `nebula_engine`) stores keys in the engine's
order, the vertex id first (`'v' vid tag`, `'e' src etype dst`). The older order, the type first,
is the arm `nebula-typefirst` (`KVEVAL_NEBULA_KEY_ORDER=type_first`): the same fields and counts, another
neighbourhood on disk; it is never a baseline.

Milestones: M0 skeleton, M1 pertype_csr + compression at sf0.1 P=1, M2 parallel executor +
scale (all done); M3 (this tree): the `read_filter_by_label_probe` /
`read_filter_by_label_index` op split (the access path is the op name; a parked bundle's body is
selected by the format's capabilities on the schema), nebula and dgraph -- every
format checked against the Python oracle by the differential test (`just cpp-diff`).
