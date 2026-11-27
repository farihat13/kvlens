// The unit of parallel work. A KV op splits itself into independent tasks; the pool runs
// them on P workers; results merge in task-index order (rows identical at every P); the
// logical S/G/N come from replaying the P=1 chunk-reader rule over the tasks' landing
// records (plan 4.5), the physical counts from what the worker threads actually did.
#pragma once
#include <cstdint>
#include <exception>
#include <functional>
#include <memory>
#include <unordered_map>
#include <string>
#include <vector>

#include "codec/timers.h"
#include "store/counting.h"
#include "store/perf_snapshot.h"
#include "value/value.h"

namespace evalcpp {

// One chunk probe as the STORE answers it, independent of who probed: the chunk at or
// before `probe` under `prefix` (or none). The replay decides what P=1 would have paid.
struct Landing {
  uint64_t group;        // one Python-level reader (op call): the replay keeps a cache per group
  std::string prefix;
  uint64_t probe;
  bool landed;           // a chunk key of this prefix is at or before the probe
  uint64_t first, lo, hi;
  uint32_t chunk_bytes;  // the chunk value's length (charged once per group at P=1)
  bool key_valid;        // seek_for_prev landed on some key (of any prefix)
  uint32_t key_bytes;    // that key's length (charged on every landing at P=1)
  uint64_t cache = 0;    // the shared chunk cache it probed (SharedChunkCache's id; 0: none, e.g. a predicted landing)
};

// A chunk parsed into a shared chunk cache (exec/chunk_cache.h), as its owner recorded it: which thread owns a chunk
// is a race, so its decode counts are kept off that thread's part window and credited after the replay, to the part
// of the first landing on the chunk in task order -- the landing the replay charges its value to (replay.h,
// credit_chunk_parses).
struct ChunkParse {
  uint64_t cache;        // SharedChunkCache's id
  std::string prefix;
  uint64_t first;        // the chunk's first id (its key's)
  DecodeCounts decoded;
};

struct TaskStats {
  uint64_t wall_ns = 0;
  uint64_t wait_ns = 0;  // spent waiting for a sibling's chunk decode (inside wall_ns)
  Counts physical;       // this task's own KV ops (its worker's counting store)
  Counts direct;         // counts that ARE the logical ones (index walks: no order dependence)
  int slot = -1;
  // PerfContext/IOStats + codec timers are accumulated per WORKER for the job (WorkerSlot);
  // a per-task copy exists only when the run asks for task rows (emit.tasks) -- a full
  // PerfSnapshot is ~1 KB, and a fan-out can have millions of tasks
  std::shared_ptr<PerfSnapshot> perf;
  CodecStats codec;
};

struct TaskResult {
  Value out;
  std::vector<Landing> landings;
  TaskStats stats;
  std::exception_ptr error;   // a task never throws across the pool: the caller rethrows this
};
// TaskStats holds two Counts (physical, direct): every uint64_t field added to Counts costs
// 8 B twice, 16 B here.
// 496 = the bound with the two audit tallies on Counts (raw_gets, rocks_write_bytes:
// +16 B per Counts, two Counts here); 464 was the six-miss-lane bound with 8 B of slack.
// 552 = 496 + the 56 B of the decode counters in CodecStats (codec/decode_counts.h: seven uint64_t).
// 680 = 552 + 128: the last-chunk lane on Counts (raw_seeks and the seven LastChunk fields: +64 B per
// Counts, two Counts here).
static_assert(sizeof(TaskResult) <= 680, "TaskResult is allocated once per task: keep it small");

// this thread's running wait total (a task's reader adds to it; the pool reads it per task)
uint64_t& task_wait_ns();

struct WorkerSlot {
  int id;
  CountingStore store;
  // per-job scratch a format may keep across the tasks it runs on this worker (e.g. a private
  // mirror of a shared cache); cleared by the pool at the start of every job
  std::unordered_map<const void*, std::shared_ptr<void>> scratch;
  // the engine + codec timers of every batch this worker ran for the current job; the pool
  // sums them per job (ThreadPool::take_job_stats)
  PerfSnapshot perf_acc;
  CodecStats codec_acc;
  // the largest chunk value a fused scan on this worker held during the current job (a scan decodes chunks off its
  // cursor, one at a time, outside the chunk cache); the pool takes the maximum per job
  uint64_t scan_chunk_bytes_max = 0;
  // parts on: the chunks this worker parsed into a shared chunk cache in the current job (StepExec collects them)
  std::vector<ChunkParse> parses;
};

using TaskFn = std::function<void(WorkerSlot&, TaskResult&)>;

}  // namespace evalcpp
