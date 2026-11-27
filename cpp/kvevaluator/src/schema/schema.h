// The dataset schema, loaded from the neutral `schema.json` Python emits (schema.to_json()).
// Vertex order is the declaration order and is load-bearing: pertype_csr tries a Message
// vid's candidate labels in that order, which sets the SEEK count of unknown-type reads.
#pragma once
#include <cstdint>
#include <map>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include "value/value.h"

namespace evalcpp {

struct PropDecl {
  std::string name;
  std::string type;     // "str" | "int" | "date" | "bytes"
  bool multi = false;
  bool nullable = false;
};

struct VertexDecl {
  std::string label;
  int id = 0;           // label id (the key codec's u16)
  std::string group;    // logical supertype (Post -> Message); the label itself if ungrouped
  int tag = 0;          // vid tag (top 4 bits)
  std::vector<PropDecl> props;
};

struct EdgeDecl {
  std::string etype;
  int id = 0;
  bool reverse = false;
  std::string multiplicity = "unique";   // unique: at most one (src, etype, dst); multi: parallel edges
  std::vector<std::pair<std::string, std::string>> endpoints;
  std::vector<PropDecl> props;
};

struct IndexDecl {
  std::string label;
  std::string prop;     // "" for the per-label type (empty-tag) index
  std::string kind;     // "value" | "sorted" | "type"
  int id = 0;
  int width = 0;        // fixed string width of a value index on a str column; 0 otherwise
  std::string layout;   // optional: "sorted_keys" | "posting" (index/index_backend.h); "" = the format's default
  // the opt-in keying flags a design may set on a property index (Python Schema.type_scoped_index /
  // key_sorted_index / value_sorted_index); absent = false. Only dgraph's own index reads them --
  // its index is predicate-global day-token postings unless told otherwise.
  bool type_scoped = false;   // the index is keyed per (label, prop), not per predicate
  bool key_sorted = false;    // a date index with the ms IN THE KEY: one key-only entry per vertex
  bool value_sorted = false;  // a date index whose day posting is ordered by ms (Python-only today)
};

class Schema {
 public:
  static Schema load(const std::string& path);
  static Schema from_json(const json& j);

  const std::string& name() const { return name_; }
  const std::string& hash() const { return hash_; }
  // sha256 over the WHOLE schema.json (raw_.dump(): nlohmann sorts keys, so it is one
  // canonical serialization on every load path). The dataset hash above deliberately
  // EXCLUDES the adj/prop_groups blocks (they change what a STORE holds, not what the
  // dataset contains), so an in-place edit of only those lines leaves `hash` unchanged --
  // this fingerprint is what the store manifest pins against (session open checks it).
  const std::string& content_sha() const { return content_sha_; }
  const std::vector<VertexDecl>& vertices() const { return vertices_; }
  const std::vector<EdgeDecl>& edges() const { return edges_; }
  const std::vector<IndexDecl>& indexes() const { return indexes_; }

  const VertexDecl& vertex(const std::string& label) const;      // throws on unknown
  const EdgeDecl& edge(const std::string& etype) const;
  bool has_vertex(const std::string& label) const { return vidx_.count(label) > 0; }
  const std::vector<PropDecl>& edge_props(const std::string& etype) const;   // empty if none

  // Python Schema facts
  bool is_indexed(const std::string& label, const std::string& prop) const;   // value index
  bool is_date_col(const std::string& label, const std::string& prop) const;  // sorted index
  bool is_multi(const std::string& label, const std::string& prop) const;
  bool is_nullable(const std::string& label, const std::string& prop) const;
  bool has_type_index(const std::string& label) const { return type_indexed_.count(label) > 0; }
  // does the design declare any PROPERTY index (value or date; a type index has prop "")? The store cap
  // property_index (Format::store_capabilities; Python kvevaluator.plans.ir.capabilities.store_caps)
  bool has_property_index() const {
    for (auto& ix : indexes_)
      if (!ix.prop.empty()) return true;
    return false;
  }
  // the DSL's `predicates per_label`: a store that names its data by predicate (Dgraph) names a vertex property's
  // predicate per label (`Label.prop`), data keys and index keys alike; the other formats key by label anyway
  bool predicates_per_label() const { return predicates_per_label_; }
  std::vector<std::string> index_cols_of(const std::string& label) const;    // sorted names
  const std::string& group_of(const std::string& label) const;
  int tag_of_label(const std::string& label) const;                // vid tag of its group
  // labels whose rows live under a vid tag, in declaration order (Message -> Post, Comment)
  const std::vector<std::string>& labels_of_tag(uint64_t tag) const;
  // the declared endpoints: can a `label` vertex own etype's list in this direction (out = a source
  // endpoint, in = a destination endpoint)? Catalog knowledge, no KV op: a type-scoped layout that keys
  // adjacency by the owner's label never probes a label the schema says cannot hold the edge.
  bool owns_edge(const std::string& label, const std::string& etype, bool out) const;
  // the distinct labels the FAR end of an etype's entry can have in this direction (out: the destinations, in: the
  // sources), sorted by name; empty when the etype declares no endpoints
  std::vector<std::string> far_labels(const std::string& etype, bool out) const;
  // [(etype, out?)] -- every edge type that can touch `label`, and from which side (etype order);
  // what a vertex delete has to sweep (Python schema.incident_etypes)
  std::vector<std::pair<std::string, bool>> incident_etypes(const std::string& label) const;
  // nebula's rule: labels with a required (non-nullable) property index need no empty-tag index
  bool prop_indexed(const std::string& label) const { return prop_indexed_.count(label) > 0; }
  // the alphabetically first required indexed prop of a prop-indexed label (enumeration entry)
  std::string enumeration_prop(const std::string& label) const;

  // --- the physical adjacency directives + property groups (schema.json's post-hash
  // blocks; Python Schema.adj_sort / adj_inline / adj_sort_facet / adj_direction /
  // prop_groups). Empty maps when the blocks are absent (canonical / older artifacts).
  const std::map<std::string, std::string>& adj_sort() const { return adj_sort_; }
  const std::map<std::string, std::string>& adj_inline() const { return adj_inline_; }
  const std::map<std::string, std::string>& adj_sort_facet() const { return adj_sort_facet_; }
  bool is_dated_etype(const std::string& etype) const {
    return adj_sort_.count(etype) || adj_sort_facet_.count(etype);
  }
  // the declared walk side of a dated/inlined etype ("in" unless declared out)
  std::string adj_direction(const std::string& etype) const {
    auto it = adj_dir_.find(etype);
    return it == adj_dir_.end() ? "in" : it->second;
  }
  const std::map<std::string, std::vector<std::string>>& prop_groups() const { return prop_groups_; }
  // the group a predicate belongs to, or "" (Python's _pred_to_group)
  const std::string& group_of_pred(const std::string& prop) const {
    auto it = pred_group_.find(prop);
    return it == pred_group_.end() ? no_group_ : it->second;
  }

  const IndexDecl& index_decl(const std::string& label, const std::string& prop) const;  // throws
  const IndexDecl* find_index(const std::string& label, const std::string& prop) const;
  const IndexDecl& type_index_decl(const std::string& label) const;

  json to_json() const { return raw_; }

 private:
  std::string name_, hash_, content_sha_;
  json raw_;
  std::map<std::string, std::string> adj_sort_, adj_inline_, adj_sort_facet_, adj_dir_;
  std::map<std::string, std::vector<std::string>> prop_groups_;
  std::map<std::string, std::string> pred_group_;
  std::string no_group_;
  std::vector<VertexDecl> vertices_;
  std::vector<EdgeDecl> edges_;
  std::vector<IndexDecl> indexes_;
  std::map<std::string, size_t> vidx_, eidx_;
  std::map<std::pair<std::string, std::string>, size_t> iidx_;
  std::set<std::string> type_indexed_, prop_indexed_;
  std::map<std::string, std::vector<std::string>> group_members_;
  bool predicates_per_label_ = false;
  std::map<std::string, int> vid_tags_;                 // group -> tag
  std::map<uint64_t, std::vector<std::string>> tag_labels_;
  std::vector<PropDecl> no_props_;
  std::vector<std::string> no_labels_;
};

}  // namespace evalcpp
