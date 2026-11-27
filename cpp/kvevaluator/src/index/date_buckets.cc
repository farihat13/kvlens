#include "index/date_buckets.h"

#include <climits>
#include <stdexcept>
#include <utility>

namespace evalcpp {

DateGranularity parse_date_granularity(const std::string& name) {
  if (name == "full") return DateGranularity::full;
  if (name == "day") return DateGranularity::day;
  throw std::runtime_error("date index granularity '" + name + "': expected full or day");
}

const char* date_granularity_name(DateGranularity g) { return g == DateGranularity::day ? "day" : "full"; }

int64_t date_bucket_of(int64_t ms, DateGranularity g) {
  if (g == DateGranularity::full) return ms;
  // Python floor division: -1 ms is day -1, not day 0 (dgraph's day_token rule); written
  // without negating ms, so INT64_MIN is not UB
  int64_t q = ms / kDayMs, r = ms % kDayMs;
  return r < 0 ? q - 1 : q;
}

std::pair<int64_t, int64_t> date_bucket_window(int64_t lo, int64_t hi, DateGranularity g) {
  int64_t first = date_bucket_of(lo, g);
  // an empty window: no bucket qualifies, but the walk still SEEKS at bucket(lo) (the
  // pre-knob code's landing under full); INT64_MIN cannot be lowered, that one term qualifies
  if (hi <= lo) return {first, first == INT64_MIN ? INT64_MIN : first - 1};
  return {first, date_bucket_of(hi - 1, g)};
}

}  // namespace evalcpp
