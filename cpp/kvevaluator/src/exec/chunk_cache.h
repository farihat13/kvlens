// Shared chunk-reader infra for chunked (packed) layouts (pertype_csr, meta). A chunked read is
// seek_for_prev on a chunk key -- the family prefix plus the chunk's first owner id as 8 big-endian
// bytes -- plus the whole chunk value; a chunk another task in the SAME op call already decoded is
// free (no physical seek, no value re-read). This cache only saves physical work and makes P>1 safe:
// the LOGICAL S/G/N are the replay of the landing records (exec/replay.h), which is P-independent, so
// the cache never changes rows or logical counts. Neutral infra -- no format owns it.
#pragma once
#include <atomic>
#include <cstdint>
#include <map>
#include <memory>
#include <mutex>
#include <shared_mutex>
#include <string>
#include <string_view>
#include <vector>

#include "exec/tasks.h"
#include "store/counting.h"

namespace evalcpp {

// A chunk key is the family prefix + the owner id as 8 big-endian bytes (so seek_for_prev on a probe
// lands on the covering chunk). Both packed formats use this exact layout.
inline std::string chunk_with_id(const std::string& prefix, uint64_t id) {
  std::string k = prefix;
  for (int i = 7; i >= 0; i--) k.push_back(char((id >> (8 * i)) & 0xff));
  return k;
}
inline uint64_t chunk_first_id(std::string_view key) {   // the last 8 bytes, big-endian
  uint64_t u = 0;
  for (size_t i = key.size() - 8; i < key.size(); i++) u = (u << 8) | uint8_t(key[i]);
  return u;
}
inline bool chunk_has_prefix(std::string_view key, std::string_view prefix) {
  return key.size() >= prefix.size() && key.substr(0, prefix.size()) == prefix;
}

// Decoded chunks of one op call, shared by its tasks: first arrival decodes, others reuse. Never
// changes rows or logical counts (those are replayed); it saves decode CPU and lets a task skip a
// seek its siblings already resolved (reported as physical < logical).
class SharedChunkCache {
 public:
  // A chunk is claimed by the first task that lands on it (a placeholder: first = lo known, the rest
  // unknown) and filled once decoded; siblings landing on it wait for `ready` rather than decoding it
  // again.
  struct Entry {
    uint64_t first, lo, hi = 0;
    uint32_t bytes = 0;
    std::shared_ptr<void> payload;
    std::atomic<bool> ready{false};
    std::atomic<bool> failed{false};   // the owner's decode threw: every waiter throws too
    void wait_ready() const;   // spins; the time is added to the task's wait_ns; throws if failed
  };
  // One bucket per prefix with a reader-shared lock: entries keyed by first id (a claim is O(log n))
  // plus the same entries in claim order, so a mirror can pull only what arrived since its last
  // refresh. (A sorted vector + full snapshot per refresh was O(chunks^2) per op call: 54 s vs 3 s
  // for a 245k-chunk read_neighbors at sf30 -- perf sweep.)
  struct Bucket {
    uint64_t cache_id = 0;
    std::shared_mutex m;
    std::map<uint64_t, std::unique_ptr<Entry>> entries;   // by first (== lo)
    std::vector<Entry*> log;                       // in claim order; == entries as a sequence
    std::atomic<size_t> count{0};                  // == log.size(); read without the lock
    // the entry for the chunk at `first`; `owner` = this caller must decode and publish it
    Entry* claim(uint64_t first, bool& owner);
    void pull(size_t from, std::vector<Entry*>& into);   // log[from..) appended to `into`
  };
  // A worker's private, lock-free view of a bucket: refreshed under the lock only when the bucket has
  // grown since (count moved), and then only by the new entries, so the steady state does no locking
  // and a refresh is O(new), never O(all).
  struct Mirror {
    uint64_t cache_id = 0;                         // the cache this mirror belongs to
    size_t seen = 0;
    std::map<uint64_t, Entry*> by_first;
    const Entry* last = nullptr;                   // the previous hit: sorted probes repeat it
    std::vector<Entry*> fresh;                     // scratch for a pull
    const Entry* covering(Bucket& b, uint64_t id); // waits for a still-decoding candidate
  };
  Bucket& bucket(const std::string& prefix);
  uint64_t id() const { return id_; }            // unique per cache instance (mirrors key on it)
 private:
  std::mutex m_;
  std::map<std::string, std::unique_ptr<Bucket>> by_prefix_;
  uint64_t id_ = 0;
};

// The buckets an op can touch, resolved ONCE before its fan-out (no lock inside a task). A format
// registers every prefix its op could probe (register_* helpers stay in the format: they know the
// key shapes).
struct BucketTable {
  std::vector<std::pair<std::string, SharedChunkCache::Bucket*>> buckets;
  SharedChunkCache::Bucket& of(const std::string& prefix) const {
    for (auto& b : buckets)
      if (b.first == prefix) return *b.second;
    throw std::runtime_error("chunk cache: prefix not registered for this op");
  }
  void add(SharedChunkCache& cache, const std::string& prefix) {
    for (auto& b : buckets)
      if (b.first == prefix) return;
    buckets.emplace_back(prefix, &cache.bucket(prefix));
  }
};

// One task's reader: Python's _ChunkReader.locate, with the cache shared and every probe recorded as
// a landing (the replay re-derives P=1's seeks/bytes from the records). T = the decoded chunk type;
// decode(raw) -> shared_ptr<T>, called once per chunk (by the owner).
class ChunkReader {
 public:
  ChunkReader(const BucketTable& table, WorkerSlot& slot, std::vector<Landing>& log, uint64_t group)
      : table_(table), slot_(slot), st_(slot.store), log_(log), group_(group) {}

  template <class T, class Decode>
  const T* locate(const std::string& prefix, uint64_t id, Decode decode) {
    SharedChunkCache::Bucket& bucket = table_.of(prefix);
    SharedChunkCache::Mirror& mirror = mirror_of(bucket);
    if (const auto* e = mirror.covering(bucket, id)) {
      if (st_.parts_on()) st_.decodes_at(chunk_with_id(prefix, id));   // decodes out of a cached chunk: this probe's part
      log_.push_back({group_, prefix, id, true, e->first, e->lo, e->hi, e->bytes, true, uint32_t(prefix.size() + 8), bucket.cache_id});
      return static_cast<const T*>(e->payload.get());
    }
    Cursor cur = st_.seek_for_prev(chunk_with_id(prefix, id));      // physical
    if (!cur.valid() || !chunk_has_prefix(cur.key(), prefix)) {
      cur.mark_miss();                                               // no chunk of this prefix: a miss
      log_.push_back({group_, prefix, id, false, 0, 0, 0, 0, cur.valid(), cur.valid() ? uint32_t(cur.key().size()) : 0, bucket.cache_id});
      return nullptr;
    }
    uint64_t first = chunk_first_id(cur.key());
    uint32_t key_bytes = uint32_t(cur.key().size());
    bool owner = false;
    SharedChunkCache::Entry* e = bucket.claim(first, owner);
    if (owner) {
      std::string_view raw = cur.value();                            // the whole chunk, charged once
      try {
        // the parse's decode counts go to the chunk's first landing in task order (ChunkParse), not to this thread's
        // part: which probe gets here first is a race
        const DecodeCounts d0 = decode_counts();
        std::shared_ptr<T> payload = decode(raw);
        if (st_.parts_on()) {
          DecodeCounts d = decode_counts().since(d0);
          st_.decodes_exclude(d);
          slot_.parses.push_back({bucket.cache_id, prefix, first, d});
        }
        e->hi = payload->last();
        e->bytes = uint32_t(raw.size());
        e->payload = payload;
      } catch (...) {
        e->failed.store(true, std::memory_order_release);            // waiters throw, nobody spins forever
        e->ready.store(true, std::memory_order_release);
        throw;
      }
      e->ready.store(true, std::memory_order_release);
    } else {
      e->wait_ready();                                               // a sibling is decoding it
    }
    log_.push_back({group_, prefix, id, true, first, e->lo, e->hi, e->bytes, true, key_bytes, bucket.cache_id});
    return static_cast<const T*>(e->payload.get());
  }

  // The worker's mirror of this bucket. Scratch is keyed by the bucket's ADDRESS and lives for the
  // job; a FanOut destroyed and re-created inside the job (read_chase, per hop) can put a new bucket
  // at an old address, so the mirror also remembers which cache it belongs to.
  SharedChunkCache::Mirror& mirror_of(SharedChunkCache::Bucket& b) {
    auto& p = slot_.scratch[&b];
    if (!p) p = std::make_shared<SharedChunkCache::Mirror>();
    auto* m = static_cast<SharedChunkCache::Mirror*>(p.get());
    if (m->cache_id != b.cache_id) {
      m->cache_id = b.cache_id;
      m->seen = 0;
      m->by_first.clear();
      m->last = nullptr;
    }
    return *m;
  }

 private:
  const BucketTable& table_;
  WorkerSlot& slot_;
  CountingStore& st_;
  std::vector<Landing>& log_;
  uint64_t group_;
};

// Per-op fan-out context: the shared cache and its bucket table live here for the duration of the
// fan-out, so a task captures only a pointer and an index. (SharedChunkCache::id_ is assigned on
// first use, so a FanOut that never probes costs nothing.)
struct FanOut {
  SharedChunkCache cache;
  BucketTable table;
};

}  // namespace evalcpp
