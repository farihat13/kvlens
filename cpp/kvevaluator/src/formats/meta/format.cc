#include "formats/meta/format.h"
#include "formats/meta/format_shared.h"
#include "schema/far_labels.h"
#include "codec/decode_counts.h"

#include <algorithm>
#include <iterator>
#include <optional>
#include <unordered_map>
#include <unordered_set>
#include <stdexcept>
#include <vector>

#include "codec/columns.h"
#include "codec/lists.h"
#include "formats/common.h"
#include "formats/meta/keys.h"
#include "store/unit_value.h"
#include "util/uidset.h"
#include "util/vid.h"

namespace evalcpp::meta {

Meta::Meta(const Schema& schema, const Config& cfg) : schema_(schema), cfg_(cfg) {
  cfg_.validate();   // an unbuilt / invalid combination fails here, never silently measures another cell
  for (auto& e : schema.edges())   // a multi edge type needs a discriminator after dst (keys.h): not built
    if (e.multiplicity == "multi")
      throw std::runtime_error("meta: edge type " + e.etype + " declares multiplicity multi (not built: keys.h)");
  for (auto& v : schema.vertices()) {
    for (auto& p : v.props) {   // the stored columns: declared props only (vertex_cols; id is key-derived)
      if (schema.is_indexed(v.label, p.name)) indexed_.insert(p.name);
      else if (schema.is_date_col(v.label, p.name)) date_preds_.insert(p.name);
    }
    vcols_[v.label] = vertex_cols(v);
    if (vcols_[v.label].size() > 254)   // a Split record's attr byte is the column position + 1 (keys.h attr_of_col)
      throw std::runtime_error("meta: label " + v.label + " declares more than 254 props (the Split attr byte)");
  }
  for (auto& e : schema.edges()) {
    etypes_.insert(e.etype);
    if (e.reverse) reverse_.insert(e.etype);
    std::vector<ColDecl> cols;
    for (auto& p : e.props) cols.push_back({p.name, p.type, false});
    ecols_[e.etype] = cols;
  }
  // per-type overrides (config.h): the names must be the schema's (a typo must fail, not silently measure
  // the default), and the labels of one vid group share one props layout -- a message read probes Post
  // then Comment inside one fan-out, which is either replayed (packed) or counted directly (unpacked)
  for (auto& [label, c] : cfg_.vertex_props_by_label)
    if (!vcols_.count(label)) throw std::runtime_error("meta spec: vertex_props_by_label names unknown label '" + label + "'");
  for (auto& [etype, c] : cfg_.adjacency_by_etype)
    if (!ecols_.count(etype)) throw std::runtime_error("meta spec: adjacency_by_etype names unknown edge type '" + etype + "'");
  // per direction: the etype must exist, an "in" needs in lists to set (else the setting would change nothing),
  // and an etype's two directions agree on the edge-prop placement (own placement's chunks are one family per
  // etype, keyed by the OUT list's ordinals -- config.h)
  for (auto& [etype, d] : cfg_.adjacency_by_etype_dir) {
    if (!ecols_.count(etype))
      throw std::runtime_error("meta spec: adjacency_by_etype_dir names unknown edge type '" + etype + "'");
    if (d.in && !reverse_available(etype))
      throw std::runtime_error("meta spec: adjacency_by_etype_dir." + etype + ".in: this store keeps no in lists of " +
                               etype + " (direction \"declared\" and no @reverse in the schema) -- remove it");
    const Component& co = cfg_.adj_of(etype, true);
    const Component& ci = cfg_.adj_of(etype, false);
    if (reverse_available(etype) && (co.props_own() || ci.props_own()) && co != ci)
      throw std::runtime_error("meta spec: adjacency_by_etype_dir." + etype + ": edge_props \"own\" is per edge type "
                               "(one chunk family with one pack target, keyed by the out list's ordinals) -- give "
                               "both directions the same Chunked component with edge_props \"own\", or neither");
    if (cfg_.adjacency_by_etype.count(etype) && d.out && (d.in || !reverse_available(etype)))
      throw std::runtime_error("meta spec: adjacency_by_etype." + etype + " is never used: adjacency_by_etype_dir." +
                               etype + " sets every direction this store keeps -- remove one of the two");
  }
  // grouped props (vertex_props.groups): each listed label's lists must partition its declared props exactly,
  // on a Condensed props layout. The errors name the label and the prop, so a hand-edited typo fails here.
  for (auto& [label, lists] : cfg_.vertex_groups) {
    auto it = vcols_.find(label);
    if (it == vcols_.end()) throw std::runtime_error("meta spec: vertex_props.groups names unknown label '" + label + "'");
    const Component& pl = cfg_.props_of(label);
    if (!pl.grouped() || pl.packed())
      throw std::runtime_error("meta spec: vertex_props.groups." + label + ": grouped props need a Condensed props "
                               "layout (group owner, owners one) for " + label);
    if (lists.size() > 255)   // a later group's key carries its number in one byte (keys.h vertex_group_key)
      throw std::runtime_error("meta spec: vertex_props.groups." + label + ": more than 255 groups");
    std::string declared;
    for (auto& c : it->second) declared += (declared.empty() ? "" : ", ") + c.name;
    std::map<std::string, size_t> seen;
    for (size_t g = 0; g < lists.size(); g++)
      for (auto& p : lists[g]) {
        std::string where = "meta spec: vertex_props.groups." + label + " group " + std::to_string(g) + ": ";
        if (p == "id") throw std::runtime_error(where + "'id' is not a stored prop (the key holds it) -- remove it");
        bool known = false;
        for (auto& c : it->second) known = known || c.name == p;
        if (!known) throw std::runtime_error(where + "unknown prop '" + p + "' (" + label + " declares: " + declared + ")");
        if (seen.count(p))
          throw std::runtime_error(where + "prop '" + p + "' is listed twice (also in group " + std::to_string(seen[p]) +
                                   ") -- keep it in one group");
        seen[p] = g;
      }
    std::string missing;
    for (auto& c : it->second)
      if (!seen.count(c.name)) missing += (missing.empty() ? "" : ", ") + c.name;
    if (!missing.empty())
      throw std::runtime_error("meta spec: vertex_props.groups." + label + ": props missing: " + missing +
                               " -- every declared prop must be in exactly one group (add them to a group)");
  }
  // the far-end label: per (etype, dir) the labels its far end can have, from the schema's endpoints. k = 1: a hop's
  // neighbours are that label, whatever the setting (catalog knowledge, no stored byte). k > 1 (adjacency far_label
  // on): each entry stores a code of ceil(log2 k) bits. With the setting on, an etype with no endpoints cannot say
  // either, so it fails here
  for (auto& e : schema.edges()) {
    for (bool out : {true, false}) {
      const Component& al = cfg_.adj_of(e.etype, out);   // this direction's lists (the etype's when it has no override)
      if (al.far_label && e.endpoints.empty())
        throw std::runtime_error("meta spec: adjacency far_label needs the schema's endpoints, and edge type " + e.etype +
                                 " declares none (declare them, or set far_label false)");
      std::vector<std::string> far = schema.far_labels(e.etype, out);
      if (far.empty()) continue;
      far_labels_[{e.etype, out}] = far;
      if (!al.far_label) continue;   // off: no code stored (k = 1 still learned from the schema)
      check_far_label_count(far, e.etype, "meta spec");   // a code is one byte (far_code)
      far_bits_[{e.etype, out}] = far_code_bits(far.size());
    }
  }
  for (auto& [key, far] : far_labels_) {   // far_label_id's rule, tabulated per (etype, direction)
    FarIds ids;
    if (far.size() == 1) ids.any = schema.vertex(far[0]).id;             // k = 1: the schema's one label
    else if (far_bits_.count(key) && far_bits_.at(key))                    // k > 1: known only where a code is stored
      for (auto& l : far) ids.by_code.push_back(schema.vertex(l).id);
    far_ids_[key] = std::move(ids);
  }
  for (auto& [label, cols] : vcols_) {   // the row groups: the listed ones, else one group of every prop
    RowGroups rg;
    auto lg = cfg_.vertex_groups.find(label);
    size_t n = lg == cfg_.vertex_groups.end() ? 1 : lg->second.size();
    rg.cols.resize(n);
    rg.at.resize(n);
    for (size_t c = 0; c < cols.size(); c++) {   // declared order inside each group
      size_t g = 0;
      if (lg != cfg_.vertex_groups.end())
        while (std::find(lg->second[g].begin(), lg->second[g].end(), cols[c].name) == lg->second[g].end()) g++;
      rg.cols[g].push_back(cols[c]);
      rg.at[g].push_back(c);
      rg.group_of[cols[c].name] = g;
    }
    row_groups_[label] = std::move(rg);
  }
  std::map<std::string, std::string> group_first;   // vid group -> its first label
  for (auto& v : schema.vertices()) {
    auto [it, fresh] = group_first.emplace(v.group, v.label);
    if (!fresh && cfg_.props_of(v.label) != cfg_.props_of(it->second))
      throw std::runtime_error("meta spec: labels " + it->second + " and " + v.label + " share the vid group " + v.group +
                               " but not a props layout (per-type props are per vid group; give both the same component)");
  }
  // the index is a co-location COMPONENT (config.h): group item -> sorted_keys, owner -> posting (split =
  // the posting's overflow, chunk_target), owner+{pack} -> packed_posting (the sorted/date index chunked,
  // the interior cell). term -> the value granularity. validate() already rejected the unbuilt combinations.
  // Each corner's index: nebula sorted_keys/full, All-Condensed posting/day, pertype_csr posting/full, dgraph
  // posting/day (a per-type near-match of dgraph's bespoke GLOBAL date index).
  std::string idx_layout = "sorted_keys";
  uint64_t idx_chunk = UINT64_MAX;                        // sorted_keys ignores chunk_target
  if (cfg_.index_grouped() && cfg_.index.owners.packed()) {
    idx_layout = "packed_posting";
    idx_chunk = cfg_.index.owners.pack;                   // the pack target (many values per chunk)
  } else if (cfg_.index_grouped()) {
    idx_layout = "posting";
    // split sentinel: never/0 -> UINT64_MAX (one unsplit posting). NOT 0, which shatters a posting into
    // one KV per vid (unit_value.cc). The posting corner specs pass 65536 (the posting index's long-standing
    // overflow).
    idx_chunk = cfg_.index.split == 0 ? UINT64_MAX : cfg_.index.split;
  }
  index_ = make_index_backend(schema, idx_layout, idx_chunk,
                              cfg_.index_term == IndexTerm::day ? DateGranularity::day : DateGranularity::full);
}

const std::vector<ColDecl>& Meta::ecols(const std::string& etype) const {
  auto it = ecols_.find(etype);
  if (it == ecols_.end()) throw std::runtime_error("meta: unknown etype '" + etype + "'");
  return it->second;
}
const std::vector<ColDecl>& Meta::vcols(const std::string& label) const {
  auto it = vcols_.find(label);
  if (it == vcols_.end()) throw std::runtime_error("meta: unknown label '" + label + "'");
  return it->second;
}
uint8_t Meta::vtype_of(const std::string& label) const { return uint8_t(schema_.vertex(label).id); }
const Meta::RowGroups& Meta::row_groups(const std::string& label) const {
  auto it = row_groups_.find(label);
  if (it == row_groups_.end()) throw std::runtime_error("meta: unknown label '" + label + "'");
  return it->second;
}
std::vector<size_t> Meta::groups_for(const std::string& label, const std::vector<std::string>& props) const {
  const RowGroups& rg = row_groups(label);
  std::set<size_t> gs;
  for (auto& p : props) {
    auto it = rg.group_of.find(p);
    if (it != rg.group_of.end()) gs.insert(it->second);
  }
  if (gs.empty()) gs.insert(0);
  return {gs.begin(), gs.end()};
}
bool Meta::probe_label(CountingStore& st, const std::string& label, uint64_t u, const std::vector<std::string>& props,
                       std::map<std::string, Value>& cells) {
  GroupPlan plan = group_plan(label, props);
  columns::View view;
  std::vector<Value> got(props.size());
  if (!probe_planned(st, label, u, plan, view, got)) return false;
  for (auto& gc : plan.cells)   // a position no touched row answered reads as null, as a missing entry does
    for (auto& [i, c] : gc) cells[props[i]] = got[i];
  return true;
}
bool Meta::probe_planned(CountingStore& st, const std::string& label, uint64_t u, const GroupPlan& plan,
                         columns::View& view, std::vector<Value>& cells) {
  size_t g = plan.groups[0];
  std::string k = vertex_group_key(vtype_of(label), u, g);
  std::optional<std::string> blob = st.get(k);
  if (blob) st.count_used(k.size(), 0);
  else if (g != 0) {
    std::string k0 = vertex_key(vtype_of(label), u);
    if (!st.get(k0)) return false;                  // no existence row: not this label
    st.count_used(k0.size(), 0);
  } else return false;
  project_planned(st, label, u, plan, {{g, std::move(blob)}}, view, cells);
  return true;
}

bool Meta::project_groups(CountingStore& st, const std::string& label, uint64_t u, const std::vector<std::string>& props,
                          const std::map<size_t, std::optional<std::string>>& have, std::map<std::string, Value>& cells) {
  GroupPlan plan = group_plan(label, props);
  columns::View view;
  std::vector<Value> got(props.size());
  bool any = project_planned(st, label, u, plan, have, view, got);
  for (auto& gc : plan.cells)   // a position no touched row answered reads as null, as a missing entry does
    for (auto& [i, c] : gc) cells[props[i]] = got[i];
  return any;
}
Meta::GroupPlan Meta::group_plan(const std::string& label, const std::vector<std::string>& props) const {
  const RowGroups& rg = row_groups(label);
  GroupPlan plan;
  plan.groups = groups_for(label, props);
  for (size_t g : plan.groups) {
    std::vector<std::pair<size_t, size_t>> cs;
    for (size_t i = 0; i < props.size(); i++) {
      const std::vector<ColDecl>& cols = rg.cols[g];
      for (size_t c = 0; c < cols.size(); c++)
        if (cols[c].name == props[i]) { cs.emplace_back(i, c); break; }   // View::col_index's answer: the first match
    }
    plan.cells.push_back(std::move(cs));
  }
  return plan;
}
bool Meta::project_planned(CountingStore& st, const std::string& label, uint64_t u, const GroupPlan& plan,
                           const std::map<size_t, std::optional<std::string>>& have, columns::View& view,
                           std::vector<Value>& cells) {
  const RowGroups& rg = row_groups(label);
  bool any = false;
  uint64_t uv = 0;
  RtParallel par(st);   // the touched groups' keys follow from the schema: one batch
  for (size_t gi = 0; gi < plan.groups.size(); gi++) {
    size_t g = plan.groups[gi];
    par.member();
    std::optional<std::string> blob;
    auto h = have.find(g);
    if (h != have.end()) blob = h->second;
    else {
      std::string k = vertex_group_key(vtype_of(label), u, g);
      blob = st.get(k);
      if (blob) st.count_used(k.size(), 0);
    }
    if (!blob) continue;   // an absent later group: its cells are all absent
    any = true;
    view.reset(&rg.cols[g], *blob, 0, 1);   // the group's row: its columns, one row (read before `blob` goes)
    for (auto& [i, c] : plan.cells[gi]) {
      cells[i] = view.cell(c, 0);
      uv += view.cell_bytes(c, 0);
    }
  }
  if (any) st.count_used(0, uv);
  return any;
}
int Meta::attr_id(const std::string& label, const std::string& prop) const {
  const std::vector<ColDecl>& cols = vcols(label);   // singleton keys by the prop's column (per label)
  for (size_t c = 0; c < cols.size(); c++)
    if (cols[c].name == prop) return attr_of_col(c);
  return -1;
}
bool Meta::reverse_available(const std::string& etype) const {
  return cfg_.dir_both || reverse_.count(etype) > 0;
}
std::vector<std::string> Meta::labels_or_throw(const char* op, const std::vector<std::string>& label) const {
  if (label.empty()) throw std::runtime_error(std::string("meta ") + op + ": needs the step's label(s)");
  for (auto& l : label) vcols(l);
  return label;
}
const Component& Meta::props_layout(const char* op, const std::vector<std::string>& labels) const {
  if (labels.empty()) throw std::runtime_error(std::string("meta ") + op + ": needs the step's label(s)");
  const Component& c = cfg_.props_of(labels[0]);
  for (auto& l : labels)
    if (cfg_.props_of(l) != c)
      throw std::runtime_error(std::string("meta ") + op + ": labels " + labels[0] + " and " + l +
                               " have different props layouts (one op reads one layout; not built)");
  return c;
}
unsigned Meta::far_bits(const std::string& etype, bool out) const {
  auto it = far_bits_.find({etype, out});
  return it == far_bits_.end() ? 0 : it->second;
}
uint8_t Meta::far_code(const std::string& etype, bool out, const std::string& label) const {
  auto it = far_labels_.find({etype, out});
  if (it == far_labels_.end()) return 0;   // far_label off: nothing stored, nothing learned
  return far_label_code(it->second, label, etype, out, "meta");   // k = 1 is checked too: a hop learns that one label
}
const Meta::FarIds& Meta::far_ids(const std::string& etype, bool out) const {
  static const FarIds none;                                                    // no endpoints: nothing known
  auto it = far_ids_.find({etype, out});
  return it == far_ids_.end() ? none : it->second;
}
int Meta::far_label_id(const std::string& etype, bool out, uint8_t code) const {
  return far_ids(etype, out).of(code);
}
std::optional<std::string> Meta::tag_label(const Value& v) const {
  if (v.label < 0) return std::nullopt;
  for (auto& vd : schema_.vertices())
    if (vd.id == v.label) return vd.label;
  return std::nullopt;
}
Value Meta::tag_all(const Value& vids, const std::string& label) const {
  ValueList out;
  out.reserve(vids.as_list().size());
  for (auto& v : vids.as_list()) out.push_back(tagged(v, label));
  return Value::list(std::move(out));
}
Value Meta::tagged(Value v, const std::string& label) const {
  if (!label.empty()) v.label = int16_t(schema_.vertex(label).id);
  return v;
}
std::vector<std::string> Meta::narrow(const Value& vid, const std::vector<std::string>& labs) const {
  std::optional<std::string> k = tag_label(vid);
  if (!k) return labs;
  if (std::find(labs.begin(), labs.end(), *k) != labs.end()) return {*k};
  return {};
}
Value Meta::nbr_item(const NeighborItems& item, const Value* src_val, uint64_t su, uint64_t dst, int lid,
                     const Value* facets) const {
  Value d = Value::vid(dst);
  if (src_val && lid >= 0) d.label = int16_t(lid);   // the hop's far end, on the value itself
  return item(src_val ? *src_val : Value::vid(su), d, facets);   // the input value: keeps its tag
}
std::vector<std::string> Meta::props_row_names(const Component& layout, const std::vector<std::string>& props) const {
  if (!layout.grouped()) return props_names(props);   // Split: in the asked order (split_props)
  bool want_id = false;                                // Condensed / Chunked: the key-derived id first, then the props
  std::vector<std::string> row_props;
  for (auto& p : props) (p == "id") ? (void)(want_id = true) : (void)row_props.push_back(p);
  return props_names_id_first(want_id, row_props);
}
Value Meta::absent_row(const Component& layout, uint64_t u, const std::vector<std::string>& props,
                       const RowBuilder& rb) const {
  RowBuilder::Row f = rb.start();
  f.push(Value::vid(u));
  if (layout.grouped()) {   // Condensed / Chunked: the key-derived id first, then the props (props_of, props_of_packed)
    for (auto& p : props) if (p == "id") { f.push(key_id_value(u)); break; }
    for (auto& p : props) if (p != "id") f.push(Value());
  } else {                  // Split: in the asked order (split_props)
    for (auto& p : props) f.push(p == "id" ? key_id_value(u) : Value());
  }
  return f.done();
}

bool Meta::group_owns(uint64_t owner, const std::string& etype, bool out) const {
  for (auto& lab : schema_.labels_of_tag(tag_of_uid(owner)))
    if (schema_.owns_edge(lab, etype, out)) return true;
  return false;
}
std::vector<bool> Meta::dirs_of(const std::string& op, const std::string& etype, const std::string& dir) const {
  if (!etypes_.count(etype)) throw std::runtime_error("meta: unknown etype '" + etype + "'");
  std::vector<bool> dirs = parse_dirs(op, dir);
  for (bool d : dirs)
    if (!d && !reverse_available(etype))
      throw std::runtime_error("meta: '" + etype + "' has no reverse in this store");
  return dirs;
}

// --- caps: derived from the configured index backend + the props/adjacency layout, so meta's caps
// match the format a corner reproduces (nebula/pertype_csr exactly; dgraph modulo per_type_index) ----
// VERTEX caps are per LABEL: a label's own props layout (capabilities_for). The store-wide set is their INTERSECTION
// over the labels, so the planner's un-annotated pick runs on every label it may touch. A variant annotated `on` a
// label (dump_planir._on_labels: the labels its steps touch) is checked on that label's own set -- the one definition
// the C++ selector (store_capabilities_for), the Python selector and the grid's validity gate (caps_resolver) read.
// Mirrors Python MetaFormat.capabilities_for; check_caps_parity compares the two per label.
std::set<std::string> Meta::label_free_capabilities() const {
  std::set<std::string> caps = {"label_scoped_props"};   // every props layout keys by vtype (keys.h vertex_key)
  // INDEX caps derive from the backend, exactly as nebula/pertype_csr's own capabilities() do:
  // a full-precision index stops a range walk early (sorted_range); a day bucket is a coarse pre-filter
  // whose range returns the boundary-day superset (unsorted_range).
  caps.insert(index_->date_granularity() == DateGranularity::full ? "sorted_range_index" : "unsorted_range_index");
  caps.insert("per_type_index");   // meta's index is per-type (make_index_backend); dgraph's GLOBAL date index (no per_type) is out of scope
  return caps;
}

std::set<std::string> Meta::capabilities_for(const std::string& label) const {
  std::set<std::string> caps = label_free_capabilities();
  if (schema_.has_type_index(label)) {
    caps.insert("type_index");
    if (index_->single_posting_type_index()) caps.insert("type_index_single_posting");   // a posting holds the whole type in one unit
  }
  const auto& props = cfg_.props_of(label);
  // a grouped-row layout (nebula/pertype_csr) reads a whole row in one GET; singleton (dgraph) can't.
  // Grouped props keep the cap: a whole-row read is valid there and pays one GET per group row it touches.
  if (props.grouped()) caps.insert("whole_row_read");
  // a chunked vertex layout enumerates by walking the label's chunks, so a label scan projects props in the same
  // pass (pertype_csr) -- read_vertices_by_label(props) checks exactly this label's layout
  if (props.packed()) caps.insert("label_scan_carries_props");
  // every props layout keys a label's records together ('v'·vtype·...): one scan of that run may read the label
  // (read_vertices_by_label with props). Validity only: whether the scan is the cheaper plan is the pick's question
  caps.insert("label_rows_one_run");
  caps.insert("whole_label_scan_by_default");   // and a whole-label read with props takes that scan by default
  return caps;
}

std::set<std::string> Meta::capabilities() const {
  if (schema_.vertices().empty()) return label_free_capabilities();
  std::optional<std::set<std::string>> all;
  for (auto& v : schema_.vertices()) {
    std::set<std::string> c = capabilities_for(v.label);
    if (!all) { all = std::move(c); continue; }
    std::set<std::string> both;
    std::set_intersection(all->begin(), all->end(), c.begin(), c.end(), std::inserter(both, both.begin()));
    all = std::move(both);
  }
  return *all;
}
// the SMALLEST vertex-props pack over the labels (props_of: the per-label override, else the default); nullopt if any
// label's props are not packed -- the every-label intersection capabilities() uses. Mirrors Python MetaFormat.pack_target.
std::optional<uint64_t> Meta::pack_target() const {
  std::optional<uint64_t> least;
  for (auto& v : schema_.vertices()) {
    const auto& c = cfg_.props_of(v.label);
    if (!c.packed()) return std::nullopt;
    if (!least || c.owners.pack < *least) least = c.owners.pack;
  }
  return least;
}

bool Meta::has_edge_type(const std::string& name) const {
  for (auto& e : schema_.edges())
    if (e.etype == name) return true;
  return false;
}
// per edge type, from the spec: the in lists where the store keeps them (direction both, or the schema's @reverse);
// lists when every stored direction of it is Condensed or Chunked (one Split direction: not advertised)
std::set<std::string> Meta::edge_capabilities_for(const std::string& etype) const {
  std::set<std::string> caps;
  bool rev = reverse_available(etype);
  if (rev) caps.insert("reverse_adjacency");
  bool lists = true;
  for (bool out : {true, false}) {
    if (!out && !rev) continue;
    const Component& c = cfg_.adj_of(etype, out);
    lists = lists && (c.packed() || c.grouped());
  }
  if (lists) caps.insert("adjacency_lists");
  return caps;
}

// the contract's reads every format serves (ops/kv_contract.h), its optional index_range_buckets, and the writes
bool Meta::has_kv_op(const std::string& op) const {
  return kv::is_every_format_read_op(op) || op == "index_range_buckets" || is_write_op(op);
}

Value Meta::run_kv_op(const Step& step, const Args& a, StepExec& ex) {
  const std::string& op = step.op;
  // Packed (chunked) reads use the thread-safe shared cache (exec/chunk_cache.h), so they run at any P;
  // the logical S/G/N are the replay of the landings, which is P-independent, and the executor asserts
  // physical == logical only at P=1 (physical < logical when a sibling task already landed on a chunk).
  if (op == "read_vertex_by_id") {
    const Value& vid = a.get("vid");
    if (vid.is_text()) return Value::list({vid});   // vid is the key: 0 ops
    std::vector<std::string> labs = a.labels("label");
    // singleton (group item): no existence probe, the candidates pass as they are. With no label, that
    // holds only when EVERY label is singleton (else read_vertex_by_id refuses the missing label)
    bool item = true;
    if (labs.empty()) for (auto& v : schema_.vertices()) item = item && !cfg_.props_of(v.label).grouped();
    else item = !props_layout("read_vertex_by_id", labs).grouped();
    if (item) return read_vertex_by_id_split(ex, vid.as_list(), labs);
    return read_vertex_by_id(ex, vid, labs);
  }
  if (op == "read_vertex_props") return read_vertex_props(ex, a.list("vids"), a.str_list("props"), a.labels("label"));
  if (op == "read_anchor_props") return read_anchor_props(ex, a.list("cands"), a.str_list("props"), a.labels("labels"));
  if (op == "read_neighbors") {
    kv::NeighborsArgs n = kv::neighbors_args(a);
    return read_neighbors(ex, *n.srcs, n.etype, n.dir, n.edge_props, n.pairs, SIZE_MAX, 0, n.labels);
  }
  if (std::optional<Value> v = run_operator(*this, schema_, op, a, ex)) return *v;   // closure, chase, edge exists, top k
  if (op == "read_index_eq" || op == "read_index_range") {   // one index walk (S=1/#labels, N=matches)
    // a label's index holds only that label's vertices: each output vid carries its index's label (the backends walk
    // one label after another, so a range is read per label -- the same ops -- to know which label each vid came from).
    // The label comes from the index, not from a charged read, so it is kept under every label_tags setting, as in
    // every format
    kv::IndexRangeArgs ir = op == "read_index_range" ? kv::index_range_args(a, op) : kv::IndexRangeArgs{};
    if (op == "read_index_range") labels_or_throw("read_index_range", ir.labels);   // each label is the schema's
    std::vector<Value> r = ex.run_range_map(1, [&](WorkerSlot& slot, size_t, std::vector<Landing>&) {
        if (op == "read_index_eq")
          return tag_all(Value::list(index_->eq(slot.store, a.str("label"), a.str("prop"), a.get("value"))), a.str("label"));
        ValueList out;
        RtParallel par(slot.store);   // one range per label, the labels are the plan's
        for (auto& lab : ir.labels) {
          par.member();
          ValueList part = index_->range(slot.store, {lab}, ir.prop, ir.lo, ir.hi);
          for (auto& v : part) out.push_back(tagged(v, lab));
        }
        return Value::list(std::move(out));
    }, /*additive_counts=*/true);
    return r[0];
  }
  if (op == "read_vertices_by_label") return read_vertices_by_label(ex, a.str("label"), a.opt("props"), a.opt("keep"));
  if (op == "read_filter_by_label_probe") return read_filter_by_label_probe(ex, a.list("vids"), a.labels("label"));
  if (op == "read_filter_by_label_index") return read_filter_by_label_index(ex, a.list("vids"), a.labels("label"));
  if (op == "index_range_buckets") {   // the bucketed-range plan under a day index (microbench entry_range)
    // over the same index walk: each hit with its day, so the plan reads only the boundary days' dates.
    // The contract is dgraph's (the plans pass hi = end - 1): the days day(lo)..day(hi) INCLUSIVE, so the walk's
    // exclusive bound is hi + 1. Read per label, as read_index_range is
    kv::IndexRangeArgs ir = kv::index_range_args(a, op);
    labels_or_throw("index_range_buckets", ir.labels);   // each label is the schema's
    int64_t lo = ir.lo, hi = ir.hi, hi_excl = hi == INT64_MAX ? hi : hi + 1;
    std::vector<Value> r = ex.run_range_map(1, [&](WorkerSlot& slot, size_t, std::vector<Landing>&) {
        ValueList out;
        RowBuilder bk({"_vid", "_bucket"});              // the call's one shape
        bool day_terms = index_->date_granularity() == DateGranularity::day;   // the term IS the day already
        RtParallel par(slot.store);
        for (auto& lab : ir.labels) {
          par.member();
          for (auto& h : index_->range_hits(slot.store, {lab}, ir.prop, lo, hi_excl)) {
            RowBuilder::Row b = bk.start();
            b.push(tagged(Value::vid(h.vid), lab));   // the index's label, under every label_tags setting
            b.push(Value(day_terms ? h.bucket : date_bucket_of(h.bucket, DateGranularity::day)));
            out.push_back(b.done());
          }
        }
        return Value::list(std::move(out));
    }, /*additive_counts=*/true);
    return r[0];
  }
  if (is_write_op(op)) return run_write_op(step, a, ex);   // meta/writes.cc (per-component dispatch)
  throw std::runtime_error("meta: unknown kv op '" + op + "'");
}

// the key's store PART (parts.h) names its role: props:* vertex data, adj:* / eprops:* edge data, index:* an index
// one KV's property-cell payload (S5): the part (keys.h "Parts") names the label or edge type, the config its layout;
// every layout decodes its cells and sums columns::payload_of, so the payload is one definition, the framing the rest.
// Index parts: 0 (the index is held fixed, not an axis).
std::optional<uint64_t> Meta::payload_bytes(std::string_view key, std::string_view value) const {
  const PartScheme* ps = part_scheme();
  std::string part = ps->name(ps->part_of(key));
  auto field = [&](size_t i) {                    // part = kind:f1:f2:...
    size_t at = 0;
    for (size_t k = 0; k < i; k++) at = part.find(':', at) + 1;
    size_t end = part.find(':', at);
    return part.substr(at, end == std::string::npos ? std::string::npos : end - at);
  };
  auto row_payload = [](const std::vector<ColDecl>& cols, const Value& row) {
    uint64_t n = 0;
    for (auto& c : cols) n += columns::payload_of(c, row.get(c.name));
    return n;
  };
  std::string raw(value);
  if (part.rfind("props:", 0) == 0) {
    std::string label = field(1);
    const Component& pl = cfg_.props_of(label);
    const std::vector<ColDecl>& cols = vcols(label);
    if (pl.packed()) {
      auto chunk = PropsChunk::decode(&cc_, &cols, raw);
      uint64_t n = 0;
      for (size_t r = 0; r < chunk->size(); r++) n += row_payload(cols, chunk->row(r));
      return n;
    }
    if (!pl.grouped()) {                          // Split: 'v'·vtype·vid·attr, the value is the cell's payload alone
      const ColDecl& col = cols[uint8_t(key.back()) - 1];
      return columns::payload_of(col, columns::decode_cell(col, raw));
    }
    size_t g = key.size() > 10 ? uint8_t(key[10]) : 0;   // a grouped row: group 0 is the plain vertex key
    const std::vector<ColDecl>& gc = row_groups(label).cols[g];
    return row_payload(gc, columns::View(&gc, raw, 0, 1).row(0));
  }
  if (part.rfind("adj:", 0) == 0 || part.rfind("eprops:", 0) == 0) {
    bool own_chunk = part.rfind("eprops:", 0) == 0;
    std::string etype = field(2);
    const std::vector<ColDecl>& cols = ecols(etype);
    if (cols.empty()) return 0;
    const Component& al = cfg_.adj_of(etype, own_chunk || field(3) == "out");   // the part's direction
    if (own_chunk) {                              // own placement: the edge-prop chunk's rows
      auto chunk = PropsChunk::decode(&cc_, &cols, raw);
      uint64_t n = 0;
      for (size_t r = 0; r < chunk->size(); r++) n += row_payload(cols, chunk->row(r));
      return n;
    }
    unsigned bits = far_bits(etype, field(3) == "out");   // the label block / byte: framing, never payload
    if (al.packed()) {
      if (al.props_own()) return 0;               // ordinals only; the props are in the eprops part
      lists::ChunkView cv(&cols, raw, bits);
      uint64_t n = 0;
      for (size_t i = 0; i < cv.size(); i++)
        for (size_t j = 0, d = cv.dsts(i).size(); j < d; j++) n += row_payload(cols, cv.props_row(i, j));
      return n;
    }
    if (al.grouped()) {
      lists::View lv(&cols, raw, bits);
      uint64_t n = 0;
      for (size_t e = 0; e < lv.size(); e++) n += row_payload(cols, lv.props_row(e));
      return n;
    }
    if (bits) raw.erase(0, 1);                    // a Split edge record: [the far label's code byte] + its columns
    return raw.empty() ? 0 : row_payload(cols, columns::View(&cols, raw, 0, 1).row(0));
  }
  return 0;
}

// a stored adjacency list / part, chunk or own edge-prop chunk holding nothing (S4 A2: an emptied unit is DELed, so
// none should exist); vertex rows and index entries are never "empty"
std::optional<bool> Meta::empty_value(std::string_view key, std::string_view value) const {
  const PartScheme* ps = part_scheme();
  std::string part = ps->name(ps->part_of(key));
  bool adj = part.rfind("adj:", 0) == 0, ep = part.rfind("eprops:", 0) == 0;
  if (!adj && !ep) return false;
  std::string etype = part.substr(part.find(':', part.find(':') + 1) + 1);
  std::string dir = etype.find(':') == std::string::npos ? "" : etype.substr(etype.find(':') + 1);
  etype = etype.substr(0, etype.find(':'));
  const std::vector<ColDecl>& cols = ecols(etype);
  const Component& al = cfg_.adj_of(etype, ep || dir == "out");   // the part's direction
  std::string raw(value);
  if (ep) return PropsChunk::decode(&cc_, &cols, raw)->size() == 0;
  unsigned bits = far_bits(etype, dir == "out");
  if (al.packed()) {
    bool own = al.props_own();   // own placement: ordinals where the columns block is
    lists::ChunkView cv(own ? &kNoCols : &cols, raw, bits, own && !cols.empty());
    cv.validate();               // the view builds its ordinals and props lazily: census checks every chunk whole
    return cv.size() == 0;
  }
  if (al.grouped()) {
    // a split list's FIRST part is kept when a write empties it (store/unit_list.h: every id keeps a part to land
    // on), so an empty value there is the layout, not a leftover; the owner's delete DELs it with the other parts
    size_t unit_len = adj_prefix(0, true, 0).size();
    if (unit::is_part_key(key, unit_len) && unit::part_start(key, unit_len) == 0) return false;
    return lists::View(&cols, raw, bits).size() == 0;
  }
  return false;                                   // a Split edge record: the key is the edge
}

std::vector<ValueInspector::FarLabel> Meta::far_label_entries(std::string_view key, std::string_view value) const {
  const PartScheme* ps = part_scheme();
  std::string part = ps->name(ps->part_of(key));
  if (part.rfind("adj:", 0) != 0) return {};
  size_t a = part.find(':', 4), b = part.find(':', a + 1);
  std::string etype = part.substr(a + 1, b - a - 1), dir = part.substr(b + 1);
  bool out = dir == "out";
  unsigned bits = far_bits(etype, out);
  if (!bits) return {};
  const std::vector<std::string>& far = far_labels_.at({etype, out});
  const Component& al = cfg_.adj_of(etype, out);
  const std::vector<ColDecl>& cols = ecols(etype);
  std::string fam = etype + ":" + dir;
  std::vector<FarLabel> got;
  uint64_t owner = 0;   // 'e'·etype·dir·owner (keys.h adj_prefix) for a list or a record
  for (size_t i = 3; i < 11 && i < key.size(); i++) owner = (owner << 8) | uint8_t(key[i]);
  if (al.packed()) {
    bool own = al.props_own();   // own placement: ordinals where the columns block is
    lists::ChunkView cv(own ? &kNoCols : &cols, std::string(value), bits, own && !cols.empty());
    std::vector<uint64_t> srcs = cv.owners();
    for (size_t i = 0; i < cv.size(); i++) {
      std::vector<uint64_t> d = cv.dsts(i);
      for (size_t j = 0; j < d.size(); j++) got.emplace_back(fam, srcs[i], d[j], far.at(cv.label(i, j)));
    }
  } else if (al.grouped()) {
    lists::View lv(&cols, value, bits);
    for (size_t e = 0; e < lv.size(); e++) got.emplace_back(fam, owner, lv.dsts()[e], far.at(lv.label(e)));
  } else {
    got.emplace_back(fam, owner, edge_dst(key), far.at(uint8_t(value.at(0))));
  }
  return got;
}

std::optional<std::tuple<std::string, uint64_t, uint64_t>> Meta::chunk_range(std::string_view key,
                                                                               std::string_view value) const {
  const PartScheme* ps = part_scheme();
  std::string part = ps->name(ps->part_of(key));
  if (part.rfind("props:", 0) == 0) {   // a packed props chunk: family props:<label>, its first and last vid
    std::string label = part.substr(6);
    if (!cfg_.props_of(label).packed()) return std::nullopt;
    auto c = PropsChunk::decode(&cc_, &vcols(label), std::string(value));
    if (c->size() == 0) return std::nullopt;
    return std::make_tuple(part, c->first(), c->last());
  }
  if (part.rfind("adj:", 0) != 0) return std::nullopt;
  size_t a = part.find(':', 4), b = part.find(':', a + 1);
  std::string etype = part.substr(a + 1, b - a - 1), dir = part.substr(b + 1);
  const Component& al = cfg_.adj_of(etype, dir == "out");
  if (!al.packed()) return std::nullopt;
  const std::vector<ColDecl>& cols = ecols(etype);
  std::string raw(value);
  if (al.props_own()) {
    lists::ChunkView c(&kNoCols, raw, far_bits(etype, dir == "out"), !cols.empty());
    if (c.size() == 0) return std::nullopt;
    return std::make_tuple(etype + ":" + dir, c.first(), c.last());
  }
  lists::ChunkView cv(&cols, raw, far_bits(etype, dir == "out"));
  if (cv.size() == 0) return std::nullopt;
  return std::make_tuple(etype + ":" + dir, cv.first(), cv.last());
}

// the key's store part (parts.h) decides everything: props:<label> vertex data, adj:<owner>:<etype>:<dir> and
// eprops:<owner>:<etype> edge data, index:<label>[.<prop>] an index; a name of another shape is unclassified
KeyClass Meta::classify(std::string_view key) const {
  const PartScheme* ps = part_scheme();
  std::string name = ps->name(ps->part_of(key));
  std::vector<std::string> f;
  size_t a = 0;
  for (size_t b; (b = name.find(':', a)) != std::string::npos; a = b + 1) f.push_back(name.substr(a, b - a));
  f.push_back(name.substr(a));
  if (f[0] == "props" && f.size() == 2) return {"vertex_props", f[1], "", "", "", "vertex", "props"};
  if (f[0] == "adj" && f.size() == 4) return {"adjacency", f[2], f[3], f[1], "", "edge", "adj"};
  if (f[0] == "eprops" && f.size() == 3) return {"edge_props", f[2], "", f[1], "", "edge", "eprops"};
  if (f[0] == "index" && f.size() == 2) {
    size_t dot = f[1].find('.');
    return {"index", f[1].substr(0, dot), "", "", dot == std::string::npos ? "" : f[1].substr(dot + 1), "index-vertex", "idx"};
  }
  // the role as before: a props: / adj: / eprops: / index: name of another field count keeps its role
  if (name.rfind("props:", 0) == 0) return {"unclassified", "", "", "", name, "vertex", "props"};
  if (name.rfind("adj:", 0) == 0) return {"unclassified", "", "", "", name, "edge", "adj"};
  if (name.rfind("eprops:", 0) == 0) return {"unclassified", "", "", "", name, "edge", "eprops"};
  if (name.rfind("index:", 0) == 0) return {"unclassified", "", "", "", name, "index-vertex", "idx"};
  return {"unclassified", "", "", "", name, "other", "other"};
}

}  // namespace evalcpp::meta
