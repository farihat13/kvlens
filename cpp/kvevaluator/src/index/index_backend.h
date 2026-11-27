// The secondary-index component behind ONE interface, so a format picks its index LAYOUT
// without owning it: SortedIndex (nebula's one key per (value, vid), index/sorted_index.h) and
// PostingIndex (one key per (index, value) holding a UidPack posting of vids,
// index/posting_index.h). Both answer the same questions with the same vids -- only the
// S/G/N and bytes differ, which is the point of the comparison. A format holds a
// std::unique_ptr<IndexBackend> from make_index_backend(schema, layout); the per-IndexDecl
// `layout` field in schema.json ("sorted_keys" | "posting") overrides the format's default
// per index when present.
#pragma once
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "formats/format.h"
#include "schema/schema.h"
#include "index/date_buckets.h"
#include "store/counting.h"
#include "util/uidset.h"
#include "value/value.h"

namespace evalcpp {

// one entry a top-k walk crossed and kept: the vid, its sort value (exact under a full-precision
// index; the resolved exact value when the walk was given a resolver; else the bucket), the
// bucket the walk read it from, the label it came from
struct IndexHit {
  uint64_t vid;        // the uid, as the index key holds it
  int64_t value;
  int64_t bucket;
  std::string label;
};

// the top-k walk's exactness hook for a COARSE (day-bucketed) date index: called for every
// candidate the walk collects, returns the entry's exact value (the caller reads it: 1 GET,
// dgraph's ms_of rule) or nullopt to drop it (a boundary-bucket entry past the exclusive hi);
// only kept entries count toward k. Absent (full precision) the term value IS exact.
using TopKResolver = std::function<std::optional<int64_t>(uint64_t uid, int64_t bucket, const std::string& label)>;

// the write ops' pending puts/dels (a format's Writes struct hands its vectors in)
struct IndexWrites {
  std::vector<std::pair<std::string, std::string>>& puts;
  std::vector<std::string>& dels;
};

class IndexBackend {
 public:
  virtual ~IndexBackend() = default;
  virtual std::string layout() const = 0;   // "sorted_keys" | "posting"
  // does entering by the type index cost ONE unit read whatever the type size, for EVERY label
  // (the type_index_single_posting capability)?
  virtual bool single_posting_type_index() const = 0;
  // the date index's term granularity (index/date_buckets.h): full = exact terms; day = a
  // coarse pre-filter whose range results are a boundary-day SUPERSET and whose top-k walk
  // needs a resolver for exactness
  virtual DateGranularity date_granularity() const { return DateGranularity::full; }

  // --- reads (the vids are the same on every backend; the counts are the layout's) ---
  // read_index_eq: the vids whose `prop` equals `value`, under `label`
  virtual ValueList eq(CountingStore& st, const std::string& label, const std::string& prop,
                       const Value& value) const = 0;
  // read_index_range: [lo, hi) on a sorted column, per label, concatenated in label order
  virtual ValueList range(CountingStore& st, const std::vector<std::string>& labels, const std::string& prop,
                          int64_t lo, int64_t hi) const = 0;
  // read_vertices_by_label: every vid of the label through its enumeration structure
  virtual ValueList enumerate(CountingStore& st, const std::string& label) const = 0;
  // the same walk, each member's uid handed to `fn` (membership filters probe in uid space)
  virtual void enumerate_uids(CountingStore& st, const std::string& label,
                              const std::function<void(uint64_t)>& fn) const = 0;
  // read_top_k_by_index's walk: newest-first over the labels' sorted indexes, collecting hits
  // (in the candidate set, or every entry without one), stopping once k hits are in hand and
  // the next value is older (ties at the k-th value are kept). Walk order.
  virtual std::vector<IndexHit> top_k_desc(CountingStore& st, const std::vector<std::string>& labels,
                                           const std::string& prop, int64_t k, std::optional<int64_t> hi,
                                           const UidSet* candidates, const TopKResolver& resolve) const = 0;
  // read_index_range with each vid's TERM value (the exact value, or the bucket): what a
  // bucketed plan (index_range_buckets) needs to tell boundary buckets from interior ones
  virtual std::vector<IndexHit> range_hits(CountingStore& st, const std::vector<std::string>& labels,
                                           const std::string& prop, int64_t lo, int64_t hi) const = 0;

  // --- ingest: one vertex's entries (streamed), then whatever the layout buffers ---
  virtual void ingest_vertex(KvSink& sink, const std::string& label, uint64_t uid, const Value& props) = 0;
  virtual void finalize(KvSink& sink) = 0;

  // --- the write ops' index maintenance (composition surface: the format's row/chunk writes
  // stay index-agnostic). Each appends its puts/dels and charges its reads on `st`. ---
  virtual void w_insert(CountingStore& st, const std::string& label, uint64_t uid, const Value& props,
                        IndexWrites w) = 0;
  virtual void w_delete(CountingStore& st, const std::string& label, uint64_t uid, const Value& old_props,
                        IndexWrites w) = 0;
  // update_prop: the row before and after, and which prop changed
  virtual void w_update(CountingStore& st, const std::string& label, uint64_t uid, const Value& old_row,
                        const Value& new_row, const std::string& prop, IndexWrites w) = 0;
};

// the backend for `layout` ("sorted_keys" | "posting"); `chunk_target` = the posting layout's
// overflow split (store/unit_value.h), ignored by sorted_keys. Throws on an unknown layout.
std::unique_ptr<IndexBackend> make_index_backend(const Schema& schema, const std::string& layout,
                                                 uint64_t chunk_target = 65536,
                                                 DateGranularity date_granularity = DateGranularity::full);

}  // namespace evalcpp
