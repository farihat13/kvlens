// The opt-in reuse-distance pass (run.reuse_distance; P=1): for every KV pair a plan fetches, how many DISTINCT kvbytes
// were fetched since that KV pair was last fetched (its LRU stack distance in bytes). A step's fetched kvbytes by
// distance bucket give the bytes a cache of any size would miss, read off the histogram. One tracker per store
// session: the distances run across the requests of a pass, as a cache does. Beside the bytes, per bucket, the sum of
// each KV pair's own 4 KB blocks (reuse_kvpair_blocks): missed bytes / 4096 is the fewest blocks the missed KV pairs can
// fill (they share blocks), that sum the most (no two share one). Between the two, per REQUEST: the keys of one range
// read are contiguous, so its KV pairs of one bucket are taken to share blocks: ceil(their bytes / 4096), added when the
// cursor dies (a GET is its own request). An estimate, not a bound on the device: missed KV pairs of one read need not be
// neighbours, and a read's KV pairs in two buckets are rounded up twice.
//   bucket 0..39   SizeHist's power-of-two buckets of the distance (bucket i = [2^(i-1), 2^i) bytes; 0 = distance 0)
//   bucket kNew    the KV pair was never fetched before in this session
// A KV pair is one key: a GET hit (key + value bytes), a cursor landing (its key; + the value when it is read).
// Exact, O(log n) per fetch: a Fenwick tree over access times holds each KV pair's bytes at its last access.
#pragma once
#include <cstdint>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

#include "store/size_hist.h"

namespace evalcpp {

class ReuseTracker {
 public:
  static constexpr int kNew = SizeHist::kBuckets;
  static constexpr int kBuckets = SizeHist::kBuckets + 1;
  // the KV pair is fetched now with `bytes`; returns the bucket of its reuse distance
  int touch(std::string_view key, uint64_t bytes);
  // the KV pair just touched turned out larger (its value was read after its key): no new fetch
  void grow(std::string_view key, uint64_t more);
  void clear();
  size_t kvpairs() const { return kvpairs_.size(); }
 private:
  struct KvPair { uint64_t t, bytes; };
  void add(uint64_t t, int64_t d);
  uint64_t prefix(uint64_t t) const;       // bytes of the KV pairs last fetched at a time <= t
  void renumber();                         // the tree is full: compact the times to 1..n
  std::unordered_map<std::string, KvPair> kvpairs_;
  std::vector<int64_t> tree_ = std::vector<int64_t>(1 << 16, 0);
  uint64_t now_ = 0, total_ = 0;
};

// the process's tracker: a serve session measures one store, and the distances run across its requests
ReuseTracker& process_reuse_tracker();

// {"<bucket>": bytes, ..., "new": bytes}; empty when the step fetched nothing
json reuse_json(const uint64_t* bytes);

// the 4 KB blocks one KV pair fills when it shares its block with no other KV pair: what a missed KV pair costs at most
inline uint64_t reuse_kvpair_blocks(uint64_t kvpair_bytes) { return (kvpair_bytes + 4095) / 4096; }

}  // namespace evalcpp
