// pertype_csr: column chunks for props, CSR chunks for adjacency, one KV pair per CHUNK.
// Reads price the cheapest plan the layout allows: a lookup is seek_for_prev on the chunk
// key (1 SEEK) plus the whole chunk value; a type scan is 1 SEEK + 1 NEXT per chunk; a chunk
// read earlier in the same operator call is free. The indexes are nebula's (index/).
// Every op fans out into tasks (one per vid / src / index walk); the logical counts are the
// P=1 rule replayed over the tasks' landings (exec/replay.h).
#pragma once
#include <map>
#include <memory>
#include <string>
#include <vector>

#include "exec/chunk_cache.h"
#include "ingest/label_stream.h"
#include "exec/chunk_write.h"
#include "formats/common.h"
#include "formats/format.h"
#include "codec/chunks.h"
#include "index/index_backend.h"
#include "ingest/buffers.h"
#include "ops/kv_contract.h"

namespace evalcpp::pertype_csr {

// every prefix an op over vertex rows / adjacency / edge props can touch, registered in a FanOut (format.cc)
void register_props(FanOut& f, const Schema& schema);
void register_adj(FanOut& f, const Schema& schema, const std::string& etype);

struct Config {
  uint64_t chunk_target = 65536;        // logical size at which a chunk closes (64K, plan Q5)
  std::string uids = "delta_varint";    // UidList encoder
  std::string strings = "raw_offsets";  // StringColumn encoder
  std::string layout = "plain";         // chunk body layout: plain (chunks.h)
  // the secondary index layout (index/index_backend.h): "posting" = one UidPack posting per
  // (index, value) -- THE DEFAULT; "sorted_keys" = nebula's one key per
  // (value, vid), kept selectable so the pre-flip numbers stay reproducible
  std::string index_layout = "posting";
  // each adjacency entry stores its far end's LABEL where the schema gives that direction more than one (a code of
  // ceil(log2 k) bits per entry, a block after the chunk's neighbours: codec/chunks.h). The layout is ours to
  // design (our own formats may store what helps; the engine baselines may not); a hop then names its far end's label, so the next read opens one family. On by default;
  // off = the chunks without the far label, byte for byte (the arm pertype_csr-nofarlabel)
  bool far_label = true;
  static Config from_json(const json& j);
  json to_json() const;
};

class PerTypeCsr : public Format, public ValueInspector, public OperatorReads {
 public:
  PerTypeCsr(const Schema& schema, const Config& cfg);
  std::string name() const override { return "pertype_csr"; }
  // v3: the index layout is a store axis (Config.index_layout, default posting -- a
  // sorted_keys store keeps v2's bytes); v2 was the variable-width str index terms
  // v5: an adjacency chunk may hold a far-end label block (Config::far_label); the keys are v4's. Bumped so that a
  // store written before the setting existed is refused, never decoded under a width it was not written with
  // v6: a split posting-index term's parts are keyed by their range's start uid, was a part number (store/unit_value.h)
  // v7: a chunk's size estimate counts a string's UTF-8 bytes, was its characters (codec/chunks.cc est_props)
  static constexpr const char* kKeyLayout = "realistic-v7";   // v4: props chunks no longer store id
  std::string key_codec() const override { return kKeyLayout; }
  std::set<std::string> capabilities() const override;
  bool has_edge_type(const std::string& name) const override;
  std::set<std::string> edge_capabilities_for(const std::string& etype) const override;
  // rows are packed into chunks that close at ~the chunk target (finalize): the pack target IS the chunk target
  std::optional<uint64_t> pack_target() const override { return cfg_.chunk_target; }
  bool has_kv_op(const std::string& op) const override;
  Value run_kv_op(const Step& step, const Args& a, StepExec& ex) override;

  void add_vertex(KvSink& sink, uint64_t uid, const std::string& label, const Value& props) override;
  void add_edge(KvSink& sink, const std::string& etype, uint64_t src, uint64_t dst, const Value& props,
                const std::string& src_label, const std::string& dst_label) override;
  void finalize(KvSink& sink) override;
  json ingest_counts() const override;
  KeyClass classify(std::string_view key) const override;
  const ValueInspector* values() const override { return this; }   // the far-end labels only
  json codec_config() const override { return cfg_.to_json(); }
  // every far-end label one adjacency chunk stores (census --far-labels)
  std::vector<FarLabel> far_label_entries(std::string_view key, std::string_view value) const override;

  const std::vector<ColDecl>& vcols(const std::string& label) const;
  const std::vector<ColDecl>& ecols(const std::string& etype) const;
  using WReader = ChunkWriteReader;   // the write path's chunk reader (shared: exec/chunk_write.h)

 private:
  using Reader = ChunkReader;      // one task's view of the shared cache (exec/chunk_cache.h)
  struct RowHit { const PropsChunk* chunk; size_t row; std::string label; };

  // the far labels of (etype, direction), sorted: an entry's stored code is its far label's index here
  const std::vector<std::string>& far_names(const std::string& etype, bool out) const;
  // the label block's width for a list of (etype, direction): ceil(log2 k) when the schema gives the far end k > 1
  // labels and far_label is on, else 0 (nothing stored)
  unsigned far_bits(const std::string& etype, bool out) const;
  // the code an entry of (etype, direction) stores for a far end of `label`; throws when it is not a far label
  uint8_t far_code(const std::string& etype, bool out, const std::string& label, const char* what) const;
  // the labels whose family can hold a list of (etype, direction), in schema order: what a hop from a vid with no
  // label on its row probes, in order
  std::vector<std::string> owners_of(const std::string& etype, bool out) const;
  // the labels a props read tries for `vid`: its row's label alone when the plan asks for it (none when it asks for
  // another), else the plan's list in order. A read with no label named and none carried is refused
  std::vector<std::string> props_labels(const char* op, const Value& vid, const std::vector<std::string>& labels) const;
  // (row or none) for vid under the given labels IN ORDER: one chunk lookup per label tried, the first hit wins
  bool row_lookup(Reader& rd, uint64_t uid, const std::vector<std::string>& labels, RowHit& hit);
  // what a hop over one direction of an edge type needs from the schema, worked out ONCE per op call (not per
  // source: it is operator CPU inside wall_ns): the owner labels in schema order (what an unlabelled vid probes),
  // the far end's label id where the schema gives ONE (else -1), the labels an in hop's edge props are read under
  struct HopDir {
    bool out;
    std::vector<std::string> owners;
    int16_t far;
    std::vector<std::string> ep_labels;
    unsigned bits = 0;                      // the stored label block's width for this direction (0: none stored)
    const std::vector<std::string>* names = nullptr;   // code -> the far label (far_names)
    std::vector<int16_t> ids;               // code -> its label id, for the row's tag
  };
  std::vector<HopDir> hop_dirs(const char* op, const std::string& etype, const std::vector<bool>& dirs,
                               const LabelTagPolicy& tags, bool want_ep) const;
  // one src's neighbors (the directions asked) -- the unit of read_neighbors' fan-out. `s` as the row carries it:
  // its label opens ONE family; none = the schema's owners in order. `tags` = the run's label_tags. `st` books the
  // used lanes as the configurable format's Condensed list: per direction the first `used_first_n` entries, each its
  // id bytes + (edge props asked) its edge's framed cells; used_key 0 (the chunk key is shared)
  void neighbors_of(CountingStore& st, Reader& rd, const Value& s, const std::string& etype, const std::vector<HopDir>& dirs,
                    const std::vector<std::string>& edge_props, bool pairs, bool has_ep, bool want_ep, int etype_id,
                    const LabelTagPolicy& tags, ValueList& out, size_t used_first_n = SIZE_MAX);
  // the props of out-edge #ordinal of out_src, tried under `labels` in order (the family that owns the edge-prop run);
  // `used` (when given) += the found row's framed cell bytes, every column; `rb`: the caller's builder of `wanted` rows
  Value edge_props_of(Reader& rd, const std::vector<std::string>& labels, const std::string& etype, uint64_t ordinal,
                      const std::vector<std::string>& wanted, const RowBuilder& rb, uint64_t* used = nullptr);
  // read_neighbors as a fan-out on `ex` under landing group `group`; returns the concatenation. `shared`: the
  // caller's chunk cache (registered for `etype`), when several reads of ONE step must see each other's chunks
  // (read_edge_exists); the group and the cache go together: one cache, one group
  Value read_neighbors(StepExec& ex, uint64_t group, const ValueList& srcs, const std::string& etype,
                       const std::string& dir, const std::vector<std::string>& edge_props, bool pairs,
                       FanOut* shared = nullptr, size_t used_first_n = SIZE_MAX);
  Value read_vertex_props(StepExec& ex, const ValueList& vids, const std::vector<std::string>& props,
                          const std::vector<std::string>& label);
  Value read_anchor_props(StepExec& ex, const ValueList& cands, const std::vector<std::string>& props,
                          const std::vector<std::string>& labels);
  Value read_vertices_by_label(StepExec& ex, const std::string& label, const Value& props, const Value& keep);
  Value read_filter_by_label_probe(StepExec& ex, const ValueList& vids, const std::vector<std::string>& labels);
  Value read_filter_by_label_index(StepExec& ex, const ValueList& vids, const std::vector<std::string>& labels);
  // the four operators' reads (formats/common.h OperatorReads; run_operator runs their meaning)
  Value closure_level(StepExec& ex, uint64_t level, const ValueList& frontier, const std::string& etype,
                      const std::string& dir) override;
  ChaseReads chase_reads(StepExec& ex, const std::string& etype, const std::string& dir) override;
  EdgeExistsReads edge_exists_reads(StepExec& ex, const Args& a) override;
  TopKReads top_k_reads(StepExec& ex, const kv::TopKArgs& t) override;
  Value run_write_op(const Step& step, const Args& a, StepExec& ex);   // writes.cc

  const Schema& schema_;
  Config cfg_;
  ChunkCodecs cc_;
  std::unique_ptr<IndexBackend> index_;   // picked by cfg_.index_layout
  std::map<std::string, std::vector<ColDecl>> vcols_, ecols_;
  std::map<std::pair<std::string, bool>, std::vector<std::string>> far_;   // (etype, out) -> far_names

  // ingest buffers (plan 6.2: flat). Vertex rows are buffered ONE LABEL AT A TIME (the record
  // stream groups them by label) and flushed to chunks when the next label starts; edges are
  // (src, dst) pairs per (label, etype, direction) plus, for a prop-carrying etype, the edge
  // props of the OUT key aligned with its pairs.
  struct AdjKey { std::string label, etype; bool out; bool operator<(const AdjKey& o) const; };
  void flush_vertices(KvSink& sink);
  LabelStream labels_{"pertype_csr"};   // the vertex stream: one label at a time (ingest/label_stream.h)
  std::unique_ptr<RowBuffer> cur_rows_;
  std::map<AdjKey, EdgeBuffer> adj_;
  // per AdjKey whose direction has more than one far label: each buffered edge's far-end code, parallel to adj_.
  // Kept whatever far_label says: ingest numbers an in entry's edge under its source's label from it
  std::map<AdjKey, std::vector<uint8_t>> adj_far_;
  std::map<AdjKey, std::unique_ptr<RowBuffer>> eprops_;   // out keys of prop-carrying etypes
  uint64_t n_vertices_ = 0, n_edges_ = 0;
};

}  // namespace evalcpp::pertype_csr
