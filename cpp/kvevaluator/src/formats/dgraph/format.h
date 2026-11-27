// dgraph: the predicate-oriented posting store (Python formats/dgraph.py). Key = (predicate,
// kind, last): a scalar predicate holds one tagged value per vertex; an edge predicate holds
// a vertex's whole adjacency as one UidPack posting (+ facets); an index key holds the matching
// uids in its VALUE, one posting per term. The vid IS the uid (no fqid index: the anchor is a
// direct address, `id` is key-derived). Every op is independent SEEKs / GETs, so counts are
// all DIRECT.
#pragma once
#include <map>
#include <set>
#include <string>
#include <vector>

#include "codec/props_codec.h"
#include "ingest/label_stream.h"
#include "codec/posting.h"
#include "formats/common.h"
#include "formats/format.h"
#include "formats/rows.h"
#include "ingest/buffers.h"
#include "schema/schema.h"
#include "ops/kv_contract.h"

namespace evalcpp { class NeighborItems; }   // formats/common.h: read_neighbors' items over one shape

namespace evalcpp::dgraph {

// the index term of a value (Python str(value)): decimal for ints, the string itself for strs
std::string term_of_value(const Value& v);

struct Config {
  // the chunk knob (the dgraph-fairness arm): a posting unit -- an adjacency, an index term --
  // whose encoding exceeds chunk_target splits into parts (store/unit_value.h), as meta's
  // Condensed lists do. 0 = unbounded: one KV per posting whatever its size, real dgraph's layout and the
  // regression anchor (`dgraph`); 65536 = the `dgraph-c64k` store, apples-to-apples with
  // pertype_csr-c64k. Emitted to the manifest only when finite, so the anchor's stays {}.
  uint64_t chunk_target = 0;
  bool chunked() const { return chunk_target > 0; }
  // the facet codec of every adjacency entry (codec/props_codec.h): schema = the etype's RowV2 row,
  // named = dgraph's real facets (every name per edge). Always in the manifest (store identity).
  PropsCodec props_codec = kPropsCodecDefault;
  static Config from_json(const json& j);
  json to_json() const;
};

class Dgraph : public Format, public OperatorReads {
 public:
  Dgraph(const Schema& schema, const Config& cfg);
  std::string name() const override { return "dgraph"; }
  // "realistic" + part-start: a chunked arm's split parts are keyed by their range's start uid (unit_value.h), was a
  // part number. One stamp for every arm: the unbounded store's keys did not move, but it is re-ingested too
  static constexpr const char* kKeyLayout = "realistic-part-start";
  std::string key_codec() const override { return kKeyLayout; }
  std::set<std::string> capabilities() const override;
  bool has_edge_type(const std::string& name) const override;
  std::set<std::string> edge_capabilities_for(const std::string& etype) const override;
  std::set<std::string> capabilities_for(const std::string& label) const override;
  bool has_kv_op(const std::string& op) const override;
  Value run_kv_op(const Step& step, const Args& a, StepExec& ex) override;

  void add_vertex(KvSink& sink, uint64_t uid, const std::string& label, const Value& props) override;
  void add_edge(KvSink& sink, const std::string& etype, uint64_t src, uint64_t dst, const Value& props,
                const std::string& src_label, const std::string& dst_label) override;
  void finalize(KvSink& sink) override;
  json ingest_counts() const override;
  KeyClass classify(std::string_view key) const override;   // no type decoded: the part from the role (role_class)
  json codec_config() const override { return cfg_.to_json(); }

  // --- write COMPOSITION hooks (delete_vertex is built from these -- Python _w_delete_incident's
  // composition-point role; step-3 S8). Both append the
  // tombstone/rewrite PUTS for their slice of a delete into `puts` and charge their reads
  // on `st`; the caller applies everything at op end (dels before puts, as base._apply_writes).
  using WritePuts = std::vector<std::pair<std::string, std::string>>;
  using WriteDels = std::vector<std::string>;   // key-only index entries go by DELETE (nebula's rule)
  // the vertex's OWN keys + index tokens (delete_vertex's with_edges=false half)
  void w_delete_vertex_own(CountingStore& st, const std::string& vid, const std::string& label,
                           WritePuts& puts, WriteDels& dels);
  // one incident etype's sweep: our own posting tombed, each neighbour's SHARED posting
  // rewritten minus us (entry mode; self-loops tomb -- the guarded branch)
  void w_delete_incident(CountingStore& st, const std::string& vid, const std::string& label,
                         const std::string& etype, bool out, WritePuts& puts);

  // the index-posting codec (codec/posting.h); adjacency postings go through adj_codec(), whose
  // facet codec is this store's Config.props_codec -- no default, so every call site names it
  static std::string encode_posting(const std::vector<uint64_t>& uids) { return posting::encode_posting(uids); }
  static std::vector<uint64_t> decode_posting(std::string_view raw) { return posting::decode_posting(raw); }
  static std::vector<uint64_t> decode_posting_w(std::string_view raw, std::vector<uint8_t>* widths) {
    return posting::decode_posting_w(raw, widths);
  }
  // the facet codec of (etype, kind)'s postings: named, or schema over the etype's declared columns
  // (row_timestamp 0: dgraph has no Split edge record whose timestamp a list entry would mirror).
  // Virtual: a subclass's inline copy may add a column on the copy side.
  virtual posting::AdjCodec adj_codec(const std::string& etype, char kind) const;

 protected:
  // Which adjacency form this format WRITES for `etype`. Base dgraph: always the uid-ascending
  // 'A' form -- it IGNORES the schema's adj directives, exactly as Python's base DgraphFormat
  // does (the feature behavior belongs to the hybrid subclass, gated on the same schema).
  virtual bool adj_ordered(const std::string&) const { return true; }

  // The hybrid subclass reaches the read/write/ingest seams below (its features re-route
  // WHERE a value lives, not what the ops mean), mirroring the Python subclass's override
  // surface: props_of (group-aware reads -- virtual so every prop fetch incl. the top-k
  // winner path routes through it), run_write_op, add_vertex, and flush_adjacency.
  struct Neighbor { std::string vid; Value facets; };
  // the posting-list read path: 1 SEEK, the whole value at one key, 0 NEXT (nullopt = absent)
  std::optional<std::string> read_list(CountingStore& st, const std::string& key);
  // a posting UNIT (adjacency, index term): ∞ = read_list wrapped (1 SEEK, the whole); chunked
  // = the unit's parts in order (1 SEEK + N = parts). Callers decode each part and concatenate.
  std::optional<std::vector<std::string>> read_unit(CountingStore& st, const std::string& key);
  // one src's adjacency in the asked kinds (D and/or R); the str form is the write paths' (their vid is an arg)
  ValueList neighbors_of(CountingStore& st, uint64_t su, const std::string& etype,
                         const std::vector<char>& kinds, const std::vector<std::string>& edge_props, bool pairs,
                         size_t used_first_n = SIZE_MAX);
  // the same appended to `out`, the rows over `item`'s one shape (read_neighbors: one list and one shape per step)
  void neighbors_of_into(CountingStore& st, uint64_t su, const std::string& etype, const std::vector<char>& kinds,
                         const std::vector<std::string>& edge_props, bool pairs, size_t used_first_n,
                         const NeighborItems& item, ValueList& out);
  ValueList neighbors_of(CountingStore& st, const std::string& src, const std::string& etype,
                         const std::vector<char>& kinds, const std::vector<std::string>& edge_props, bool pairs,
                          size_t used_first_n = SIZE_MAX);
  std::vector<char> kinds_of(const std::string& op, const std::string& etype, const std::string& dir) const;
  // one vertex's props: id derived, list predicates a SEEK each, scalars a GET each; `rb` is the call's builder of
  // props_names(props) rows (the 3-arg form makes one for this row)
  virtual Value props_of(CountingStore& st, uint64_t u, const std::vector<std::string>& props, const RowBuilder& rb);
  Value props_of(CountingStore& st, uint64_t u, const std::vector<std::string>& props);

 private:
  Value read_neighbors(StepExec& ex, const ValueList& srcs, const std::string& etype, const std::string& dir,
                       const std::vector<std::string>& edge_props, bool pairs, size_t used_first_n = SIZE_MAX);
  Value read_vertex_props(StepExec& ex, const ValueList& vids, const std::vector<std::string>& props,
                          const std::string& label = "");
  Value read_index_eq(StepExec& ex, const std::string& label, const std::string& prop, const Value& value);
  Value read_index_range(StepExec& ex, const std::vector<std::string>& labels, const std::string& prop,
                         int64_t lo, int64_t hi, bool buckets);
  Value read_vertices_by_label(StepExec& ex, const std::string& label, const Value& props);
  Value read_filter_by_label_probe(StepExec& ex, const ValueList& vids, const std::vector<std::string>& labels);
  Value read_filter_by_label_index(StepExec& ex, const ValueList& vids, const std::vector<std::string>& labels);
  // the four operators' reads (formats/common.h OperatorReads; run_operator runs their meaning)
  Value closure_level(StepExec& ex, uint64_t level, const ValueList& frontier, const std::string& etype,
                      const std::string& dir) override;
  ChaseReads chase_reads(StepExec& ex, const std::string& etype, const std::string& dir) override;
  EdgeExistsReads edge_exists_reads(StepExec& ex, const Args& a) override;
  TopKReads top_k_reads(StepExec& ex, const kv::TopKArgs& t) override;
  // top_k's three walks, one per date-index design (index.cc): the predicate-global day-token walk, the key-sorted
  // walk, and the per-type day walks (TYPE-SCOPED, Python _read_top_k_type_scoped, and VALUE-SORTED,
  // _read_top_k_value_sorted): one cursor per label's band, merged newest-first by day token, stopping at the k-th DAY
  // boundary (the type-scoped walk GETs each hit's exact ms; the value-sorted one reads it from the bucket)
  TopKReads::Walk top_k_walk_day_global(const kv::TopKArgs& t);
  TopKReads::Walk top_k_walk_key_sorted(const kv::TopKArgs& t);
  TopKReads::Walk top_k_walk_day_scoped(const kv::TopKArgs& t, bool value_sorted);

 protected:
  virtual Value run_write_op(const Step& step, const Args& a, StepExec& ex);   // writes.cc

  const Schema& schema_;
  Config cfg_;
  std::set<std::string> indexed_, date_preds_, list_preds_, etypes_, reverse_;   // per-PREDICATE views
  // The opt-in index KEYING (schema.json's type_scoped / key_sorted flags on an IndexDecl; the
  // full_index design). type-scoped: the index of (label, prop) is keyed under its own predicate
  // `Label.prop`, so the index IS the type restriction. key-sorted (a date index): one key-only
  // entry per vertex, the 8 B order-preserving ms then the 8 B uid in the key (nebula's
  // SortedIndex term), so a range / top-k walk reads exactly the entries it crosses and stops.
  // Both empty on canonical -> the predicate-global day-token index, unchanged.
  std::set<std::pair<std::string, std::string>> type_scoped_, key_sorted_;
  std::set<std::string> key_sorted_props_;   // the predicates key-sorted on SOME label (the no-labels guard)
  bool all_type_scoped_ = false;   // every property index is type-scoped -> per_type_index
  // the schema's `predicates per_label`: a vertex property's data key is `<Label>.<prop> 0x00 <uid>`, so a label's
  // values of one property are one whole predicate (one key run). Reads then need the label (data_pred)
  bool per_label_ = false;
  std::string data_pred(const std::string& label, const std::string& prop) const;
  Value props_at(CountingStore& st, uint64_t u, const std::vector<std::string>& props, const std::string& label,
                 const RowBuilder& rb);
  Value read_label_run(StepExec& ex, const std::string& label, const std::vector<std::string>& props);
  // does (label, prop) get an index entry? canonical: the predicate view -- ANY label carrying an
  // indexed / dated predicate lands in its global index (Forum.creationDate, Organisation.name:
  // undeclared, yet indexed, faithful to real dgraph's per-predicate @index). A type-scoped design
  // indexes only the DECLARED (label, prop) pairs: no other format holds an index the schema does
  // not declare, and the arm must not carry one either. The policy is PER DESIGN (any type-scoped
  // index flips the whole store to declared-only), not per column: a design that scopes some indexes and keeps others
  // predicate-global (type_scoped_date) gets declared-only entries in both, as Python's _index_entry.
  bool index_entry(const std::string& label, const std::string& prop) const {
    if (type_scoped_.empty()) return indexed_.count(prop) || date_preds_.count(prop);
    return schema_.is_indexed(label, prop) || schema_.is_date_col(label, prop);
  }
  bool all_key_sorted_ = false;    // every date index is key-sorted -> the range walk is exact (no unsorted cap)
  // the ATTR bytes an index of (label, prop) is keyed under: the predicate name (real length) on
  // canonical; under a type-scoped design the schema's 4 B index id -- nebula's folded index
  // identity, so the key band prices the same bytes as the three nebula-keyed formats. Prefix-free
  // against the string attrs: an id's first byte is 0x00, no predicate name starts with one.
  std::string index_pred(const std::string& label, const std::string& prop) const;
  // An index read on (labels, prop) that the schema does not declare is refused, never answered from an empty key
  // range: each label must declare an index on prop (schema index_decl's error, as the other formats); no label = a
  // bare predicate, declared when any label indexes it (Dgraph's index is per predicate; the engine refuses a
  // non-indexed predicate too). Called at every index read's entry.
  void require_index(const std::vector<std::string>& labels, const std::string& prop) const;
  // the index TERM of a value: str(value) on canonical; under a type-scoped design the shared
  // codec nebula's SortedIndex writes (index/sorted_index.h index_value_bytes: a VelocyPack string,
  // an 8 B order-preserving int), so a value index's key bytes match nebula's store byte for byte
  std::string index_term(const std::string& label, const std::string& prop, const Value& v) const;
  bool is_key_sorted(const std::string& label, const std::string& prop) const { return key_sorted_.count({label, prop}) > 0; }
  // The VALUE-SORTED date index (schema value_sorted, dim-2 A; implies type_scoped): each day bucket holds its
  // (uid, ms) pairs date-descending, so a top-k ranks -- and applies its exact hi -- with no per-candidate GET.
  std::set<std::pair<std::string, std::string>> value_sorted_;
  std::set<std::string> value_sorted_preds_;   // index_pred of each value-sorted (label, prop): a bucket's codec by key
  std::set<std::string> scoped_props_;         // the predicates type-scoped on SOME label (the day walks' no-labels guard)
  bool is_value_sorted(const std::string& label, const std::string& prop) const { return value_sorted_.count({label, prop}) > 0; }
  // the index bands a DAY-token walk of `prop` crosses: each label's own band when every label type-scopes it, else
  // the predicate-global band; a mix, or no labels on a type-scoped prop, throws (the bare band is empty under such a
  // design -- a silent []). Python _day_walk_preds.
  std::vector<std::string> day_walk_preds(const std::vector<std::string>& labels, const std::string& prop, const char* op) const;
  // a value-sorted bucket's value: uvarint n, n x u64be uid, n x prim ms (date-descending; Python's 'd' codec in shape)
  static std::string encode_value_sorted(const std::vector<std::pair<int64_t, uint64_t>>& ms_uid);
  static std::vector<std::pair<uint64_t, int64_t>> decode_value_sorted(std::string_view raw);
  // do `labels` key-sort `prop`? all of them -> true; none -> false; a mix throws (one walk per op)
  bool key_sorted_walk(const std::vector<std::string>& labels, const std::string& prop, const char* op) const;
  static std::string key_sorted_term(int64_t ms, uint64_t uid);                     // 16 B: sorted ms | uid
  static bool key_sorted_landing(std::string_view key, const std::string& pre);   // a 16 B term under `pre`
  static int64_t key_sorted_ms(std::string_view key, const std::string& pre);
  static uint64_t key_sorted_uid(std::string_view key);
  static int64_t day_number(int64_t ms);                                            // Python ms // 86400000
  std::map<std::string, std::vector<ColDecl>> ecols_;   // an etype's facet columns (its declared props)
  const std::vector<ColDecl>& ecols(const std::string& etype) const;

  // ingest (plan 6.2, streaming): the scalar postings of the CURRENT label (the record stream
  // groups vertices by label) are flushed as one run per predicate when the next label starts
  // -- runs of one predicate overlap across labels, the sink batches them; edges as (src, dst)
  // pairs per etype with the facet columns aligned (an adjacency posting needs every edge of a
  // vertex, so edges wait for finalize); index postings per (predicate, term), uids only.
  void flush_scalars(KvSink& sink);
  // finalize = flush_scalars + flush_adjacency + flush_indexes; the adjacency leg is the
  // virtual one (the hybrid's date sort / inline copy changes HOW a posting flushes)
  virtual void flush_adjacency(KvSink& sink);
  void flush_indexes(KvSink& sink);
  // one edge's facet bytes under `codec` (named: the prim dict of its present columns; schema: its
  // RowV2 row, "" for a prop-less etype) -- the piece a flush_adjacency override composes
  static std::string facet_bytes(const posting::AdjCodec& codec, const std::vector<ColDecl>& cols,
                                 const RowBuffer* buf, uint32_t row);
  LabelStream labels_{"dgraph"};   // the vertex stream: one label at a time (ingest/label_stream.h)
  std::map<std::string, std::vector<std::pair<uint64_t, std::string>>> cur_scalars_;
  std::map<std::string, EdgeBuffer> edges_;
  std::map<std::string, std::unique_ptr<RowBuffer>> efacets_;   // per prop-carrying etype, aligned with the pairs
  std::map<std::pair<std::string, std::string>, std::vector<uint64_t>> index_;
  std::map<std::pair<std::string, std::string>, std::vector<std::pair<int64_t, uint64_t>>> index_vs_;   // value-sorted buckets: (ms, uid)
  uint64_t n_vertices_ = 0, n_edges_ = 0;
};

}  // namespace evalcpp::dgraph
