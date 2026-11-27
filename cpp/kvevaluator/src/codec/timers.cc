#include "codec/timers.h"

namespace evalcpp {
CodecStats& codec_stats() {
  thread_local CodecStats s;
  return s;
}
std::atomic<bool>& decode_timing_on() {
  static std::atomic<bool> on{false};
  return on;
}
}  // namespace evalcpp
