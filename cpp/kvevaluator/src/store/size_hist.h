// KV-pair size histograms: what ONE request fetched or wrote, per step and per store part (the summed byte lanes of
// Counts cannot price a request). Kept BESIDE Counts, never inside it: a Counts is copied per task and per part.
//   get          one entry per GET: the KV pair's key + value bytes; a miss is size 0
//   range_bytes  one entry per SEEK (one cursor's life = one range read): the kvbytes it returned -- the key bytes of
//                its hit landings + the value bytes it read. The landing that ended the read is in the miss lanes of
//                Counts, not here. A chunk read's entry comes from the landing replay (exec/replay.h), so it is the
//                logical read at every P
//   range_keys   the same reads by the number of keys they returned (bytes alone cannot tell many small keys from
//                few large ones)
//   put          one entry per PUT: key + value bytes. A PUT that a later DELETE of the same write removes is still
//                an entry: the histogram counts requests, not bytes that stay
//   del          one entry per DELETE: the deleted KV pair's key + value bytes where the writer had them in hand, else
//                the `unknown` count (never an extra read to learn a size)
// The write units of these requests (put_units_1k, del_units_1k below) are counted with them, by the one rule there.
// Buckets are powers of two: bucket 0 = size 0, bucket i >= 1 = [2^(i-1), 2^i). `sum` is the exact total of the sizes
// (the executor's invariant: each histogram's count is its operation count and its sum is the summed lane).
#pragma once
#include <algorithm>
#include <cstdint>
#include <mutex>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

#include "value/value.h"

namespace evalcpp {

struct SizeHist {
  static constexpr int kBuckets = 40;
  uint64_t b[kBuckets] = {};
  uint64_t n = 0, sum = 0, unknown = 0;   // unknown: entries of no known size (counted in n, in no bucket)
  static int bucket(uint64_t size) {
    if (size == 0) return 0;
    int i = 64 - __builtin_clzll(size);
    return i < kBuckets ? i : kBuckets - 1;
  }
  void add(uint64_t size) { b[bucket(size)]++; n++; sum += size; }
  void add_unknown() { unknown++; n++; }
  void clear() { if (n) *this = SizeHist(); }   // an untouched histogram costs one test
  // one entry's size changed (a range read that grew, or a landing reclassified as a miss)
  void move(uint64_t from, uint64_t to) {
    int x = bucket(from), y = bucket(to);
    if (x != y) { b[x]--; b[y]++; }
    sum = sum - from + to;
  }
  void add(const SizeHist& o) {
    if (!o.n) return;
    for (int i = 0; i < kBuckets; i++) b[i] += o.b[i];
    n += o.n; sum += o.sum; unknown += o.unknown;
  }
  bool operator==(const SizeHist& o) const {
    if (n != o.n || sum != o.sum || unknown != o.unknown) return false;
    for (int i = 0; i < kBuckets; i++) if (b[i] != o.b[i]) return false;
    return true;
  }
  // sparse: {"<bucket>": count, ..., "u": unknown}; empty object when nothing was recorded
  json to_json() const {
    json o = json::object();
    for (int i = 0; i < kBuckets; i++) if (b[i]) o[std::to_string(i)] = b[i];
    if (unknown) o["u"] = unknown;
    return o;
  }
};

struct KvPairHists {
  SizeHist get, range_bytes, range_keys, put, del;
  // landings a cursor crossed without reading the value (a hit whose value() was never asked: the id came from the
  // key, or the entry was only stepped over), or whose value holds no byte (nothing to read). Logical like the
  // histograms: it follows their commit / discard
  uint64_t keys_crossed = 0;
  // cursor landings whose (non-empty) value was read: with keys_crossed, every key a range read returned is one or
  // the other (the executor asserts range_keys.sum == keys_crossed + values_read)
  uint64_t values_read = 0;
  // exact Fetch, two units, per kind of request (a histogram's bucket above 4 KB spans a factor of two in blocks):
  //   fetch_4k      the sum over requests of ceil(the request's kvbytes / 4096)
  //   store_blocks  the same requests in the store's own unit: a KV pair of 4 KB or more is one block (RocksDB keeps a
  //                 large value in a block of its own), the smaller KV pairs of one request fill 4 KB blocks together
  // A request that returned nothing (a GET miss, an empty range read) is 0 in both.
  uint64_t get_fetch_4k = 0, range_fetch_4k = 0, get_store_blocks = 0, range_store_blocks = 0;
  // exact write units, 1 KB each (a billed key-value service's write unit, DynamoDB's), per kind of write request:
  //   put_units_1k  the sum over PUTs of ceil(the KV pair's key + value bytes / 1024), at least 1 per PUT
  //   del_units_1k  the same over DELETEs of a known size; a DELETE of unknown size (del.unknown) counts 1
  // The one rule for write units (the bill reads these columns; nothing re-derives them from the histograms).
  uint64_t put_units_1k = 0, del_units_1k = 0;
  static uint64_t units(uint64_t bytes, uint64_t unit) { return (bytes + unit - 1) / unit; }
  static uint64_t blocks_4k(uint64_t bytes) { return units(bytes, 4096); }
  static uint64_t write_units_1k(uint64_t bytes) { return std::max<uint64_t>(1, units(bytes, 1024)); }
  void add_get(uint64_t kvpair_bytes) {              // a GET: its KV pair's key + value bytes, 0 for a miss
    get.add(kvpair_bytes);
    get_fetch_4k += blocks_4k(kvpair_bytes);
    get_store_blocks += kvpair_bytes ? 1 : 0;
  }
  // a range read's entry: the kvbytes it returned, of which `big_kvpairs` KV pairs of 4 KB or more and `small_bytes` in
  // smaller KV pairs (the keys entry, range_keys, is the caller's)
  void add_range_bytes(uint64_t bytes, uint64_t big_kvpairs, uint64_t small_bytes) {
    range_bytes.add(bytes);
    range_fetch_4k += blocks_4k(bytes);
    range_store_blocks += big_kvpairs + blocks_4k(small_bytes);
  }
  void add_put(uint64_t kvpair_bytes) {              // a PUT: its key + value bytes
    put.add(kvpair_bytes);
    put_units_1k += write_units_1k(kvpair_bytes);
  }
  void add_del(uint64_t kvpair_bytes) {              // a DELETE of a KV pair whose size the writer knew
    del.add(kvpair_bytes);
    del_units_1k += write_units_1k(kvpair_bytes);
  }
  void add_del_unknown() {                           // a DELETE of unknown size: 1 unit (never a read to learn it)
    del.add_unknown();
    del_units_1k += 1;
  }
  bool empty() const { return !(get.n || range_bytes.n || put.n || del.n); }
  void clear() { get.clear(); range_bytes.clear(); range_keys.clear(); put.clear(); del.clear(); keys_crossed = 0; values_read = 0;
                 get_fetch_4k = range_fetch_4k = get_store_blocks = range_store_blocks = 0;
                 put_units_1k = del_units_1k = 0; }
  void add(const KvPairHists& o) {
    get.add(o.get); range_bytes.add(o.range_bytes); range_keys.add(o.range_keys); put.add(o.put); del.add(o.del);
    keys_crossed += o.keys_crossed;
    values_read += o.values_read;
    get_fetch_4k += o.get_fetch_4k; range_fetch_4k += o.range_fetch_4k;
    get_store_blocks += o.get_store_blocks; range_store_blocks += o.range_store_blocks;
    put_units_1k += o.put_units_1k; del_units_1k += o.del_units_1k;
  }
  bool operator==(const KvPairHists& o) const {
    return get == o.get && range_bytes == o.range_bytes && range_keys == o.range_keys && put == o.put && del == o.del &&
           keys_crossed == o.keys_crossed && values_read == o.values_read &&
           get_fetch_4k == o.get_fetch_4k && range_fetch_4k == o.range_fetch_4k &&
           get_store_blocks == o.get_store_blocks && range_store_blocks == o.range_store_blocks &&
           put_units_1k == o.put_units_1k && del_units_1k == o.del_units_1k;
  }
  json to_json() const {   // only the histograms that hold an entry
    json o = json::object();
    if (get.n) o["get"] = get.to_json();
    if (range_bytes.n) { o["range_bytes"] = range_bytes.to_json(); o["range_keys"] = range_keys.to_json(); }
    if (put.n) o["put"] = put.to_json();
    if (del.n) o["del"] = del.to_json();
    if (keys_crossed) o["keys_crossed"] = keys_crossed;
    if (get_fetch_4k) { o["get_fetch_4k"] = get_fetch_4k; o["get_store_blocks"] = get_store_blocks; }
    if (range_fetch_4k) { o["range_fetch_4k"] = range_fetch_4k; o["range_store_blocks"] = range_store_blocks; }
    if (put.n) o["put_units_1k"] = put_units_1k;
    if (del.n) o["del_units_1k"] = del_units_1k;
    return o;
  }
};
// The sizes of the KV pairs a WRITE plan has read or put so far in this run, by key: how a DELETE learns the size of the
// KV pair it removes without reading it (a KV pair the plan never touched stays "unknown"). One per plan run, shared by the
// workers; a read-only plan has none, so reads pay nothing.
struct ReadSizes {
  void note(std::string_view key, uint64_t kvpair_bytes) {
    std::lock_guard<std::mutex> g(mu_);
    m_[std::string(key)] = kvpair_bytes;
  }
  int64_t find(std::string_view key) {
    std::lock_guard<std::mutex> g(mu_);
    auto it = m_.find(std::string(key));
    return it == m_.end() ? -1 : int64_t(it->second);
  }
  void clear() { std::lock_guard<std::mutex> g(mu_); m_.clear(); }
 private:
  std::mutex mu_;
  std::unordered_map<std::string, uint64_t> m_;
};

using HistParts = std::vector<KvPairHists>;   // indexed by part id; one entry (id 0) without a part scheme

inline void add_hists(HistParts& into, const HistParts& from) {
  if (into.size() < from.size()) into.resize(from.size());
  for (size_t i = 0; i < from.size(); i++) into[i].add(from[i]);
}

}  // namespace evalcpp
