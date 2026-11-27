#include "formats/nebula/format.h"

#include <algorithm>
#include <numeric>
#include <optional>
#include <stdexcept>

#include <unordered_map>

#include "exec/label_tags.h"
#include "formats/common.h"
#include "formats/nebula/keys.h"
#include "codec/rowv2.h"
#include "util/uidset.h"
#include "util/vid.h"

namespace evalcpp::nebula {

Config Config::from_json(const json& j) {
  Config c;
  if (j.is_object()) c.row_timestamp = j.value("row_timestamp", c.row_timestamp);
  if (j.is_object()) c.key_order = j.value("key_order", c.key_order);
  parse_key_order(c.key_order);                                 // throws on anything but vid_first | type_first
  if (c.row_timestamp < 0) throw std::runtime_error("nebula: row_timestamp must be >= 0");
  return c;
}

json Config::to_json() const { return {{"row_timestamp", row_timestamp}, {"key_order", key_order}}; }

Nebula::Nebula(const Schema& schema, const Config& cfg) : schema_(schema), cfg_(cfg), index_(schema) {
  keys_.order = parse_key_order(cfg.key_order);
  for (auto& v : schema.vertices()) {
    vcols_[v.label] = vertex_cols(v);   // declared props only: id is key-derived, never stored
  }
  for (auto& e : schema.edges()) {
    std::vector<ColDecl> cols;
    for (auto& p : e.props) cols.push_back({p.name, p.type, false});
    ecols_[e.etype] = cols;
  }
}

std::set<std::string> Nebula::capabilities() const {
  // a per-vertex type index (never type_index_single_posting), a sorted date index, whole rows; props
  // keyed by tag (keys.h vertex_key(tag_id, vid)): label_scoped_props
  return {"sorted_range_index", "per_type_index", "type_index", "whole_row_read", "label_scoped_props"};
}

const std::vector<ColDecl>& Nebula::vcols(const std::string& label) const {
  auto it = vcols_.find(label);
  if (it == vcols_.end()) throw std::runtime_error("nebula: unknown label '" + label + "'");
  return it->second;
}

const std::vector<ColDecl>& Nebula::ecols(const std::string& etype) const {
  auto it = ecols_.find(etype);
  if (it == ecols_.end()) throw std::runtime_error("nebula: unknown etype '" + etype + "'");
  return it->second;
}

std::vector<std::string> Nebula::labels_or_throw(const std::string& op, const std::vector<std::string>& label) const {
  if (label.empty()) throw std::runtime_error("nebula " + op + " needs label (a tag name or candidate list)");
  for (auto& l : label) vcols(l);
  return label;
}

bool Nebula::has_edge_type(const std::string& name) const {
  for (auto& e : schema_.edges())
    if (e.etype == name) return true;
  return false;
}

// both directions of every edge type are stored, each edge its own key: in reads valid, point GETs (no lists)
std::set<std::string> Nebula::edge_capabilities_for(const std::string&) const { return {"reverse_adjacency"}; }

// the contract's reads every format serves (ops/kv_contract.h), and the writes
bool Nebula::has_kv_op(const std::string& op) const { return kv::is_every_format_read_op(op) || is_write_op(op); }

Value Nebula::run_kv_op(const Step& step, const Args& a, StepExec& ex) {
  const std::string& op = step.op;
  if (op == "read_vertex_by_id") return read_vertex_by_id(ex, a.get("vid"), a.labels("label"));
  if (op == "read_vertex_props") return read_vertex_props(ex, a.list("vids"), a.str_list("props"), a.labels("label"));
  if (op == "read_anchor_props") return read_anchor_props(ex, a.list("cands"), a.str_list("props"), a.labels("labels"));
  if (op == "read_neighbors") {
    kv::NeighborsArgs n = kv::neighbors_args(a);
    return read_neighbors(ex, *n.srcs, n.etype, n.dir, n.edge_props, n.pairs);
  }
  if (op == "read_index_eq" || op == "read_index_range") {
    kv::IndexRangeArgs ir = op == "read_index_range" ? kv::index_range_args(a, op) : kv::IndexRangeArgs{};
    if (op == "read_index_range") labels_or_throw("read_index_range", ir.labels);   // each label is the schema's
    std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {   // one index walk
      Counts before = slot.store.counts();
      // a tag's index holds only that tag's vertices: each vid it gives carries the tag (source (c) of the rule). A
      // range over several tags is read tag by tag -- the walk the index does anyway, in the same order -- so each
      // vid knows which tag's index it came from
      if (op == "read_index_eq") {
        r.out = tag_all(schema_, Value::list(index_.eq(slot.store, a.str("label"), a.str("prop"), a.get("value"))), a.str("label"));
      } else {
        ValueList out;
        RtParallel par(slot.store);                         // one member per label's walk
        for (auto& lab : ir.labels) {
          par.member();
          for (auto& v : index_.range(slot.store, {lab}, ir.prop, ir.lo, ir.hi))
            out.push_back(tagged(schema_, v, lab));
        }
        r.out = Value::list(std::move(out));
      }
      direct_counts(slot, before, r);
    }};
    return ex.run(tasks)[0].out;
  }
  if (op == "read_vertices_by_label") {
    if (a.has("keep")) throw std::runtime_error("nebula read_vertices_by_label takes no keep (no scan of a label's rows here)");
    return read_vertices_by_label(ex, a.str("label"), a.opt("props"));
  }
  if (op == "read_filter_by_label_probe") return read_filter_by_label_probe(ex, a.list("vids"), a.labels("label"));
  if (op == "read_filter_by_label_index") return read_filter_by_label_index(ex, a.list("vids"), a.labels("label"));
  if (std::optional<Value> v = run_operator(*this, schema_, op, a, ex)) return *v;   // closure, chase, edge exists, top k
  if (is_write_op(op)) return run_write_op(step, a, ex);
  throw std::runtime_error("nebula: unknown kv op '" + op + "'");
}

// 'v' tag:u32 vid:u64 = a vertex row (a longer key, a prop-group sub-row, is not written); 'e' = edge data in both
// directions (the etype's top bit marks the reverse side); 'i' = the shared value / type index. The type: the tag of
// a 'v' key -> the vertex label, the etype of an 'e' key -> the edge type (keys.h: where that u32 sits follows the
// store's key order), 'i' index_id -> its label.
KeyClass Nebula::classify(std::string_view key) const {
  if (key.empty()) return {"unclassified", "", "", "", "", "other", "other"};
  if (key[0] == 'v') {
    std::string type = key.size() >= 13 ? vertex_label_of_id(schema_, keys_.type_field(key)) : "";
    return {"vertex_props", type, "", "", "", "vertex", key.size() > 13 ? "vertex-prop-split" : "vertex-row"};
  }
  if (key[0] == 'e' && key.size() >= 13) {
    uint32_t field = keys_.type_field(key);
    return {"adjacency", etype_of_id(schema_, field & 0x7fffffffu), "", "", "", "edge",
            (field & 0x80000000u) ? "edge-rev" : "edge-fwd"};
  }
  if (key[0] == 'i') return index_key_class(schema_, key);
  return {"unclassified", "", "", "", "", "other", "other"};
}

}  // namespace evalcpp::nebula
