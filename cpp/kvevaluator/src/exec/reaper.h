// The step outputs' free on a background thread (run.free_outputs = "reaper", opt-in; inline is the default: below). A plan's outputs can be
// millions of rows; destroying them took 15-19% of a request's time on IC3 / IC5 / IC6 / IC9 / IC10 at SF3, after the
// plan's clock and in no wall. execute hands a finished iteration's outputs to one background thread, which destroys
// them while the response is built and sent and the next request starts. Freeing BETWEEN steps instead (run.free_rows)
// made the steps 14% slower on IC5 (recycled memory), so the outputs are freed only after the plan.
// execute waits for the reaper to be idle before EVERY iteration, the first of a request included: a free running
// beside a plan slowed that plan 35-60% (IC5, SF0.1). So a free overlaps only the response's assembly and write and
// the gap until the next request. At most two hand-offs are queued: a third waits (memory stays bounded).
// Measured (IC5, SF0.1, a block of runs per mode): the free takes 2.2x as long on this thread (memory freed away from
// the thread that allocated it), and the next request waits for it in drain(): its window +14%, its plan +3.7%. Kept
// opt-in for the SF3 check, where the free (~0.8 s on IC5) and the response's assembly overlap more.
#pragma once
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <map>
#include <mutex>
#include <string>
#include <thread>

#include "value/value.h"

namespace evalcpp {

class Reaper {
 public:
  static Reaper& get();
  void give(std::map<std::string, Value>&& outputs);
  void give(Value&& v);
  void drain();                 // waits until nothing is queued or being freed
  uint64_t take_done_ns();      // the time of the frees finished since the last call (earlier hand-offs)
  ~Reaper();

 private:
  struct Job {
    std::map<std::string, Value> outputs;
    Value v;
  };
  void push(Job&& j);
  void loop();
  std::mutex mu_;
  std::condition_variable cv_;
  std::deque<Job> queue_;
  bool busy_ = false, stop_ = false;
  uint64_t done_ns_ = 0;
  std::thread th_;
};

}  // namespace evalcpp
