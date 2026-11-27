#include "exec/pool.h"

#include "util/tsc.h"

#include <sched.h>

#include <algorithm>
#include <chrono>
#include <stdexcept>

namespace evalcpp {

using Clock = std::chrono::steady_clock;

static void add_codec(CodecStats& acc, const CodecStats& c) {
  acc.decode_ns += c.decode_ns;
  acc.decompress_ns += c.decompress_ns;
  acc.chunks_decoded += c.chunks_decoded;
  acc.decoded.add(c.decoded);
}

ThreadPool::JobStats ThreadPool::take_job_stats() {
  JobStats js;
  for (auto& s : slots_) {
    js.perf.add(s->perf_acc);
    add_codec(js.codec, s->codec_acc);
    if (s->scan_chunk_bytes_max > js.scan_chunk_bytes_max) js.scan_chunk_bytes_max = s->scan_chunk_bytes_max;
    s->scan_chunk_bytes_max = 0;
    s->perf_acc = PerfSnapshot();
    s->codec_acc.reset();
  }
  return js;
}

ThreadPool::ThreadPool(Store& store, int P, const std::vector<int>& cpus, int perf_level, bool per_task_perf,
                       ShadowOverlay* overlay, bool apply_writes)
    : busy_ns_(size_t(P)), cpus_(cpus), perf_level_(perf_level), per_task_perf_(per_task_perf) {
  if (P < 1) throw std::runtime_error("P must be >= 1");
  if (!cpus.empty() && int(cpus.size()) < P) throw std::runtime_error("pin_cpus lists fewer cores than P");
  for (int i = 0; i < P; i++)
    slots_.push_back(std::make_unique<WorkerSlot>(WorkerSlot{i, CountingStore(store, overlay, apply_writes)}));
  if (!cpus.empty()) restore_mask_ = sched_getaffinity(0, sizeof caller_mask_, &caller_mask_) == 0;
  pin(0);                                  // the caller is worker 0
  for (int i = 1; i < P; i++) threads_.emplace_back([this, i] { helper(i); });
}

ThreadPool::~ThreadPool() {
  stop_.store(true, std::memory_order_release);
  for (auto& t : threads_) t.join();
  if (restore_mask_) sched_setaffinity(0, sizeof caller_mask_, &caller_mask_);   // unpin the caller (worker 0)
}

void ThreadPool::pin(int id) {
  if (cpus_.empty()) return;
  cpu_set_t set;
  CPU_ZERO(&set);
  CPU_SET(cpus_[size_t(id)], &set);
  sched_setaffinity(0, sizeof set, &set);
}

void ThreadPool::set_sample_every(uint64_t k) {
  for (auto& s : slots_) s->store.sampler().every = k;
}

json ThreadPool::take_op_samples() {
  OpSampler all;
  for (auto& s : slots_) {
    all.add(s->store.sampler());
    s->store.sampler() = OpSampler();
  }
  static const char* kNames[OpSampler::kKinds] = {"get", "seek", "seek_for_prev", "next", "prev"};
  double npt = tsc_ns_per_tick();
  json out = json::object();
  for (int k = 0; k < OpSampler::kKinds; k++) {
    if (!all.seen[k]) continue;
    json buckets = json::object();
    uint64_t sampled = 0;
    for (int b = 0; b < OpSampler::kBuckets; b++)
      if (all.buckets[k][b]) {
        buckets[std::to_string(b)] = all.buckets[k][b];   // raw index; edges derive from ns_per_tick
        sampled += all.buckets[k][b];
      }
    out[kNames[k]] = {{"seen", all.seen[k]}, {"sampled", sampled}, {"bucket_idx", buckets}};
  }
  out["ns_per_tick"] = npt;
  return out;
}

void ThreadPool::run_job(Job& job, WorkerSlot& slot, int id) {
  slot.scratch.clear();
  const size_t n = job.n;
  while (true) {
    size_t start = job.next.fetch_add(job.batch);
    if (start >= n) break;              // exhausted (or a late arrival): the list is not touched
    size_t end = std::min(n, start + job.batch);
    const std::vector<TaskFn>& tasks = *job.tasks;
    std::vector<TaskResult>& results = *job.results;
    if (!per_task_perf_) { codec_reset(); slot.store.decodes_begin(); perf_reset(); }   // one window for the batch
    for (size_t i = start; i < end; i++) {
      TaskResult& r = results[i];
      slot.store.parts_discard();         // parts on: a task commits only its own ops (direct_counts)
      Counts before = slot.store.counts();
      if (per_task_perf_) { codec_reset(); slot.store.decodes_begin(); perf_reset(); }
      task_wait_ns() = 0;
      auto t0 = Clock::now();
      try {
        slot.store.rt_unit();                // a task is one unit of the step's round trips (store/round_trips.h)
        tasks[i](slot, r);
      } catch (...) {
        r.error = std::current_exception();   // reported by the caller, never across a thread
      }
      auto t1 = Clock::now();
      r.stats.wall_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count();
      r.stats.wait_ns = task_wait_ns();
      busy_ns_[size_t(id)].v += r.stats.wall_ns;
      r.stats.physical = slot.store.counts().since(before);
      r.stats.slot = id;
      if (per_task_perf_) {
        PerfSnapshot cur = PerfSnapshot::current();
        r.stats.perf = std::make_shared<PerfSnapshot>(cur);
        slot.store.decodes_attribute();     // the window's tail to its part, where the totals are read
        r.stats.codec = codec_snapshot();
        slot.perf_acc.add(cur);
        add_codec(slot.codec_acc, codec_snapshot());
      }
    }
    if (!per_task_perf_) {                 // one window per batch: add it to this worker's job total
      slot.store.decodes_attribute();
      slot.perf_acc.add(PerfSnapshot::current());
      add_codec(slot.codec_acc, codec_snapshot());
    }
    job.finished.fetch_add(end - start, std::memory_order_acq_rel);   // after the totals: the caller reads them
  }
}

void ThreadPool::helper(int id) {
  pin(id);
  set_perf_level(perf_level_);
  WorkerSlot& slot = *slots_[size_t(id)];
  uint64_t seen = 0;
  while (true) {
    while (generation_.load(std::memory_order_acquire) == seen) {   // hot: never sleeps
      if (stop_.load(std::memory_order_acquire)) return;
      __builtin_ia32_pause();
    }
    std::shared_ptr<Job> job;
    {
      std::lock_guard<std::mutex> lk(m_);
      seen = generation_.load(std::memory_order_acquire);
      job = job_;
    }
    if (job) run_job_any(*job, slot, id);
  }
}

// One worker's share of a range job: morsels off the shared cursor; results[i] written at this
// worker's own indices (disjoint, so no lock); ONE counts baseline per worker for the whole job
// (the deltas are additive), one clock pair per morsel (the morsel is the unit of the
// work/critical/max instruments here), the perf/codec window per morsel batch as run_job's.
void ThreadPool::run_range_job(Job& job, WorkerSlot& slot, int id) {
  slot.scratch.clear();
  const size_t n = job.n;
  Counts base = slot.store.counts();
  uint64_t waited = 0;
  while (true) {
    size_t start = job.next.fetch_add(job.batch);
    if (start >= n) break;
    size_t end = std::min(n, start + job.batch);
    codec_reset();
    slot.store.decodes_begin();
    perf_reset();
    task_wait_ns() = 0;
    std::vector<Landing> log;                        // this morsel's landings, in index order
    Counts morsel_base = slot.store.counts();        // the row's own delta (base = the worker's total)
    auto t0 = Clock::now();
    try {
      if (job.append && job.batch >= n) {            // one morsel (P=1): one list for the range, at index 0
        ValueList acc;
        for (size_t i = start; i < end; i++) {
          slot.store.rt_unit();
          (*job.append)(slot, i, acc, log);
        }
        (*job.lists)[start] = std::move(acc);
      } else if (job.append) {
        // several morsels (P>1): a list per index, as small as one src's items. A morsel's one list grows to hundreds
        // of KB, past tcmalloc's per-thread caches, and 8 workers growing such lists at once wait on its page heap's
        // lock (P=8 plans were 12-20% slower with it)
        for (size_t i = start; i < end; i++) {
          slot.store.rt_unit();
          (*job.append)(slot, i, (*job.lists)[i], log);
        }
      } else {
        for (size_t i = start; i < end; i++) {
          slot.store.rt_unit();                      // each source is one unit of the step's round trips
          (*job.out)[i] = (*job.body)(slot, i, log);
        }
      }
    } catch (...) {
      (*job.worker_errors)[size_t(id)].emplace_back(start, std::current_exception());
      // abandon the rest: claim [next, n) for nobody and count it finished, so the caller's
      // wait (finished == n) still completes; morsels already claimed finish on their own
      size_t rest = job.next.exchange(n, std::memory_order_acq_rel);
      if (rest < n) job.finished.fetch_add(n - rest, std::memory_order_acq_rel);
    }
    auto t1 = Clock::now();
    if (!log.empty()) (*job.worker_logs)[size_t(id)].push_back({start, std::move(log)});
    uint64_t wall = uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count());
    busy_ns_[size_t(id)].v += wall;
    waited += task_wait_ns();
    TaskStats ts;                                    // one row per morsel (the instruments' unit)
    ts.wall_ns = wall;
    ts.wait_ns = task_wait_ns();
    ts.slot = id;
    ts.physical = slot.store.counts().since(morsel_base);   // this morsel's counts (emit.tasks rows)
    (*job.worker_rows)[size_t(id)].push_back(std::move(ts));
    slot.perf_acc.add(PerfSnapshot::current());
    slot.store.decodes_attribute();
    add_codec(slot.codec_acc, codec_snapshot());
    // this worker's totals so far, written BEFORE the finished signal: the caller returns (and
    // its per-worker vectors die) the moment finished reaches n, so nothing may be written
    // after that add -- a late worker that takes no morsel writes nothing at all
    (*job.worker_counts)[size_t(id)] = slot.store.counts().since(base);
    (*job.worker_wait)[size_t(id)] = waited;
    job.finished.fetch_add(end - start, std::memory_order_acq_rel);
  }
}

std::vector<Value> ThreadPool::run_range_map(size_t n, size_t batch, const RangeBody& body, RangeStats& stats,
                                             bool keep_rows) {
  std::vector<Value> out(n);
  if (n == 0) return out;
  auto job = std::make_shared<Job>();
  job->body = &body;
  job->out = &out;
  run_range(job, n, batch, stats, keep_rows);
  return out;
}

std::vector<ValueList> ThreadPool::run_range_append(size_t n, size_t batch, const AppendBody& body, RangeStats& stats,
                                                    bool keep_rows) {
  std::vector<ValueList> lists(n);                   // a morsel's list at its start index; the other indices stay empty
  if (n == 0) return lists;
  auto job = std::make_shared<Job>();
  job->append = &body;
  job->lists = &lists;
  run_range(job, n, batch, stats, keep_rows);
  return lists;
}

void ThreadPool::run_range(const std::shared_ptr<Job>& job, size_t n, size_t batch, RangeStats& stats, bool keep_rows) {
  const size_t P = slots_.size();
  std::vector<Counts> worker_counts(P);
  std::vector<uint64_t> worker_wait(P, 0);
  std::vector<std::vector<TaskStats>> worker_rows(P);
  std::vector<std::vector<Job::MorselLog>> worker_logs(P);
  std::vector<std::vector<std::pair<size_t, std::exception_ptr>>> worker_errors(P);
  job->worker_logs = &worker_logs;
  job->worker_errors = &worker_errors;
  job->worker_counts = &worker_counts;
  job->worker_rows = &worker_rows;
  job->worker_wait = &worker_wait;
  job->n = n;
  if (threads_.empty() || n <= batch) {
    // P=1, or a range that fits one morsel: the caller does it alone -- one loop, one counts
    // window, one clock pair, no generation bump (the helpers are never woken for nothing;
    // the classic run() shortcuts the same way)
    job->batch = n;
    run_range_job(*job, *slots_[0], 0);
  } else {
    job->batch = batch < 1 ? 1 : batch;
    {
      std::lock_guard<std::mutex> lk(m_);
      job_ = job;
      generation_.fetch_add(1, std::memory_order_acq_rel);
    }
    run_range_job(*job, *slots_[0], 0);          // the caller works too
    while (job->finished.load(std::memory_order_acquire) < n) __builtin_ia32_pause();
  }
  // every worker is done with `out` and the logs now; the earliest failing morsel (by index) wins
  std::pair<size_t, std::exception_ptr> first{n, nullptr};
  for (auto& we : worker_errors)
    for (auto& e : we)
      if (e.first < first.first) first = e;
  if (first.second) std::rethrow_exception(first.second);
  for (size_t w = 0; w < P; w++) {
    stats.physical.add(worker_counts[w]);
    stats.wait_ns += worker_wait[w];
    for (auto& ts : worker_rows[w]) {
      stats.work_ns += ts.wall_ns;
      if (ts.wall_ns > stats.morsel_max_ns) stats.morsel_max_ns = ts.wall_ns;
      stats.morsels++;
      if (keep_rows) stats.morsel_rows.push_back(ts);
    }
  }
  // the landings in index order: morsels are contiguous index ranges, so ordering them by
  // start and concatenating IS the per-index order the replay expects (P=1: one morsel)
  std::vector<Job::MorselLog*> logs;
  for (auto& wl : worker_logs)
    for (auto& ml : wl) logs.push_back(&ml);
  std::sort(logs.begin(), logs.end(), [](const Job::MorselLog* a, const Job::MorselLog* b) { return a->start < b->start; });
  for (Job::MorselLog* ml : logs)
    stats.landings.insert(stats.landings.end(), std::make_move_iterator(ml->landings.begin()),
                          std::make_move_iterator(ml->landings.end()));
}

std::vector<TaskResult> ThreadPool::run(const std::vector<TaskFn>& tasks, size_t batch) {
  std::vector<TaskResult> results(tasks.size());
  if (tasks.empty()) return results;
  auto job = std::make_shared<Job>();
  job->tasks = &tasks;
  job->results = &results;
  job->n = tasks.size();
  job->batch = batch < 1 ? 1 : batch;
  if (threads_.empty() || tasks.size() <= job->batch) {
    // P=1, or a fan-out that fits one batch (every flow op, index walk, anchor read): the
    // caller does it itself -- no thread hand-off on the clock at any P
    run_job(*job, *slots_[0], 0);
    return results;
  }
  {
    std::lock_guard<std::mutex> lk(m_);
    job_ = job;
    generation_.fetch_add(1, std::memory_order_acq_rel);
  }
  run_job(*job, *slots_[0], 0);            // the caller works too
  while (job->finished.load(std::memory_order_acquire) < tasks.size()) __builtin_ia32_pause();
  return results;
}

}  // namespace evalcpp
