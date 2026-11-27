// rdtsc sampling clock (perop_latency Stage B). The TSC is the only clock cheap enough to
// wrap a ~50 ns NEXT without drowning it (~5-8 ns per read vs ~20-30 for steady_clock);
// invariant-TSC hardware (everything we run on) makes it a wall clock. DIAGNOSTIC only --
// sampled per-op histograms, never the headline latency currency.
#pragma once
#include <cstdint>
#include <x86intrin.h>

namespace evalcpp {

inline uint64_t rdtsc() { return __rdtsc(); }
// ns per TSC tick, calibrated once per process against steady_clock (~20 ms busy-wait)
double tsc_ns_per_tick();

}  // namespace evalcpp
