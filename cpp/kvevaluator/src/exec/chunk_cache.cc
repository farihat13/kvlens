#include "exec/chunk_cache.h"

#include <chrono>
#include <stdexcept>

namespace evalcpp {

void SharedChunkCache::Entry::wait_ready() const {
  if (!ready.load(std::memory_order_acquire)) {
    auto t0 = std::chrono::steady_clock::now();
    while (!ready.load(std::memory_order_acquire)) {}
    task_wait_ns() += std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - t0).count();
  }
  if (failed.load(std::memory_order_acquire)) throw std::runtime_error("chunk decode failed in a sibling task");
}

static std::atomic<uint64_t> g_cache_ids{1};

SharedChunkCache::Bucket& SharedChunkCache::bucket(const std::string& prefix) {
  std::lock_guard<std::mutex> lk(m_);
  if (id_ == 0) id_ = g_cache_ids.fetch_add(1);
  auto& b = by_prefix_[prefix];
  if (!b) {
    b = std::make_unique<Bucket>();
    b->cache_id = id_;
  }
  return *b;
}

SharedChunkCache::Entry* SharedChunkCache::Bucket::claim(uint64_t first, bool& owner) {
  std::unique_lock<std::shared_mutex> lk(m);
  auto it = entries.find(first);
  if (it != entries.end()) {
    owner = false;
    return it->second.get();
  }
  auto e = std::make_unique<Entry>();
  e->first = first;
  e->lo = first;
  Entry* p = e.get();
  entries.emplace(first, std::move(e));
  log.push_back(p);
  count.store(log.size(), std::memory_order_release);
  owner = true;
  return p;
}

void SharedChunkCache::Bucket::pull(size_t from, std::vector<Entry*>& into) {
  std::shared_lock<std::shared_mutex> lk(m);
  into.insert(into.end(), log.begin() + std::ptrdiff_t(from), log.end());
}

const SharedChunkCache::Entry* SharedChunkCache::Mirror::covering(Bucket& b, uint64_t id) {
  if (last && last->first <= id && id <= last->hi) return last;   // a ready entry: hi is final
  for (int pass = 0; pass < 2; pass++) {
    // the last entry with first <= id is the only one that can cover id (ranges are disjoint)
    auto it = by_first.upper_bound(id);
    if (it != by_first.begin()) {
      Entry* e = std::prev(it)->second;
      e->wait_ready();
      if (id <= e->hi) { last = e; return e; }
    }
    size_t n = b.count.load(std::memory_order_acquire);
    if (n == seen) return nullptr;                 // nothing new in the bucket: a real miss
    fresh.clear();                                 // refresh by the new entries only, then look again
    b.pull(seen, fresh);
    for (Entry* e : fresh) by_first.emplace(e->first, e);
    seen = n;
  }
  return nullptr;
}

}  // namespace evalcpp
