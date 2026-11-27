// The executor: runs a bound plan step by step; each KV op fans out into tasks on a pool of
// P workers (P=1 = one worker, the same path); flow ops are one task. Collects the three
// instrument layers -- counters (logical S/G/N + bytes from the landing replay, physical
// from the workers and from RocksDB's tickers), latency (per-task wall + PerfContext/IOStats
// + codec timers, aggregated per step), and the plan-level span / work / wall.
#pragma once
#include "exec/label_tags.h"
#include <string>
#include <vector>

#include "formats/format.h"
#include "ir/plan.h"
#include "store/rocksdb_store.h"
#include "value/value.h"

namespace evalcpp {

struct RunConfig {
  int P = 1;
  size_t task_batch = 64;        // contiguous tasks a worker takes at once
  std::vector<int> pin_cpus;     // one core per worker; empty = no pinning
  int warmup = 0;
  int measured = 1;
  std::string cache = "asis";    // asis | warm | cold
  int perf_level = 2;            // RocksDB PerfLevel: 2 counts only, 3 adds its per-call timers (~1% of a cold wall)
  bool emit_iterations = true;
  bool emit_tasks = false;       // per-task rows (volume)
  // the RocksDB histograms (histograms_delta per measured iteration, histograms_since_open): each snapshot merges
  // every histogram's per-core copies (~2.7 ms per request on 64 cores, 75% of a short request's server time, all
  // outside the plan clock). Off by default: at that cost they are not a free channel, and a reader asks
  // for them (run.emit.histograms = true). The tickers (the
  // counts) are always taken.
  bool emit_histograms = false;
  // rows_in_labelled visits every input row of every step (outside the step clocks, inside the harness time): on
  // by default (the counts runs), off for a latency run: run.emit.input_labels = false leaves the field out
  bool emit_input_labels = true;
  // the per-request bookkeeping on the read and write paths (the KV-pair size histograms, range reads, crossed keys):
  // a few additions per landing and per GET, 1-3% of a row layout's step walls. On by default (the counts runs);
  // run.emit.kvpair_stats = false (the latency drivers) leaves it out: no "hist", "chunks", "rows_in" or decode counts
  // in the response (a step is then field for field the response of before these statistics), no histogram check
  bool emit_kvpair_stats = true;
  // out_bytes per step: a walk over each step's output at its step's end, timed out of the plan wall (a later step may
  // take the output by move). On by default (the counts run
  // keeps it); the latency drivers turn it off (run.emit.out_bytes = false): the counts run of the same cell has it.
  bool emit_out_bytes = true;
  // rows_digest / rows_set_digest per step (run.emit.row_digest, default off): a digest of the step's output rows, in
  // order and with the order ignored, taken at the step's end like out_bytes. A CHECK, not a statistic: no
  // cost reads it. It lets a gate compare what flows between steps across layouts (tests/gates/check_grid.py);
  // off, a step is field for field the response of before it. Stable for one binary, not across builds.
  bool emit_row_digest = false;
  // run.emit.pinned_usage (default off): each kv step reports the block cache's pinned and total bytes at its end,
  // before the pooled iterators' deletes (pinned_bytes_at_end, cache_usage_at_end), and the capacity (block_cache_bytes)
  bool emit_pinned_usage = false;
  // run.kv_timing (default off): time every RocksDB call at the API boundary (store/perf_snapshot.h); each step's kv
  // block gains kv_call_ns and the per-kind ns and calls, the response a kv_timing block with the timer's own cost.
  // Off, a response is field for field the response of before the option.
  bool kv_timing = false;
  // run.decode_timing (default off; on with kv_timing too): the codec clocks (codec/timers.h: decode_ns, decompress_ns).
  // Off, they take no time and each step's codec block reports them as null; chunks_decoded and the counts stay
  bool decode_timing = false;
  // run.free_rows (default off): free a step's output once no later step reads it, instead of at the iteration's
  // end. Peak memory only: no count, byte, row or answer moves. For COUNTS runs: a latency run refuses it (the frees
  // would run between the steps, inside the span the plan clock covers). Off, the executor does exactly what it did
  // before the option: the flag is one local bool, read before the first iteration.
  bool free_rows = false;
  // run.free_outputs: where an iteration's step outputs are destroyed after the plan's clock. "inline" (default): at
  // the iteration's end on the request's thread, timed: totals.free_outputs_ns (in no wall). "reaper" (exec/reaper.h):
  // on a background thread; totals.reaper_done_ns is the time of the frees it finished since the previous response.
  // Measured (IC5, SF0.1, 4 runs a mode in a row): the reaper's free takes 55 ms against 25 inline (memory
  // freed on another thread), and the next request waits for it: its window +14%, its plan +3.7%. So inline stays the
  // default. No count, byte or row depends on it
  // run.iterator_pool: each worker store reuses one RocksDB iterator across a step's SEEKs (CountingStore's pool,
  // deleted at the step's end). Counts never change; RocksDB's own work does: a reused iterator keeps its data block
  // and table iterator (fewer block-cache lookups, hits pinned through the step) and its readahead state spans SEEKs.
  // On by default (an engine keeps an iterator per reader); off: a new iterator per SEEK, as before the pool
  bool iterator_pool = true;
  std::string free_outputs = "inline";
  // the page of a range read in the round-trip counts (store/round_trips.h): run.remote.page_bytes. It moves
  // round_trips, round_trips_critical and range_pages_extra only; the response carries it
  uint64_t remote_page_bytes = uint64_t(1) << 20;
  // the remote regime (store/round_trips.h Remote): run.remote.delay_us > 0 turns it on; mode operation | batch | step
  // (batch when not given); bandwidth_mbps 0 = no bandwidth term. Off by default: no count and no clock moves
  uint64_t remote_delay_us = 0;
  std::string remote_mode = "batch";
  double remote_bandwidth_mbps = 0;
  bool drop_caches = false;      // cold: also echo 3 > drop_caches (needs sudo; off by default)
  uint64_t sample_every = 0;   // Stage B sampler (0 = off)
  // write-latency mode: writes APPLY to the real store instead of the shadow overlay. The
  // session only allows it on a checkpoint-managed arena dir; from_json pins P=1,
  // warmup=0, measured=1, cache=asis (an applied write cannot re-run).
  bool apply_writes = false;
  // per-part attribution (store/counting.h PartScheme): each step also emits "parts" -- its counts
  // split by the store part each op touched -- when the format names parts (meta); a format
  // without a scheme emits nothing. Off by default: the counts are identical either way.
  bool parts = false;
  // each KV step also emits "args": its resolved arguments (the vid / candidate lists, labels, props), and
  // "out_vids": the _vid of each output row when the output is a list of rows (e.g. a top-k's winners). Off by
  // default (volume); the counts are identical either way. For offline models that replay a plan's reads.
  bool trace_args = false;
  // the opt-in reuse-distance pass (store/reuse_distance.h): each step also emits "reuse", its fetched kvbytes by
  // the reuse distance of the KV pair, "reuse_blocks" (the KV pairs' own 4 KB blocks) and "reuse_request_blocks" (each request's bytes rounded up once) by
  // the same buckets; the distances
  // run across the requests of the session until reuse_reset.
  // Needs P=1 (the order of fetches is the measurement). Off by default: nothing is tracked.
  bool reuse_distance = false;
  bool reuse_reset = false;      // forget every KV pair first (the start of a pass)
  // which reads tag their output vertex ids with a label (exec/label_tags.h): run.label_tags = {"hops", "probes"}
  LabelTagPolicy label_tags;
  static RunConfig from_json(const json& j);
  json to_json() const;
};

// What a batch asks of each response beyond the RunConfig (cli/session.cc Session::batch); a single request takes the
// defaults, and its response is the one of before these options.
struct ResponseOpts {
  bool rows = true;                             // false: no `rows` (a batch without emit_rows erased them unread)
  // drop exact duplicates only: the run echo (the batch's response carries it once) and a step's per_iteration whose
  // every field equals the step's own; the fixed note strings
  bool slim = false;
  const std::vector<size_t>* tickers = nullptr; // statistics_delta over these tickers (TickersNameMap indices); null: all
  const json* run_echo = nullptr;               // run_echo(cfg, store), built once per distinct run; null: built here
};
// The response's `run` block: the config, the store's options, the host's cpu note.
json run_echo(const RunConfig& cfg, const Store& store);

// Runs the plan; returns the response object (without provenance).
json execute(Format& fmt, Store& store, const Plan& plan, const RunConfig& cfg, const ResponseOpts& opts = {});

}  // namespace evalcpp
