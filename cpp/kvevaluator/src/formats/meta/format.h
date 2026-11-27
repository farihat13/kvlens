// meta: the config-driven format (see config.h + keys.h). ONE consistent key family, varying only
// by the co-location suffix; ops + ingest branch on the dials, reusing the neutral codecs
// (codec/columns, codec/lists, store/unit_list) and the shared IndexBackend (index/), whose
// layout also comes from the config. Nothing here imports a sibling format.
//
//   vertex_props.group: item (one record per (vtype, vid, attr): the cell's payload) | owner (one row of columns
//                       per (vtype, vid))
//   adjacency.group:    owner (one list per (owner, dir, etype)) | item (one record per edge: its columns)
// Every layout decision is per vertex LABEL (cfg_.props_of) or per EDGE TYPE AND DIRECTION (cfg_.adj_of(etype, out)):
// a spec may give a label, an etype, or one direction's lists of an etype its own layout (config.h). An op over several labels needs them to share one
// props layout (props_layout): a packed read is replayed from its landings, an unpacked one counted
// directly, and one fan-out is one or the other. The ctor refuses a spec whose vid group mixes props
// layouts, so the multi-label reads the plans do (Post + Comment) always agree.
#pragma once
#include <cstdint>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <set>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include "codec/chunks.h"
#include "ingest/label_stream.h"
#include "codec/columns.h"
#include "codec/posting.h"
#include "exec/chunk_cache.h"
#include "exec/tasks.h"
#include "formats/common.h"
#include "formats/format.h"
#include "formats/meta/config.h"
#include "formats/meta/parts.h"
#include "formats/rows.h"
#include "index/index_backend.h"
#include "ingest/buffers.h"
#include "schema/schema.h"
#include "ops/kv_contract.h"

namespace evalcpp::predict { struct DatasetStats; struct EstimateInput; }

namespace evalcpp::meta {

// a write request's items as the predictor's truth card wrote them so far (key -> value; nullopt = deleted): the
// card's stand-in for the run's shadow overlay, kept by the caller across the request's steps
struct WriteReplay {
  std::map<std::string, std::optional<std::string>> items;
};

class Meta : public Format, public ValueInspector, public OperatorReads {
 public:
  Meta(const Schema& schema, const Config& cfg);
  std::string name() const override { return "meta"; }
  // v2: the same spec now writes adjacency split parts and drops the packed reverse list
  // under declared direction -- a store built before that must refuse, not reuse silently
  // v3: Condensed / Chunked vertex rows no longer store id. v4 (the symmetric format): one property
  // encoding (codec/columns.h) and one list encoding (codec/lists.h) in every layout. While v4 is built step by step
  // each byte-changing step names its own layout (meta-v4-s1b: vertex props as columns too; meta-v4-s2: edge-prop
  // placement, inline by default; meta-v4-s3: key family D, etype-first adjacency keys with no label), so a store from
  // an earlier step is refused, never reused. The finished format is "meta-v4" (the last step, meta-v4-fl,
  // added the far end's label to an adjacency entry where the schema leaves it open, far_label)
  // meta-v4-part-start: a split list's parts are keyed by their range's start id, was a part number (store/unit_value.h)
  // meta-v4-chunk-bytes: a chunk's size estimate counts a string's UTF-8 bytes, was its characters (codec/chunks.cc
  // est_props), so a Chunked family with non-ASCII strings closes its chunks a little earlier
  static constexpr const char* kKeyLayout = "meta-v4-chunk-bytes";
  std::string key_codec() const override { return kKeyLayout; }
  std::set<std::string> capabilities() const override;
  bool has_edge_type(const std::string& name) const override;
  std::set<std::string> edge_capabilities_for(const std::string& etype) const override;
  std::set<std::string> capabilities_for(const std::string& label) const override;   // the label's own layout
  std::optional<uint64_t> pack_target() const override;   // the smallest vertex-props pack over the labels
  bool has_kv_op(const std::string& op) const override;
  Value run_kv_op(const Step& step, const Args& a, StepExec& ex) override;

  void add_vertex(KvSink& sink, uint64_t uid, const std::string& label, const Value& props) override;
  void add_edge(KvSink& sink, const std::string& etype, uint64_t src, uint64_t dst, const Value& props,
                const std::string& src_label, const std::string& dst_label) override;
  void finalize(KvSink& sink) override;
  json ingest_counts() const override;
  KeyClass classify(std::string_view key) const override;   // from the part scheme (part_scheme below)
  const ValueInspector* values() const override { return this; }
  std::optional<uint64_t> payload_bytes(std::string_view key, std::string_view value) const override;
  std::optional<bool> empty_value(std::string_view key, std::string_view value) const override;
  std::optional<std::tuple<std::string, uint64_t, uint64_t>> chunk_range(std::string_view key, std::string_view value) const override;
  std::vector<FarLabel> far_label_entries(std::string_view key, std::string_view value) const override;
  json codec_config() const override { return cfg_.to_json(); }
  // --- predict mode (formats/meta/predict.cc) ---
  // One KV step of a traced run (its response object: op, args, arg_tags, parts) priced from its TRUTH card: the
  // step's cardinalities from its arguments and raw reads of `store` (no CountingStore), never from the step's own
  // counters -- except a top-k walk's length, taken from its `index:` parts (an input of the rule, not a rule), and its
  // winners, taken from its `out_vids`. `plant`: the name of one rule to break (the exact gate's own check; "" = none).
  // `coverage` (when given): += the cases each rule priced in this step, by rule name (a rule with 0 cases is untested).
  // `plants_asked` (when given): += the plant names the rules consulted pricing this step. A plant not consulted
  // cannot change this step's prediction (the rules take the same path), so the gate re-prices a step only for those.
  // Throws on an op or a layout the rules do not cover yet.
  // `replay` (a write step: required): the items the truth card's own rules wrote at the request's earlier steps, read
  // merged over the store as the run's shadow overlay merges them; this step's writes are added to it
  // `terms_out` (a read step, when given): the step's terms (predict/terms.h) from the same truth card, for level true_terms
  Counts predict_step_truth(const json& step, Store& store, const std::string& plant, json* coverage = nullptr,
                            std::set<std::string>* plants_asked = nullptr, WriteReplay* replay = nullptr,
                            std::map<std::string, double>* terms_out = nullptr) const;
  static const std::vector<std::string>& predict_plants();   // the rule names --plants takes
  static const std::vector<std::string>& predict_rules();    // the cases the gate counts (0 = untested)
  // The same step priced from STATISTICS (the estimate card), no store: `in` = the step's inputs as the caller decides
  // them (predict/estimate_input.h: a run's true counts and its truth card's terms at level true_terms, the cardinality layer's and the StepSet's at predicted),
  // `stats` = stats.json, `tally` = the per-part key and value bytes (a store census, standing in for the ingest tally).
  // Returns the lanes as expected values, by name.
  json predict_step_estimate(const json& step, const predict::EstimateInput& in, const predict::DatasetStats& stats,
                             const json& tally) const;
  // keys.h "Parts". Built on first use (a run with run.parts), so its schema-name check can never
  // fail a plain meta run
  const PartScheme* part_scheme() const override {
    std::call_once(parts_once_, [this] { parts_ = std::make_unique<MetaParts>(schema_); });
    return parts_.get();
  }

 private:
  std::set<std::string> label_free_capabilities() const;   // index + key caps, the same on every label
  // --- schema helpers ---
  const std::vector<ColDecl>& ecols(const std::string& etype) const;
  const std::vector<ColDecl>& vcols(const std::string& label) const;
  uint8_t vtype_of(const std::string& label) const;                 // the exact label id (keys by it)
  int attr_id(const std::string& label, const std::string& prop) const;   // the prop's column in vcols, -1 if absent
  bool reverse_available(const std::string& etype) const;         // dir_stored: always_both, or schema @reverse
  std::vector<std::string> labels_or_throw(const char* op, const std::vector<std::string>& label) const;
  std::vector<bool> dirs_of(const std::string& op, const std::string& etype, const std::string& dir) const;
  // grouped props (Config::vertex_groups): a Condensed label's props as one or more rows. A label
  // not listed in the spec has ONE group of every declared prop (the Condensed row, same key, same bytes).
  struct RowGroups {
    std::vector<std::vector<ColDecl>> cols;   // group g's columns, in the label's declared order
    std::vector<std::vector<size_t>> at;      // group g's columns as indexes into vcols (the ingest buffer)
    std::map<std::string, size_t> group_of;   // prop -> its group
  };
  const RowGroups& row_groups(const std::string& label) const;
  // the groups a read of `props` touches, ascending; group 0 (the existence row) when none of the props is
  // the label's -- a read still probes the vertex, as a Condensed row GET does
  std::vector<size_t> groups_for(const std::string& label, const std::vector<std::string>& props) const;
  // project `props` of (label, u) from its touched group rows: GET each (a group already in `have` is reused --
  // its key already counted; nullopt = already known absent), count the hit keys + the wanted cells as used.
  // Returns whether any touched row exists.
  bool project_groups(CountingStore& st, const std::string& label, uint64_t u, const std::vector<std::string>& props,
                      const std::map<size_t, std::optional<std::string>>& have, std::map<std::string, Value>& cells);
  // project_groups' part that does not depend on the vertex, made once per op call: the touched groups, and per
  // group the (position in props, column) pairs its row answers, in the order project_groups assigns them
  struct GroupPlan {
    std::vector<size_t> groups;                                  // groups_for(label, props)
    std::vector<std::vector<std::pair<size_t, size_t>>> cells;   // per groups[i]: (position in props, column)
  };
  GroupPlan group_plan(const std::string& label, const std::vector<std::string>& props) const;
  // project_groups over a plan: `cells` holds one value per position of the plan's props (a position no touched row
  // answers is left as it is); `view` parses each row, reused across calls
  bool project_planned(CountingStore& st, const std::string& label, uint64_t u, const GroupPlan& plan,
                       const std::map<size_t, std::optional<std::string>>& have, columns::View& view,
                       std::vector<Value>& cells);
  // a read over candidate labels, one label: is the vertex this label? The first touched row -- a hit proves it;
  // only a miss on a later group needs group 0 (the existence row, always written) to decide. A wrong label costs
  // <= 2 GETs. Found: the vertex's touched rows projected into `cells` (the first row reused). The one rule for
  // read_anchor_props and a multi-label read_vertex_props.
  bool probe_label(CountingStore& st, const std::string& label, uint64_t u, const std::vector<std::string>& props,
                   std::map<std::string, Value>& cells);
  bool probe_planned(CountingStore& st, const std::string& label, uint64_t u, const GroupPlan& plan,
                     columns::View& view, std::vector<Value>& cells);   // probe_label over a plan
  // the one props layout of an op's candidate labels (throws when they differ: see the header comment)
  const Component& props_layout(const char* op, const std::vector<std::string>& labels) const;
  // does some label of `owner`'s vid group own (etype, dir) lists? A packed hop skips the probe when none does
  // own placement's edge-prop read only (held: plan table T3); no other read or write may take a
  // decision from a vid's group -- tests/check_vid_opaque.py guards it
  bool group_owns(uint64_t owner, const std::string& etype, bool out) const;
  // --- the far-end label (adjacency far_label; codec/lists.h label block) ---
  unsigned far_bits(const std::string& etype, bool out) const;    // the label block's width; 0 = none stored
  // the code of a far end labelled `label` (0 when nothing is stored); a label outside the endpoints throws
  uint8_t far_code(const std::string& etype, bool out, const std::string& label) const;
  // the label id a hop's entry tells: the schema's one label (k = 1, any setting), the decoded code (k > 1, far_label
  // on), else -1 (nothing known)
  int far_label_id(const std::string& etype, bool out, uint8_t code) const;
  // far_label_id's answer for every code of one (etype, direction), resolved when the format is built: a read looks
  // the pair up once per list, not once per edge
  struct FarIds {
    int any = -1;                  // the answer for a code outside by_code (k = 1: the schema's one label)
    std::vector<int> by_code;      // k > 1 with stored codes: the label id per code
    int of(uint8_t code) const { return code < by_code.size() ? by_code[code] : any; }
  };
  const FarIds& far_ids(const std::string& etype, bool out) const;
  // labels on the rows (Value::label): the label a vid value carries (nullopt = none), a value carrying `label`
  std::optional<std::string> tag_label(const Value& v) const;
  Value tagged(Value v, const std::string& label) const;
  Value tag_all(const Value& vids, const std::string& label) const;   // a list of vid values, each carrying `label`
  // the candidate labels left to probe for a vid value: its carried label if a candidate, none if not one, `labs` if
  // it carries none
  std::vector<std::string> narrow(const Value& vid, const std::vector<std::string>& labs) const;
  // a hop's item (formats/common NeighborItems' shape, `item` made once per list): with `src_val`, the neighbour value
  // carries label id `lid` (when >= 0) and a pairs row's src is `*src_val` (the input value: its tag kept)
  Value nbr_item(const NeighborItems& item, const Value* src_val, uint64_t su, uint64_t dst, int lid,
                 const Value* facets) const;
  // a props row's names in the layout's field order: Condensed / Chunked put the key-derived id first, Split keeps the
  // asked order (a RowBuilder's names; every props read of the layout pushes in this order)
  std::vector<std::string> props_row_names(const Component& layout, const std::vector<std::string>& props) const;
  // the row a props read returns for a vertex of none of the candidate labels (what a probe of each would have
  // returned, in the layout's field order), at no op; `rb`: props_row_names' builder
  Value absent_row(const Component& layout, uint64_t u, const std::vector<std::string>& props, const RowBuilder& rb) const;

  // --- ingest (flat buffers in; one sorted run per label / per (etype, dir) straight to the sink) ---
  void flush_label(KvSink& sink);                                // current label's rows -> its run
  Value cell_value(const RowBuffer& b, size_t row, size_t col, const ColDecl& cd) const;

  // --- packed (chunked) reads: locate the covering chunk via the shared cache (exec/chunk_cache.h),
  // emitting a Landing per probe; the neutral replay (exec/replay.h) turns the landings into logical
  // S/N + value bytes (free iff a probe lands in a chunk already read this op). The cache is
  // thread-safe (a per-thread Mirror over a shared Bucket), so packed reads run at any P and the
  // logical counts stay the P=1 replay. One FanOut per op call (a fresh one per hop for read_chase),
  // registered with the prefixes the op can touch; each task builds its own ChunkReader on it. ---
  // `hit` (when given) receives the label whose row was found, and the row's _vid then carries it (a probe tags).
  // `st` (the task's store) books the used lanes, the bytes Condensed counts as used for the same request: the asked
  // cells' framed bytes; a consumed entry's id bytes + its edge props; used_key 0 (the chunk key is shared). The
  // owner's own id inside the chunk is not counted.
  // `rb`: the call's props_row_names builder
  Value props_of_packed(CountingStore& st, ChunkReader& rd, uint64_t u, const std::vector<std::string>& props,
                        const std::vector<std::string>& labels, const RowBuilder& rb, std::string* hit = nullptr);
  // `src_val` (when given): the input value; each neighbour value then carries what its entry tells (far_label_id)
  ValueList neighbors_of_packed(CountingStore& st, ChunkReader& rd, uint64_t su, const std::string& etype,
                                const std::vector<bool>& dirs, const std::vector<std::string>& edge_props,
                                bool pairs, size_t used_first_n, const Value* src_val = nullptr);
  // the same appended to `out`, the rows over `item`'s one shape (read_neighbors: one list and one shape per step)
  void neighbors_of_packed_into(CountingStore& st, ChunkReader& rd, uint64_t su, const std::string& etype,
                                const std::vector<bool>& dirs, const std::vector<std::string>& edge_props,
                                bool pairs, size_t used_first_n, const Value* src_val, const NeighborItems& item,
                                ValueList& out);
  // `used` (when given) += the found row's framed cell bytes, every column (Condensed's list props_bytes)
  // `rb`: the caller's builder of `wanted` rows
  Value edge_props_of_packed(ChunkReader& rd, uint64_t out_su, const std::string& etype, uint64_t ordinal,
                             const std::vector<std::string>& wanted, const RowBuilder& rb, uint64_t* used = nullptr);

  // --- reads (dial-branched; index deferred) ---
  std::optional<std::vector<std::string>> read_unit(CountingStore& st, const std::string& key);
  // a props read's part that does not depend on the vertex, made once per op call over its candidate labels: the
  // grouped layouts' plan per label, or the singleton layout's attr ids per label and prop
  struct PropsPlan {
    bool grouped = false;                 // the layout's props are rows (Condensed, grouped), not one record per attr
    std::vector<std::string> row_props;   // grouped: the props without "id", in order
    bool want_id = false;                 // grouped: "id" asked (key-derived, first in the row)
    struct Label {
      std::string label;
      GroupPlan groups;                          // grouped
      uint8_t vtype = 0;                         // singleton: the label's id, its columns, per prop its attr id
      const std::vector<ColDecl>* cols = nullptr;
      std::vector<int> attr;
    };
    std::vector<Label> labels;
    const Label& of(const std::string& label) const;   // a label outside the plan's candidates throws
  };
  PropsPlan props_plan(const Component& layout, const std::vector<std::string>& props,
                       const std::vector<std::string>& labels) const;
  // a worker's reused buffers for props_of (WorkerSlot::scratch, one per op call): one row view, the cells
  struct PropsScratch {
    columns::View view;
    std::vector<Value> cells;
  };
  PropsScratch& props_scratch(WorkerSlot& slot, const PropsPlan& plan) const;
  // `rb`: the call's props_row_names builder; `plan` covers `labels` (a subset of the call's candidates)
  Value props_of(CountingStore& st, uint64_t u, const std::vector<std::string>& props,
                 const std::vector<std::string>& labels, const PropsPlan& plan, PropsScratch& scratch,
                 const RowBuilder& rb, std::string* hit = nullptr);
  // Split props: one value per asked prop, in order, into `vals`; over candidate labels the first hit decides the label
  // (returned; "" = no wanted prop hit). `plan`: props_plan over the same props, covering `labs`
  std::string split_props(CountingStore& st, uint64_t u, const std::vector<std::string>& props,
                          const std::vector<std::string>& labs, const PropsPlan& plan, ValueList& vals);
  bool split_exists(CountingStore& st, const std::string& label, uint64_t u);   // Split: the prefix SEEK
  Value read_vertex_by_id_split(StepExec& ex, const ValueList& cands, const std::vector<std::string>& labels);
  ValueList neighbors_of(CountingStore& st, uint64_t su, const std::string& etype,
                         const std::vector<bool>& dirs, const std::vector<std::string>& edge_props, bool pairs,
                         size_t used_first_n = SIZE_MAX, const Value* src_val = nullptr);
  void neighbors_of_into(CountingStore& st, uint64_t su, const std::string& etype,
                         const std::vector<bool>& dirs, const std::vector<std::string>& edge_props, bool pairs,
                         size_t used_first_n, const Value* src_val, const NeighborItems& item, ValueList& out);
  // the write paths' form: their vid is an arg (a str)
  ValueList neighbors_of(CountingStore& st, const std::string& src, const std::string& etype,
                         const std::vector<bool>& dirs, const std::vector<std::string>& edge_props, bool pairs,
                         size_t used_first_n = SIZE_MAX);
  Value read_vertex_by_id(StepExec& ex, const Value& vid, const std::vector<std::string>& labels);
  Value read_vertex_props(StepExec& ex, const ValueList& vids, const std::vector<std::string>& props,
                          const std::vector<std::string>& labels);
  Value read_anchor_props(StepExec& ex, const ValueList& cands, const std::vector<std::string>& props,
                          const std::vector<std::string>& labels);
  // a hop's decisions from the plan and the layout alone, made once per op call; predict mode walks the same shape
  struct HopShape {
    std::vector<bool> dirs;   // the walked directions, in the plan's order (an unowned one dropped)
    bool two = false;         // one direction Chunked, the other not: one pass each, in the directions' order
    bool packed = false;      // the one pass reads chunks (landings, replayed); unused when `two`
  };
  HopShape hop_shape(const std::string& etype, const std::string& dir, const std::vector<std::string>& owner_labels) const;
  friend struct StepPredictor;   // formats/meta/predict.cc: the cards, the truth reads and the rules
  friend struct Estimate;    // formats/meta/predict.cc: the estimate card (statistics)
  Value read_neighbors(StepExec& ex, const ValueList& srcs, const std::string& etype, const std::string& dir,
                       const std::vector<std::string>& edge_props, bool pairs, size_t used_first_n = SIZE_MAX,
                       uint64_t group = 0, const std::vector<std::string>& owner_labels = {},
                       FanOut* shared = nullptr);
  // the four operators' reads (formats/common.h OperatorReads; run_operator runs their meaning)
  Value closure_level(StepExec& ex, uint64_t level, const ValueList& frontier, const std::string& etype,
                      const std::string& dir) override;
  ChaseReads chase_reads(StepExec& ex, const std::string& etype, const std::string& dir) override;
  EdgeExistsReads edge_exists_reads(StepExec& ex, const Args& a) override;
  TopKReads top_k_reads(StepExec& ex, const kv::TopKArgs& t) override;

  // --- index ops (delegated to the controlled SortedIndex; same walks as nebula) ---
  Value read_vertices_by_label(StepExec& ex, const std::string& label, const Value& props, const Value& keep);
  Value read_filter_by_label_probe(StepExec& ex, const ValueList& vids, const std::vector<std::string>& labels);
  Value read_filter_by_label_index(StepExec& ex, const ValueList& vids, const std::vector<std::string>& labels);

  // --- write ops (meta/writes.cc): the 6 write ops, dispatched per component like the reads
  Value run_write_op(const Step& step, const Args& a, StepExec& ex);

  const Schema& schema_;
  Config cfg_;
  ChunkCodecs cc_;   // the packed-chunk codec (default = plain / delta_varint / raw_offsets = pertype_csr bytes)
  std::unique_ptr<IndexBackend> index_;   // the controlled/fair index (nebula-equivalent SortedIndex)
  mutable std::unique_ptr<MetaParts> parts_;   // the per-part attribution scheme (run.parts), lazily
  mutable std::once_flag parts_once_;

  std::set<std::string> indexed_, date_preds_, etypes_, reverse_;
  std::map<std::string, std::vector<ColDecl>> ecols_;   // an etype's declared prop columns
  std::map<std::string, std::vector<ColDecl>> vcols_;   // a label's declared prop columns (no id: key-derived)
  std::map<std::string, RowGroups> row_groups_;           // a label's grouped-props rows (Config::vertex_groups)
  // per (etype, out) the far end's labels from the endpoints (sorted; a code is the index), and -- far_label on only --
  // the stored bits
  std::map<std::pair<std::string, bool>, std::vector<std::string>> far_labels_;
  std::map<std::pair<std::string, bool>, unsigned> far_bits_;
  std::map<std::pair<std::string, bool>, FarIds> far_ids_;

  // ingest: per-label row buffer -> its run at the label's end; edge buffers -> their runs at finalize
  LabelStream labels_{"meta"};   // the vertex stream: one label at a time (ingest/label_stream.h)
  std::unique_ptr<RowBuffer> cur_rows_;
  std::map<std::string, EdgeBuffer> edges_;
  std::map<std::string, std::unique_ptr<RowBuffer>> efacets_;
  // packed adjacency: partitioned by (etype, dir) -- no label (keys.h, family D). Own placement: ep rows keyed by
  // their edge's number in the etype's OUT buffer (peprops_). Inline placement: each edge's props once in
  // efacets_[etype] (row = the edge's number), and per partition the edge number of each appended pair (pinline_),
  // so both directions' chunks read the same row.
  using EdgePart = std::pair<std::string, bool>;   // (etype, out)
  std::map<EdgePart, EdgeBuffer> padj_;
  std::map<std::string, std::unique_ptr<RowBuffer>> peprops_;
  std::map<EdgePart, std::vector<uint32_t>> pinline_;
  // far_label: each appended entry's far-end label code, per (etype, dir) that stores them (far_bits > 0) -- unpacked:
  // indexed by the edge's number in edges_[etype]; packed: parallel to padj_[part]
  std::map<EdgePart, std::vector<uint8_t>> ufar_, pfar_;
  void finalize_packed_adjacency(KvSink& sink);   // adj + ep chunks, mirroring pertype_csr's finalize
  void finalize_inline_chunks(KvSink& sink, const EdgePart& key, const EdgeBuffer& eb,
                              const std::vector<uint32_t>& perm, uint64_t target);   // inline placement
  uint64_t n_vertices_ = 0, n_edges_ = 0;
  uint64_t n_split_units_ = 0, n_parts_ = 0;   // grouped adjacency postings written as parts (adjacency.split)
};

}  // namespace evalcpp::meta
