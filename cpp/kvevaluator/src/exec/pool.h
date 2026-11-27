// P workers = the calling thread (slot 0) plus P-1 helper threads. `run` hands out a task
// list; workers take contiguous batches in index order and fill results[i] for task i. At
// P=1 the caller does every task itself, so there is no thread hand-off (moving work to a
// different thread) on the clock. The pool is HOT: helpers spin on an atomic generation
// counter for the pool's whole life (one plan run) and never sleep, so no task runs on a
// core that is coming out of idle -- the thing a latency measurement must not fold into a
// task's wall clock.
#pragma once
#include <sched.h>

#include <atomic>
#include <condition_variable>
#include <memory>
#include <mutex>
#include <thread>
#include <vector>

#include "exec/tasks.h"
#include "store/rocksdb_store.h"

namespace evalcpp {

class ThreadPool {
 public:
  // cpus: one core id per worker (empty = no pinning); overlay: the run's write overlay
  // (every worker's CountingStore shares it; may be null for a plain read run);
  // apply_writes: writes go to the REAL store (the write-latency mode; overlay must be null)
  ThreadPool(Store& store, int P, const std::vector<int>& cpus, int perf_level, bool per_task_perf,
             ShadowOverlay* overlay = nullptr, bool apply_writes = false);
  ~ThreadPool();
  int P() const { return int(slots_.size()); }
  // Runs every task; blocks until all are done. Results are in task-index order.
  std::vector<TaskResult> run(const std::vector<TaskFn>& tasks, size_t batch);

  // The RANGE primitive (executor range refactor): one output Value per index i in
  // [0, n), returned in index order; `body(slot, i)` does src i's work on the worker's store.
  // The pool owns the morsel loop, the counts/clock windows and the ordered collection, so a
  // per-src fan-out pays its overhead per MORSEL (per STEP at P=1) instead of per src -- no
  // std::function, TaskResult, clock pair or counts snapshot per tuple.
  //   P=1: ONE loop over [0, n), ONE counts snapshot before and after, ONE clock pair.
  //   P>1: morsels of `batch` indices off a shared cursor; a worker writes results[i] at its
  //        own indices; each worker's counts delta is summed into `stats` at the end.
  //   per_task_perf (the per-op-latency casestudy): the caller keeps the per-src task path
  //   (StepExec::run_range_map wraps the body into one TaskFn per src), so that mode is
  //   byte-for-byte what it was.
  // The counts are ADDITIVE over srcs, so grouping them per morsel/step cannot move a total;
  // the output order is the index order either way. `tasks`/`critical_ns`/`task_max_ns` and
  // the emit.tasks rows describe morsels here, not srcs (the one accepted change).
  // `landings`: the chunk-reader ops (pertype) log one Landing per probe; the replay consumes
  // them in INDEX order, so the body appends to the morsel's log and the pool concatenates the
  // morsels by start index (a morsel is a contiguous index range walked in order). A
  // direct-count op never touches it.
  using RangeBody = std::function<Value(WorkerSlot&, size_t, std::vector<Landing>& landings)>;
  struct RangeStats {
    Counts physical;            // the step's KV ops + bytes, summed over the workers
    std::vector<Landing> landings;   // every morsel's log, in index order
    uint64_t work_ns = 0;       // sum of morsel walls
    uint64_t wait_ns = 0;       // of which: waiting for a sibling's decode (task_wait_ns)
    uint64_t morsel_max_ns = 0; // the longest morsel (P=1: the whole loop)
    size_t morsels = 0;
    std::vector<TaskStats> morsel_rows;   // one per morsel, only when asked for (emit.tasks)
  };
  std::vector<Value> run_range_map(size_t n, size_t batch, const RangeBody& body, RangeStats& stats,
                                   bool keep_rows);
  // The APPEND form: body(slot, i, out, landings) appends src i's items to `out`, the list of its morsel, so a fan-out
  // whose result is the srcs' items one after another builds no list per src. Returns n lists in index order: a range
  // of one morsel (P=1) appends all to lists[0]; several morsels (P>1) append src i's to lists[i] (see run_range_job).
  // Joined in index order, they are the per-index order. Counts, clocks and landings as run_range_map.
  using AppendBody = std::function<void(WorkerSlot&, size_t, ValueList&, std::vector<Landing>& landings)>;
  std::vector<ValueList> run_range_append(size_t n, size_t batch, const AppendBody& body, RangeStats& stats,
                                          bool keep_rows);
  bool per_task_perf() const { return per_task_perf_; }
  // per-part attribution (store/counting.h PartScheme): every worker's store records parts; the
  // StepExec commits/discards and collects them between jobs (never while a job runs)
  void set_part_scheme(const PartScheme* s) { for (auto& sl : slots_) sl->store.set_part_scheme(s); }
  void set_read_sizes(ReadSizes* r) { for (auto& sl : slots_) sl->store.set_read_sizes(r); }
  void set_kvpair_stats(bool on) { for (auto& sl : slots_) sl->store.set_kvpair_stats(on); }
  void set_round_trips(bool on) { for (auto& sl : slots_) sl->store.set_round_trips(on); }
  WorkerSlot& slot(int i) { return *slots_[size_t(i)]; }
  // The PerfContext/IOStats + codec timers of the last run(), summed over its workers (and
  // cleared for the next run).
  struct JobStats { PerfSnapshot perf; CodecStats codec; uint64_t scan_chunk_bytes_max = 0; };
  JobStats take_job_stats();
  // Stage B sampling: arm/disarm every slot's OpSampler (0 = off; the executor arms only
  // measured iterations); merged histograms of the whole run, ns bucket edges.
  void set_sample_every(uint64_t k);
  json take_op_samples();
  std::vector<uint64_t> busy_ns() const {                              // per worker, cumulative
    std::vector<uint64_t> v;
    for (auto& b : busy_ns_) v.push_back(b.v);
    return v;
  }
  void reset_busy() { for (auto& b : busy_ns_) b.v = 0; }

 private:
  // One job per run(): its own counters, so a worker that wakes late or lags behind can only
  // ever exhaust the job it captured -- never touch the next run's task list.
  struct Job {
    const std::vector<TaskFn>* tasks = nullptr;   // the caller's list; valid only while finished < n
    std::vector<TaskResult>* results = nullptr;
    // a RANGE job instead (run_range_map): body + the index-ordered output; the workers'
    // counts deltas and morsel rows land in per-worker slots, merged by the caller
    const RangeBody* body = nullptr;
    std::vector<Value>* out = nullptr;
    const AppendBody* append = nullptr;   // the append form instead: lists[start] = the morsel's list
    std::vector<ValueList>* lists = nullptr;
    std::vector<Counts>* worker_counts = nullptr;         // one per worker
    std::vector<std::vector<TaskStats>>* worker_rows = nullptr;   // one per worker: a row per morsel
    std::vector<uint64_t>* worker_wait = nullptr;
    struct MorselLog { size_t start; std::vector<Landing> landings; };
    std::vector<std::vector<MorselLog>>* worker_logs = nullptr;   // one per worker: a log per morsel
    // a body that threw: (morsel start, the exception); a worker never throws across the pool --
    // the caller rethrows the earliest one in index order, as StepExec::run does for tasks
    std::vector<std::vector<std::pair<size_t, std::exception_ptr>>>* worker_errors = nullptr;
    size_t n = 0;                        // job-owned, so a late worker never reads the list
    size_t batch = 1;
    std::atomic<size_t> next{0};
    std::atomic<size_t> finished{0};
  };
  void helper(int id);
  void run_job(Job& job, WorkerSlot& slot, int id);
  void run_range_job(Job& job, WorkerSlot& slot, int id);
  void run_job_any(Job& job, WorkerSlot& slot, int id) {
    if (job.body || job.append) run_range_job(job, slot, id); else run_job(job, slot, id);
  }
  // the range job's setup, run and merge, shared by run_range_map and run_range_append (the job's body or append set)
  void run_range(const std::shared_ptr<Job>& job, size_t n, size_t batch, RangeStats& stats, bool keep_rows);
  void pin(int id);
  std::vector<std::unique_ptr<WorkerSlot>> slots_;
  std::vector<std::thread> threads_;
  struct alignas(64) Padded { uint64_t v = 0; };   // one cache line per worker: no false sharing
  std::vector<Padded> busy_ns_;
  std::vector<int> cpus_;
  // the caller's CPU mask before pin(0) pinned it (worker 0 is the calling thread: a server's request thread), put back
  // by the destructor, which runs on that same thread, so a later request without pin_cpus is not left pinned
  cpu_set_t caller_mask_;
  bool restore_mask_ = false;
  int perf_level_;
  bool per_task_perf_;
  std::mutex m_;                        // guards job_ (taken once per job by each helper)
  std::shared_ptr<Job> job_;
  std::atomic<uint64_t> generation_{0};
  std::atomic<bool> stop_{false};
};

}  // namespace evalcpp
