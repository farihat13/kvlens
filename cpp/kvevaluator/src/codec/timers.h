// Per-thread codec timers: what the chunk decoders cost, reported per step next to the
// RocksDB timers (decode = our parsing; decompress = a codec-level compressor, if any).
#pragma once
#include <atomic>
#include <chrono>
#include <cstdint>

#include "codec/decode_counts.h"
#include "value/value.h"

namespace evalcpp {

struct CodecStats {
  uint64_t decode_ns = 0;       // all time inside the chunk decoders (INCLUDES decompress_ns)
  uint64_t decompress_ns = 0;   // the part spent in a codec-level decompressor (zstd_column)
  uint64_t chunks_decoded = 0;
  // what was decoded (codec/decode_counts.h): filled from the thread's decode counters when a window is read
  // (codec_snapshot); deterministic, unlike the two clocks above
  DecodeCounts decoded;
  void reset() { *this = CodecStats(); }
  json to_json() const {
    json kinds = json::object();     // sparse: a kind that decoded nothing is not written
    if (decoded.rows) kinds["row"] = decoded.rows;
    if (decoded.cells) kinds["cell"] = decoded.cells;
    if (decoded.postings) kinds["posting"] = decoded.postings;
    if (decoded.chunks) kinds["chunk"] = decoded.chunks;
    if (decoded.docs) kinds["doc"] = decoded.docs;
    return {{"decode_ns", decode_ns}, {"decompress_ns", decompress_ns}, {"chunks_decoded", chunks_decoded},
            {"decode_requests", kinds}, {"fields_decoded", decoded.fields}, {"decoded_kvbytes", decoded.bytes}};
  }
};

CodecStats& codec_stats();   // this thread's counters (the clocks and chunks_decoded)
// this thread's whole window: the clocks plus the decode counters; and the start of a new window
inline CodecStats codec_snapshot() { CodecStats s = codec_stats(); s.decoded = decode_counts(); return s; }
inline void codec_reset() { codec_stats().reset(); decode_counts().reset(); }

// process-wide: execute sets it for its run (run.decode_timing, or run.kv_timing), every worker reads it. Off, the
// codec clocks take no time (a clock pair per cell was ~12% of a Chunked scan's cycles) and report null
std::atomic<bool>& decode_timing_on();

// Adds the enclosed scope's wall time to codec_stats().decode_ns, when the run asked for the codec clocks.
struct DecodeTimer {
  bool on = decode_timing_on().load(std::memory_order_relaxed);
  std::chrono::steady_clock::time_point t0 = on ? std::chrono::steady_clock::now() : std::chrono::steady_clock::time_point();
  ~DecodeTimer() {
    if (on) codec_stats().decode_ns += std::chrono::duration_cast<std::chrono::nanoseconds>(
                                           std::chrono::steady_clock::now() - t0).count();
  }
};

}  // namespace evalcpp
