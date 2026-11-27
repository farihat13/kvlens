// The PACKED posting index (meta's index.owners:{pack}): the SORTED/date index is CHUNKED -- many
// (value -> vids) terms packed into one AdjChunk KV keyed by the first value -- instead of one KV per
// value. An interior cell no engine occupies: it trades KV count for per-KV bytes on range/top-k.
// Reads walk CHUNKS (1 SEEK + 1 NEXT per chunk) with DIRECT cursor counting -- an index read is a single
// task (no fan-out), so no chunk_cache/replay (that machinery is for read_neighbors' multi-src fan-out).
// The TYPE and string VALUE indexes stay plain postings (delegated to PostingIndex): the type index is
// one value (nothing to pack), a string value needs a variable-width chunk key (a separate project).
// FULL-only: a day packed index would need the exact-value resolver DURING the walk (guarded in the meta
// ctor). The vids are the same as PostingIndex/SortedIndex for every question; only the S/G/N + bytes
// differ. Per-VALUE used-bytes == the posting index (encode each group with posting::encode_posting), so a
// rows-invariance + used-bytes cross-check pins correctness with no engine reference.
#pragma once
#include <map>
#include <string>
#include <vector>

#include "codec/chunks.h"
#include "index/posting_index.h"

namespace evalcpp {

class PackedPostingIndex : public PostingIndex {
 public:
  PackedPostingIndex(const Schema& s, uint64_t chunk_target, DateGranularity g);
  std::string layout() const override { return "packed_posting"; }
  // date_granularity, single_posting_type_index, enumerate/enumerate_uids (type index), writes: inherited.

  ValueList eq(CountingStore&, const std::string& label, const std::string& prop, const Value& value) const override;
  ValueList range(CountingStore&, const std::vector<std::string>& labels, const std::string& prop,
                  int64_t lo, int64_t hi) const override;
  std::vector<IndexHit> top_k_desc(CountingStore&, const std::vector<std::string>& labels, const std::string& prop,
                                   int64_t k, std::optional<int64_t> hi, const UidSet* candidates,
                                   const TopKResolver& resolve) const override;
  std::vector<IndexHit> range_hits(CountingStore&, const std::vector<std::string>& labels, const std::string& prop,
                                   int64_t lo, int64_t hi) const override;
  void finalize(KvSink& sink) override;   // ingest_vertex (buffering) is inherited
  // writes would RMW a per-term posting that a chunked index does not have -- fail loud (unreachable
  // today: meta runs no index write ops, but the inherited PostingIndex versions would corrupt a chunk store)
  void w_insert(CountingStore&, const std::string&, uint64_t, const Value&, IndexWrites) override;
  void w_delete(CountingStore&, const std::string&, uint64_t, const Value&, IndexWrites) override;
  void w_update(CountingStore&, const std::string&, uint64_t, const Value&, const Value&, const std::string&, IndexWrites) override;

 private:
  bool is_packed(int index_id) const { return kind_by_id_.count(index_id) > 0; }
  // the packed index ids -> their kind ("sorted" = u64 dates -> AdjChunk; "value" = variable-width strings
  // -> the value chunk). type indexes are one value (nothing to pack) and stay plain postings.
  std::map<int, std::string> kind_by_id_;
  ChunkCodecs cc_;             // the AdjChunk codec (default = plain / delta_varint = pertype_csr bytes)
};

}  // namespace evalcpp
