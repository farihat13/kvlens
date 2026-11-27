// The sorted KV index component -- nebula's layout, shared infra (pertype_csr borrows it
// byte-for-byte in Python; both formats use this directory in C++).
//   ("i", label, prop, value, vid)  ->  'i' index_id:u32 <value bytes> vid:u64
// value bytes: a sorted (int/date) column = 8B big-endian with the sign bit flipped (byte
// order == numeric order); a str column = a VARIABLE-width length-tagged term (vpack_str_bytes:
// the declared fixed width is an engine declaration knob and its zero pad carries no
// information -- the fairness ruling charges the structural bytes only, like
// arango's VPack term); the empty-tag type index has no value bytes.
// The walks reproduce nebula.py's cost shapes exactly (seek + 1 NEXT per entry crossed).
#pragma once
#include <cstdint>
#include <functional>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "index/index_backend.h"
#include "schema/schema.h"
#include "store/counting.h"
#include "util/uidset.h"
#include "value/value.h"

namespace evalcpp {

struct IndexKeys {
  static std::string prefix(int index_id);
  static std::string sorted_bytes(int64_t v);
  static int64_t sorted_value(std::string_view bytes8);
  // the VARIABLE-width str term every value index uses: the VelocyPack string encoding a real
  // ArangoDB persistent index stores in its RocksDB key (0x40+len header for <= 126 bytes, else
  // 0xbf + 8-byte length), and the fair charge for nebula's too. The length rides in the header,
  // so an equality prefix scan cannot bleed into a longer term with the same leading bytes.
  // Order-preserving within a length, which is all an equality / prefix scan needs (the range
  // walks are on the sorted int/date columns).
  static std::string vpack_str_bytes(const std::string& s);
  static std::string key(const std::string& prefix, std::string_view value_bytes, uint64_t vid);
  // A null value's term (a write with no value for a declared index column; ingest writes none). Nulls are one block
  // BEFORE every value, as ArangoDB (VelocyPack null < numbers < strings), NebulaGraph and the Python oracle keep them:
  // a sorted / int term's null is the INT64_MIN slot (8 zero bytes, so every entry stays 16 B and a walk never meets
  // one unless it asks for INT64_MIN), a str term's null is the VelocyPack null byte, below every string header.
  static constexpr char kNullStr = 0x18;
  static bool is_null_sorted(std::string_view bytes8) { return bytes8 == std::string_view("\0\0\0\0\0\0\0\0", 8); }
  // a stored (non-null) sorted term that lands in the null slot is refused (a value of INT64_MIN)
  static const std::string& refuse_null_slot(const std::string& term, const std::string& what);
  static uint64_t vid_of(std::string_view key);
  static uint32_t index_id_of(std::string_view key);   // the u32 after the marker
};

// the bytes a value takes in an index key -- shared by every index layout (the term of a
// posting index is exactly these bytes): sorted columns 8B BE sign-flipped, str columns the
// length-tagged vpack term, other value columns as sorted ints
// the null term of one index column: 8 zero bytes for a sorted / int term, IndexKeys::kNullStr for a str term
std::string index_null_bytes(const Schema& schema, const IndexDecl& ix, const std::string& label);

std::string index_value_bytes(const Schema& schema, const IndexDecl& ix, const std::string& label, const Value& v,
                              DateGranularity date_granularity = DateGranularity::full);

class SortedIndex : public IndexBackend {
 public:
  explicit SortedIndex(const Schema& s) : schema_(s) {}
  std::string layout() const override { return "sorted_keys"; }
  bool single_posting_type_index() const override { return false; }   // one entry per vertex

  // The index entries one vertex owns (ingest): exactly nebula add_vertex's writes.
  std::vector<std::string> vertex_keys(const std::string& label, uint64_t uid, const Value& props) const;
  // The WRITE path's entries (nebula _vertex_index_pairs): one per DECLARED index column of the
  // label -- a column with no value still gets an entry under the NULL term (a row-store index
  // holds one entry per row per index, which keeps the insert cost flat in property count) --
  // plus the empty-tag type entry when the label has no required property index.
  std::vector<std::string> vertex_write_keys(const std::string& label, uint64_t uid, const Value& props) const;
  // One write-path index key (nebula _index_key): the null term (index_null_bytes) when `val` is null.
  std::string write_key(const std::string& label, const std::string& prop, const Value& val, uint64_t uid) const;

  // read_index_eq: prefix seek + NEXT per match, vid from the key (S=1, N=matches).
  ValueList eq(CountingStore& st, const std::string& label, const std::string& prop, const Value& value) const override;
  // read_index_range: per label seek lo, NEXT until >= hi; [lo, hi) on a sorted column.
  ValueList range(CountingStore& st, const std::vector<std::string>& labels, const std::string& prop,
                  int64_t lo, int64_t hi) const override;
  // read_vertices_by_label (nebula's): 1 SEEK + 1 NEXT per vertex over the label's
  // enumeration structure (its first required property index, else the empty-tag index).
  ValueList enumerate(CountingStore& st, const std::string& label) const override;
  // the same walk (same SEEK/NEXT/used charges), each member's uid handed to `fn` instead of
  // materialized as a vid Value: the membership filters probe their candidate set in uid space
  void enumerate_uids(CountingStore& st, const std::string& label, const std::function<void(uint64_t)>& fn) const override;

  // read_top_k_by_index's walk: per label seek_for_prev(hi) on the sorted index, merge the
  // cursors newest-first, collect hits (vid in the candidate set, or every entry when there is
  // no set), stop after the k-th hit's date is passed. Returns the hits in walk order.
  using Hit = IndexHit;
  std::vector<IndexHit> range_hits(CountingStore& st, const std::vector<std::string>& labels, const std::string& prop,
                                   int64_t lo, int64_t hi) const override;
  std::vector<Hit> top_k_desc(CountingStore& st, const std::vector<std::string>& labels, const std::string& prop,
                              int64_t k, std::optional<int64_t> hi,
                              const UidSet* candidates, const TopKResolver& resolve) const override;

  // --- IndexBackend's ingest + write surface: the key-only entries above, streamed ---
  void ingest_vertex(KvSink& sink, const std::string& label, uint64_t uid, const Value& props) override;
  void finalize(KvSink&) override {}   // nothing buffered: the sink sorts the index runs
  // one entry-put per declared index (insert), one del per entry (delete); an update moves
  // only the touched column's entry -- every other key holds an unchanged value (real nebula
  // rewrites them all, mb2 MB81 == MB82: an engine choice, not a layout cost)
  void w_insert(CountingStore& st, const std::string& label, uint64_t uid, const Value& props, IndexWrites w) override;
  void w_delete(CountingStore& st, const std::string& label, uint64_t uid, const Value& old_props, IndexWrites w) override;
  void w_update(CountingStore& st, const std::string& label, uint64_t uid, const Value& old_row, const Value& new_row,
                const std::string& prop, IndexWrites w) override;

 private:
  std::string value_bytes(const IndexDecl& ix, const std::string& label, const Value& v, bool query) const;
  const Schema& schema_;
};

}  // namespace evalcpp
