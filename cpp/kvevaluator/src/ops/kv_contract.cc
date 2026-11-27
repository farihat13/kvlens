#include "ops/kv_contract.h"

#include <stdexcept>

#include "contract/contract.h"

namespace evalcpp::kv {

namespace {
// the table is inputs/contract/kv_ops.json, compiled in (contract/contract.h); Python reads the same file
std::vector<OpSpec> make_table() {
  std::vector<OpSpec> t;
  for (auto& o : contract::kv_ops().at("ops")) {
    OpSpec s{o.at("name").get<std::string>(), o.at("kind").get<std::string>(), o.at("every_format").get<bool>(), {}, {}};
    for (auto& a : o.at("args"))
      s.args.push_back({a.at("name").get<std::string>(), a.at("required").get<bool>(),
                        a.contains("default") ? a["default"].dump() : std::string()});
    for (auto& r : o.at("refusals")) s.refusals.push_back(r.get<std::string>());
    t.push_back(std::move(s));
  }
  return t;
}
}  // namespace

const std::vector<OpSpec>& ops() {
  static const std::vector<OpSpec> table = make_table();
  return table;
}

const OpSpec* find_op(const std::string& op) {
  for (auto& s : ops())
    if (s.name == op) return &s;
  return nullptr;
}

bool is_read_op(const std::string& op) {
  const OpSpec* s = find_op(op);
  return s && s->kind == "read";
}

bool is_every_format_read_op(const std::string& op) {
  const OpSpec* s = find_op(op);
  return s && s->kind == "read" && s->every_format;
}

bool is_write_op(const std::string& op) {
  const OpSpec* s = find_op(op);
  return s && s->kind == "write";
}

json ops_json() {
  json out = json::array();
  for (auto& s : ops()) {
    json args = json::array();
    for (auto& a : s.args) {
      json j = {{"name", a.name}, {"required", a.required}};
      if (!a.default_json.empty()) j["default"] = json::parse(a.default_json);
      args.push_back(j);
    }
    out.push_back({{"name", s.name}, {"kind", s.kind}, {"every_format", s.every_format}, {"args", args},
                   {"refusals", s.refusals}});
  }
  return {{"ops", out}};
}

// --- the parsers -------------------------------------------------------------------------------------------------

std::string dir_of(const Args& a) { return a.has("dir") ? a.str("dir") : kDefaultDir; }

std::string dir_of(const json& a) {
  return a.contains("dir") && !a["dir"].is_null() ? a["dir"].get<std::string>() : std::string(kDefaultDir);
}

NeighborsArgs neighbors_args(const Args& a) {
  return {&a.list("srcs"), a.str("etype"), dir_of(a),
          a.has("edge_props") ? a.str_list("edge_props") : std::vector<std::string>{}, a.flag("pairs", false),
          a.labels("label")};
}

WalkArgs chase_args(const Args& a) { return {&a.list("srcs"), a.str("etype"), dir_of(a), a.opt("max_hops")}; }
WalkArgs closure_args(const Args& a) { return {&a.list("srcs"), a.str("etype"), dir_of(a), a.opt("max_levels")}; }

IndexRangeArgs index_range_args(const Args& a, const std::string&) {
  return {a.labels("labels"), a.str("prop"), a.integer("lo"), a.integer("hi")};
}

TopKArgs top_k_args(const Args& a) {
  std::string order = a.has("order") ? a.str("order") : "desc";
  if (order != "desc") throw std::runtime_error("read_top_k_by_index: only order=desc (the LDBC shape)");
  std::string id_order = a.has("id_order") ? a.str("id_order") : "asc";
  if (id_order != "asc" && id_order != "desc")
    throw std::runtime_error("read_top_k_by_index: id_order must be asc or desc, got '" + id_order + "'");
  TopKArgs t{a.opt("vids"), a.str("prop"), a.integer("k"), a.opt("hi"), a.labels("labels"),
             a.has("props") ? a.str_list("props") : std::vector<std::string>{}, id_order == "desc"};
  if (t.k < 1) throw std::runtime_error("read_top_k_by_index: k must be >= 1");
  return t;
}

}  // namespace evalcpp::kv
