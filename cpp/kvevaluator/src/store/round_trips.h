// Round trips: what a step's requests would cost a client whose key-value store is remote. Counted beside the
// operations; no operation count or byte lane depends on it.
//   a request      one GET, one range read (a cursor's life) or one further page of a range read (every
//                  page_bytes of keys + values it returned after the first)
//   a level        the requests of a step that can be sent together: their keys are known before any of them is
//                  answered. A job's units (one per source of a fan-out) run level by level: level k of every unit
//                  is sent at once. Inside a unit each request is taken to need the one before it (level + 1),
//                  unless the operator says otherwise with RtParallel (store/counting.h).
//   round_trips    per level: 1 for all its GETs (one batch) + 1 per range read or page (they go side by side, each
//                  its own request); + 1 for a job's writes (one write batch)
//   critical       the chain a client must wait through: 1 per level that holds a request, + 1 for a job's writes
// A step's numbers are the sums over its jobs (a later job reads what an earlier one returned).
#pragma once
#include <cstdint>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace evalcpp {

inline uint64_t& rt_page_bytes() {          // the page of a range read (run.remote.page_bytes); one run at a time
  static uint64_t v = uint64_t(1) << 20;
  return v;
}
inline uint64_t rt_pages_after_first(uint64_t bytes) { return bytes ? (bytes - 1) / rt_page_bytes() : 0; }

// The remote regime (opt-in, run.remote.delay_us > 0): a real wait per round trip, so a latency run times the plan as
// against a store one network hop away. The wait is a sleep, never a spin (parallel requests must overlap). Modes --
// where the network boundary sits:
//   operation   every request waits where it is made, in the worker that makes it (+ one wait per job that writes)
//   batch       a step waits round_trips_critical x delay, once its requests are done (a client that sends a level at once)
//   step        a step with a request waits one delay (the storage operator runs beside the store)
// ns_per_byte (run.remote.bandwidth_mbps): the fetched keys + values also wait bytes / bandwidth. Off (the default):
// one untaken branch per request, no count and no clock moves. The counts are the same in every mode.
struct Remote {
  enum Mode { kOff = 0, kOperation, kBatch, kStep };
  Mode mode = kOff;
  uint64_t delay_ns = 0;
  double ns_per_byte = 0;
};
inline Remote& remote() {                   // one run at a time, as the page
  static Remote r;
  return r;
}
uint64_t remote_sleep(uint64_t ns);         // sleeps, returns the time it took (store/counting.cc)

struct RtTotals {
  uint64_t round_trips = 0, critical = 0, pages_extra = 0;
  void add(const RtTotals& o) { round_trips += o.round_trips; critical += o.critical; pages_extra += o.pages_extra; }
  bool operator==(const RtTotals& o) const {
    return round_trips == o.round_trips && critical == o.critical && pages_extra == o.pages_extra;
  }
};

// one job's requests by level (one per worker while the job runs; summed over the workers after it)
struct RtLevels {
  struct Level { uint64_t gets = 0, ranges = 0; };
  std::vector<Level> lv;
  uint64_t writes = 0, pages_extra = 0;
  bool empty() const { return lv.empty() && !writes; }
  void clear() { lv.clear(); writes = 0; pages_extra = 0; }
  Level& at(size_t k) { if (k >= lv.size()) lv.resize(k + 1); return lv[k]; }
  void add(const RtLevels& o) {
    if (lv.size() < o.lv.size()) lv.resize(o.lv.size());
    for (size_t k = 0; k < o.lv.size(); k++) { lv[k].gets += o.lv[k].gets; lv[k].ranges += o.lv[k].ranges; }
    writes += o.writes; pages_extra += o.pages_extra;
  }
  RtTotals totals() const {
    RtTotals t;
    for (auto& l : lv) {
      if (!l.gets && !l.ranges) continue;
      t.round_trips += (l.gets ? 1 : 0) + l.ranges;
      t.critical++;
    }
    if (writes) { t.round_trips++; t.critical++; }
    t.pages_extra = pages_extra;
    return t;
  }
  // the job as text (a piece of the step column rt_levels): its levels that hold a request, in order, as
  // "gets:ranges" (a range read's further pages are ranges of later levels), a run of n equal levels as
  // "gets:rangesxn"; then "wN" for N write keys. "" when the job holds no request
  std::string text() const {
    std::string out;
    auto put = [&](const Level& l, uint64_t n) {
      if (!out.empty()) out += ' ';
      out += std::to_string(l.gets) + ':' + std::to_string(l.ranges);
      if (n > 1) out += 'x' + std::to_string(n);
    };
    Level run{};
    uint64_t n = 0;
    for (auto& l : lv) {
      if (!l.gets && !l.ranges) continue;
      if (n && l.gets == run.gets && l.ranges == run.ranges) { n++; continue; }
      if (n) put(run, n);
      run = l;
      n = 1;
    }
    if (n) put(run, n);
    if (writes) out += (out.empty() ? "w" : " w") + std::to_string(writes);
    return out;
  }
};

// a step's levels as text, the step column rt_levels: its jobs in order (the landing replay's first, then the direct
// jobs), "|" between them, a run of n equal jobs as "<job>*n". A reader re-sums round_trips and round_trips_critical
// from it (RtLevels::totals per job); a client with a batch size and an in-flight cap is costed from it level by level
struct RtText {
  std::vector<std::pair<std::string, uint64_t>> jobs;
  void add(const std::string& t, uint64_t n = 1) {
    if (t.empty() || !n) return;
    if (!jobs.empty() && jobs.back().first == t) jobs.back().second += n;
    else jobs.emplace_back(t, n);
  }
  void add(const RtText& o) { for (auto& [t, n] : o.jobs) add(t, n); }
  std::string str() const {
    std::string out;
    for (auto& [t, n] : jobs) {
      if (!out.empty()) out += '|';
      out += t;
      if (n > 1) out += '*' + std::to_string(n);
    }
    return out;
  }
};

// a number of an rt_levels text at i (advanced past it); throws when none is there
inline uint64_t rt_text_num(const std::string& text, size_t& i) {
  if (i >= text.size() || text[i] < '0' || text[i] > '9')
    throw std::runtime_error("rt_levels: a number expected at " + std::to_string(i) + " in '" + text + "'");
  uint64_t v = 0;
  while (i < text.size() && text[i] >= '0' && text[i] <= '9') v = v * 10 + uint64_t(text[i++] - '0');
  return v;
}

// one level token at i, "gets:ranges" or "gets:rangesxN", added into a job's totals; throws on a malformed one
inline void rt_text_level(const std::string& text, size_t& i, RtTotals& job) {
  uint64_t gets = rt_text_num(text, i);
  if (i >= text.size() || text[i] != ':') throw std::runtime_error("rt_levels: ':' expected in '" + text + "'");
  i++;
  uint64_t ranges = rt_text_num(text, i), n = 1;
  if (i < text.size() && text[i] == 'x') { i++; n = rt_text_num(text, i); }
  if (!gets && !ranges) throw std::runtime_error("rt_levels: a level with no request in '" + text + "'");
  job.round_trips += n * ((gets ? 1 : 0) + ranges);
  job.critical += n;
}

// round_trips and critical re-summed from an rt_levels text (pages_extra is not in it: 0). Throws on a malformed text.
// The executor checks every step's text against its columns with it
inline RtTotals rt_text_totals(const std::string& text) {
  RtTotals t;
  size_t i = 0;
  while (i < text.size()) {                   // one job
    RtTotals job;
    while (i < text.size() && text[i] != '|' && text[i] != '*') {
      if (text[i] == ' ') { i++; continue; }
      if (text[i] == 'w') { i++; if (rt_text_num(text, i)) { job.round_trips++; job.critical++; } continue; }
      rt_text_level(text, i, job);
    }
    uint64_t n = 1;
    if (i < text.size() && text[i] == '*') { i++; n = rt_text_num(text, i); }
    if (i < text.size() && text[i] == '|') i++;
    t.round_trips += n * job.round_trips;
    t.critical += n * job.critical;
  }
  return t;
}

}  // namespace evalcpp
