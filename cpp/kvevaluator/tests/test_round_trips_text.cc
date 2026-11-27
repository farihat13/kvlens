// The step column rt_levels (store/round_trips.h RtLevels::text, RtText, rt_text_totals): a job's levels as text, runs
// of equal levels and equal jobs folded, and the text re-summed to the same round_trips and critical as the levels'
// totals -- for direct jobs and for the landing replay's segment (exec/replay.cc), pages included. Also the refusals:
// a planted wrong level is caught by the re-sum, a malformed text throws.
#include <cstdint>
#include <string>
#include <vector>

#include "exec/replay.h"
#include "store/round_trips.h"
#include "harness.h"

namespace {

using namespace evalcpp;

template <class F> static bool throws(F f) {
  try { f(); } catch (const std::exception&) { return true; }
  return false;
}

static bool same(const RtTotals& a, const RtTotals& b) { return a.round_trips == b.round_trips && a.critical == b.critical; }

}  // namespace

TEST_CASE(test_round_trips_text) {
  RtLevels job;                                // 250 GETs | 3 ranges | 5 GETs + 1 range; then 7 write keys
  job.at(0).gets = 250;
  job.at(1).ranges = 3;
  job.at(2).gets = 5;
  job.at(2).ranges = 1;
  job.at(4).gets = 0;                          // an empty level (a gap): not in the text, not in the totals
  job.writes = 7;
  CHECK(job.text() == "250:0 0:3 5:1 w7", "a job's text: " << job.text());
  CHECK(same(rt_text_totals(job.text()), job.totals()) && job.totals().round_trips == 7 && job.totals().critical == 4,
        "the text re-sums to the levels' totals (7, 4)");

  RtLevels chain;                              // a unit's 1,000 GETs one after another: one level each
  for (size_t k = 0; k < 1000; k++) chain.at(k).gets = 1;
  CHECK(chain.text() == "1:0x1000", "equal levels fold: " << chain.text());
  CHECK(same(rt_text_totals(chain.text()), chain.totals()), "a folded run re-sums to 1,000 levels");

  RtText step;
  step.add(job.text());
  step.add(chain.text());
  step.add(chain.text());
  step.add("");                                // a job with no request adds nothing
  CHECK(step.str() == "250:0 0:3 5:1 w7|1:0x1000*2", "equal jobs fold: " << step.str());
  RtTotals want = job.totals();
  want.add(chain.totals());
  want.add(chain.totals());
  CHECK(same(rt_text_totals(step.str()), want), "a step's text re-sums to its jobs' totals");
  RtText other;
  other.add(step);
  CHECK(other.str() == step.str(), "a step text added into another keeps its jobs and counts");

  // a planted wrong level (one more range on level 3) is caught by the re-sum the executor runs on every step
  CHECK(!same(rt_text_totals("250:0 0:3 5:2 w7|1:0x1000*2"), want), "a planted level count is caught");
  CHECK(throws([] { rt_text_totals("250:x"); }) && throws([] { rt_text_totals("250 0:3"); }), "a malformed text throws");
  CHECK(same(rt_text_totals(""), RtTotals{}), "no request: an empty text, (0, 0)");

  // the landing replay's segment: two batches (groups) that charge SEEKs, chunks over one page (page = 100 bytes here)
  const uint64_t page = rt_page_bytes();
  rt_page_bytes() = 100;
  std::vector<Landing> ls = {
    {1, "p", 10, true, 10, 10, 19, 250, true, 1},   // 251 bytes: 2 further pages
    {1, "p", 30, true, 30, 30, 39, 150, true, 1},   // 151 bytes: 1 further page
    {1, "p", 12, true, 10, 10, 19, 250, true, 1},   // inside a decoded chunk: free
    {2, "p", 50, true, 50, 50, 59, 20, true, 1},    // no further page
    {2, "p", 70, false, 0, 0, 0, 0, false, 0},      // a miss: a SEEK (a range read that returned nothing)
  };
  RtTotals rt;
  RtText txt;
  Counts c = replay_landings(ls, nullptr, nullptr, nullptr, nullptr, &rt, &txt);
  CHECK(txt.str() == "0:2x3 0:1", "the replay's levels (two batches, then the page depths; equal ones fold): " << txt.str());
  CHECK(same(rt_text_totals(txt.str()), rt) && rt.round_trips == c.S + rt.pages_extra && rt.pages_extra == 3,
        "the replay's text re-sums to its totals (" << rt.round_trips << ", " << rt.critical << ")");
  // batches that differ from the page levels: group 3 charges three SEEKs, one chunk with one further page
  std::vector<Landing> ls2 = {
    {3, "p", 10, true, 10, 10, 19, 150, true, 1},   // 151 bytes: 1 further page
    {3, "p", 30, true, 30, 30, 39, 20, true, 1},
    {3, "p", 50, true, 50, 50, 59, 20, true, 1},
  };
  RtTotals rt2;
  RtText txt2;
  replay_landings(ls2, nullptr, nullptr, nullptr, nullptr, &rt2, &txt2);
  CHECK(txt2.str() == "0:3 0:1" && same(rt_text_totals(txt2.str()), rt2) && rt2.round_trips == 4 && rt2.critical == 2,
        "one batch of three SEEKs, then one page level: " << txt2.str());
  CHECK(throws([] { rt_text_totals("0:0"); }) && throws([] { rt_text_totals("1:2x"); }),
        "a level with no request and a fold with no count are refused (the writer emits neither)");
  rt_page_bytes() = page;
  return fails ? 1 : 0;
}
