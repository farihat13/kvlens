// The statistics file predict mode reads (stats.json: kvevaluator graph-stats, written by python -m
// kvevaluator.predict.build_stats) and its optional anchor file (the anchor tier: per-vertex facts of the params' bound
// vertices): graph statistics of one dataset, format-free, every field keyed by schema names
// (inputs/contract/stats.schema.json). The accessors the cardinality layer uses.
#pragma once
#include <array>
#include <optional>
#include <set>
#include <string>
#include <vector>

#include "value/value.h"   // json

namespace evalcpp::predict {

struct DatasetStats {
  json d;
  double total_vertices = 0;
  // the schema each file was built from (the schema's dataset hash, Schema::hash): the statistics' schema_hash and
  // the anchor file's
  std::string stats_schema_hash, anchor_schema_hash;
  std::string stats_scale;   // the statistics' provenance.sf
  std::string stats_path, anchor_path;

  // the statistics, with the anchor tier (anchor_degrees, anchor_recency) of `anchor` when it is given. Refuses a
  // file whose degree_hist, date_days, neighbor_degree_ratio or burstiness disagrees with its tier-1 counts (a hand-written flat form)
  static DatasetStats load(const std::string& path, const std::string& anchor = "");
  explicit DatasetStats(json j);
  // refuses (throws) unless every file loaded was built from the schema with this dataset hash: a stats file of another
  // schema would otherwise be read silently. `against` names where the hash came from (a --schema file, a store)
  void require_schema(const std::string& hash, const std::string& against) const;
  // refuses (throws) unless stats.json was built at this scale (a store's manifest `scale`, a request's store.scale).
  // graph-stats records no scale: load already refuses a graph file whose type counts differ from stats.json's
  void require_scale(const std::string& scale, const std::string& against) const;
  // the same statistics without the anchor tier (the bound vertex's own degrees and recency rows). It is conditioned
  // on the query's parameter, so predict mode proper (level predicted) reads the population statistics instead; the accuracy
  // check reports the level with it apart
  DatasetStats without_anchor_stats() const;
  static const std::vector<std::string>& anchor_stat_keys();
  // the fields load refuses a file without (inputs/contract/stats.schema.json's "required"), and one line of each
  // field's meaning for a refusal's message
  static const std::vector<std::string>& required_fields();
  static std::string field_meaning(const std::string& field);

  double type_count(const std::string& label) const;
  double type_count(const std::vector<std::string>& labels) const;
  double mean_degree(const std::string& etype, const std::string& dir) const;
  double population_degree(const std::string& etype, const std::string& dir) const;
  double population_count(const std::string& etype, const std::string& dir) const;
  // the labels at one end of an edge type ("out": its sources' end, else its destinations'), from the sizes.content keys
  std::vector<std::string> side_labels(const std::string& etype, const std::string& dir) const;
  double edges(const std::string& etype) const;   // edge_totals: the edges of a type, refused by name when missing
  double participant_count(const std::string& etype, const std::string& dir) const;
  std::optional<double> cond_ratio(const std::string& via, const std::string& etype, const std::string& dir) const;
  // a neighbour's degree over (etype, dir): sum d^2 / sum d over the vertices with such an edge (degree_hist, every
  // label holding it), the degree of a vertex reached through that edge type; else neighbor_degree_ratio (that
  // degree over the mean degree, edges / owners) times the mean degree. Refused without either, the message giving
  // the flat form (1.0)
  double neighbor_degree(const std::string& etype, const std::string& dir) const;
  // the ranked date-index walk's burstiness over a hop's far ends (burstiness, key etype|dir|prop): per owner of a list,
  // the index entries newer than its k-th newest neighbour (k = 20) over that rank under an even spread, the mean
  // weighted by the owners' list lengths. Refused without the key, the message giving the flat form (1.0)
  double burstiness(const std::string& etype, const std::string& dir, const std::string& prop,
                    const std::string& rule) const;
  // the list a new edge from a `far`-label vertex grows (degree_by_far_label, key owner|etype|dir|far; an empty owner
  // or far label sums over every declared one): {edges from that label, the summed degree of the owner each lands on}.
  // Refused without the field or the key, the message giving the flat form (degree_hist's sum d^2 / sum d)
  std::array<double, 2> far_degree(const std::string& owner, const std::string& etype, const std::string& dir,
                                   const std::string& far, const std::string& rule) const;
  // the anchor vertex's own degree on (etype, dir) (anchor_degrees); nullopt when it is no anchor (or level predicted)
  std::optional<double> anchor_degree(const std::string& vid, const std::string& etype, const std::string& dir) const;
  // the anchor vertex's own edges on (etype, dir) by far-end label, as shares (anchor_far_labels); nullopt when it has
  // no row there (no anchor, level predicted, an old anchor file, or a far end of one label): the caller takes the population
  std::optional<std::map<std::string, double>> anchor_far_share(const std::string& vid, const std::string& etype,
                                                                const std::string& dir) const;
  // a forest's mean depth (hops to the chain's root): over its source labels' vertices, or over every label's when
  // `all`; nullopt when the edge type is no forest
  std::optional<double> forest_depth(const std::string& etype, bool all) const;
  double ndv(const std::string& label, const std::string& prop) const;
  // the vertices of `label` whose indexed `prop` equals `value` (top_values, key label|prop): a listed value's count; else
  // the rest spread evenly, (holders - the listed counts) / (ndv - listed), 0 when every value is listed. The one rule
  // for an equality's rows on an indexed property (index read and filter). Refused by name without the entry
  double value_rows(const std::string& label, const std::string& prop, const json& value, const std::string& rule) const;
  // whether the file holds a top_values entry for label|prop (an indexed property)
  bool has_top_values(const std::string& label, const std::string& prop) const;
  // the edges a hop on (etype, dir) reaches from the vertices of `label` whose indexed `prop` equals `value` (top_degrees,
  // key label|etype|dir|prop): a listed value's edges; else the rest spread over the other owners' values,
  // (edges - the listed) / (owner_values - listed), 0 when every value is listed. Refused by name without the entry
  double entry_degree(const std::string& label, const std::string& etype, const std::string& dir,
                      const std::string& prop, const json& value) const;
  // the declared date axis (date_axis: labels and a date property), when the file has one
  bool has_date_axis() const;
  std::vector<std::string> date_axis_labels() const;
  std::string date_axis_prop() const;
  // over the per-day counts of `labels`' date `prop` (date_days, refused by name without them): the days from the
  // first to the last populated day, the populated days, and the vertices within `days` of the last day
  double date_span(const std::vector<std::string>& labels, const std::string& prop, const std::string& rule) const;
  double populated_days(const std::vector<std::string>& labels, const std::string& prop, const std::string& rule) const;
  double within_days(const std::vector<std::string>& labels, const std::string& prop, double days,
                     const std::string& rule) const;
  // the recency key "<L>|etype|dir|prop" of a hop's far end ("" when the file has none, or more than one label has it)
  std::string recency_key(const std::string& etype, const std::string& dir, const std::string& prop) const;
  // the anchor's own days back to its k-th newest neighbour (k = 1, 10, 20; the key's span for a known vertex with no
  // neighbour there); nullopt without the anchor tier
  std::optional<double> anchor_recency(const std::string& key, const std::string& vid, int k) const;
  std::optional<double> recency_mean(const std::string& key, int k) const;
  std::vector<double> recency_deciles(const std::string& key, int k) const;
  std::vector<std::string> content_keys() const;   // the neutral size families (sizes.content's keys)
  // the mean `target` degree of n vertices of a set reached through an edge of kind `via` ("knows|both"): each vertex is
  // in the set with probability 1 - (1 - c)^(its via degree), c chosen so the expected size is n (degree_given_via, all
  // labels holding both degrees). nullopt without the histogram
  std::optional<double> mean_given_via(const std::string& via, const std::string& target, double n) const;
  // the distinct vertices among n rows reached through edges of kind `via`: each row a draw, a vertex drawn in
  // proportion to its via degree d, so E[distinct] = sum over vertices of 1 - (1 - d / E)^n (E = the via edges).
  // nullopt without the histogram
  std::optional<double> distinct_given_via(const std::string& via, double n) const;
  // the vertices of `label` whose date `prop` falls in [lo, hi] (ms), from the per-day histogram (date_days; the end
  // days pro rata). nullopt without the histogram
  std::optional<double> in_window(const std::string& label, const std::string& prop, double lo, double hi) const;
  // the share of `label` among the vertices a hop over `etype` in direction `dir` ("out" / "in") reaches: their edges
  // by the far end's label (the graph statistics' owners), else the type counts of the labels at that end (esrc /
  // edst). nullopt for "both", or when neither says which labels sit there
  std::optional<double> far_label_share(const std::string& etype, const std::string& dir, const std::string& label) const;

  // the estimate card's reads (formats/meta/predict.cc), each refused by name when the file lacks it: an edge type's
  // edges; the owners rows (per edge type, direction and owner label: owners and edges); a declared property's
  // vertices holding it and their bytes (sizes.content prop|L|p: graph-stats writes one per declared property, 0 when
  // no vertex holds it); a (label, etype, dir) degree histogram, nullptr when the label holds no such edge
  // (graph-stats writes a row only where one exists; load refuses a file whose rows do not sum to the edge totals;
  // refused without degree_hist, the message giving the flat rows); whether the anchor tier holds any vertex (level predicted: no)
  double edge_total(const std::string& etype, const std::string& rule) const;
  const json& owners(const std::string& rule) const;
  std::pair<double, double> prop_content(const std::string& label, const std::string& prop, const std::string& rule) const;
  const json* degree_rows(const std::string& label, const std::string& etype, const std::string& dir,
                          const std::string& rule) const;
  bool has_anchor_tier() const;
  // the (label, property) pairs the dataset schema declares as dates, "L|p" (predict passes them from --schema or the
  // store's schema): a date filter on one with no date_days entry refuses; on any other field it keeps the filter rule
  void declare_date_props(std::set<std::string> props) { date_props_ = std::move(props); }
  bool is_date_prop(const std::string& label, const std::string& prop) const {
    return date_props_.count(label + "|" + prop) > 0;
  }
  // the properties the dataset schema declares and their types ("str", "int", "date", "bytes"), keyed "prop|L|p" (a
  // vertex label's) and "eprop|E|p" (an edge type's): what a range filter's field is
  void declare_prop_types(std::map<std::string, std::string> types) { prop_types_ = std::move(types); }
  std::string prop_type(const std::string& key) const {   // "" when the schema does not declare it
    auto it = prop_types_.find(key);
    return it == prop_types_.end() ? "" : it->second;
  }
  bool is_int_prop(const std::string& key) const { return prop_type(key) == "int"; }
  bool declares(const std::string& label, const std::string& prop) const { return !prop_type("prop|" + label + "|" + prop).empty(); }
  // an int property's least and greatest value (value_range, key prop|L|p or eprop|E|p); nullopt when no element holds
  // it. Refused by name without the entry
  std::optional<std::pair<double, double>> value_range(const std::string& key, const std::string& rule) const;
  // the elements whose int property `key` is in [lo, hi) (integers; open sides at -inf / +inf), from its per-value
  // counts (int_values, the same form as date_days); 0 when value_range is null (no element holds it). Refused by name
  // without the entry, the message giving the flat form (an even spread over value_range)
  double int_in_window(const std::string& key, double lo, double hi, const std::string& rule) const;

 private:
  std::set<std::string> date_props_;
  std::map<std::string, std::string> prop_types_;
  // one entry of a per-key field (edge_totals, distinct_src, ndv, ...), refused by name when the file lacks it
  double entry(const std::string& field, const std::string& key, const std::string& rule) const;
  double scalar(const std::string& field, const std::string& rule) const;   // a number field, refused by name
  double pop(const std::string& side, const std::string& etype) const;
};

// Python's round(): halves to even (the estimator's integer rounding; std::round would round them up)
double py_round(double x);

}  // namespace evalcpp::predict
