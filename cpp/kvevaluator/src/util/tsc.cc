#include "util/tsc.h"

#include <chrono>

namespace evalcpp {

// NB: steady_clock is itself TSC-backed on this host (clocksource=tsc), so this recovers
// the kernel's tsc_khz rather than measuring independently -- accurate, but its perfect
// reproducibility across runs is NOT an independent cross-check.
double tsc_ns_per_tick() {
  static const double r = [] {
    using C = std::chrono::steady_clock;
    auto c0 = C::now();
    uint64_t t0 = rdtsc();
    while (std::chrono::duration_cast<std::chrono::milliseconds>(C::now() - c0).count() < 20) {}
    auto c1 = C::now();
    uint64_t t1 = rdtsc();
    return double(std::chrono::duration_cast<std::chrono::nanoseconds>(c1 - c0).count()) / double(t1 - t0);
  }();
  return r;
}

}  // namespace evalcpp
