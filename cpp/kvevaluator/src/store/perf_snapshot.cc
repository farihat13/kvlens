#include "store/perf_snapshot.h"

#include <chrono>

#include <rocksdb/iostats_context.h>
#include <rocksdb/perf_context.h>

namespace evalcpp {

KvCallTimes& kv_call_times() {
  thread_local KvCallTimes t;
  return t;
}

std::atomic<bool>& kv_timing_on() {
  static std::atomic<bool> on{false};
  return on;
}

uint64_t kv_timer_pair_ns() {
  static const uint64_t pair = [] {
    using C = std::chrono::steady_clock;
    const int n = 200000;
    uint64_t sink = 0;
    auto t0 = C::now();
    for (int i = 0; i < n; i++) { auto a = C::now(); auto b = C::now(); sink += uint64_t((b - a).count()); }
    auto t1 = C::now();
    (void)sink;
    return uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count()) / n;
  }();
  return pair;
}

PerfSnapshot PerfSnapshot::current() {
  PerfSnapshot s;
  const rocksdb::PerfContext* p = rocksdb::get_perf_context();
  const rocksdb::IOStatsContext* io = rocksdb::get_iostats_context();
#define EVALCPP_COPY(f) s.f = p->f;
  EVALCPP_PERF_FIELDS(EVALCPP_COPY)
#undef EVALCPP_COPY
#define EVALCPP_COPY_IO(f) s.io_##f = io->f;
  EVALCPP_IOSTATS_FIELDS(EVALCPP_COPY_IO)
#undef EVALCPP_COPY_IO
  const KvCallTimes& k = kv_call_times();
#define EVALCPP_COPY_KV(f) s.f = k.f;
  EVALCPP_KVCALL_FIELDS(EVALCPP_COPY_KV)
#undef EVALCPP_COPY_KV
  return s;
}

void PerfSnapshot::add(const PerfSnapshot& o) {
#define EVALCPP_ADD(f) f += o.f;
  EVALCPP_PERF_FIELDS(EVALCPP_ADD)
  EVALCPP_KVCALL_FIELDS(EVALCPP_ADD)
#undef EVALCPP_ADD
#define EVALCPP_ADD_IO(f) io_##f += o.io_##f;
  EVALCPP_IOSTATS_FIELDS(EVALCPP_ADD_IO)
#undef EVALCPP_ADD_IO
}

json PerfSnapshot::to_json() const {
  json j = json::object();
#define EVALCPP_OUT(f) if (f) j[#f] = f;
  EVALCPP_PERF_FIELDS(EVALCPP_OUT)
  EVALCPP_KVCALL_FIELDS(EVALCPP_OUT)
#undef EVALCPP_OUT
#define EVALCPP_OUT_IO(f) if (io_##f) j["io_" #f] = io_##f;
  EVALCPP_IOSTATS_FIELDS(EVALCPP_OUT_IO)
#undef EVALCPP_OUT_IO
  return j;
}

}  // namespace evalcpp
