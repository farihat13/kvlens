#include "formats/arangodb/format.h"

#include <algorithm>
#include <optional>
#include <stdexcept>
#include <unordered_map>
#include <unordered_set>

#include "exec/label_tags.h"

#include "codec/rowv2.h"
#include "formats/arangodb/keys.h"
#include "formats/arangodb/vpack.h"
#include "formats/common.h"
#include "index/sorted_index.h"
#include "ir/expr.h"
#include "util/uidset.h"
#include "util/vid.h"

namespace evalcpp::arangodb {

static bool is_sys_member(const std::string& n) {   // VPack's 1-byte dictionary members (vpack.cc kSysNames)
  return n == "_key" || n == "_rev" || n == "_id" || n == "_from" || n == "_to";
}

Config Config::from_json(const json& j) {
  Config c;
  c.props_codec = props_codec_from_json(j);
  return c;
}

json Config::to_json() const { return {{"props_codec", props_codec_name(props_codec)}}; }

Arango::Arango(const Schema& schema, const Config& cfg) : schema_(schema), cfg_(cfg) {
  // the edge-doc coll offset (keys.h) is only disjoint while every raw id stays below it
  for (auto& v : schema.vertices())
    if (uint32_t(v.id) >= kEdgeDocColl)
      throw std::runtime_error("arango: vertex-label id >= kEdgeDocColl -- doc keys would collide");
  for (auto& e : schema.edges())
    if (uint32_t(e.id) >= kEdgeDocColl)
      throw std::runtime_error("arango: edge-type id >= kEdgeDocColl -- edge_doc_coll would wrap");
  for (auto& v : schema.vertices()) {
    for (auto& p : v.props)
      if (is_sys_member(p.name) || p.name == "id")
        throw std::runtime_error("arango: vertex prop '" + p.name + "' of " + v.label + " collides with a system / id field");
    vdoc_cols_[v.label] = vertex_cols(v);   // declared props only: id is key-derived (_key), never a member
  }
  for (auto& e : schema.edges()) {
    std::vector<ColDecl> cols;
    for (auto& p : e.props) {
      // an edge prop named like a system field would duplicate a member (named) or overwrite an endpoint on update
      if (is_sys_member(p.name))
        throw std::runtime_error("arango: edge prop '" + p.name + "' of " + e.etype + " collides with a system field");
      cols.push_back({p.name, p.type, false});
    }
    ecols_[e.etype] = cols;
  }
}

std::set<std::string> Arango::capabilities() const {
  // the type lives in the collection-qualified id, an always-on per-type structure: the type
  // filter is free by either op, so the index-variant cap costs arango nothing
  // index_entries_by_docid: the persistent index keys by the internal docid (the _key lives
  // in the doc), so a candidate-filtered index walk pays a document GET per crossed entry
  // (read_top_k_by_index below) -- the property that prices one-phase reads cheaper here
  // label_scoped_props: a vertex is a document in its label's collection (keys.h doc_key(coll_id, docid))
  // label_rows_one_run: a collection's documents are one key run ('D' coll docid), so one sequential scan of the run
  // may read the label with its props (read_vertices_by_label with props). NOT held:
  // whole_label_scan_by_default -- the scan is a candidate here (the person-run twins, ic10's pin), not the default
  // plan of the microbench whole-label scans, until the other formats' scans are sized
  return {"sorted_range_index", "per_type_index", "type_index", "whole_row_read",
          "type_index_single_posting", "index_entries_by_docid", "label_scoped_props", "label_rows_one_run"};
}

// --- index term bytes. A str term is the VelocyPack string a real ArangoDB persistent index
// keys on (variable width: 1-byte header + the bytes); the shared FIXED-width padding is
// Nebula's FIXED_STRING rule and over-charged arango's index scans by the pad.
// Scans visit the same keys either way: the header carries the length, so an equality prefix
// is exact. Python's tuple parts are self-delimiting and price the raw length. -----------------

std::string Arango::index_prefix(const std::string& label, const std::string& prop) const {
  return IndexKeys::prefix(schema_.index_decl(label, prop).id);
}

std::string Arango::term_bytes(const std::string& label, const std::string& prop, const Value& v, bool query) const {
  const IndexDecl& ix = schema_.index_decl(label, prop);
  if (ix.kind == "sorted") return IndexKeys::sorted_bytes(v.as_int());
  std::string type;
  for (auto& p : schema_.vertex(label).props)
    if (p.name == ix.prop) type = p.type;
  if (type == "str") {
    // a vid is its hex16 str (vpack decodes a 16-hex str as one): the same term as ingest's str
    std::string s = v.is_text() ? v.text() : (v.is_int() ? std::to_string(v.as_int()) : repr(v));
    (void)query;                                   // ingest and query encode identically
    return IndexKeys::vpack_str_bytes(s);
  }
  return IndexKeys::sorted_bytes(v.as_int());
}

std::string Arango::index_write_key(const std::string& label, const std::string& prop, const Value& v, uint64_t docid) const {
  // a column with no value still gets an entry, under the NULL term (Python _index_key's empty term sorts first too):
  // nulls are one block before every value, as in a real ArangoDB persistent index (VelocyPack null sorts first)
  return IndexKeys::key(index_prefix(label, prop),
                        v.is_null() ? index_null_bytes(schema_, schema_.index_decl(label, prop), label)
                                    : IndexKeys::refuse_null_slot(term_bytes(label, prop, v, false), "write " + label + "." + prop),
                        docid);
}

bool Arango::has_edge_type(const std::string& name) const {
  for (auto& e : schema_.edges())
    if (e.etype == name) return true;
  return false;
}

// the from / to edge indexes hold both directions; an edge's document id is not a function of its endpoints, so
// there is no per-edge key to probe: the lists are what a membership check reads
std::set<std::string> Arango::edge_capabilities_for(const std::string&) const { return {"reverse_adjacency", "adjacency_lists"}; }

// the contract's reads every format serves (ops/kv_contract.h), and the writes
bool Arango::has_kv_op(const std::string& op) const { return kv::is_every_format_read_op(op) || is_write_op(op); }

Value Arango::run_kv_op(const Step& step, const Args& a, StepExec& ex) {
  const std::string& op = step.op;
  if (op == "read_vertex_by_id") return read_vertex_by_id(ex, a.get("vid"), a.labels("label"));
  if (op == "read_vertex_props") return read_vertex_props(ex, a.list("vids"), a.str_list("props"), a.labels("label"));
  if (op == "read_anchor_props") return read_anchor_props(ex, a.list("cands"), a.str_list("props"), a.labels("labels"));
  if (op == "read_neighbors") {
    kv::NeighborsArgs n = kv::neighbors_args(a);
    return read_neighbors(ex, *n.srcs, n.etype, n.dir, n.edge_props, n.pairs);
  }
  if (op == "read_index_eq") return read_index_eq(ex, a.str("label"), a.str("prop"), a.get("value"));
  if (op == "read_index_range") {
    kv::IndexRangeArgs ir = kv::index_range_args(a, op);
    return read_index_range(ex, ir.labels, ir.prop, ir.lo, ir.hi);
  }
  if (op == "read_vertices_by_label") return read_vertices_by_label(ex, a.str("label"), a.opt("props"), a.opt("keep"));
  if (op == "read_filter_by_label_probe" || op == "read_filter_by_label_index")
    return read_filter_by_label_probe(ex, a.list("vids"), a.labels("label"));   // free either way
  if (std::optional<Value> v = run_operator(*this, schema_, op, a, ex)) return *v;   // closure, chase, edge exists, top k
  if (is_write_op(op)) return run_write_op(step, a, ex);
  throw std::runtime_error("arangodb: unknown kv op '" + op + "'");
}

// --- documents (Config.props_codec): one rule for vertex and edge docs ---------------------------

std::string Arango::doc_encode(const std::vector<ColDecl>& cols, RowFields sys, const Value& props,
                               const std::string& what) const {
  std::string doc;
  if (cfg_.props_codec == PropsCodec::named) {       // arango's document: every prop a named member
    if (props.is_row())
      for (auto& kv : props.as_row()) sys.emplace_back(kv.first, kv.second);
    vp_encode_obj(doc, Value::row(std::move(sys)));
    return doc;
  }
  vp_encode_obj(doc, Value::row(std::move(sys)));    // schema: the system members + the declared columns' RowV2 row
  Value p = props.is_row() ? props : Value::row({});
  for (auto& [name, v] : p.as_row()) {               // a row holds declared columns only
    bool declared = false;
    for (auto& c : cols) declared = declared || c.name == name;
    if (!declared) throw std::runtime_error("arango: prop '" + name + "' is not a declared column of " + what);
  }
  if (cols.empty()) return doc;                      // no declared columns: no row (byte-identical to named)
  std::vector<PropRow> rows = {{0, &p}};
  ValueRows rs(cols, rows);
  return doc + rowv2_encode(cols, rs, 0, 0);
}

// the schema doc's row, after the object (checked to span the rest exactly); "" when `cols` is empty
static std::string_view doc_row_bytes(const std::vector<ColDecl>& cols, std::string_view raw, size_t obj) {
  std::string_view r = raw.substr(obj);
  if (cols.empty()) {
    if (!r.empty()) throw std::runtime_error("arango: trailing bytes after a doc with no declared columns");
    return r;
  }
  if (rowv2_size(cols, r, 0, 0) != r.size()) throw std::runtime_error("arango: a doc's row has trailing bytes");
  return r;
}

RowFields Arango::doc_fields(const std::vector<ColDecl>& cols, std::string_view raw, const std::vector<std::string>& fields,
                             uint64_t* used) const {
  if (cfg_.props_codec == PropsCodec::named) {
    if (used) *used = vp_doc_field_bytes(raw, fields);
    return vp_decode_fields(raw, fields);
  }
  size_t obj = vp_object_size(raw);
  std::vector<std::string> sys, props;
  for (auto& n : fields) (is_sys_member(n) ? sys : props).push_back(n);
  RowFields f;
  if (!sys.empty()) f = vp_decode_fields(raw.substr(0, obj), sys);
  if (used) *used = sys.empty() ? 0 : vp_doc_field_bytes(raw.substr(0, obj), sys);
  std::string_view r = doc_row_bytes(cols, raw, obj);
  if (cols.empty() || props.empty()) return f;
  RowV2 row(&cols, r);
  for (auto& name : props) {
    int c = row.col_index(name);
    if (c < 0 || !row.present(size_t(c))) continue;
    f.emplace_back(name, row.cell(size_t(c)));
    if (used) *used += row.cell_bytes(size_t(c));
  }
  return f;
}

// (a prop set to null: named keeps a null member, schema an absent cell -- reads treat both as absent, as the
// posting codec does)
Value Arango::doc_row(const std::vector<ColDecl>& cols, std::string_view raw) const {
  if (cfg_.props_codec == PropsCodec::named) return vp_decode_doc(raw);
  size_t obj = vp_object_size(raw);
  RowFields f = vp_decode_doc(raw.substr(0, obj)).as_row().to_fields();   // the system members
  std::string_view r = doc_row_bytes(cols, raw, obj);
  if (!cols.empty()) {
    Value props = RowV2(&cols, r).row();              // a local: a range-for over a temporary's row dangles
    for (auto& kv : props.as_row()) f.push_back(kv);
  }
  return Value::row(std::move(f));
}

// bytes only, no value decoded (the hot index-walk path): named = the members' framed bytes; schema = the system
// members' framed bytes + the present cells' bytes (the row's length is checked where a doc is decoded)
uint64_t Arango::doc_used(const std::vector<ColDecl>& cols, std::string_view raw, const std::vector<std::string>& names) const {
  if (cfg_.props_codec == PropsCodec::named) return vp_doc_field_bytes(raw, names);
  size_t obj = vp_object_size(raw);
  std::vector<std::string> sys, props;
  for (auto& n : names) (is_sys_member(n) ? sys : props).push_back(n);
  uint64_t used = sys.empty() ? 0 : vp_doc_field_bytes(raw.substr(0, obj), sys);
  if (cols.empty() || props.empty()) return used;
  RowV2 row(&cols, raw.substr(obj));
  for (auto& n : props) { int c = row.col_index(n); if (c >= 0) used += row.cell_bytes(size_t(c)); }
  return used;
}

// 'D' = a document (a vertex doc, or an edge doc at the edge collection offset); 'P' = the primary index and
// 'i' = a value / date index (vertices); 'f' / 't' = the covering edge indexes (arango's index-edge).
KeyClass Arango::classify(std::string_view key) const {
  if (key.size() < 5) return role_class("other", "other");
  uint32_t coll = (uint32_t(uint8_t(key[1])) << 24) | (uint32_t(uint8_t(key[2])) << 16) |
                  (uint32_t(uint8_t(key[3])) << 8) | uint32_t(uint8_t(key[4]));
  switch (key[0]) {
    case 'D': return coll >= kEdgeDocColl ? role_class("edge", "edge-doc")
                                          : role_class("vertex", "vertex-doc");
    case 'P': return coll >= kEdgeDocColl ? role_class("index-edge", "idx-pk-edge")
                                          : role_class("index-vertex", "idx-pk");
    case 'i': return role_class("index-vertex", "idx-value");
    case 'f': return role_class("index-edge", "idx-edge-fwd");
    case 't': return role_class("index-edge", "idx-edge-rev");
  }
  return role_class("other", "other");
}

}  // namespace evalcpp::arangodb
