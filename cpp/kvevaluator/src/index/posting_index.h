// The POSTING index component (dgraph's index value under nebula's per-(type, prop) key):
//   'i' index_id:u32 <value bytes>  ->  'U' UidPack posting of every vid with that value
// One key per (index, value) -- no vid in the key (the concise move); index_id is per
// IndexDecl, so Post.creationDate and Comment.creationDate are separate indexes; a date
// index's value is the timestamp's BUCKET at the index's granularity (index/date_buckets.h:
// full = the ms, day = days since the epoch -- one term per day, dgraph's @index(day) scoped
// per type; a range read is then the boundary-day superset the plan's precise filter trims,
// and the top-k walk resolves exactness through the caller's TopKResolver); value bytes
// are SortedIndex's (full-precision int/date, length-tagged str), so no term key is a prefix
// of another's -- which is what lets a hot term overflow into parts (store/unit_value.h)
// while staying point-seekable. The type index is one posting per label (the whole
// membership: 1 unit read whatever the type size).
// Same vids as SortedIndex for every question; the counts are this layout's: an equality is
// 1 SEEK (+ parts) and the posting's bytes, a range/top-k walk is 1 SEEK + 1 NEXT per TERM
// crossed (not per entry), an enumeration is the type posting.
#pragma once
#include <cstdint>
#include <map>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "index/index_backend.h"

namespace evalcpp {

class PostingIndex : public IndexBackend {
 public:
  PostingIndex(const Schema& s, uint64_t chunk_target, DateGranularity date_granularity = DateGranularity::full)
      : schema_(s), chunk_target_(chunk_target), date_granularity_(date_granularity) {}
  std::string layout() const override { return "posting"; }
  DateGranularity date_granularity() const override { return date_granularity_; }
  // dgraph's rule: only when EVERY label declares the type index (a partial set is not a design)
  bool single_posting_type_index() const override;

  ValueList eq(CountingStore& st, const std::string& label, const std::string& prop, const Value& value) const override;
  ValueList range(CountingStore& st, const std::vector<std::string>& labels, const std::string& prop,
                  int64_t lo, int64_t hi) const override;
  // the TYPE posting when the schema declares one for the label (one unit), else every term of
  // the enumeration prop's index (nebula's rule for a label without a type index)
  ValueList enumerate(CountingStore& st, const std::string& label) const override;
  void enumerate_uids(CountingStore& st, const std::string& label,
                      const std::function<void(uint64_t)>& fn) const override;
  std::vector<IndexHit> top_k_desc(CountingStore& st, const std::vector<std::string>& labels, const std::string& prop,
                                   int64_t k, std::optional<int64_t> hi, const UidSet* candidates,
                                   const TopKResolver& resolve) const override;
  std::vector<IndexHit> range_hits(CountingStore& st, const std::vector<std::string>& labels, const std::string& prop,
                                   int64_t lo, int64_t hi) const override;

  // ingest buffers every (index, term) -> uids in RAM (an index posting needs every vertex of
  // the term); finalize writes one run per index id, terms in key order, uids ascending
  void ingest_vertex(KvSink& sink, const std::string& label, uint64_t uid, const Value& props) override;
  void finalize(KvSink& sink) override;
  // read-modify-write of the term's posting (store/unit_posting.h: the current value read
  // by counted SEEKs (unit::rmw_locate), dgraph's seam; a split unit reads and rewrites only the part holding the entry).
  // An update touches only the CHANGED prop's terms (the layout's cheapest correct write); a
  // same-value update, or a delete of an entry that is in no posting, writes nothing.
  void w_insert(CountingStore& st, const std::string& label, uint64_t uid, const Value& props, IndexWrites w) override;
  void w_delete(CountingStore& st, const std::string& label, uint64_t uid, const Value& old_props, IndexWrites w) override;
  void w_update(CountingStore& st, const std::string& label, uint64_t uid, const Value& old_row, const Value& new_row,
                const std::string& prop, IndexWrites w) override;

  // the unit key of one term: 'i' index_id <value bytes>
  static std::string term_key(int index_id, std::string_view value_bytes);

 protected:   // PackedPostingIndex (index/packed_posting_index.*) specializes only the sorted-date kind
  // the (index id, term bytes) a vertex's row owns: the type posting when declared, one per
  // declared value/sorted index whose value is present (a null value has no term)
  std::vector<std::pair<int, std::string>> terms_of(const std::string& label, const Value& props) const;
  // one counted unit read: every uid to `fn`, used = the uids' packed widths; false = absent
  bool read_term(CountingStore& st, const std::string& key, const std::function<void(uint64_t)>& fn) const;
  // the uids of one landing's value (a whole term or one of its parts), used-accounted
  static std::vector<uint64_t> decode_landing(std::string_view raw, uint64_t& used);

  const Schema& schema_;
  uint64_t chunk_target_;
  DateGranularity date_granularity_;
  std::map<std::pair<int, std::string>, std::vector<uint64_t>> buffered_;
};

}  // namespace evalcpp
