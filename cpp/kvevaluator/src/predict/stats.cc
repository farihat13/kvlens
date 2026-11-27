// The statistics file's accessors (stats.h).
#include "predict/stats.h"
#include "util/record_stream.h"

#include <algorithm>
#include <cfenv>
#include <cmath>
#include <array>
#include <fstream>
#include <map>
#include <set>
#include <stdexcept>

namespace evalcpp::predict {

double py_round(double x) {
  // nearbyint rounds in the current mode; the default (to nearest) breaks ties to even, as Python's round() does
  return std::nearbyint(x);
}

namespace {
// The keys the readers below look up, one function each, so a refusal's example names the key the reader reads.
std::string degree_key(const std::string& label, const std::string& etype, const std::string& dir) {
  return label + "|" + etype + "|" + dir;   // degree_hist
}
std::string cond_key(const std::string& via, const std::string& target) { return via + ">" + target; }   // cond_degree
std::string ratio_key(const std::string& etype, const std::string& dir) { return etype + "|" + dir; }   // neighbor_degree_ratio
std::string date_key(const std::string& label, const std::string& prop) { return label + "|" + prop; }   // date_days
std::string burst_key(const std::string& etype, const std::string& dir, const std::string& prop) {
  return etype + "|" + dir + "|" + prop;   // burstiness
}
std::string prop_key(const std::string& label, const std::string& prop) { return "prop|" + label + "|" + prop; }
const char* const kFirstDay = "first_day";   // a date_days entry's members
const char* const kFirst = "first";          // an int_values entry's: the same form, per integer value
const char* const kCounts = "counts";

// Tier 1 has no fallback for a tier-2 field: a missing one refuses, and the refusal says how to
// write its flat form, the one a file with tier 1 alone implies. The flat neighbour ratio: every owner of a list at
// the mean degree (edges / owners), so a vertex reached through an edge has the mean degree too (no friendship
// paradox). For an edge type with one owner label it equals the flat degree_hist rows; with several owner labels the
// rows keep each label's own mean, so a neighbour's degree from them is higher
constexpr double kFlatNeighbourRatio = 1.0;
// The flat burstiness: every owner's neighbours spread evenly over the date index, so a ranked walk crosses k / n of it
constexpr double kFlatBurstiness = 1.0;

std::string num(double x) {
  char b[32];
  std::snprintf(b, sizeof b, "%.10g", x);
  return b;
}

// degree_hist's log2 bucket of a degree: 0 for no edge, else floor(log2 ceil(d)) + 1 (graph-stats' floor(log2 d) + 1 on
// an integer degree; a mean above 1 never lands in bucket 1, which meta reads as "exactly one entry")
int degree_bucket(double d) {
  if (d <= 0) return 0;
  int b = 1;
  for (uint64_t x = uint64_t(std::ceil(d)); x >>= 1;) b++;
  return b;
}

bool same(double a, double b) { return std::fabs(a - b) <= 1e-9 * std::max(1.0, std::max(std::fabs(a), std::fabs(b))); }

// the flat degree_hist rows of (label, etype, dir) from tier 1's owners row: the label's vertices with no list in
// bucket 0, every owner at the mean degree
json flat_degree_rows(double vertices, double owners, double edges) {
  double m = owners > 0 ? edges / owners : 0.0;
  json rows = json::array({json::array({0, vertices - owners, 0, 0})});
  if (owners > 0) rows.push_back(json::array({degree_bucket(m), owners, edges, edges * m}));
  return rows;
}

const json* owners_row(const json& d, const std::string& etype, const std::string& dir, const std::string& label) {
  if (!d.contains("owners") || !d["owners"].is_array()) return nullptr;
  for (auto& o : d["owners"])
    if (o.value("etype", "") == etype && o.value("direction", "") == dir && o.value("owner_label", "") == label)
      return &o;
  return nullptr;
}

// the tier-2 fields a file holds must agree with its tier-1 counts (a measured histogram does, by construction; a
// flat row written by hand might not): refused here, by name, before any estimate reads it
void check_against_tier1(const json& d, const std::string& files) {
  auto refuse = [&](const std::string& field, const std::string& key, const std::string& what) {
    throw std::runtime_error("predict: the statistics (" + files + ") " + field + " entry '" + key + "' disagrees with "
                             "tier 1: " + what + " (inputs/contract/stats.schema.json): refused");
  };
  const json& tc = d["type_counts"];
  if (d.contains("degree_hist") && d["degree_hist"].is_object()) {
    std::map<std::string, double> per_edge;   // "etype|dir" -> summed degree over the labels (out and in)
    std::map<std::string, double> per_label;  // "label|etype|dir" -> summed degree, to check a 'both' row
    for (auto& [k, rows] : d["degree_hist"].items()) {
      size_t a = k.find('|'), b = k.rfind('|');
      if (a == std::string::npos || a == b) refuse("degree_hist", k, "the key is not label|edge type|direction");
      std::string label = k.substr(0, a), etype = k.substr(a + 1, b - a - 1), dir = k.substr(b + 1);
      if (dir != "out" && dir != "in" && dir != "both") refuse("degree_hist", k, "the direction is not out, in or both");
      if (!tc.contains(label)) refuse("degree_hist", k, "type_counts has no label '" + label + "'");
      if (!d["edge_totals"].contains(etype)) refuse("degree_hist", k, "edge_totals has no edge type '" + etype + "'");
      double n = 0, owners = 0, s1 = 0, s2 = 0;
      for (auto& r : rows) {
        if (!r.is_array() || r.size() != 4) refuse("degree_hist", k, "a row is not [bucket, vertices, sum d, sum d^2]");
        for (auto& x : r)
          if (!x.is_number() || x.get<double>() < 0) refuse("degree_hist", k, "a row holds a negative or non-number value");
        if (r[0].get<double>() != std::floor(r[0].get<double>())) refuse("degree_hist", k, "a bucket is not a whole number");
        n += r[1].get<double>();
        if (r[0].get<int>() > 0) { owners += r[1].get<double>(); s1 += r[2].get<double>(); s2 += r[3].get<double>(); }
      }
      if (!same(n, tc[label].get<double>()))
        refuse("degree_hist", k, "its vertices sum to " + num(n) + ", type_counts['" + label + "'] is " +
                                 num(tc[label].get<double>()));
      if (owners > 0 && s2 < s1 * s1 / owners * (1 - 1e-9))
        refuse("degree_hist", k, "sum d^2 " + num(s2) + " is below (sum d)^2 / owners " + num(s1 * s1 / owners) +
                                 ", which no set of degrees has");
      per_label[k] = s1;
      if (dir == "both") continue;
      per_edge[etype + "|" + dir] += s1;
      if (const json* o = owners_row(d, etype, dir, label)) {
        if (!same(owners, o->value("owners", 0.0)) || !same(s1, o->value("edges", 0.0)))
          refuse("degree_hist", k, "its owners and summed degree are " + num(owners) + " and " + num(s1) +
                                   ", the owners row says " + num(o->value("owners", 0.0)) + " and " +
                                   num(o->value("edges", 0.0)));
      }
    }
    for (auto& [ed, s1] : per_edge) {
      std::string etype = ed.substr(0, ed.rfind('|'));
      if (!same(s1, d["edge_totals"][etype].get<double>()))
        refuse("degree_hist", ed, "the labels' summed degrees are " + num(s1) + ", edge_totals['" + etype + "'] is " +
                                  num(d["edge_totals"][etype].get<double>()) + " (every label holding the edges needs "
                                  "its row)");
    }
    // a 'both' row (a label at both ends) counts each edge at both of its ends: out's and in's summed degrees
    for (auto& [k, s1] : per_label) {
      if (k.size() < 5 || k.compare(k.size() - 5, 5, "|both") != 0) continue;
      std::string base = k.substr(0, k.size() - 5);
      if (!per_label.count(base + "|out") || !per_label.count(base + "|in"))
        refuse("degree_hist", k, "a 'both' row needs the label's out and in rows to check it against");
      double want = per_label[base + "|out"] + per_label[base + "|in"];
      if (!same(s1, want))
        refuse("degree_hist", k, "its summed degree is " + num(s1) + ", the label's out and in rows sum to " + num(want));
    }
  }
  if (d.contains("date_days") && d["date_days"].is_object()) {
    const json& content = d["sizes"]["content"];
    for (auto& [k, h] : d["date_days"].items()) {
      size_t a = k.find('|');
      if (a == std::string::npos) refuse("date_days", k, "the key is not label|property");
      if (!h.is_object() || !h.contains(kFirstDay) || !h[kFirstDay].is_number() || !h.contains(kCounts) ||
          !h[kCounts].is_array())
        refuse("date_days", k, std::string("it is not {") + kFirstDay + ", " + kCounts + "}");
      double n = 0;
      for (auto& c : h[kCounts]) {
        if (!c.is_number() || c.get<double>() < 0) refuse("date_days", k, "a count is negative or not a number");
        n += c.get<double>();
      }
      std::string pk = prop_key(k.substr(0, a), k.substr(a + 1));
      if (!content.contains(pk)) refuse("date_days", k, "sizes.content has no " + pk + " to check its total against");
      double u = content[pk].value("u", 0.0);
      if (!same(n, u))
        refuse("date_days", k, "its counts sum to " + num(n) + ", sizes.content " + pk + " has " + num(u) +
                               " vertices with the property");
    }
  }
  if (d.contains("neighbor_degree_ratio") && d["neighbor_degree_ratio"].is_object())
    for (auto& [k, v] : d["neighbor_degree_ratio"].items()) {
      size_t b = k.rfind('|');
      std::string etype = b == std::string::npos ? k : k.substr(0, b), dir = b == std::string::npos ? "" : k.substr(b + 1);
      if (dir != "out" && dir != "in" && dir != "both") refuse("neighbor_degree_ratio", k, "the key is not edge type|direction");
      if (!d["edge_totals"].contains(etype)) refuse("neighbor_degree_ratio", k, "edge_totals has no edge type '" + etype + "'");
      if (!v.is_number()) refuse("neighbor_degree_ratio", k, "it is not a number");
      // sum d^2 / sum d is at least sum d / owners, the mean degree: a ratio below 1 has no set of degrees
      if (v.get<double>() < kFlatNeighbourRatio * (1 - 1e-9))
        refuse("neighbor_degree_ratio", k, "a neighbour's degree over the mean degree is " + num(v.get<double>()) +
                                           ", below " + num(kFlatNeighbourRatio) + ", which no set of degrees has");
    }
  if (d.contains("value_range") && d["value_range"].is_object())
    for (auto& [k, v] : d["value_range"].items()) {
      bool vertex = k.rfind("prop|", 0) == 0, edge = k.rfind("eprop|", 0) == 0;
      size_t a = k.find('|'), b = k.rfind('|');
      if ((!vertex && !edge) || a == b) refuse("value_range", k, "the key is not prop|label|property or eprop|edge type|property");
      std::string owner = k.substr(a + 1, b - a - 1);
      if (vertex && !d["type_counts"].contains(owner)) refuse("value_range", k, "type_counts has no label '" + owner + "'");
      if (edge && !d["edge_totals"].contains(owner)) refuse("value_range", k, "edge_totals has no edge type '" + owner + "'");
      if (!v.is_null() && !(v.is_array() && v.size() == 2 && v[0].is_number() && v[1].is_number() &&
                            v[0].get<double>() <= v[1].get<double>()))
        refuse("value_range", k, "it is not [least, greatest] or null");
    }
  if (d.contains("int_values") && d["int_values"].is_object())
    for (auto& [k, h] : d["int_values"].items()) {
      // the same form as date_days, per integer value: it must span value_range exactly and count every holder
      const json* r = d.contains("value_range") && d["value_range"].contains(k) ? &d["value_range"][k] : nullptr;
      if (r == nullptr || r->is_null()) refuse("int_values", k, "value_range has no [least, greatest] for it");
      if (!h.is_object() || !h.contains(kFirst) || !h[kFirst].is_number() || !h.contains(kCounts) || !h[kCounts].is_array())
        refuse("int_values", k, std::string("it is not {") + kFirst + ", " + kCounts + "}");
      double lo = (*r)[0].get<double>(), hi = (*r)[1].get<double>(), n = 0;
      if (h[kFirst].get<double>() != lo || double(h[kCounts].size()) != hi - lo + 1)
        refuse("int_values", k, "its values do not run from value_range's least " + num(lo) + " to its greatest " + num(hi));
      for (auto& c : h[kCounts]) {
        if (!c.is_number() || c.get<double>() < 0) refuse("int_values", k, "a count is negative or not a number");
        n += c.get<double>();
      }
      size_t a = k.find('|'), b = k.rfind('|');
      std::string owner = k.substr(a + 1, b - a - 1);
      if (k.rfind("prop|", 0) == 0) {
        double u = d.contains("sizes") && d["sizes"].contains("content") && d["sizes"]["content"].contains(k)
                       ? d["sizes"]["content"][k].value("u", -1.0) : -1.0;
        if (u >= 0 && std::abs(n - u) > 1e-6 * std::max(1.0, u))
          refuse("int_values", k, "its counts sum to " + num(n) + ", sizes.content " + k + " has " + num(u) +
                                  " vertices with the property");
      } else if (n > d["edge_totals"].value(owner, 0.0) * (1 + 1e-9)) {
        refuse("int_values", k, "its counts sum to " + num(n) + ", above edge_totals['" + owner + "']");
      }
    }
  if (d.contains("top_values") && d["top_values"].is_object())
    for (auto& [k, t] : d["top_values"].items()) {
      // per indexed property: its most common values, each counted, within its holders and its distinct values
      size_t bar = k.find('|');
      if (bar == std::string::npos || !d["type_counts"].contains(k.substr(0, bar)))
        refuse("top_values", k, "the key is not label|property of a label in type_counts");
      if (!t.is_object() || !t.contains("holders") || !t["holders"].is_number() || !t.contains("values") ||
          !t["values"].is_array())
        refuse("top_values", k, "it is not {k, holders, values}");
      double holders = t["holders"].get<double>(), n = 0;
      if (holders > d["type_counts"][k.substr(0, bar)].get<double>() * (1 + 1e-9))
        refuse("top_values", k, "its holders " + num(holders) + " are above the label's type_counts");
      for (auto& v : t["values"]) {
        if (!v.is_array() || v.size() != 2 || !v[1].is_number() || v[1].get<double>() <= 0)
          refuse("top_values", k, "a value is not [value, count > 0]");
        n += v[1].get<double>();
      }
      if (n > holders * (1 + 1e-9)) refuse("top_values", k, "its counts sum to " + num(n) + ", above its holders " + num(holders));
      if (d.contains("ndv") && d["ndv"].contains(k) && double(t["values"].size()) > d["ndv"][k].get<double>())
        refuse("top_values", k, "it lists more values than ndv has");
    }
  if (d.contains("top_degrees") && d["top_degrees"].is_object())
    for (auto& [k, t] : d["top_degrees"].items()) {
      // per (owner label, etype, dir, indexed property): the values owning the most edges, within the owners' edges
      size_t bar = k.find('|');
      if (bar == std::string::npos || !d["type_counts"].contains(k.substr(0, bar)) ||
          std::count(k.begin(), k.end(), '|') != 3)
        refuse("top_degrees", k, "the key is not label|edge type|direction|property of a label in type_counts");
      if (!t.is_object() || !t.contains("owners") || !t.contains("owner_values") || !t.contains("edges") ||
          !t.contains("values") || !t["values"].is_array() || !t["owners"].is_number() ||
          !t["owner_values"].is_number() || !t["edges"].is_number())
        refuse("top_degrees", k, "it is not {k, owners, owner_values, edges, values}");
      double owners = t["owners"].get<double>(), edges = t["edges"].get<double>(), n = 0;
      if (owners > d["type_counts"][k.substr(0, bar)].get<double>() * (1 + 1e-9))
        refuse("top_degrees", k, "its owners " + num(owners) + " are above the label's type_counts");
      if (t["owner_values"].get<double>() > owners * (1 + 1e-9))
        refuse("top_degrees", k, "its owner_values are above its owners");
      for (auto& v : t["values"]) {
        if (!v.is_array() || v.size() != 2 || !v[1].is_number() || v[1].get<double>() <= 0)
          refuse("top_degrees", k, "a value is not [value, edges > 0]");
        n += v[1].get<double>();
      }
      if (n > edges * (1 + 1e-9)) refuse("top_degrees", k, "its edges sum to " + num(n) + ", above its edges " + num(edges));
      if (double(t["values"].size()) > t["owner_values"].get<double>())
        refuse("top_degrees", k, "it lists more values than owner_values has");
    }
  if (d.contains("burstiness") && d["burstiness"].is_object())
    for (auto& [k, v] : d["burstiness"].items()) {
      size_t b2 = k.rfind('|'), b1 = b2 == std::string::npos || b2 == 0 ? std::string::npos : k.rfind('|', b2 - 1);
      if (b1 == std::string::npos) refuse("burstiness", k, "the key is not edge type|direction|property");
      std::string etype = k.substr(0, b1), dir = k.substr(b1 + 1, b2 - b1 - 1);
      if (dir != "out" && dir != "in") refuse("burstiness", k, "the direction is not out or in");
      if (!d["edge_totals"].contains(etype)) refuse("burstiness", k, "edge_totals has no edge type '" + etype + "'");
      if (!v.is_number() || v.get<double>() < 0) refuse("burstiness", k, "it is not a number >= 0");
    }
  if (d.contains("degree_by_far_label") && d["degree_by_far_label"].is_object()) {
    std::map<std::string, double> per;   // etype|dir -> the edges summed over owner and far labels
    for (auto& [k, v] : d["degree_by_far_label"].items()) {
      size_t a = k.find('|'), b = a == std::string::npos ? a : k.find('|', a + 1),
             c = b == std::string::npos ? b : k.find('|', b + 1);
      if (c == std::string::npos) refuse("degree_by_far_label", k, "the key is not owner label|edge type|direction|far label");
      std::string etype = k.substr(a + 1, b - a - 1), dir = k.substr(b + 1, c - b - 1);
      if (dir != "out" && dir != "in") refuse("degree_by_far_label", k, "the direction is not out or in");
      if (!d["edge_totals"].contains(etype)) refuse("degree_by_far_label", k, "edge_totals has no edge type '" + etype + "'");
      if (!v.is_array() || v.size() != 2 || !v[0].is_number() || !v[1].is_number() || v[0].get<double>() < 0 ||
          v[1].get<double>() < v[0].get<double>())
        refuse("degree_by_far_label", k, "it is not [edges, summed owner degree] with the sum >= the edges");
      per[etype + "|" + dir] += v[0].get<double>();
    }
    for (auto& [ed, n] : per) {
      std::string etype = ed.substr(0, ed.rfind('|'));
      if (n > d["edge_totals"][etype].get<double>() * (1 + 1e-9))
        refuse("degree_by_far_label", ed, "its edges sum to " + num(n) + ", above edge_totals['" + etype + "']");
    }
  }
}
}  // namespace

DatasetStats DatasetStats::load(const std::string& path, const std::string& anchor) {
  std::ifstream f(path);
  if (!f) throw std::runtime_error("predict: cannot read the statistics file " + path);
  json d = json::parse(f);
  std::string stats_hash, anchor_hash, stats_scale;
  stats_hash = d.value("schema_hash", "");
  if (d.contains("provenance") && d["provenance"].is_object()) {
    if (stats_hash.empty()) stats_hash = d["provenance"].value("schema_hash", "");
    const json& sf = d["provenance"].contains("sf") ? d["provenance"]["sf"] : json();
    stats_scale = sf.is_string() ? sf.get<std::string>() : sf.is_null() ? "" : sf.dump();
  }
  if (!anchor.empty()) {
    std::ifstream g(anchor);
    if (!g) throw std::runtime_error("predict: cannot read the anchor statistics " + anchor);
    json gd = json::parse(g);
    anchor_hash = gd.value("schema_hash", "");
    // the same record stream (util/record_stream.h: its line count, and its freshness key when both name one)
    std::string why = record_stream_mismatch(d.value("records", json()), gd.value("records", json()));
    if (!why.empty())
      throw std::runtime_error("predict: " + path + " and " + anchor + " were built from different graphs (" + why +
                               "): refused");
    for (auto& k : anchor_stat_keys())
      if (gd.contains(k)) d[k] = gd[k];
  }
  std::string graph = anchor;   // the files named in a refusal
  // the contract (inputs/contract/stats.schema.json, "required"): a field every estimate reads is refused here by name,
  // never replaced by a default (python -m kvevaluator.predict.check_stats checks the rest before predict)
  std::string files = path + (graph.empty() ? "" : " + " + graph);
  for (auto& f : required_fields())
    if (!d.contains(f) || !d[f].is_object())
      throw std::runtime_error("predict: the statistics (" + files + ") have no '" + f + "' (" + field_meaning(f) +
                               "; inputs/contract/stats.schema.json): refused");
  if (!d["sizes"].contains("content") || !d["sizes"]["content"].is_object())
    throw std::runtime_error("predict: the statistics (" + files + ") have no 'sizes.content' (" +
                             field_meaning("sizes") + "; inputs/contract/stats.schema.json): refused");
  if (d["sizes"].contains("sample")) {   // the share of elements a sampled b was measured on
    const json& s = d["sizes"]["sample"];
    if (!s.is_number() || s.get<double>() <= 0 || s.get<double>() > 1)
      throw std::runtime_error("predict: the statistics (" + files + ") have sizes.sample " + s.dump() +
                               ", not a share in (0, 1] (inputs/contract/stats.schema.json): refused");
  }
  // every label and edge type the file itself names (the owners rows, the end-label and edge-property keys) has its
  // counts: a file that knows a label or an edge type but lacks its count is refused here. A label or edge type the
  // file never names cannot be seen without the schema: check_stats --schema refuses it before predict
  {
    std::set<std::string> labels, etypes;
    if (d.contains("owners") && d["owners"].is_array())
      for (auto& o : d["owners"]) {
        labels.insert(o.value("owner_label", ""));
        etypes.insert(o.value("etype", ""));
      }
    for (auto& [k, _] : d["sizes"]["content"].items()) {
      size_t a = k.find('|');
      std::string kind = k.substr(0, a);
      if (kind != "esrc" && kind != "edst" && kind != "eprop") continue;
      size_t b = k.find('|', a + 1);
      etypes.insert(k.substr(a + 1, b == std::string::npos ? std::string::npos : b - a - 1));
      if (b != std::string::npos) labels.insert(k.substr(b + 1));
    }
    for (auto& l : labels)
      if (!l.empty() && !d["type_counts"].contains(l))
        throw std::runtime_error("predict: the statistics (" + files + ") name the label '" + l + "' but type_counts has no "
                                 "entry for it (inputs/contract/stats.schema.json): refused");
    for (auto& e : etypes)
      for (const char* f : {"edge_totals", "distinct_src", "distinct_dst"})
        if (!e.empty() && !d[f].contains(e))
          throw std::runtime_error("predict: the statistics (" + files + ") name the edge type '" + e + "' but " + f +
                                   " has no entry for it (inputs/contract/stats.schema.json): refused");
    for (auto& [e, n] : d["edge_totals"].items())
      for (const char* side : {"esrc", "edst"}) {
        bool any = false;
        for (auto& [k, _] : d["sizes"]["content"].items()) any = any || k.rfind(std::string(side) + "|" + e + "|", 0) == 0;
        if (n.get<double>() > 0 && !any)
          throw std::runtime_error("predict: the statistics (" + files + ") have edges of '" + e + "' but no sizes.content " +
                                   side + "|" + e + "|<label> key (the labels at that end; " + field_meaning("sizes") +
                                   "): refused");
      }
  }
  check_against_tier1(d, files);
  // the anchor statistics are optional (level predicted never reads them): absent is unknown, the same as empty
  for (auto& k : anchor_stat_keys())
    if (!d.contains(k) || d[k].is_null()) d[k] = json::object();
  DatasetStats out(std::move(d));
  out.stats_schema_hash = stats_hash;
  out.stats_scale = stats_scale;
  out.anchor_schema_hash = anchor_hash;
  out.stats_path = path;
  out.anchor_path = anchor;
  return out;
}

void DatasetStats::require_scale(const std::string& scale, const std::string& against) const {
  if (stats_scale != scale)
    throw std::runtime_error("predict: " + stats_path + " was built at scale " +
                             (stats_scale.empty() ? std::string("(none recorded)") : "'" + stats_scale + "'") +
                             ", not '" + scale + "' (" + against + "): refused; rebuild it "
                             "(python -m kvevaluator.predict.build_stats --sf " + scale + ")");
}

void DatasetStats::require_schema(const std::string& hash, const std::string& against) const {
  auto refuse = [&](const std::string& file, const std::string& had, const std::string& rebuild) {
    throw std::runtime_error("predict: " + file + " was built from schema " +
                             (had.empty() ? std::string("(none recorded)") : had.substr(0, 12)) + ", not " +
                             hash.substr(0, 12) + " (" + against + "): refused; " + rebuild);
  };
  if (stats_schema_hash != hash)
    refuse(stats_path, stats_schema_hash, "rebuild it (python -m kvevaluator.predict.build_stats --sf <SF>)");
  if (!anchor_path.empty() && anchor_schema_hash != hash)
    refuse(anchor_path, anchor_schema_hash, "rebuild it (python -m kvevaluator.predict.build_stats ... --anchor-out)");
}

DatasetStats::DatasetStats(json j) : d(std::move(j)) {
  if (std::fegetround() != FE_TONEAREST) throw std::runtime_error("predict: the rounding mode is not to-nearest");
  for (auto& [l, n] : d.at("type_counts").items()) total_vertices += n.get<double>();
}

const std::vector<std::string>& DatasetStats::required_fields() {
  static const std::vector<std::string> keys = {"type_counts", "edge_totals", "distinct_src", "distinct_dst", "ndv",
                                                "sizes"};
  return keys;
}

std::string DatasetStats::field_meaning(const std::string& field) {
  static const std::map<std::string, std::string> m = {
      {"type_counts", "vertices per label"},
      {"edge_totals", "edges per edge type"},
      {"distinct_src", "vertices with an out-edge, per edge type"},
      {"distinct_dst", "vertices with an in-edge, per edge type"},
      {"ndv", "distinct values per label and property"},
      {"sizes", "content sizes per label and property, and the end labels of each edge type"},
      {"date_days", "vertices per day of a date property"},
      {"degree_hist", "vertices by degree, per label, edge type and direction"},
      {"neighbor_degree_ratio", "a neighbour's mean degree over the mean degree, per edge type and direction"},
      {"recency", "days back to a vertex's k-th newest neighbour"},
      {"value_range", "the least and greatest value of each int property"},
      {"int_values", "elements per integer value of an int property"},
      {"top_values", "the most common values of an indexed property, each with its vertices"},
      {"top_degrees", "per owner label, edge type, direction and indexed property: the values whose vertices own the "
                      "most edges, each with its edges"},
      {"burstiness", "a ranked date-index walk's entries crossed per owner, over an even spread's, per edge type, "
                     "direction and date property"},
      {"owners", "vertices holding a list and its edges, per edge type, direction and owner label"},
      {"degree_by_far_label", "per owner label, edge type, direction and far label: the edges from that label and the "
                              "summed degree of the owner each lands on"}};
  auto it = m.find(field);
  return it == m.end() ? field : it->second;
}

double DatasetStats::entry(const std::string& field, const std::string& key, const std::string& rule) const {
  const json& m = d.at(field);   // load refused a file without the field
  auto it = m.find(key);
  if (it == m.end())
    throw std::runtime_error("predict: the statistics have no " + field + " entry for '" + key + "' (" +
                             field_meaning(field) + "), read by " + rule +
                             " (inputs/contract/stats.schema.json): refused, never a default");
  return it->get<double>();
}

double DatasetStats::scalar(const std::string& field, const std::string& rule) const {
  if (!d.contains(field) || !d[field].is_number())
    throw std::runtime_error("predict: the statistics have no '" + field + "' (" + field_meaning(field) + "), read by " +
                             rule + " (inputs/contract/stats.schema.json): refused");
  return d[field].get<double>();
}

double DatasetStats::edge_total(const std::string& etype, const std::string& rule) const {
  return entry("edge_totals", etype, rule);
}

const json& DatasetStats::owners(const std::string& rule) const {
  if (!d.contains("owners") || !d["owners"].is_array())
    throw std::runtime_error("predict: the statistics have no owners (" + field_meaning("owners") + "), read by " + rule +
                             " (inputs/contract/stats.schema.json): refused");
  return d["owners"];
}

std::pair<double, double> DatasetStats::prop_content(const std::string& label, const std::string& prop,
                                                     const std::string& rule) const {
  std::string k = prop_key(label, prop);
  const json& c = d.at("sizes").at("content");   // load refused a file without it
  auto it = c.find(k);
  if (it == c.end())
    throw std::runtime_error("predict: the statistics have no sizes.content entry for '" + k + "' (" +
                             field_meaning("sizes") + "), read by " + rule +
                             " (inputs/contract/stats.schema.json): refused, never a default");
  return {it->value("u", 0.0), it->value("b", 0.0)};
}

const json* DatasetStats::degree_rows(const std::string& label, const std::string& etype, const std::string& dir,
                                      const std::string& rule) const {
  // no row for any label of (etype, dir) while the edge type has edges: refused, never read as no list. Once one label
  // has rows, load has checked that the labels' rows hold every edge, so a label without a row owns no such list
  bool any = false;
  if (d.contains("degree_hist") && d["degree_hist"].is_object())
    for (auto& [k, _] : d["degree_hist"].items())
      if (k.size() > etype.size() + dir.size() + 2 && k.compare(k.size() - etype.size() - dir.size() - 2,
                                                                 std::string::npos, "|" + etype + "|" + dir) == 0)
        any = true;
  double edges = d["edge_totals"].contains(etype) ? d["edge_totals"][etype].get<double>() : 0.0;
  if (!any && edges > 0) {
    std::string flat;
    if (d.contains("owners") && d["owners"].is_array())
      for (auto& o : d["owners"])
        if (o.value("etype", "") == etype && o.value("direction", "") == dir) {
          std::string l = o.value("owner_label", "");
          flat += (flat.empty() ? "" : ", ") + std::string("\"") + degree_key(l, etype, dir) + "\": " +
                  flat_degree_rows(type_count(l), o.value("owners", 0.0), o.value("edges", 0.0)).dump();
        }
    if (flat.empty())
      flat = "\"" + degree_key(label, etype, dir) + "\": [[0, <vertices - owners>, 0, 0], [<bucket>, <owners>, <edges>, "
             "<edges x mean>]]";
    throw std::runtime_error("predict: the statistics have no degree_hist row for " + etype + "|" + dir + " (" +
                             field_meaning("degree_hist") + "), read by " + rule + ": refused. Its flat form, every "
                             "owner at the mean degree, from the owners rows: \"degree_hist\": {" + flat +
                             "} (inputs/contract/stats.schema.json)");
  }
  if (!any) return nullptr;   // an edge type with no edges: every list is empty
  auto it = d["degree_hist"].find(degree_key(label, etype, dir));
  return it == d["degree_hist"].end() ? nullptr : &*it;
}

bool DatasetStats::has_anchor_tier() const {
  return d.contains("anchor_degrees") && d["anchor_degrees"].is_object() && !d["anchor_degrees"].empty();
}

const std::vector<std::string>& DatasetStats::anchor_stat_keys() {
  static const std::vector<std::string> keys = {"anchor_degrees", "anchor_recency", "anchor_far_labels"};
  return keys;
}

DatasetStats DatasetStats::without_anchor_stats() const {
  DatasetStats out = *this;
  for (auto& k : anchor_stat_keys()) out.d[k] = json::object();   // empty = unknown: every accessor takes the population
  return out;
}

double DatasetStats::type_count(const std::string& label) const {
  return entry("type_counts", label, "a label's population");
}

double DatasetStats::type_count(const std::vector<std::string>& labels) const {
  double n = 0;
  for (auto& l : labels) n += type_count(l);
  return n;
}

double DatasetStats::edges(const std::string& etype) const { return entry("edge_totals", etype, "a hop's degree"); }

double DatasetStats::mean_degree(const std::string& etype, const std::string& dir) const {
  double e = edges(etype);
  // the owners are refused when missing (a missing entry taken as 1 would make the mean degree the
  // edge total); an edge type with no edges has 0 owners, and its mean degree stays 0 / 1
  double ds = std::max(entry("distinct_src", etype, "the mean degree"), 1.0);
  double dd = std::max(entry("distinct_dst", etype, "the mean degree"), 1.0);
  if (dir == "out") return e / ds;
  if (dir == "in") return e / dd;
  return e / ds + e / dd;
}

// the population a traversal starts from: the type counts of the labels seen on that side ("esrc|etype|label" content keys)
double DatasetStats::pop(const std::string& side, const std::string& etype) const {
  double n = 0;
  std::string pre = side + "|" + etype + "|";
  for (auto& k : content_keys())
    if (k.compare(0, pre.size(), pre) == 0) {
      size_t a = k.find('|'), b = k.find('|', a + 1);
      std::string label = k.substr(b + 1, k.find('|', b + 1) - b - 1);
      n += type_count(label);
    }
  return n;
}

double DatasetStats::population_degree(const std::string& etype, const std::string& dir) const {
  double e = edges(etype);
  // a population of 0 with edges: no end label known (load refuses that file) or labels with no vertices; refused,
  // never the participants' mean in its place. No edges: the old fallback, a degree of 0
  auto refuse = [&](const char* side) {
    throw std::runtime_error("predict: the statistics give the edges of '" + etype + "' no " + side +
                             " population (sizes.content " + side + "|" + etype + "|<label> and its type_counts), "
                             "read by a hop's population degree: refused");
  };
  if (dir == "both") {
    double po = pop("esrc", etype), pi = pop("edst", etype);
    if (e > 0 && po == 0) refuse("esrc");
    if (e > 0 && pi == 0) refuse("edst");
    if (po == 0 || pi == 0) return mean_degree(etype, "both");
    return e / po + e / pi;
  }
  const char* side = dir == "out" ? "esrc" : "edst";
  double p = pop(side, etype);
  if (e > 0 && p == 0) refuse(side);
  return p != 0 ? e / p : mean_degree(etype, dir);
}

double DatasetStats::population_count(const std::string& etype, const std::string& dir) const {
  return pop(dir == "out" ? "esrc" : "edst", etype);
}

std::vector<std::string> DatasetStats::side_labels(const std::string& etype, const std::string& dir) const {
  std::vector<std::string> out;
  std::string pre = std::string(dir == "out" ? "esrc|" : "edst|") + etype + "|";
  for (auto& k : content_keys())
    if (k.compare(0, pre.size(), pre) == 0) out.push_back(k.substr(pre.size(), k.find('|', pre.size()) - pre.size()));
  return out;
}

double DatasetStats::participant_count(const std::string& etype, const std::string& dir) const {
  return entry(dir == "out" ? "distinct_src" : "distinct_dst", etype, "the participants of a sorted read");
}

std::optional<double> DatasetStats::cond_ratio(const std::string& via, const std::string& etype,
                                               const std::string& dir) const {
  if (!d.contains("cond_degree") || d["cond_degree"].is_null()) return std::nullopt;
  std::string k = cond_key(via, etype + "|" + dir);
  if (!d["cond_degree"].contains(k)) return std::nullopt;
  return d["cond_degree"][k].get<double>();
}

double DatasetStats::neighbor_degree(const std::string& etype, const std::string& dir) const {
  // (sum d^2 / n) / (sum d / n) over the vertices with such an edge, every label holding it (in this order the
  // value equals, bit for bit, the knows_ndeg the retired LDBC builder wrote)
  double n = 0, s1 = 0, s2 = 0;
  bool found = false;
  std::string suffix = "|" + etype + "|" + dir;
  if (d.contains("degree_hist") && d["degree_hist"].is_object())
    for (auto& [k, rows] : d["degree_hist"].items()) {
      if (k.size() <= suffix.size() || k.compare(k.size() - suffix.size(), suffix.size(), suffix) != 0) continue;
      found = true;
      for (auto& r : rows)
        if (r[0].get<int>() > 0) { n += r[1].get<double>(); s1 += r[2].get<double>(); s2 += r[3].get<double>(); }
    }
  if (found) return n > 0 && s1 > 0 ? (s2 / n) / (s1 / n) : 0.0;
  // else one number: a neighbour's mean degree over the mean degree (edges / owners)
  std::string k = ratio_key(etype, dir);
  if (d.contains("neighbor_degree_ratio") && d["neighbor_degree_ratio"].contains(k))
    return d["neighbor_degree_ratio"][k].get<double>() * mean_degree(etype, dir);
  throw std::runtime_error("predict: the statistics have no degree_hist row for " + k + " (" +
                           field_meaning("degree_hist") + ") and no neighbor_degree_ratio entry '" + k + "' (" +
                           field_meaning("neighbor_degree_ratio") + "), read by a neighbour's degree: refused. Its flat "
                           "form, every owner at the mean degree: \"neighbor_degree_ratio\": {\"" + k + "\": " +
                           num(kFlatNeighbourRatio) + "} (a measured one is sum(d^2) / sum(d) over edges / owners; "
                           "inputs/contract/stats.schema.json)");
}

std::optional<std::pair<double, double>> DatasetStats::value_range(const std::string& key, const std::string& rule) const {
  if (!d.contains("value_range") || !d["value_range"].contains(key))
    throw std::runtime_error("predict: the statistics have no value_range entry '" + key + "' (" +
                             field_meaning("value_range") + "), read by " + rule + ": refused (a count: MATCH (n) "
                             "RETURN min(n.p), max(n.p); null when no element holds the property; "
                             "inputs/contract/stats.schema.json)");
  const json& r = d["value_range"][key];
  if (r.is_null()) return std::nullopt;
  return std::make_pair(r[0].get<double>(), r[1].get<double>());
}

double DatasetStats::burstiness(const std::string& etype, const std::string& dir, const std::string& prop,
                               const std::string& rule) const {
  std::string k = burst_key(etype, dir, prop);
  if (d.contains("burstiness") && d["burstiness"].contains(k)) return d["burstiness"][k].get<double>();
  throw std::runtime_error("predict: the statistics have no burstiness entry '" + k + "' (" + field_meaning("burstiness") +
                           "), read by " + rule + ": refused. Its flat form, every owner's neighbours spread evenly "
                           "over the date index: \"burstiness\": {\"" + k + "\": " + num(kFlatBurstiness) +
                           "} (inputs/contract/stats.schema.json)");
}

std::array<double, 2> DatasetStats::far_degree(const std::string& owner, const std::string& etype,
                                               const std::string& dir, const std::string& far,
                                               const std::string& rule) const {
  std::array<double, 2> t = {0, 0};
  bool found = false;
  if (d.contains("degree_by_far_label") && d["degree_by_far_label"].is_object())
    for (auto& [k, v] : d["degree_by_far_label"].items()) {
      // owner|etype|dir|far (labels and edge types hold no '|')
      size_t a = k.find('|'), b = k.find('|', a + 1), c = k.find('|', b + 1);
      if (c == std::string::npos) continue;
      if (k.substr(a + 1, b - a - 1) != etype || k.substr(b + 1, c - b - 1) != dir) continue;
      if (!owner.empty() && k.substr(0, a) != owner) continue;
      if (!far.empty() && k.substr(c + 1) != far) continue;
      t[0] += v[0].get<double>();
      t[1] += v[1].get<double>();
      found = true;
    }
  if (found) return t;
  std::string k = (owner.empty() ? std::string("<owner>") : owner) + "|" + etype + "|" + dir + "|" +
                  (far.empty() ? std::string("<far>") : far);
  throw std::runtime_error("predict: the statistics have no degree_by_far_label entry '" + k + "' (" +
                           field_meaning("degree_by_far_label") + "), read by " + rule + ": refused. Its flat form, "
                           "the owners row's edges split over the far labels the key declares, in proportion to each "
                           "one's type_counts, every owner at the mean degree m = edges / owners: [n_F, n_F x m] (n_F: "
                           "far label F's share of the edges) (inputs/contract/stats.schema.json)");
}

std::optional<double> DatasetStats::anchor_degree(const std::string& vid, const std::string& etype,
                                                  const std::string& dir) const {
  if (!d.contains("anchor_degrees") || !d["anchor_degrees"].is_object()) return std::nullopt;
  const json& ad = d["anchor_degrees"];
  if (!ad.contains(vid)) return std::nullopt;   // not an anchor: the population
  double o = ad[vid].value(etype + "|out", 0.0), i = ad[vid].value(etype + "|in", 0.0);
  return dir == "out" ? o : dir == "in" ? i : o + i;
}

std::optional<std::map<std::string, double>> DatasetStats::anchor_far_share(const std::string& vid,
                                                                           const std::string& etype,
                                                                           const std::string& dir) const {
  if (!d.contains("anchor_far_labels") || !d["anchor_far_labels"].is_object()) return std::nullopt;
  const json& af = d["anchor_far_labels"];
  if (!af.contains(vid)) return std::nullopt;
  std::map<std::string, double> share;
  double all = 0;
  for (const char* x : {"out", "in"}) {
    if (dir != x && dir != "both") continue;
    auto it = af[vid].find(etype + "|" + x);
    if (it == af[vid].end()) continue;
    for (auto l = it->begin(); l != it->end(); ++l) {
      share[l.key()] += l->get<double>();
      all += l->get<double>();
    }
  }
  if (all <= 0) return std::nullopt;
  for (auto& [l, n] : share) n /= all;
  return share;
}

std::optional<double> DatasetStats::forest_depth(const std::string& etype, bool all) const {
  if (!d.contains("forest") || !d["forest"].contains(etype)) return std::nullopt;
  const json& f = d["forest"][etype];
  double n = 0;
  for (auto& l : f.at(all ? "labels" : "source_labels")) n += type_count(l.get<std::string>());
  return f.at("depth_sum").get<double>() / std::max(n, 1.0);
}

bool DatasetStats::has_top_values(const std::string& label, const std::string& prop) const {
  return d.contains("top_values") && d["top_values"].contains(label + "|" + prop);
}

double DatasetStats::value_rows(const std::string& label, const std::string& prop, const json& value,
                                const std::string& rule) const {
  std::string key = label + "|" + prop;
  if (!has_top_values(label, prop))
    throw std::runtime_error("predict: the statistics have no top_values entry for '" + key + "' (" +
                             field_meaning("top_values") + "), read by " + rule +
                             " (inputs/contract/stats.schema.json): refused, never a default");
  const json& t = d["top_values"][key];
  std::string text = value.dump();
  double listed = 0;
  for (auto& v : t["values"]) {
    if (v[0].dump() == text) return v[1].get<double>();
    listed += v[1].get<double>();
  }
  double rest = ndv(label, prop) - double(t["values"].size());
  return rest > 0 ? std::max(0.0, t["holders"].get<double>() - listed) / rest : 0.0;
}

double DatasetStats::entry_degree(const std::string& label, const std::string& etype, const std::string& dir,
                                  const std::string& prop, const json& value) const {
  std::string key = label + "|" + etype + "|" + dir + "|" + prop;
  if (!d.contains("top_degrees") || !d["top_degrees"].contains(key))
    throw std::runtime_error("predict: the statistics have no top_degrees entry for '" + key + "' (" +
                             field_meaning("top_degrees") + "), read by a hop straight from an equality index read "
                             "(entry_value_degree; inputs/contract/stats.schema.json): refused, never a default");
  const json& t = d["top_degrees"][key];
  std::string text = value.dump();
  double listed = 0;
  for (auto& v : t["values"]) {
    if (v[0].dump() == text) return v[1].get<double>();
    listed += v[1].get<double>();
  }
  double rest = t["owner_values"].get<double>() - double(t["values"].size());
  return rest > 0 ? std::max(0.0, t["edges"].get<double>() - listed) / rest : 0.0;
}

double DatasetStats::ndv(const std::string& label, const std::string& prop) const {
  // refused when missing (taken as 1, an equality filter would keep every vertex)
  return entry("ndv", label + "|" + prop, "an equality index read (read_index_eq)");
}

bool DatasetStats::has_date_axis() const { return d.contains("date_axis") && d["date_axis"].is_object(); }
namespace {
void require_date_axis(bool has) {
  if (!has)
    throw std::runtime_error("predict: a date rule names no labels or prop and the statistics declare no date_axis "
                             "(graph-stats --date-axis; inputs/contract/stats.schema.json): refused");
}
}  // namespace
std::vector<std::string> DatasetStats::date_axis_labels() const {
  require_date_axis(has_date_axis());
  std::vector<std::string> out;
  for (auto& l : d["date_axis"].at("labels")) out.push_back(l.get<std::string>());
  return out;
}
std::string DatasetStats::date_axis_prop() const {
  require_date_axis(has_date_axis());
  return d["date_axis"].at("prop").get<std::string>();
}

namespace {
// day -> vertices over `labels`' date `prop` (date_days), refused by name when a label has no row
std::map<int64_t, double> day_counts(const json& d, const std::vector<std::string>& labels, const std::string& prop,
                                     const std::string& rule) {
  std::map<int64_t, double> out;
  for (auto& l : labels) {
    std::string k = date_key(l, prop);
    if (!d.contains("date_days") || !d["date_days"].contains(k)) {
      std::string pk = prop_key(l, prop), n = "<the vertices with " + prop + ">";
      if (d.contains("sizes") && d["sizes"]["content"].contains(pk)) n = num(d["sizes"]["content"][pk].value("u", 0.0));
      throw std::runtime_error("predict: the statistics have no date_days entry for '" + k + "' (vertices per day of a "
                               "date property), read by " + rule + ": refused. Its flat form, an even spread over the "
                               "property's dates: \"date_days\": {\"" + k + "\": {\"" + kFirstDay + "\": <the earliest " +
                               prop + ", in days since 1970-01-01>, \"" + kCounts + "\": [one count per day to the latest, each " +
                               n + " / the days]}} (the counts sum to sizes.content " + pk + "'s u exactly: unrounded, or the remainder on one day; "
                               "inputs/contract/stats.schema.json)");
    }
    const json& h = d["date_days"][k];
    int64_t first = h.at(kFirstDay).get<int64_t>();
    const json& c = h.at(kCounts);
    for (size_t i = 0; i < c.size(); i++) out[first + int64_t(i)] += c[i].get<double>();
  }
  return out;
}
}  // namespace

double DatasetStats::date_span(const std::vector<std::string>& labels, const std::string& prop,
                               const std::string& rule) const {
  int64_t lo = 0, hi = 0;
  bool any = false;
  for (auto& [day, n] : day_counts(d, labels, prop, rule))
    if (n > 0) { lo = any ? std::min(lo, day) : day; hi = any ? std::max(hi, day) : day; any = true; }
  return any ? double(hi - lo + 1) : 0.0;
}

double DatasetStats::populated_days(const std::vector<std::string>& labels, const std::string& prop,
                                    const std::string& rule) const {
  double n = 0;
  for (auto& [day, c] : day_counts(d, labels, prop, rule)) n += c > 0 ? 1 : 0;
  return n;
}

double DatasetStats::within_days(const std::vector<std::string>& labels, const std::string& prop, double days,
                                 const std::string& rule) const {
  // the cumulative count back from the last populated day, the days rounded and clamped to the span (as the retired
  // LDBC builder's msgs_within was indexed)
  auto counts = day_counts(d, labels, prop, rule);
  int64_t lo = 0, hi = 0;
  bool any = false;
  for (auto& [day, n] : counts)
    if (n > 0) { lo = any ? std::min(lo, day) : day; hi = any ? std::max(hi, day) : day; any = true; }
  if (!any) return 0.0;
  int64_t span = hi - lo + 1;
  int64_t at = std::min(int64_t(py_round(days)), span);
  double n = 0;
  for (auto& [day, c] : counts)
    if (std::min(span, std::max<int64_t>(0, hi - day)) <= at) n += c;
  return n;
}

std::string DatasetStats::recency_key(const std::string& etype, const std::string& dir, const std::string& prop) const {
  if (!d.contains("recency") || !d["recency"].is_object()) return "";
  std::string suffix = "|" + etype + "|" + dir + "|" + prop, found;
  for (auto& [k, _] : d["recency"].items())
    if (k.size() > suffix.size() && k.compare(k.size() - suffix.size(), suffix.size(), suffix) == 0) {
      if (!found.empty()) return "";
      found = k;
    }
  return found;
}

std::optional<double> DatasetStats::anchor_recency(const std::string& key, const std::string& vid, int k) const {
  int at = k == 1 ? 0 : k == 10 ? 1 : k == 20 ? 2 : -1;
  if (at < 0 || !d.contains("anchor_recency") || !d["anchor_recency"].contains(key) || d["anchor_recency"][key].empty())
    return std::nullopt;
  const json& m = d["anchor_recency"][key];
  if (!m.contains(vid)) return d.at("recency").at(key).at("span").get<double>();   // no neighbour there: the whole span
  return m[vid][at].get<double>();
}

std::optional<double> DatasetStats::recency_mean(const std::string& key, int k) const {
  std::string kk = std::to_string(k);
  if (!d.contains("recency") || !d["recency"].contains(key) || !d["recency"][key]["mean"].contains(kk)) return std::nullopt;
  return d["recency"][key]["mean"][kk].get<double>();
}

std::vector<double> DatasetStats::recency_deciles(const std::string& key, int k) const {
  std::string kk = std::to_string(k);
  std::vector<double> out;
  if (d.contains("recency") && d["recency"].contains(key) && d["recency"][key]["deciles"].contains(kk))
    for (auto& x : d["recency"][key]["deciles"][kk]) out.push_back(x.get<double>());
  return out;
}

std::vector<std::string> DatasetStats::content_keys() const {
  std::vector<std::string> out;
  if (d.contains("sizes") && d["sizes"].contains("content"))
    for (auto& [k, _] : d["sizes"]["content"].items()) out.push_back(k);
  return out;
}

namespace {
// the elements of a per-unit histogram ({<first>, counts}: date_days per day, int_values per integer value) in
// [lo, hi): entry i covers [(first + i) unit, (first + i + 1) unit), a partly covered one counts by its covered part
double window_count(const json& h, const char* first_member, double lo, double hi, double unit) {
  double first = h.at(first_member).get<double>();
  const json& counts = h.at(kCounts);
  double n = 0;
  for (size_t i = 0; i < counts.size(); i++) {
    double a = (first + double(i)) * unit, b = a + unit;
    double overlap = std::min(b, hi) - std::max(a, lo);
    if (overlap > 0) n += counts[i].get<double>() * overlap / unit;
  }
  return n;
}
}  // namespace

std::optional<double> DatasetStats::in_window(const std::string& label, const std::string& prop, double lo,
                                              double hi) const {
  std::string k = date_key(label, prop);
  if (!d.contains("date_days") || !d["date_days"].contains(k)) return std::nullopt;
  return window_count(d["date_days"][k], kFirstDay, lo, hi, 86400000.0);
}

double DatasetStats::int_in_window(const std::string& key, double lo, double hi, const std::string& rule) const {
  auto r = value_range(key, rule);
  if (!r) return 0.0;
  if (!d.contains("int_values") || !d["int_values"].contains(key)) {
    double width = r->second - r->first + 1;
    throw std::runtime_error("predict: the statistics have no int_values entry '" + key + "' (" +
                             field_meaning("int_values") + "), read by " + rule + ": refused. Its flat form, an even "
                             "spread over value_range: \"int_values\": {\"" + key + "\": {\"" + kFirst + "\": " +
                             num(r->first) + ", \"" + kCounts + "\": [" + num(width) + " counts, each <the elements "
                             "holding the property> / " + num(width) + "]}} (inputs/contract/stats.schema.json)");
  }
  return window_count(d["int_values"][key], kFirst, lo, hi, 1.0);
}

std::optional<double> DatasetStats::far_label_share(const std::string& etype, const std::string& dir,
                                                    const std::string& label) const {
  if (dir != "out" && dir != "in") return std::nullopt;
  std::string far = dir == "out" ? "in" : "out";   // an out hop reaches the owners of the edge type's in-lists
  double mine = 0, all = 0;
  if (d.contains("owners") && d["owners"].is_array())
    for (auto& o : d["owners"]) {
      if (o.value("etype", "") != etype || o.value("direction", "") != far) continue;
      double e = o.value("edges", 0.0);
      all += e;
      if (o.value("owner_label", "") == label) mine += e;
    }
  if (all > 0) return mine / all;
  // no owners rows: the labels at that end, by their type counts (edst: the far end of an out hop; esrc: of an in hop)
  std::string pre = std::string(dir == "out" ? "edst|" : "esrc|") + etype + "|";
  if (!d.contains("sizes") || !d["sizes"].contains("content")) return std::nullopt;
  for (auto it = d["sizes"]["content"].begin(); it != d["sizes"]["content"].end(); ++it)
    if (it.key().rfind(pre, 0) == 0) {
      std::string l = it.key().substr(pre.size());
      double n = type_count(l);
      all += n;
      if (l == label) mine += n;
    }
  if (all > 0) return mine / all;
  return std::nullopt;
}

std::optional<double> DatasetStats::distinct_given_via(const std::string& via, double n) const {
  if (!d.contains("degree_given_via")) return std::nullopt;
  // the via-degree buckets of each label holding `via`: identical under every target, so the first key per label
  std::map<std::string, const json*> per_label;
  std::string mid = "|" + via + ">";
  for (auto& [k, rows] : d["degree_given_via"].items()) {
    size_t at = k.find(mid);
    if (at == std::string::npos || at == 0 || k.find('|') != at) continue;   // the label is the first field
    per_label.emplace(k.substr(0, at), &rows);
  }
  if (per_label.empty()) return std::nullopt;
  double edges = 0;
  for (auto& [l, rows] : per_label)
    for (auto& r : *rows) edges += r[2].get<double>();
  if (edges <= 0) return std::nullopt;
  double distinct = 0;
  for (auto& [l, rows] : per_label)
    for (auto& r : *rows) {
      double cnt = r[1].get<double>(), vs = r[2].get<double>();
      if (cnt <= 0 || vs <= 0) continue;
      distinct += cnt * (1.0 - std::pow(1.0 - (vs / cnt) / edges, n));
    }
  return distinct;
}

std::optional<double> DatasetStats::mean_given_via(const std::string& via, const std::string& target, double n) const {
  if (!d.contains("degree_given_via")) return std::nullopt;
  std::string suffix = "|" + via + ">" + target;
  std::map<int, std::array<double, 3>> h;   // bucket -> vertices, via sum, target sum (over the labels holding both)
  bool found = false;
  for (auto& [k, rows] : d["degree_given_via"].items()) {
    if (k.size() <= suffix.size() || k.compare(k.size() - suffix.size(), suffix.size(), suffix) != 0) continue;
    found = true;
    for (auto& r : rows) {
      auto& b = h[r[0].get<int>()];
      for (int i = 0; i < 3; i++) b[i] += r[i + 1].get<double>();
    }
  }
  if (!found || n <= 0) return std::nullopt;
  double reach = 0, reach_t = 0;   // the vertices with a via edge, and their target degrees
  for (auto& [b, x] : h)
    if (b > 0) { reach += x[0]; reach_t += x[2]; }
  if (reach <= 0) return std::nullopt;
  if (n >= reach) {   // every reachable vertex is in, the rest come from the vertices with no via edge
    const auto& z = h[0];
    double rest = std::min(n - reach, z[0]);
    return (reach_t + (z[0] > 0 ? rest * z[2] / z[0] : 0.0)) / (reach + rest);
  }
  auto expected = [&](double c, double* tsum) {
    double s = 0, t = 0;
    for (auto& [b, x] : h) {
      if (b == 0 || x[0] <= 0) continue;
      double p = 1.0 - std::pow(1.0 - c, x[1] / x[0]);
      s += x[0] * p;
      t += x[2] * p;
    }
    if (tsum) *tsum = t;
    return s;
  };
  double lo = 0, hi = 1;
  for (int i = 0; i < 60; i++) {
    double c = (lo + hi) / 2;
    (expected(c, nullptr) < n ? lo : hi) = c;
  }
  double t = 0, s = expected((lo + hi) / 2, &t);
  if (s <= 0) return std::nullopt;
  return t / s;
}

}  // namespace evalcpp::predict
