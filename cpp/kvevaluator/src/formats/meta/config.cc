#include "formats/meta/config.h"

#include <algorithm>
#include <initializer_list>
#include <map>
#include <stdexcept>
#include <string>

namespace evalcpp::meta {

namespace {
[[noreturn]] void bad(const std::string& what) { throw std::runtime_error("meta spec: " + what); }
// a positive integer, however the JSON stores it (a parsed file: unsigned; a C++ literal: signed);
// rejects floats (1.5 would truncate) and negatives (-1 would wrap to 2^64-1)
bool positive_int(const json& j) { return j.is_number_unsigned() || (j.is_number_integer() && j.get<int64_t>() > 0); }
// strict keys: a typo (e.g. "adjacncy") must fail, not silently measure the default
void only_keys(const json& j, std::initializer_list<const char*> allowed, const std::string& where) {
  for (auto& [k, v] : j.items()) {
    bool ok = false;
    for (const char* a : allowed) ok = ok || k == a;
    if (!ok) bad("unknown key '" + k + "' in " + where);
  }
}
Group group_of(const std::string& s) {
  if (s == "owner") return Group::owner;
  if (s == "item") return Group::item;
  bad("bad group '" + s + "' (owner|item)");
}
Owners owners_of(const json& j) {
  Owners o;
  if (j.is_string()) {
    if (j.get<std::string>() != "one") bad("bad owners (want \"one\" or {pack})");
    return o;
  }
  if (j.is_object() && j.contains("pack")) {
    only_keys(j, {"pack"}, "owners");
    if (!positive_int(j.at("pack"))) bad("owners.pack must be a positive integer (bytes)");
    o.pack = j.at("pack").get<uint64_t>();
    if (o.pack == 0) bad("owners.pack must be > 0 bytes (use \"one\" for unpacked)");
    return o;
  }
  bad("bad owners (want \"one\" or {\"pack\": bytes})");
}
uint64_t split_of(const json& j) {
  if (j.is_string()) {
    if (j.get<std::string>() != "never") bad("bad split (want \"never\" or bytes)");
    return 0;
  }
  if (!j.is_number()) bad("split must be \"never\" or a positive integer (bytes)");
  if (!positive_int(j)) bad("split must be a positive integer (bytes; use \"never\" for unsplit)");
  return j.get<uint64_t>();
}
Component component_of(const json& j, const std::string& where, bool is_index = false, bool is_vertex_props = false,
                       bool is_adjacency = false) {
  if (!j.is_object()) bad(where + " must be an object");
  if (!is_vertex_props && j.contains("groups"))
    bad(where + ": groups belong in vertex_props.groups {\"Label\": [[...], ...]} (one map for every label)");
  if (is_index) only_keys(j, {"group", "owners", "split", "term", "scope"}, where);
  else if (is_vertex_props) only_keys(j, {"group", "owners", "split", "groups"}, where);
  else if (is_adjacency)
    only_keys(j, {"group", "owners", "split", "edge_props", "far_label", "own_chunk", "chunk_header"}, where);
  else only_keys(j, {"group", "owners", "split"}, where);
  if (!j.contains("group")) bad(where + " needs a group (owner|item)");
  Component c;
  c.group = group_of(j.at("group").get<std::string>());
  if (j.contains("owners")) c.owners = owners_of(j.at("owners"));
  if (j.contains("split")) c.split = split_of(j.at("split"));
  if (j.contains("edge_props")) {
    std::string pl = j.at("edge_props").is_string() ? j.at("edge_props").get<std::string>() : "";
    if (pl != "adjacency" && pl != "own") bad(where + ".edge_props must be \"adjacency\" or \"own\"");
    c.placement = pl == "own" ? Placement::own : Placement::adjacency;
  }
  // own_chunk: the form of own placement's adjacency chunk (config.h kOwnChunk). A spec need not say it; Config
  // always writes it, so a store from before the field existed is refused at open. Only that one value
  if (j.contains("own_chunk")) {
    if (c.placement != Placement::own || !j.at("own_chunk").is_string() || j.at("own_chunk").get<std::string>() != kOwnChunk)
      bad(where + ".own_chunk must be \"" + kOwnChunk + "\", and only with edge_props \"own\"");
  }
  if (j.contains("far_label")) {
    if (!j.at("far_label").is_boolean()) bad(where + ".far_label must be true or false");
    c.far_label = j.at("far_label").get<bool>();
  }
  // chunk_header (config.h kChunkHeader): a spec need not say it; Config writes it on every lists chunk, so a store
  // from before the field existed is refused at open. Only that one value, and only where Config writes it
  if (j.contains("chunk_header")) {
    if (!j.at("chunk_header").is_string() || j.at("chunk_header").get<std::string>() != kChunkHeader ||
        !c.lists_chunk())
      bad(where + ".chunk_header must be \"" + kChunkHeader + "\", and only on a packed component");
  }
  return c;
}
// vertex_props.groups: {label: [[prop, ...], ...]} -- the shape only (the Meta ctor checks the names); each
// group's props sorted (canonical), the group order kept (group 0 = the existence row)
std::map<std::string, std::vector<std::vector<std::string>>> groups_of(const json& j) {
  const char* hint = " (want {\"Label\": [[\"prop\", ...], [\"prop\", ...]], ...})";
  if (!j.is_object()) bad(std::string("vertex_props.groups must be an object") + hint);
  std::map<std::string, std::vector<std::vector<std::string>>> m;
  for (auto& [label, lists] : j.items()) {
    if (!lists.is_array() || lists.empty()) bad("vertex_props.groups." + label + " must be a non-empty list of groups" + hint);
    for (size_t g = 0; g < lists.size(); g++) {
      const json& grp = lists[g];
      if (!grp.is_array() || grp.empty())
        bad("vertex_props.groups." + label + " group " + std::to_string(g) + " is empty or not a list: each group "
            "names at least one prop" + hint);
      std::vector<std::string> props;
      for (auto& p : grp) {
        if (!p.is_string()) bad("vertex_props.groups." + label + " group " + std::to_string(g) + ": " + p.dump() + " is not a prop name");
        props.push_back(p.get<std::string>());
      }
      std::sort(props.begin(), props.end());
      m[label].push_back(std::move(props));
    }
  }
  return m;
}
json owners_json(const Owners& o) { return o.packed() ? json{{"pack", o.pack}} : json("one"); }
json split_json(uint64_t s) { return s ? json(s) : json("never"); }
json component_json(const Component& c) {
  return json{{"group", c.group == Group::owner ? "owner" : "item"},
              {"owners", owners_json(c.owners)}, {"split", split_json(c.split)}};
}
json adjacency_json(const Component& c) {   // an adjacency component: + where its edge props live
  json o = component_json(c);
  o["edge_props"] = c.props_own() ? "own" : "adjacency";
  if (c.props_own()) o["own_chunk"] = kOwnChunk;
  o["far_label"] = c.far_label;
  if (c.lists_chunk()) o["chunk_header"] = kChunkHeader;
  return o;
}
// a per-type map {name: component}; the names are checked against the schema by the Meta ctor
std::map<std::string, Component> component_map(const json& j, const std::string& where, bool adjacency) {
  if (!j.is_object()) bad(where + " must be an object {name: component}");
  std::map<std::string, Component> m;
  for (auto& [name, comp] : j.items()) m[name] = component_of(comp, where + "." + name, false, false, adjacency);
  return m;
}
json component_map_json(const std::map<std::string, Component>& m, bool adjacency) {
  json o = json::object();
  for (auto& [name, c] : m) o[name] = adjacency ? adjacency_json(c) : component_json(c);
  return o;
}
// the per-direction map {etype: {"out": component, "in": component}}: at least one direction, no other key
std::map<std::string, DirComponents> dir_component_map(const json& j, const std::string& where) {
  if (!j.is_object()) bad(where + " must be an object {etype: {\"out\": component, \"in\": component}}");
  std::map<std::string, DirComponents> m;
  for (auto& [etype, dirs] : j.items()) {
    std::string at = where + "." + etype;
    if (!dirs.is_object() || dirs.empty()) bad(at + " must name a direction: {\"out\": component} and / or {\"in\": component}");
    only_keys(dirs, {"out", "in"}, at);
    DirComponents d;
    if (dirs.contains("out")) d.out = component_of(dirs.at("out"), at + ".out", false, false, true);
    if (dirs.contains("in")) d.in = component_of(dirs.at("in"), at + ".in", false, false, true);
    m[etype] = std::move(d);
  }
  return m;
}
json dir_component_map_json(const std::map<std::string, DirComponents>& m) {
  json o = json::object();
  for (auto& [etype, d] : m) {
    json e = json::object();
    if (d.out) e["out"] = adjacency_json(*d.out);
    if (d.in) e["in"] = adjacency_json(*d.in);
    o[etype] = e;
  }
  return o;
}
// the combinations meta builds, per component (the default and every per-type override alike)
void validate_props(const Component& c, const std::string& where) {
  // a row never overflow-splits; one attribute per record packed across owners is the COLUMNAR cell, not
  // built (the packed path writes whole rows -- it would silently build chunked)
  if (c.split) bad(where + ".split is not built (a vertex row never splits)");
  if (c.group == Group::item && c.packed()) bad(where + " item + pack (the columnar cell) is not built");
}
void validate_adj(const Component& c, const std::string& where) {
  // own placement (the props in their own ordinal-keyed chunks) is built beside packed adjacency only
  if (c.props_own() && !c.packed()) bad(where + ".edge_props \"own\" is built on Chunked adjacency only (not supported on Split / Condensed)");
  // per-edge records have nothing to split; per-edge packed is not built; a packed chunk closes after the
  // owner that crosses the target, so a hub is never split inside it
  if (c.group == Group::item && c.split) bad(where + " item + split is invalid (one edge per record)");
  if (c.group == Group::item && c.packed()) bad(where + " item + pack is not built");
  if (c.packed() && c.split) bad(where + " pack + split is not built (hubs never split in a chunk)");
}
}  // namespace

Config Config::from_json(const json& j) {
  Config c;
  if (j.is_null()) return c;                     // no codec config: the default cell
  if (!j.is_object()) bad("a spec must be a JSON object (got " + std::string(j.type_name()) + ")");
  if (j.contains("preset"))   // there are no presets: a spec states its layout in full
    bad("\"preset\" is not a meta key: the engine layouts are the separate formats (dgraph, nebula, dgneb, "
        "pertype_csr); their full meta specs are inputs/specs/corners/*.json; the old meta is the tag "
        "meta-v3-engine-shaped-20261008");
  if (j.contains("edge_props"))   // meta-v4: placement is a field of the adjacency component
    bad("the top-level edge_props {\"in\"} is retired: placement is the adjacency component's field "
        "\"edge_props\": \"adjacency\" | \"own\" (adjacency.edge_props, adjacency_by_etype.<etype>.edge_props)");
  only_keys(j, {"version", "direction", "vertex_props", "adjacency", "index",
                "vertex_props_by_label", "adjacency_by_etype", "adjacency_by_etype_dir"}, "spec");
  if (j.contains("version")) {
    if (!j.at("version").is_number_integer()) bad("version must be an integer");
    c.version = j.at("version").get<int>();
  }
  // a full spec, or per-component fields over the defaults
  if (j.contains("vertex_props")) {
    const json& vp = j.at("vertex_props");
    if (vp.is_object() && !vp.contains("group") && vp.contains("groups"))   // groups on top of the default props layout
      only_keys(vp, {"groups"}, "vertex_props");
    else
      c.vertex_props = component_of(vp, "vertex_props", false, /*is_vertex_props=*/true);
    if (vp.is_object() && vp.contains("groups")) c.vertex_groups = groups_of(vp.at("groups"));
  }
  if (j.contains("adjacency")) c.adjacency = component_of(j.at("adjacency"), "adjacency", false, false, true);
  if (j.contains("vertex_props_by_label"))
    c.vertex_props_by_label = component_map(j.at("vertex_props_by_label"), "vertex_props_by_label", false);
  if (j.contains("adjacency_by_etype"))
    c.adjacency_by_etype = component_map(j.at("adjacency_by_etype"), "adjacency_by_etype", true);
  if (j.contains("adjacency_by_etype_dir"))
    c.adjacency_by_etype_dir = dir_component_map(j.at("adjacency_by_etype_dir"), "adjacency_by_etype_dir");
  if (j.contains("direction")) {
    std::string d = j.at("direction").get<std::string>();
    if (d != "both" && d != "declared") bad("bad direction '" + d + "' (both|declared)");
    c.dir_both = d == "both";
  }
  if (j.contains("value"))   // meta-v4: one property encoding everywhere (columns), no row timestamps
    bad("\"value\" (row_timestamp, the Condensed list codec) is retired in meta-v4: every property is the columns "
        "encoding and every list the list encoding (codec/columns.h, codec/lists.h) -- remove it");
  if (j.contains("index")) {
    const json& ix = j.at("index");
    if (!ix.is_object()) bad("index must be an object");
    if (ix.contains("group")) c.index = component_of(ix, "index", /*is_index=*/true);   // item = sorted_keys / owner = posting
    else only_keys(ix, {"term", "scope"}, "index");
    if (ix.contains("term")) {
      std::string t = ix.at("term").get<std::string>();
      if (t != "exact" && t != "day") bad("bad index term '" + t + "' (exact|day)");
      c.index_term = t == "exact" ? IndexTerm::exact : IndexTerm::day;
    }
    if (ix.contains("scope") && ix.at("scope").get<std::string>() != "type")
      bad("index scope '" + ix.at("scope").get<std::string>() + "' is not built (only per-type)");
  }
  return c;
}

void Config::validate() const {
  if (version != 1) bad("unknown spec version " + std::to_string(version));
  // the default components and every per-type override: each must be a combination meta builds
  validate_props(vertex_props, "vertex_props");
  validate_adj(adjacency, "adjacency");
  for (auto& [label, c] : vertex_props_by_label) validate_props(c, "vertex_props_by_label." + label);
  for (auto& [etype, c] : adjacency_by_etype) validate_adj(c, "adjacency_by_etype." + etype);
  for (auto& [etype, d] : adjacency_by_etype_dir) {
    if (d.out) validate_adj(*d.out, "adjacency_by_etype_dir." + etype + ".out");
    if (d.in) validate_adj(*d.in, "adjacency_by_etype_dir." + etype + ".in");
  }
  // index: sorted_keys (item) is full precision only and has no posting to split or pack
  if (index.group == Group::item) {
    if (index.owners.packed()) bad("index item + pack (sorted_keys packed) is not built");
    if (index.split) bad("index item + split is invalid (sorted_keys has one key per entry)");
    if (index_term == IndexTerm::day) bad("index item + term day is not built (sorted_keys is full-precision only)");
  }
  if (index.owners.packed() && index.split) bad("index pack + split is not built (a hot term splitting inside a chunk)");
  // packed vertex_props + a day index: read_top_k's chunked pass has no exact-value resolver, so a day
  // index would return silently-coarse top-k values
  bool any_packed_props = vertex_props.packed();
  for (auto& [label, c] : vertex_props_by_label) any_packed_props = any_packed_props || c.packed();
  if (any_packed_props && index_term == IndexTerm::day)
    bad("packed vertex_props with a day index is not built (read_top_k needs the resolver on the chunked pass)");
}

json Config::to_json() const {
  // the full settled spec shape (store identity); from_json(to_json()) == this on the live fields.
  json o = json::object();
  o["version"] = version;
  o["direction"] = dir_both ? "both" : "declared";
  o["vertex_props"] = component_json(vertex_props);
  if (!vertex_groups.empty()) o["vertex_props"]["groups"] = vertex_groups;   // absent = one group per label (the default shape)
  o["adjacency"] = adjacency_json(adjacency);
  // the per-type maps only when present: a uniform spec keeps the exact shape (and store identity) it had
  if (!vertex_props_by_label.empty()) o["vertex_props_by_label"] = component_map_json(vertex_props_by_label, false);
  if (!adjacency_by_etype.empty()) o["adjacency_by_etype"] = component_map_json(adjacency_by_etype, true);
  if (!adjacency_by_etype_dir.empty()) o["adjacency_by_etype_dir"] = dir_component_map_json(adjacency_by_etype_dir);
  json ix = component_json(index);   // {group item|owner, owners, split}
  ix["term"] = index_term == IndexTerm::exact ? "exact" : "day";
  ix["scope"] = "type";              // scope is always per-type (shared/global index is out of scope)
  o["index"] = ix;
  return o;
}

}  // namespace evalcpp::meta
