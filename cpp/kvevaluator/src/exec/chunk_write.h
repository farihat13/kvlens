// Shared chunk WRITE machinery for chunked (packed) layouts (pertype_csr, meta): every write to a
// chunked family is a chunk READ-MODIFY-WRITE -- locate the chunk (1 SEEK unless this op already
// decoded it), mutate the decoded rows, re-serialize the WHOLE chunk with the chunk codecs (so the
// written bytes equal an ingest's encoding of the same rows), PUT it under the key of its (new) first
// row and DELETE the old key when the first row moved or the chunk emptied. Chunks are never re-split
// on overflow (Python pertype_csr's rule). A chunk key is the family prefix + the first id as 8
// big-endian bytes (exec/chunk_cache.h), for every chunked format. Neutral infra (as the read side's chunk_cache) -- the
// formats keep their own key builders and write policy (which chunks an op touches).
#pragma once
#include <algorithm>
#include <cstdint>
#include <functional>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "codec/chunks.h"
#include "store/counting.h"
#include "value/value.h"

namespace evalcpp {

// one write's puts + dels; dels apply BEFORE puts (T2): a same-key del+put applies like an engine
// batch -- the put wins (base._apply_writes order)
// One exception to that order, for chunks (chunk_rmw): a key this SAME write first rewrote (a put) and then moved
// away from or emptied. Its delete must follow that earlier put, or the put would re-create the old chunk beside the
// moved one (a delete_vertex whose vertex is its chunk's first owner, with a neighbour in the same chunk).
// Such a key goes to `late_dels`, applied after the puts; if the write puts the key again later, it goes
// back to `dels` and the last put wins. The number of puts and dels of a write is the same either way.
struct ChunkWrites {
  std::vector<std::pair<std::string, std::string>> puts;
  std::vector<std::string> dels;
  std::vector<std::string> late_dels;
  // the puts that rewrite a family's LAST chunk (chunk_rmw): the put's index, the chunk's value bytes before this
  // rewrite, and the family's configured target -- noted on the store when the put is applied (Counts::LastChunk)
  struct LastPut { size_t index; uint64_t base_bytes, target; };
  std::vector<LastPut> last_puts;
  void apply(CountingStore& st) {
    for (auto& k : dels) st.del_key(k);
    size_t li = 0;
    for (size_t i = 0; i < puts.size(); i++) {
      st.put(puts[i].first, puts[i].second);
      if (li < last_puts.size() && last_puts[li].index == i) {
        st.note_last_chunk_write(puts[i].first, puts[i].second.size(), last_puts[li].base_bytes, last_puts[li].target);
        li++;
      }
    }
    for (auto& k : late_dels) st.del_key(k);
  }
  bool has_put(const std::string& key) const {
    for (auto& p : puts)
      if (p.first == key) return true;
    return false;
  }
  // a chunk rewritten under `key` (chunk_rmw's put): a pending late delete of the key is overtaken by this put.
  // (No write of the configurable format reaches that branch today: it needs a chunk to be put under a key the same
  // write already left, and no test exercises it. has_put below is a linear scan of the puts per dropped key: fine
  // for the handful of chunks a write rewrites, O(puts x drops) on a hub delete at a tiny pack.)
  // `last`: the chunk is its family's last (ChunkWriteReader::Chunk::is_last), with its bytes before and its target
  void put_chunk(std::string key, std::string value, const LastPut* last = nullptr) {
    auto it = std::find(late_dels.begin(), late_dels.end(), key);
    if (it != late_dels.end()) { dels.push_back(*it); late_dels.erase(it); }
    if (last) last_puts.push_back({puts.size(), last->base_bytes, last->target});
    puts.emplace_back(std::move(key), std::move(value));
  }
  // the chunk under `key` moved to another key or emptied (chunk_rmw's delete)
  void drop_chunk(const std::string& key) { (has_put(key) ? late_dels : dels).push_back(key); }
};

constexpr uint64_t kNoOrdinal = ~0ull;   // an adjacency entry with no edge-prop row

// The write path's chunk reader (Python _ChunkReader with MUTABLE decoded chunks): a probe inside a
// decoded chunk's [lo, hi] range costs 0 ops; anything else is 1 SEEK (charged through the counting
// store), decoding the landed chunk once (its value bytes charged). Scope = one write op.
class ChunkWriteReader {
 public:
  struct AdjG {
    uint64_t src;
    std::vector<uint64_t> dsts;
    std::vector<uint64_t> ords;   // kind 1, kind 3 with ordinals: parallel when the etype carries props, else empty
    std::vector<Value> props;     // kind 3: parallel when the etype carries props, else empty
    std::vector<uint8_t> labels;  // kinds 3, 4: the far-end label codes, parallel when the chunk stores them, else empty
  };
  // kind 3 = meta's adjacency chunk (codec/lists.h), a write path only meta uses: the edge props INLINE (meta-v4's
  // `adjacency` placement), or with with_ords their ordinals (`own` placement; `cols` is then an empty list).
  // Kinds 0 and 2 are pertype_csr's and meta's props and own chunks; kind 1 is pertype_csr's adjacency chunk.
  // kind 4 = kind 1 + the far-end LABEL block (codec/chunks.h): Per-type CSR's adjacency chunk under far_label. Its
  // own kind, not kind 1 with a width: a kind 1 chunk never stores a block, whatever width a caller passes
  struct Chunk {
    int kind = 0;                 // 0 props, 1 adj, 2 ep, 3 adj + inline props, 4 adj + the label block
    unsigned label_bits = 0;      // kinds 3, 4: the label block's width (0 = none)
    std::vector<std::pair<uint64_t, Value>> rows;   // props/ep
    std::vector<AdjG> groups;                        // adj
    uint64_t lo = 0, hi = 0;
    // the family's LAST chunk (the landing is the family's final key, from the store: CountingStore::raw_last_key),
    // its configured target (the layout's pack, passed by the format) and its value bytes as stored now
    bool is_last = false;
    uint64_t target = 0, stored_bytes = 0;
    bool empty() const { return rows.empty() && groups.empty(); }
    bool adj() const { return kind == 1 || kind == 3 || kind == 4; }
    uint64_t first() const { return adj() ? groups.front().src : rows.front().first; }
    uint64_t last() const { return adj() ? groups.back().src : rows.back().first; }
  };

  ChunkWriteReader(const ChunkCodecs& cc, CountingStore& st) : cc_(cc), st_(st) {}

  // (key, chunk) of the chunk covering `id` in the family `prefix`; ("", nullptr) = the family holds
  // no chunk at or below it (a counted SEEK miss). label_bits: kinds 3 and 4. target: the family's configured chunk
  // size (the layout's pack), recorded when the landing is the family's last chunk
  std::pair<std::string, Chunk*> locate(const std::string& prefix, uint64_t target, uint64_t id, int kind,
                                        const std::vector<ColDecl>* cols, bool with_ords, unsigned label_bits = 0);
  void forget(const std::string& key) { cache_.erase(key); }
  void remember(std::string key, Chunk c) { cache_[std::move(key)] = std::move(c); }
  // one chunk's value decoded for a rewrite (static: the predictor's truth card decodes with the same codecs)
  static Chunk decode(const ChunkCodecs& cc, int kind, const std::vector<ColDecl>* cols, bool with_ords,
                      std::string_view raw, unsigned label_bits = 0);

 private:
  const ChunkCodecs& cc_;
  CountingStore& st_;
  std::map<std::string, Chunk> cache_;
};

std::string serialize_chunk(const ChunkCodecs& cc, const std::vector<ColDecl>* cols,
                            const ChunkWriteReader::Chunk& c, bool with_ords);

// Python _rmw: locate, mutate in place, sort, PUT the whole chunk, DELETE a moved/emptied key
void chunk_rmw(const ChunkCodecs& cc, ChunkWriteReader& rd, ChunkWrites& w, const std::string& prefix, uint64_t target,
               uint64_t id, int kind, const std::vector<ColDecl>* cols, bool with_ords,
               const std::function<void(ChunkWriteReader::Chunk&)>& mutate, unsigned label_bits = 0);

// Python _upsert / _drop_row / _adj_add / _adj_remove, verbatim
void chunk_upsert(std::vector<std::pair<uint64_t, Value>>& rows, uint64_t key, Value value);
void chunk_drop_row(std::vector<std::pair<uint64_t, Value>>& rows, uint64_t key);
// label: the new entry's far-end code (kind 4), inserted beside its dst; nullopt for a chunk that stores none
void chunk_adj_add(std::vector<ChunkWriteReader::AdjG>& groups, uint64_t src, uint64_t dst, uint64_t ordinal,
                   std::optional<uint8_t> label = std::nullopt);
std::optional<uint64_t> chunk_adj_remove(std::vector<ChunkWriteReader::AdjG>& groups, uint64_t src, uint64_t dst);
// kind 3: add (src -> dst) with its props row and (when the chunk stores labels) its far-end label code; remove it
// (its props and label go with it); set its props ({**old, **props}). The remove / set return whether the edge was there.
void chunk_adj_add_inline(std::vector<ChunkWriteReader::AdjG>& groups, uint64_t src, uint64_t dst, const Value& props,
                          bool has_props, std::optional<uint8_t> label);
bool chunk_adj_remove_inline(std::vector<ChunkWriteReader::AdjG>& groups, uint64_t src, uint64_t dst);
bool chunk_adj_set_inline(std::vector<ChunkWriteReader::AdjG>& groups, uint64_t src, uint64_t dst, const Value& props);
// update_edge_prop's two steps: the edge-prop ordinal of src -> dst (nullopt: no such edge, or the
// etype carries no props), and the row at that ordinal with `props` set ({**old, **props}; a
// missing row is added)
std::optional<uint64_t> chunk_adj_ordinal(const std::vector<ChunkWriteReader::AdjG>& groups, uint64_t src, uint64_t dst);
void chunk_merge_row(std::vector<std::pair<uint64_t, Value>>& rows, uint64_t key, const Value& props);

}  // namespace evalcpp
