// The engine-shaped CORNER specs of the configurable format (inputs/specs/corners/corner_<name>.json: the full
// spec each retired preset expanded to at the source repository's tag meta-v3-engine-shaped-20261008), for the tests that compare meta
// with an engine format. dgneb's (the All-Condensed corner) is tests/fixtures/meta_corner_dgneb.json, beside its saved
// reference: the format retired, and only these tests read its corner. `over` applies overrides the way a preset-based spec once did (Config::from_json's merge): a
// component replaces the corner's, except an index / vertex_props object with no `group` (its term / groups only)
// and `value` (field by field); an adjacency override drops the placement and the list codec unless it states them
// (they follow the adjacency).
#pragma once
#include <fstream>
#include <stdexcept>
#include <string>

#include "value/value.h"
#include "repo_paths.h"

namespace evalcpp::test {

inline json meta_corner(const std::string& name, const json& over = json::object()) {
  std::string path = name == "dgneb" ? std::string(FIXTURE_DIR) + "/meta_corner_dgneb.json"
                                     : specs_dir() + "/corners/corner_" + name + ".json";
  std::ifstream f(path);
  if (!f) throw std::runtime_error("meta corner spec missing: " + path);
  json c = json::parse(f);
  if (over.contains("adjacency") || over.contains("adjacency_by_etype")) {
    if (!over.contains("edge_props")) c.erase("edge_props");
    bool states_codec = over.contains("value") && over["value"].is_object() && over["value"].contains("edge_props");
    if (!states_codec && c.contains("value")) c["value"].erase("edge_props");
  }
  for (auto& [k, v] : over.items()) {
    bool merge = v.is_object() && c.contains(k) &&
                 (k == "value" || ((k == "index" || k == "vertex_props") && !v.contains("group")));
    if (merge) {
      for (auto& [kk, vv] : v.items()) c[k][kk] = vv;
    } else if (k == "index" && v.is_object() && c.contains(k)) {   // a new index component keeps the corner's term
      json n = v;
      if (!n.contains("term")) n["term"] = c[k]["term"];
      c[k] = n;
    } else {
      c[k] = v;
    }
  }
  return c;
}

}  // namespace evalcpp::test
