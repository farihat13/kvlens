#include "store/reuse_distance.h"

#include <algorithm>

namespace evalcpp {

void ReuseTracker::add(uint64_t t, int64_t d) {
  for (; t < tree_.size(); t += t & (~t + 1)) tree_[t] += d;
}

uint64_t ReuseTracker::prefix(uint64_t t) const {
  int64_t s = 0;
  for (; t > 0; t -= t & (~t + 1)) s += tree_[t];
  return uint64_t(s);
}

void ReuseTracker::renumber() {
  std::vector<KvPair*> by_time;
  by_time.reserve(kvpairs_.size());
  for (auto& [k, it] : kvpairs_) by_time.push_back(&it);
  std::sort(by_time.begin(), by_time.end(), [](const KvPair* a, const KvPair* b) { return a->t < b->t; });
  size_t cap = tree_.size();
  while (cap < 2 * (by_time.size() + 2)) cap *= 2;
  tree_.assign(cap, 0);
  now_ = 0;
  for (KvPair* it : by_time) {
    it->t = ++now_;
    add(it->t, int64_t(it->bytes));
  }
}

int ReuseTracker::touch(std::string_view key, uint64_t bytes) {
  if (now_ + 1 >= tree_.size()) renumber();
  auto [pos, fresh] = kvpairs_.try_emplace(std::string(key), KvPair{0, 0});
  KvPair& it = pos->second;
  int bucket = kNew;
  if (!fresh) {
    bucket = SizeHist::bucket(total_ - prefix(it.t));   // the bytes of the KV pairs fetched after its last fetch
    add(it.t, -int64_t(it.bytes));
    total_ -= it.bytes;
  }
  it.t = ++now_;
  it.bytes = bytes;
  add(it.t, int64_t(bytes));
  total_ += bytes;
  return bucket;
}

void ReuseTracker::grow(std::string_view key, uint64_t more) {
  auto pos = kvpairs_.find(std::string(key));
  if (pos == kvpairs_.end()) return;
  pos->second.bytes += more;
  add(pos->second.t, int64_t(more));
  total_ += more;
}

void ReuseTracker::clear() {
  kvpairs_.clear();
  tree_.assign(1 << 16, 0);
  now_ = total_ = 0;
}

ReuseTracker& process_reuse_tracker() {
  static ReuseTracker t;
  return t;
}

json reuse_json(const uint64_t* bytes) {
  json o = json::object();
  for (int i = 0; i < ReuseTracker::kNew; i++) if (bytes[i]) o[std::to_string(i)] = bytes[i];
  if (bytes[ReuseTracker::kNew]) o["new"] = bytes[ReuseTracker::kNew];
  return o;
}

}  // namespace evalcpp
