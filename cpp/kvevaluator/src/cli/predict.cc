// `predict --gate [--plants <rule>,...|all] [--threads N] [--steps-out FILE] <request.json>...`: the exact gate of
// predict mode. Each request is a `run` request on a meta store; it runs once, with trace_args and parts
// on (P = 1), then every KV step is priced from its truth card (Meta::predict_step_truth) and compared with the run's
// counts on every lane. Each planted rule (--plants) is priced from the same run's steps and must differ somewhere; a
// step is re-priced for a plant only when its rules consulted that plant (else the prediction is the gate's own).
// One JSON object on stdout: the binary, per request its body_sha and steps, the differences, and per plant whether
// it was caught; --steps-out: two rows per KV step (measured, truth), every lane. --threads N: N sessions, one store
// each at a time; the output is the same bytes at any N. Exit 0: no lane differs, at least one step was compared, and
// every plant was caught; 1: the gate differs (or compared nothing); 3: a refusal by the gate's own pricing; 4: the
// gate passed but a plant was not caught (no lane differed, or its pricing refused: a refusal is not a catch).
#include <algorithm>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <tuple>
#include <map>
#include <set>
#include <iostream>
#include <atomic>
#include <mutex>
#include <thread>

#include "cli/capture_writer.h"
#include "cli/commands.h"
#include "cli/session.h"
#include "formats/common.h"
#include "formats/meta/format.h"
#include "predict/cardinality.h"
#include "predict/estimate_input.h"
#include "provenance.h"
#include "util/record_stream.h"
#include "util/sha256.h"
#include "ops/kv_contract.h"
#include "contract/contract.h"

namespace evalcpp {

namespace {
// the lanes of a KV step, written once: name -> the step JSON's value and the predicted Counts' field. Every list
// below is read from it: the gate compares all of them, --accuracy writes all of them, --plans writes the run and used
// lanes on a read row (`run`: a counts row sums them) and the write lanes beside them
enum class LaneKind { kRun, kUsed, kWrite };
struct Lane { const char* name; bool in_bytes; LaneKind kind; uint64_t Counts::*field; };
const Lane kLanes[] = {
    {"S", false, LaneKind::kRun, &Counts::S}, {"G", false, LaneKind::kRun, &Counts::G},
    {"N", false, LaneKind::kRun, &Counts::N},
    {"S_miss", false, LaneKind::kRun, &Counts::S_miss}, {"G_miss", false, LaneKind::kRun, &Counts::G_miss},
    {"N_miss", false, LaneKind::kRun, &Counts::N_miss},
    {"get_key", true, LaneKind::kRun, &Counts::get_key}, {"get_value", true, LaneKind::kRun, &Counts::get_value},
    {"scan_key", true, LaneKind::kRun, &Counts::scan_key}, {"scan_value", true, LaneKind::kRun, &Counts::scan_value},
    {"seek_miss_key", true, LaneKind::kRun, &Counts::seek_miss_key},
    {"next_miss_key", true, LaneKind::kRun, &Counts::next_miss_key},
    {"get_miss_key", true, LaneKind::kRun, &Counts::get_miss_key},
    {"used_key", true, LaneKind::kUsed, &Counts::used_key}, {"used_value", true, LaneKind::kUsed, &Counts::used_value},
    {"P", false, LaneKind::kWrite, &Counts::P}, {"D", false, LaneKind::kWrite, &Counts::D},
    {"write_key", true, LaneKind::kWrite, &Counts::write_key},
    {"write_value", true, LaneKind::kWrite, &Counts::write_value},
};
// the names of the lanes `keep` takes, in kLanes' order
template <class F> std::vector<std::string> lane_names(F keep) {
  std::vector<std::string> out;
  for (const Lane& L : kLanes) if (keep(L)) out.push_back(L.name);
  return out;
}
// ... and the last-chunk lane: the step JSON's "last_chunk" object (absent: no touch) and Counts::LastChunk's field
struct LcLane { const char* name; uint64_t Counts::LastChunk::*field; };
const LcLane kLcLanes[] = {
    {"reads", &Counts::LastChunk::reads}, {"read_bytes", &Counts::LastChunk::read_bytes},
    {"read_target", &Counts::LastChunk::read_target}, {"writes", &Counts::LastChunk::writes},
    {"write_bytes", &Counts::LastChunk::write_bytes}, {"write_base", &Counts::LastChunk::write_base},
    {"write_target", &Counts::LastChunk::write_target},
};
uint64_t measured_lc(const json& step, const char* name) {
  return step.contains("last_chunk") ? step["last_chunk"].value(name, uint64_t(0)) : 0;
}
}  // namespace

namespace {
json load_json(const std::string& path) {
  std::ifstream f(path);
  if (!f) throw std::runtime_error("cannot open " + path);
  return json::parse(f);
}
std::vector<std::string> split_commas(const std::string& list) {   // "a,b" -> {a, b}; empty items dropped
  std::vector<std::string> out;
  for (size_t a = 0; a <= list.size();) {
    size_t b = list.find(',', a);
    if (b == std::string::npos) b = list.size();
    if (b > a) out.push_back(list.substr(a, b - a));
    a = b + 1;
  }
  return out;
}
std::string base_of(const std::string& path) {   // a file's name without its dir and extension
  size_t a = path.find_last_of('/'), b = path.rfind('.');
  a = a == std::string::npos ? 0 : a + 1;
  return path.substr(a, (b == std::string::npos || b < a) ? std::string::npos : b - a);
}
size_t distinct_count(const json& vids) {
  std::set<std::string> d;
  for (auto& v : vids) d.insert(v.get<std::string>());
  return d.size();
}
// the labels a step's distinct inputs carried (label id -> count; "?" = none): what the truth-card levels knows of its inputs
std::vector<std::pair<std::string, double>> label_mix(const json& step, const char* key) {
  std::vector<std::pair<std::string, double>> mix;
  std::set<std::string> seen;
  const json& a = step["args"][key];
  const json* tags = step.contains("arg_tags") && step["arg_tags"].contains(key) ? &step["arg_tags"][key] : nullptr;
  for (size_t i = 0; i < a.size(); i++) {
    if (!a[i].is_string() || !seen.insert(a[i].get<std::string>()).second) continue;
    std::string k = tags && !(*tags)[i].is_null() ? std::to_string((*tags)[i].get<int>()) : "?";
    auto it = std::find_if(mix.begin(), mix.end(), [&k](const auto& e) { return e.first == k; });
    if (it == mix.end()) mix.emplace_back(k, 1.0);
    else it->second += 1.0;
  }
  return mix;
}

// the vertex properties the schema declares as dates, "L|p" (a date filter on one needs its date_days entry)
std::set<std::string> date_props(const Schema& schema) {
  std::set<std::string> out;
  for (auto& v : schema.vertices())
    for (auto& p : v.props)
      if (p.type == "date") out.insert(v.label + "|" + p.name);
  return out;
}

// every property the schema declares with its type, keyed prop|L|p (a vertex label's) and eprop|E|p (an edge type's)
std::map<std::string, std::string> prop_types(const Schema& schema) {
  std::map<std::string, std::string> out;
  for (auto& v : schema.vertices())
    for (auto& p : v.props) out["prop|" + v.label + "|" + p.name] = p.type;
  for (auto& e : schema.edges())
    for (auto& p : schema.edge_props(e.etype)) out["eprop|" + e.etype + "|" + p.name] = p.type;
  return out;
}

// one body of a request's plan, bound to the request's params: a bundle's params and lets with that body's steps
Plan bind_body(const json& plan, const std::string& body, const json& params, const Schema& schema) {
  json one = json::object();
  for (auto it = plan.begin(); it != plan.end(); ++it)
    if (it.key() != "plans" && it.key() != "variants" && it.key() != "requires" && it.key() != "for")
      one[it.key()] = it.value();   // which body runs is settled: no capability selection
  if (plan.contains("plans"))
    for (auto it = plan["plans"].at(body).begin(); it != plan["plans"][body].end(); ++it) one[it.key()] = it.value();
  return bind_plan(one, &params, {}, schema);
}

// estimate_cardinalities with a refusal (a statistic the file lacks, thrown by name) returned as nullopt and its cause
// in `why`, counted in `refused`: the run names the body and goes on to the next, instead of stopping
std::optional<predict::Env> estimate_or_refuse(const Plan& plan, const predict::DatasetStats& st, const predict::Rules& rules,
                                               std::string* why, int* refused) {
  try {
    return predict::estimate_cardinalities(plan, st, rules, why);
  } catch (const std::exception& ex) {
    if (why) *why = std::string("refused: ") + ex.what();
    if (refused) (*refused)++;
    return std::nullopt;
  }
}

std::vector<std::string> bodies_of(const json& plan) {
  std::vector<std::string> out;
  if (plan.contains("plans")) for (auto it = plan["plans"].begin(); it != plan["plans"].end(); ++it) out.push_back(it.key());
  else out.push_back(plan.at("plan").get<std::string>());
  return out;
}

// the `producer` input of a Chunked read (formats/meta/predict.cc chunks_touched): the hop that made the set, its
// owners, and the set's one label when it holds one (then the span is that label's lists only). One helper for every
// level, so true_terms and predicted read the same set the same way (the level from the truth card built it without the label)
predict::EstimateInput::Producer producer_input(const json& hop, const predict::StepSet& set, double owners) {
  return {hop.value("etype", ""), kv::dir_of(hop), set.label, owners};
}

// the StepSet of the set an argument names (a ref to a step the walk filled); null for a literal or an unknown step
const predict::StepSet* set_of(const predict::StepSets& sets, const json& arg) {
  if (!arg.is_object() || !arg.contains("ref")) return nullptr;
  auto it = sets.find(arg["ref"].get<std::string>());
  return it == sets.end() ? nullptr : &it->second;
}

// the cardinality layer's sizes for one body, read by the name a refusal gives (<step>, or <step>#<size>): a step or
// size the layer did not give refuses the step that needs it
struct EnvReader {
  const predict::Env& env;
  const std::string& what;
  using Field = predict::Size predict::StepCard::*;
  using List = std::vector<predict::Size> predict::StepCard::*;
  const predict::StepCard* card(const std::string& n) const {
    auto it = env.find(n);
    return it == env.end() ? nullptr : &it->second;
  }
  [[noreturn]] void refuse(const std::string& k) const {
    throw std::runtime_error("no cardinality estimate '" + k + "' for " + what);
  }
  double rows(const std::string& n) const {
    if (const predict::StepCard* c = card(n)) return c->rows;
    refuse(n);
  }
  bool has(const std::string& n, Field f) const {
    const predict::StepCard* c = card(n);
    return c && (c->*f).has_value();
  }
  double size(const std::string& n, Field f, const char* k) const {
    if (!has(n, f)) refuse(n + "#" + k);
    return *(card(n)->*f);
  }
  double at(const std::string& n, List f, size_t i, const char* k) const {
    const predict::StepCard* c = card(n);
    if (!c || i >= (c->*f).size() || !(c->*f)[i]) refuse(n + "#" + k + std::to_string(i));
    return *(c->*f)[i];
  }
};

// a read_edge_exists step's inputs, from the cardinality layer's env: the candidates, the via walk, the anchors
// and each read's owners and (candidate, anchor) pairs. The anchors are the plan's anchor when `others` names its step
predict::EstimateInput edge_exists_inputs(const std::string& name, const predict::Env& env, const json& st,
                                          const std::string& anchor, const json& anchor_vid, const std::string& what) {
  EnvReader e{env, what};
  using C = predict::StepCard;
  predict::EstimateInput in;
  in.rows = e.rows(name);
  in.nvia = e.size(name, &C::nvia, "nvia");
  in.nvnbrs = e.size(name, &C::nvnbrs, "nvnbrs");
  in.nothers = e.size(name, &C::nothers, "nothers");
  for (size_t i = 0; i < st.at("reads").size(); i++) {
    in.eown.push_back(e.at(name, &C::eown, i, "eown"));
    in.eprobe.push_back(e.at(name, &C::eprobe, i, "eprobe"));
  }
  const json& o = st.contains("others") ? st["others"] : json();
  if (o.is_object() && o.contains("ref") && o["ref"] == anchor && !anchor_vid.is_null())
    in.anchor_vid = anchor_vid.get<std::string>();
  return in;
}

// the predicted level's inputs to a KV step's estimate: everything from the cardinality layer's env and the step's set, nothing
// from a run (predict mode proper). `anchor`: the plan's bound anchor step; `anchor_vid`: its vid (a parameter).
// Throws on a missing env value (the step is then not estimated). `hops_tagged`: the run's label_tags.hops
predict::EstimateInput inputs_b(const std::string& op, const std::string& name, const predict::Env& env,
                                const std::map<std::string, json>& ir, const predict::StepSets& sets,
                                const predict::DatasetStats& stats, const std::string& anchor, const json& anchor_vid, const std::string& what,
                                bool hops_tagged) {
  // every KV step needs the statistics' owners (a file without them refuses each step here, before its sizes)
  stats.owners("a step's producer hop");
  EnvReader e{env, what};
  using C = predict::StepCard;
  predict::EstimateInput in;
  // the set a read reads: its far end's labels by share, and the hop that made it with its owners (Chunked locality)
  auto set_facts = [&](const json& arg) {
    const predict::StepSet* s = set_of(sets, arg);
    if (!s) return;
    // a hop the raw body does not name (a step an inlined fragment renamed) gives neither, as the plan walk did
    if (auto h = ir.find(s->hop_via_concat); h != ir.end()) {
      std::map<std::string, double> share = predict::far_labels(h->second, stats);
      // a hop from the plan's anchor: the anchor's own far-label mix when the anchor tier has it (a person's posts vs
      // comments), else the population's. Never at level predicted: there is no anchor vid
      const json& hs = h->second.contains("srcs") ? h->second["srcs"] : json();
      if (hs.is_object() && hs.contains("ref") && hs["ref"] == anchor && anchor_vid.is_string())
        if (auto own = stats.anchor_far_share(anchor_vid.get<std::string>(), h->second.value("etype", ""),
                                              kv::dir_of(h->second)))
          share = std::move(*own);
      if (!share.empty()) in.label_share = std::move(share);
    }
    if (auto h = ir.find(s->hop_via_props); h != ir.end() && e.has(s->hop_via_props, &C::sd))
      in.producer = producer_input(h->second, *s, e.size(s->hop_via_props, &C::sd, "sd"));
  };
  const json& st = ir.at(name);
  if (op == "read_neighbors") {
    in.n = e.size(name, &C::sd, "sd");
    in.entries = e.rows(name);
    const json& src = st["srcs"];
    set_facts(src);
    if (src.is_object() && src.contains("ref") && src["ref"] == anchor && !anchor_vid.is_null())
      in.anchor_vid = anchor_vid.get<std::string>();
  } else if (op == "read_vertex_props") {
    in.n = e.has(name, &C::nv) ? e.size(name, &C::nv, "nv") : e.rows(name);   // the distinct vids, else the rows
    set_facts(st["vids"]);
  } else if (op == "read_top_k_by_index") {
    in.crossed = e.size(name, &C::crossed, "crossed");
    in.winners = e.rows(name);
  } else if (op == "read_index_eq" || op == "read_index_range") {
    in.matches = e.rows(name);
  } else if (op == "read_filter_by_label_probe" || op == "read_filter_by_label_index") {
    // the inputs that carry no label probe. Rows a hop made carry its far end's label (a run tags hop outputs by
    // default: exec/label_tags.h), so none probes; otherwise the layer's count, every input taken as unlabelled
    double nb = e.has(name, &C::nprobe) ? e.size(name, &C::nprobe, "nprobe") : 0;
    const predict::StepSet* s = set_of(sets, st["vids"]);
    if (hops_tagged && s && !s->hop.empty()) nb = 0;
    in.probes = nb;
    in.walk = nb > 0 ? 1.0 : 0.0;
  } else if (op == "read_closure") {
    // the sources: a ref's estimate, or a literal list's length (the cardinality layer's sz rule)
    const json& s = st["srcs"];
    double v = e.rows(name), src = s.is_object() && s.contains("ref") ? e.rows(s["ref"].get<std::string>())
                                                                 : (s.is_array() ? double(s.size()) : 1.0);
    in.nodes = v;
    in.links = std::max(0.0, v - src);
  } else if (op == "read_chase") {
    in.nodes = e.size(name, &C::steps, "steps");
    in.links = e.size(name, &C::edges, "edges");
  } else if (op == "read_edge_exists") {
    in = edge_exists_inputs(name, env, st, anchor, anchor_vid, what);
  }
  return in;
}

bool hops_tag(const json& req) {   // the run tags hop outputs with their label (the default)
  const json* r = req.contains("run") ? &req["run"] : nullptr;
  return !(r && r->contains("label_tags") && (*r)["label_tags"].is_object() && !(*r)["label_tags"].value("hops", true));
}

// the output rows a level assumed
double rows_assumed(const predict::EstimateInput& in, const predict::Env& env, const std::string& name) {
  for (const predict::Size* v : {&in.entries, &in.matches, &in.nodes, &in.winners, &in.n, &in.rows})
    if (*v) return **v;
  auto it = env.find(name);
  return it != env.end() ? it->second.rows : NAN;
}

// predict mode proper: each request's plan, every body of it, priced from the statistics alone; no store is opened (a
// request's store names the layout's tally, <tally-dir>/<store name>.json, which carries the layout's spec). Two levels:
// `predicted` (the population statistics only) and `predicted_anchor` (with the anchor statistics, which are
// conditioned on the query's parameter: DatasetStats::without_anchor_stats). The anchor level runs only when an anchor
// statistics file is given (anchor_path not empty): without one, the file is not read and only `predicted` is costed
// (the anchor file's parse was over half a layout's time on LinkBench and SF30). Each level is written as a capture in the
// measured captures' shape (a counts row per body, a step row per KV step, one part row per step), marked
// measured_as=predicted. A write plan is priced too (its steps name their vertices: no cardinality layer), its write
// lanes in its step and counts rows (a cell that holds a write request writes capture::write_run_cols)
bool is_kv_op(const std::string& op) { return kv::is_read_op(op); }   // a read of the contract's table

// a step predict prices: a KV read, or a write (its PUTs and DELs, and the reads of its read-modify-write)
bool is_predicted_op(const std::string& op) { return is_kv_op(op) || is_write_op(op); }

// what a request's earlier write steps wrote, which a later write step's estimate reads (the run's shadow overlay): the
// vertices created, and the edges added per "vid|etype|dir". Noted after the step is priced. One rule for --plans and
// --accuracy
void note_write(const std::string& op, const json& a, predict::EstimateInput& so_far) {
  if (op == "insert_vertex") so_far.created.push_back(a.at("vid").get<std::string>());
  if (op == "insert_edge")
    for (auto& [end, dir] : {std::pair<const char*, const char*>{"src", "out"}, {"dst", "in"}})
      so_far.added[a.at(end).get<std::string>() + "|" + a.at("etype").get<std::string>() + "|" + dir] += 1;
}

// Where predict --plans writes: a capture dir per level (the parent of counts/ steps/ parts/; the caller names it,
// python/kvevaluator/output_tree.py)
struct PlansOut {
  std::string dir[2];   // [0] level predicted, [1] level predicted_anchor; empty: not written
  bool fresh = false;   // replace a capture made from other inputs (statistics, rules, tally, spec, requests)
  std::vector<std::string> rules_off;
  json record_fields = json::object();   // fields the caller adds to every record (kvcost predict: the workload, the
                                         // plan files' tree and pipeline, as a measured record carries them)
};

std::string num(double v) {   // a lane as a capture writes it (12 significant digits)
  std::ostringstream o;
  o.precision(12);
  o << v;
  return o.str();
}

int predict_plans(const std::string& stats_path, const std::string& anchor_path, const predict::Rules& rules,
                  const std::string& tally_dir, const std::string& schema_path, const std::vector<std::string>& reqs,
                  const PlansOut& out) {
  auto t_start = std::chrono::steady_clock::now();
  static const std::set<std::string> own = {"cell", "sf", "measured_as", "source", "complete", "store", "inputs", "tally",
                                            "spec", "schema", "runs", "steps", "refused", "wall_s", "binary"};
  for (auto& [k, v] : out.record_fields.items())
    if (own.count(k)) {
      std::cerr << "predict --plans: --record-fields names " << k << ", a field the record writes itself\n";
      return 2;
    }
  const bool anchor_level = !anchor_path.empty();   // no anchor statistics: the population level only
  predict::DatasetStats full = predict::DatasetStats::load(stats_path, anchor_path);
  predict::DatasetStats bare = full.without_anchor_stats();
  struct Level { const char* source; const predict::DatasetStats* st; uint64_t not_estimated; };
  std::vector<Level> levels = {{"predicted", &bare, 0}};
  if (anchor_level) levels.push_back({"predicted_anchor", &full, 0});
  Schema schema = Schema::load(schema_path);
  full.require_schema(schema.hash(), "--schema " + schema_path);
  full.declare_date_props(date_props(schema));
  bare.declare_date_props(date_props(schema));
  full.declare_prop_types(prop_types(schema));
  bare.declare_prop_types(prop_types(schema));
  // a read row's lanes: the run's ops and read bytes, and the used bytes (kLanes)
  static const std::vector<std::string> lanes = lane_names([](const Lane& L) { return L.kind != LaneKind::kWrite; });
  // the lanes a counts row sums over its KV steps: the run's ops and its 7 read lanes (grid_bodies.RUN_COLS)
  static const std::vector<std::string> run_lanes = lane_names([](const Lane& L) { return L.kind == LaneKind::kRun; });
  static const std::vector<std::string> read_lanes =
      lane_names([](const Lane& L) { return L.kind == LaneKind::kRun && L.in_bytes; });
  // a write's own lanes: its PUTs, DELs and the bytes they write (step rows always; counts rows in a cell with writes)
  static const std::vector<std::string> write_lanes =
      lane_names([](const Lane& L) { return L.kind == LaneKind::kWrite; });
  // ... and its last-chunk touches (Counts::LastChunk), in a cell with writes only, as the write lanes
  static const std::vector<std::string> lc_lanes = contract::columns("last_chunk");
  // the identity a capture's record carries; a capture made from other inputs is not overwritten (unless --fresh).
  // The anchor statistics are in the anchor level's identity only: the population level never reads them. An input
  // file is recorded by its name and its sha256 (what it is), never by its path (where one machine kept it)
  auto file_name = [](const std::string& p) { return std::filesystem::path(p).filename().string(); };
  json inputs = {{"stats", {{"path", file_name(stats_path)}, {"sha256", capture::file_sha256(stats_path)}}},
                 {"anchor_stats", anchor_level ? json{{"path", file_name(anchor_path)},
                                                      {"sha256", capture::file_sha256(anchor_path)}}
                                               : json(nullptr)},
                 {"rules_off", out.rules_off}};
  // reqs: each request's name -> the sha256 of its plan and params (the cell's request set, part of its identity)
  struct Cell { std::string store, sf; std::vector<capture::Row> runs, steps, parts; std::map<std::string, std::string> reqs;
                std::set<std::string> queries; bool writes = false; };   // writes: a write request is in the cell
  std::map<std::string, Cell> cells[2];   // per level, per cell (a capture file per cell)
  std::map<std::string, json> tallies;
  std::map<std::string, std::unique_ptr<Format>> formats;
  uint64_t steps = 0, bodies = 0;
  int refused = 0;   // (body, level) pairs a missing statistic refused
  json refusals = json::array();
  for (auto& path : reqs) {
    json req = load_json(path);
    std::string sf = req["store"].contains("scale") ? req["store"]["scale"].get<std::string>() : "";
    if (!sf.empty()) full.require_scale(sf, "the request " + path);
    bool hops_tagged = hops_tag(req);
    std::string store = base_of(req["store"]["dir"].get<std::string>() + ".x");
    if (!tallies.count(store)) {
      tallies[store] = load_json(tally_dir + "/" + store + ".json");
      // the statistics and the tally price one graph: the same record stream (its lines, its freshness key)
      std::string why = record_stream_mismatch(full.d.value("records", json()), tallies[store].value("records", json()));
      if (!why.empty()) {
        std::cerr << "predict --plans: refused: the statistics " << stats_path << " and the tally " << tally_dir << "/"
                  << store << ".json were made from different record streams (" << why
                  << "): build the statistics again (python -m kvevaluator.predict.build_stats) or the tally\n";
        return 2;
      }
      formats[store] = make_format("meta", schema, tallies[store].at("spec"));
    }
    auto* meta = dynamic_cast<meta::Meta*>(formats[store].get());
    std::string b = base_of(path);
    size_t c1 = b.find("__"), c2 = b.rfind("__");
    // the capture's cell, query and param come from the request's name, as kvcost predict writes it
    if (c1 == std::string::npos || c1 == c2 || b.find_first_not_of("0123456789", c2 + 2) != std::string::npos ||
        c2 + 2 == b.size())
      throw std::runtime_error(path + ": a request is named <cell>__<query>__<param>.json (the param a number)");
    std::string cell = b.substr(0, c1), query = b.substr(c1 + 2, c2 - c1 - 2), param = b.substr(c2 + 2);
    // a capture row names the plan, as a measured capture's: a request for one body is named <plan>@<body>
    std::string plan_name = query.substr(0, query.find('@'));
    std::string req_sha = sha256_hex(json{{"plan", req["plan"]}, {"params", req["params"]}}.dump());
    for (auto& per : cells) {
      Cell& c = per[cell];
      if (c.store.empty()) c.store = store, c.sf = sf;
      if (c.store != store) throw std::runtime_error("cell " + cell + " names two stores: " + c.store + ", " + store);
      c.reqs[b] = req_sha;
      c.queries.insert(plan_name);
    }
    const json& plan = req["plan"];
    for (auto& body_name : bodies_of(plan)) {
      bodies++;
      const json& body = plan.contains("plans") ? plan["plans"].at(body_name) : plan;
      Plan bound = bind_body(plan, body_name, req["params"], schema);
      std::string what = plan_name + "__" + param + "@" + body_name;
      std::map<std::string, json> ir;
      for (auto& st : body.at("steps")) ir[st.value("name", "")] = st;
      auto an = predict::anchor_step(bound);
      std::string anchor = an ? an->first : "";
      json anchor_vid = an ? json(an->second) : json(nullptr);
      bool write_plan = false;
      for (auto& st : bound.steps) {
        if (is_predicted_op(st.op)) steps++;
        write_plan = write_plan || is_write_op(st.op);
      }
      if (write_plan) for (auto& per : cells) per[cell].writes = true;
      // each step's set facts (level-free). A plan that cannot be bound throws here; the estimate then refuses the
      // body by name below, as it did before the sets existed. A write plan has none (as --accuracy)
      predict::StepSets sets;
      if (!write_plan) try { sets = predict::step_sets(bound); } catch (const std::exception&) {}
      for (size_t li = 0; li < levels.size(); li++) {
        auto& lv = levels[li];
        Cell& cap = cells[li][cell];
        std::string why, error;
        predict::Env env;
        predict::EstimateInput written;   // a write plan: what its earlier steps wrote (note_write)
        if (write_plan) {
          // no cardinality layer: a write step's arguments are the params
        } else if (auto e = estimate_or_refuse(bound, *lv.st, rules, &why, &refused)) {
          env = std::move(*e);
        } else {
          std::cerr << "not estimated (the whole body, " << lv.source << "): " << cell << " " << what << ": " << why << "\n";
          refusals.push_back({{"source", lv.source}, {"cell", cell}, {"body", what}, {"why", why}});
          error = "refused: " + why;
        }
        capture::Row run = {{"sf", sf}, {"cell", cell}, {"query", plan_name}, {"param_idx", param}, {"body", body_name},
                            {"measure_only", "0"}, {"body_sha", bound.body_sha}, {"measured_as", "predicted"}};
        std::map<std::string, double> sum;
        int64_t kv_idx = -1;   // step_idx numbers the KV steps only, as a measured capture's (grid_bodies: kind kv)
        for (auto& st : bound.steps) {
          if (!is_predicted_op(st.op)) continue;
          std::string i = std::to_string(++kv_idx);
          bool write = is_write_op(st.op);
          json step = {{"name", st.name}, {"op", st.op}, {"args", predict::bound_args(st.args, bound.bindings)}};
          try {
            bool with_anchor = lv.st == &full;   // level predicted is not handed the parameter at all
            predict::EstimateInput in;
            if (write) {
              in.created = written.created;
              in.added = written.added;
            } else {
              in = inputs_b(st.op, st.name, env, ir, sets, *lv.st, with_anchor ? anchor : "",
                            with_anchor ? anchor_vid : json(nullptr), what, hops_tagged);
            }
            json p;
            try {
              p = meta->predict_step_estimate(step, in, *lv.st, tallies[store]);
            } catch (...) {
              if (write) note_write(st.op, step["args"], written);   // later steps see this write, as in --accuracy
              throw;
            }
            if (write) note_write(st.op, step["args"], written);
            double r = write ? 1 : rows_assumed(in, env, st.name);   // a write step: one row (as --accuracy)
            capture::Row srow = {{"query", plan_name}, {"param_idx", param}, {"body", body_name},
                                 {"step_idx", i}, {"step", st.name}, {"op", st.op}, {"kind", "kv"},
                                 {"rows", std::isnan(r) ? "" : num(r)}};
            for (auto& l : lanes) srow[l] = num(p.at(l).get<double>());
            for (auto& l : write_lanes) srow[l] = num(p.at(l).get<double>());   // a read step's are 0
            if (cap.writes) for (auto& l : lc_lanes) srow[l] = num(p.at(l).get<double>());
            for (auto& l : run_lanes) sum[l] += p.at(l).get<double>();
            for (auto& l : write_lanes) sum[l] += p.at(l).get<double>();
            for (auto& l : lc_lanes) sum[l] += p.at(l).get<double>();
            // one part row per store part the step reads or writes; the parts must add up to the step's lanes (a
            // code defect if not: the run stops). In a cell with writes a part row carries the write lanes too
            // (capture::write_part_cols); a read-only cell's columns do not change
            std::map<std::string, double> psum;
            for (auto& pr : p.at("parts")) {
              capture::Row prow = {{"query", plan_name}, {"param_idx", param}, {"body", body_name},
                                   {"step_idx", i}, {"step", st.name}, {"op", st.op},
                                   {"part", pr[0].get<std::string>()}};
              for (auto& l : run_lanes) prow[l] = num(pr[1].at(l).get<double>());
              if (cap.writes) for (auto& l : write_lanes) prow[l] = num(pr[1].at(l).get<double>());
              if (cap.writes) for (auto& l : lc_lanes) prow[l] = num(pr[1].at(l).get<double>());
              for (auto& l : lanes) psum[l] += pr[1].at(l).get<double>();
              for (auto& l : write_lanes) psum[l] += pr[1].at(l).get<double>();
              for (auto& l : lc_lanes) psum[l] += pr[1].at(l).get<double>();
              cap.parts.push_back(std::move(prow));
            }
            std::vector<std::string> checked = lanes;
            checked.insert(checked.end(), write_lanes.begin(), write_lanes.end());
            checked.insert(checked.end(), lc_lanes.begin(), lc_lanes.end());
            for (auto& l : checked) {
              double v = p.at(l).get<double>();
              if (std::fabs(psum[l] - v) > 1e-9 * std::max(1.0, std::fabs(v))) {
                std::cerr << "predict --plans: the parts of " << cell << " " << what << " " << st.name << " " << st.op
                          << " sum to " << psum[l] << " " << l << ", the step to " << v << " (a defect)\n";
                return 2;
              }
            }
            cap.steps.push_back(std::move(srow));
          } catch (const std::exception& ex) {
            std::cerr << "not estimated (" << lv.source << "): " << cell << " " << what << " " << st.name << " " << st.op
                      << ": " << ex.what() << "\n";
            lv.not_estimated++;
            if (error.empty()) error = "step " + st.name + " not estimated: " + ex.what();
          }
        }
        double rb = 0;
        for (auto& l : run_lanes) run[l] = num(sum[l]);
        if (cap.writes) for (auto& l : write_lanes) run[l] = num(sum[l]);   // written only in a cell with writes
        if (cap.writes) for (auto& l : lc_lanes) run[l] = num(sum[l]);
        for (auto& l : read_lanes) rb += sum[l];
        run["read_bytes"] = num(rb);
        run["ok"] = error.empty() ? "1" : "0";
        run["error"] = error;
        cap.runs.push_back(std::move(run));
      }
    }
  }
  json prov = provenance();
  // a cell's identity: the call's inputs, its tally, its spec and its request set (every request's name, plan and
  // params). A measured cell is written whole for one identity and records all of it (grid_bodies.write_cell); so is a
  // predicted one, and a capture made from another identity is refused (unless --fresh), never partly replaced
  auto identity = [&](const Cell& c, size_t li) {
    std::string all;
    for (auto& [name, sha] : c.reqs) all += name + "=" + sha + "\n";
    json in = inputs;
    if (li == 0) in.erase("anchor_stats");
    in["requests"] = {{"count", c.reqs.size()}, {"queries", json(c.queries)}, {"sha256", sha256_hex(all)}};
    std::string tally = tally_dir + "/" + c.store + ".json";
    return json{{"inputs", in}, {"tally", {{"path", file_name(tally)}, {"sha256", capture::file_sha256(tally)}}},
                {"spec", tallies[c.store].at("spec")}};
  };
  // every capture this call writes is checked before any file is written: none is left half-replaced
  for (size_t li = 0; li < 2; li++) {
    if (out.dir[li].empty()) continue;
    for (auto& [cell, c] : cells[li]) {
      std::string rec = out.dir[li] + "/counts/" + cell + ".json";
      if (!std::filesystem::exists(rec)) continue;
      json old = load_json(rec), now = identity(c, li);
      json was = old.value("inputs", json::object());
      std::vector<std::string> moved;
      if (was.value("stats", json::object()).value("sha256", "") != now["inputs"]["stats"]["sha256"])
        moved.push_back("stats " + was.value("stats", json::object()).value("sha256", "?").substr(0, 12) + " -> " +
                        now["inputs"]["stats"]["sha256"].get<std::string>().substr(0, 12));
      // the anchor level only: a population capture with an anchor level still records them, and never read them
      if (li == 1 &&
          was.value("anchor_stats", json::object()).value("sha256", "") != now["inputs"]["anchor_stats"]["sha256"])
        moved.push_back("anchor statistics " +
                        was.value("anchor_stats", json::object()).value("sha256", "?").substr(0, 12) + " -> " +
                        now["inputs"]["anchor_stats"]["sha256"].get<std::string>().substr(0, 12));
      if (was.value("rules_off", json::array()) != now["inputs"]["rules_off"])
        moved.push_back("rules off " + was.value("rules_off", json::array()).dump() + " -> " +
                        now["inputs"]["rules_off"].dump());
      json wr = was.value("requests", json::object());
      if (wr.value("sha256", "") != now["inputs"]["requests"]["sha256"])
        moved.push_back("requests (" + std::to_string(wr.value("count", 0)) + " of queries " +
                        wr.value("queries", json::array()).dump() + " -> " +
                        now["inputs"]["requests"]["count"].dump() + " of " + now["inputs"]["requests"]["queries"].dump() +
                        ": another query set, param count, body set or param value)");
      if (old.value("tally", json::object()).value("sha256", "") != now["tally"]["sha256"])
        moved.push_back("tally " + old.value("tally", json::object()).value("sha256", "?").substr(0, 12) + " -> " +
                        now["tally"]["sha256"].get<std::string>().substr(0, 12));
      if (old.value("spec", json()) != now["spec"]) moved.push_back("spec");
      if (!moved.empty()) {
        std::cerr << "predict --plans: " << (out.fresh ? "replacing (--fresh) " : "refused: ") << rec
                  << (out.fresh ? ", made from other inputs: " : " was made from other inputs: ");
        for (size_t i = 0; i < moved.size(); i++) std::cerr << (i ? "; " : "") << moved[i];
        if (out.fresh) { std::cerr << "\n"; continue; }
        std::cerr << ". Name another capture dir, or --fresh to replace it\n";
        return 2;
      }
    }
  }
  double wall_s = std::chrono::duration<double>(std::chrono::steady_clock::now() - t_start).count();
  for (size_t li = 0; li < 2; li++) {
    if (out.dir[li].empty()) continue;
    for (auto* kind : {"counts", "steps", "parts"}) std::filesystem::create_directories(out.dir[li] + "/" + kind);
    for (auto& [cell, c] : cells[li]) {
      auto key = [](const capture::Row& r) {   // the canonical order: query, param, body, step
        return std::make_tuple(r.at("query"), std::stoll(r.at("param_idx")), r.at("body"),
                               r.count("step_idx") ? std::stoll(r.at("step_idx")) : -1);
      };
      for (auto* v : {&c.runs, &c.steps, &c.parts})
        std::stable_sort(v->begin(), v->end(), [&](auto& a, auto& b) { return key(a) < key(b); });
      // a cell learns it holds writes at its first write request: rows written before then get the write lanes as 0
      if (c.writes)
        for (auto* v : {&c.runs, &c.parts, &c.steps})
          for (auto& r : *v) {
            if (v != &c.steps) for (auto& l : write_lanes) r.emplace(l, "0");
            for (auto& l : lc_lanes) r.emplace(l, "0");
          }
      capture::write_csv(out.dir[li] + "/counts/" + cell + ".csv", c.writes ? capture::write_run_cols() : capture::run_cols(),
                         c.runs);
      capture::write_csv(out.dir[li] + "/steps/" + cell + ".csv", c.writes ? capture::write_step_cols() : capture::step_cols(),
                         c.steps);
      capture::write_csv(out.dir[li] + "/parts/" + cell + ".csv", c.writes ? capture::write_part_cols() : capture::part_cols(),
                         c.parts);
      json id = identity(c, li);
      json rec = {{"cell", cell}, {"sf", c.sf}, {"measured_as", "predicted"}, {"source", levels[li].source},
                  {"complete", true}, {"store", c.store},
                  {"inputs", id["inputs"]}, {"tally", id["tally"]},
                  {"spec", id["spec"]}, {"schema", {{"path", file_name(schema_path)}, {"hash", schema.hash()}}},
                  {"runs", c.runs.size()}, {"steps", c.steps.size()}, {"refused", refusals},
                  {"wall_s", std::round(wall_s * 10) / 10}, {"binary", prov}};
      for (auto& [k, v] : out.record_fields.items()) rec[k] = v;   // checked at the start: no field of the record's own
      // the record is written last: a capture with a record is complete
      std::ofstream f(out.dir[li] + "/counts/" + cell + ".json");
      f << rec.dump(2) << "\n";
      if (!f) throw std::runtime_error("cannot write the record of " + cell);
    }
  }
  std::cerr << "predict --plans: binary " << prov.value("binary_git_sha", "").substr(0, 10)
            << dirty_note(prov) << ", " << reqs.size() << " requests, " << bodies
            << " bodies, " << steps << " KV steps, " << (steps - levels[0].not_estimated) << " predicted, "
            << levels[0].not_estimated << " not estimated; "
            << (anchor_level ? "with the anchor statistics " + std::to_string(steps - levels[1].not_estimated) +
                                   " predicted"
                             : std::string("no anchor level (no --anchor-stats)"))
            << " (no store opened); " << refused << " (body, level) refused (named above)\n";
  for (size_t li = 0; li < 2; li++)
    if (!out.dir[li].empty())
      std::cerr << "predict --plans: " << levels[li].source << " capture " << out.dir[li] << ": " << cells[li].size()
                << " cells\n";
  if (refused) return 3;   // a statistic missing: every other body was still priced
  return steps > levels[0].not_estimated ? 0 : 1;
}

// the requests run by `threads` workers, each with its own session, taking the requests one store at a time (a store is
// opened by one session only: RocksDB locks it); `one(session, path, parsed request)` gives each request's result, and
// the results come back in the request order whatever the thread that ran each one. R has `rc` and `err`
template <class R, class F>
std::vector<R> run_by_store(const std::vector<std::string>& reqs, int threads, F one) {
  std::vector<json> parsed(reqs.size());
  std::vector<std::vector<size_t>> by_store;   // request indices per store dir, stores in order of first request
  std::map<std::string, size_t> store_at;
  std::vector<R> res(reqs.size());
  for (size_t i = 0; i < reqs.size(); i++) {
    std::ifstream f(reqs[i]);
    if (!f) { res[i].rc = 1; res[i].err = "cannot open " + reqs[i]; return res; }
    parsed[i] = json::parse(f);
    const json& run = parsed[i].contains("run") ? parsed[i]["run"] : json::object();
    if (threads > 1 && (run.contains("remote") || run.value("reuse_distance", false))) {   // process-wide state
      res[i].rc = 1;
      res[i].err = reqs[i] + ": run.remote and run.reuse_distance hold one run's state for the process: --threads 1";
      return res;
    }
    std::string dir = parsed[i].contains("store") ? parsed[i]["store"].value("dir", "") : "";
    auto [it, fresh] = store_at.emplace(dir, by_store.size());
    if (fresh) by_store.emplace_back();
    by_store[it->second].push_back(i);
  }
  std::atomic<size_t> next{0};
  auto work = [&] {
    Session s;
    for (size_t gi; (gi = next++) < by_store.size();)
      for (size_t i : by_store[gi]) {
        try { res[i] = one(s, reqs[i], std::move(parsed[i])); }
        catch (const std::exception& e) { res[i].rc = 1; res[i].err = reqs[i] + ": " + e.what(); }
      }
  };
  int n = std::max(1, std::min<int>(threads, int(by_store.size())));
  std::vector<std::thread> pool;
  for (int t = 1; t < n; t++) pool.emplace_back(work);
  work();
  for (auto& t : pool) t.join();
  return res;
}

// one request's part of `predict --accuracy`: its steps.csv and terms.csv rows, its messages, its counts
struct AccRequest {
  std::string csv, terms_csv, msgs;
  size_t steps = 0, not_estimated = 0, truth_lanes = 0, truth_differ = 0, costed = 0;
  std::map<std::string, size_t> terms_from_statistics;   // per truth-card level
  int refused = 0;   // (body, level) pairs a missing statistic refused
  int rc = 0;        // 1: the run failed
  std::string err;
};

// what every request of `predict --accuracy` shares: the statistics (declared once per schema, before any estimate reads
// them), the tallies (loaded once per store), the rules
struct AccShared {
  predict::DatasetStats dstats, bare;   // with the anchor statistics (the truth-card levels, predicted_anchor); without (predicted)
  const predict::Rules& rules;
  std::string tally_dir;
  std::mutex mu;
  std::set<std::string> declared;       // the schemas the statistics were declared for
  std::map<std::string, json> tallies;  // per store name
  const json& tally(const std::string& store) {
    std::lock_guard<std::mutex> g(mu);
    if (!tallies.count(store)) tallies[store] = load_json(tally_dir + "/" + store + ".json");
    return tallies[store];
  }
  void declare(const Schema& schema, const std::string& scale, const std::string& path) {
    std::lock_guard<std::mutex> g(mu);
    dstats.require_schema(schema.hash(), "the store of " + path);   // the store's schema, as its manifest pins it
    dstats.require_scale(scale, "the store of " + path);            // the store's scale (its manifest)
    if (!declared.insert(schema.hash()).second) return;
    dstats.declare_date_props(date_props(schema));
    bare.declare_date_props(date_props(schema));
    dstats.declare_prop_types(prop_types(schema));
    bare.declare_prop_types(prop_types(schema));
  }
};

// a count term of the step's truth (its part empty: "<name>@"), refused by name when the truth card gave none
double true_count(const predict::Terms& t, const std::string& name) {
  auto it = t.find(name + "@");
  if (it == t.end()) throw std::runtime_error("the truth card gave no count '" + name + "'");
  return it->second;
}

// the inputs to a KV read's estimate at a level that takes terms from the truth card (predict/terms.h kLevels): its counts
// from the run's traced arguments and the truth card `t` (already narrowed to the level's kinds: terms_for), and those terms
predict::EstimateInput true_terms_input(const json& step, const std::string& name, const predict::Terms& t,
                                        const std::map<std::string, json>& ir, const predict::StepSets& sets,
                                        const std::map<std::string, const json*>& resp_steps, const std::string& anchor) {
  const std::string op = step.value("op", "");
  const json& a = step["args"];
  predict::EstimateInput in;
  // the hop that made a set, and its true distinct sources: Chunked locality
  auto producer = [&](const json& arg) {
    const predict::StepSet* ss = set_of(sets, arg);
    if (!ss || !ir.count(ss->hop_via_props)) return;
    auto rs = resp_steps.find(ss->hop_via_props);
    if (rs != resp_steps.end())
      in.producer = producer_input(ir.at(ss->hop_via_props), *ss, double(distinct_count((*rs->second)["args"]["srcs"])));
  };
  if (op == "read_neighbors") {
    in.n = double(distinct_count(a["srcs"]));
    in.entries = true_count(t, "entries");
    in.label_mix = label_mix(step, "srcs");
    producer(ir.at(name)["srcs"]);
    const json& src = ir.at(name)["srcs"];
    bool anch = src.is_object() && src.contains("ref") && src["ref"] == anchor;
    if (anch && !a["srcs"].empty()) in.anchor_vid = a["srcs"][0].get<std::string>();   // the bound vid: a parameter
  } else if (op == "read_vertex_props") {
    in.n = double(distinct_count(a["vids"]));
    in.label_mix = label_mix(step, "vids");
    producer(ir.at(name)["vids"]);
  } else if (op == "read_top_k_by_index") {
    in.crossed = true_count(t, "crossed");
    in.winners = true_count(t, "winners");
  } else if (op == "read_index_eq" || op == "read_index_range") {
    in.matches = true_count(t, "matches");
  } else if (op == "read_filter_by_label_probe" || op == "read_filter_by_label_index") {
    double untagged = 0.0;   // the distinct inputs that carry no label: each probes; one is enough to walk the membership
    for (auto& [k, c] : label_mix(step, "vids")) if (k == "?") untagged = c;
    in.probes = untagged;
    in.walk = untagged > 0 ? 1.0 : 0.0;
  } else if (op == "read_closure" || op == "read_chase") {
    in.nodes = true_count(t, "nodes");
    in.links = true_count(t, "links");
  } else if (op == "read_edge_exists") {
    in.rows = true_count(t, "rows");
    in.nothers = true_count(t, "nothers");
    in.nvia = t.count("nvia@") ? t.at("nvia@") : 0.0;
    in.nvnbrs = t.count("nvnbrs@") ? t.at("nvnbrs@") : 0.0;
    for (size_t i = 0; i < a.at("reads").size(); i++) {
      in.eown.push_back(true_count(t, "eown" + std::to_string(i)));
      in.eprobe.push_back(true_count(t, "eprobe" + std::to_string(i)));
    }
    if (a.contains("others") && a["others"].is_array() && a["others"].size() == 1)
      in.anchor_vid = a["others"][0].get<std::string>();   // one given anchor: a parameter, not a count
  }
  in.true_terms = t;
  return in;
}

// one request of `predict --accuracy`: the run (measured), then per KV step its truth card, costed by the gate's rules
// (`truth`: the gate's check) and giving the step's terms, and the levels of predict/terms.h kLevels
AccRequest accuracy_request(Session& s, const std::string& path, json req, AccShared& sh) {
  AccRequest r;
  static const std::vector<std::string> lanes = lane_names([](const Lane&) { return true; });
  req["run"]["trace_args"] = true;
  req["run"]["parts"] = true;
  req["run"]["P"] = 1;
  bool hops_tagged = hops_tag(req);
  std::string store = base_of(req["store"]["dir"].get<std::string>() + ".x");   // the store dir's name
  const json& tally = sh.tally(store);
  // <cell>__<query>__<param>.json: the estimates are keyed <query>__<param>@<body>, the body this run picked
  std::string b = base_of(path);
  size_t c1 = b.find("__"), c2 = b.rfind("__");
  std::string cell = b.substr(0, c1), query = b.substr(c1 + 2, c2 - c1 - 2), param = b.substr(c2 + 2);
  json resp = s.handle(req);
  if (!resp["error"].is_null()) { r.rc = 1; r.err = path + ": " + resp["error"].dump(); return r; }
  auto* meta = dynamic_cast<meta::Meta*>(s.format());
  if (!meta) { r.rc = 1; r.err = path + ": predict covers the meta format only"; return r; }
  sh.declare(*s.schema(), s.scale(), path);
  std::ostringstream msgs, csv, tcsv;
  csv.precision(12);
  tcsv.precision(12);
  // the body this run picked (a bundle holds several), and its estimates
  std::string body_name = resp["provenance"].value("body", "");
  const json& plan = req["plan"];
  const json& body = plan.contains("plans") ? plan["plans"].at(body_name) : plan;
  std::string pq = query.substr(0, query.find('@'));   // a fixture request names <plan>@<body>
  // the cardinality layer's env for this body (predict/cardinality.h); a body it cannot estimate: every step refuses.
  // env: with the anchor statistics (predicted_anchor); env_b: without (predicted)
  std::string why;
  predict::Env env, env_b;
  Plan bound = bind_body(plan, body_name, req["params"], *s.schema());
  // a write plan: its steps name their vertices (the params); no cardinality layer, its steps costed by the write
  // estimate (levels predicted and predicted_anchor)
  bool write_plan = false;
  for (auto& st : body.at("steps")) write_plan = write_plan || is_write_op(st.value("op", ""));
  predict::StepSets sets;   // each step's set facts; a write plan has none (a plan that cannot be bound: none)
  if (!write_plan) try { sets = predict::step_sets(bound); } catch (const std::exception&) {}
  if (write_plan) {
  } else if (auto e = estimate_or_refuse(bound, sh.dstats, sh.rules, &why, &r.refused))
    env = std::move(*e);
  else
    msgs << "not estimated (the whole body): " << cell << " " << query << " " << param << ": " << why << "\n";
  if (write_plan) {
  } else if (auto e = estimate_or_refuse(bound, sh.bare, sh.rules, &why, &r.refused))
    env_b = std::move(*e);
  else
    msgs << "not estimated (the whole body, B): " << cell << " " << query << " " << param << ": " << why << "\n";
  predict::EstimateInput written;            // what the request's earlier write steps wrote (note_write)
  auto an = predict::anchor_step(bound);     // the plan's anchor (the cardinality layer's own)
  std::string anchor = an ? an->first : "";
  json anchor_vid = an ? json(an->second) : json(nullptr);
  std::map<std::string, json> ir;
  for (auto& st : body.at("steps")) ir[st.value("name", "")] = st;
  std::string sha = resp["provenance"].value("body_sha", "");
  meta::WriteReplay replay;   // a write request: what the truth card's rules wrote at its earlier steps (the shadow overlay)
  static const json lc_keys = contract::capture_columns().at("response_keys").at("last_chunk");   // column -> lane
  std::map<std::string, const json*> resp_steps;   // the run's steps by name (a producer's traced arguments)
  for (auto& st : resp["steps"]) resp_steps[st.value("name", "")] = &st;
  for (auto& step : resp["steps"]) {
    if (step.value("kind", "") != "kv") continue;
    std::string name = step.value("name", ""), op = step.value("op", "");
    const json& a = step["args"];
    std::string what = pq + "__" + param + "@" + body_name;
    bool write = is_write_op(op);
    // the truth card: its rules' lanes (the gate's check) and the step's terms; one walk, with the request's replay as the gate
    predict::Terms tt;
    Counts truth = meta->predict_step_truth(step, *s.store(), "", nullptr, nullptr, &replay, &tt);
    // a step the estimate does not cover yet (or lacks an input for) is NOT ESTIMATED: its measured and truth rows only,
    // and its reason in the messages, one line each (the report lists them)
    // each level of predict/terms.h kLevels, in its order: the output rows it assumed (the step's main input count)
    struct LevelOut { const predict::LevelDef* L; json p; double rows = NAN; };
    std::vector<LevelOut> outs;
    bool estimated = true;
    try {
      // a hop with a Chunked direction: its entries are the rows the run's step emitted (the card counts none there)
      if (!write && op == "read_neighbors" && !tt.count("entries@") && step.contains("rows"))
        tt["entries@"] = step["rows"].get<double>();
      for (const predict::LevelDef& L : predict::kLevels) {
        if (L.sampled) continue;         // made by kvcost predict (the sampled capture), not by this pass
        const predict::DatasetStats& stats = L.card || L.anchor ? sh.dstats : sh.bare;
        predict::EstimateInput in;
        double rows = 1;                 // a write step: one row
        if (write) {                     // its vertices from the params; at a truth-card level its terms from the card
          in.created = written.created;
          in.added = written.added;
          if (L.card) {
            in.true_terms = predict::terms_for(L, tt);
            in.level = &L;
          }
        } else if (L.card) {             // the counts, and the terms of the kinds the level takes, from the truth card
          in = true_terms_input(step, name, predict::terms_for(L, tt), ir, sets, resp_steps, anchor);
          in.level = &L;
          rows = rows_assumed(in, env, name);
        } else {                         // the cardinality layer, with or without the anchor statistics
          in = inputs_b(op, name, L.anchor ? env : env_b, ir, sets, sh.dstats, L.anchor ? anchor : "",
                        L.anchor ? anchor_vid : nullptr, what, hops_tagged);
          rows = rows_assumed(in, L.anchor ? env : env_b, name);
        }
        outs.push_back({&L, meta->predict_step_estimate(step, in, stats, tally), rows});
      }
    } catch (const std::exception& ex) {
      msgs << "not estimated: " << cell << " " << query << " " << param << " " << name << " " << op << ": " << ex.what()
           << "\n";
      estimated = false;
      r.not_estimated++;
    }
    json meas = json::object();   // the run's step: the op counts at the top, the byte lanes under "bytes"
    for (auto& l : lanes) {
      const json* v = step.contains(l) ? &step[l] : step["bytes"].contains(l) ? &step["bytes"][l] : nullptr;
      if (!v) { r.rc = 1; r.err = path + " step " + name + ": the run gave no lane " + l; return r; }
      meas[l] = v->get<double>();
    }
    if (!step.contains("chunks")) {   // the chunks column: a silent 0 would read as no Chunked read
      r.rc = 1;
      r.err = path + " step " + name + ": the run gave no chunk statistics (emit.kvpair_stats off?)";
      return r;
    }
    json tr = json::object();   // the truth row, and the gate's check on it: every lane equal to the run's
    auto check = [&](const std::string& lane, double t) {
      tr[lane] = t, r.truth_lanes++;
      if (t == meas[lane].get<double>()) return;
      r.truth_differ++;
      msgs << "truth differs: " << path << " step " << name << " lane " << lane << ": measured "
           << meas[lane].get<double>() << ", truth " << t << "\n";
    };
    for (const Lane& L : kLanes) check(L.name, double(truth.*L.field));
    const json tlc = truth.last_chunk.to_json();   // a write's last chunk, as the gate compares it (no touch: 0)
    for (auto& [col, key] : lc_keys.items()) {
      meas[col] = step.contains("last_chunk") ? step["last_chunk"].value(key.get<std::string>(), 0.0) : 0.0;
      check(col, tlc.at(key.get<std::string>()).get<double>());
    }
    // chunks: the run's charged Chunked chunks (measured, and truth: the same replay), each level's closed form (a write's
    // estimate counts none, so its predicted rows show 0 beside the run's count)
    double chunks_run = step["chunks"].value("charged", 0.0);
    struct Row { const char* src; json* c; double rows; double chunks; std::string from_stats; };
    std::vector<Row> rows = {{"measured", &meas, step.value("rows", 0.0), chunks_run, ""},
                             {"truth", &tr, step.value("rows", 0.0), chunks_run, ""}};
    note_write(op, a, written);
    if (estimated) {
      r.costed++;
      for (LevelOut& o : outs) {
        std::string fs;
        if (o.L->card) {
          double v = o.p.value("terms_from_statistics", 0.0);
          r.terms_from_statistics[o.L->name] += size_t(v);
          fs = num(v);
        }
        rows.push_back({o.L->name, &o.p, o.rows, o.p.value("chunks", 0.0), fs});
      }
    }
    for (auto& row : rows) {
      csv << cell << "," << query << "," << param << "," << sha << "," << name << "," << op << "," << row.src;
      for (auto& l : lanes) csv << "," << row.c->at(l).get<double>();
      csv << ",";
      if (!std::isnan(row.rows)) csv << row.rows;
      csv << "," << row.chunks << "," << row.from_stats;
      for (auto& [col, key] : lc_keys.items()) csv << "," << (row.c->contains(col) ? row.c->at(col).get<double>() : 0.0);
      csv << "\n";
      if (!row.c->contains("terms")) continue;   // terms.csv: every term each level's formulas read
      for (auto& t : (*row.c)["terms"]) {
        std::string k = t[0].get<std::string>();
        size_t at = k.find('@');
        tcsv << cell << "," << query << "," << param << "," << sha << "," << name << "," << op << "," << row.src << ","
             << k.substr(0, at) << "," << k.substr(at + 1) << "," << t[1].get<double>() << ","
             << (t[2].get<bool>() ? "truth" : "statistics") << "\n";
      }
    }
    r.steps++;
  }
  r.csv = csv.str();
  r.terms_csv = tcsv.str();
  r.msgs = msgs.str();
  return r;
}

// --levels-out FILE: the levels of predict/terms.h kLevels as JSON, the data file the analysis reads (python/kvcost/
// predict_accuracy/report.py, predict_formulas.py); 1 when it cannot be written
int write_levels(const std::string& path) {
  json lv = json::array();
  for (const predict::LevelDef& L : predict::kLevels)
    lv.push_back({{"name", L.name}, {"card", L.card}, {"sizes", L.sizes}, {"chunks", L.chunks}, {"anchor", L.anchor},
                  {"chain", L.chain}, {"measures", L.measures}, {"meaning", L.meaning}, {"sampled", L.sampled},
                  {"pick", L.pick}});
  std::ofstream lf(path);
  if (lf << lv.dump(2) << "\n") return 0;
  std::cerr << "predict: cannot write --levels-out " << path << "\n";
  return 1;
}

// the summary line's level names, in kLevels order, and per truth-card level the terms the statistics filled
std::pair<std::string, std::string> level_lists(const std::map<std::string, size_t>& from_stats) {
  std::string levels, filled;
  for (const predict::LevelDef& L : predict::kLevels) {
    if (L.sampled) continue;   // not this pass's
    levels += std::string(levels.empty() ? "" : ", ") + L.name;
    auto it = from_stats.find(L.name);
    if (L.card) filled += std::string(filled.empty() ? "" : ", ") + L.name + " " +
                          std::to_string(it == from_stats.end() ? 0 : it->second);
  }
  return {levels, filled};
}

// `predict --accuracy`: each request runs once (the measured side, traced), then each KV step is costed --
// truth: the gate's rules on the step's truth card (the run's arguments + raw store reads); it must equal the run on every
//   lane (the exact gate's check, printed as one line);
// then at each level of predict/terms.h kLevels, in its order, through the one estimate: true_chunks, true_terms and
//   true_counts take their counts and the term kinds they name from that truth card, the rest from the statistics;
//   predicted takes every input from the population statistics (predict mode proper), predicted_anchor adds the anchor
//   statistics, which are conditioned on the query's parameter (DatasetStats::without_anchor_stats). A write step: its
//   truth card with the request's replay, its terms from its read-modify-writes, its vertices from the params at every
//   level (no cardinality layer); the check covers its write and last-chunk lanes too.
// One row per (KV step, level) on stdout as CSV: the lanes, rows, chunks (the run's charged chunks; each level's closed
// form), on a truth-card level's rows the terms the statistics filled (no item of the step's kind), and the last-chunk
// lane (inputs/contract/capture_columns.json "last_chunk": a write's, 0 on a read); --terms-out FILE: one row
// per term each level read; --levels-out FILE: the levels (kLevels) as JSON. kvcost costs them (python/kvcost/predict_accuracy/report.py, write_report.py,
// predict_formulas.py). --threads N: N sessions, one store each at a time; the output is the same bytes at any N.
int accuracy(const std::string& stats_path, const std::string& anchor_path, const predict::Rules& rules,
             const std::string& tally_dir, const std::vector<std::string>& reqs, int threads,
             const std::string& terms_out, const std::string& levels_out) {
  predict::DatasetStats loaded = predict::DatasetStats::load(stats_path, anchor_path);
  AccShared sh{loaded, loaded.without_anchor_stats(), rules, tally_dir, {}, {}, {}};
  json prov = provenance();
  static const std::vector<std::string> lanes = lane_names([](const Lane&) { return true; });
  std::vector<AccRequest> res = run_by_store<AccRequest>(
      reqs, threads, [&](Session& s, const std::string& path, json req) { return accuracy_request(s, path, std::move(req), sh); });
  std::cout << "cell,query,param,body_sha,step,op,source";
  for (auto& l : lanes) std::cout << "," << l;
  std::cout << ",rows,chunks,terms_from_statistics";
  for (auto& col : contract::columns("last_chunk")) std::cout << "," << col;   // the write's last-chunk lane
  std::cout << std::endl;
  std::ofstream tf;
  if (!levels_out.empty() && write_levels(levels_out)) return 1;
  if (!terms_out.empty()) {
    tf.open(terms_out);
    if (!tf) { std::cerr << "predict --accuracy: cannot write --terms-out " << terms_out << "\n"; return 1; }
    tf << "cell,query,param,body_sha,step,op,level,term,part,value,source\n";
  }
  size_t steps = 0, not_estimated = 0, truth_lanes = 0, truth_differ = 0, costed = 0;
  std::map<std::string, size_t> from_stats;
  int refused = 0;
  for (auto& r : res) {
    std::cerr << r.msgs;
    if (r.rc) { std::cerr << r.err << "\n"; return r.rc; }
    std::cout << r.csv;
    if (tf) tf << r.terms_csv;
    steps += r.steps;
    not_estimated += r.not_estimated;
    truth_lanes += r.truth_lanes;
    truth_differ += r.truth_differ;
    costed += r.costed;
    for (auto& [lv, n] : r.terms_from_statistics) from_stats[lv] += n;
    refused += r.refused;
  }
  auto [levels, filled] = level_lists(from_stats);
  std::cerr << "predict --accuracy: binary " << prov.value("binary_git_sha", "").substr(0, 10) << dirty_note(prov) << ", "
            << reqs.size() << " requests, " << steps << " KV steps, " << costed << " costed at " << levels
            << ", " << not_estimated << " not estimated; " << refused << " (body, level) refused (named above); terms "
            << "the statistics filled at a level that takes them from the truth card (no item of their kind in the "
            << "step): " << filled << "\n";
  std::cerr << "exact gate on these steps: " << truth_differ << " of " << truth_lanes << " lanes differ\n";
  if (truth_differ) return 1;   // first: a refusal must not hide a truth that differs
  if (refused) return 3;        // a statistic missing: every other body was still costed
  return steps > not_estimated ? 0 : 1;
}
}  // namespace

// --- predict --gate ---------------------------------------------------------------------------------------------
namespace {
// per planted rule, from the same steps as the gate: lanes that differ, the first one, or its refusal
struct PlantResult { uint64_t differ = 0, repredicted = 0; json first = nullptr; std::string refused; };

// one request's part of the gate: its totals, differences, cases per rule, plants and steps.csv rows. The requests are
// independent (each runs in shadow on its own store), so the gate adds these up in the request order, whatever the
// thread that ran each one
struct GateRequest {
  json r;                                   // {request, plan, body_sha, steps}
  uint64_t steps = 0, lanes = 0, differ = 0, with_cost = 0, walk_from_counters = 0;
  json diffs = json::array();
  json coverage = json::object();
  std::vector<PlantResult> planted;
  std::string csv;                          // its steps.csv rows
  int rc = 0;                               // 1: the run failed; 3: the gate's own rules refused a step
  std::string err;
};

uint64_t measured_lane(const json& step, const Lane& L) {
  return L.in_bytes ? step["bytes"][L.name].get<uint64_t>() : step[L.name].get<uint64_t>();
}
// every lane of a step where the run and `q` differ: (lane name, measured, predicted), in kLanes then kLcLanes order
std::vector<std::tuple<std::string, uint64_t, uint64_t>> lane_diffs(const json& step, const Counts& q) {
  std::vector<std::tuple<std::string, uint64_t, uint64_t>> out;
  for (const Lane& L : kLanes)
    if (measured_lane(step, L) != q.*L.field) out.emplace_back(L.name, measured_lane(step, L), q.*L.field);
  for (const LcLane& L : kLcLanes)
    if (measured_lc(step, L.name) != q.last_chunk.*L.field)
      out.emplace_back(std::string("last_chunk.") + L.name, measured_lc(step, L.name), q.last_chunk.*L.field);
  return out;
}
json diff_json(const std::string& path, const json& step, const std::tuple<std::string, uint64_t, uint64_t>& d) {
  return {{"request", path}, {"step", step.value("name", "")}, {"op", step.value("op", "")},
          {"lane", std::get<0>(d)}, {"measured", std::get<1>(d)}, {"predicted", std::get<2>(d)}};
}

std::string csv_field(const std::string& v) {   // quoted when it holds a comma or a quote
  if (v.find_first_of(",\"\n") == std::string::npos) return v;
  std::string q = "\"";
  for (char c : v) q += c == '"' ? std::string("\"\"") : std::string(1, c);
  return q + "\"";
}
// steps.csv: two rows per KV step, `measured` (the run) and `truth` (predicted from its truth card), every lane. The
// keys are the request's dir and file name (kvevaluator.measure.predict_requests names them), the body's sha and the
// step
std::string steps_csv_header() {
  std::string h = "request_set,request,body_sha,kv_step,step,op,rows,chunks,source";
  for (const Lane& L : kLanes) h += std::string(",") + L.name;
  for (const LcLane& L : kLcLanes) h += std::string(",last_chunk.") + L.name;
  return h + "\n";
}
std::string steps_csv_rows(const std::string& key, uint64_t kv_step, const json& step, const Counts& p) {
  std::string chunks =
      step.contains("chunks") && step["chunks"].contains("charged") ? step["chunks"]["charged"].dump() : "";
  std::string k = key + std::to_string(kv_step) + "," + csv_field(step.value("name", "")) + "," +
                  csv_field(step.value("op", "")) + "," + (step.contains("rows") ? step["rows"].dump() : "") + "," +
                  chunks + ",";
  std::string m = k + "measured", t = k + "truth";
  for (const Lane& L : kLanes) {
    m += "," + std::to_string(measured_lane(step, L));
    t += "," + std::to_string(p.*L.field);
  }
  for (const LcLane& L : kLcLanes) {
    m += "," + std::to_string(measured_lc(step, L.name));
    t += "," + std::to_string(p.last_chunk.*L.field);
  }
  return m + "\n" + t + "\n";
}

// each planted rule on one step: re-predicted when the step's rules consulted it (a write step: always, so its own
// replay follows its writes), else the gate's own prediction `p`
void plant_step(const meta::Meta& meta, Store& store, const std::string& path, const json& step, const Counts& p,
                bool write, const std::set<std::string>& asked, const std::vector<std::string>& plants,
                std::vector<meta::WriteReplay>& replays, std::vector<PlantResult>& planted) {
  for (size_t k = 0; k < plants.size(); k++) {
    PlantResult& pr = planted[k];
    if (!pr.refused.empty()) continue;   // a plant that refused once is not tried again: it is not caught
    Counts q = p;                        // not consulted: the same rules ran, the same prediction
    if (asked.count(plants[k]) || write) {
      pr.repredicted++;
      try {
        q = meta.predict_step_truth(step, store, plants[k], nullptr, nullptr, &replays[k]);
      } catch (const std::exception& e) {
        pr.refused = path + " step " + step.value("name", "") + ": " + e.what();
        continue;
      }
    }
    for (auto& d : lane_diffs(step, q))
      if (pr.differ++ == 0) pr.first = diff_json(path, step, d);
  }
}

GateRequest gate_request(Session& s, const std::string& path, json req, const std::vector<std::string>& plants,
                         bool want_csv) {
  GateRequest g;
  g.planted.resize(plants.size());
  req["run"]["trace_args"] = true;
  req["run"]["parts"] = true;
  req["run"]["P"] = 1;
  json resp = s.handle(req);
  if (!resp["error"].is_null()) { g.rc = 1; g.err = path + ": " + resp["error"].dump(); return g; }
  auto* meta = dynamic_cast<meta::Meta*>(s.format());
  if (!meta) { g.rc = 1; g.err = path + ": predict covers the meta format only"; return g; }
  std::string body_sha = resp["provenance"].value("body_sha", "");
  g.r = {{"request", path}, {"plan", resp.value("plan", "")}, {"body_sha", body_sha}, {"steps", 0}};
  std::string dir = std::filesystem::path(path).parent_path().filename().string();
  std::string key = csv_field(dir) + "," + csv_field(base_of(path)) + "," + csv_field(body_sha) + ",";
  // a write request: the KV pairs the card wrote at earlier steps (the run's shadow overlay), one per plant too
  meta::WriteReplay replay;
  std::vector<meta::WriteReplay> plant_replays(plants.size());
  for (auto& step : resp["steps"]) {
    if (step.value("kind", "") != "kv") continue;
    Counts p;
    std::set<std::string> asked;   // the plants this step's rules consulted: only they can change its prediction
    try {
      p = meta->predict_step_truth(step, *s.store(), "", &g.coverage, &asked, &replay);
    } catch (const std::exception& e) {   // a refusal (an uncovered op or layout): exit 3
      g.rc = 3;
      g.err = path + " step " + step.value("name", "") + ": " + e.what();
      return g;
    }
    plant_step(*meta, *s.store(), path, step, p, is_write_op(step.value("op", "")), asked, plants, plant_replays,
               g.planted);
    if (want_csv) g.csv += steps_csv_rows(key, g.steps, step, p);
    g.steps++;
    if (step["S"].get<uint64_t>() + step["G"].get<uint64_t>() + step["N"].get<uint64_t>() > 0) g.with_cost++;
    if (step.value("op", "") == "read_top_k_by_index") g.walk_from_counters++;
    g.lanes += std::size(kLanes) + std::size(kLcLanes);
    for (auto& d : lane_diffs(step, p)) {
      g.differ++;
      g.diffs.push_back(diff_json(path, step, d));
    }
  }
  g.r["steps"] = g.steps;
  return g;
}

// every request run by `threads` workers, each with its own session, taking the requests one store at a time (a store
// is opened by one session only: RocksDB locks it); the results in the request order
std::vector<GateRequest> run_requests(const std::vector<std::string>& reqs, const std::vector<std::string>& plants,
                                      bool want_csv, int threads) {
  return run_by_store<GateRequest>(reqs, threads, [&](Session& s, const std::string& path, json req) {
    return gate_request(s, path, std::move(req), plants, want_csv);
  });
}

// one request's part added to the gate's, as one pass in the request order would: a plant's first refusal ends its
// count
void add_request(json& out, const GateRequest& g, std::vector<PlantResult>& planted) {
  out["requests"].push_back(g.r);
  auto add = [&out](const char* k, uint64_t v) { out[k] = out[k].get<uint64_t>() + v; };
  add("steps_compared", g.steps);
  add("lanes_compared", g.lanes);
  add("lanes_differ", g.differ);
  add("steps_with_cost", g.with_cost);
  add("topk_steps_walk_from_counters", g.walk_from_counters);
  for (auto& d : g.diffs) out["differences"].push_back(d);
  for (auto& [rule, v] : g.coverage.items()) out["coverage"][rule] = out["coverage"].value(rule, 0.0) + v.get<double>();
  for (size_t k = 0; k < planted.size(); k++) {
    PlantResult& pr = planted[k];
    const PlantResult& q = g.planted[k];
    if (!pr.refused.empty()) continue;
    pr.differ += q.differ;
    pr.repredicted += q.repredicted;
    if (pr.first.is_null()) pr.first = q.first;
    pr.refused = q.refused;
  }
}

// the gate's lines on stderr: the totals, the cases per rule, the untested rules, each plant
void report(const json& out, const json& prov, size_t nreqs) {
  std::cerr << "predict --gate: binary " << prov.value("binary_git_sha", "").substr(0, 10) << dirty_note(prov) << ", "
            << nreqs << " requests, " << out["steps_compared"] << " KV steps, " << out["lanes_compared"]
            << " lanes compared, " << out["lanes_differ"] << " differ (" << out["steps_with_cost"]
            << " steps with a cost; " << out["topk_steps_walk_from_counters"]
            << " top-k walks taken from the run's index parts)\n";
  for (auto& [rule, n] : out["coverage"].items()) std::cerr << "  covered " << rule << ": " << n.get<double>() << "\n";
  std::cerr << "  rules with cases " << (meta::Meta::predict_rules().size() - out["untested"].size()) << "/"
            << meta::Meta::predict_rules().size() << "; untested:";
  for (auto& r : out["untested"]) std::cerr << " " << r.get<std::string>();
  std::cerr << (out["untested"].empty() ? " none\n" : "\n");
  size_t caught = 0;
  for (auto& p : out["plants"]) {
    caught += p["caught"].get<bool>();
    std::cerr << "predict --gate --plant " << p["plant"].get<std::string>() << ": ";
    if (!p["refused"].is_null()) std::cerr << "refused (" << p["refused"].get<std::string>() << "): NOT caught\n";
    else std::cerr << p["lanes_differ"] << " lanes differ in the " << p["steps_repredicted"]
                   << " steps its rule predicted: " << (p["caught"].get<bool>() ? "caught" : "NOT caught") << "\n";
  }
  if (!out["plants"].empty())
    std::cerr << "predict --gate: plants caught " << caught << "/" << out["plants"].size() << "\n";
}

// each plant's result; caught = every step predicted and some lane differs (the gate would exit 1): a refusal is not
// a catch
json plants_json(const std::vector<std::string>& plants, const std::vector<PlantResult>& planted, size_t* caught) {
  json out = json::array();
  for (size_t k = 0; k < plants.size(); k++) {
    const PlantResult& pr = planted[k];
    bool c = pr.refused.empty() && pr.differ > 0;
    *caught += c;
    out.push_back({{"plant", plants[k]}, {"caught", c}, {"lanes_differ", pr.differ},
                   {"steps_repredicted", pr.repredicted}, {"first_difference", pr.first},
                   {"refused", pr.refused.empty() ? json(nullptr) : json(pr.refused)}});
  }
  return out;
}

// the gate over every request (run_requests); the output is the same bytes whatever the thread count
int gate(const std::vector<std::string>& reqs, const std::vector<std::string>& plants, const std::string& steps_out,
         int threads) {
  std::vector<GateRequest> res = run_requests(reqs, plants, !steps_out.empty(), threads);
  for (auto& g : res)   // the first request (in order) that failed decides the exit, as one thread would
    if (g.rc) { std::cerr << g.err << "\n"; return g.rc; }
  json prov = provenance();
  json out = {{"binary_git_sha", prov.value("binary_git_sha", "")},
              {"binary_dirty", prov.contains("binary_dirty") ? prov["binary_dirty"] : json(true)},
              {"requests", json::array()}, {"steps_compared", 0}, {"steps_with_cost", 0},
              {"topk_steps_walk_from_counters", 0}, {"coverage", json::object()}, {"lanes_compared", 0},
              {"lanes_differ", 0}, {"differences", json::array()}};
  std::vector<PlantResult> planted(plants.size());
  std::ofstream csv;
  if (!steps_out.empty()) {
    csv.open(steps_out);
    csv << steps_csv_header();
  }
  for (auto& g : res) {
    add_request(out, g, planted);
    if (csv.is_open()) csv << g.csv;
  }
  if (!steps_out.empty() && !(csv.close(), csv)) {
    std::cerr << "predict --gate: writing --steps-out " << steps_out << " failed\n";
    return 1;
  }
  json untested = json::array();   // a rule the requests gave no case: the gate says nothing about it
  for (auto& r : meta::Meta::predict_rules())
    if (!out["coverage"].contains(r)) untested.push_back(r);
  out["untested"] = untested;
  size_t caught = 0;
  out["plants"] = plants_json(plants, planted, &caught);
  std::cout << out.dump(1) << "\n";
  report(out, prov, reqs.size());
  if (out["steps_compared"].get<uint64_t>() == 0 || out["lanes_differ"].get<uint64_t>() > 0) return 1;
  return caught == plants.size() ? 0 : 4;
}
}  // namespace

int cmd_predict(const std::vector<std::string>& args) {
  std::string stats, tally_dir;
  std::vector<std::string> plants;
  std::vector<std::string> reqs;
  std::string schema, anchor_stats;
  predict::Rules rules;
  PlansOut plans_out;
  bool gate_mode = false, acc = false, plans = false;
  std::string steps_out, terms_out, levels_out;
  int threads = 1;
  for (size_t i = 0; i < args.size(); i++) {
    if (args[i] == "--gate") gate_mode = true;
    else if (args[i] == "--steps-out" && i + 1 < args.size()) steps_out = args[++i];
    else if (args[i] == "--terms-out" && i + 1 < args.size()) terms_out = args[++i];
    else if (args[i] == "--levels-out" && i + 1 < args.size()) levels_out = args[++i];
    else if (args[i] == "--threads" && i + 1 < args.size()) {
      threads = std::atoi(args[++i].c_str());
      if (threads < 1) { std::cerr << "predict: --threads takes a count of at least 1\n"; return 2; }
    }
    else if (args[i] == "--accuracy") acc = true;
    else if (args[i] == "--plans") plans = true;
    else if (args[i] == "--schema" && i + 1 < args.size()) schema = args[++i];
    else if (args[i] == "--anchor-stats" && i + 1 < args.size()) anchor_stats = args[++i];
    else if (args[i] == "--rules-off" && i + 1 < args.size()) {   // the cardinality rules to turn off (comma list)
      for (auto& n : split_commas(args[++i])) {
        if (!rules.set(n, false)) { std::cerr << "predict: no cardinality rule named '" << n << "'\n"; return 2; }
        plans_out.rules_off.push_back(n);
      }
    }
    else if (args[i] == "--made-from" && i + 3 < args.size() && args[i + 2] == "--records") {
      // --made-from F --records R: was the tally or statistics file F made from the record stream R? By the rule
      // predict refuses by (util/record_stream.h record_stream_mismatch): 0, else 1 and what differs on stdout
      // (run/predict-accuracy.sh reuses a file only then)
      std::ifstream f(args[i + 1]);
      json d = json::parse(f, nullptr, false);
      if (!d.is_object() || !d.contains("records") || !d["records"].is_object()) {
        std::cout << args[i + 1] << " names no record stream\n";
        return 1;
      }
      std::string why = record_stream_mismatch(d["records"], record_stream_at_open(args[i + 3]));
      if (why.empty()) return 0;
      std::cout << why << "\n";
      return 1;
    }
    else if (args[i] == "--rule-names") {   // the cardinality rule switches (tests/unit/check_predict_rules.py)
      std::cout << json(predict::Rules::names()).dump() << "\n";
      return 0;
    }
    else if (args[i] == "--capture-columns") {   // the capture's column lists (inputs/contract/capture_columns.json)
      std::cout << json{{"counts", capture::run_cols()}, {"counts_writes", capture::write_run_cols()},
                        {"steps", capture::step_cols()}, {"steps_writes", capture::write_step_cols()},
                        {"parts", capture::part_cols()},
                        {"parts_writes", capture::write_part_cols()}}.dump()
                << "\n";
      return 0;
    }
    else if (args[i] == "--csv") {   // refused: the capture is the output, kvcost prices it
      std::cerr << "predict: --csv is not an option: --out-predicted DIR writes the capture (kvcost predict; kvcost "
                   "predict-accuracy reads it)\n";
      return 2;
    }
    else if (args[i] == "--fresh") plans_out.fresh = true;
    else if (args[i] == "--record-fields" && i + 1 < args.size()) {   // a JSON object merged into every record
      plans_out.record_fields = json::parse(args[++i]);
      if (!plans_out.record_fields.is_object()) {
        std::cerr << "predict: --record-fields takes a JSON object\n";
        return 2;
      }
    }
    else if (args[i] == "--out-predicted" && i + 1 < args.size()) plans_out.dir[0] = args[++i];
    else if (args[i] == "--out-predicted-anchor" && i + 1 < args.size()) plans_out.dir[1] = args[++i];
    else if (args[i] == "--plants" && i + 1 < args.size()) {   // "all": every rule the binary can plant
      plants = args[++i] == "all" ? meta::Meta::predict_plants() : split_commas(args[i]);
    }
    else if (args[i] == "--stats" && i + 1 < args.size()) stats = args[++i];
    else if (args[i] == "--tally-dir" && i + 1 < args.size()) tally_dir = args[++i];
    else if (args[i] == "--requests-from" && i + 1 < args.size()) {
      // the request files listed one per line (a set too large for the argument list: LinkBench's whole log)
      const std::string list = args[++i];
      std::ifstream in(list);
      if (!in) { std::cerr << "predict: --requests-from " << list << ": cannot read the list file\n"; return 2; }
      size_t listed = 0;
      for (std::string line; std::getline(in, line);) {
        const auto b = line.find_first_not_of(" \t\r"), e = line.find_last_not_of(" \t\r");   // a CRLF list too
        if (b == std::string::npos) continue;
        reqs.push_back(line.substr(b, e - b + 1));
        listed++;
      }
      if (listed == 0) { std::cerr << "predict: --requests-from " << list << ": the list file names no request\n"; return 2; }
      std::cerr << "predict: --requests-from " << list << ": " << listed << " request paths\n";   // the caller checks it
    }
    else reqs.push_back(args[i]);
  }
  if (plans) {
    if (stats.empty() || tally_dir.empty() || schema.empty() || reqs.empty() ||
        (plans_out.dir[0].empty() && plans_out.dir[1].empty())) {
      std::cerr << "usage: kvevaluator predict --plans --stats S [--anchor-stats A] --tally-dir D --schema SCHEMA\n"
                   "         [--out-predicted DIR] [--out-predicted-anchor DIR] [--fresh] [--record-fields JSON]\n"
                   "         <request.json>... | --requests-from <file: one request path per line>\n"
                   "  (at least one output: a capture dir per level; the anchor level, --out-predicted-anchor,\n"
                   "   needs --anchor-stats)\n";
      return 2;
    }
    if (!plans_out.dir[1].empty() && anchor_stats.empty()) {
      std::cerr << "predict --plans: --out-predicted-anchor needs --anchor-stats (the anchor level reads them): refused\n";
      return 2;
    }
    if (!plans_out.dir[0].empty() && !plans_out.dir[1].empty() &&
        std::filesystem::weakly_canonical(plans_out.dir[0]) == std::filesystem::weakly_canonical(plans_out.dir[1])) {
      std::cerr << "predict --plans: --out-predicted and --out-predicted-anchor name one dir (" << plans_out.dir[0]
                << "): the anchor level would replace the population level: refused\n";
      return 2;
    }
    std::sort(plans_out.rules_off.begin(), plans_out.rules_off.end());
    try { return predict_plans(stats, anchor_stats, rules, tally_dir, schema, reqs, plans_out); }
    catch (const std::exception& e) { std::cerr << "predict --plans: " << e.what() << "\n"; return 1; }
  }
  // --levels-out alone: the levels only (a run that joins predicted captures with measured ones writes them beside its
  // steps.csv: run/predict-accuracy.sh)
  if (!levels_out.empty() && !acc && !plans && !gate_mode && reqs.empty()) return write_levels(levels_out);
  if (acc && !stats.empty() && !tally_dir.empty() && !reqs.empty()) {
    if (anchor_stats.empty()) { std::cerr << "predict --accuracy: --anchor-stats is required (kvevaluator graph-stats --anchor-out)\n"; return 2; }
    try { return accuracy(stats, anchor_stats, rules, tally_dir, reqs, threads, terms_out, levels_out); }
    catch (const std::exception& e) { std::cerr << "predict --accuracy: " << e.what() << "\n"; return 1; }
  }
  for (auto& plant : plants)
    if (std::find(meta::Meta::predict_plants().begin(), meta::Meta::predict_plants().end(), plant) ==
        meta::Meta::predict_plants().end()) {
      std::cerr << "predict: no rule named '" << plant << "' to plant\n";
      return 2;
    }
  if (!gate_mode || reqs.empty()) {
    std::string rule_names;   // every switch, from the table --rule-names prints
    for (const auto& r : predict::Rules::names()) rule_names += (rule_names.empty() ? "" : ",") + r;
    std::cerr << "usage: kvevaluator predict --gate [--plants <rule>,...|all] [--threads N] [--steps-out <steps.csv>]\n"
                 "                 <request.json>...\n"
                 "  --threads N: N sessions over the requests, one store each at a time (the output is the same)\n"
                 "  --steps-out: two rows per KV step, measured and truth, every lane\n"
                 "       kvevaluator predict --accuracy --stats <stats.json> --anchor-stats <anchor.json> --tally-dir <dir>\n"
                 "                 [--threads N] [--terms-out <terms.csv>] [--levels-out <levels.json>] [--rules-off <names>]\n"
                 "                 <request.json>...\n"
                 "                 (the names: " << rule_names << ")\n"
                 "       kvevaluator predict --levels-out <levels.json>   (the levels alone, as JSON)\n"
                 "       kvevaluator predict --made-from <tally or statistics json> --records <stream>   (0: made from it)\n"
                 "       kvevaluator predict --plans --stats <stats.json> --anchor-stats <anchor.json> --tally-dir <dir> --schema <schema.json>\n"
                 "                 [--out-predicted <capture dir>] [--out-predicted-anchor <capture dir>] [--fresh]\n"
                 "                 [--record-fields <JSON object>: fields added to every record, none the record writes itself]\n"
                 "                 <request.json>...   (predict mode: no store opened; every body of each plan)\n"
                 "  --requests-from <file>: the request files listed one per line, in any mode (beside or in place of\n"
                 "    the arguments); an unreadable or empty list is refused\n"
                 "  --out-predicted / --out-predicted-anchor: a capture dir per level (counts/ steps/ parts/<cell>.csv and\n"
                 "    counts/<cell>.json; the path from python -m kvevaluator.output_tree); a capture made from other\n"
                 "    statistics or rules is refused (exit 2) unless --fresh\n"
                 "  rules to plant: the names in meta::Meta::predict_plants(), or all of them (--plants all; a wrong\n"
                 "    name is refused)\n"
                 "  --tally-dir: <store name>.json per store (kvevaluator census --chunk-keys <store>)\n";
    return 2;
  }
  return gate(reqs, plants, steps_out, threads);
}

}  // namespace evalcpp
