// What a KV op gets from the executor for one step: a way to run its tasks (fan-out) and the
// place where their results and instruments are collected.
#pragma once
#include <atomic>
#include <string>
#include <vector>

#include "exec/label_tags.h"
#include "exec/pool.h"
#include "exec/tasks.h"

namespace evalcpp {

class StepExec {
 public:
  StepExec(ThreadPool& pool, size_t batch, bool keep_task_rows) : pool_(pool), batch_(batch), keep_rows_(keep_task_rows) {}
  // Runs the tasks (ordered results); their landings/stats are folded into the step's totals.
  std::vector<TaskResult> run(const std::vector<TaskFn>& tasks);
  // The range primitive (pool.h run_range_map) with the step's instruments folded in: the
  // workers' counts delta goes to `physical` and -- for an order-independent op (every
  // fan-out whose per-src counts are simply summed; the chunk-reader ops replay landings
  // instead) -- to `direct` as well. In per_task_perf mode the body runs as one task per
  // src on the classic path, so the per-op-latency casestudy keeps its per-src rows.
  std::vector<Value> run_range_map(size_t n, const ThreadPool::RangeBody& body, bool additive_counts);
  // The append form (pool.h run_range_append): src i's items appended in index order, one list for the step
  Value run_range_append(size_t n, const ThreadPool::AppendBody& body, bool additive_counts);
  // The step's totals so far
  const std::vector<Landing>& landings() const { return landings_; }
  Counts physical, direct;
  // per-part attribution (only when the pool's stores carry a PartScheme): the parts of `direct`,
  // committed by the tasks whose counts are logical; the replayed landings' parts come from the replay
  PartCounts direct_parts;
  // the KV-pair size histograms of `direct` (store/size_hist.h), by part id (one entry, id 0, without a scheme)
  HistParts direct_hists;
  std::vector<DecodeCounts> decode_parts;   // parts on: the decode counts (codec/decode_counts.h) by part id
  std::vector<ChunkParse> chunk_parses;     // parts on: the shared chunks' parses, not in decode_parts (replay.h)
  RtTotals direct_round_trips;         // the round trips of `direct` (store/round_trips.h): summed over the step's jobs
  RtText direct_rt_text;               // ... and its levels, job by job (the step column rt_levels)
  uint64_t remote_wait_ns = 0;         // the remote regime, mode operation: what the step's requests waited, summed
  uint64_t scan_chunk_bytes_max = 0;   // the largest chunk a fused scan held (it holds one at a time)
  uint64_t work_ns = 0, wait_ns = 0, task_max_ns = 0, critical_ns = 0;   // critical = sum over runs of the longest task
  size_t ntasks = 0;
  PerfSnapshot perf;
  CodecStats codec;
  const std::vector<TaskStats>& task_rows() const { return task_rows_; }   // only when asked for
  int P() const { return pool_.P(); }
  // labels on the rows (Value::label): which reads may tag what they output (run.label_tags), and whether a vertex
  // write (delete_vertex, insert_vertex) has run earlier in this run -- a tag states a label, not that the vertex still
  // exists, and only a vertex write can change that (an edge or property write cannot), so after one an existence
  // check probes. Set by the executor per step
  LabelTagPolicy tags;
  bool after_vertex_write = false;
  bool strict_tags = false;   // run.trace_args (the checkers): a vid with two labels in one input throws (first_tags)
 private:
  void fold_range(ThreadPool::RangeStats& st, bool additive_counts);   // a range job's counts and stats into the step's
  ThreadPool& pool_;
  size_t batch_;
  bool keep_rows_;
  // between jobs (main thread, no worker running): committed parts -> direct_parts; pending dropped
  void collect_parts();
  void end_job();                     // the workers' pooled iterators deleted (CountingStore::end_step)
  std::vector<Landing> landings_;
  std::vector<TaskStats> task_rows_;
};

}  // namespace evalcpp
