#include "exec/reaper.h"

#include <chrono>
#include <utility>

namespace evalcpp {

Reaper& Reaper::get() {
  static Reaper r;
  return r;
}

void Reaper::push(Job&& j) {
  std::unique_lock<std::mutex> lk(mu_);
  if (!th_.joinable()) th_ = std::thread([this] { loop(); });
  cv_.wait(lk, [this] { return queue_.size() < 2; });
  queue_.push_back(std::move(j));
  cv_.notify_all();
}

void Reaper::give(std::map<std::string, Value>&& outputs) {
  Job j;
  j.outputs = std::move(outputs);
  push(std::move(j));
}

void Reaper::give(Value&& v) {
  Job j;
  j.v = std::move(v);
  push(std::move(j));
}

void Reaper::drain() {
  std::unique_lock<std::mutex> lk(mu_);
  cv_.wait(lk, [this] { return queue_.empty() && !busy_; });
}

uint64_t Reaper::take_done_ns() {
  std::lock_guard<std::mutex> lk(mu_);
  uint64_t d = done_ns_;
  done_ns_ = 0;
  return d;
}

void Reaper::loop() {
  std::unique_lock<std::mutex> lk(mu_);
  for (;;) {
    cv_.wait(lk, [this] { return stop_ || !queue_.empty(); });
    if (queue_.empty()) return;                  // stop, with nothing left to free
    Job j = std::move(queue_.front());
    queue_.pop_front();
    busy_ = true;
    cv_.notify_all();                            // a waiting give() may queue now
    lk.unlock();
    auto t0 = std::chrono::steady_clock::now();
    j.outputs.clear();
    j.v = Value();
    uint64_t d = uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - t0).count());
    lk.lock();
    done_ns_ += d;
    busy_ = false;
    cv_.notify_all();
  }
}

Reaper::~Reaper() {
  {
    std::lock_guard<std::mutex> lk(mu_);
    stop_ = true;
  }
  cv_.notify_all();
  if (th_.joinable()) th_.join();                // frees what is still queued first (the loop drains before it stops)
}

}  // namespace evalcpp
