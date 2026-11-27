// `graph-stats --schema <schema.json> --records <records.jsonl>`: predict mode's graph statistics, format-free, with no
// store. One pass over the record stream an
// ingest reads (python/kvevaluator/measure/records_dump.py dumps it):
//   type_counts       vertices per label
//   owners            per (edge type, direction, owner label): the owners with at least one edge, and the edges
//   degree_given_via  per label and per pair of its edge degrees (via, target): a histogram over the via degree, each
//                     bucket [via-degree bucket, vertices, summed via degree, summed target degree]. A set reached
//                     through an edge of the via kind holds a vertex about in proportion to its via degree; the
//                     cardinality layer's frontier rule (predict/cardinality.cc) reads the target degree of such a
//                     set from it. Bucket 0 holds the vertices with no via edge; bucket b > 0 holds via degrees in
//                     [2^(b-1), 2^b). Keys "<label>|<etype>|<dir>><etype>|<dir>", dir out, in, or both (out + in, on a
//                     label at both ends of the edge type)
//   degree_hist       per (label, edge type, direction): the label's vertices by their degree, rows [log2 bucket,
//                     vertices, summed degree, summed squared degree]; bucket 0 holds the vertices with no such edge.
//                     A write's list read is the endpoint's list: its mean (sum / vertices) or, for an endpoint picked
//                     through an existing edge, the size-biased mean (squared sum / sum); a neighbour's degree is
//                     the squared sum over the sum. Key "<label>|<etype>|<dir>", dir out, in, or both (a label at both
//                     ends)
//   date_days         per (label, date property): the vertices per day ("first_day" in days since the epoch, then one
//                     count per day); a date window's rows are the days it covers (the cardinality layer's date_histogram)
//   cond_degree       per pair of degrees (via, target) over the labels holding both: the mean target degree of a vertex
//                     reached through a via edge (weighted by its via degree) over the target's population mean, 4
//                     places; "chase-root|<etype>><etype>|<dir>": the same for the root a chase over a forest lands on
//   forest            per edge type whose source labels are all destination labels and whose every source has one such
//                     edge (replyOf, isSubclassOf): its sources, the summed depth (hops to the chain's root), the source
//                     labels and every label of the type
//   recency           per "<L>|<etype>|<dir>|<prop>" (L an anchor label, the far end's labels all holding the date prop):
//                     over the vertices of L with a neighbour there, the days back from the far end's last day to the
//                     k-th newest neighbour (k = 1, 10, 20; the far end's span when fewer): the means and the deciles
//   value_range       per declared int property, "prop|<L>|<p>" or "eprop|<etype>|<p>": [least, greatest] over the
//                     elements holding it (null when none does)
//   top_values        per indexed non-date property "<label>|<prop>": the kTopValues most common values (all of them when there
//                     are no more), [value, elements] by count then value, and "holders": the elements whose record
//                     carries the property (null included), the population ndv counts. An equality's rows on an
//                     indexed property (predict's DatasetStats::value_rows); tier 1 keeps the first 10
//   top_degrees       per "<owner label>|<etype>|<dir>|<prop>" (every declared end of an edge type, every indexed
//                     non-date property of that label): the kTopDegrees values of prop whose vertices own the most edges there,
//                     [value, edges] by edges then value (a value several vertices hold sums their edges: an index read
//                     returns them all), "owners" the vertices with an edge there, "owner_values" the distinct values
//                     of those holding the prop, "edges" the edges of those. A hop straight from an equality index read (predict's
//                     DatasetStats::entry_degree); both tiers keep the same list
//   int_values        per declared int property spanning at most 1,000,000 integers: the elements per value
//                     ("first", the least value, then one count per value to the greatest), date_days' form; a range
//                     filter's rows are the values it covers
//   burstiness        per "<etype>|<dir>|<prop>" (prop a date every far label of that end has a sorted index on: the
//                     ranked date-index walk's candidates are a hop's far ends): per owner o with n_o >= 80
//                     (4 k) neighbours, the index rank (newest first) of o's 20th newest neighbour over the even-spread
//                     value min(N, 20 N / n_o); the mean over those owners weighted by n_o (1 when there is none).
//                     1 = an even spread
//   degree_by_far_label per "<owner label>|<etype>|<dir>|<far label>" (every declared pair of ends): [the edges whose
//                     far end has that label, the summed degree of the owner each lands on]. Their ratio is the list a
//                     new edge from that label grows (an owner drawn by its edges from that label); with one far label
//                     it is degree_hist's sum d^2 / sum d
//   date_axis         (--date-axis L,...:prop) the labels and date property a date rule reads when its step names
//                     none, declared by the dataset's build (python/kvevaluator/datasets/<name>/statistics/build.json)
//   anchor_degrees    (--anchor-labels L,...: the labels a plan's bound vertex has) every vertex of those labels, its
//                     degree per edge type and direction, key the vid (hex16), then "<etype>|<dir>" (non-zero only; a
//                     vertex with no edge has an empty entry). --anchor-vids FILE (one encoded vid per line, hex16) adds
//                     those vertices, of any label: the vids a study's params bind; each listed vid has an entry
//   anchor_recency    per recency key, each vertex's own [k = 1, 10, 20] row
//   anchor_far_labels each anchor vertex's edges per far-end label, key the vid, then "<etype>|<dir>", then the label:
//                     only for an (etype, dir) whose far end declares 2+ labels (a person's posts vs comments)
// The last three are the anchor tier (predict reads it at the anchor level only, never at level predicted): in their own file
// with --anchor-out FILE, else on stdout with the rest. --dataset NAME is written as "dataset". JSON on stdout.
#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <cmath>
#include <fstream>
#include <iostream>
#include <map>
#include <set>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <array>
#include <tuple>
#include <vector>

#include "cli/commands.h"
#include "ingest/ingest.h"
#include "provenance.h"
#include "schema/schema.h"
#include "util/record_stream.h"
#include "util/uidset.h"
#include "util/vid.h"
#include "value/value.h"

namespace evalcpp {

namespace {
// a per-unit histogram, the form date_days and int_values share: {<first_member>: the least unit, "counts": one count
// per unit up to the greatest, 0 where none falls}
json dense_counts(const std::map<int64_t, uint64_t>& m, const char* first_member) {
  json counts = json::array();
  for (int64_t u = m.begin()->first; u <= m.rbegin()->first; u++) {
    auto it = m.find(u);
    counts.push_back(it == m.end() ? 0 : it->second);
  }
  return {{first_member, m.begin()->first}, {"counts", counts}};
}

// int_values is written for a property whose values span at most this many integers (a wider one: value_range only)
constexpr int64_t kMaxIntValues = 1000000;

// top_values keeps this many values per indexed property (tier 2; tier 1 cuts it to 10, python/kvevaluator/predict/tier1.py)
constexpr size_t kTopValues = 1000;

// top_degrees keeps this many values per (owner label, edge type, direction, indexed property), both tiers
constexpr size_t kTopDegrees = 10;

// one indexed value: its elements, and its JSON text (the key an equality's value is matched by)
struct ValueCount {
  uint64_t n = 0;
  std::string text;
};


using Degrees = std::unordered_map<uint64_t, uint32_t>;   // vid -> degree

// a value's content bytes, as python/kvevaluator/schema/graph.py content_bytes (the neutral widths): a string its length
// in characters, an int 8, a bool 1, a float 0, a bytes value ({"$bytes": base64}) its raw length, a list or an object
// the sum of its members
uint64_t utf8_chars(const std::string& s) {
  uint64_t n = 0;
  for (unsigned char c : s) n += (c & 0xC0) != 0x80;
  return n;
}
uint64_t content_bytes(const json& v) {
  if (v.is_string()) return utf8_chars(v.get_ref<const std::string&>());
  if (v.is_boolean()) return 1;
  if (v.is_number_integer()) return 8;
  if (v.is_object()) {
    if (v.contains("$bytes")) {
      const std::string& b = v["$bytes"].get_ref<const std::string&>();
      uint64_t pad = (!b.empty() && b.back() == '=') + (b.size() > 1 && b[b.size() - 2] == '=');
      return b.size() / 4 * 3 - pad;
    }
    uint64_t n = 0;
    for (auto& [k, x] : v.items()) n += content_bytes(x);
    return n;
  }
  if (v.is_array()) {
    uint64_t n = 0;
    for (auto& x : v) n += content_bytes(x);
    return n;
  }
  return 0;
}
// an index token's length: Python's len(str(value)) for a scalar (a list or an object is refused)
uint64_t token_chars(const json& v) {
  if (v.is_string()) return utf8_chars(v.get_ref<const std::string&>());
  if (v.is_boolean()) return v.get<bool>() ? 4 : 5;
  if (v.is_number()) return v.dump().size();
  throw std::runtime_error("graph-stats: an index token of a list or an object (not covered)");
}

int bucket_of(uint64_t d) {
  if (d == 0) return 0;
  int b = 1;
  while (d >>= 1) b++;
  return b;
}

// The version of what graph-stats writes. Bump it with any change that moves a number in its output (here or in the
// code it calls). build_stats keys a saved statistics file on it, with the contract's x-contract, and builds a file of
// another version again; tests/gates/check_statistics_rebuild.py refuses a changed sf0.1 file under the same version.
constexpr int kGraphStatsVersion = 5;   // 2: degree_by_far_label; 3: anchor_far_labels; 4: top_values, top_degrees;
                                        // 5: no date props in them, top_degrees' edges of the owners holding the prop

}  // namespace

int cmd_graph_stats(const std::vector<std::string>& args) {
  auto t0 = std::chrono::steady_clock::now();
  if (args.size() == 1 && args[0] == "--version") {   // the version only, for a reuse check: no pass over the data
    std::cout << nlohmann::json{{"graph_stats_version", kGraphStatsVersion}}.dump() << "\n";
    return 0;
  }
  std::string schema_path, records_path;
  std::set<std::string> anchor_labels;
  std::string anchor_vids_path, anchor_out, dataset, date_axis;
  for (size_t i = 0; i + 1 < args.size(); i += 2) {
    if (args[i] == "--schema") schema_path = args[i + 1];
    else if (args[i] == "--dataset") dataset = args[i + 1];
    else if (args[i] == "--anchor-out") anchor_out = args[i + 1];
    else if (args[i] == "--date-axis") date_axis = args[i + 1];
    else if (args[i] == "--records") records_path = args[i + 1];
    else if (args[i] == "--anchor-vids") anchor_vids_path = args[i + 1];
    else if (args[i] == "--anchor-labels") {
      std::string l;
      for (char c : args[i + 1] + ",") {
        if (c != ',') { l += c; continue; }
        if (!l.empty()) anchor_labels.insert(l);
        l.clear();
      }
    }
    else { std::cerr << "graph-stats: unknown option " << args[i] << "\n"; return 2; }
  }
  if (schema_path.empty() || records_path.empty() || args.size() % 2) {
    std::cerr << "usage: kvevaluator graph-stats --schema <schema.json> --records <records.jsonl> [--dataset NAME] "
                 "[--anchor-labels L,...] [--anchor-vids FILE] [--anchor-out FILE] [--date-axis L,...:prop]\n"
                 "       kvevaluator graph-stats --version\n";
    return 2;
  }
  Schema schema = Schema::load(schema_path);
  std::set<uint64_t> anchor_vids;
  if (!anchor_vids_path.empty()) {
    std::ifstream f(anchor_vids_path);
    if (!f) { std::cerr << "graph-stats: cannot read --anchor-vids " << anchor_vids_path << "\n"; return 2; }
    for (std::string line; std::getline(f, line);) {
      if (line.empty()) continue;
      uint64_t u;
      if (!canonical_vid(line, &u)) { std::cerr << "graph-stats: --anchor-vids: '" << line << "' is not an encoded vid\n"; return 2; }
      anchor_vids.insert(u);
    }
  }
  for (auto& l : anchor_labels) {
    bool known = false;
    for (auto& v : schema.vertices()) known = known || v.label == l;
    if (!known) { std::cerr << "graph-stats: --anchor-labels names no label " << l << "\n"; return 2; }
  }
  // the declared date axis: the labels and the date property every date rule reads (absent: each step's own labels)
  json axis;
  if (!date_axis.empty()) {
    size_t colon = date_axis.find(':');
    if (colon == std::string::npos) { std::cerr << "graph-stats: --date-axis wants L1,L2:prop\n"; return 2; }
    std::string prop = date_axis.substr(colon + 1), l;
    std::vector<std::string> labels;
    for (char c : date_axis.substr(0, colon) + ",") {
      if (c != ',') { l += c; continue; }
      if (!l.empty()) labels.push_back(l);
      l.clear();
    }
    for (auto& lab : labels) {
      bool dated = false;
      for (auto& v : schema.vertices())
        if (v.label == lab)
          for (auto& pd : v.props) dated = dated || (pd.name == prop && pd.type == "date");
      if (!dated) { std::cerr << "graph-stats: --date-axis: " << lab << " has no date property " << prop << "\n"; return 2; }
    }
    axis = {{"labels", labels}, {"prop", prop}};
  }
  // recency keys: for a vertex of an anchor label L, its neighbours over (etype, dir) by a date property every far
  // label of that end declares (L not itself a far label): "L|etype|dir|prop"
  struct RecKey { std::string label, etype, dir, prop; std::vector<std::string> far; };
  std::vector<RecKey> rec_keys;
  std::set<std::pair<std::string, std::string>> rec_dated;   // (far label, prop) whose vertices' days the pass keeps
  for (auto& l : anchor_labels)
    for (auto& e : schema.edges())
      for (bool out : {true, false}) {
        if (!schema.owns_edge(l, e.etype, out)) continue;
        std::vector<std::string> far = schema.far_labels(e.etype, out);
        if (far.empty() || std::find(far.begin(), far.end(), l) != far.end()) continue;
        std::set<std::string> common;
        bool first = true;
        for (auto& f : far) {
          std::set<std::string> ps;
          for (auto& p : schema.vertex(f).props) if (p.type == "date") ps.insert(p.name);
          if (first) common = ps;
          else { std::set<std::string> keep; for (auto& p : common) if (ps.count(p)) keep.insert(p); common = keep; }
          first = false;
        }
        for (auto& p : common) {
          rec_keys.push_back({l, e.etype, out ? "out" : "in", p, far});
          for (auto& f : far) rec_dated.insert({f, p});
        }
      }
  // burstiness keys: "etype|dir|prop" for a date property every far label of that end indexes (sorted), every owner
  // label of the near end (ic8's replyOf|in is owned by messages)
  struct BurstKey { std::string etype, dir, prop; std::vector<std::string> far; };
  std::vector<BurstKey> burst_keys;
  for (auto& e : schema.edges())
    for (bool out : {true, false}) {
      std::vector<std::string> far = schema.far_labels(e.etype, out);
      if (far.empty()) continue;
      std::set<std::string> common;
      bool first = true;
      for (auto& f : far) {
        std::set<std::string> ps;
        for (auto& p : schema.vertex(f).props)
          if (p.type == "date" && schema.is_date_col(f, p.name)) ps.insert(p.name);
        if (first) common = ps;
        else { std::set<std::string> keep; for (auto& p : common) if (ps.count(p)) keep.insert(p); common = keep; }
        first = false;
      }
      for (auto& p : common) {
        burst_keys.push_back({e.etype, out ? "out" : "in", p, far});
        for (auto& f : far) rec_dated.insert({f, p});
      }
    }
  std::vector<std::unordered_map<uint64_t, std::vector<int32_t>>> burst_days(burst_keys.size());   // key -> owner -> days
  std::map<std::string, std::vector<size_t>> burst_of_etype;
  for (size_t i = 0; i < burst_keys.size(); i++) burst_of_etype[burst_keys[i].etype].push_back(i);
  std::map<std::string, std::unordered_map<uint64_t, int64_t>> vday;   // prop -> vid -> day (the far labels' vertices)
  std::vector<std::unordered_map<uint64_t, std::vector<int32_t>>> rec_days(rec_keys.size());   // key -> near vid -> days
  std::map<std::string, std::vector<size_t>> rec_of_etype;
  for (size_t i = 0; i < rec_keys.size(); i++) rec_of_etype[rec_keys[i].etype].push_back(i);
  // forest candidates: an edge type whose source labels are all among its destination labels (replyOf, isSubclassOf);
  // it is a forest when every source has one such edge (checked after the pass)
  std::map<std::string, std::unordered_map<uint64_t, uint64_t>> parent;
  for (auto& e : schema.edges()) {
    std::set<std::string> srcs, dsts;
    for (auto& [s, d] : e.endpoints) { srcs.insert(s); dsts.insert(d); }
    bool self = !srcs.empty();
    for (auto& s : srcs) self = self && dsts.count(s);
    if (self) parent[e.etype];
  }
  std::map<std::string, std::vector<uint64_t>> anchor_label_vids;   // every vertex of an anchor label
  // anchor_far_labels: the (etype, out) whose far end declares 2+ labels, and per anchor vertex its edges by far label
  std::map<std::pair<std::string, bool>, bool> multi_far;
  for (auto& e : schema.edges())
    for (bool out : {true, false}) multi_far[{e.etype, out}] = schema.far_labels(e.etype, out).size() > 1;
  std::unordered_map<uint64_t, std::map<std::string, std::map<std::string, uint64_t>>> anchor_far;
  std::map<std::string, std::vector<std::string>> date_props;   // label -> its date properties (schema type "date")
  // value_range and int_values: every declared int property's elements per value, key prop|L|p (vertex) or eprop|E|p (edge)
  std::map<std::string, std::map<int64_t, uint64_t>> ivals;
  auto count_int = [&](const std::string& key, const json& v) {
    if (v.is_null()) return;
    if (!v.is_number_integer())
      throw std::runtime_error("graph-stats: the int property " + key + " holds " + v.dump() + ", not an integer");
    ivals[key][v.get<int64_t>()]++;
  };
  std::map<std::string, std::vector<std::string>> int_props;   // label or edge type -> its int properties
  std::map<std::pair<std::string, std::string>, std::map<int64_t, uint64_t>> days;   // (label, prop) -> day -> vertices
  std::ifstream records(records_path);
  if (!records) { std::cerr << "graph-stats: cannot read --records " << records_path << "\n"; return 2; }
  json stream = record_stream_at_open(records_path);       // labelled now, before the first record
  std::map<std::string, uint64_t> type_counts;
  // (label, "etype|dir") -> per-vertex degree; dir out (the vertex is the source) or in (the target)
  std::map<std::pair<std::string, std::string>, Degrees> deg;
  std::map<std::pair<std::string, std::string>, uint64_t> edges;
  // degree_by_far_label: (owner label, "etype|dir") -> the far labels the schema declares at the other end; a list whose
  // far end can hold several labels also counts, per far label, each owner's edges to it
  std::map<std::pair<std::string, std::string>, std::set<std::string>> far_labels;
  for (auto& e : schema.edges())
    for (auto& [s, d] : e.endpoints) {
      far_labels[{s, e.etype + "|out"}].insert(d);
      far_labels[{d, e.etype + "|in"}].insert(s);
    }
  std::map<std::tuple<std::string, std::string, std::string>, Degrees> far_deg;   // (owner label, etype|dir, far label)
  auto count_far = [&](const std::string& owner_label, const std::string& key, const std::string& far, uint64_t owner) {
    auto f = far_labels.find({owner_label, key});
    if (f != far_labels.end() && f->second.size() > 1) far_deg[{owner_label, key, far}][owner]++;
  };
  // the neutral core: per edge type its edges and distinct ends; per indexed property its distinct values (a hash of
  // the value's JSON); content bytes per property, index token, edge props and endpoint label
  std::map<std::string, uint64_t> etype_edges;
  std::map<std::string, std::unordered_set<uint64_t>> etype_srcs, etype_dsts;
  std::set<std::pair<std::string, std::string>> indexed;
  for (auto& ix : schema.indexes()) if (!ix.prop.empty()) indexed.insert({ix.label, ix.prop});
  // an indexed date prop: its distinct values only (ndv), no top_values or top_degrees (no plan reads a date by
  // equality; at SF3 they are 8.9 M of the 9.1 M indexed holders)
  std::set<std::pair<std::string, std::string>> indexed_dates;
  for (auto& [l, p] : indexed)
    for (auto& vp : schema.vertex(l).props)
      if (vp.name == p && vp.type == "date") indexed_dates.insert({l, p});
  std::map<std::pair<std::string, std::string>, std::unordered_set<uint64_t>> date_values;   // hash of the JSON
  std::map<std::pair<std::string, std::string>, std::unordered_map<uint64_t, ValueCount>> values;   // hash of the JSON -> count
  std::map<std::pair<std::string, std::string>, std::unordered_map<uint64_t, uint64_t>> vid_value;   // vid -> hash (top_degrees)
  std::map<std::string, std::array<uint64_t, 2>> content;   // "fam|parts" -> [bytes, units]
  // each phase's seconds to stderr, so a slow run's log says where the time went (stdout's JSON keeps one total wall_s)
  auto mark = t0;
  auto phase = [&mark](const char* name) {
    auto now = std::chrono::steady_clock::now();
    std::cerr << "graph-stats: " << name << " " << std::chrono::duration<double>(now - mark).count() << " s\n";
    mark = now;
  };
  std::string line;
  uint64_t n_lines = 0;
  while (std::getline(records, line)) {
    if (line.empty()) continue;
    n_lines++;
    json r = json::parse(line);
    const std::string kind = r.at(0).get<std::string>();
    if (kind == "v") {
      const std::string label = r.at(2).get<std::string>();
      type_counts[label]++;
      auto dp = date_props.find(label);
      if (dp == date_props.end()) {
        std::vector<std::string> ps;
        for (auto& p : schema.vertex(label).props) if (p.type == "date") ps.push_back(p.name);
        dp = date_props.emplace(label, ps).first;
      }
      const json& props = r.at(3);
      if (anchor_labels.count(label)) anchor_label_vids[label].push_back(parse_vid(r.at(1)));
      for (auto& [lp, p] : rec_dated)
        if (lp == label && props.contains(p) && props[p].is_number()) {
          int64_t ms = props[p].get<int64_t>();
          vday[p][parse_vid(r.at(1))] = ms >= 0 ? ms / 86400000 : (ms - 86399999) / 86400000;
        }
      for (auto& [p, val] : props.items()) {
        if (indexed_dates.count({label, p})) {
          date_values[{label, p}].insert(std::hash<std::string>()(val.dump()));
        } else if (indexed.count({label, p})) {
          std::string text = val.dump();
          uint64_t h = std::hash<std::string>()(text);
          ValueCount& vc = values[{label, p}][h];
          if (vc.n++ == 0) vc.text = std::move(text);
          if (!val.is_null()) vid_value[{label, p}][parse_vid(r.at(1))] = h;   // a null value never matches an equality
        }
        if (val.is_null()) continue;   // an absent value: no content, no index token
        auto& c = content["prop|" + label + "|" + p];
        c[0] += content_bytes(val);
        c[1]++;
        if (indexed.count({label, p})) {
          auto& t = content["tok|" + label + "|" + p];
          t[0] += token_chars(val);
          t[1]++;
        }
      }
      for (auto& p : dp->second)
        if (props.contains(p) && props[p].is_number()) {
          int64_t ms = props[p].get<int64_t>();
          days[{label, p}][ms >= 0 ? ms / 86400000 : (ms - 86399999) / 86400000]++;
        }
      auto ip = int_props.find("v|" + label);
      if (ip == int_props.end()) {
        std::vector<std::string> ps;
        for (auto& p : schema.vertex(label).props) if (p.type == "int") ps.push_back(p.name);
        ip = int_props.emplace("v|" + label, ps).first;
      }
      for (auto& p : ip->second)
        if (props.contains(p)) count_int("prop|" + label + "|" + p, props[p]);
    } else if (kind == "e") {
      std::string etype = r.at(1).get<std::string>(), sl = r.at(5).get<std::string>(), dl = r.at(6).get<std::string>();
      etype_edges[etype]++;
      etype_srcs[etype].insert(parse_vid(r.at(2)));
      etype_dsts[etype].insert(parse_vid(r.at(3)));
      auto& ep = content["eprop|" + etype];
      ep[0] += r.at(4).is_null() ? 0 : content_bytes(r.at(4));
      ep[1]++;
      {
        auto ip = int_props.find("e|" + etype);
        if (ip == int_props.end()) {
          std::vector<std::string> ps;
          for (auto& p : schema.edge_props(etype)) if (p.type == "int") ps.push_back(p.name);
          ip = int_props.emplace("e|" + etype, ps).first;
        }
        const json& eprops = r.at(4);
        for (auto& p : ip->second)
          if (eprops.is_object() && eprops.contains(p)) count_int("eprop|" + etype + "|" + p, eprops[p]);
      }
      content["esrc|" + etype + "|" + sl][1]++;
      content["edst|" + etype + "|" + dl][1]++;
      {
        auto pf = parent.find(etype);
        if (pf != parent.end()) pf->second[parse_vid(r.at(2))] = parse_vid(r.at(3));
        auto rk = rec_of_etype.find(etype);
        if (rk != rec_of_etype.end())
          for (size_t i : rk->second) {
            const RecKey& k = rec_keys[i];
            bool out = k.dir == "out";
            if ((out ? sl : dl) != k.label) continue;
            uint64_t near = parse_vid(r.at(out ? 2 : 3)), far = parse_vid(r.at(out ? 3 : 2));
            const auto& days_of = vday[k.prop];
            auto it = days_of.find(far);
            if (it != days_of.end()) rec_days[i][near].push_back(int32_t(it->second));
          }
        auto bk = burst_of_etype.find(etype);
        if (bk != burst_of_etype.end())
          for (size_t i : bk->second) {
            bool out = burst_keys[i].dir == "out";
            uint64_t near = parse_vid(r.at(out ? 2 : 3)), far = parse_vid(r.at(out ? 3 : 2));
            const auto& days_of = vday[burst_keys[i].prop];
            auto it = days_of.find(far);
            if (it != days_of.end()) burst_days[i][near].push_back(int32_t(it->second));
          }
      }
      deg[{sl, etype + "|out"}][parse_vid(r.at(2))]++;
      deg[{dl, etype + "|in"}][parse_vid(r.at(3))]++;
      count_far(sl, etype + "|out", dl, parse_vid(r.at(2)));
      count_far(dl, etype + "|in", sl, parse_vid(r.at(3)));
      for (bool out : {true, false}) {   // an anchor's edges by the far end's label, where the far end has 2+ labels
        auto mf = multi_far.find({etype, out});
        if (mf == multi_far.end() || !mf->second) continue;
        const std::string& near_label = out ? sl : dl;
        uint64_t near = parse_vid(r.at(out ? 2 : 3));
        if (anchor_labels.count(near_label) || anchor_vids.count(near))
          anchor_far[near][etype + (out ? "|out" : "|in")][out ? dl : sl]++;
      }
      edges[{sl, etype + "|out"}]++;
      edges[{dl, etype + "|in"}]++;
    } else {
      throw std::runtime_error("graph-stats: record line " + std::to_string(n_lines) + ": unknown kind '" + kind + "'");
    }
  }
  phase("read the records (with the arguments and the schema)");
  // "both" (out + in) on a label at both ends of an edge type
  std::vector<std::pair<std::string, std::string>> both;
  for (auto& [k, _] : deg) {
    const std::string& key = k.second;
    if (key.size() > 4 && key.compare(key.size() - 4, 4, "|out") == 0) {
      std::string et = key.substr(0, key.size() - 4);
      if (deg.count({k.first, et + "|in"})) both.push_back({k.first, et});
    }
  }
  for (auto& [label, et] : both) {
    Degrees sum = deg[{label, et + "|out"}];
    for (auto& [v, d] : deg[{label, et + "|in"}]) sum[v] += d;
    deg[{label, et + "|both"}] = std::move(sum);
    edges[{label, et + "|both"}] = edges[{label, et + "|out"}] + edges[{label, et + "|in"}];
  }
  json owners = json::array();
  for (auto& [k, m] : deg) {
    const std::string& key = k.second;
    size_t bar = key.rfind('|');
    std::string dir = key.substr(bar + 1);
    if (dir == "both") continue;   // the owners section is per stored direction
    owners.push_back({{"etype", key.substr(0, bar)}, {"direction", dir}, {"owner_label", k.first},
                      {"owners", m.size()}, {"edges", edges[k]}});
  }
  // the plain degree histogram per direction (out, in, and both on a label at both ends), the degree-0 vertices
  // included
  json dh = json::object();
  for (auto& [k, m] : deg) {
    const std::string& key = k.second;
    std::map<int, std::array<double, 3>> h;   // bucket -> vertices, sum of degree, sum of squared degree
    for (auto& [v, d] : m) {
      auto& b = h[bucket_of(d)];
      b[0] += 1;
      b[1] += double(d);
      b[2] += double(d) * double(d);
    }
    h[0][0] = double(type_counts[k.first]) - double(m.size());
    json rows = json::array();
    for (auto& [b, x] : h) rows.push_back({b, x[0], x[1], x[2]});
    dh[k.first + "|" + key] = rows;
  }
  // degree_by_far_label: per declared (owner label, etype, dir, far label), [the edges whose far end has that label,
  // the summed degree of the owner each of them lands on]: sum over owners of n_far(o) x d(o). Its ratio is the list
  // size a new edge from that label meets, an owner drawn by its edges from that label. A list with one declared far
  // label has sum d^2 there; every declared entry is written, [0, 0] when the data has none (the contract)
  json dfl = json::object();
  for (auto& [k, fars] : far_labels) {
    auto dk = deg.find(k);
    for (auto& far : fars) {
      double n = 0, sum = 0;
      if (dk != deg.end()) {
        if (fars.size() == 1) {
          for (auto& [v, d] : dk->second) { n += double(d); sum += double(d) * double(d); }
        } else {
          auto fk = far_deg.find({k.first, k.second, far});
          if (fk != far_deg.end())
            for (auto& [v, c] : fk->second) { n += double(c); sum += double(c) * double(dk->second.at(v)); }
        }
      }
      dfl[k.first + "|" + k.second + "|" + far] = {n, sum};
    }
  }
  // the histograms: every ordered pair (via, target) of distinct degrees on one label
  json dgv = json::object();
  std::map<std::pair<std::string, std::string>, std::array<double, 2>> cond_sums;   // (via, target) -> sum via, sum via x target
  std::map<std::string, std::vector<std::string>> keys_of;   // label -> its degree keys
  for (auto& [k, _] : deg) keys_of[k.first].push_back(k.second);
  for (auto& [label, keys] : keys_of) {
    double n_label = double(type_counts[label]);
    for (auto& via : keys) {
      const Degrees& V = deg[{label, via}];
      for (auto& tgt : keys) {
        if (tgt == via) continue;
        const Degrees& T = deg[{label, tgt}];
        std::map<int, std::array<double, 3>> h;   // bucket -> vertices, via sum, target sum
        double tsum_in_v = 0;
        for (auto& [v, d] : V) {
          auto it = T.find(v);
          double t = it == T.end() ? 0.0 : double(it->second);
          auto& b = h[bucket_of(d)];
          b[0] += 1;
          b[1] += d;
          b[2] += t;
          tsum_in_v += t;
          auto& cs = cond_sums[{via, tgt}];
          cs[0] += double(d);
          cs[1] += double(d) * t;
        }
        double ttotal = double(edges[{label, tgt}]);
        auto& z = h[0];
        z[0] = n_label - double(V.size());
        z[2] = ttotal - tsum_in_v;
        json rows = json::array();
        for (auto& [b, x] : h) rows.push_back({b, x[0], x[1], x[2]});
        dgv[label + "|" + via + ">" + tgt] = rows;
      }
    }
  }
  json dd = json::object();
  for (auto& [k, m] : days) {
    if (m.empty()) continue;
    dd[k.first + "|" + k.second] = dense_counts(m, "first_day");
  }
  json ad = json::object();
  for (uint64_t v : anchor_vids) ad[hex16(v)] = json::object();   // a listed vid with no edge: its degree is 0, not unknown
  // every vertex of an anchor label has an entry: one with no edge at all has degree 0, not "unknown"
  for (auto& [l, vs] : anchor_label_vids)
    for (uint64_t v : vs)
      if (!ad.contains(hex16(v))) ad[hex16(v)] = json::object();
  for (auto& [k, m] : deg) {   // (label, "etype|dir"): the stored directions only (not the derived "|both")
    bool both = k.second.size() >= 5 && k.second.compare(k.second.size() - 5, 5, "|both") == 0;
    if (both) continue;
    bool by_label = anchor_labels.count(k.first) > 0;
    if (!by_label && anchor_vids.empty()) continue;
    for (auto& [v, d] : m)
      if (d > 0 && (by_label || anchor_vids.count(v))) ad[hex16(v)][k.second] = d;
  }
  // a degree's population mean, as predict's population_degree reads it: the edges over the vertices of the labels
  // seen at that end (sizes.content esrc / edst keys); "both" is the sum of the two ends'. 0 when no label is seen
  auto pop_degree = [&](const std::string& key) {
    size_t bar = key.rfind('|');
    std::string et = key.substr(0, bar), dir = key.substr(bar + 1);
    double e = double(etype_edges[et]);
    auto side = [&](const char* s) {
      double n = 0;
      std::string pre = std::string(s) + "|" + et + "|";
      for (auto& [k, _] : content)
        if (k.compare(0, pre.size(), pre) == 0) n += double(type_counts[k.substr(pre.size())]);
      return n;
    };
    if (dir == "both") {
      double po = side("esrc"), pi = side("edst");
      return po > 0 && pi > 0 ? e / po + e / pi : 0.0;
    }
    double n = side(dir == "out" ? "esrc" : "edst");
    return n > 0 ? e / n : 0.0;
  };
  // Python's round(x, 4)
  auto round4 = [](double x) {
    char buf[64];
    std::snprintf(buf, sizeof buf, "%.4f", x);
    return std::strtod(buf, nullptr);
  };
  // the conditional degrees: the mean target degree of a vertex reached through a via edge (each vertex weighted by
  // its via degree), over the target's population mean; the labels holding both degrees, rounded to 4 places
  json cond = json::object();
  for (auto& [k, s] : cond_sums) {
    double pd = pop_degree(k.second);
    if (s[0] <= 0 || pd <= 0) continue;
    cond[k.first + ">" + k.second] = round4(s[1] / s[0] / pd);
  }
  // forests: an edge type whose source labels are all destination labels and whose every source has one such edge.
  // Raw: its sources, the summed depth (hops to the chain's root), the source labels and every label of the type;
  // predict divides (a mean over the source labels' vertices, or over every label's)
  json forest = json::object();
  for (auto& [et, par] : parent) {
    if (par.empty()) continue;
    bool one = true;
    for (auto& [k, m] : deg)
      if (k.second == et + "|out")
        for (auto& [v, d] : m) one = one && d == 1;
    if (!one) continue;
    std::vector<uint64_t> starts;
    for (auto& [v, _] : par) starts.push_back(v);
    std::sort(starts.begin(), starts.end());
    std::unordered_map<uint64_t, uint64_t> depth;
    for (uint64_t start : starts) {   // each chain followed to its root; a cycle broken at the revisit
      std::vector<uint64_t> chain;
      std::unordered_set<uint64_t> on;
      uint64_t cur = start;
      while (par.count(cur) && !depth.count(cur)) {
        if (on.count(cur)) break;
        chain.push_back(cur);
        on.insert(cur);
        cur = par.at(cur);
      }
      uint64_t base = depth.count(cur) ? depth[cur] : 0;
      for (size_t i = 0; i < chain.size(); i++) depth[chain[chain.size() - 1 - i]] = base + i + 1;
    }
    uint64_t dsum = 0;
    for (auto& [v, d] : depth) dsum += d;
    std::set<std::string> srcl, alll;
    for (auto& [s, d] : schema.edge(et).endpoints) { srcl.insert(s); alll.insert(s); alll.insert(d); }
    forest[et] = {{"sources", par.size()}, {"depth_sum", dsum},
                  {"source_labels", std::vector<std::string>(srcl.begin(), srcl.end())},
                  {"labels", std::vector<std::string>(alll.begin(), alll.end())}};
    // the chase-root ratio: a chase from every source lands on its root once per source (each root weighted by its
    // sources); per target degree of the roots' labels, its mean over those landings over its population mean
    std::unordered_map<uint64_t, uint64_t> root_of, root_w;
    for (uint64_t c : starts) {   // stats.py _cond_degree's path-compressed walk
      std::vector<uint64_t> seen;
      std::unordered_set<uint64_t> on;
      uint64_t v = c;
      while (par.count(v) && !on.count(v)) {
        auto hit = root_of.find(v);
        if (hit != root_of.end()) { v = hit->second; break; }
        seen.push_back(v);
        on.insert(v);
        v = par.at(v);
      }
      for (uint64_t x : seen) root_of[x] = v;
      root_w[v]++;
    }
    double wsum = 0;
    for (auto& [r, w] : root_w) wsum += double(w);
    std::set<std::string> tkeys;
    for (auto& [k, _] : deg) if (alll.count(k.first)) tkeys.insert(k.second);
    for (auto& tk : tkeys) {
      double pd = pop_degree(tk), dot = 0;
      if (pd <= 0 || wsum <= 0) continue;
      for (auto& [r, w] : root_w)
        for (auto& l : alll) {
          auto m = deg.find({l, tk});
          if (m == deg.end()) continue;
          auto it = m->second.find(r);
          if (it != m->second.end()) dot += double(w) * double(it->second);
        }
      cond["chase-root|" + et + ">" + tk] = round4(dot / wsum / pd);
    }
  }
  phase("degrees, histograms and forests");
  // recency: per key, for each vertex with a neighbour there, the days back from the far labels' last day to its k-th
  // newest neighbour (k = 1, 10, 20; the whole span when it has fewer than k); the population means and deciles here,
  // each vertex's own row in the anchor tier (stats.py _recency_summary)
  json recency = json::object(), anchor_recency = json::object();
  for (size_t i = 0; i < rec_keys.size(); i++) {
    const RecKey& k = rec_keys[i];
    int64_t lo = 0, hi = 0;
    bool any = false;
    for (auto& f : k.far) {
      auto it = days.find({f, k.prop});
      if (it == days.end() || it->second.empty()) continue;
      int64_t a = it->second.begin()->first, b = it->second.rbegin()->first;
      lo = any ? std::min(lo, a) : a;
      hi = any ? std::max(hi, b) : b;
      any = true;
    }
    if (!any) continue;
    int64_t span = hi - lo + 1, last = hi;
    const int ks[3] = {1, 10, 20};
    std::vector<std::array<int64_t, 3>> rows;
    std::vector<uint64_t> vids;
    for (auto& [v, _] : rec_days[i]) vids.push_back(v);
    std::sort(vids.begin(), vids.end());
    std::string key = k.label + "|" + k.etype + "|" + k.dir + "|" + k.prop;
    json per = json::object();
    for (uint64_t v : vids) {
      std::vector<int32_t> ds = rec_days[i][v];
      std::sort(ds.begin(), ds.end(), std::greater<int32_t>());
      std::array<int64_t, 3> row;
      for (int j = 0; j < 3; j++)
        row[j] = ds.size() >= size_t(ks[j]) ? std::min(span, last - int64_t(ds[ks[j] - 1])) : span;
      rows.push_back(row);
      per[hex16(v)] = {row[0], row[1], row[2]};
    }
    json mean = json::object(), dec = json::object();
    for (int j = 0; j < 3; j++) {
      std::vector<int64_t> vals;
      for (auto& r : rows) vals.push_back(r[j]);
      std::sort(vals.begin(), vals.end());
      if (vals.empty()) continue;
      double s = 0;
      for (int64_t x : vals) s += double(x);
      mean[std::to_string(ks[j])] = s / double(vals.size());
      json d = json::array();
      for (int q = 0; q <= 10; q++) {
        double at = std::nearbyint(double(q * int64_t(vals.size() - 1)) / 10.0);
        d.push_back(vals[std::min(vals.size() - 1, size_t(at))]);
      }
      dec[std::to_string(ks[j])] = d;
    }
    recency[key] = {{"vertices", vids.size()}, {"span", span}, {"last_day", last}, {"mean", mean}, {"deciles", dec}};
    anchor_recency[key] = per;
  }
  phase("recency");
  // burstiness: per key, the walk's own quantity per owner (the ranked date-index walk prices crossed = k N / n
  // entries for n candidates: an even spread). The index: the far labels' vertices by day, newest first; an owner's
  // k-th newest neighbour on day t has rank (the entries on newer days) + half of day t's. Only owners with at least 4k
  // neighbours: the even spread k N / n is then at most N / 4, so the ratio measures where the k-th newest sits, not the
  // owner's size (with n near k the even spread is near all of N and the ratio is about 0.5 whatever the dates). A key
  // with no such owner is 1, the even spread
  json burstiness = json::object();
  for (size_t i = 0; i < burst_keys.size(); i++) {
    const BurstKey& bk = burst_keys[i];
    std::map<int64_t, double> per_day;
    for (auto& f : bk.far) {
      auto it = days.find({f, bk.prop});
      if (it == days.end()) continue;
      for (auto& [day, c] : it->second) per_day[day] += double(c);
    }
    double total = 0;
    std::map<int64_t, double> newer;   // day -> the entries on newer days
    for (auto it = per_day.rbegin(); it != per_day.rend(); ++it) { newer[it->first] = total; total += it->second; }
    const size_t k = 20, min_n = 4 * k;
    std::vector<uint64_t> vids;
    for (auto& [v, ds] : burst_days[i]) if (ds.size() >= min_n) vids.push_back(v);
    std::sort(vids.begin(), vids.end());
    double bsum = 0, wsum = 0;
    for (uint64_t v : vids) {
      std::vector<int32_t> ds = burst_days[i][v];
      std::sort(ds.begin(), ds.end(), std::greater<int32_t>());
      double n = double(ds.size());
      int64_t t = ds[k - 1];
      double crossed = newer[t] + 0.5 * per_day[t];
      double even = std::min(total, double(k) * total / n);
      bsum += n * crossed / even;
      wsum += n;
    }
    std::string key = bk.etype + "|" + bk.dir + "|" + bk.prop;
    burstiness[key] = wsum > 0 ? std::nearbyint(bsum / wsum * 1e6) / 1e6 : 1.0;
    std::cerr << "graph-stats: burstiness " << key << " " << burstiness[key].get<double>() << " over " << vids.size()
              << " owners with >= " << min_n << " neighbours, of " << burst_days[i].size() << "\n";
  }
  for (auto& v : schema.vertices()) type_counts.emplace(v.label, 0);   // every label, 0 when the data has none (the contract)
  phase("burstiness");
  json tc = json::object();
  for (auto& [l, n] : type_counts) tc[l] = n;
  // every edge type and every property of the schema has an entry, 0 when the data has none (the contract)
  json et_tot = json::object(), d_src = json::object(), d_dst = json::object(), ndv = json::object();
  for (auto& e : schema.edges()) {
    et_tot[e.etype] = etype_edges[e.etype];
    d_src[e.etype] = etype_srcs[e.etype].size();
    d_dst[e.etype] = etype_dsts[e.etype].size();
  }
  json top_values = json::object();
  for (auto& [lp, hs] : date_values) ndv[lp.first + "|" + lp.second] = hs.size();
  for (auto& [lp, vs] : values) {
    ndv[lp.first + "|" + lp.second] = vs.size();
    std::vector<const ValueCount*> all;
    uint64_t holders = 0;
    for (auto& [h, vc] : vs) {
      all.push_back(&vc);
      holders += vc.n;
    }
    size_t k = std::min(kTopValues, all.size());
    std::partial_sort(all.begin(), all.begin() + k, all.end(), [](const ValueCount* a, const ValueCount* b) {
      return a->n != b->n ? a->n > b->n : a->text < b->text;
    });
    json listed = json::array();
    for (size_t i = 0; i < k; i++) listed.push_back(json::array({json::parse(all[i]->text), all[i]->n}));
    top_values[lp.first + "|" + lp.second] = {{"k", kTopValues}, {"holders", holders}, {"values", listed}};
  }
  json top_degrees = json::object();
  for (auto& [k, fars] : far_labels) {   // every declared (owner label, etype|dir)
    auto dk = deg.find(k);
    for (auto& [lp, by_vid] : vid_value) {
      if (lp.first != k.first) continue;
      std::unordered_map<uint64_t, uint64_t> by_value;   // value hash -> the edges of its vertices
      uint64_t n_owners = 0, total = 0;
      if (dk != deg.end())
        for (auto& [v, d] : dk->second) {
          n_owners++;
          auto it = by_vid.find(v);
          if (it == by_vid.end()) continue;   // an owner without the prop: no equality reaches it
          total += d;
          by_value[it->second] += d;
        }
      const auto& texts = values[lp];
      std::vector<std::pair<uint64_t, const std::string*>> all;
      for (auto& [h, n] : by_value) all.push_back({n, &texts.at(h).text});
      size_t kk = std::min(kTopDegrees, all.size());
      std::partial_sort(all.begin(), all.begin() + kk, all.end(), [](const auto& a, const auto& b) {
        return a.first != b.first ? a.first > b.first : *a.second < *b.second;
      });
      json listed = json::array();
      for (size_t i = 0; i < kk; i++) listed.push_back(json::array({json::parse(*all[i].second), all[i].first}));
      top_degrees[k.first + "|" + k.second + "|" + lp.second] = {{"k", kTopDegrees},
                                                                {"owners", n_owners},
                                                                {"owner_values", by_value.size()},
                                                                {"edges", total},
                                                                {"values", listed}};
    }
  }
  for (auto& v : schema.vertices())
    for (auto& p : v.props) content.try_emplace("prop|" + v.label + "|" + p.name, std::array<uint64_t, 2>{0, 0});
  json sizes = json::object();
  for (auto& [k, c] : content) sizes[k] = {{"b", c[0]}, {"u", c[1]}};
  json value_range = json::object();   // every declared int property: [least, greatest], null when no element holds it
  for (auto& v : schema.vertices())
    for (auto& p : v.props)
      if (p.type == "int") value_range["prop|" + v.label + "|" + p.name] = nullptr;
  for (auto& e : schema.edges())
    for (auto& p : schema.edge_props(e.etype))
      if (p.type == "int") value_range["eprop|" + e.etype + "|" + p.name] = nullptr;
  json int_values = json::object();
  for (auto& [k, m] : ivals) {
    value_range[k] = json::array({m.begin()->first, m.rbegin()->first});
    if (m.rbegin()->first - m.begin()->first < kMaxIntValues) int_values[k] = dense_counts(m, "first");
  }
  stream["lines"] = n_lines;
  phase("the main tier's entries");
  json out = {{"records", stream},
              {"type_counts", tc},
              {"edge_totals", et_tot},
              {"distinct_src", d_src},
              {"distinct_dst", d_dst},
              {"ndv", ndv},
              {"value_range", value_range},
              {"int_values", int_values},
              {"top_values", top_values},
              {"top_degrees", top_degrees},
              {"sizes", {{"content", sizes}}},
              {"owners", owners},
              {"degree_given_via", dgv},
              {"degree_hist", dh},
              {"date_days", dd},
              {"cond_degree", cond},
              {"forest", forest},
              {"recency", recency},
              {"burstiness", burstiness},
              {"degree_by_far_label", dfl},
              {"dataset", dataset},
              {"wall_s", std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count()},
              {"schema_hash", schema.hash()},   // predict refuses this file against another schema (require_schema)
              {"provenance", provenance()}};
  out["provenance"]["graph_stats_version"] = kGraphStatsVersion;   // the anchor tier's copy below carries it too
  // the anchor tier (per-vertex facts of the params' bound vertices): its own file with --anchor-out, else here
  if (!axis.is_null()) out["date_axis"] = axis;
  json afl = json::object();
  for (auto& [v, by_key] : anchor_far)
    for (auto& [k, by_label] : by_key)
      for (auto& [l, n] : by_label) afl[hex16(v)][k][l] = n;
  json anchor = {{"anchor_degrees", ad}, {"anchor_recency", anchor_recency}, {"anchor_far_labels", afl}};
  if (anchor_out.empty()) {
    for (auto& [k, v] : anchor.items()) out[k] = v;
  } else {
    anchor["records"] = out["records"];
    anchor["dataset"] = dataset;
    anchor["schema_hash"] = schema.hash();
    anchor["provenance"] = out["provenance"];
    std::ofstream f(anchor_out);
    if (!f) { std::cerr << "graph-stats: cannot write --anchor-out " << anchor_out << "\n"; return 2; }
    f << anchor.dump(1) << "\n";
  }
  std::cout << out.dump(1) << "\n";
  phase(anchor_out.empty() ? "write (the main tier and the anchor tier)" : "write (the main tier; the anchor tier to --anchor-out)");
  return 0;
}

}  // namespace evalcpp
