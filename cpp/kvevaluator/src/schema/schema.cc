#include "schema/schema.h"

#include <algorithm>
#include <fstream>
#include <set>
#include <stdexcept>

#include "util/sha256.h"

namespace evalcpp {

static PropDecl prop_from(const json& p) {
  PropDecl d;
  d.name = p.at("name").get<std::string>();
  d.type = p.at("type").get<std::string>();
  d.multi = p.value("multi", false);
  d.nullable = p.value("nullable", false);
  if (d.type != "str" && d.type != "int" && d.type != "date" && d.type != "bytes")
    throw std::runtime_error("schema: property '" + d.name + "' has type '" + d.type + "' (str | int | date | bytes)");
  if (d.type == "bytes" && d.multi) throw std::runtime_error("schema: property '" + d.name + "': a bytes property is single-valued");
  return d;
}

Schema Schema::load(const std::string& path) {
  std::ifstream f(path);
  if (!f) throw std::runtime_error("cannot open schema " + path);
  json j = json::parse(f);
  return from_json(j);
}

Schema Schema::from_json(const json& j) {
  Schema s;
  s.raw_ = j;
  s.name_ = j.value("name", "");
  s.hash_ = j.value("hash", "");
  for (auto& v : j.at("vertices")) {
    VertexDecl d;
    d.label = v.at("label").get<std::string>();
    d.id = v.at("id").get<int>();
    d.group = v.value("group", d.label);
    d.tag = v.at("tag").get<int>();
    for (auto& p : v.at("props")) d.props.push_back(prop_from(p));
    s.vidx_[d.label] = s.vertices_.size();
    s.vertices_.push_back(d);
    s.group_members_[d.group].push_back(d.label);
    s.tag_labels_[uint64_t(d.tag)].push_back(d.label);
  }
  for (auto& e : j.at("edges")) {
    EdgeDecl d;
    d.etype = e.at("etype").get<std::string>();
    d.id = e.at("id").get<int>();
    d.reverse = e.value("reverse", false);
    d.multiplicity = e.value("multiplicity", std::string("unique"));
    if (d.multiplicity != "unique" && d.multiplicity != "multi")
      throw std::runtime_error("schema: edge '" + d.etype + "' multiplicity '" + d.multiplicity + "' (unique | multi)");
    for (auto& ep : e.value("endpoints", json::array())) {
      d.endpoints.emplace_back(ep.at(0).get<std::string>(), ep.at(1).get<std::string>());
      // authoring constraint (mirrors compile_schema): endpoints are CONCRETE labels --
      // a by-group endpoint would silently skip the copy-maintenance fan-out
      for (auto& lab : {d.endpoints.back().first, d.endpoints.back().second})
        if (!s.vidx_.count(lab))
          throw std::runtime_error("schema: edge '" + d.etype + "' endpoint '" + lab +
                                   "' is not a declared vertex label (endpoints must be "
                                   "concrete labels, not type groups)");
    }
    for (auto& p : e.value("props", json::array())) d.props.push_back(prop_from(p));
    s.eidx_[d.etype] = s.edges_.size();
    s.edges_.push_back(d);
  }
  for (auto& ix : j.at("indexes")) {
    IndexDecl d;
    d.label = ix.at("label").get<std::string>();
    d.prop = ix.value("prop", "");
    d.kind = ix.at("kind").get<std::string>();
    d.id = ix.at("id").get<int>();
    d.width = ix.value("width", 0);
    d.layout = ix.value("layout", "");
    d.type_scoped = ix.value("type_scoped", false);
    d.key_sorted = ix.value("key_sorted", false);
    d.value_sorted = ix.value("value_sorted", false);
    s.iidx_[{d.label, d.prop}] = s.indexes_.size();
    s.indexes_.push_back(d);
  }
  for (auto& l : j.value("type_indexed_labels", json::array())) s.type_indexed_.insert(l.get<std::string>());
  s.predicates_per_label_ = j.value("predicates", std::string()) == "per_label";
  for (auto it = j.at("vid_tags").begin(); it != j.at("vid_tags").end(); ++it)
    s.vid_tags_[it.key()] = it.value().get<int>();
  // a bytes property is a blob: no format keys an index term by it
  for (auto& ix : s.indexes_)
    if (!ix.prop.empty() && s.vidx_.count(ix.label))
      for (auto& p : s.vertices_[s.vidx_.at(ix.label)].props)
        if (p.name == ix.prop && p.type == "bytes")
          throw std::runtime_error("schema: index on " + ix.label + "." + ix.prop + ": a bytes property cannot be indexed");
  // nebula's _prop_indexed: a label with a REQUIRED value/sorted property index
  for (auto& ix : s.indexes_)
    if ((ix.kind == "value" || ix.kind == "sorted") && !s.is_nullable(ix.label, ix.prop))
      s.prop_indexed_.insert(ix.label);
  // the post-hash blocks (adj directives + prop groups); absent on canonical/older files
  if (j.contains("adj")) {
    const json& a = j["adj"];
    for (auto& [k, sub] : std::map<std::string, std::map<std::string, std::string>*>{
             {"sort", &s.adj_sort_}, {"inline", &s.adj_inline_},
             {"sort_facet", &s.adj_sort_facet_}, {"dir", &s.adj_dir_}})
      if (a.contains(k))
        for (auto it = a[k].begin(); it != a[k].end(); ++it)
          (*sub)[it.key()] = it.value().get<std::string>();
  }
  const json groups = j.value("prop_groups", json::object());
  for (auto it = groups.begin(); it != groups.end(); ++it) {
    std::vector<std::string> members = it.value().get<std::vector<std::string>>();
    s.prop_groups_[it.key()] = members;
    for (auto& p : members) s.pred_group_[p] = it.key();
  }
  s.content_sha_ = sha256_hex(j.dump());   // one canonical serialization: nlohmann sorts keys
  return s;
}

const VertexDecl& Schema::vertex(const std::string& label) const {
  auto it = vidx_.find(label);
  if (it == vidx_.end()) throw std::runtime_error("schema: unknown vertex label '" + label + "'");
  return vertices_[it->second];
}

const EdgeDecl& Schema::edge(const std::string& etype) const {
  auto it = eidx_.find(etype);
  if (it == eidx_.end()) throw std::runtime_error("schema: unknown edge type '" + etype + "'");
  return edges_[it->second];
}

const std::vector<PropDecl>& Schema::edge_props(const std::string& etype) const {
  auto it = eidx_.find(etype);
  if (it == eidx_.end()) return no_props_;
  return edges_[it->second].props;
}

const IndexDecl* Schema::find_index(const std::string& label, const std::string& prop) const {
  auto it = iidx_.find({label, prop});
  return it == iidx_.end() ? nullptr : &indexes_[it->second];
}

const IndexDecl& Schema::index_decl(const std::string& label, const std::string& prop) const {
  const IndexDecl* d = find_index(label, prop);
  if (!d) throw std::runtime_error("schema: no index on " + label + "." + prop);
  return *d;
}

const IndexDecl& Schema::type_index_decl(const std::string& label) const {
  const IndexDecl* d = find_index(label, "");
  if (!d) throw std::runtime_error("schema: no type index id for " + label);
  return *d;
}

bool Schema::is_indexed(const std::string& label, const std::string& prop) const {
  const IndexDecl* d = find_index(label, prop);
  return d && d->kind == "value";
}
bool Schema::is_date_col(const std::string& label, const std::string& prop) const {
  const IndexDecl* d = find_index(label, prop);
  return d && d->kind == "sorted";
}
bool Schema::is_multi(const std::string& label, const std::string& prop) const {
  for (auto& p : vertex(label).props)
    if (p.name == prop) return p.multi;
  return false;
}
bool Schema::is_nullable(const std::string& label, const std::string& prop) const {
  auto it = vidx_.find(label);
  if (it == vidx_.end()) return false;
  for (auto& p : vertices_[it->second].props)
    if (p.name == prop) return p.nullable;
  return false;
}

std::vector<std::string> Schema::index_cols_of(const std::string& label) const {
  std::vector<std::string> out;
  for (auto& ix : indexes_)
    if (ix.label == label && (ix.kind == "value" || ix.kind == "sorted")) out.push_back(ix.prop);
  std::sort(out.begin(), out.end());
  return out;
}

const std::string& Schema::group_of(const std::string& label) const { return vertex(label).group; }

int Schema::tag_of_label(const std::string& label) const {
  // a vertex label (Post -> its group Message) or a group name itself (vids.group_of)
  std::string group = has_vertex(label) ? group_of(label) : label;
  auto it = vid_tags_.find(group);
  if (it == vid_tags_.end()) throw std::runtime_error("schema: no vid tag for '" + label + "'");
  return it->second;
}

std::vector<std::pair<std::string, bool>> Schema::incident_etypes(const std::string& label) const {
  std::vector<std::pair<std::string, bool>> out;   // edges_ is schema order; Python sorts by etype
  std::vector<const EdgeDecl*> es;
  for (auto& e : edges_) es.push_back(&e);
  std::sort(es.begin(), es.end(), [](auto* a, auto* b) { return a->etype < b->etype; });
  for (auto* e : es) {
    bool src = false, dst = false;
    for (auto& [s, d] : e->endpoints) { src |= s == label; dst |= d == label; }
    if (src) out.emplace_back(e->etype, true);
    if (dst) out.emplace_back(e->etype, false);
  }
  return out;
}

bool Schema::owns_edge(const std::string& label, const std::string& etype, bool out) const {
  for (auto& [src, dst] : edge(etype).endpoints)
    if ((out ? src : dst) == label) return true;
  return false;
}

std::vector<std::string> Schema::far_labels(const std::string& etype, bool out) const {
  std::set<std::string> far;
  for (auto& [src, dst] : edge(etype).endpoints) far.insert(out ? dst : src);
  return {far.begin(), far.end()};
}

const std::vector<std::string>& Schema::labels_of_tag(uint64_t tag) const {
  auto it = tag_labels_.find(tag);
  return it == tag_labels_.end() ? no_labels_ : it->second;
}

std::string Schema::enumeration_prop(const std::string& label) const {
  std::vector<std::string> props;
  for (auto& ix : indexes_)
    if (ix.label == label && (ix.kind == "value" || ix.kind == "sorted") && !is_nullable(label, ix.prop))
      props.push_back(ix.prop);
  std::sort(props.begin(), props.end());
  if (props.empty()) throw std::runtime_error("schema: " + label + " has no required property index");
  return props[0];
}

}  // namespace evalcpp
