#include "formats/pertype_csr/format.h"
#include "ingest/chunker.h"
#include "schema/far_labels.h"

#include <memory>
#include <unordered_map>
#include <algorithm>
#include <stdexcept>
#include <numeric>

#include "formats/common.h"
#include "formats/pertype_csr/keys.h"
#include "ir/expr.h"
#include "util/uidset.h"
#include "util/vid.h"

namespace evalcpp::pertype_csr {

Config Config::from_json(const json& j) {
  Config c;
  if (j.is_object()) {
    c.chunk_target = j.value("chunk_target", c.chunk_target);
    c.uids = j.value("uids", c.uids);
    c.strings = j.value("strings", c.strings);
    c.layout = j.value("layout", c.layout);
    c.index_layout = j.value("index_layout", c.index_layout);
    c.far_label = j.value("far_label", c.far_label);
  }
  ChunkCodecs::from_names(c.uids, c.strings, c.layout);   // validates the names
  return c;
}

json Config::to_json() const {
  return {{"chunk_target", chunk_target}, {"uids", uids}, {"strings", strings}, {"layout", layout},
          {"index_layout", index_layout}, {"far_label", far_label}};
}

bool PerTypeCsr::AdjKey::operator<(const AdjKey& o) const {
  // Python sorts (label, etype, dir) tuples with dir in {"in","out"}: "in" < "out"
  if (label != o.label) return label < o.label;
  if (etype != o.etype) return etype < o.etype;
  return int(out) < int(o.out);
}

PerTypeCsr::PerTypeCsr(const Schema& schema, const Config& cfg)
    : schema_(schema), cfg_(cfg), cc_(ChunkCodecs::from_names(cfg.uids, cfg.strings, cfg.layout)), index_(make_index_backend(schema, cfg.index_layout, cfg.chunk_target)) {
  for (auto& v : schema.vertices()) {
    vcols_[v.label] = vertex_cols(v);   // declared props only: id is key-derived, never stored
  }
  for (auto& e : schema.edges()) {
    std::vector<ColDecl> cols;
    for (auto& p : e.props) cols.push_back({p.name, p.type, false});
    ecols_[e.etype] = cols;
    far_[{e.etype, true}] = schema.far_labels(e.etype, true);
    far_[{e.etype, false}] = schema.far_labels(e.etype, false);
    if (cfg_.far_label)   // a code is one byte: the same refusal as meta's (schema/far_labels.h)
      for (bool out : {true, false}) check_far_label_count(far_[{e.etype, out}], e.etype, "pertype_csr");
  }
}

const std::vector<std::string>& PerTypeCsr::far_names(const std::string& etype, bool out) const {
  auto it = far_.find({etype, out});
  if (it == far_.end()) throw std::runtime_error("pertype_csr: unknown edge type '" + etype + "'");
  return it->second;
}

unsigned PerTypeCsr::far_bits(const std::string& etype, bool out) const {
  return cfg_.far_label ? far_code_bits(far_names(etype, out).size()) : 0;
}

uint8_t PerTypeCsr::far_code(const std::string& etype, bool out, const std::string& label, const char* what) const {
  return far_label_code(far_names(etype, out), label, etype, out, (std::string("pertype_csr ") + what).c_str());
}

std::set<std::string> PerTypeCsr::capabilities() const {
  // a per-type index (single-posting under the posting layout), a sorted date index, whole rows;
  // a label scan walks the chunks themselves, so it projects props in the same pass
  std::set<std::string> caps = {"sorted_range_index", "per_type_index", "type_index", "whole_row_read",
                                "label_scan_carries_props", "label_rows_one_run", "whole_label_scan_by_default",
                                "label_scoped_props"};   // a chunk run per type
  // the posting index holds a type's whole membership in ONE unit (dgraph's rule: every label
  // declares the type index); sorted_keys never grants it (one entry per vertex)
  if (index_->single_posting_type_index()) caps.insert("type_index_single_posting");
  return caps;
}

const std::vector<ColDecl>& PerTypeCsr::vcols(const std::string& label) const {
  auto it = vcols_.find(label);
  if (it == vcols_.end()) throw std::runtime_error("pertype_csr: unknown label '" + label + "'");
  return it->second;
}

const std::vector<ColDecl>& PerTypeCsr::ecols(const std::string& etype) const {
  auto it = ecols_.find(etype);
  if (it == ecols_.end()) throw std::runtime_error("pertype_csr: unknown edge type '" + etype + "'");
  return it->second;
}

// The shared per-op chunk cache (SharedChunkCache / ChunkReader / BucketTable / FanOut) lives in
// neutral infra, exec/chunk_cache.h, shared with the meta format. Reader is an alias for ChunkReader
// (format.h). Only the format-specific prefix registration stays here.

// register every prefix an op over vertex rows / adjacency / edge props can touch
void register_props(FanOut& f, const Schema& schema) {
  for (auto& v : schema.vertices()) f.table.add(f.cache, props_prefix(v.id));
}

void register_adj(FanOut& f, const Schema& schema, const std::string& etype) {
  int eid = schema.edge(etype).id;
  for (auto& v : schema.vertices()) {
    f.table.add(f.cache, adj_prefix(v.id, eid, true));
    f.table.add(f.cache, adj_prefix(v.id, eid, false));
    f.table.add(f.cache, ep_prefix(v.id, eid));
  }
}

bool PerTypeCsr::has_edge_type(const std::string& name) const {
  for (auto& e : schema_.edges())
    if (e.etype == name) return true;
  return false;
}

// both directions of every edge type, each a chunked list
std::set<std::string> PerTypeCsr::edge_capabilities_for(const std::string&) const { return {"reverse_adjacency", "adjacency_lists"}; }

// the contract's reads every format serves (ops/kv_contract.h), and the writes
bool PerTypeCsr::has_kv_op(const std::string& op) const { return kv::is_every_format_read_op(op) || is_write_op(op); }

Value PerTypeCsr::run_kv_op(const Step& step, const Args& a, StepExec& ex) {
  const std::string& op = step.op;
  if (op == "read_vertex_by_id") {
    // the vid IS the id: 0 ops; existence and type are resolved by the read that follows
    const Value& vid = a.get("vid");
    if (vid.is_text()) return Value::list({vid});
    return Value::list(vid.as_list());
  }
  if (op == "read_vertex_props") return read_vertex_props(ex, a.list("vids"), a.str_list("props"), a.labels("label"));
  if (op == "read_anchor_props") return read_anchor_props(ex, a.list("cands"), a.str_list("props"), a.labels("labels"));
  if (op == "read_neighbors") {
    kv::NeighborsArgs n = kv::neighbors_args(a);
    return read_neighbors(ex, 0, *n.srcs, n.etype, n.dir, n.edge_props, n.pairs);
  }
  if (op == "read_index_eq" || op == "read_index_range") {
    kv::IndexRangeArgs ir = op == "read_index_range" ? kv::index_range_args(a, op) : kv::IndexRangeArgs{};
    std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {   // one index walk
      Counts before = slot.store.counts();
      // a label's index holds only that label's vertices: each vid it gives carries the label. A range over several
      // labels is read label by label -- the walk the index does anyway, in the same order
      if (op == "read_index_eq") {
        r.out = tag_all(schema_, Value::list(index_->eq(slot.store, a.str("label"), a.str("prop"), a.get("value"))), a.str("label"));
      } else {
        ValueList out;
        RtParallel par(slot.store);   // one index walk per label, the labels are the plan's
        for (auto& lab : ir.labels) {
          par.member();
          for (auto& v : index_->range(slot.store, {lab}, ir.prop, ir.lo, ir.hi))
            out.push_back(tagged(schema_, v, lab));
        }
        r.out = Value::list(std::move(out));
      }
      direct_counts(slot, before, r);
    }};
    return ex.run(tasks)[0].out;
  }
  if (op == "read_vertices_by_label") return read_vertices_by_label(ex, a.str("label"), a.opt("props"), a.opt("keep"));
  if (op == "read_filter_by_label_probe") return read_filter_by_label_probe(ex, a.list("vids"), a.labels("label"));
  if (op == "read_filter_by_label_index") return read_filter_by_label_index(ex, a.list("vids"), a.labels("label"));
  if (std::optional<Value> v = run_operator(*this, schema_, op, a, ex)) return *v;   // closure, chase, edge exists, top k
  if (is_write_op(op)) return run_write_op(step, a, ex);
  throw std::runtime_error("pertype_csr: unknown kv op '" + op + "'");
}

// 't' label:u16 then 'p' props chunk | 'a' etype:u16 'o'|'i' adjacency chunk | 'e' edge-props chunk; 'i' = the
// value / type index (nebula's key shape, the layout's own value). The type: 't' label:u16 'p' -> the vertex label;
// 't' label:u16 'a'|'e' etype:u16 -> the edge type; 'i' index_id -> its label.
KeyClass PerTypeCsr::classify(std::string_view key) const {
  if (key.size() >= 4 && key[0] == 't') {
    if (key[3] == 'p')
      return {"vertex_props", vertex_label_of_id(schema_, (uint32_t(uint8_t(key[1])) << 8) | uint8_t(key[2])), "", "",
              "", "vertex", "chunk-props"};
    std::string etype = key.size() >= 6 ? etype_of_id(schema_, (uint32_t(uint8_t(key[4])) << 8) | uint8_t(key[5])) : "";
    if (key[3] == 'a' && key.size() >= 7)
      return {"adjacency", etype, "", "", "", "edge", key[6] == 'o' ? "chunk-adj-out" : "chunk-adj-in"};
    if (key[3] == 'e') return {"edge_props", etype, "", "", "", "edge", "chunk-edgeprops"};
  }
  if (!key.empty() && key[0] == 'i') return index_key_class(schema_, key);
  return {"unclassified", "", "", "", "", "other", "other"};
}

// 't' label:u16 'a' etype:u16 'o'|'i' first-owner:u64 -> the chunk's entries with the label each stores
std::vector<ValueInspector::FarLabel> PerTypeCsr::far_label_entries(std::string_view key, std::string_view value) const {
  if (key.size() < 7 || key[0] != 't' || key[3] != 'a') return {};
  std::string etype = etype_of_id(schema_, (uint32_t(uint8_t(key[4])) << 8) | uint8_t(key[5]));
  bool out = key[6] == 'o';
  unsigned bits = etype.empty() ? 0 : far_bits(etype, out);
  if (!bits) return {};
  const std::vector<std::string>& far = far_names(etype, out);
  std::string fam = etype + (out ? ":out" : ":in");
  std::shared_ptr<AdjChunk> c = AdjChunk::decode(&cc_, std::string(value), !ecols(etype).empty(), bits);
  std::vector<FarLabel> got;
  for (size_t i = 0; i < c->size(); i++) {
    std::vector<uint64_t> dsts = c->dsts(i);
    std::vector<uint8_t> codes = c->labels(i);
    for (size_t j = 0; j < dsts.size(); j++) got.emplace_back(fam, c->src(i), dsts[j], far.at(codes[j]));
  }
  return got;
}

}  // namespace evalcpp::pertype_csr
