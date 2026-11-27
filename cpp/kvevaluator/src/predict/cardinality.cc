// The cardinality layer (cardinality.h: each model's rule beside its declaration); the rule switches' fixtures are
// tests/unit/check_predict_rules.py.
#include "predict/cardinality.h"
#include "ops/kv_contract.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <limits>
#include <set>
#include <stdexcept>
#include <tuple>

namespace evalcpp::predict {

namespace {

constexpr double kDay = 86400000.0;

bool is_ref(const json& v) { return v.is_object() && v.size() == 1 && v.contains("ref"); }
std::string ref_name(const json& v) { return v["ref"].get<std::string>(); }

// a step's argument with params and lets replaced by their values; a ref stays a ref (the estimator sizes it)
json bound(const json& v, const Bindings& b) {
  if (v.is_object() && v.size() == 1) {
    const std::string& kind = v.begin().key();
    const json& arg = v.begin().value();
    if (kind == "ref") return v;
    if (kind == "param") return to_json(b.params.at(arg.get<std::string>()));
    if (kind == "let") return to_json(b.lets.at(arg.get<std::string>()));
    if (kind == "lit") return arg;
  }
  if (v.is_array()) {
    json out = json::array();
    for (auto& x : v) out.push_back(bound(x, b));
    return out;
  }
  if (v.is_object()) {
    json out = json::object();
    for (auto it = v.begin(); it != v.end(); ++it) out[it.key()] = bound(it.value(), b);
    return out;
  }
  return v;
}

// a label argument: "Post" or ["Post", "Comment"]
std::vector<std::string> label_list(const json& v) {
  std::vector<std::string> out;
  if (v.is_string()) out.push_back(v.get<std::string>());
  else if (v.is_array())
    for (auto& x : v) out.push_back(x.get<std::string>());
  return out;
}

double type_count_of(const DatasetStats& st, const json& labels) {
  return labels.is_array() ? st.type_count(label_list(labels)) : labels.is_string() ? st.type_count(labels.get<std::string>()) : 0;
}

bool has(const json& a, const char* k) { return a.contains(k) && !a[k].is_null(); }
std::string str_or(const json& a, const char* k, const std::string& dflt) {
  return a.contains(k) && a[k].is_string() ? a[k].get<std::string>() : (a.contains(k) && !a[k].is_null() ? "" : dflt);
}
// `a.get(k) or dflt` for a number
double num_or(const json& a, const char* k, double dflt) {
  if (!has(a, k)) return dflt;
  double x = a[k].get<double>();
  return x != 0 ? x : dflt;
}

// Python's round(x, 3): rounds the exact binary value to 3 decimals (printf does the same), then reads it back
double round3(double x) {
  char buf[64];
  std::snprintf(buf, sizeof buf, "%.3f", x);
  return std::strtod(buf, nullptr);
}

// E[GETs] to resolve an unknown message type: labels tried in order
std::vector<std::string> step_labels(const json& labels, const char* op) {
  if (!labels.is_array() || labels.empty())
    throw std::runtime_error(std::string("predict: a ") + op + " step names no candidate labels: refused");
  return label_list(labels);
}

double expected_probes(const DatasetStats& st, const json& labels) {
  std::vector<std::string> labs = step_labels(labels, "candidate-vid read");
  std::vector<double> c;
  double tot = 0;
  for (auto& l : labs) { c.push_back(std::max(0.0, st.type_count(l))); tot += c.back(); }
  if (tot == 0) tot = 1;
  double s = 0;
  for (size_t i = 0; i < c.size(); i++) s += double(i + 1) * c[i] / tot;
  return round3(s);
}

double miss_fraction(const DatasetStats& st, const json& labels) {
  std::vector<std::string> labs = step_labels(labels, "props read");
  std::vector<double> c;
  double tot = 0;
  for (auto& l : labs) { c.push_back(std::max(0.0, st.type_count(l))); tot += c.back(); }
  if (tot == 0) tot = 1;
  return round3(1.0 - c[0] / tot);
}

struct Ctx {
  std::string anchor, anchor_out;    // the bound anchor's vid and its step
  std::map<std::string, std::string> label_share;   // hops far_label_consumer scaled: step out-name -> the label
  std::map<std::string, std::string> anchor_hops;   // the anchor's own hops: step out-name -> "etype|dir" (a recency key's)
  std::set<std::string> size_biased; // estimate_hints.size_biased: edge types whose hop sources were reached through them
  std::set<std::string> root_ratio;  // estimate_hints.chase_root_ratio: "etype|dir" a hop after a chase reads its ratio for
  std::map<std::string, std::string> via;      // step -> how its set was reached ("knows|both", ...)
  std::set<std::string> thinned;               // steps whose set passed a predicate filter since it was reached
  std::map<std::string, double> universe;      // step -> the type population its set is drawn from
};

struct Walker {
  const DatasetStats& st;
  const Rules& rules;
  Ctx ctx;
  std::map<std::string, double> est;
  std::map<std::string, std::pair<std::string, json>> steps;   // name -> (op, bound arguments), the steps walked so far
  std::vector<std::tuple<std::string, std::string, const json*>> plan_steps;   // every step: name, op, bound arguments
  // a domain: how many distinct vertices a field's values can be, and the labels they belong to (empty: not known)
  struct Dom {
    double n;
    std::set<std::string> labels;
  };
  // field_domain at a concat or join, by (step, field): a step's domain is fixed once it is walked; a walk the depth
  // limit cut short (dom_cut) is not kept, so no result depends on which caller came first
  mutable std::map<std::pair<std::string, std::string>, std::optional<Dom>> dom_cache;
  mutable bool dom_cut = false;

  double sz(const json& v) const {
    if (is_ref(v)) {
      auto it = est.find(ref_name(v));
      if (it == est.end()) throw std::runtime_error("ref to a step not estimated: " + ref_name(v));
      return it->second;
    }
    if (v.is_array()) return double(v.size());
    return 1;
  }
  double sz_arg(const json& a, const char* k) const { return sz(a.contains(k) ? a[k] : json()); }
  // whether two sets share no step among their sources (each set's own step included): drawn apart
  bool apart_sets(const json& x, const json& y) const {
    auto ax = ancestors(x), ay = ancestors(y);
    for (auto& n : ax)
      if (ay.count(n)) return false;
    return true;
  }
  // whether a set's items are distinct vertices: made by a dedup, a label or index read or a closure, back through the
  // ops that only drop items (filter, filter_in, top_k, exclude, and intersect, which keeps its items' repeats)
  bool distinct_set(json cur) const {
    static const std::map<std::string, std::string> drop = {
        {"filter", "rows"}, {"filter_in", "rows"}, {"top_k", "rows"}, {"exclude", "items"}, {"intersect", "items"}};
    for (int i = 0; i < 32 && is_ref(cur); i++) {
      auto it = steps.find(ref_name(cur));
      if (it == steps.end()) return false;
      const std::string& op = it->second.first;
      if (op == "dedup" || op == "read_vertices_by_label" || op == "read_index_eq" || op == "read_index_range" ||
          op == "read_closure")
        return true;
      auto k = drop.find(op);
      if (k == drop.end() || !it->second.second.contains(k->second)) return false;
      cur = it->second.second[k->second];
    }
    return false;
  }
  // the hop (a read_neighbors step) a set came from, back through the ops that keep its rows ("" when none)
  std::string producer_hop(json cur) const {
    static const std::map<std::string, std::string> keep = {{"dedup", "items"}, {"exclude", "items"}, {"pluck", "rows"},
                                                            {"rename", "rows"}, {"filter", "rows"}, {"filter_in", "rows"},
                                                            {"top_k", "rows"}};
    for (int i = 0; i < 16 && is_ref(cur); i++) {
      auto it = steps.find(ref_name(cur));
      if (it == steps.end()) return "";
      const std::string& op = it->second.first;
      const json& sa = it->second.second;
      if (op == "read_neighbors") return ref_name(cur);
      auto k = keep.find(op);
      if (k == keep.end() || (op == "pluck" && sa.value("field", json()) == "src") || !sa.contains(k->second)) return "";
      cur = sa[k->second];
    }
    return "";
  }

  // field_domain at a hop: the vertices on the side `field` names ("dst" or "": the far end; "src": the sources'),
  // those with such an edge (distinct_src / distinct_dst), at most the side's labels' population; and that side's labels
  std::optional<Dom> hop_domain(const json& sa, const std::string& field) const {
    std::string et = str_or(sa, "etype", ""), dir = kv::dir_of(sa);
    bool far = field.empty() || field == "dst";
    if (!far && field != "src") return std::nullopt;
    // population_count(et, "out") is the edges' source side, "in" their destination side
    double pop_src = st.population_count(et, "out"), pop_dst = st.population_count(et, "in");
    double src_side = std::min(pop_src, st.participant_count(et, "out"));
    double dst_side = std::min(pop_dst, st.participant_count(et, "in"));
    // both directions: either side's vertices, at most both sides' population (one population when the two sides are
    // the same labels, e.g. knows)
    double both = std::min(pop_src == pop_dst ? pop_src : pop_src + pop_dst, src_side + dst_side);
    bool src_end = dir != "both" && (dir == "out") != far;   // the field names the edges' source end
    double n = dir == "both" ? both : (src_end ? src_side : dst_side);
    if (n <= 0) return std::nullopt;
    Dom d{n, {}};
    for (const char* end : {"out", "in"})
      if (dir == "both" || (std::string(end) == "out") == src_end)
        for (auto& l : st.side_labels(et, end)) d.labels.insert(l);
    return d;
  }
  // field_domain at a label read: its labels' count, for its vertices themselves
  std::optional<Dom> label_domain(const json& sa, const std::string& field) const {
    if (!field.empty() && field != "_vid") return std::nullopt;
    json l = sa.contains("labels") ? sa["labels"] : (sa.contains("label") ? sa["label"] : json());
    std::vector<std::string> ls;
    if (l.is_string()) ls.push_back(l.get<std::string>());
    else if (l.is_array()) ls = label_list(l);
    double n = ls.empty() ? 0 : st.type_count(ls);
    if (n <= 0) return std::nullopt;
    return Dom{n, std::set<std::string>(ls.begin(), ls.end())};
  }

  // how many distinct vertices the values of `field` in a set can be: the population of the side of the hop that made
  // them (a hop's "dst": its far end; "src": its sources' side), or the label count of a label read. Walked back
  // through the ops that keep the field (a rename maps it to its old name, a pluck turns items back into its field, a
  // join takes the side that holds it); "" names a set of items. nullopt when the walk finds neither
  std::optional<Dom> field_domain(json cur, std::string field, int depth = 0) const {
    // concat_domain: a concat chain of any length up to 64 (JOB unions one hop per edge type, ~11 deep); off: 8
    int max_depth = rules.concat_domain ? 64 : 8;
    if (depth >= max_depth) {
      dom_cut = true;
      return std::nullopt;
    }
    for (int i = 0; i < 32 && is_ref(cur); i++) {
      auto it = steps.find(ref_name(cur));
      if (it == steps.end()) return std::nullopt;
      const std::string& op = it->second.first;
      const json& sa = it->second.second;
      if (op == "read_neighbors") return hop_domain(sa, field);
      if (op == "read_vertices_by_label" || op == "read_index_eq" || op == "read_index_range") return label_domain(sa, field);
      if (op == "concat" || op == "join" || op == "join_multi" || op == "left_join")
        return cached_two_side_domain(op, sa, ref_name(cur), field, depth);
      if (!field_step_back(op, sa, cur, field)) return std::nullopt;
    }
    return std::nullopt;
  }
  // two_side_domain, kept per (step, field): a side shared by two concats is walked once. A result is kept, found or
  // not, unless the depth limit cut the walk below it
  std::optional<Dom> cached_two_side_domain(const std::string& op, const json& sa, const std::string& step,
                                            const std::string& field, int depth) const {
    auto key = std::make_pair(step, field);
    if (auto c = dom_cache.find(key); c != dom_cache.end()) return c->second;
    bool cut_before = dom_cut;
    dom_cut = false;
    auto d = two_side_domain(op, sa, field, depth);
    if (!dom_cut) dom_cache[key] = d;
    dom_cut = dom_cut || cut_before;
    return d;
  }
  // field_domain at a concat (the sum of its sides' domains: they may hold other labels; concat_domain: at most the
  // population of their labels together, when both sides name theirs) or a join (the left side's rows carry their
  // fields; a field only the right side holds is found there)
  std::optional<Dom> two_side_domain(const std::string& op, const json& sa, const std::string& field, int depth) const {
    if (op == "concat") {
      auto x = field_domain(sa.value("a", json()), field, depth + 1), y = field_domain(sa.value("b", json()), field, depth + 1);
      if (!x || !y) return std::nullopt;
      Dom d{x->n + y->n, {}};
      if (!x->labels.empty() && !y->labels.empty()) {
        d.labels = x->labels;
        d.labels.insert(y->labels.begin(), y->labels.end());
        if (rules.concat_domain)
          d.n = std::min(d.n, st.type_count(std::vector<std::string>(d.labels.begin(), d.labels.end())));
      }
      return d;
    }
    bool cut_before = dom_cut;
    dom_cut = false;
    auto x = field_domain(sa.value("left", json()), field, depth + 1);
    if (x || dom_cut) {   // found, or the left side's walk was cut: its field is not known to be absent there
      dom_cut = dom_cut || cut_before;
      return x;
    }
    dom_cut = cut_before;
    return field_domain(sa.value("right", json()), field, depth + 1);
  }
  // the name a rename's output field had in its input (the field itself when the rename leaves it)
  static std::string renamed_from(const json& sa, const std::string& field) {
    const json names = sa.value("names", json::object());   // a named copy: items() of a temporary dangles
    for (auto& [old, now] : names.items())
      if (now.is_string() && now.get<std::string>() == field) return old;
    return field;
  }
  // one step back for field_domain through an op that keeps the field's values: `cur` and `field` become the step's
  // input and the field's name there (a rename maps it to its old name; a pluck turns items back into its field; an
  // annotate's rows are its items' `_vid`); false when the op makes the field (derive, flag_in) or is no such op
  bool field_step_back(const std::string& op, const json& sa, json& cur, std::string& field) const {
    static const std::map<std::string, std::string> keep = {
        {"dedup", "items"}, {"exclude", "items"}, {"dedup_rows", "rows"}, {"filter", "rows"}, {"filter_in", "rows"},
        {"flag_in", "rows"}, {"top_k", "rows"}, {"derive", "rows"}, {"project", "rows"},
        {"read_filter_by_label_probe", "vids"}, {"read_filter_by_label_index", "vids"}};
    if (op == "rename") {
      field = renamed_from(sa, field);
      cur = sa.value("rows", json());
      return true;
    }
    if (op == "pluck" || op == "annotate") {   // items <-> rows: a pluck's field; an annotate's rows hold `_vid`
      if (!field.empty() && !(op == "annotate" && field == "_vid")) return false;
      field = op == "pluck" ? str_or(sa, "field", "") : "";
      cur = sa.value(op == "pluck" ? "rows" : "items", json());
      return true;
    }
    if ((op == "derive" || op == "flag_in") && str_or(sa, "into", "") == field) return false;   // the step's own column
    auto k = keep.find(op);
    if (k == keep.end() || !sa.contains(k->second)) return false;
    cur = sa[k->second];
    return true;
  }

  // the steps a step's set is made from (every ref in its arguments, followed back), the step itself included
  std::set<std::string> ancestors(const json& ref) const {
    std::set<std::string> seen;
    std::vector<std::string> todo;
    if (is_ref(ref)) todo.push_back(ref_name(ref));
    while (!todo.empty() && seen.size() < 4096) {
      std::string n = todo.back();
      todo.pop_back();
      if (!seen.insert(n).second) continue;
      auto it = steps.find(n);
      if (it == steps.end()) continue;
      std::vector<const json*> stack = {&it->second.second};
      while (!stack.empty()) {
        const json* v = stack.back();
        stack.pop_back();
        if (is_ref(*v)) todo.push_back(ref_name(*v));
        else if (v->is_object() || v->is_array())
          for (auto& x : *v) stack.push_back(&x);
      }
    }
    return seen;
  }

  // whether a set's vertices were reached through edge type `et`: walked back through the ops that keep its rows to the
  // hop or closure that made them (a pluck of a hop's "src" goes on to that hop's sources; a concat: either side)
  bool reached_through(json cur, const std::string& et, int depth = 0) const {
    static const std::map<std::string, std::string> keep = {
        {"dedup", "items"}, {"exclude", "items"}, {"dedup_rows", "rows"}, {"rename", "rows"}, {"filter", "rows"},
        {"filter_in", "rows"}, {"top_k", "rows"}, {"project", "rows"}, {"derive", "rows"}};
    for (int i = 0; i < 32 && is_ref(cur) && depth < 8; i++) {
      auto it = steps.find(ref_name(cur));
      if (it == steps.end()) return false;
      const std::string& op = it->second.first;
      const json& sa = it->second.second;
      if (op == "read_neighbors" || op == "read_closure") return str_or(sa, "etype", "") == et;
      if (op == "pluck") {
        if (str_or(sa, "field", "") == "src") {   // a hop's sources: how they were reached
          std::string h = producer_hop(sa.value("rows", json()));
          if (h.empty()) return false;
          cur = steps.at(h).second.value("srcs", json());
          continue;
        }
        cur = sa.value("rows", json());
        continue;
      }
      if (op == "annotate") {
        cur = sa.value("items", json());
        continue;
      }
      if (op == "concat")
        return reached_through(sa.value("a", json()), et, depth + 1) || reached_through(sa.value("b", json()), et, depth + 1);
      auto k = keep.find(op);
      if (k == keep.end() || !sa.contains(k->second)) return false;
      cur = sa[k->second];
    }
    return false;
  }

  // the dated vertices a date rule reads: the step's own labels and date prop, else the file's declared date axis
  std::vector<std::string> axis_labels(const json& a) const {
    if (a.contains("labels") && a["labels"].is_array() && !a["labels"].empty()) return label_list(a["labels"]);
    return st.date_axis_labels();
  }
  std::string axis_prop(const json& a) const {
    return a.contains("prop") && a["prop"].is_string() ? a["prop"].get<std::string>() : st.date_axis_prop();
  }
  double axis_span(const json& a, const std::string& rule) const { return st.date_span(axis_labels(a), axis_prop(a), rule); }
  // one step: its rows and auxiliaries, or false when the op has no rule
  bool op(const std::string& out, const std::string& op, const json& a, double& e, StepCard& c);
  // the share of rows a filter keeps, from what it tests (Rules::notnull_label, Rules::date_filter); nullopt: no rule
  // reads this predicate, and the filter keeps half its rows
  std::optional<double> filter_share(const json& a) const;
  // entry_value_degree: a hop whose srcs is an equality index read on its own source-side label, with a value: that
  // value's edges (top_degrees). nullopt otherwise (the hop keeps the other rules)
  std::optional<double> entry_degree(const json& a, const std::string& et, const std::string& dir) {
    if (!rules.entry_value_degree || !is_ref(a["srcs"]) || dir == "both") return std::nullopt;
    auto it = std::find_if(plan_steps.begin(), plan_steps.end(),
                           [&](const auto& t) { return std::get<0>(t) == ref_name(a["srcs"]); });
    if (it == plan_steps.end() || std::get<1>(*it) != "read_index_eq") return std::nullopt;
    const json& ix = *std::get<2>(*it);
    if (!ix.contains("value") || !ix.contains("label") || !ix["label"].is_string()) return std::nullopt;
    std::string lab = ix["label"].get<std::string>();
    if (!hop_side_labels(et, dir, true).count(lab)) return std::nullopt;   // the label owns no such list
    return st.entry_degree(lab, et, dir, ix.at("prop").get<std::string>(), ix["value"]);
  }
  // the one label every consumer of step `name` reads its rows as (far_label_consumer): a hop from them reaches edges
  // only from the labels at its edge type's source side (a vertex of another label has none); a filter_in testing a
  // hop's dst against them, that hop's far labels; a join's side reads no label; the ops that keep rows pass the
  // question on. nullopt when a consumer reads them any other way (a props read keeps every row, so it is one), or
  // when no consumer names a label
  std::optional<std::string> consumer_label(const std::string& name, int depth = 0) const;
  // the labels at one side of a hop over `etype` in `dir` (sizes.content's esrc / edst keys): `near` the side it leaves
  // from, else the side it reaches; "both" covers both ends
  std::set<std::string> hop_side_labels(const std::string& etype, const std::string& dir, bool near) const;
  // a filter_in testing the dst of `rows` (a hop) against a set: the labels that hop reaches; nullopt otherwise
  std::optional<std::set<std::string>> allowed_labels(const json& a) const;
  // the labels one consumer (step `sn`, `op`, `a`) reads step `name`'s rows as: empty for a join's side; nullopt when
  // it reads them another way
  std::optional<std::set<std::string>> read_as(const std::string& name, const std::string& sn, const std::string& op,
                                               const json& a, int depth) const;
  // an op that keeps rows (dedup, pluck, filter, ...) reading them: the label its own consumers read; nullopt otherwise
  std::optional<std::set<std::string>> passed_on(const std::string& name, const std::string& sn, const std::string& op,
                                                 const json& a, int depth) const;
  // where a set's rows come from, for a range filter on `field`: the labels it holds with their weights (a label list
  // by type counts; a concat's two sides by their estimated rows; an unlabelled hop's far end by its labels' share of
  // the edges), and the edge type of the hop that made it (its rows carry the edge's properties). Empty when the walk
  // back finds neither, or when the field may come from either side of a join or is a column the plan computed
  struct SetSource {
    std::vector<std::pair<std::string, double>> labels;
    std::string etype;
  };
  SetSource set_source(json cur, const std::string& field, int depth = 0) const;
  // the date window [lo, hi) a sorted-index read applied to the rows' `field` before them (read_index_range on that
  // property, reached back through the ops that keep rows, a props read's vids and a label scan's `keep`); nullopt
  // when none did
  std::optional<std::pair<double, double>> prior_window(json cur, const std::string& field) const;
  // a date window's share of label `l`'s rows: its vertices in the window over the label's, or, when an index read
  // already applied `prior` to the rows, those in both windows over those in `prior`; nullopt without per-day counts
  std::optional<double> date_share(const std::string& l, const std::string& f, std::pair<double, double> w,
                                   const std::optional<std::pair<double, double>>& prior) const;
  // whether a set's rows can hold `field` as a stored property (one of its labels declares it, or its edge type)
  bool holds(const SetSource& s, const std::string& field) const;
  // an int range filter's share on one property: its elements in [lo, hi) (int_values) over `total`, the rows of the
  // label or edge type; 0 when no element holds the property
  double int_share(const std::string& key, std::pair<double, double> lohi, double total) const;
  StepLookup lookup() const {
    return [this](const std::string& n) -> std::optional<std::pair<std::string, const json*>> {
      auto it = steps.find(n);
      if (it == steps.end()) return std::nullopt;
      return std::make_pair(it->second.first, &it->second.second);
    };
  }
};

bool Walker::op(const std::string& out, const std::string& op, const json& a, double& e, StepCard& c) {
  auto A = [&](const std::string& k, double v) { c.aux[k] = v; };   // an auxiliary no cost rule reads
  if (op == "read_vertex_by_id" || op == "read_anchor_props" || op == "count" || op == "as_row") {
    bool cand = op == "read_anchor_props" || (op == "read_vertex_by_id" && a.contains("vid") && a["vid"].is_array());
    if (cand) {
      json labels = has(a, "labels") && !a["labels"].empty() ? a["labels"] : (a.contains("label") ? a["label"] : json());
      A("probes", expected_probes(st, labels));
    }
    e = 1;
    return true;
  }
  if (op == "concat") {
    e = sz_arg(a, "a") + sz_arg(a, "b");
    if (rules.concat_keeps_via && is_ref(a["a"]) && is_ref(a["b"])) {
      auto va = ctx.via.find(ref_name(a["a"])), vb = ctx.via.find(ref_name(a["b"]));
      if (va != ctx.via.end() && vb != ctx.via.end() && va->second == vb->second) {
        ctx.via[out] = va->second;
        if (ctx.thinned.count(ref_name(a["a"])) || ctx.thinned.count(ref_name(a["b"]))) ctx.thinned.insert(out);
      }
    }
    return true;
  }
  if (op == "top_k") {
    double base = sz_arg(a, "rows");
    e = has(a, "k") ? std::min(base, a["k"].get<double>()) : base;
    return true;
  }
  if (op == "read_edge_exists") {
    const json& reads = a.at("reads");
    if (!reads.is_array() || reads.empty()) throw std::runtime_error("read_edge_exists: the step names no reads");
    double nrows = sz_arg(a, "rows");
    if (nrows == 0) {   // no candidate: every count 0
      c.nvia = c.nvnbrs = c.nothers = 0.0;
      c.eown.assign(reads.size(), 0.0);
      c.eprobe.assign(reads.size(), 0.0);
      for (const char* k : {"eprobes", "eowners", "noedges", "ehits"}) A(k, 0);
      e = 0;
      return true;
    }
    bool via = has(a, "via_srcs");
    double nvia = 0, nothers;
    if (via) {
      nvia = sz(a["via_srcs"]);
      if (!has(a, "via_etype")) throw std::runtime_error("predict: a probe with via_srcs names no via_etype: refused");
      nothers = std::max(1.0, py_round(nvia * st.mean_degree(a["via_etype"].get<std::string>(), "out")));
    } else {
      nothers = std::max(1.0, sz_arg(a, "others"));
    }
    std::string etype = a.at("etype").get<std::string>();
    double targets = st.population_count(etype, "in");
    double d = targets != 0 ? std::min(1.0, st.population_degree(etype, "out") / targets) : 0.0;
    double p = 1.0 - std::pow(1.0 - d, nothers);
    double miss = 1.0, eprobes = 0, own_out = 0, own_in = 0;
    std::set<bool> halves;
    for (size_t i = 0; i < reads.size(); i++) {
      const json& r = reads[i];
      std::string dir = r.at("dir").get<std::string>(), at = r.at("at").get<std::string>();
      bool unless = r.value("unless_found", false);
      double todo = unless ? nrows * miss : nrows;
      double n_own = at == "cand" ? todo : (unless ? nothers * std::min(1.0, todo) : nothers);
      c.eown.push_back(n_own);
      c.eprobe.push_back(todo * nothers);   // the (candidate, anchor) pairs this read checks
      (dir == "out" ? own_out : own_in) += n_own;
      eprobes += todo * nothers;
      bool half = (dir == "out") == (at == "anchor");
      if (!halves.count(half)) { halves.insert(half); miss *= 1.0 - p; }
    }
    double noedges = own_out * st.mean_degree(etype, "out") + own_in * st.mean_degree(etype, "in");
    c.nvia = nvia;
    c.nvnbrs = via ? nothers : 0;
    c.nothers = nothers;
    A("eprobes", eprobes);
    A("eowners", own_out + own_in);
    A("noedges", py_round(noedges));
    A("ehits", 0);
    e = nrows;
    return true;
  }
  // the passthrough ops: one row per input row
  static const std::map<std::string, std::string> passthrough = {
      {"read_vertex_props", "vids"}, {"read_chase", "srcs"}, {"all_tag_props", "vids"}, {"pluck", "rows"},
      {"project", "rows"}, {"rename", "rows"}, {"derive", "rows"}, {"annotate", "items"}, {"flag_in", "rows"},
      {"attach", "rows"}, {"nest", "rows"}, {"zip_rows", "left"}, {"join", "left"}, {"left_join", "left"}};
  if (auto pt = passthrough.find(op); pt != passthrough.end()) {
    double v = sz(a.at(pt->second));
    if (op == "read_vertex_props") {
      if (rules.props_distinct && is_ref(a.at("vids"))) {
        auto via = ctx.via.find(ref_name(a["vids"]));
        if (via != ctx.via.end())
          if (auto d = st.distinct_given_via(via->second, v)) v = py_round(std::min(v, *d));
      }
      c.nv = v;
      if (a.contains("label") && a["label"].is_array()) A("miss", py_round(v * miss_fraction(st, a["label"])));
    } else if (op == "read_chase") {
      double D = 2;   // a chase over an edge type that is no forest
      if (auto m = st.forest_depth(str_or(a, "etype", ""), true)) D = *m;
      c.steps = v * (D + 1);
      c.edges = v * D;
      ctx.via[out] = "chase-root|" + str_or(a, "etype", "None");
    }
    e = v;
    return true;
  }
  if (op == "read_neighbors") {
    double es = sz(a.at("srcs"));
    std::string dir = kv::dir_of(a), et = a.at("etype").get<std::string>();
    bool from_anchor = (is_ref(a["srcs"]) && ref_name(a["srcs"]) == ctx.anchor_out) || !is_ref(a["srcs"]);
    bool anchored = !ctx.anchor_out.empty();
    double o;
    bool literal = !is_ref(a["srcs"]) && a["srcs"].is_array() && a["srcs"].size() == 1 && a["srcs"][0].is_string();
    if (from_anchor && es <= 1) {
      // the anchor's own hop, over any edge type: its own degree (the anchor tier), else the participants' mean; a vid
      // the plan names: that vertex's own degree
      std::optional<double> exact;
      if (is_ref(a["srcs"]) && anchored) exact = st.anchor_degree(ctx.anchor, et, dir);
      else if (literal) exact = st.anchor_degree(a["srcs"][0].get<std::string>(), et, dir);
      o = exact ? *exact : es * st.mean_degree(et, dir);
      if (is_ref(a["srcs"]) && anchored) ctx.anchor_hops[out] = et + "|" + dir;
    } else if (auto ed = entry_degree(a, et, dir)) {
      o = *ed;   // entry_value_degree: the indexed value's own edges
    } else if (ctx.size_biased.count(et) && (!rules.size_biased_via || reached_through(a["srcs"], et))) {
      o = es * st.neighbor_degree(et, dir);   // sources reached through this edge type: a neighbour's degree
    } else if (es <= 1) {
      o = es * st.mean_degree(et, dir);
    } else {
      o = es * st.population_degree(et, dir);
      if (is_ref(a["srcs"])) {
        auto via = ctx.via.find(ref_name(a["srcs"]));
        if (ctx.thinned.count(ref_name(a["srcs"]))) via = ctx.via.end();   // past a filter: the population mean
        std::optional<double> given;
        if (via != ctx.via.end() && rules.frontier_inclusion) given = st.mean_given_via(via->second, et + "|" + dir, es);
        if (given)
          o = es * *given;
        else if (via != ctx.via.end() &&
                 (via->second.rfind("chase-root|", 0) != 0 || ctx.root_ratio.count(et + "|" + dir)))
          if (auto ratio = st.cond_ratio(via->second, et, dir)) {
            double f = std::min(1.0, es / std::max(st.population_count(et, dir), 1.0));
            o *= 1.0 + (*ratio - 1.0) * (1.0 - f);
          }
      }
    }
    // far_label_consumer: the rows every consumer reads as one label keep that label's share of the far end's edges
    if (rules.far_label_consumer && !(from_anchor && es <= 1))
      if (auto l = consumer_label(out)) {
        auto share = st.far_label_share(et, dir, *l);
        if (share && *share > 0 && *share < 1) {
          o *= *share;
          ctx.label_share[out] = *l;   // notnull_label does not apply it again
        }
      }
    // hop_edge_cap: a hop from distinct sources returns each edge at most once: at most its type's edges (x2 for both
    // directions). Repeated sources are no bound: sources reached through the type repeat its high-degree vertices
    if (rules.hop_edge_cap && distinct_set(a["srcs"])) o = std::min(o, st.edges(et) * (dir == "both" ? 2.0 : 1.0));
    std::string rdir = dir == "out" ? "in" : dir == "in" ? "out" : dir;
    ctx.via[out] = et + "|" + rdir;
    c.sd = es;
    e = py_round(o);
    return true;
  }
  if (op == "read_index_eq") {
    double tc = st.type_count(a.at("label").get<std::string>());
    ctx.universe[out] = std::max(1.0, tc);
    std::string lab = a["label"].get<std::string>(), prop = a.at("prop").get<std::string>();
    if (rules.index_value_rows && a.contains("value"))
      e = std::max(1.0, py_round(st.value_rows(lab, prop, a["value"], "an equality index read (read_index_eq)")));
    else
      e = std::max(1.0, py_round(tc / std::max(st.ndv(lab, prop), 1.0)));
    return true;
  }
  if (op == "read_index_range") {
    double span = axis_span(a, "a date-index read");
    double wd = has(a, "lo") && has(a, "hi") ? std::max(1.0, (a["hi"].get<double>() - a["lo"].get<double>()) / kDay) : span;
    double frac = std::min(1.0, wd / std::max(span, 1.0));
    double days = std::min(py_round(wd), span);
    double tc = type_count_of(st, a.at("labels"));
    ctx.universe[out] = std::max(1.0, tc);
    A("days", days);
    e = py_round(tc * frac);
    if (rules.date_histogram && has(a, "lo") && has(a, "hi") && a.contains("prop") && a["prop"].is_string()) {
      double n = 0;
      bool all = true;
      for (auto& l : label_list(a["labels"])) {
        auto w = st.in_window(l, a["prop"].get<std::string>(), a["lo"].get<double>(), a["hi"].get<double>());
        if (!w) { all = false; break; }
        n += *w;
      }
      if (all) e = py_round(n);
    }
    return true;
  }
  if (op == "read_vertices_by_label") {
    double tc = type_count_of(st, a.at("label"));
    e = has(a, "keep") ? std::min(tc, sz(a["keep"])) : tc;
    return true;
  }
  if (op == "read_filter_by_label_probe" || op == "read_filter_by_label_index") {
    double tc = type_count_of(st, a.at("label"));
    double frac = tc / std::max(st.total_vertices, 1.0);
    double n = sz(a.at("vids"));
    A("members", tc);
    c.nprobe = n;
    e = std::max(0.0, py_round(n * frac));
    return true;
  }
  if (op == "intersect") {
    double na = sz(a.at("items")), nb = sz(a.at("other"));
    std::vector<double> ns;
    for (const char* k : {"items", "other"})
      if (is_ref(a[k]) && ctx.universe.count(ref_name(a[k]))) ns.push_back(ctx.universe[ref_name(a[k])]);
    if (!ns.empty())
      e = std::min({na, nb, py_round(na * nb / *std::min_element(ns.begin(), ns.end()))});
    else
      e = std::min(na, nb);
    return true;
  }
  if (op == "exclude") {
    double items = sz(a.at("items")), rem = sz(a.at("remove"));
    e = std::max(0.0, items - rem);   // an estimate: every removed value among the items, the items distinct
    // exclude_share: a removed set that shares no step with the items (neither made from the other, nor both from one
    // source: a BFS level and the levels before it are correlated) is taken as drawn apart from them over their
    // labels: an item is in it with the chance (its removed vertices on the labels the two share) / (the items'
    // labels' population), its part on those labels pro rata by type count. Subtracting its size assumed it held only
    // items, and gave 0 once it was the larger
    if (rules.exclude_share && rem > 0 && is_ref(a["items"]) && is_ref(a["remove"]) &&
        apart_sets(a["items"], a["remove"])) {
      auto di = field_domain(a["items"], ""), dr = field_domain(a["remove"], "");
      if (di && dr && !di->labels.empty() && !dr->labels.empty()) {
        std::vector<std::string> mine(di->labels.begin(), di->labels.end()), theirs(dr->labels.begin(), dr->labels.end());
        std::vector<std::string> shared;
        for (auto& l : mine)
          if (dr->labels.count(l)) shared.push_back(l);
        double n_mine = st.type_count(mine), n_theirs = st.type_count(theirs);
        if (n_mine > 0 && n_theirs > 0) {
          double p = std::min(1.0, rem * st.type_count(shared) / n_theirs / n_mine);
          e = std::max(e, py_round(items * (1.0 - p)));
        }
      }
    }
    return true;
  }
  if (op == "read_top_k_by_index") {
    double k = num_or(a, "k", 1);
    double total = std::max(1.0, type_count_of(st, a.contains("labels") ? a["labels"] : json()));
    double ncand = has(a, "vids") ? sz(a["vids"]) : total;
    double span = axis_span(a, "the ranked date-index walk");
    double msg_days = st.populated_days(axis_labels(a), axis_prop(a), "the ranked date-index walk");
    std::optional<double> dback;
    std::vector<double> deciles;
    auto hop = has(a, "vids") && is_ref(a["vids"]) ? ctx.anchor_hops.find(ref_name(a["vids"])) : ctx.anchor_hops.end();
    if (hop != ctx.anchor_hops.end() && !has(a, "hi") && k <= 20) {   // the anchor's own neighbours, ranked by date
      int kk = k <= 1 ? 1 : k <= 10 ? 10 : 20;
      size_t bar = hop->second.find('|');
      std::string rkey = st.recency_key(hop->second.substr(0, bar), hop->second.substr(bar + 1),
                                        a.at("prop").get<std::string>());
      if (!ctx.anchor_out.empty()) dback = st.anchor_recency(rkey, ctx.anchor, kk);
      if (!dback) {
        dback = st.recency_mean(rkey, kk);
        deciles = st.recency_deciles(rkey, kk);
      }
    }
    // the candidates a hop reached (not the anchor's own, read by its recency above): a hop's far ends cluster in time
    // (a person's messages in bursts), so the walk crosses b times the even spread's entries (burstiness). No hop (a
    // label scan, no vids): the candidates are the index itself, spread evenly
    double b = 1.0;
    if (!dback && has(a, "vids")) {
      std::string hs = producer_hop(a["vids"]);
      if (!hs.empty()) {
        const json& ha = steps.at(hs).second;
        b = st.burstiness(ha.at("etype").get<std::string>(), kv::dir_of(ha), axis_prop(a),
                          "the ranked date-index walk");
      }
    }
    double crossed = std::min(total, py_round(b * k * total / std::max(ncand, 1.0)));
    double tokens = std::max(1.0, std::min(span, py_round(crossed * span / total)));
    if (dback) tokens = std::max(1.0, std::min(tokens, py_round(*dback)));
    double nmsg = std::max(st.type_count(axis_labels(a)), 1.0);
    std::set<std::string> dated_labels;   // the labels holding the walk's date prop (the index is the prop's, not a label's)
    const std::string suf = "|" + axis_prop(a);
    for (auto& key : st.content_keys())
      if (key.compare(0, 5, "prop|") == 0 && key.size() >= suf.size() &&
          key.compare(key.size() - suf.size(), suf.size(), suf) == 0) {
        size_t b = key.find('|', 5);
        dated_labels.insert(key.substr(5, b - 5));
      }
    double dated = 0;
    for (auto& l : dated_labels) dated += st.type_count(l);
    if (dated_labels.empty()) dated = nmsg;
    double dated_ratio = dated / nmsg, wentries;
    auto within = [&](double days) { return st.within_days(axis_labels(a), axis_prop(a), days, "the ranked date-index walk"); };
    if (dback) {
      if (!deciles.empty()) {
        double s = 0;
        for (double dd : deciles) s += within(std::min(dd, tokens));
        wentries = dated_ratio * s / double(deciles.size());
      } else {
        wentries = within(std::min(*dback, tokens)) * dated_ratio;
      }
      crossed = std::min(crossed, py_round(wentries));
    } else {
      wentries = nmsg * dated_ratio * tokens / std::max(msg_days, 1.0);
    }
    double hits = std::min(k, ncand), dayhits = hits;
    if (!dback) dayhits = std::min(ncand, std::max(hits, py_round(ncand * (tokens + 1) / std::max(msg_days, 1.0))));
    c.crossed = crossed;
    A("tokens", tokens);
    A("hits", hits);
    A("dayhits", dayhits);
    A("wentries", wentries);
    e = std::min(k, ncand);
    return true;
  }
  if (op == "read_closure") {
    double nsrc = sz(a.at("srcs"));
    std::optional<double> cap;
    if (has(a, "max_levels")) cap = a["max_levels"].get<double>();
    std::string et = str_or(a, "etype", "");
    double grow = 3.0;   // an edge type that is no forest and not declared size-biased
    if (auto m = st.forest_depth(et, true)) {
      grow = 1.0 + (cap ? std::min(*cap, *m) : *m);   // a forest: a subtree's mean size is 1 + the mean depth
    } else if (ctx.size_biased.count(et)) {
      std::string dir = kv::dir_of(a);
      double d = st.mean_degree(et, dir);
      grow = 1.0 + d + ((!cap || *cap >= 2) ? d * st.neighbor_degree(et, dir) : 0.0);
    }
    double csize = py_round(nsrc * grow);
    A("cnodes", csize);
    A("cedges", std::max(0.0, csize - nsrc));
    e = csize;
    return true;
  }
  if (op == "join_multi") {
    // each pair test (`where`; the key's membership) halves the estimate, as one filter step does (the rough rule below)
    e = std::max(sz(a.at("left")), sz(a.at("right")));
    for (bool test : {a.contains("where"), a.contains("where_in") || a.contains("where_not_in")})
      if (test) e = std::max(0.0, py_round(e * 0.5));
    return true;
  }
  if (op == "dedup" || op == "dedup_rows") {
    const json& in = a.contains("items") ? a["items"] : (a.contains("rows") ? a["rows"] : json());
    e = sz(in);
    if (rules.dedup_by_degree && op == "dedup" && is_ref(in)) {
      auto via = ctx.via.find(ref_name(in));
      if (via != ctx.via.end())
        if (auto d = st.distinct_given_via(via->second, e)) e = py_round(std::min(e, *d));
    }
    if (rules.dedup_domain_cap && op == "dedup" && is_ref(in))
      if (auto n = field_domain(in, "")) e = std::min(e, py_round(n->n));
    return true;
  }
  if (op == "gate") {
    double p = std::min(1.0, sz_arg(a, "when"));
    e = std::max(0.0, py_round(sz_arg(a, "items") * p));
    return true;
  }
  if (op == "filter" && (rules.notnull_label || rules.date_filter || rules.filter_value_share)) {
    if (auto f = filter_share(a)) {
      e = std::max(0.0, py_round(sz_arg(a, "rows") * *f));
      return true;
    }
  }
  if (op == "group_agg" && a.contains("by") && a["by"].is_array() && a["by"].empty()) {
    // an ungrouped aggregate: one row when any row comes in, none otherwise (ops/flow.cc group_agg: one row per
    // distinct key tuple)
    e = std::min(1.0, sz(a.at("rows")));
    return true;
  }
  // the share holds when the allowed set is drawn apart from the rows; one made from the rows themselves (a semi-join
  // back onto their own values, e.g. JOB's back sets: the rows' step, or the hop that made them, among the allowed
  // set's sources) holds their values far more often, and keeps half
  if (op == "filter_in" && rules.filter_in_share && a.contains("allowed") && a.contains("field") &&
      a["field"].is_string() && is_ref(a.value("rows", json())) && is_ref(a["allowed"])) {
    auto from_allowed = ancestors(a["allowed"]);
    std::string hop = producer_hop(a["rows"]);
    bool apart = !from_allowed.count(ref_name(a["rows"])) && (hop.empty() || !from_allowed.count(hop));
    if (auto n = field_domain(a["rows"], a["field"].get<std::string>()); apart && n && n->n > 0) {
      e = std::max(0.0, py_round(sz(a["rows"]) * std::min(1.0, sz(a["allowed"]) / n->n)));
      return true;
    }
  }
  if (op == "filter" || op == "filter_in" || op == "group_count" || op == "group_sum" || op == "group_top" ||
      op == "group_agg") {   // a keyed group_agg: the same rough rule
    double in = a.contains("rows") ? sz(a["rows"]) : sz_arg(a, "items");
    e = std::max(0.0, py_round(in * 0.5));
    // group_key_cap: a group step makes one row per distinct key, so at most the product of its keys' domains
    // (Walker::field_domain; a group_count with no key groups its items themselves), and one row at least when a row
    // comes in (half of one row rounds to 0)
    if (rules.group_key_cap && op.rfind("group_", 0) == 0) {
      const json src = a.contains("rows") ? a["rows"] : a.value("items", json());
      std::vector<std::string> keys;
      if (a.contains("by") && a["by"].is_string()) keys.push_back(a["by"].get<std::string>());
      else if (a.contains("by") && a["by"].is_array())
        for (auto& k : a["by"])
          if (k.is_string()) keys.push_back(k.get<std::string>());
      if (!a.contains("by")) keys.push_back("");
      double cap = 1;
      bool known = !keys.empty();
      for (auto& k : keys)
        if (auto d = field_domain(src, k)) cap *= d->n;
        else known = false;
      if (known) e = std::min(e, py_round(cap));
      if (in >= 1) e = std::max(e, 1.0);
    }
    return true;
  }
  return false;
}

}  // namespace

void notnull_fields(const json& pred, std::set<std::string>& out) {
  if (!pred.is_object()) return;
  if (pred.contains("notnull") && pred["notnull"].is_object() && pred["notnull"].contains("field") &&
      pred["notnull"]["field"].is_string())
    out.insert(pred["notnull"]["field"].get<std::string>());
  if (pred.contains("and") && pred["and"].is_array())
    for (auto& p : pred["and"]) notnull_fields(p, out);
}

std::string set_label(const json& arg, const StepLookup& step, std::set<std::string> required, std::string* props_step) {
  static const std::map<std::string, std::string> keep = {{"dedup", "items"}, {"exclude", "items"}, {"pluck", "rows"},
                                                          {"rename", "rows"}, {"filter", "rows"}, {"filter_in", "rows"},
                                                          {"top_k", "rows"}};
  json cur = arg;
  for (int hops = 0; hops < 16 && is_ref(cur); hops++) {
    std::string name = ref_name(cur);
    auto got = step(name);
    if (!got) return "";
    const std::string& op = got->first;
    const json& st = *got->second;
    if (op == "read_neighbors") {   // the hop's own one-label filter
      const json& l = st.contains("label") ? st["label"] : json();
      if (l.is_string()) return l.get<std::string>();
      if (l.is_array() && l.size() == 1 && l[0].is_string()) return l[0].get<std::string>();
      return "";
    }
    if (op == "read_vertex_props" && st.contains("vids")) {
      if (st.contains("label") && st["label"].is_string() && st.contains("props") && st["props"].is_array())
        for (auto& p : st["props"])
          if (p.is_string() && required.count(p.get<std::string>())) {
            if (props_step) *props_step = name;
            return st["label"].get<std::string>();
          }
      cur = st["vids"];
      continue;
    }
    if (op == "filter" && st.contains("pred")) notnull_fields(st["pred"], required);
    auto k = keep.find(op);
    if (k == keep.end() || (op == "pluck" && st.value("field", json()) == "src") || !st.contains(k->second)) return "";
    cur = st[k->second];
  }
  return "";
}

namespace {
// a bound value as a number: a number, or a string that is one (params arrive as strings); nullopt otherwise
std::optional<double> number(const json& v) {
  if (v.is_number()) return v.get<double>();
  if (v.is_string()) {
    const std::string& t = v.get_ref<const std::string&>();
    char* end = nullptr;
    double x = std::strtod(t.c_str(), &end);
    if (!t.empty() && end && *end == '\0') return x;
  }
  return std::nullopt;
}
}  // namespace

std::set<std::string> Walker::hop_side_labels(const std::string& etype, const std::string& dir, bool near) const {
  std::set<std::string> out;
  const json content = st.d.contains("sizes") ? st.d["sizes"].value("content", json::object()) : json::object();
  for (std::string side : {"esrc|", "edst|"}) {
    bool src_side = side == "esrc|";
    if (dir != "both" && src_side != ((dir == "out") == near)) continue;
    std::string pre = side + etype + "|";
    for (auto& [k, v] : content.items())
      if (k.rfind(pre, 0) == 0) out.insert(k.substr(pre.size()));
  }
  return out;
}

std::optional<std::set<std::string>> Walker::allowed_labels(const json& a) const {
  if (!a.contains("rows") || !is_ref(a["rows"]) || a.value("field", "") != "dst") return std::nullopt;
  for (auto& [rn, rop, rap] : plan_steps)   // the whole plan: the rows' step may come later than the hop asking
    if (rn == ref_name(a["rows"]) && rop == "read_neighbors")
      return hop_side_labels(rap->value("etype", ""), kv::dir_of(*rap), false);
  return std::nullopt;
}

// whether `x` (an argument, at any depth) refers to step `name`
static bool refers_to(const json& x, const std::string& name) {
  if (is_ref(x)) return ref_name(x) == name;
  if (x.is_object())
    for (auto& [k, v] : x.items())
      if (refers_to(v, name)) return true;
  if (x.is_array())
    for (auto& v : x)
      if (refers_to(v, name)) return true;
  return false;
}

// whether argument `arg` of a step is a plain reference to step `name`
static bool passes(const json& a, const char* arg, const std::string& name) {
  return a.contains(arg) && is_ref(a[arg]) && ref_name(a[arg]) == name;
}

std::optional<std::set<std::string>> Walker::read_as(const std::string& name, const std::string& sn,
                                                     const std::string& op, const json& a, int depth) const {
  if (op == "read_neighbors" && passes(a, "srcs", name))   // a hop reaches edges only from its source side's labels
    return hop_side_labels(a.value("etype", ""), kv::dir_of(a), true);
  if (op == "read_vertex_props" && rules.props_read_consumer && passes(a, "vids", name) && a.contains("label") &&
      a["label"].is_string())
    return std::set<std::string>{a["label"].get<std::string>()};   // the plan reads the rows as that label
  if ((op == "join" || op == "join_multi") && (passes(a, "left", name) || passes(a, "right", name)))
    return std::set<std::string>{};   // the side of a join: its keys only meet the other side's rows
  if (op == "filter_in" && passes(a, "allowed", name) && !refers_to(a.value("rows", json()), name))
    return allowed_labels(a);         // a membership list tested against a hop's dst
  return passed_on(name, sn, op, a, depth);
}

std::optional<std::set<std::string>> Walker::passed_on(const std::string& name, const std::string& sn,
                                                       const std::string& op, const json& a, int depth) const {
  static const std::map<std::string, std::string> keep = {{"dedup", "items"}, {"exclude", "items"}, {"pluck", "rows"},
                                                          {"rename", "rows"}, {"filter", "rows"}, {"filter_in", "rows"},
                                                          {"top_k", "rows"}};
  auto k = keep.find(op);
  if (k == keep.end() || !passes(a, k->second.c_str(), name) || depth >= 8 ||
      (op == "pluck" && a.value("field", json()) == "src"))
    return std::nullopt;              // a consumer that reads the rows another way (a props read, a count, a concat)
  auto l = consumer_label(sn, depth + 1);
  if (!l) return std::nullopt;
  return std::set<std::string>{*l};
}

std::optional<std::string> Walker::consumer_label(const std::string& name, int depth) const {
  std::set<std::string> labels;
  bool any = false;
  for (auto& [sn, op, ap] : plan_steps) {
    if (!refers_to(*ap, name)) continue;
    any = true;
    auto got = read_as(name, sn, op, *ap, depth);
    if (!got) return std::nullopt;
    labels.insert(got->begin(), got->end());
  }
  if (!any || labels.size() != 1) return std::nullopt;
  return *labels.begin();
}

std::optional<double> Walker::filter_share(const json& a) const {
  if (!a.contains("pred") || !is_ref(a.value("rows", json()))) return std::nullopt;
  // the predicate as conjuncts; each must be one these rules read, else none applies (the filter keeps half)
  std::vector<json> conj;
  std::vector<json> todo = {a["pred"]};
  while (!todo.empty()) {
    json p = todo.back();
    todo.pop_back();
    if (p.is_object() && p.size() == 1 && p.contains("and") && p["and"].is_array())
      for (auto& x : p["and"]) todo.push_back(x);
    else conj.push_back(p);
  }
  std::set<std::string> notnull;
  std::map<std::string, std::pair<double, double>> window;   // field -> [lo, hi), open sides at -inf / +inf
  std::map<std::string, std::pair<double, double>> iwin;     // the same on integers: [lo, hi), each bound's own < or <=
  const double inf = std::numeric_limits<double>::infinity();
  // filter_value_share: field -> (eq / in / ne, the literal values), and the fields a notnull reads with {get}
  std::map<std::string, std::pair<std::string, std::vector<json>>> vals;
  std::set<std::string> get_notnull;
  auto field_of = [](const json& x) -> std::string {
    for (const char* f : {"field", "get"})
      if (x.is_object() && x.size() == 1 && x.contains(f) && x[f].is_string()) return x[f].get<std::string>();
    return "";
  };
  for (auto& p : conj) {
    if (!p.is_object() || p.size() != 1) return std::nullopt;
    const std::string& k = p.begin().key();
    const json& v = p.begin().value();
    if (k == "notnull" && v.is_object() && v.contains("field") && v["field"].is_string()) {
      notnull.insert(v["field"].get<std::string>());
      continue;
    }
    if (k == "notnull" && v.is_object() && v.contains("get") && v["get"].is_string()) {
      get_notnull.insert(v["get"].get<std::string>());   // implied by an eq / in on the same field (checked below)
      continue;
    }
    if ((k == "eq" || k == "ne" || k == "in") && v.is_array() && v.size() == 2) {
      // a field against literals: eq / ne either way round, in as [field, values]. A bound predicate holds a literal
      // as its value ({"lit": x} as written); any other expression (a field, a computed value): no rule
      int side = !field_of(v[0]).empty() ? 0 : (k != "in" && !field_of(v[1]).empty() ? 1 : -1);
      if (side < 0) return std::nullopt;
      const json& other = v[1 - side];
      bool written = other.is_object() && other.size() == 1 && other.contains("lit");
      if (other.is_object() && !written) return std::nullopt;
      const json& lit = written ? other["lit"] : other;
      if ((k == "in") != lit.is_array() || vals.count(field_of(v[side]))) return std::nullopt;
      std::vector<json> list;
      if (k == "in") for (auto& x : lit) list.push_back(x);
      else list.push_back(lit);
      vals[field_of(v[side])] = {k, list};
      continue;
    }
    if ((k == "lt" || k == "le" || k == "gt" || k == "ge") && v.is_array() && v.size() == 2) {
      bool left = v[0].is_object() && v[0].contains("field") && v[0]["field"].is_string();
      bool right = v[1].is_object() && v[1].contains("field") && v[1]["field"].is_string();
      if (left == right) return std::nullopt;   // a field against a value, nothing else
      std::string f = (left ? v[0] : v[1])["field"].get<std::string>();
      auto x = number(left ? v[1] : v[0]);
      if (!x) return std::nullopt;
      bool upper = left ? (k == "lt" || k == "le") : (k == "gt" || k == "ge");   // field < x, or x > field
      auto& w = window.emplace(f, std::make_pair(-inf, inf)).first->second;
      if (upper) w.second = std::min(w.second, *x);
      else w.first = std::max(w.first, *x);
      bool strict = k == "lt" || k == "gt";   // field < x, field > x (either side): x excluded
      auto& iw = iwin.emplace(f, std::make_pair(-inf, inf)).first->second;
      if (upper) iw.second = std::min(iw.second, strict ? std::ceil(*x) : std::floor(*x) + 1);
      else iw.first = std::max(iw.first, strict ? std::floor(*x) + 1 : std::ceil(*x));
      continue;
    }
    return std::nullopt;
  }
  for (auto& f : get_notnull)
    if (!vals.count(f) || vals[f].first == "ne") return std::nullopt;   // a notnull no eq / in implies: no rule
  if (notnull.empty() && window.empty() && vals.empty()) return std::nullopt;
  if ((!notnull.empty() && !rules.notnull_label) || (!window.empty() && !rules.date_filter) ||
      (!vals.empty() && !rules.filter_value_share))
    return std::nullopt;
  double share = 1.0;
  std::map<std::string, double> label_value_share;   // per label: its value conjuncts' share (value_share_correlated)
  for (auto& [f, kv] : vals) {
    // a vertex property of the rows' one label that the schema indexes; an edge's property or any other: no rule
    if (!set_source(a["rows"], f).etype.empty()) return std::nullopt;
    std::string lab = set_label(a["rows"], lookup(), {f});
      if (lab.empty() || !st.has_top_values(lab, f)) return std::nullopt;
    double n = 0;
    for (auto& x : kv.second) n += st.value_rows(lab, f, x, "a filter's eq / in / ne on an indexed property");
    double s = std::min(1.0, n / std::max(st.type_count(lab), 1.0));
    double fs = kv.first == "ne" ? 1.0 - s : s;
    if (!rules.value_share_correlated) { share *= fs; continue; }
    auto [it, fresh] = label_value_share.emplace(lab, fs);
    if (!fresh) it->second = std::min(it->second, fs);
  }
  // value_share_correlated: several eq / in / ne fields of one label keep the smallest field's share, the conjunction's
  // upper bound (the fields assumed correlated, as JOB's info and info_type_id are), not the product
  for (auto& [lab, fs] : label_value_share) share *= fs;
  if (!notnull.empty()) {
    // the props read whose prop this filter requires: its label's share of the hop that made its input
    std::string props;
    std::string lab = set_label(a["rows"], lookup(), notnull, &props);
    if (lab.empty() || props.empty()) return std::nullopt;
    const json& pa = steps.at(props).second;
    std::string hop;
    json cur = pa["vids"];
    for (int i = 0; i < 16 && is_ref(cur); i++) {   // back to the hop through the ops that keep rows
      auto it = steps.find(ref_name(cur));
      if (it == steps.end()) break;
      const std::string& op = it->second.first;
      const json& sa = it->second.second;
      if (op == "read_neighbors") { hop = ref_name(cur); break; }
      static const std::map<std::string, std::string> keep = {{"dedup", "items"}, {"exclude", "items"},
                                                              {"pluck", "rows"},  {"rename", "rows"},
                                                              {"filter", "rows"}, {"filter_in", "rows"},
                                                              {"top_k", "rows"}};
      auto k = keep.find(op);
      if (k == keep.end() || (op == "pluck" && sa.value("field", json()) == "src") || !sa.contains(k->second)) break;
      cur = sa[k->second];
    }
    if (hop.empty()) return std::nullopt;
    const json& ha = steps.at(hop).second;
    auto done = ctx.label_share.find(hop);
    if (done == ctx.label_share.end() || done->second != lab) {   // else the hop holds the label's share already
      auto s = st.far_label_share(ha.value("etype", ""), kv::dir_of(ha), lab);
      if (!s) return std::nullopt;
      share *= *s;
    }
  }
  for (auto& [f, w] : window) {
    SetSource src = set_source(a["rows"], f);
    if (!src.etype.empty()) {
      // rows from a hop carry the edge's properties: an int one takes the edge type's values; any other (an edge's
      // date: no per-day counts per edge type) has no rule
      std::string t = st.prop_type("eprop|" + src.etype + "|" + f);
      if (t == "int") {
        share *= int_share("eprop|" + src.etype + "|" + f, iwin.at(f), st.edge_total(src.etype, "a range filter"));
        continue;
      }
      if (!t.empty()) return std::nullopt;
    }
    auto prior = prior_window(a["rows"], f);
    std::string lab = set_label(a["rows"], lookup(), notnull);
    if (lab.empty()) {
      // no one label: each label's share, weighted
      double num = 0, den = 0;
      bool any = false;   // a property of some label (a field no label declares is no property: computed by the plan)
      for (auto& [l, wt] : src.labels) {
        double sl;
        if (st.is_date_prop(l, f)) {
          auto n = date_share(l, f, w, prior);
          if (!n) st.date_span({l}, f, "a date filter");   // refused with its flat form
          sl = *n;
        } else if (st.is_int_prop("prop|" + l + "|" + f)) {
          sl = int_share("prop|" + l + "|" + f, iwin.at(f), st.type_count(l));
        } else if (st.declares(l, f)) {
          return std::nullopt;   // a property with no range rule (a string): the filter rule
        } else {
          den += wt;   // the label does not declare the field: its rows hold no value, none passes
          continue;
        }
        any = true;
        num += wt * sl;
        den += wt;
      }
      if (!any || den <= 0) return std::nullopt;
      share *= num / den;
      continue;
    }
    if (auto n = date_share(lab, f, w, prior)) {
      share *= *n;
    } else if (st.is_date_prop(lab, f)) {
      st.date_span({lab}, f, "a date filter");   // a declared date with no date_days entry: refused with its flat form
      return std::nullopt;
    } else if (st.is_int_prop("prop|" + lab + "|" + f)) {
      share *= int_share("prop|" + lab + "|" + f, iwin.at(f), st.type_count(lab));
    } else {
      // a string, or a field the plan computes: no rule (the date axis's span is a date's share only)
      return std::nullopt;
    }
  }
  return share;
}

double Walker::int_share(const std::string& key, std::pair<double, double> lohi, double total) const {
  return std::min(1.0, st.int_in_window(key, lohi.first, lohi.second, "a range filter") / std::max(total, 1.0));
}

std::optional<double> Walker::date_share(const std::string& l, const std::string& f, std::pair<double, double> w,
                                         const std::optional<std::pair<double, double>>& prior) const {
  if (!prior) {
    auto n = st.in_window(l, f, w.first, w.second);
    if (!n) return std::nullopt;
    return std::min(1.0, *n / std::max(st.type_count(l), 1.0));
  }
  auto both = st.in_window(l, f, std::max(w.first, prior->first), std::min(w.second, prior->second));
  auto pre = st.in_window(l, f, prior->first, prior->second);
  if (!both || !pre) return std::nullopt;
  return *pre > 0 ? std::min(1.0, *both / *pre) : 0.0;
}

std::optional<std::pair<double, double>> Walker::prior_window(json cur, const std::string& field) const {
  static const std::map<std::string, std::string> keep = {
      {"dedup", "items"},   {"exclude", "items"}, {"pluck", "rows"},  {"rename", "rows"},
      {"filter", "rows"},   {"filter_in", "rows"}, {"top_k", "rows"}, {"project", "rows"}};
  for (int i = 0; i < 16 && is_ref(cur); i++) {
    auto it = steps.find(ref_name(cur));
    if (it == steps.end()) return std::nullopt;
    const std::string& op = it->second.first;
    const json& sa = it->second.second;
    if (op == "read_index_range") {
      if (sa.value("prop", json()) != field || !has(sa, "lo") || !has(sa, "hi") || !sa["lo"].is_number() ||
          !sa["hi"].is_number())
        return std::nullopt;
      return std::make_pair(sa["lo"].get<double>(), sa["hi"].get<double>());
    }
    if (op == "read_vertex_props" || op == "read_vertices_by_label") {   // a label scan keeps `keep`'s vids
      const char* in = op == "read_vertex_props" ? "vids" : "keep";
      if (!sa.contains(in)) return std::nullopt;
      cur = sa[in];
      continue;
    }
    auto k = keep.find(op);
    if (k == keep.end() || (op == "pluck" && sa.value("field", json()) == "src") || !sa.contains(k->second))
      return std::nullopt;
    cur = sa[k->second];
  }
  return std::nullopt;
}

bool Walker::holds(const SetSource& s, const std::string& field) const {
  if (!s.etype.empty() && !st.prop_type("eprop|" + s.etype + "|" + field).empty()) return true;
  for (auto& [l, w] : s.labels)
    if (st.declares(l, field)) return true;
  return false;
}

Walker::SetSource Walker::set_source(json cur, const std::string& field, int depth) const {
  // the ops whose rows are their input's rows (or a subset): the labels carry through
  static const std::map<std::string, std::string> keep = {
      {"dedup", "items"},     {"exclude", "items"}, {"pluck", "rows"}, {"rename", "rows"},  {"filter", "rows"},
      {"filter_in", "rows"},  {"top_k", "rows"},    {"derive", "rows"}, {"project", "rows"}, {"attach", "rows"},
      {"annotate", "items"},  {"flag_in", "rows"}};
  auto by_count = [&](const json& l) {   // a label or a label list, each label weighted by its type count
    std::vector<std::pair<std::string, double>> out;
    if (l.is_string()) out.push_back({l.get<std::string>(), 1.0});
    if (l.is_array())
      for (auto& x : l)
        if (x.is_string()) out.push_back({x.get<std::string>(), std::max(st.type_count(x.get<std::string>()), 0.0)});
    return out;
  };
  SetSource src;
  for (int i = 0; i < 16 && depth < 8 && is_ref(cur); i++) {
    auto it = steps.find(ref_name(cur));
    if (it == steps.end()) return src;
    const std::string& op = it->second.first;
    const json& sa = it->second.second;
    if (op == "read_neighbors") {
      src.etype = str_or(sa, "etype", "");
      if (sa.contains("label")) {
        src.labels = by_count(sa["label"]);
      } else {   // the far end's labels, by their share of its edges (none for "both")
        const json tc = st.d.value("type_counts", json::object());   // a named copy: items() must not outlive it
        for (auto& [l, _] : tc.items())
          if (auto f = st.far_label_share(src.etype, kv::dir_of(sa), l); f && *f > 0) src.labels.push_back({l, *f});
      }
      return src;
    }
    if (op == "read_vertex_props" || op == "read_vertices_by_label") {
      if (sa.contains("label") && (sa["label"].is_string() || sa["label"].is_array())) {
        src.labels = by_count(sa["label"]);
        return src;
      }
      if (op == "read_vertex_props" && sa.contains("vids")) { cur = sa["vids"]; continue; }
      return src;
    }
    if (op == "concat") {   // the two sides, each weighted by its estimated rows
      for (const char* side : {"a", "b"}) {
        if (!sa.contains(side) || !is_ref(sa[side])) return SetSource{};
        SetSource s = set_source(sa[side], field, depth + 1);
        double tot = 0;
        for (auto& [l, w] : s.labels) tot += w;
        if (s.labels.empty() || tot <= 0) return SetSource{};
        double rows = est.count(ref_name(sa[side])) ? est.at(ref_name(sa[side])) : 0.0;
        for (auto& [l, w] : s.labels) src.labels.push_back({l, w / tot * rows});
      }
      return src;
    }
    if (op == "join" || op == "left_join" || op == "join_multi" || op == "zip_rows") {
      // a joined row holds both sides' fields: the field's side, when only one side can hold it
      if (!sa.contains("left") || !sa.contains("right")) return SetSource{};
      SetSource l = set_source(sa["left"], field, depth + 1), r = set_source(sa["right"], field, depth + 1);
      bool hl = holds(l, field), hr = holds(r, field);
      return hl == hr ? SetSource{} : (hl ? l : r);
    }
    auto k = keep.find(op);
    if (k == keep.end() || (op == "pluck" && sa.value("field", json()) == "src") || !sa.contains(k->second)) return src;
    // a column the step computes under the field's name is no stored property
    if (sa.value("into", json()) == field || (op == "annotate" && sa.value("field", json()) == field)) return SetSource{};
    cur = sa[k->second];
  }
  return src;
}

// every switch: its name (what --rules-off takes) and its member
static const std::vector<std::pair<std::string, bool Rules::*>>& rule_table() {
  static const std::vector<std::pair<std::string, bool Rules::*>> t = {
      {"concat_keeps_via", &Rules::concat_keeps_via}, {"frontier_inclusion", &Rules::frontier_inclusion},
      {"dedup_by_degree", &Rules::dedup_by_degree},   {"filter_keeps_via", &Rules::filter_keeps_via},
      {"date_histogram", &Rules::date_histogram},     {"props_distinct", &Rules::props_distinct},
      {"notnull_label", &Rules::notnull_label},       {"date_filter", &Rules::date_filter},
      {"filter_in_share", &Rules::filter_in_share},   {"dedup_domain_cap", &Rules::dedup_domain_cap},
      {"size_biased_via", &Rules::size_biased_via},   {"concat_domain", &Rules::concat_domain},
      {"exclude_share", &Rules::exclude_share},       {"group_key_cap", &Rules::group_key_cap},
      {"hop_edge_cap", &Rules::hop_edge_cap},         {"index_value_rows", &Rules::index_value_rows},
      {"filter_value_share", &Rules::filter_value_share},     {"far_label_consumer", &Rules::far_label_consumer},
      {"entry_value_degree", &Rules::entry_value_degree},
      {"props_read_consumer", &Rules::props_read_consumer},
      {"value_share_correlated", &Rules::value_share_correlated}};
  return t;
}

const std::vector<std::string>& Rules::names() {
  static const std::vector<std::string> n = [] {
    std::vector<std::string> out;
    for (auto& [name, member] : rule_table()) out.push_back(name);
    return out;
  }();
  return n;
}

bool Rules::set(const std::string& name, bool on) {
  for (auto& [n, member] : rule_table()) {
    if (n != name) continue;
    this->*member = on;
    return true;
  }
  return false;
}

json bound_args(const json& args, const Bindings& b) { return bound(args, b); }

std::optional<std::pair<std::string, std::string>> anchor_step(const Plan& plan) {
  for (auto& s : plan.steps) {
    if (s.op != "read_vertex_by_id") continue;
    json a = bound(s.args, plan.bindings);
    if (a.contains("vid") && a["vid"].is_string()) return std::make_pair(s.name, a["vid"].get<std::string>());
  }
  return std::nullopt;
}

// a step's StepSet from its op, its arguments and the sets of the steps it reads (the walk's order: inputs first)
StepSet step_set(const std::string& name, const std::string& op, const json& a, const StepSets& sets,
                 const StepLookup& lookup) {
  static const std::map<std::string, std::string> keep = {{"dedup", "items"}, {"exclude", "items"}, {"pluck", "rows"},
                                                          {"rename", "rows"}, {"filter", "rows"}, {"filter_in", "rows"},
                                                          {"top_k", "rows"}};
  auto from = [&](const char* k) -> const StepSet* {
    if (!a.contains(k) || !is_ref(a[k])) return nullptr;
    auto it = sets.find(ref_name(a[k]));
    return it == sets.end() ? nullptr : &it->second;
  };
  StepSet s;
  if (op == "read_neighbors") {
    s.hop = s.hop_via_props = s.hop_via_concat = name;
  } else if (auto k = keep.find(op); k != keep.end() && !(op == "pluck" && str_or(a, "field", "") == "src")) {
    if (const StepSet* in = from(k->second.c_str())) {
      s.hop = in->hop;
      s.hop_via_props = in->hop_via_props;
      s.hop_via_concat = in->hop_via_concat;
    }
  } else if (op == "concat") {
    if (const StepSet* in = from("a")) s.hop_via_concat = in->hop_via_concat;
  } else if (op == "read_vertex_props") {
    if (const StepSet* in = from("vids")) s.hop_via_props = in->hop_via_props;
  }
  s.label = set_label(json{{"ref", name}}, lookup);
  return s;
}

// each step's arguments as predict sees them: bound, and a filter's predicate as written (Walker::filter_share and
// set_label read it), and a join_multi's `where` (its row estimate)
std::vector<json> walk_args(const Plan& plan) {
  std::vector<json> args;
  for (auto& s : plan.steps) {
    args.push_back(bound(s.args, plan.bindings));
    if (auto p = s.expr_asts.find("pred"); p != s.expr_asts.end()) args.back()["pred"] = bound(p->second, plan.bindings);
    if (auto w = s.expr_asts.find("where"); w != s.expr_asts.end()) args.back()["where"] = bound(w->second, plan.bindings);
  }
  return args;
}

std::map<std::string, double> far_labels(const json& hop, const DatasetStats& stats) {
  std::string et = hop.value("etype", ""), dir = kv::dir_of(hop);
  std::map<std::string, double> share;
  double all = 0;
  for (auto& o : stats.owners("a step's producer hop")) {   // the far end of an out hop is the edge type's in-owners
    std::string od = o.at("direction").get<std::string>();
    bool far = (dir == "out" && od == "in") || (dir == "in" && od == "out") || dir == "both";
    if (o.at("etype").get<std::string>() != et || !far) continue;
    share[o.at("owner_label").get<std::string>()] += o.at("edges").get<double>();
    all += o.at("edges").get<double>();
  }
  if (all <= 0) return {};
  for (auto& [l, n] : share) n /= all;
  return share;
}

StepSets step_sets(const Plan& plan) {
  std::vector<json> args = walk_args(plan);
  std::map<std::string, std::pair<std::string, const json*>> steps;   // the steps walked so far
  StepLookup lookup = [&steps](const std::string& n) -> std::optional<std::pair<std::string, const json*>> {
    auto it = steps.find(n);
    if (it == steps.end()) return std::nullopt;
    return it->second;
  };
  StepSets sets;
  for (size_t i = 0; i < plan.steps.size(); i++) {
    const Step& s = plan.steps[i];
    steps[s.name] = {s.op, &args[i]};
    sets[s.name] = step_set(s.name, s.op, args[i], sets, lookup);
  }
  return sets;
}

std::optional<Env> estimate_cardinalities(const Plan& plan, const DatasetStats& stats, const Rules& rules,
                                          std::string* why) {
  Walker w{stats, rules, {}, {}, {}, {}, {}};
  std::vector<json> args = walk_args(plan);
  if (auto an = anchor_step(plan)) {
    w.ctx.anchor = an->second;
    w.ctx.anchor_out = an->first;
  }
  if (plan.hints.is_object()) {
    for (auto& e : plan.hints.value("size_biased", json::array())) w.ctx.size_biased.insert(e.get<std::string>());
    for (auto& e : plan.hints.value("chase_root_ratio", json::array())) w.ctx.root_ratio.insert(e.get<std::string>());
  }
  // the ops that reshape a set keep how it was reached
  static const std::map<std::string, std::string> via_passthru = {
      {"dedup", "items"}, {"exclude", "items"}, {"pluck", "rows"}, {"rename", "rows"},
      {"read_filter_by_label_probe", "vids"}, {"read_filter_by_label_index", "vids"}};
  for (size_t i = 0; i < plan.steps.size(); i++) w.plan_steps.emplace_back(plan.steps[i].name, plan.steps[i].op, &args[i]);
  Env env;
  for (size_t i = 0; i < plan.steps.size(); i++) {
    const Step& s = plan.steps[i];
    const json& a = args[i];
    double e = 0;
    StepCard card;
    w.steps[s.name] = {s.op, a};
    if (!w.op(s.name, s.op, a, e, card)) {
      if (why) *why = "no rule for op " + s.op + " (step " + s.name + ")";
      return std::nullopt;
    }
    std::string via_arg;
    if (auto vp = via_passthru.find(s.op); vp != via_passthru.end()) via_arg = vp->second;
    else if (rules.filter_keeps_via && (s.op == "filter" || s.op == "filter_in")) via_arg = "rows";
    if (!via_arg.empty() && a.contains(via_arg) && is_ref(a[via_arg])) {
      std::string src = ref_name(a[via_arg]);
      bool src_pluck = s.op == "pluck" && a.value("field", json()) == "src";
      if (w.ctx.via.count(src) && !src_pluck) {
        w.ctx.via[s.name] = w.ctx.via[src];
        if (w.ctx.thinned.count(src) || s.op == "filter" || s.op == "filter_in") w.ctx.thinned.insert(s.name);
      }
      if (w.ctx.universe.count(src)) w.ctx.universe[s.name] = w.ctx.universe[src];
    }
    w.est[s.name] = e;
    card.rows = e;
    env[s.name] = std::move(card);
  }
  return env;
}

}  // namespace evalcpp::predict
