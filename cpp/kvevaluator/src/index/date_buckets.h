// The GRANULARITY of a date index term: `full` = one term per distinct timestamp (ms), exact by
// construction; `day` = one term per day (days since the epoch, Python floor semantics), a coarse
// pre-filter whose boundary days the plan re-checks with its precise date filter (dgraph's
// @index(day) mechanism, scoped per (type, prop) by the index id). A bucket is a FIXED-WIDTH
// order-preserving int in the term key (IndexKeys::sorted_bytes), so a range walk seeks the lo
// bucket and stops at the hi one; nothing about the key shape changes between granularities.
// Named and pluggable: a third granularity (month, year) is one more enum value + bucket_of case.
#pragma once
#include <cstdint>
#include <string>

namespace evalcpp {

enum class DateGranularity { full, day };

DateGranularity parse_date_granularity(const std::string& name);   // "full" | "day"; throws otherwise
const char* date_granularity_name(DateGranularity g);

constexpr int64_t kDayMs = 86400000;

// the bucket a timestamp falls in (ms under `full`; floor(ms / day) under `day`, -1 ms = day -1)
int64_t date_bucket_of(int64_t ms, DateGranularity g);
// the inclusive bucket range a [lo, hi) ms window covers: [bucket(lo), bucket(hi - 1)]; an empty
// window (hi <= lo) gives first > last, first still = bucket(lo) (the walk's seek target)
std::pair<int64_t, int64_t> date_bucket_window(int64_t lo, int64_t hi, DateGranularity g);

}  // namespace evalcpp
