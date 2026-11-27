// nebula: the vertex-oriented row store (Python formats/nebula.py). A vertex's props are one
// RowV2 value under (tag, vid); every directed edge is its own key in BOTH directions with a
// full copy of its props; the sorted index is the shared component. Every op is a set of
// independent GETs / prefix scans, so its counts are all DIRECT (no order dependence, no
// landing replay, no shared cache): what a task did is what P=1 pays.
#pragma once
#include <map>
#include <memory>
#include <set>
#include <string>
#include <vector>

#include "formats/common.h"
#include "formats/format.h"
#include "ingest/label_stream.h"
#include "formats/rows.h"
#include "formats/nebula/keys.h"
#include "index/sorted_index.h"
#include "ingest/buffers.h"
#include "schema/schema.h"
#include "ops/kv_contract.h"

namespace evalcpp { class NeighborItems; }   // formats/common.h: read_neighbors' items over one shape

namespace evalcpp::nebula {

struct Config {
  int row_timestamp = 0;   // trailing zero bytes per row (NebulaSizing.row_timestamp; 0 = unversioned)
  std::string key_order = "vid_first";   // vid_first (the engine's order) | type_first (the arm nebula-typefirst)
  static Config from_json(const json& j);
  json to_json() const;
};

class Nebula : public Format, public OperatorReads {
 public:
  Nebula(const Schema& schema, const Config& cfg);
  std::string name() const override { return "nebula"; }
  // v2: variable-width str index terms (index/sorted_index.h); a subclass inherits it
  // v3: vertex rows no longer store id; v4: the keys in the engine's order (vid first), the default.
  // A type_first store (Config::key_order) has its own stamp: one binary reads both, never one as the other
  static constexpr const char* kKeyLayout = "realistic-v4";
  static constexpr const char* kKeyLayoutTypeFirst = "realistic-v4-typefirst";   // store_identity.stale_key_layout knows the suffix
  std::string key_codec() const override { return keys_.order == KeyOrder::VidFirst ? kKeyLayout : kKeyLayoutTypeFirst; }
  std::set<std::string> capabilities() const override;
  bool has_edge_type(const std::string& name) const override;
  std::set<std::string> edge_capabilities_for(const std::string& etype) const override;
  bool has_kv_op(const std::string& op) const override;
  Value run_kv_op(const Step& step, const Args& a, StepExec& ex) override;

  void add_vertex(KvSink& sink, uint64_t uid, const std::string& label, const Value& props) override;
  void add_edge(KvSink& sink, const std::string& etype, uint64_t src, uint64_t dst, const Value& props,
                const std::string& src_label, const std::string& dst_label) override;
  void finalize(KvSink& sink) override;
  json ingest_counts() const override;
  KeyClass classify(std::string_view key) const override;
  json codec_config() const override { return cfg_.to_json(); }

  const std::vector<ColDecl>& vcols(const std::string& label) const;
  const std::vector<ColDecl>& ecols(const std::string& etype) const;

  // --- write COMPOSITION hooks (delete_vertex is built from these -- Python _w_delete_incident's
  // composition-point role; step-3 S8). Nebula removals are
  // real DELETEs: both append into `dels`, charging their reads on `st`.
  using WriteDels = std::vector<std::string>;
  // the vertex's row + its index entries (delete_vertex's with_edges=false half)
  void w_delete_vertex_own(CountingStore& st, const std::string& vid, const std::string& label,
                           WriteDels& dels);
  // one incident etype's sweep: both key orientations of every incident edge
  void w_delete_incident(CountingStore& st, const std::string& vid, const std::string& etype,
                         bool out, WriteDels& dels);

 protected:
  // this store's keys (keys.h): every member builds a key through these, in the store's order
  Keys keys_;
  std::string vertex_key(int tag_id, uint64_t vid) const { return keys_.vertex_key(tag_id, vid); }
  std::string edge_prefix(int etype_id, bool out, uint64_t src) const { return keys_.edge_prefix(etype_id, out, src); }
  std::string edge_key(int etype_id, bool out, uint64_t src, uint64_t dst) const { return keys_.edge_key(etype_id, out, src, dst); }
  std::string edge_key_dated(int etype_id, bool out, uint64_t src, int64_t date, uint64_t dst) const {
    return keys_.edge_key_dated(etype_id, out, src, date, dst);
  }
  std::string vertex_subrow_key(int tag_id, uint64_t vid, const std::string& gname) const {
    return keys_.vertex_subrow_key(tag_id, vid, gname);
  }

  // The hybrid subclass's seams (mirror of the dgraph family's): DATED etypes carry the
  // sort DATE in their reverse key (dated_key_etype -- base always false, exactly as base
  // nebula ignores the schema's adj directives; the field is an edge property in the key,
  // NOT the native rank), split schemas re-route prop reads/writes, and the edge flush
  // orders a dated etype's reverse run by date.
  virtual bool dated_key_etype(const std::string&) const { return false; }
  virtual void flush_edges(KvSink& sink);
  // the candidate tags a plan names for a vid: a tag name or a candidate list; nebula.py
  // needs one (prop access is tag-qualified), so an empty list is an error
  std::vector<std::string> labels_or_throw(const std::string& op, const std::vector<std::string>& label) const;
  // one src's neighbors in the asked directions: 1 SEEK per direction + 1 NEXT per edge, the
  // inline value charged on every edge -- the unit of read_neighbors' and read_chase's work
  ValueList neighbors_of(CountingStore& st, const std::string& src, const std::string& etype,
                         const std::vector<bool>& dirs, const std::vector<std::string>& edge_props, bool pairs,
                          size_t used_first_n = SIZE_MAX);
  // the same from a VALUE: the src keeps the label it carries (pairs), and with `tag` a neighbour carries its label
  // when the SCHEMA gives that far end one label. An open far end (hasCreator in: Post or Comment) gets none: the
  // edge key holds no tag for the far vertex, so NebulaGraph learns it only by a charged read. That miss is the
  // layout's price and the baseline's honest cost; do not "fix" it here: a baseline is never given information its layout does not store
  ValueList neighbors_of(CountingStore& st, const Value& src, const std::string& etype,
                         const std::vector<bool>& dirs, const std::vector<std::string>& edge_props, bool pairs,
                         bool tag, size_t used_first_n, bool first_only = false);
  // the same appended to `out`, the rows over `item`'s one shape (read_neighbors: one list and one shape per step).
  // first_only: stop on the first edge, with no NEXT past it (the chase continues on the first edge only)
  void neighbors_of_into(CountingStore& st, const Value& src, const std::string& etype, const std::vector<bool>& dirs,
                         const std::vector<std::string>& edge_props, bool pairs, bool tag, size_t used_first_n,
                         const NeighborItems& item, ValueList& out, bool first_only = false);
  Value read_neighbors(StepExec& ex, const ValueList& srcs, const std::string& etype, const std::string& dir,
                       const std::vector<std::string>& edge_props, bool pairs);
  Value read_vertex_by_id(StepExec& ex, const Value& vid, const std::vector<std::string>& labels);
  virtual Value read_vertex_props(StepExec& ex, const ValueList& vids, const std::vector<std::string>& props,
                                  const std::vector<std::string>& labels);
  virtual Value read_anchor_props(StepExec& ex, const ValueList& cands, const std::vector<std::string>& props,
                                  const std::vector<std::string>& labels);
  Value read_vertices_by_label(StepExec& ex, const std::string& label, const Value& props);
  Value read_filter_by_label_probe(StepExec& ex, const ValueList& vids, const std::vector<std::string>& labels);
  Value read_filter_by_label_index(StepExec& ex, const ValueList& vids, const std::vector<std::string>& labels);
  // the four operators' reads (formats/common.h OperatorReads; run_operator runs their meaning)
  Value closure_level(StepExec& ex, uint64_t level, const ValueList& frontier, const std::string& etype,
                      const std::string& dir) override;
  ChaseReads chase_reads(StepExec& ex, const std::string& etype, const std::string& dir) override;
  EdgeExistsReads edge_exists_reads(StepExec& ex, const Args& a) override;
  TopKReads top_k_reads(StepExec& ex, const kv::TopKArgs& t) override;
  virtual Value run_write_op(const Step& step, const Args& a, StepExec& ex);   // writes.cc

  const Schema& schema_;
  Config cfg_;
  SortedIndex index_;
  std::map<std::string, std::vector<ColDecl>> vcols_, ecols_;

  // ingest: vertex rows of the CURRENT label (the record stream groups them by label), flushed
  // as one key run when the next label starts; edges as (src, dst) pairs per etype with the
  // props aligned to the pairs, written as one run per (etype, direction) at finalize
  virtual void flush_vertices(KvSink& sink);   // virtual: the split flushes sub-rows beside base rows
  LabelStream labels_{"nebula"};   // the vertex stream: one label at a time (ingest/label_stream.h)
  std::unique_ptr<RowBuffer> cur_rows_;
  std::map<std::string, EdgeBuffer> edges_;
  std::map<std::string, std::unique_ptr<RowBuffer>> eprops_;
  uint64_t n_vertices_ = 0, n_edges_ = 0;
};

}  // namespace evalcpp::nebula
