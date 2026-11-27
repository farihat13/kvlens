#include "exec/step_exec.h"

namespace evalcpp {
uint64_t& task_wait_ns() {
  thread_local uint64_t w = 0;
  return w;
}
}  // namespace evalcpp

namespace evalcpp {

// The used lanes (what the op asked for of what it read) are the operator's own counts, never re-derived: they do not
// depend on the chunk cache or on P, so a replayed chunk reader keeps them although its other counts are dropped
// for the replay's. `direct`'s used lanes are set to the job's physical ones, on every path.
static Counts with_used_of(Counts d, const Counts& physical) {
  d.used_key = physical.used_key;
  d.used_value = physical.used_value;
  d.used_write_key = physical.used_write_key;
  d.used_write_value = physical.used_write_value;
  return d;
}

// between jobs (main thread, no worker running): each worker's pooled iterators are deleted (CountingStore::end_step),
// inside the step's clock -- so no iterator outlives the job that made it, and its deletes stay in the step's wall
void StepExec::end_job() {
  for (int i = 0; i < pool_.P(); i++) pool_.slot(i).store.end_step();
}

void StepExec::collect_parts() {
  RtLevels job;                               // one job's requests, the workers' summed level by level
  for (int i = 0; i < pool_.P(); i++) {
    CountingStore& st = pool_.slot(i).store;
    st.take_committed_round_trips(job);
    remote_wait_ns += st.take_remote_wait_ns();
    st.take_committed_hists(direct_hists);    // with or without a part scheme
    st.take_decode_parts(decode_parts);       // parts on: what each part's reads decoded (physical, never discarded)
    std::vector<ChunkParse>& ps = pool_.slot(i).parses;   // ... and the shared chunks this worker parsed
    chunk_parses.insert(chunk_parses.end(), std::make_move_iterator(ps.begin()), std::make_move_iterator(ps.end()));
    ps.clear();
    st.take_committed_parts(direct_parts);
    st.parts_discard();                       // what no task committed: physical-only (replayed) ops
  }
  direct_round_trips.add(job.totals());
  direct_rt_text.add(job.text());
  if (remote().mode == Remote::kOperation) {  // a job's writes are one batch: one wait; and the bandwidth's rest
    uint64_t owed = job.writes ? remote().delay_ns : 0;
    for (int i = 0; i < pool_.P(); i++) owed += pool_.slot(i).store.take_remote_owed_ns();
    remote_wait_ns += remote_sleep(owed);
  }
}

std::vector<TaskResult> StepExec::run(const std::vector<TaskFn>& tasks) {
  collect_parts();                            // a clean pending on every worker before the job
  std::vector<TaskResult> results = pool_.run(tasks, batch_);
  end_job();
  collect_parts();                            // (before a rethrow too: nothing half-collected lingers)
  for (auto& r : results)                     // the first failing task, in task order
    if (r.error) std::rethrow_exception(r.error);
  uint64_t mx = 0;
  for (auto& r : results) {
    landings_.insert(landings_.end(), std::make_move_iterator(r.landings.begin()), std::make_move_iterator(r.landings.end()));
    r.landings.clear();
    physical.add(r.stats.physical);
    direct.add(with_used_of(r.stats.direct, r.stats.physical));
    work_ns += r.stats.wall_ns;
    wait_ns += r.stats.wait_ns;
    if (r.stats.wall_ns > mx) mx = r.stats.wall_ns;
    if (keep_rows_) task_rows_.push_back(r.stats);
  }
  ThreadPool::JobStats js = pool_.take_job_stats();
  perf.add(js.perf);
  codec.decode_ns += js.codec.decode_ns;
  codec.decompress_ns += js.codec.decompress_ns;
  codec.chunks_decoded += js.codec.chunks_decoded;
  codec.decoded.add(js.codec.decoded);
  if (js.scan_chunk_bytes_max > scan_chunk_bytes_max) scan_chunk_bytes_max = js.scan_chunk_bytes_max;
  ntasks += results.size();
  critical_ns += mx;
  if (mx > task_max_ns) task_max_ns = mx;
  return results;
}

std::vector<Value> StepExec::run_range_map(size_t n, const ThreadPool::RangeBody& body, bool additive_counts) {
  if (pool_.per_task_perf()) {
    // the casestudy path: one task per src, exactly the pre-refactor fan-out
    std::vector<TaskFn> tasks;
    tasks.reserve(n);
    for (size_t i = 0; i < n; i++)
      tasks.push_back([&, i](WorkerSlot& slot, TaskResult& r) {
        Counts before = slot.store.counts();
        r.out = body(slot, i, r.landings);
        if (additive_counts) {
          r.stats.direct = slot.store.counts().since(before);
          slot.store.parts_commit(&r.stats.direct);
        }
      });
    std::vector<TaskResult> res = run(tasks);
    std::vector<Value> out;
    out.reserve(n);
    for (auto& r : res) out.push_back(std::move(r.out));
    return out;
  }
  ThreadPool::RangeStats st;
  collect_parts();                            // a clean pending on every worker before the job
  std::vector<Value> out;
  try {
    out = pool_.run_range_map(n, batch_, body, st, keep_rows_);
  } catch (...) {
    end_job();
    collect_parts();
    throw;
  }
  fold_range(st, additive_counts);
  return out;
}

Value StepExec::run_range_append(size_t n, const ThreadPool::AppendBody& body, bool additive_counts) {
  if (pool_.per_task_perf()) {
    // the casestudy path: one task per src (its own list), joined in index order
    std::vector<Value> per_src = run_range_map(n, [&](WorkerSlot& slot, size_t i, std::vector<Landing>& log) {
      ValueList items;
      body(slot, i, items, log);
      return Value::list(std::move(items));
    }, additive_counts);
    ValueList out;
    for (auto& v : per_src) {
      ValueList& items = const_cast<ValueList&>(v.as_list());   // made just above, held by per_src alone
      for (auto& x : items) out.push_back(std::move(x));
    }
    return Value::list(std::move(out));
  }
  ThreadPool::RangeStats st;
  collect_parts();
  std::vector<ValueList> lists;
  try {
    lists = pool_.run_range_append(n, batch_, body, st, keep_rows_);
  } catch (...) {
    end_job();
    collect_parts();
    throw;
  }
  fold_range(st, additive_counts);
  // the morsels' lists in index order; one morsel (P=1) is the output as it is
  size_t total = 0, nonempty = 0;
  for (auto& l : lists) { total += l.size(); nonempty += !l.empty(); }
  if (nonempty <= 1)
    for (auto& l : lists)
      if (!l.empty()) return Value::list(std::move(l));
  ValueList out;
  out.reserve(total);
  for (auto& l : lists)
    for (auto& x : l) out.push_back(std::move(x));
  return Value::list(std::move(out));
}

void StepExec::fold_range(ThreadPool::RangeStats& st, bool additive_counts) {
  end_job();
  // every worker's pending holds exactly this job's ops: logical for an additive op (commit),
  // physical-only for a replayed chunk reader (dropped; the replay attributes its landings)
  if (additive_counts)
    for (int i = 0; i < pool_.P(); i++) pool_.slot(i).store.parts_commit();
  collect_parts();
  landings_.insert(landings_.end(), std::make_move_iterator(st.landings.begin()), std::make_move_iterator(st.landings.end()));
  physical.add(st.physical);
  direct.add(with_used_of(additive_counts ? st.physical : Counts(), st.physical));
  work_ns += st.work_ns;
  wait_ns += st.wait_ns;
  critical_ns += st.morsel_max_ns;
  if (st.morsel_max_ns > task_max_ns) task_max_ns = st.morsel_max_ns;
  ntasks += st.morsels;
  ThreadPool::JobStats js = pool_.take_job_stats();
  perf.add(js.perf);
  codec.decode_ns += js.codec.decode_ns;
  codec.decompress_ns += js.codec.decompress_ns;
  codec.chunks_decoded += js.codec.chunks_decoded;
  codec.decoded.add(js.codec.decoded);
  if (js.scan_chunk_bytes_max > scan_chunk_bytes_max) scan_chunk_bytes_max = js.scan_chunk_bytes_max;
  if (keep_rows_) task_rows_.insert(task_rows_.end(), st.morsel_rows.begin(), st.morsel_rows.end());
}

}  // namespace evalcpp
