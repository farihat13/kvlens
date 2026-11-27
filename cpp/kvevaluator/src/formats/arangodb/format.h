// arangodb: the document store (Python formats/arangodb.py). A vertex is a document keyed
// (collection, docid) with a primary index (collection, _key) -> loc; every edge is its own
// document in a per-etype collection, with its own primary-index entry too (as real arango),
// and COVERING from/to edge indexes (the index value is
// the opposite vertex's "Label/vid" id, so a pure-id expansion reads no edge documents); the
// value/date indexes hold the DOCID in the key, so recovering a vid costs a doc GET per match
// (the id-indirection this layout pays everywhere). Counts are all DIRECT. A vertex's collection
// is never taken from its vid: a hop reads it from the edge-index value and puts it on the row
// (Value::label), an index or a label scan is its collection's own, and a vid that arrives with
// none is probed, a miss GET per collection tried (what a step knows travels on the row). No map from vid to collection.
#pragma once
#include <map>
#include <mutex>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

#include "codec/props_codec.h"
#include "ingest/label_stream.h"
#include "formats/common.h"
#include "formats/format.h"
#include "formats/rows.h"
#include "schema/schema.h"
#include "ops/kv_contract.h"

namespace evalcpp { class NeighborItems; }   // formats/common.h: read_neighbors' items over one shape

namespace evalcpp::arangodb {

struct Config {
  // the document codec (codec/props_codec.h), vertex AND edge documents: schema = the VPack object of the system
  // members (_key; _from, _to) followed by the RowV2 row of the declared columns (the row a Split record holds;
  // none for a prop-less etype, whose doc is then byte-identical to named); named = arango's real document,
  // every property a named member. Always in the manifest.
  PropsCodec props_codec = kPropsCodecDefault;
  static Config from_json(const json& j);
  json to_json() const;
};

class Arango : public Format, public OperatorReads {
 public:
  Arango(const Schema& schema, const Config& cfg);
  std::string name() const override { return "arangodb"; }
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
  KeyClass classify(std::string_view key) const override;   // no type decoded: the part from the role (role_class)
  json codec_config() const override { return cfg_.to_json(); }

 protected:   // the engine twin (engine.h) reads through these, as the dgraph / nebula families' subclasses do
  // a vertex was ingested or inserted under `label`. The fair format keeps nothing: a vid is opaque at query time,
  // and a vertex's collection reaches a step on the row (Value::label) or by a charged read. The engine twin keeps
  // its collection map on this (a real ArangoDB id is "collection/key": engine.h)
  virtual void note_vertex(uint64_t, const std::string&) {}
  // one src's neighbors in the asked directions (the unit of read_neighbors' fan-out). tag: each neighbour carries
  // the collection its edge-index value stores ("Label/vid", read and charged anyway)
  ValueList neighbors_of(CountingStore& st, const Value& src, const std::string& etype,
                         const std::vector<bool>& dirs, const std::vector<std::string>& edge_props, bool pairs,
                         bool tag, size_t used_first_n = SIZE_MAX, bool first_only = false);
  // the same appended to `out`, the rows over `item`'s one shape (read_neighbors: one list and one shape per step).
  // first_only: stop on the first edge, with no NEXT past it (the chase continues on the first edge only)
  void neighbors_of_into(CountingStore& st, const Value& src, const std::string& etype, const std::vector<bool>& dirs, const std::vector<std::string>& edge_props,
                         bool pairs, bool tag, size_t used_first_n,
                         const NeighborItems& item, ValueList& out, bool first_only = false);
  // the index term bytes of one (label, prop, value); write=false pads a long string to nothing
  std::string term_bytes(const std::string& label, const std::string& prop, const Value& v, bool query) const;
  std::string index_prefix(const std::string& label, const std::string& prop) const;
  // v2: edge-doc coll offset (keys.h); v3: variable-width VPack str index terms (format.cc); v4: vertex docs follow
  // props_codec (a schema store of v3 holds named vertex docs under the same manifest config -- refuse it)
  std::string key_codec() const override { return kKeyLayout; }
 public:
  static constexpr const char* kKeyLayout = "realistic-v7";   // v5: vertex docs no longer store id; v6: edge docs have their pk entry; v7: edge docs carry _key
 protected:
  std::string index_write_key(const std::string& label, const std::string& prop, const Value& v, uint64_t docid) const;

  Value read_vertex_by_id(StepExec& ex, const Value& vid, const std::vector<std::string>& labels);
  Value read_neighbors(StepExec& ex, const ValueList& srcs, const std::string& etype, const std::string& dir,
                       const std::vector<std::string>& edge_props, bool pairs, size_t used_first_n = SIZE_MAX);
  Value read_vertex_props(StepExec& ex, const ValueList& vids, const std::vector<std::string>& props,
                          const std::vector<std::string>& labels);
  Value read_anchor_props(StepExec& ex, const ValueList& cands, const std::vector<std::string>& props,
                          const std::vector<std::string>& labels);
  Value read_index_eq(StepExec& ex, const std::string& label, const std::string& prop, const Value& value);
  Value read_index_range(StepExec& ex, const std::vector<std::string>& labels, const std::string& prop,
                         int64_t lo, int64_t hi);
  Value read_vertices_by_label(StepExec& ex, const std::string& label, const Value& props, const Value& keep);
  Value read_filter_by_label_probe(StepExec& ex, const ValueList& vids, const std::vector<std::string>& labels);
  // the four operators' reads (formats/common.h OperatorReads; run_operator runs their meaning)
  Value closure_level(StepExec& ex, uint64_t level, const ValueList& frontier, const std::string& etype,
                      const std::string& dir) override;
  ChaseReads chase_reads(StepExec& ex, const std::string& etype, const std::string& dir) override;
  EdgeExistsReads edge_exists_reads(StepExec& ex, const Args& a) override;
  TopKReads top_k_reads(StepExec& ex, const kv::TopKArgs& t) override;
  Value run_write_op(const Step& step, const Args& a, StepExec& ex);   // writes.cc

  // an edge document's id, found by scanning src's forward covering index (the document
  // layout's write-side tax: the edge id is not a function of its endpoints)
  std::optional<uint64_t> locate_edge(CountingStore& st, const std::string& etype, uint64_t su, const std::string& dst);

  // DOCUMENTS under the store's codec (Config.props_codec), one rule for vertex and edge docs: `cols` = the row's
  // columns (vdoc_cols / edoc_cols). doc_encode: the system members + the props; doc_fields: the wanted members
  // (system ones from the object, props from the row) + their attributable bytes (named: each member's framed
  // bytes; schema: the system members' framed bytes + each present cell's bytes); doc_row: the whole doc as a row
  // (system members, then the props) for a read-modify-write; doc_used: the attributable bytes of some members.
  std::string doc_encode(const std::vector<ColDecl>& cols, RowFields sys, const Value& props, const std::string& what) const;
  RowFields doc_fields(const std::vector<ColDecl>& cols, std::string_view raw, const std::vector<std::string>& fields,
                       uint64_t* used) const;
  Value doc_row(const std::vector<ColDecl>& cols, std::string_view raw) const;
  uint64_t doc_used(const std::vector<ColDecl>& cols, std::string_view raw, const std::vector<std::string>& names) const;
  const std::vector<ColDecl>& vdoc_cols(const std::string& label) const { return vdoc_cols_.at(label); }   // the label's props (no id)
  const std::vector<ColDecl>& edoc_cols(const std::string& etype) const { return ecols_.at(etype); }       // the etype's props

  const Schema& schema_;
  Config cfg_;
  std::map<std::string, std::vector<ColDecl>> ecols_;
  std::map<std::string, std::vector<ColDecl>> vdoc_cols_;   // a vertex doc's row: the declared props (no id: key-derived)

  uint64_t docn_ = 0, edgen_ = 0, writen_ = 0;       // docid counters (writen_: the top-bit space)
  uint64_t n_vertices_ = 0, n_edges_ = 0;

  // ingest: docs and pk entries stream per label (docids ascend within a label); the covering
  // edge-index triples buffer per (etype, side) and sort at finalize
  struct EdgeIx { uint64_t vid, eid; int other_coll; uint64_t other_vid; };
  void flush_pk(KvSink& sink);
  LabelStream labels_{"arangodb"};   // the vertex stream: one label at a time (ingest/label_stream.h)
  std::string cur_etype_;                               // the edge-doc run's etype segment
  std::vector<std::pair<uint64_t, uint64_t>> cur_pk_;   // (vid, docid) of the current label
  bool doc_run_open_ = false;
  std::map<std::string, std::vector<EdgeIx>> eix_f_, eix_t_;
};

}  // namespace evalcpp::arangodb
