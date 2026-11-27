// `tally --schema <schema.json> --spec <meta spec.json> < records.jsonl`: predict mode's layout statistics for one meta
// layout, with no store. The ingest encoder runs
// as it does for a store, into a sink that counts each key and keeps nothing:
//   groups   per (part, type, direction, owner, detail), census's rows exactly (cli/census_groups.h): keys, key and
//            value bytes, payload bytes, empty values. A census of a store built from the same records and spec gives
//            the same rows (run/predict-accuracy.sh checks it)
//   chunks   per Chunked family: the chunk count, the owners they cover, and a histogram of chunk value bytes
//   locality per (edge type, direction, owner label, target label) and per Chunked family the targets live in (their
//            props family; the adjacency families they own lists in): the mean distinct chunks one owner's targets
//            span, overall and by the owner's degree (log2 buckets: [bucket, owners, summed degree, summed span]). A set
//            reached from P owners of degree d touches about C (1 - (1 - span(d) / C)^P) of the family's C chunks
//            (formats/meta/predict.cc); message ids come in blocks, so a person's messages share few chunks
// JSON on stdout. Reads the jsonl record stream an ingest reads (python/kvevaluator/measure/records_dump.py dumps it).
// --owner-sample P [--seed S]: the same statistics from P% of the owners (a vertex is in the sample when a hash of its
// vid and the seed falls below P%; the same sample in every direction), to test what a sample of owners carries (the
// layout statistics without a full pass: experiments/predict_statistics). The vertices in the sample give the props
// and index rows; the out lists of the sampled sources and the in lists of the sampled targets give the adjacency
// rows (two encoders, one per direction, so no list of an owner outside the sample is written). Every count is scaled
// by 100 / P; a chunk table's id span is not (sampled chunks cover the same id range, fewer of them). Locality: the
// sampled owners only (its sums scaled), against each target family's chunks as the sample estimates them: every
// sampled chunk cut where its sampled owners' bytes cross equal shares, into its share of the estimated chunks. A chunk
// count (a Chunked group's keys, a chunk table's count) is estimated from the chunks' fill (est_chunks), not scaled.
// The sample reads nothing outside it: a sampled chunk family's first id is the sample's lowest owner in it, and its
// last chunk is assumed half full (the pack target x inputs/contract/capabilities.json last_chunk_fill, flagged
// last_chunk_assumed); a full tally, or a family kept whole, gives both its own.
// P = 100 writes the tally without the flag, byte for byte
// (wall time aside). An output with P < 100 carries "owner_sample".
// --owner-floor F (with --owner-sample): at least F owners of every vertex type. A type L with N_L vertices (counted in
// a first pass over the stream's vertex lines) is sampled at rate r_L = max(P / 100, min(1, F / N_L)), by the same hash
// at the threshold 100 r_L, so the floor sample contains the plain one; a type with fewer than F vertices is kept whole.
// Each count is scaled by 1 / r_L of the type that owns it: props and index rows by their label, an adjacency or edge
// props row by its owner group (the group's labels must share one rate, or tally refuses), a chunk family by its
// owner groups' rates (weighted by their sampled value bytes when they differ), locality by the owner label. A family
// whose owners are all kept whole keeps its real chunks. "owner_sample" gains "floor" and "rates" (per label). F = 0
// writes the plain sample's output, byte for byte.
#include <algorithm>
#include <chrono>
#include <cmath>
#include <fstream>
#include <iostream>
#include <map>
#include <optional>
#include <array>
#include <set>
#include <tuple>
#include <string>
#include <vector>

#include "cli/census_groups.h"
#include "cli/tally_version.h"
#include "contract/contract.h"
#include "cli/commands.h"
#include "formats/format.h"
#include "formats/common.h"
#include "ingest/ingest.h"
#include "provenance.h"
#include "schema/schema.h"
#include "util/record_stream.h"
#include "util/vid.h"
#include "value/value.h"

namespace evalcpp {

using namespace census_groups;

namespace {

// counts every key the encoder writes, by census group; keeps no key or value
struct TallySink : KvSink {
  const Format& fmt;
  const ValueInspector* vals;
  std::map<GroupKey, Tally> groups;
  struct Chunks { uint64_t count = 0, owners_span = 0; std::map<int, uint64_t> value_bytes_log2; };
  std::map<std::string, Chunks> chunks;
  std::map<std::string, std::vector<std::pair<uint64_t, uint64_t>>> ranges;   // family -> each chunk's [first, last]
  std::map<std::string, std::map<uint64_t, uint64_t>> chunk_bytes;            // family -> first id -> value bytes
  // the chunk keys of a group or a family: how many, their value bytes, and the last one's (the highest first id):
  // with --owner-sample, a chunk count is estimated from them (est_chunks), not scaled as a count
  struct Fill { uint64_t n = 0, bytes = 0, last_first = 0, last_bytes = 0; };
  std::map<GroupKey, Fill> group_fill;
  std::map<std::string, Fill> family_fill;
  bool has_payload = false;
  explicit TallySink(const Format& f) : fmt(f), vals(f.values()) {
    if (!f.part_scheme() || !vals) throw std::runtime_error("tally: the format has no part scheme (meta only)");
  }
  void begin_run(const std::string&) override {}
  void end_run() override {}
  void put(std::string_view key, std::string_view value) override {
    const GroupKey g = group_of(fmt.classify(key));
    Tally& t = groups[g];
    std::optional<uint64_t> pb = vals->payload_bytes(key, value);
    has_payload = has_payload || pb.has_value();
    std::optional<bool> ev = vals->empty_value(key, value);
    t.keys++;
    t.key_bytes += key.size();
    t.value_bytes += value.size();
    t.payload_bytes += pb.value_or(0);
    t.empty_values += ev.value_or(false) ? 1 : 0;
    if (auto cr = vals->chunk_range(key, value)) {
      auto& [fam, first, last] = *cr;
      for (Fill* f : {&group_fill[g], &family_fill[fam]}) {
        f->n++;
        f->bytes += value.size();
        if (f->n == 1 || first >= f->last_first) { f->last_first = first; f->last_bytes = value.size(); }
      }
      Chunks& c = chunks[fam];
      c.count++;
      c.owners_span += last - first + 1;   // the id range the chunk covers (an upper bound on its owners)
      int b = 0;
      for (uint64_t v = value.size(); v > 1; v >>= 1) b++;
      c.value_bytes_log2[b]++;
      ranges[fam].push_back({first, last});
      chunk_bytes[fam][first] = value.size();
    }
  }
  void put_index(int, std::string key) override { put(key, ""); }
};

}  // namespace

// the chunks a sample's chunk keys stand for: every sampled chunk but the last is a full one, the last one counts by
// its fill (its bytes over the others' mean; one chunk alone: its bytes over the pack target), all times `scale`.
// Scaling the count instead would add a whole chunk per family for a part-filled last one (about 100 at P = 1)
double est_chunks(const TallySink::Fill& f, double scale, double pack) {
  if (f.n == 0) return 0;
  double full = f.n >= 2 ? double(f.bytes - f.last_bytes) / double(f.n - 1) : pack;
  double last = full > 0 ? std::min(1.0, double(f.last_bytes) / full) : 1.0;
  return std::max(1.0, (double(f.n - 1) + last) * scale);
}

// the owner sample's membership: splitmix64 of the vid and the seed, below percent / 100 of the range
bool in_sample(uint64_t vid, uint64_t seed, double percent) {
  uint64_t z = vid ^ (seed * 0x9E3779B97F4A7C15ULL);
  z += 0x9E3779B97F4A7C15ULL;
  z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
  z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
  z ^= z >> 31;
  return double(z % 1000000ULL) < percent * 10000.0;
}

int cmd_tally(const std::vector<std::string>& args) {
  auto t0 = std::chrono::steady_clock::now();
  std::string schema_path, spec_path, records_path;
  double percent = 100;
  uint64_t seed = 0;
  bool sampled = false;   // --owner-sample given: the two-encoder path (P = 100 must give the one-encoder output)
  uint64_t floor_owners = 0;   // --owner-floor: the fewest owners kept per vertex type (0: no floor)
  std::string print_sample;    // --print-sample PATH: the kept vertices per label (hex16), for the leak test
  std::string plant;           // --plant leak_one_owner: the control the leak test must catch (one owner outside the
                               // sample encoded whole)
  for (size_t i = 0; i + 1 < args.size(); i += 2) {
    if (args[i] == "--schema") schema_path = args[i + 1];
    else if (args[i] == "--spec") spec_path = args[i + 1];
    else if (args[i] == "--records") records_path = args[i + 1];
    else if (args[i] == "--owner-sample") { percent = std::stod(args[i + 1]); sampled = true; }
    else if (args[i] == "--seed") seed = std::stoull(args[i + 1]);
    else if (args[i] == "--owner-floor") floor_owners = std::stoull(args[i + 1]);
    else if (args[i] == "--print-sample") print_sample = args[i + 1];
    else if (args[i] == "--plant") plant = args[i + 1];
    else { std::cerr << "tally: unknown option " << args[i] << "\n"; return 2; }
  }
  if (schema_path.empty() || spec_path.empty() || records_path.empty() || args.size() % 2) {
    std::cerr << "usage: kvevaluator tally --schema <schema.json> --spec <meta spec.json> --records <records.jsonl> "
                 "[--owner-sample <percent> [--seed <n>] [--owner-floor <owners per type>] [--print-sample <path>]]\n";
    return 2;
  }
  if (!(percent > 0 && percent <= 100)) { std::cerr << "tally: --owner-sample takes a percent in (0, 100]\n"; return 2; }
  if (floor_owners > 0 && !sampled) { std::cerr << "tally: --owner-floor needs --owner-sample\n"; return 2; }
  if (!print_sample.empty() && !sampled) { std::cerr << "tally: --print-sample needs --owner-sample\n"; return 2; }
  if (!plant.empty() && plant != "leak_one_owner") { std::cerr << "tally: unknown --plant " << plant << "\n"; return 2; }
  const double scale = 100.0 / percent;   // the plain sample's scale (and a row with no owner type's)
  Schema schema = Schema::load(schema_path);
  // --owner-floor: each label's percent, max(P, min(100, 100 F / N_L)), from a count of the stream's vertex lines.
  // Without it every label is sampled at P (pct_of answers P, so every line below runs as before)
  std::map<std::string, double> label_pct;
  std::map<std::string, uint64_t> label_vertices;
  if (floor_owners > 0) {
    std::ifstream cr(records_path);
    if (!cr) { std::cerr << "tally: cannot read --records " << records_path << "\n"; return 2; }
    std::string ln;
    while (std::getline(cr, ln)) {
      if (ln.rfind("[\"e\"", 0) == 0 || ln.empty()) continue;
      // the stream's vertex line: ["v","<vid>","<label>",{...}]; anything else is parsed in full
      std::string label;
      if (ln.rfind("[\"v\",\"", 0) == 0) {
        size_t a = ln.find('"', 6);                                 // the vid's closing quote
        if (a != std::string::npos && ln.compare(a, 3, "\",\"") == 0) {
          size_t b = ln.find('"', a + 3);
          if (b != std::string::npos && ln.find('\\', a + 3) > b) label = ln.substr(a + 3, b - a - 3);
        }
      }
      if (label.empty()) {
        json r = json::parse(ln);
        if (r.at(0).get<std::string>() != "v") continue;
        label = r.at(2).get<std::string>();
      }
      label_vertices[label]++;
    }
    for (auto& v : schema.vertices()) {
      uint64_t n = label_vertices.count(v.label) ? label_vertices[v.label] : 0;
      label_pct[v.label] = n == 0 ? 100.0 : std::max(percent, std::min(100.0, 100.0 * double(floor_owners) / double(n)));
    }
  }
  auto pct_of = [&](const std::string& label) {
    auto it = label_pct.find(label);
    return it == label_pct.end() ? percent : it->second;
  };
  // an owner group's percent (adjacency and edge props rows name the group): its labels' one percent (a label with no
  // vertex in the stream has nothing to sample and does not count)
  std::map<std::string, double> group_pct;
  for (auto& v : schema.vertices()) {
    if (floor_owners > 0 && !label_vertices.count(v.label)) continue;
    auto [it, fresh] = group_pct.emplace(v.group, pct_of(v.label));
    if (!fresh && it->second != pct_of(v.label))
      throw std::runtime_error("tally: --owner-floor gives the labels of group " + v.group +
                               " different rates; a group's rows cannot be split by label");
  }
  bool any_sampled = false;   // a label below 100%: the output is a sample's (last chunk assumed; chunk ranges cut)
  bool rates_differ = false;   // --owner-floor raised a label above P: a row's owner type decides its scale
  for (auto& v : schema.vertices()) {
    any_sampled = any_sampled || pct_of(v.label) < 100.0;
    rates_differ = rates_differ || pct_of(v.label) != percent;
  }
  std::ifstream sf(spec_path);
  if (!sf) { std::cerr << "tally: cannot read --spec " << spec_path << "\n"; return 2; }
  json spec = json::parse(sf);
  std::unique_ptr<Format> fmt = make_format("meta", schema, spec);
  TallySink sink(*fmt);
  // with --owner-sample: a second encoder for the in lists of the sampled targets (the first one writes the sampled
  // vertices and the out lists of the sampled sources)
  std::unique_ptr<Format> fmt_in = sampled ? make_format("meta", schema, spec) : nullptr;
  std::unique_ptr<TallySink> sink_in = sampled ? std::make_unique<TallySink>(*fmt_in) : nullptr;
  // a sampled family's last chunk is assumed LAST_CHUNK_FILL of its pack target (it reads only its sample): a part's
  // pack target from the spec; a uniform spec (the diagonals a sample is drawn for) gives one per side
  auto pack_of = [&](const char* part) -> uint64_t {
    if (!spec.contains(part) || !spec[part].contains("owners") || !spec[part]["owners"].is_object()) return 0;
    return spec[part]["owners"].value("pack", uint64_t(0));
  };
  const uint64_t props_pack = any_sampled ? pack_of("vertex_props") : 0, adj_pack = any_sampled ? pack_of("adjacency") : 0;
  if (any_sampled && (props_pack || adj_pack))
    for (auto& [k, v] : spec.items())
      if (k != "adjacency" && k != "vertex_props" && k != "index" && k != "direction" && k != "version")
        throw std::runtime_error("tally: a sampled tally takes a Chunked family's pack target from a uniform spec only; the spec has '" + k + "'");
  // what entered the counts against what was read: per label (vertices read, kept) and per edge type (edges read,
  // entered as an out list, entered as an in list)
  std::map<std::string, std::array<uint64_t, 2>> kept_vertices;
  std::map<std::string, std::array<uint64_t, 3>> kept_edges;
  bool have_plant = false;
  uint64_t plant_owner = 0;
  std::ifstream records(records_path);
  if (!records) { std::cerr << "tally: cannot read --records " << records_path << "\n"; return 2; }
  json stream = record_stream_at_open(records_path);       // labelled now, before the first record
  stream["schema_sha256"] = file_sha256(schema_path);       // what it encodes with: indexes and property kinds
  // the edges by (etype, direction, owner label, target label): (owner, target) pairs, for the locality section; and
  // which (etype, direction) lists each label owns
  std::map<std::tuple<std::string, std::string, std::string, std::string>, std::vector<std::pair<uint64_t, uint64_t>>> pairs;
  std::set<std::tuple<std::string, std::string, std::string>> owns;
  std::map<std::string, std::vector<uint64_t>> sampled_vertices;   // --owner-sample: label -> the sampled vertices
  std::string line;
  uint64_t n_lines = 0;
  while (std::getline(records, line)) {
    if (line.empty()) continue;
    n_lines++;
    json r = json::parse(line);
    const std::string kind = r.at(0).get<std::string>();
    if (kind == "v") {
      std::string label = r.at(2).get<std::string>();
      uint64_t v = parse_vid(r.at(1));
      if (sampled) kept_vertices[label][0]++;
      if (sampled && !in_sample(v, seed, pct_of(label))) continue;
      if (sampled) kept_vertices[label][1]++;
      if (sampled) sampled_vertices[label].push_back(v);
      fmt->add_vertex(sink, v, label, check_kinds(from_json(r.at(3)), schema.vertex(label).props, label));
    } else if (kind == "e") {
      std::string etype = r.at(1).get<std::string>(), sl = r.at(5).get<std::string>(), dl = r.at(6).get<std::string>();
      uint64_t s = parse_vid(r.at(2)), d = parse_vid(r.at(3));
      owns.insert({etype, "out", sl});
      owns.insert({etype, "in", dl});
      if (!sampled) {
        fmt->add_edge(sink, etype, s, d, check_kinds(from_json(r.at(4)), schema.edge_props(etype), etype), sl, dl);
        pairs[{etype, "out", sl, dl}].push_back({s, d});
        pairs[{etype, "in", dl, sl}].push_back({d, s});
        continue;
      }
      bool s_in = in_sample(s, seed, pct_of(sl)), d_in = in_sample(d, seed, pct_of(dl));
      if (plant == "leak_one_owner" && !s_in) {   // the control: the first source outside the sample, its out list whole
        if (!have_plant) { have_plant = true; plant_owner = s; }
        s_in = s == plant_owner;
      }
      auto& ke = kept_edges[etype];
      ke[0]++;
      ke[1] += s_in;
      ke[2] += d_in;
      if (!s_in && !d_in) continue;
      Value props = check_kinds(from_json(r.at(4)), schema.edge_props(etype), etype);
      if (s_in) {
        fmt->add_edge(sink, etype, s, d, props, sl, dl);
        pairs[{etype, "out", sl, dl}].push_back({s, d});
      }
      if (d_in) {
        fmt_in->add_edge(*sink_in, etype, s, d, props, sl, dl);
        pairs[{etype, "in", dl, sl}].push_back({d, s});
      }
    } else {
      throw std::runtime_error("tally: record line " + std::to_string(n_lines) + ": unknown kind '" + kind + "'");
    }
  }
  fmt->finalize(sink);
  if (sampled) {   // the in side: its groups, chunk families and chunk ranges join the first sink's
    fmt_in->finalize(*sink_in);
    for (auto it = sink.groups.begin(); it != sink.groups.end();)
      it = std::get<2>(it->first) == "in" ? sink.groups.erase(it) : std::next(it);
    for (auto& [g, t] : sink_in->groups)
      if (std::get<2>(g) == "in") sink.groups[g] = t;
    auto in_family = [](const std::string& fam) { return fam.size() > 3 && fam.compare(fam.size() - 3, 3, ":in") == 0; };
    for (auto it = sink.chunks.begin(); it != sink.chunks.end();) it = in_family(it->first) ? sink.chunks.erase(it) : std::next(it);
    for (auto it = sink.ranges.begin(); it != sink.ranges.end();) it = in_family(it->first) ? sink.ranges.erase(it) : std::next(it);
    for (auto it = sink.group_fill.begin(); it != sink.group_fill.end();)
      it = std::get<2>(it->first) == "in" ? sink.group_fill.erase(it) : std::next(it);
    for (auto& [g, f] : sink_in->group_fill) if (std::get<2>(g) == "in") sink.group_fill[g] = f;
    for (auto it = sink.family_fill.begin(); it != sink.family_fill.end();)
      it = in_family(it->first) ? sink.family_fill.erase(it) : std::next(it);
    for (auto& [fam, f] : sink_in->family_fill) if (in_family(fam)) sink.family_fill[fam] = f;
    for (auto& [fam, c] : sink_in->chunks) if (in_family(fam)) sink.chunks[fam] = c;
    for (auto& [fam, r] : sink_in->ranges) if (in_family(fam)) sink.ranges[fam] = r;
    for (auto& [fam, m] : sink_in->chunk_bytes) if (in_family(fam)) sink.chunk_bytes[fam] = m;
    sink.has_payload = sink.has_payload || sink_in->has_payload;
  }
  if (!print_sample.empty()) {   // the kept vertices per label, sorted (the leak test's in-sample set)
    json ks = json::object();
    for (auto& [label, vs] : sampled_vertices) {
      std::vector<uint64_t> s = vs;
      std::sort(s.begin(), s.end());
      json a = json::array();
      for (auto v : s) a.push_back(hex16(v));
      ks[label] = a;
    }
    std::ofstream ps(print_sample);
    if (!ps) { std::cerr << "tally: cannot write --print-sample " << print_sample << "\n"; return 2; }
    ps << ks.dump(1) << "\n";
  }
  // a count of the sample, scaled to the whole by its owner's scale s (the whole itself at s = 1: the integer, unchanged)
  auto up = [&](uint64_t n, double s) -> uint64_t { return s == 1.0 ? n : uint64_t(std::llround(double(n) * s)); };
  auto label_scale = [&](const std::string& label) { return 100.0 / pct_of(label); };
  // a group row's scale: props and index rows by their label, adjacency and edge props rows by their owner group
  auto row_scale = [&](const GroupKey& g) {
    const std::string& part = std::get<0>(g);
    if (part == "vertex_props" || part == "index") return label_scale(std::get<1>(g));
    if (part == "adjacency" || part == "edge_props") {
      auto it = group_pct.find(std::get<3>(g));
      if (it != group_pct.end()) return 100.0 / it->second;
    }
    if (rates_differ)
      throw std::runtime_error("tally: --owner-floor: no owner type for the row " + part + " " + std::get<1>(g) + " " +
                               std::get<3>(g) + " " + std::get<4>(g));
    return scale;
  };
  // a chunk family's scale: props:<label> by the label; <etype>:<dir> by its owner groups' scales, their common one,
  // else their mean weighted by the groups' sampled chunk value bytes
  auto family_scale = [&](const std::string& fam) {
    if (fam.rfind("props:", 0) == 0) return label_scale(fam.substr(6));
    size_t c = fam.rfind(':');
    std::string etype = fam.substr(0, c), dir = fam.substr(c + 1);
    std::vector<std::pair<double, double>> sw;   // (scale, sampled bytes) per owner group
    for (auto& [g, f] : sink.group_fill)
      if (std::get<0>(g) == "adjacency" && std::get<1>(g) == etype && std::get<2>(g) == dir)
        sw.push_back({row_scale(g), double(f.bytes)});
    if (sw.empty()) return scale;
    bool same = true;
    for (auto& [s, w] : sw) same = same && s == sw.front().first;
    if (same) return sw.front().first;
    double num = 0, den = 0;
    for (auto& [s, w] : sw) { num += s * w; den += w; }
    return den > 0 ? num / den : scale;
  };
  // a family the sample keeps whole: every owner label it has in the stream (props: its label; adjacency: the
  // labels that own its lists, `owns`) kept at rate 1. Not from the sampled rows' scales: a label sampled below 1 with
  // no sampled owner in the family leaves no row
  auto family_whole = [&](const std::string& fam) {
    if (fam.rfind("props:", 0) == 0) return label_scale(fam.substr(6)) == 1.0;
    size_t c = fam.rfind(':');
    std::string etype = fam.substr(0, c), dir = fam.substr(c + 1);
    bool any = false;
    for (auto& [e, d, label] : owns)
      if (e == etype && d == dir) {
        any = true;
        if (label_scale(label) != 1.0) return false;
      }
    return any;
  };
  json out_groups = json::array();
  Tally total;
  const double pack = double(fmt->pack_target().value_or(0));
  for (auto& [g, t0_] : sink.groups) {
    const double s = row_scale(g);
    Tally t = {up(t0_.keys, s), up(t0_.key_bytes, s), up(t0_.value_bytes, s), up(t0_.payload_bytes, s), up(t0_.empty_values, s)};
    auto fill = sink.group_fill.find(g);
    if (s != 1.0 && fill != sink.group_fill.end() && fill->second.n == t0_.keys && t0_.keys > 0) {
      // a group of chunk keys: its keys (and key bytes, at their mean) from the chunk estimate
      double k = est_chunks(fill->second, s, pack);
      t.keys = uint64_t(std::llround(k));
      t.key_bytes = uint64_t(std::llround(k * double(t0_.key_bytes) / double(t0_.keys)));
    }
    json row = {{"part", std::get<0>(g)}, {"type", std::get<1>(g)}, {"direction", std::get<2>(g)},
                {"owner", std::get<3>(g)}, {"detail", std::get<4>(g)},
                {"keys", t.keys}, {"key_bytes", t.key_bytes}, {"value_bytes", t.value_bytes}};
    if (sink.has_payload) {
      row["payload_bytes"] = t.payload_bytes;
      row["framing_bytes"] = t.key_bytes + t.value_bytes - t.payload_bytes;
      row["empty_values"] = t.empty_values;
    }
    out_groups.push_back(row);
    total.keys += t.keys;
    total.key_bytes += t.key_bytes;
    total.value_bytes += t.value_bytes;
  }
  json out_chunks = json::object();
  for (auto& [fam, c] : sink.chunks) {
    const double fs = family_scale(fam);
    json h = json::object();
    for (auto& [b, n] : c.value_bytes_log2) h[std::to_string(b)] = up(n, fs);
    uint64_t n = fs == 1.0 ? c.count : uint64_t(std::llround(est_chunks(sink.family_fill[fam], fs, pack)));
    out_chunks[fam] = {{"chunks", n}, {"id_span", c.owners_span}, {"value_bytes_log2", h}};
    // the family's first chunk's first id (hex16): an owner below it has no chunk to land on (a write starts one). A
    // sample reads only its sample: its own lowest owner in the family (the data's lowest when the family is kept whole)
    if (!sink.chunk_bytes[fam].empty()) out_chunks[fam]["first_id"] = hex16(sink.chunk_bytes[fam].begin()->first);
    // the family's last chunk (the highest first id), where a new vertex's insert lands. A full tally, or a family
    // whose owners a sample keeps whole: its own bytes. A sampled family's own last chunk does not stand for the data's
    // and the sample reads nothing outside it: the last chunk is assumed contract::last_chunk_fill() of the family's
    // pack target (last_chunk_assumed), as the half chunk view charges a last-chunk touch
    if (!any_sampled || family_whole(fam)) {
      out_chunks[fam]["last_chunk_value_bytes"] = sink.family_fill[fam].last_bytes;
    } else {
      const uint64_t target = fam.rfind("props:", 0) == 0 ? props_pack : adj_pack;
      if (!target) throw std::runtime_error("tally: no pack target in the spec for the sampled family " + fam);
      out_chunks[fam]["last_chunk_value_bytes"] = uint64_t(std::llround(double(target) * contract::last_chunk_fill()));
      out_chunks[fam]["last_chunk_assumed"] = true;
    }
  }
  // locality: per group and target family, the mean distinct chunks per owner
  for (auto& [fam, r] : sink.ranges) std::sort(r.begin(), r.end());
  if (any_sampled) {
    // the sample's chunks are fewer and wider than the full data's. Each sampled chunk stands for as many chunks as its
    // share of the family's sampled bytes gives (of est_chunks' total); it is cut where its sampled owners' bytes cross
    // equal shares, weighing an owner by its list length (a props family's vertices weigh 1), not at equal id widths:
    // ids are not dense (a label's ids sit in their own range, and owners of other labels sit between). A chunk's range
    // starts after the one before it, so an id between two sampled chunks belongs to the next one
    std::map<std::string, std::map<uint64_t, double>> weight;   // family -> sampled owner -> weight
    for (auto& [g, prs] : pairs)
      for (auto& [o, t] : prs) weight[std::get<0>(g) + ":" + std::get<1>(g)][o] += 1;
    for (auto& [label, vs] : sampled_vertices)
      for (auto v : vs) weight["props:" + label][v] += 1;
    for (auto& [fam, r] : sink.ranges) {
      const double fs = family_scale(fam);
      if (r.empty() || fs == 1.0) continue;   // a family whose owners are all kept whole: its real chunks
      const auto& bytes = sink.chunk_bytes[fam];
      const auto& w = weight[fam];
      double total = 0;
      for (auto& [a, b] : r) total += double(bytes.at(a));
      double est = est_chunks(sink.family_fill[fam], fs, pack);
      std::vector<std::pair<uint64_t, uint64_t>> cut;
      uint64_t lo = r.front().first;
      for (auto& [a, b] : r) {
        double k = total > 0 ? std::max(1.0, std::round(est * double(bytes.at(a)) / total)) : 1.0;
        std::vector<std::pair<uint64_t, double>> own(w.lower_bound(a), w.upper_bound(b));   // the chunk's owners
        double wsum = 0;
        for (auto& [o, x] : own) wsum += x;
        std::vector<uint64_t> starts = {lo};
        if (own.empty()) {   // no sampled owner inside: equal id widths
          for (int j = 1; j < int(k); j++) {
            uint64_t c = a + uint64_t(double(b - a + 1) * j / k);
            if (c > starts.back() && c <= b) starts.push_back(c);
          }
        } else {
          // an owner's weight spreads evenly over the ids up to the next sampled owner (the owners the sample skipped
          // sit there), so a cut can fall between two sampled owners when the chunk stands for more pieces than it
          // has sampled owners
          double acc = 0;
          int j = 1;
          for (size_t i = 0; i < own.size() && j < int(k); i++) {
            uint64_t o = own[i].first, nxt = i + 1 < own.size() ? own[i + 1].first : b + 1;
            double x = own[i].second;
            while (j < int(k) && acc + x >= wsum * j / k) {
              uint64_t c = o + uint64_t((wsum * j / k - acc) / x * double(nxt - o));
              if (c > starts.back() && c <= b) starts.push_back(c);
              j++;
            }
            acc += x;
          }
        }
        for (size_t i = 0; i < starts.size(); i++) cut.push_back({starts[i], i + 1 < starts.size() ? starts[i + 1] - 1 : b});
        lo = b + 1;
      }
      r = std::move(cut);
    }
  }
  auto chunk_of = [&](const std::vector<std::pair<uint64_t, uint64_t>>& r, uint64_t v) -> int64_t {
    auto it = std::upper_bound(r.begin(), r.end(), std::make_pair(v, UINT64_MAX));
    if (it == r.begin()) return -1;
    --it;
    return v <= it->second ? int64_t(it - r.begin()) : -1;
  };
  json out_locality = json::array();
  for (auto& [g, prs] : pairs) {
    auto& [etype, dir, owner_label, target_label] = g;
    std::vector<std::string> fams;
    if (sink.ranges.count("props:" + target_label)) fams.push_back("props:" + target_label);
    for (auto& [e2, d2, l2] : owns)
      if (l2 == target_label && sink.ranges.count(e2 + ":" + d2)) fams.push_back(e2 + ":" + d2);
    if (fams.empty()) continue;
    std::sort(prs.begin(), prs.end());
    for (auto& fam : fams) {
      const auto& r = sink.ranges.at(fam);
      uint64_t owners = 0, found = 0, spans = 0;
      std::map<int, std::array<double, 3>> by_degree;   // bucket -> owners, degree sum, span sum
      std::vector<int64_t> cs;
      for (size_t i = 0; i < prs.size();) {
        size_t j = i;
        cs.clear();
        for (; j < prs.size() && prs[j].first == prs[i].first; j++) {
          int64_t c = chunk_of(r, prs[j].second);
          if (c >= 0) cs.push_back(c);
        }
        if (!cs.empty()) {
          std::sort(cs.begin(), cs.end());
          uint64_t span = uint64_t(std::unique(cs.begin(), cs.end()) - cs.begin());
          owners++;
          found += cs.size();
          spans += span;
          int b = 1;
          for (uint64_t x = cs.size(); x >>= 1;) b++;
          auto& h = by_degree[b];
          h[0] += 1;
          h[1] += double(cs.size());
          h[2] += double(span);
        }
        i = j;
      }
      if (owners == 0) continue;
      const double s = label_scale(owner_label);
      out_locality.push_back({{"etype", etype}, {"direction", dir}, {"owner_label", owner_label},
                              {"target_label", target_label}, {"family", fam}, {"owners", up(owners, s)},
                              {"targets_found", up(found, s)}, {"mean_chunks", double(spans) / double(owners)},
                              {"chunks", r.size()}, {"by_degree", [&] {
                                json h = json::array();
                                for (auto& [b, x] : by_degree) h.push_back({b, x[0] * s, x[1] * s, x[2] * s});
                                return h;
                              }()}});
    }
  }
  stream["lines"] = n_lines;
  json out = {{"format", "meta"},
              {"tally_version", kTallyVersion},
              {"spec", spec},
              {"records", stream},
              {"groups", out_groups},
              {"chunks", out_chunks},
              {"locality", out_locality},
              {"totals", {{"keys", total.keys}, {"key_bytes", total.key_bytes}, {"value_bytes", total.value_bytes}}},
              {"wall_s", std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count()},
              {"provenance", provenance()}};
  if (scale != 1.0 && floor_owners == 0)
    out["owner_sample"] = {{"percent", percent}, {"seed", seed},
                           {"rule", "counts scaled by 100 / percent; a chunk count from the chunks' fill; chunk ranges cut at the sampled owners' byte shares"}};
  if (scale != 1.0 && floor_owners > 0) {
    json rates = json::object();
    for (auto& [label, pct] : label_pct) rates[label] = pct / 100.0;
    out["owner_sample"] = {{"percent", percent}, {"seed", seed}, {"floor", floor_owners}, {"rates", rates},
                           {"rule", "a label's rate max(percent / 100, min(1, floor / its vertices)); each count scaled by "
                                    "1 / its owner's rate; a chunk count from the chunks' fill; chunk ranges cut at the "
                                    "sampled owners' byte shares; a family whose owners are all kept whole keeps its chunks"}};
  }
  if (out.contains("owner_sample")) {
    // what entered the counts against what was read, and the rows read outside the sample
    json kv = json::object(), ke = json::object();
    for (auto& [label, k] : kept_vertices) kv[label] = {{"read", k[0]}, {"kept", k[1]}};
    for (auto& [etype, k] : kept_edges) ke[etype] = {{"read", k[0]}, {"out_lists", k[1]}, {"in_lists", k[2]}};
    out["owner_sample"]["kept"] = {{"vertices", kv}, {"edges", ke}};
    out["owner_sample"]["last_chunk_fill"] = contract::last_chunk_fill();
    out["owner_sample"]["range_ends"] =
        "the sample reads only its sample. chunks.<family>.first_id: the sample's lowest owner in the family; "
        "chunks.<family>.last_chunk_value_bytes with last_chunk_assumed: the family's pack target x last_chunk_fill (an "
        "assumption: the last chunk half full, inputs/contract/capabilities.json); a family kept whole: both its own";
    if (!plant.empty()) out["owner_sample"]["plant"] = plant;
    for (auto& [label, k] : kept_vertices)
      std::cerr << "tally: kept " << label << " " << k[1] << " of " << k[0] << " vertices ("
                << (k[0] ? 100.0 * double(k[1]) / double(k[0]) : 0.0) << "%)\n";
    for (auto& [etype, k] : kept_edges)
      std::cerr << "tally: kept " << etype << " edges: " << k[1] << " out lists, " << k[2] << " in lists, of " << k[0] << "\n";
  }
  std::cout << out.dump(1) << "\n";
  return 0;
}

}  // namespace evalcpp
