#include "formats/dgraph/format.h"
#include "codec/decode_counts.h"

#include <algorithm>
#include <cstring>
#include <numeric>
#include <optional>
#include <stdexcept>

#include "codec/prim.h"
#include "codec/rowv2.h"
#include "codec/varint.h"
#include "formats/common.h"
#include "formats/dgraph/keys.h"
#include "codec/posting.h"
#include "index/sorted_index.h"
#include "store/unit_value.h"
#include "util/uidset.h"
#include "util/vid.h"

namespace evalcpp::dgraph {

static constexpr int64_t kDayMs = 86400000;

std::string day_token(int64_t ms) {
  // Python floor division: -1 ms is day -1, not day 0
  int64_t day = ms >= 0 ? ms / kDayMs : -((-ms + kDayMs - 1) / kDayMs);
  char buf[32];
  snprintf(buf, sizeof buf, "%07lld", (long long)day);
  // %07lld is a MINIMUM width: a day outside [-999999, 9999999] (a date outside years
  // -770..29349) would give a longer token that the fixed-width landing test of the day-token
  // walks (day_landing) cannot tell apart, and that could be a proper prefix of another token
  // (1000000 / 10000000). Refuse it loudly rather than walk wrong.
  if (strlen(buf) != 7) throw std::runtime_error("dgraph: day token out of the 7-digit range: " + std::string(buf));
  return buf;
}

Config Config::from_json(const json& j) {
  Config c;
  if (j.is_object()) c.chunk_target = j.value("chunk_target", c.chunk_target);   // absent = ∞
  c.props_codec = props_codec_from_json(j);
  return c;
}
json Config::to_json() const {
  // chunk_target only when finite (the anchor's shape); props_codec always, so a store built under
  // another codec (or before the knob existed) can never pass for this one
  json o = json::object();
  if (chunked()) o["chunk_target"] = chunk_target;
  o["props_codec"] = props_codec_name(props_codec);
  return o;
}

posting::AdjCodec Dgraph::adj_codec(const std::string& etype, char) const {
  if (cfg_.props_codec == PropsCodec::named) return posting::AdjCodec::named();
  return posting::AdjCodec::schema(&ecols(etype), 0);
}

Dgraph::Dgraph(const Schema& schema, const Config& cfg) : schema_(schema), cfg_(cfg) {
  // dgraph's indexes and list reads are per PREDICATE, global across types: project the
  // (label, prop) facts down (schema.indexed_predicates / date_predicates / multival_predicates)
  list_preds_.insert("dgraph.type");
  per_label_ = schema.predicates_per_label();   // (the subclasses refuse the flag in their own constructors)
  if (per_label_)
    for (auto& v : schema.vertices())
      for (auto& p : v.props)
        if (p.multi)   // list_preds_ is keyed by the bare name; a list predicate per label is not built
          throw std::runtime_error("dgraph: per-label predicates with a multi-valued property (" + v.label + "." + p.name + ") are not built");
  for (auto& v : schema.vertices())
    for (auto& p : v.props) {
      if (schema.is_indexed(v.label, p.name)) indexed_.insert(p.name);
      else if (schema.is_date_col(v.label, p.name)) date_preds_.insert(p.name);
      if (p.multi) list_preds_.insert(p.name);
    }
  for (auto& e : schema.edges()) {
    etypes_.insert(e.etype);
    if (e.reverse) reverse_.insert(e.etype);
    std::vector<ColDecl> cols;
    for (auto& p : e.props) cols.push_back({p.name, p.type, false});
    ecols_[e.etype] = cols;
  }
  // the opt-in index keying (full_index, the dated designs): what this side builds, and the flag combinations it
  // refuses (value_sorted without type_scoped or with key_sorted; key_sorted without type_scoped)
  size_t nprop = 0, ndate = 0;
  all_type_scoped_ = all_key_sorted_ = true;
  for (auto& ix : schema.indexes()) {
    if (ix.prop.empty()) continue;                       // the membership index has no keying flags
    if (ix.value_sorted && (!ix.type_scoped || ix.key_sorted || ix.kind != "sorted"))
      throw std::runtime_error("dgraph: value_sorted index on " + ix.label + "." + ix.prop +
                               " must be a type_scoped date index, and is a distinct layout from key_sorted");
    if (ix.key_sorted && !ix.type_scoped)
      throw std::runtime_error("dgraph: key_sorted index on " + ix.label + "." + ix.prop + " needs type_scoped");
    if (per_label_ && !ix.type_scoped)   // one predicate per label owns its data keys AND its index keys
      throw std::runtime_error("dgraph: `predicates per_label` needs every property index declared type_scoped (" +
                               ix.label + "." + ix.prop + " is not)");
    if (ix.type_scoped) { type_scoped_.insert({ix.label, ix.prop}); scoped_props_.insert(ix.prop); }
    if (ix.value_sorted) value_sorted_.insert({ix.label, ix.prop});
    if (ix.key_sorted) { key_sorted_.insert({ix.label, ix.prop}); key_sorted_props_.insert(ix.prop); }
    nprop++;
    all_type_scoped_ = all_type_scoped_ && ix.type_scoped;
    if (ix.kind == "sorted") { ndate++; all_key_sorted_ = all_key_sorted_ && ix.key_sorted; }
  }
  if (!nprop) all_type_scoped_ = false;
  if (!ndate) all_key_sorted_ = false;
  for (auto& [label, prop] : value_sorted_) value_sorted_preds_.insert(index_pred(label, prop));
}

void Dgraph::require_index(const std::vector<std::string>& labels, const std::string& prop) const {
  for (auto& l : labels) schema_.index_decl(l, prop);   // throws "schema: no index on <label>.<prop>"
  if (!labels.empty()) return;
  for (auto& v : schema_.vertices())
    if (schema_.find_index(v.label, prop)) return;
  throw std::runtime_error("schema: no index on " + prop + " (no label declares one)");
}

std::string Dgraph::index_pred(const std::string& label, const std::string& prop) const {
  if (!type_scoped_.count({label, prop})) return prop;
  std::string k;
  put_u32be(k, uint32_t(schema_.index_decl(label, prop).id));
  return k;
}

std::string Dgraph::index_term(const std::string& label, const std::string& prop, const Value& v) const {
  if (!type_scoped_.count({label, prop})) return term_of_value(v);
  // A per-type VALUE term whose packed form would hold a NUL byte -- an integer (8 big-endian bytes) or a string over
  // 126 bytes (its length prefix) -- is written as its text instead (the Python twin's str(value)): a chunked store
  // tells a term's parts by a NUL (ingest_finish below), so such a term could not be chunked. Exact and order-free
  // either way; a packed short string never equals a text term (its first byte is no UTF-8 lead). Only the JOB
  // workload declares such indexes (integer codes, long titles): LDBC's per-type value terms are short strings.
  if (v.is_int() || (v.is_text() && v.text().size() > 126)) return term_of_value(v);
  return index_value_bytes(schema_, schema_.index_decl(label, prop), label, v);
}

bool Dgraph::key_sorted_walk(const std::vector<std::string>& labels, const std::string& prop, const char* op) const {
  // no labels on a key-sorted predicate would fall through to the day-token walk of the bare
  // predicate -- an EMPTY band under this design, so a silent [] both languages would agree on
  if (labels.empty() && key_sorted_props_.count(prop))
    throw std::runtime_error(std::string(op) + ": " + prop + " is key-sorted (per type); the step must name its labels");
  size_t n = 0;
  for (auto& l : labels) n += is_key_sorted(l, prop) ? 1 : 0;
  if (n && n != labels.size())
    throw std::runtime_error(std::string(op) + ": " + prop + " is key-sorted on some of the labels but not all -- one walk per op");
  return n > 0;
}

// the key-sorted term: nebula's SortedIndex codec (8 B order-preserving ms, 8 B BE uid), so the
// key band is the same bytes as the three nebula-keyed formats and a bare 8 B ms bound sorts
// before every same-ms entry (the exclusive hi of a top-k walk is free, as in Python)
std::string Dgraph::key_sorted_term(int64_t ms, uint64_t uid) {
  std::string t = IndexKeys::sorted_bytes(ms);
  for (int i = 7; i >= 0; i--) t.push_back(char((uid >> (8 * i)) & 0xff));
  return t;
}
bool Dgraph::key_sorted_landing(std::string_view key, const std::string& pre) {
  return has_prefix(key, pre) && key.size() == pre.size() + 16;
}
int64_t Dgraph::key_sorted_ms(std::string_view key, const std::string& pre) {
  return IndexKeys::sorted_value(key.substr(pre.size(), 8));
}
uint64_t Dgraph::key_sorted_uid(std::string_view key) {
  uint64_t u = 0;
  for (size_t i = key.size() - 8; i < key.size(); i++) u = (u << 8) | uint8_t(key[i]);
  return u;
}
int64_t Dgraph::day_number(int64_t ms) {
  return ms >= 0 ? ms / kDayMs : -((-ms + kDayMs - 1) / kDayMs);   // Python floor division
}

const std::vector<ColDecl>& Dgraph::ecols(const std::string& etype) const {
  auto it = ecols_.find(etype);
  if (it == ecols_.end()) throw std::runtime_error("dgraph: unknown etype '" + etype + "'");
  return it->second;
}

std::set<std::string> Dgraph::capabilities() const {
  // the dgraph.type posting makes index-once 1 SEEK whatever the type size -- when the design
  // declares the type index (all labels or none; partial sets are not a design)
  // a day-token date index answers a range with the boundary-day SUPERSET (read then re-filter);
  // with every date index key-sorted the walk is exact and stops early, and the unsorted story
  // goes (one range-walk story per design). Python's rules, mirrored.
  std::set<std::string> caps;
  caps.insert(all_key_sorted_ ? "sorted_range_index" : "unsorted_range_index");
  // NOT sorted_range_index for a type-scoped index alone: a day-token index stays a boundary-day
  // superset; exact labels get it per label (capabilities_for)
  if (!key_sorted_.empty()) caps.insert("key_sorted_index");
  if (!value_sorted_.empty()) caps.insert("value_sorted_index");   // a top-k ranks from the bucket (Python's rule)
  if (all_type_scoped_) caps.insert("per_type_index");   // the index IS the type restriction, every index
  bool all = !schema_.vertices().empty();
  for (auto& v : schema_.vertices()) all = all && schema_.has_type_index(v.label);
  if (all) caps.insert({"type_index", "type_index_single_posting"});
  // per-label predicates: a label's values of one property are one whole predicate, so the label can be read with
  // its properties in one run per property (read_label_run); needs the type posting for the label's vertices
  if (per_label_ && all) caps.insert("label_rows_one_run");
  return caps;
}

// per label: per_type_index when every property index the label declares is type-scoped (the index IS its type
// restriction); sorted_range_index when every date index it declares is key-sorted (an exact walk). Per label, not
// per (label, prop): Post and Comment have one index in every scoped design today. Python Dgraph.capabilities_for.
std::set<std::string> Dgraph::capabilities_for(const std::string& label) const {
  std::set<std::string> caps = capabilities();   // virtual: a subclass's adjacency cap would ride along
  int nprop = 0, nscoped = 0, ndate = 0, nkeyed = 0;
  for (auto& ix : schema_.indexes()) {
    if (ix.label != label || ix.prop.empty()) continue;   // the type index is not a property index
    nprop++;
    if (type_scoped_.count({ix.label, ix.prop})) nscoped++;
    if (ix.kind == "sorted") { ndate++; if (key_sorted_.count({ix.label, ix.prop})) nkeyed++; }
  }
  if (nprop && nscoped == nprop) caps.insert("per_type_index");
  if (ndate && nkeyed == ndate) caps.insert("sorted_range_index");
  return caps;
}

bool Dgraph::has_edge_type(const std::string& name) const {
  for (auto& e : schema_.edges())
    if (e.etype == name) return true;
  return false;
}
// a posting list per (predicate, uid); the reverse list only where the schema declares @reverse
std::set<std::string> Dgraph::edge_capabilities_for(const std::string& etype) const {
  std::set<std::string> caps = {"adjacency_lists"};
  if (reverse_.count(etype)) caps.insert("reverse_adjacency");
  return caps;
}

// the contract's reads every format serves (ops/kv_contract.h), its optional index_range_buckets, and the writes
bool Dgraph::has_kv_op(const std::string& op) const {
  return kv::is_every_format_read_op(op) || op == "index_range_buckets" || is_write_op(op);
}

Value Dgraph::run_kv_op(const Step& step, const Args& a, StepExec& ex) {
  const std::string& op = step.op;
  if (op == "read_vertex_by_id") {
    // the vid IS the uid: a direct address, 0 ops (a unified message anchor is one candidate)
    const Value& vid = a.get("vid");
    if (vid.is_text()) return Value::list({vid});
    return Value::list(vid.as_list());
  }
  if (op == "read_vertex_props")   // the label is ignored unless the schema names predicates per label
    return read_vertex_props(ex, a.list("vids"), a.str_list("props"), per_label_ && a.has("label") ? a.str("label") : "");
  if (op == "read_anchor_props") return read_vertex_props(ex, a.list("cands"), a.str_list("props"));  // SIM: read the candidates straight
  if (op == "read_neighbors") {
    kv::NeighborsArgs n = kv::neighbors_args(a);
    return read_neighbors(ex, *n.srcs, n.etype, n.dir, n.edge_props, n.pairs);
  }
  if (op == "read_index_eq") return read_index_eq(ex, a.has("label") ? a.str("label") : "", a.str("prop"), a.get("value"));
  if (op == "read_index_range" || op == "index_range_buckets") {
    kv::IndexRangeArgs ir = kv::index_range_args(a, op);
    return read_index_range(ex, ir.labels, ir.prop, ir.lo, ir.hi, op == "index_range_buckets");
  }
  if (op == "read_vertices_by_label") {
    if (a.has("keep")) throw std::runtime_error("dgraph read_vertices_by_label takes no keep (no scan of a label's rows here)");
    return read_vertices_by_label(ex, a.str("label"), a.opt("props"));
  }
  if (op == "read_filter_by_label_probe") return read_filter_by_label_probe(ex, a.list("vids"), a.labels("label"));
  if (op == "read_filter_by_label_index") return read_filter_by_label_index(ex, a.list("vids"), a.labels("label"));
  if (std::optional<Value> v = run_operator(*this, schema_, op, a, ex)) return *v;   // closure, chase, edge exists, top k
  if (is_write_op(op)) {
    if (per_label_) throw std::runtime_error("dgraph: writes are not built under per-label predicates (" + op + ")");
    return run_write_op(step, a, ex);
  }
  throw std::runtime_error("dgraph: unknown kv op '" + op + "'");
}


// (attr, kind, last): the predicate name runs up to the kind byte (predicate names are ASCII, keys.h).
// "I" is an index; "R" is the reverse adjacency (edge data); "D" is vertex data unless the predicate is an
// edge type -- then the forward adjacency (a predicate is scalar or uid, never both).
KeyClass Dgraph::classify(std::string_view key) const {
  // a type-scoped index key: u32be(index id) + kIndex + term (index_pred) -- its id bytes may hold 0x00/0x02/0x04,
  // so it is matched by the declared id before the attribute scan below
  if (key.size() >= 5 && key[4] == kIndex) {
    uint32_t id = (uint32_t(uint8_t(key[0])) << 24) | (uint32_t(uint8_t(key[1])) << 16) |
                  (uint32_t(uint8_t(key[2])) << 8) | uint32_t(uint8_t(key[3]));
    for (auto& ix : schema_.indexes())
      if (uint32_t(ix.id) == id && type_scoped_.count({ix.label, ix.prop})) return role_class("index-vertex", "idx-value");
  }
  size_t i = 0;
  while (i < key.size() && key[i] != kData && key[i] != kIndex && key[i] != kReverse) i++;
  if (i == 0 || i == key.size()) return role_class("other", "other");
  std::string_view attr = key.substr(0, i);
  if (key[i] == kIndex) {
    if (attr == "dgraph.type") return role_class("index-vertex", "idx-type-enum");
    if (attr == "fqid") return role_class("index-vertex", "idx-id-lookup");
    return role_class("index-vertex", "idx-value");
  }
  if (key[i] == kReverse) return role_class("edge", "edge-rev");
  for (auto& e : schema_.edges())
    if (e.etype == attr) return role_class("edge", "edge-fwd");
  if (attr == "dgraph.type") return role_class("vertex", "vertex-type-record");
  if (attr == "fqid") return role_class("vertex", "vertex-id-record");
  return role_class("vertex", "vertex-prop");
}

}  // namespace evalcpp::dgraph
