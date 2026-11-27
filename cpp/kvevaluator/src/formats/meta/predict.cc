// meta's predict mode: a KV step's S / G / N and byte lanes from its CARD
// (its cardinalities and summed item sizes), with no CountingStore.
//
//   decisions  the shared ones the run makes too: hop_shape, props_layout, props_plan, distinct_vids, narrow
//   card       the step's cardinalities. Here the TRUTH card of the exact gate: a traced run's arguments, plus raw
//              (uncounted) RocksDB reads of the store. The estimate card (statistics) fills the same fields
//   rules      Split and Condensed: per (source, direction), per entry, per GET -- a second statement of the run's
//              reads, kept equal to them by the exact gate. Chunked: the run's own replay_landings over the probes
//
// Covered: every meta read operator the LDBC IC / IS plans run (by id, vertex and anchor props, hops, chase, closure,
// the edge check, index walks, label scan and label filters, the top-k's winners), on Split, Condensed and Chunked
// (inline edge props; a sorted index at full dates). Anything else throws, naming what. The estimate card (below)
// covers the same reads and the writes; it reads each statistic as a named term (predict/terms.h, Estimate::term), which
// the truth-card levels of `predict --accuracy` fills from the truth card instead (predict_step_truth's terms_out).
#include "formats/meta/format.h"
#include "predict/estimate_input.h"
#include "predict/landing.h"
#include "predict/terms.h"
#include "predict/stats.h"

#include <rocksdb/db.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <tuple>
#include <vector>

#include "codec/columns.h"
#include "contract/contract.h"
#include "codec/lists.h"
#include "exec/chunk_cache.h"
#include "exec/chunk_write.h"
#include "exec/replay.h"
#include "formats/common.h"
#include "formats/meta/keys.h"
#include "formats/rows.h"
#include "index/sorted_index.h"
#include "store/unit_value.h"
#include "util/vid.h"
#include "ops/kv_contract.h"

namespace evalcpp::meta {

// ---- predicted counts: the run's lanes as expected values (the truth card gives whole numbers) ----

struct PCounts {
  double S = 0, G = 0, N = 0, S_miss = 0, G_miss = 0, N_miss = 0;
  double get_key = 0, get_value = 0, scan_key = 0, scan_value = 0;
  double seek_miss_key = 0, next_miss_key = 0, get_miss_key = 0, used_key = 0, used_value = 0;
  double P = 0, D = 0, write_key = 0, write_value = 0;   // a write step's own PUTs and DELs
  // the last-chunk lane (Counts::LastChunk): a Chunked RMW of a family's last chunk
  double lc_reads = 0, lc_read_bytes = 0, lc_read_target = 0, lc_writes = 0, lc_write_bytes = 0, lc_write_base = 0,
         lc_write_target = 0;
  void add(const PCounts& o) {
    S += o.S; G += o.G; N += o.N; S_miss += o.S_miss; G_miss += o.G_miss; N_miss += o.N_miss;
    get_key += o.get_key; get_value += o.get_value; scan_key += o.scan_key; scan_value += o.scan_value;
    seek_miss_key += o.seek_miss_key; next_miss_key += o.next_miss_key; get_miss_key += o.get_miss_key;
    used_key += o.used_key; used_value += o.used_value;
    P += o.P; D += o.D; write_key += o.write_key; write_value += o.write_value;
    lc_reads += o.lc_reads; lc_read_bytes += o.lc_read_bytes; lc_read_target += o.lc_read_target;
    lc_writes += o.lc_writes; lc_write_bytes += o.lc_write_bytes; lc_write_base += o.lc_write_base;
    lc_write_target += o.lc_write_target;
  }
  static PCounts of(const Counts& c) {
    PCounts p;
    p.S = double(c.S); p.G = double(c.G); p.N = double(c.N);
    p.S_miss = double(c.S_miss); p.G_miss = double(c.G_miss); p.N_miss = double(c.N_miss);
    p.get_key = double(c.get_key); p.get_value = double(c.get_value);
    p.scan_key = double(c.scan_key); p.scan_value = double(c.scan_value);
    p.seek_miss_key = double(c.seek_miss_key); p.next_miss_key = double(c.next_miss_key);
    p.get_miss_key = double(c.get_miss_key); p.used_key = double(c.used_key); p.used_value = double(c.used_value);
    p.P = double(c.P); p.D = double(c.D); p.write_key = double(c.write_key); p.write_value = double(c.write_value);
    const Counts::LastChunk& l = c.last_chunk;
    p.lc_reads = double(l.reads); p.lc_read_bytes = double(l.read_bytes); p.lc_read_target = double(l.read_target);
    p.lc_writes = double(l.writes); p.lc_write_bytes = double(l.write_bytes); p.lc_write_base = double(l.write_base);
    p.lc_write_target = double(l.write_target);
    return p;
  }
  // the gate's form: every lane a whole number (a fraction here means a rule fed a non-integral truth: refused)
  Counts whole() const {
    auto w = [](double x) {
      double r = std::llround(x);
      if (std::fabs(x - r) > 1e-6) throw std::runtime_error("predict: a truth card gave a fractional count");
      return uint64_t(r);
    };
    Counts c;
    c.S = w(S); c.G = w(G); c.N = w(N); c.S_miss = w(S_miss); c.G_miss = w(G_miss); c.N_miss = w(N_miss);
    c.get_key = w(get_key); c.get_value = w(get_value); c.scan_key = w(scan_key); c.scan_value = w(scan_value);
    c.seek_miss_key = w(seek_miss_key); c.next_miss_key = w(next_miss_key); c.get_miss_key = w(get_miss_key);
    c.used_key = w(used_key); c.used_value = w(used_value);
    c.P = w(P); c.D = w(D); c.write_key = w(write_key); c.write_value = w(write_value);
    c.last_chunk = {w(lc_reads), w(lc_read_bytes), w(lc_read_target), w(lc_writes), w(lc_write_bytes),
                    w(lc_write_base), w(lc_write_target)};
    return c;
  }
};

// ---- the cards ----

// one walked direction of a Split or Condensed hop
struct HopDirCard {
  double probes = 0;              // distinct sources probed in this direction
  double present = 0;           // probes that found a list (Split: at least one record)
  double entries = 0;           // the entries of the present lists
  double split_lists = 0;       // Condensed: present lists stored as parts
  double parts = 0;             // Condensed: the parts of those lists
  double part_key_bytes = 0;    // Condensed: those parts' keys
  double value_bytes = 0;       // the values read (Split: the records; Condensed: the lists or their parts)
  double used_value = 0;        // the entries' used bytes (Condensed: id bytes; both: the asked edge cells)
  double absent_key_bytes = 0;  // the keys an absent list's SEEK landed on (0 past the store's end)
  double end_key_bytes = 0;     // the keys the landing past a list's last record or part landed on
};

// one (label, attr) of a Split props read, or one (label, row group) of a Condensed one
struct PropsCard {
  double asked = 0;             // GETs asked
  double hits = 0;              // of them, found
  double value_bytes = 0;       // the found values
  double used_cells = 0;        // Condensed: the asked cells of the found rows
};

// Split's existence probe of one label (split_exists): a SEEK to the vertex's key prefix
struct ExistCard {
  double probes = 0;
  double hits = 0;
  double hit_key_bytes = 0;     // the attr record a hit lands on
  double miss_key_bytes = 0;    // the foreign key a miss lands on (0 past the store's end)
};

// a chase's Split hop: a SEEK landing on the first record, no NEXT
struct FirstCard {
  double probes = 0;
  double hits = 0;
  double hit_key_bytes = 0;     // the landed record's key (all of it is used)
  double hit_value_bytes = 0;   // its value, read only when it holds the far label's code
  double miss_key_bytes = 0;
};

// prefix walks of the index (eq, range, enumerate): 1 SEEK, 1 NEXT per match, the landing that ends it a miss
struct WalkCard {
  double walks = 0;
  double empty = 0;               // walks with no match: the SEEK itself lands outside
  double matches = 0;
  double match_key_bytes = 0;
  double end_key_bytes = 0;       // where a walk with matches ended (a NEXT miss)
  double empty_key_bytes = 0;     // where an empty walk landed (a SEEK miss)
};

// one scan of a label's run (read_vertices_by_label with props): every key and value read, a row per vertex
struct ScanCard {
  double scans = 0;
  double empty = 0;
  double keys = 0;                // keys landed inside the run (Chunked: chunks)
  double key_bytes = 0;
  double value_bytes = 0;
  double end_key_bytes = 0;       // the landing past the run
  double used_key = 0;            // the asked props' keys of the kept vertices
  double used_value = 0;          // the asked cells of the kept vertices
};

struct StepCard {
  std::vector<HopDirCard> dirs;                              // a hop: parallel to its shape's dirs
  std::map<std::pair<std::string, size_t>, PropsCard> props; // (label, attr | group)
  std::map<std::string, ExistCard> exist;                    // Split existence probes, per label
  std::map<std::string, PropsCard> exist_gets;               // Condensed existence GETs (the row key), per label
  std::vector<HopDirCard> chase_lists;                       // a chase's Condensed list reads, per direction
  std::vector<FirstCard> chase_first;                        // a chase's Split reads, per direction
  WalkCard walk;
  ScanCard scan;
  std::map<bool, PropsCard> edge_gets;                       // Split edge checks: GETs of an edge's key, per direction
  bool entries_used = true;                                  // a hop's entries are used (an edge check's lists: none)
  std::vector<Landing> landings;                             // Chunked: every probe, in the run's task order
  double chunk_used_value = 0;                             // Chunked: the used bytes booked from the chunks
  PCounts direct;                                            // a top-k walk's counts (group 3: an input)
};

// ---- raw reads (no counting) ----

struct Raw {
  rocksdb::DB* db;
  std::unique_ptr<rocksdb::Iterator> it;
  // no store (the estimate card): no reads
  explicit Raw(Store* st) : db(st ? st->db() : nullptr), it(st ? st->db()->NewIterator(rocksdb::ReadOptions()) : nullptr) {}
  std::optional<std::string> get(const std::string& key) const {
    std::string v;
    rocksdb::Status s = db->Get(rocksdb::ReadOptions(), key, &v);
    if (s.IsNotFound()) return std::nullopt;
    if (!s.ok()) throw std::runtime_error("predict: raw get: " + s.ToString());
    return v;
  }
  std::string_view key() const { return {it->key().data(), it->key().size()}; }
  std::string_view value() const { return {it->value().data(), it->value().size()}; }
};

// ---- the pricer: the truth card and the rules. Meta's friend, so the rules read the same schema helpers and key
// builders as the run ----

struct StepPredictor {
  const Meta& m;
  Raw raw;
  std::string plant;
  json* coverage = nullptr;   // the cases each rule priced, by rule name (the gate prints them: 0 cases = untested)
  std::set<std::string>* asked = nullptr;   // the plant names the rules consulted (the gate's re-pricing filter)
  StepPredictor(const Meta& meta, Store* st, std::string pl) : m(meta), raw(st), plant(std::move(pl)) {}

  // ---- the truth-card levels: the step's terms (predict/terms.h) from its truth card, when asked (terms_out). The cards'
  // sums are kept here as the walk fills them; terms_finish turns them into the terms: counts as counted, sizes as a sum
  // over the items of the term's kind, over their count. A read step's from its card; a write step's from its
  // read-modify-writes (a Condensed list read, a chunk read and rewritten). The pricing never reads these ----
  std::map<std::string, double>* terms = nullptr;
  std::map<std::string, double> tsum;                        // "<sum>@<part>" -> the card's sum
  std::vector<std::pair<std::string, std::string>> tfams;    // Chunked families the step probed: (family name, prefix)
  std::vector<Landing> tland;                                // the step's landings, every family
  void tadd(const std::string& k, double v) { if (terms) tsum[k] += v; }
  void tset(const std::string& k, double v) { if (terms) (*terms)[k] = v; }
  static std::string adj_term_part(const std::string& etype, bool d) { return "adj:" + etype + (d ? ":out" : ":in"); }
  // a write's chunk family by its key prefix (the estimate's names: <etype>:<dir>, props:<Label>); "" for another run
  // (an edge-prop chunk run)
  std::string family_of_prefix(const std::string& prefix) const {
    for (auto& e : m.schema_.edges())
      for (bool d : {true, false})
        if (padj_prefix(uint8_t(e.id), d) == prefix) return e.etype + (d ? ":out" : ":in");
    for (auto& v : m.schema_.vertices())
      if (vertex_family(m.vtype_of(v.label)) == prefix) return "props:" + v.label;
    return "";
  }
  void tfam(const std::string& family, const std::string& prefix) {
    if (!terms) return;
    for (auto& f : tfams) if (f.first == family) return;
    tfams.push_back({family, prefix});
  }
  // a hop-like card's directions: per (etype, dir) its probes, present lists, entries, value bytes and (Split) record keys
  // `suffix`: per direction, what the part key carries after its name (an edge check's read: "#<i>", so two reads of
  // one part keep their own terms)
  void hop_terms(const Meta::HopShape& sh, const std::string& etype, const StepCard& c,
                 const std::map<bool, std::string>& suffix = {}) {
    if (!terms) return;
    uint8_t eid = uint8_t(m.schema_.edge(etype).id);
    for (size_t di = 0; di < sh.dirs.size() && di < c.dirs.size(); di++) {
      bool d = sh.dirs[di];
      const Component& al = m.cfg_.adj_of(etype, d);
      if (al.packed()) { tfam(etype + (d ? ":out" : ":in"), padj_prefix(eid, d)); continue; }
      const HopDirCard& k = c.dirs[di];
      std::string P = adj_term_part(etype, d) + (suffix.count(d) ? suffix.at(d) : "");
      tadd("present@" + P, k.present);
      tadd("entries@" + P, k.entries);
      tadd("value_bytes@" + P, k.value_bytes);
      if (!al.grouped()) tadd("key_bytes@" + P, k.entries * double(edge_key(eid, d, 0, 0).size()));
    }
    tland.insert(tland.end(), c.landings.begin(), c.landings.end());
  }
  // a props card: per label (and Split attr) the vertices asked, those found, their bytes
  void props_terms(const Component& layout, const StepCard& c) {
    if (!terms) return;
    for (auto& [key, k] : c.props) {
      const std::string& lab = key.first;
      double& n = tsum["props_n@props:" + lab];
      n = std::max(n, k.asked);
      if (layout.grouped()) {
        tadd("row_hits@props:" + lab, k.hits);
        tadd("row_bytes@props:" + lab, k.value_bytes);
      } else {
        std::string P = "props:" + lab + ":" + std::to_string(key.second);
        tadd("asked@" + P, k.asked);
        tadd("hits@" + P, k.hits);
        tadd("prop_bytes@" + P, k.value_bytes);
      }
    }
    for (auto& [lab, k] : c.exist) {
      tadd("lprobes@props:" + lab, k.probes);
      tadd("lhits@props:" + lab, k.hits);
      tadd("lkey_bytes@props:" + lab, k.hit_key_bytes);
    }
    for (auto& [lab, k] : c.exist_gets) {
      tadd("lprobes@props:" + lab, k.asked);
      tadd("lhits@props:" + lab, k.hits);
      tadd("row_hits@props:" + lab, k.hits);
      tadd("row_bytes@props:" + lab, k.value_bytes);
    }
    tland.insert(tland.end(), c.landings.begin(), c.landings.end());
  }
  // a walk of one index part: its matches and their key bytes (the card's change across the walk)
  void walk_terms(const std::string& part, const WalkCard& before, const WalkCard& after) {
    tadd("matches@" + part, after.matches - before.matches);
    tadd("match_key_bytes@" + part, after.match_key_bytes - before.match_key_bytes);
  }
  std::string index_term_part(const std::string& label, const std::string& prop) const {
    return "index:" + label + (prop.empty() ? "" : "." + prop);
  }
  std::string enumeration_term_part(const std::string& label) const {
    return index_term_part(label, m.schema_.prop_indexed(label) ? m.schema_.enumeration_prop(label) : "");
  }
  // the sums into the terms (predict/terms.h names each)
  void tmean(const std::string& term, const std::string& part, double sum, double n) {
    if (n > 0) (*terms)[predict::term_key(term, part)] = sum / n;
  }
  void terms_finish() {
    if (!terms) return;
    terms_from_sums();
    terms_from_families();
  }
  void terms_from_sums() {
    auto at = [&](const std::string& k) { auto it = tsum.find(k); return it == tsum.end() ? 0.0 : it->second; };
    auto mean = [&](const std::string& term, const std::string& part, double sum, double n) { tmean(term, part, sum, n); };
    for (auto& [k, v] : tsum) {
      size_t sep = k.find('@');
      std::string name = k.substr(0, sep), part = k.substr(sep + 1);
      if (name == "present" || name == "entries" || name == "props_n" || name == "matches")
        (*terms)[predict::term_key(name == "matches" ? "index_matches" : name, part)] = v;
      if (name == "entries") {
        mean("adj_value_mean", part, at("value_bytes@" + part), v);
        if (tsum.count("key_bytes@" + part)) mean("adj_key_mean", part, at("key_bytes@" + part), v);
      }
      if (name == "present") {
        mean("list_value_mean", part, at("value_bytes@" + part), v);
        mean("list_entries_mean", part, at("entries@" + part), v);
      }
      if (name == "hits") {
        mean("prop_hit_share", part, v, at("asked@" + part));
        mean("prop_value_mean", part, at("prop_bytes@" + part), v);
      }
      if (name == "row_hits") mean("row_value_mean", part, at("row_bytes@" + part), v);
      if (name == "lhits") {
        mean("label_hit_share", part, v, at("lprobes@" + part));
        mean("props_key_mean", part, at("lkey_bytes@" + part), v);
      }
      if (name == "matches") mean("index_key_mean", part, at("match_key_bytes@" + part), v);
      write_terms_from_sum(name, part, v);
    }
  }
  // a write's chunk RMWs (chunk_rmw_truth): the family's last chunk, the chunk read, the bytes each rewrite added
  void write_terms_from_sum(const std::string& name, const std::string& part, double n) {
    auto at = [&](const std::string& k) { auto it = tsum.find(k); return it == tsum.end() ? 0.0 : it->second; };
    const bool props = part.rfind("props:", 0) == 0;
    if (name == "lc_n") tmean("last_chunk_value", part, at("lc_bytes@" + part), n);
    if (name == "wc_n") {
      tmean("chunk_value_mean", part, at("wc_bytes@" + part), n);
      tmean("chunk_last_share", part, at("lland@" + part), n);
    }
    if (name == "wgrow_n")
      tmean(props ? "row_value_mean" : "adj_value_mean", props ? part : "adj:" + part, at("wgrow@" + part), n);
  }
  void terms_from_families() {
    for (auto& [family, prefix] : tfams) {   // a Chunked family: its probes, and its charged chunks' key and value means
      std::vector<Landing> mine;
      for (auto& L : tland) if (L.prefix == prefix) mine.push_back(L);
      if (family.rfind("props:", 0) == 0) (*terms)[predict::term_key("props_n", family)] = double(mine.size());
      Counts r = replay_landings(mine);
      (*terms)[predict::term_key("chunks_touched", family)] = double(r.S - r.S_miss);   // the chunks charged
      tmean("chunk_key_mean", family, double(r.scan_key), double(r.S - r.S_miss));
      tmean("chunk_value_mean", family, double(r.scan_value), double(r.S - r.S_miss));
    }
  }

  // every plant is consulted only through here, so `asked` holds each plant that could change this step
  bool planted(const char* name) const {
    if (asked) asked->insert(name);
    return plant == name;
  }
  void covered(const std::string& rule, double n) const {   // const: writes through the pointer only
    const std::vector<std::string>& known = Meta::predict_rules();
    if (std::find(known.begin(), known.end(), rule) == known.end())
      throw std::runtime_error("predict: rule '" + rule + "' is not in predict_rules()");
    if (!coverage || n <= 0) return;
    (*coverage)[rule] = coverage->value(rule, 0.0) + n;
  }

  // the step's vid list as the run received it: each vid with the label its value carried (arg_tags)
  static ValueList vids_of(const json& step, const char* key) {
    ValueList out;
    const json& a = step.at("args").at(key);
    const json* tags = step.contains("arg_tags") && step["arg_tags"].contains(key) ? &step["arg_tags"][key] : nullptr;
    for (size_t i = 0; i < a.size(); i++) {
      uint64_t u;
      if (!canonical_vid(a[i].get<std::string>(), &u)) throw std::runtime_error("predict: not a canonical vid");
      Value v = Value::vid(u);
      if (tags && !(*tags)[i].is_null()) v.label = int16_t((*tags)[i].get<int>());
      out.push_back(v);
    }
    return out;
  }
  static std::vector<std::string> strs(const json& step, const char* key) {
    std::vector<std::string> out;
    const json& args = step.at("args");
    if (!args.contains(key) || args[key].is_null()) return out;
    if (args[key].is_string()) return {args[key].get<std::string>()};
    for (auto& x : args[key]) out.push_back(x.get<std::string>());
    return out;
  }

  // a chunk probe as the run's ChunkReader records it (exec/chunk_cache.h locate): seek_for_prev on prefix + id
  Landing land(uint64_t group, const std::string& prefix, uint64_t id) {
    raw.it->SeekForPrev(chunk_with_id(prefix, id));
    if (!raw.it->Valid() || !chunk_has_prefix(raw.key(), prefix))
      return {group, prefix, id, false, 0, 0, 0, 0, raw.it->Valid(), raw.it->Valid() ? uint32_t(raw.key().size()) : 0};
    uint64_t first = chunk_first_id(raw.key());
    auto range = m.chunk_range(raw.key(), raw.value());
    if (!range) throw std::runtime_error("predict: a chunk key with no chunk range");
    return {group, prefix, id, true, first, first, std::get<2>(*range), uint32_t(raw.value().size()), true,
            uint32_t(raw.key().size())};
  }

  // ---- read_neighbors ----

  // `group`: the replay group of its chunk probes (a closure's level). `nbrs`: when given, the neighbours in the run's
  // output order (per source, its directions in order), for a caller that walks on (a closure)
  void hop_truth(const Meta::HopShape& sh, const std::string& etype, const ValueList& srcs,
                 const std::vector<std::string>& edge_props, StepCard& c, uint64_t group = 0,
                 std::vector<uint64_t>* nbrs = nullptr, std::vector<std::vector<uint64_t>>* lists = nullptr) {
    DistinctVids d = distinct_vids("meta", srcs, false);
    if (lists) nbrs = nullptr;   // one form of output: per source, or flat
    std::vector<std::vector<uint64_t>> local;
    std::vector<std::vector<uint64_t>>& per_src = lists ? *lists : local;
    per_src.assign(nbrs || lists ? d.uids.size() : 0, {});
    size_t at = 0;   // the source being read
    bool want = nbrs || lists;
    auto keep = [&](uint64_t dst) { if (want) per_src[at].push_back(dst); };
    uint8_t eid = uint8_t(m.schema_.edge(etype).id);
    const std::vector<ColDecl>* cols = &m.ecols(etype);
    c.dirs.assign(sh.dirs.size(), HopDirCard());
    // Chunked: the probes in the run's order -- one pass: per source, its directions; two passes: per direction
    // (each pass a fan-out over the sources)
    auto chunked = [&](size_t di) {
      bool dir = sh.dirs[di];
      const Component& al = m.cfg_.adj_of(etype, dir);
      if (al.props_own()) throw std::runtime_error("predict: Chunked edge props in their own chunks (not covered)");
      std::string prefix = padj_prefix(eid, dir);
      unsigned bits = m.far_bits(etype, dir);
      return [&, prefix, bits](uint64_t su) {
        Landing L = land(group, prefix, su);
        c.landings.push_back(L);
        if (!L.landed || su > L.hi) return;   // no list here (the chunk before it is still read whole)
        raw.it->SeekForPrev(chunk_with_id(prefix, su));
        lists::ChunkView chunk(cols, std::string(raw.value()), bits);
        int64_t i = chunk.find(su);
        if (i < 0) return;
        if (want) for (uint64_t dst : chunk.dsts(size_t(i))) keep(dst);
        if (!c.entries_used) return;
        std::vector<uint8_t> w = chunk.id_widths(size_t(i));
        for (size_t j = 0; j < w.size(); j++)
          c.chunk_used_value += w[j] + (edge_props.empty() ? 0 : chunk.props_bytes(size_t(i), j));
      };
    };
    auto unpacked = [&](size_t di, uint64_t su) {
      bool dir = sh.dirs[di];
      HopDirCard& k = c.dirs[di];
      k.probes++;
      std::string prefix = adj_prefix(eid, dir, su);
      if (!m.cfg_.adj_of(etype, dir).grouped()) {   // Split: one record per edge, a prefix scan
        bool coded = m.far_bits(etype, dir) > 0;
        raw.it->Seek(prefix);
        if (!raw.it->Valid() || !has_prefix(raw.key(), prefix)) {
          k.absent_key_bytes += raw.it->Valid() ? raw.key().size() : 0;
          return;
        }
        k.present++;
        for (; raw.it->Valid() && has_prefix(raw.key(), prefix); raw.it->Next()) {
          k.entries++;
          keep(edge_dst(raw.key()));
          std::string_view v = raw.value();
          k.value_bytes += v.size();
          if (!edge_props.empty()) {
            if (coded) v.remove_prefix(1);
            columns::View cv(cols, v, 0, 1);
            for (auto& ep : edge_props) {
              int col = cv.col_index(ep);
              if (col >= 0) k.used_value += cv.cell_bytes(size_t(col), 0);
            }
          }
        }
        k.end_key_bytes += raw.it->Valid() ? raw.key().size() : 0;
        return;
      }
      // Condensed: one list per (owner, direction), whole or as parts (store/unit_value.cc read)
      unsigned bits = m.far_bits(etype, dir);
      auto add_list = [&](std::string_view v) {
        k.value_bytes += v.size();
        lists::View lv(cols, v, bits);
        k.entries += lv.size();
        if (want) for (uint64_t dst : lv.dsts()) keep(dst);
        if (!c.entries_used) return;
        for (size_t e = 0; e < lv.size(); e++)
          k.used_value += lv.id_bytes(e) + (edge_props.empty() ? 0 : lv.props_bytes(e));
      };
      raw.it->Seek(prefix);
      if (!raw.it->Valid()) return;   // past the end: a miss with no key
      if (raw.key() == prefix) { k.present++; add_list(raw.value()); return; }
      if (!unit::owns(raw.key(), prefix)) { k.absent_key_bytes += raw.key().size(); return; }
      k.present++;
      k.split_lists++;
      for (; raw.it->Valid() && unit::owns(raw.key(), prefix); raw.it->Next()) {
        k.parts++;
        k.part_key_bytes += raw.key().size();
        add_list(raw.value());
      }
      k.end_key_bytes += raw.it->Valid() ? raw.key().size() : 0;
    };
    auto pass = [&](const std::vector<size_t>& dis) {
      for (at = 0; at < d.uids.size(); at++)
        for (size_t di : dis) {
          if (m.cfg_.adj_of(etype, sh.dirs[di]).packed()) chunked(di)(d.uids[at]);
          else unpacked(di, d.uids[at]);
        }
    };
    if (!sh.two) {
      std::vector<size_t> all;
      for (size_t i = 0; i < sh.dirs.size(); i++) all.push_back(i);
      pass(all);
    } else {
      pass({0});
      pass({1});
    }
    if (nbrs)
      for (auto& l : per_src) nbrs->insert(nbrs->end(), l.begin(), l.end());
  }

  // read_closure: level by level, each level one hop over the frontier of vertices not yet seen (closure_over); a
  // level is its own replay group
  // `sums`: every level's card added up (its directions and landings), for the truth-card levels; the cost reads none of it
  PCounts closure(const json& s, StepCard& sums) {
    const json& a = s.at("args");
    std::string etype = a.at("etype").get<std::string>();
    std::string dir = kv::dir_of(a);
    std::optional<int64_t> max_levels;
    if (a.contains("max_levels") && !a["max_levels"].is_null()) max_levels = a["max_levels"].get<int64_t>();
    Meta::HopShape sh = m.hop_shape(etype, dir, {});
    std::set<uint64_t> seen;
    std::vector<uint64_t> frontier;
    for (auto& v : vids_of(s, "srcs"))
      if (seen.insert(v.as_vid()).second) frontier.push_back(v.as_vid());
    StepCard all;                              // every level's landings, one replay
    PCounts out;
    for (int64_t level = 0; !frontier.empty() && (!max_levels || level < *max_levels); level++) {
      ValueList fr;
      for (uint64_t u : frontier) fr.push_back(Value::vid(u));
      StepCard c;
      std::vector<uint64_t> nbrs;
      hop_truth(sh, etype, fr, {}, c, uint64_t(level), &nbrs);
      out.add(hop_rule(sh, etype, c));
      all.landings.insert(all.landings.end(), c.landings.begin(), c.landings.end());
      all.chunk_used_value += c.chunk_used_value;
      if (terms) {
        sums.dirs.resize(c.dirs.size());
        for (size_t di = 0; di < c.dirs.size(); di++) {
          HopDirCard& t = sums.dirs[di];
          const HopDirCard& k = c.dirs[di];
          t.probes += k.probes; t.present += k.present; t.entries += k.entries; t.value_bytes += k.value_bytes;
        }
        sums.landings.insert(sums.landings.end(), c.landings.begin(), c.landings.end());
      }
      std::vector<uint64_t> next;
      std::set<uint64_t> local;
      for (uint64_t x : nbrs)
        if (local.insert(x).second && !seen.count(x)) next.push_back(x);
      for (uint64_t x : next) seen.insert(x);
      frontier = std::move(next);
      covered("closure.level", 1);
    }
    out.add(chunk_rule(all));
    return out;
  }

  // Condensed: one list read per (source, direction) -- 1 SEEK; a split list: 1 NEXT per part and the landing past
  // the last part a miss (unit::read). Shared by a hop and a chase (which uses only the first entry: its card's used)
  void condensed_lanes(uint8_t eid, bool dir, const HopDirCard& k, PCounts& out) const {
    double K = double(adj_prefix(eid, dir, 0).size()) + (planted("condensed_list_key") ? 1 : 0);
    out.S += k.probes;
    out.S_miss += k.probes - k.present;                    // an absent list: a miss on a foreign key
    out.seek_miss_key += k.absent_key_bytes;
    out.N += k.parts;
    out.N_miss += k.split_lists;
    out.next_miss_key += k.end_key_bytes;
    out.scan_key += (k.present - k.split_lists) * K + k.part_key_bytes;
    out.scan_value += k.value_bytes;
    out.used_key += k.present * K;                         // the list's own key, whole or split
    out.used_value += k.used_value;
  }

  // the rule: what a hop costs, from its shape and card
  PCounts hop_rule(const Meta::HopShape& sh, const std::string& etype, const StepCard& c) const {
    PCounts out;
    uint8_t eid = uint8_t(m.schema_.edge(etype).id);
    for (size_t di = 0; di < sh.dirs.size(); di++) {
      bool dir = sh.dirs[di];
      const Component& al = m.cfg_.adj_of(etype, dir);
      if (al.packed()) continue;   // the replay prices Chunked
      const HopDirCard& k = c.dirs[di];
      if (al.grouped()) { condensed_lanes(eid, dir, k, out); continue; }
      // Split: 1 SEEK per (source, direction), 1 NEXT per record; the last lands past the list
      double K = double(edge_key(eid, dir, 0, 0).size());
      out.S += k.probes;
      out.S_miss += k.probes - k.present;
      out.seek_miss_key += k.absent_key_bytes;
      out.N += k.entries + (planted("split_next_per_entry") ? k.present : 0);
      out.N_miss += k.present;
      out.next_miss_key += k.end_key_bytes;
      out.scan_key += k.entries * K;
      out.scan_value += k.value_bytes;
      out.used_key += c.entries_used ? k.entries * K : 0;  // every entry's key is used (none in an edge check's lists)
      out.used_value += k.used_value;
    }
    return out;
  }

  // ---- existence probes: Split's prefix SEEK (split_exists), Condensed's GET of the row key, Chunked's chunk probe ----

  bool exist_split(const std::string& lab, uint64_t u, StepCard& c) {
    ExistCard& k = c.exist[lab];
    k.probes++;
    std::string key = vertex_key(m.vtype_of(lab), u);
    raw.it->Seek(key);
    if (raw.it->Valid() && has_prefix(raw.key(), key)) { k.hits++; k.hit_key_bytes += raw.key().size(); return true; }
    k.miss_key_bytes += raw.it->Valid() ? raw.key().size() : 0;
    return false;
  }
  bool exist_get(const std::string& lab, uint64_t u, StepCard& c) {
    PropsCard& k = c.exist_gets[lab];
    k.asked++;
    std::optional<std::string> v = raw.get(vertex_key(m.vtype_of(lab), u));
    if (!v) return false;
    k.hits++;
    k.value_bytes += v->size();
    return true;
  }
  // the chunk probe of `u` under `lab` (ChunkReader::locate), and the row it finds there (-1: none)
  int64_t chunk_row(uint64_t group, const std::string& lab, uint64_t u, StepCard& c,
                    std::shared_ptr<PropsChunk>* out = nullptr) {
    std::string prefix = vertex_family(m.vtype_of(lab));
    Landing L = land(group, prefix, u);
    c.landings.push_back(L);
    if (!L.landed || u > L.hi) return -1;
    raw.it->SeekForPrev(chunk_with_id(prefix, u));
    std::shared_ptr<PropsChunk> chunk = PropsChunk::decode(&m.cc_, &m.vcols(lab), std::string(raw.value()));
    int64_t row = chunk->find(u);
    if (out) *out = chunk;
    return row;
  }

  void get_lanes(double K, const PropsCard& k, double used_value, PCounts& out) const {
    out.G += k.asked;
    out.G_miss += k.asked - k.hits;
    out.get_key += k.hits * K;
    out.get_miss_key += (k.asked - k.hits) * K;
    out.get_value += k.value_bytes;
    out.used_key += k.hits * K;
    out.used_value += used_value;
  }
  PCounts exist_rule(const StepCard& c) const {
    PCounts out;
    for (auto& [lab, k] : c.exist) {                       // Split: 1 SEEK; a hit's key prefix is used, its value not
      double K = double(vertex_key(m.vtype_of(lab), 0).size());
      out.S += k.probes;
      out.S_miss += k.probes - k.hits;
      out.scan_key += k.hit_key_bytes;
      out.seek_miss_key += k.miss_key_bytes;
      out.used_key += k.hits * K;
      if (planted("split_exists_value")) out.scan_value += k.hits;   // the plant: a hit reads its landed value
    }
    for (auto& [lab, k] : c.exist_gets)                    // Condensed: 1 GET of the row key; a hit's key is used
      get_lanes(double(vertex_key(m.vtype_of(lab), 0).size()), k, 0, out);
    return out;
  }
  void exist_covered(const StepCard& c) const {
    for (auto& [lab, k] : c.exist) { covered("exist_split.hit", k.hits); covered("exist_split.miss", k.probes - k.hits); }
    for (auto& [lab, k] : c.exist_gets) { covered("exist_condensed.hit", k.hits); covered("exist_condensed.miss", k.asked - k.hits); }
  }

  // ---- index walks (index/sorted_index.cc: eq, range, enumerate) ----

  template <class In>
  void walk(const std::string& start, In in, WalkCard& w) {
    raw.it->Seek(start);
    double n = 0;
    for (; raw.it->Valid() && in(raw.key()); raw.it->Next()) {
      n++;
      w.match_key_bytes += raw.key().size();
    }
    double end = raw.it->Valid() ? raw.key().size() : 0;
    w.walks++;
    w.matches += n;
    if (n == 0) { w.empty++; w.empty_key_bytes += end; }
    else w.end_key_bytes += end;
  }
  const SortedIndex& sorted_index() const {
    if (m.index_->layout() != "sorted_keys" || m.index_->date_granularity() != DateGranularity::full)
      throw std::runtime_error("predict: an index other than sorted keys at full dates (not covered)");
    return static_cast<const SortedIndex&>(*m.index_);
  }
  void walk_eq(const std::string& label, const std::string& prop, const Value& value, WalkCard& w) {
    sorted_index();
    const IndexDecl& ix = m.schema_.index_decl(label, prop);
    std::string prefix = IndexKeys::prefix(ix.id) + index_value_bytes(m.schema_, ix, label, value);
    walk(prefix, [&](std::string_view k) { return has_prefix(k, prefix); }, w);
  }
  void walk_range(const std::string& label, const std::string& prop, int64_t lo, int64_t hi, WalkCard& w) {
    sorted_index();
    const IndexDecl& ix = m.schema_.index_decl(label, prop);
    std::string prefix = IndexKeys::prefix(ix.id);
    walk(prefix + IndexKeys::sorted_bytes(std::max(lo, INT64_MIN + 1)), [&](std::string_view k) {
      return k.size() == prefix.size() + 16 && has_prefix(k, prefix) &&
             IndexKeys::sorted_value(k.substr(prefix.size(), 8)) < hi;
    }, w);
  }
  void walk_enumerate(const std::string& label, WalkCard& w) {
    sorted_index();
    const Schema& sc = m.schema_;
    std::string prefix = sc.prop_indexed(label) ? IndexKeys::prefix(sc.index_decl(label, sc.enumeration_prop(label)).id)
                                                : IndexKeys::prefix(sc.type_index_decl(label).id);
    walk(prefix, [&](std::string_view k) { return has_prefix(k, prefix); }, w);
  }
  PCounts walk_rule(const WalkCard& w) const {
    PCounts out;
    bool end_hit = planted("index_walk_end_hit");          // the plant: the landing that ends a walk counted as a hit
    out.S += w.walks;
    out.N += w.matches;                                    // a NEXT per match: the last one lands past the walk
    out.scan_key += w.match_key_bytes + (end_hit ? w.end_key_bytes : 0);
    out.used_key += w.match_key_bytes;                     // every match is output on its key
    out.S_miss += w.empty;
    out.seek_miss_key += w.empty_key_bytes;
    out.N_miss += end_hit ? 0 : w.walks - w.empty;
    out.next_miss_key += end_hit ? 0 : w.end_key_bytes;
    return out;
  }
  void walk_covered(const WalkCard& w) const {
    covered("index_walk.match", w.matches);
    covered("index_walk.empty", w.empty);
  }

  PCounts scan_rule(const ScanCard& k) const {
    PCounts out;
    out.S += k.scans;
    out.S_miss += k.empty;
    out.seek_miss_key += k.empty ? k.end_key_bytes : 0;
    out.N += k.keys;                                       // a NEXT per landed key: the last one lands past the run
    out.N_miss += k.scans - k.empty;
    out.next_miss_key += k.empty ? 0 : k.end_key_bytes;
    out.scan_key += k.key_bytes;
    out.scan_value += k.value_bytes;
    out.used_key += k.used_key;
    out.used_value += k.used_value;
    return out;
  }

  // ---- props reads (read_vertex_props, a top-k's winners) ----

  // one vertex `u` of label `lab` (its one candidate), asked `props`: the truth of its reads into the card
  void props_truth(const Component& layout, const Meta::PropsPlan& plan, uint64_t u, const std::string& lab,
                   const std::vector<std::string>& props, StepCard& c) {
    uint8_t vt = m.vtype_of(lab);
    if (layout.packed()) {   // Chunked: one probe of the label's chunk run (props_of_packed)
      std::vector<std::string> row_props;
      for (auto& p : props) if (p != "id") row_props.push_back(p);
      if (row_props.empty()) return;
      std::string prefix = vertex_family(vt);
      Landing L = land(0, prefix, u);
      c.landings.push_back(L);
      if (!L.landed || u > L.hi) return;
      raw.it->SeekForPrev(chunk_with_id(prefix, u));
      auto chunk = PropsChunk::decode(&m.cc_, &m.vcols(lab), std::string(raw.value()));
      int64_t row = chunk->find(u);
      if (row < 0) return;
      for (auto& p : row_props) {
        int col = chunk->col_index(p);
        if (col >= 0) c.chunk_used_value += chunk->cell_bytes(size_t(col), size_t(row));
      }
      return;
    }
    if (plan.grouped) {      // Condensed: one GET per touched row group (project_planned)
      if (plan.row_props.empty()) return;
      project_truth(lab, u, plan.of(lab).groups, SIZE_MAX, std::nullopt, c);
      return;
    }
    split_props_truth(plan, u, props, {lab}, c);
  }

  // Condensed: the touched row groups of `u` under `lab` (project_planned); group `have_g` was already read (`blob`)
  bool project_truth(const std::string& lab, uint64_t u, const Meta::GroupPlan& gp, size_t have_g,
                     const std::optional<std::string>& have, StepCard& c) {
    const Meta::RowGroups& rg = m.row_groups(lab);
    uint8_t vt = m.vtype_of(lab);
    bool any = false;
    for (size_t gi = 0; gi < gp.groups.size(); gi++) {
      size_t g = gp.groups[gi];
      PropsCard& k = c.props[{lab, g}];
      std::optional<std::string> blob;
      if (g == have_g) blob = have;
      else {
        k.asked++;
        blob = raw.get(vertex_group_key(vt, u, g));
        if (blob) { k.hits++; k.value_bytes += blob->size(); }
      }
      if (!blob) continue;
      any = true;
      columns::View view(&rg.cols[g], *blob, 0, 1);
      for (auto& [i, col] : gp.cells[gi]) k.used_cells += view.cell_bytes(col, 0);
    }
    return any;
  }
  // Condensed over a candidate label (probe_planned): the first touched group's GET; when it misses and is not group
  // 0, the existence GET of the row key decides the label; then the other touched groups
  bool probe_truth(const std::string& lab, uint64_t u, const Meta::GroupPlan& gp, StepCard& c) {
    size_t g = gp.groups[0];
    PropsCard& k = c.props[{lab, g}];
    k.asked++;
    std::optional<std::string> blob = raw.get(vertex_group_key(m.vtype_of(lab), u, g));
    if (blob) { k.hits++; k.value_bytes += blob->size(); }
    else if (g == 0 || !exist_get(lab, u, c)) return false;
    project_truth(lab, u, gp, g, blob, c);
    return true;
  }
  // Split (split_props): one GET per asked prop; over candidate labels the first hit decides the label, and the later
  // props read that label only. Returns the label decided ("" = no hit)
  std::string split_props_truth(const Meta::PropsPlan& plan, uint64_t u, const std::vector<std::string>& props,
                                const std::vector<std::string>& labs, StepCard& c) {
    std::string decided;
    for (size_t pi = 0; pi < props.size(); pi++) {
      if (props[pi] == "id") continue;
      for (auto& lab : labs) {
        if (!decided.empty() && lab != decided) continue;
        int a = plan.of(lab).attr[pi];
        if (a < 0) continue;
        PropsCard& k = c.props[{lab, size_t(a)}];
        k.asked++;
        std::optional<std::string> v = raw.get(vertex_attr_key(m.vtype_of(lab), u, uint8_t(a)));
        if (!v) continue;
        k.hits++;
        k.value_bytes += v->size();
        decided = lab;
        break;
      }
    }
    return decided;
  }

  void props_covered(const Component& layout, const StepCard& c) {
    if (layout.packed()) return;
    std::string r = layout.grouped() ? "props_condensed." : "props_split.";
    for (auto& [key, k] : c.props) {
      covered(r + "get_hit", k.hits);
      covered(r + "get_miss", k.asked - k.hits);
    }
  }

  PCounts props_rule(const Component& layout, const StepCard& c) const {
    PCounts out;
    if (layout.packed()) return out;   // the replay prices Chunked
    for (auto& [key, k] : c.props) {
      const std::string& lab = key.first;
      uint8_t vt = m.vtype_of(lab);
      double K = layout.grouped() ? vertex_group_key(vt, 0, key.second).size()
                                    : vertex_attr_key(vt, 0, uint8_t(key.second)).size();
      double gets = k.asked + (planted("props_get_per_vertex") ? 1 : 0);   // the plant: one extra GET per card
      out.G += gets;                                       // 1 GET per asked row group (Condensed) or prop (Split)
      out.G_miss += gets - k.hits;
      out.get_key += k.hits * K;
      out.get_miss_key += (gets - k.hits) * K;
      out.get_value += k.value_bytes;
      out.used_key += k.hits * K;
      out.used_value += layout.grouped() ? k.used_cells : k.value_bytes;   // Split's whole value is the asked cell
    }
    return out;
  }

  // ---- the step ----

  PCounts chunk_rule(const StepCard& c) const {
    std::vector<Landing> ls = c.landings;
    // every probe its own batch: no chunk is ever free (asked only with landings: with none it changes nothing, and
    // the gate re-prices a step for each plant its rules ask)
    if (!ls.empty() && planted("chunked_batch"))
      for (size_t i = 0; i < ls.size(); i++) ls[i].group = i + 1;
    Counts r = replay_landings(ls);
    covered("chunked.probe_charged", double(r.S - r.S_miss));
    covered("chunked.probe_free", double(ls.size() - r.S));
    covered("chunked.probe_before_first_chunk", double(r.S_miss));
    PCounts out = PCounts::of(r);
    out.used_value += c.chunk_used_value;
    return out;
  }

  // the step's whole price from its card: every rule (each is 0 on the parts of the card a step did not fill)
  PCounts all_rules(const Component& layout, const StepCard& c) {
    props_covered(layout, c);
    exist_covered(c);
    PCounts out = props_rule(layout, c);
    out.add(exist_rule(c));
    out.add(chunk_rule(c));
    return out;
  }

  // read_vertex_by_id over candidates: the first candidate that exists under one of the labels wins. A candidate's
  // carried label decides it with no read (the run's after_vertex_write case does not arise: predict runs no writes)
  PCounts by_id(const json& s, StepCard& c) {
    std::vector<std::string> labs = strs(s, "label");
    bool item = true;                      // run_kv_op's choice: Split (item) unless the labels' layout is grouped
    if (labs.empty()) for (auto& v : m.schema_.vertices()) item = item && !m.cfg_.props_of(v.label).grouped();
    else item = !m.props_layout("read_vertex_by_id", labs).grouped();
    std::vector<std::string> all;
    for (auto& v : m.schema_.vertices()) all.push_back(v.label);
    if (!item) labs = m.labels_or_throw("read_vertex_by_id", labs);
    bool packed = !item && m.props_layout("read_vertex_by_id", labs).packed();
    const std::vector<std::string>& probe = labs.empty() ? all : labs;
    for (auto& cand : vids_of(s, "vid")) {
      uint64_t u = cand.as_vid();
      if (std::optional<std::string> k = m.tag_label(cand)) {
        if (std::find(probe.begin(), probe.end(), *k) != probe.end()) break;
        continue;
      }
      bool hit = false;
      for (auto& lab : probe) {
        if (item) hit = exist_split(lab, u, c);
        else if (packed) hit = chunk_row(0, lab, u, c) >= 0;
        else hit = exist_get(lab, u, c);
        if (hit) break;
      }
      if (hit) break;
    }
    covered("by_id.candidates", 1);
    return all_rules(m.cfg_.props_of(probe[0]), c);
  }

  // read_anchor_props: per candidate, its props under the first label that holds it; the first candidate found ends it
  PCounts anchor_props(const json& s, StepCard& c) {
    std::vector<std::string> labs = m.labels_or_throw("read_anchor_props", strs(s, "labels"));
    std::vector<std::string> props = strs(s, "props");
    const Component& pl = m.props_layout("read_anchor_props", labs);
    Meta::PropsPlan plan = m.props_plan(pl, props, labs);
    std::vector<std::string> row_props;
    for (auto& p : props) if (p != "id") row_props.push_back(p);
    for (auto& cand : vids_of(s, "cands")) {
      uint64_t u = cand.as_vid();
      std::vector<std::string> mine = m.narrow(cand, labs);
      bool found = false;
      if (!pl.grouped()) {                 // Split: the props' first hit decides the label; else the existence probe
        std::string decided = split_props_truth(plan, u, props, mine, c);
        for (size_t i = 0; decided.empty() && i < mine.size(); i++)
          if (exist_split(mine[i], u, c)) decided = mine[i];
        found = !decided.empty();
      } else if (pl.packed()) {            // Chunked: each label's chunk probe until the row is found
        for (auto& lab : mine) {
          std::shared_ptr<PropsChunk> chunk;
          int64_t row = chunk_row(0, lab, u, c, &chunk);
          if (row < 0) continue;
          for (auto& p : row_props) {
            int col = chunk->col_index(p);
            if (col >= 0) c.chunk_used_value += chunk->cell_bytes(size_t(col), size_t(row));
          }
          found = true;
          break;
        }
      } else {                             // Condensed: probe_label per label (the row props' groups, else group 0)
        for (auto& lab : mine)
          if (probe_truth(lab, u, m.group_plan(lab, row_props), c)) { found = true; break; }
      }
      if (found) break;
    }
    covered("anchor_props.candidates", 1);
    return all_rules(pl, c);
  }

  // read_filter_by_label_probe: one existence probe per distinct vid whose label it does not carry
  PCounts filter_probe(const json& s, StepCard& c) {
    std::vector<std::string> labs = strs(s, "label");
    if (labs.size() != 1) throw std::runtime_error("predict: read_filter_by_label_probe needs one label");
    const Component& pl = m.cfg_.props_of(labs[0]);
    DistinctVids d = distinct_vids("meta", vids_of(s, "vids"), false);
    for (size_t i = 0; i < d.uids.size(); i++) {
      if (m.tag_label(d.values[i])) continue;
      if (pl.packed()) chunk_row(0, labs[0], d.uids[i], c);
      else if (!pl.grouped()) exist_split(labs[0], d.uids[i], c);
      else exist_get(labs[0], d.uids[i], c);
    }
    return all_rules(pl, c);
  }

  // read_vertices_by_label with props: one scan of the label's run, every key and value read
  void scan_truth(const json& s, const std::string& label, ScanCard& k) {
    const json& a = s.at("args");
    const Component& pl = m.cfg_.props_of(label);
    const std::vector<ColDecl>* cols = &m.vcols(label);
    std::string prefix = vertex_family(m.vtype_of(label));
    std::vector<std::string> row_props;
    for (auto& p : a.at("props")) if (p.get<std::string>() != "id") row_props.push_back(p.get<std::string>());
    bool filtered = a.contains("keep") && !a["keep"].is_null();
    std::set<uint64_t> kept;
    if (filtered)
      for (auto& v : vids_of(s, "keep")) kept.insert(v.as_vid());
    auto wanted = [&](uint64_t u) { return !filtered || kept.count(u) > 0; };
    k.scans++;
    raw.it->Seek(prefix);
    if (!raw.it->Valid() || !has_prefix(raw.key(), prefix)) {
      k.empty++;
      k.end_key_bytes += raw.it->Valid() ? raw.key().size() : 0;
      return;
    }
    const Meta::RowGroups* rg = pl.grouped() && !pl.packed() ? &m.row_groups(label) : nullptr;
    std::vector<size_t> touch = rg && !row_props.empty() ? m.groups_for(label, row_props) : std::vector<size_t>{};
    std::vector<int> attr_slot(256, -1);
    if (!pl.grouped())
      for (size_t i = 0; i < row_props.size(); i++) {
        int at = m.attr_id(label, row_props[i]);
        if (at >= 0) attr_slot[size_t(at)] = int(i);
      }
    const size_t vid_at = prefix.size(), plain = prefix.size() + 8;
    for (; raw.it->Valid() && has_prefix(raw.key(), prefix); raw.it->Next()) {
      std::string_view key = raw.key(), v = raw.value();
      k.keys++;
      k.key_bytes += key.size();
      k.value_bytes += v.size();
      if (pl.packed()) {                   // Chunked: a chunk; the kept rows' asked cells
        std::shared_ptr<PropsChunk> chunk = PropsChunk::decode(&m.cc_, cols, std::string(v));
        chunk->bulk();
        for (size_t i = 0; i < chunk->size(); i++) {
          if (!wanted(chunk->ids()[i])) continue;
          for (auto& p : row_props) {
            int col = chunk->col_index(p);
            if (col >= 0) k.used_value += chunk->cell_bytes(size_t(col), i);
          }
        }
        continue;
      }
      uint64_t u = trailing_u64(key.substr(vid_at, 8));
      uint8_t sub = key.size() > plain ? uint8_t(key[plain]) : 0;
      if (!wanted(u)) continue;
      if (rg) {                            // Condensed: one row group's row
        columns::View row(&rg->cols[sub], v, 0, 1);
        for (auto& p : row_props) {
          int col = row.col_index(p);
          if (col >= 0) k.used_value += row.cell_bytes(size_t(col), 0);
        }
        if (std::find(touch.begin(), touch.end(), size_t(sub)) != touch.end()) k.used_key += key.size();
      } else if (attr_slot[sub] >= 0) {    // Split: an asked prop's record
        k.used_key += key.size();
        k.used_value += v.size();
      }
    }
    k.end_key_bytes += raw.it->Valid() ? raw.key().size() : 0;
  }

  // read_chase: level by level over the chain heads, each node's FIRST neighbour; a node is read once
  PCounts chase(const json& s, StepCard& c) {
    const json& a = s.at("args");
    std::string etype = a.at("etype").get<std::string>();
    std::string dir = kv::dir_of(a);
    std::optional<int64_t> max_hops;
    if (a.contains("max_hops") && !a["max_hops"].is_null()) max_hops = a["max_hops"].get<int64_t>();
    std::vector<bool> dirs = m.dirs_of("read_chase", etype, dir);
    uint8_t eid = uint8_t(m.schema_.edge(etype).id);
    const std::vector<ColDecl>* cols = &m.ecols(etype);
    c.chase_lists.assign(dirs.size(), HopDirCard());
    c.chase_first.assign(dirs.size(), FirstCard());
    // a node's first neighbour, read the way the run reads it, its reads into the card
    auto first_of = [&](uint64_t su, uint64_t level) -> std::optional<uint64_t> {
      for (size_t di = 0; di < dirs.size(); di++) {
        bool d = dirs[di];
        const Component& al = m.cfg_.adj_of(etype, d);
        unsigned bits = m.far_bits(etype, d);
        if (al.packed()) {
          if (al.props_own()) throw std::runtime_error("predict: a chase over edge props in their own chunks (not covered)");
          std::string prefix = padj_prefix(eid, d);
          Landing L = land(level, prefix, su);
          c.landings.push_back(L);
          if (!L.landed || su > L.hi) continue;
          raw.it->SeekForPrev(chunk_with_id(prefix, su));
          lists::ChunkView chunk(cols, std::string(raw.value()), bits);
          int64_t i = chunk.find(su);
          if (i < 0) continue;
          std::vector<uint64_t> dsts = chunk.dsts(size_t(i));
          if (dsts.empty()) continue;
          c.chunk_used_value += chunk.id_widths(size_t(i))[0];
          return dsts[0];
        }
        std::string prefix = adj_prefix(eid, d, su);
        raw.it->Seek(prefix);
        if (!al.grouped()) {                 // Split: the landing on the first record
          FirstCard& k = c.chase_first[di];
          k.probes++;
          if (raw.it->Valid() && has_prefix(raw.key(), prefix)) {
            k.hits++;
            k.hit_key_bytes += raw.key().size();
            if (bits) k.hit_value_bytes += raw.value().size();
            return edge_dst(raw.key());
          }
          k.miss_key_bytes += raw.it->Valid() ? raw.key().size() : 0;
          continue;
        }
        HopDirCard& k = c.chase_lists[di];   // Condensed: the whole list (or all its parts), its first entry used
        k.probes++;
        if (!raw.it->Valid()) continue;
        std::vector<std::string> parts;
        if (raw.key() == prefix) parts.emplace_back(raw.value());
        else if (unit::owns(raw.key(), prefix)) {
          k.split_lists++;
          for (; raw.it->Valid() && unit::owns(raw.key(), prefix); raw.it->Next()) {
            k.parts++;
            k.part_key_bytes += raw.key().size();
            parts.emplace_back(raw.value());
          }
          k.end_key_bytes += raw.it->Valid() ? raw.key().size() : 0;
        } else { k.absent_key_bytes += raw.key().size(); continue; }
        k.present++;
        for (auto& p : parts) k.value_bytes += p.size();
        for (auto& p : parts) {
          lists::View lv(cols, p, bits);
          if (lv.size()) { k.used_value += lv.id_bytes(0); return lv.dsts()[0]; }
        }
      }
      return std::nullopt;
    };
    std::vector<uint64_t> cur;
    for (auto& v : vids_of(s, "srcs")) cur.push_back(v.as_vid());
    std::vector<bool> done(cur.size(), false);
    std::map<uint64_t, std::optional<uint64_t>> next_of;
    for (int64_t hops = 0; !max_hops || hops < *max_hops; hops++) {
      std::vector<uint64_t> frontier;
      std::set<uint64_t> in_level;
      for (size_t i = 0; i < cur.size(); i++)
        if (!done[i] && !next_of.count(cur[i]) && in_level.insert(cur[i]).second) frontier.push_back(cur[i]);
      for (uint64_t su : frontier) next_of[su] = first_of(su, uint64_t(hops));
      bool moved = false;
      for (size_t i = 0; i < cur.size(); i++) {
        if (done[i]) continue;
        const std::optional<uint64_t>& n = next_of.at(cur[i]);
        if (!n) { done[i] = true; continue; }
        cur[i] = *n;
        moved = true;
      }
      if (!moved) break;
    }
    PCounts out;
    for (size_t di = 0; di < dirs.size(); di++) {
      const FirstCard& f = c.chase_first[di];
      out.S += f.probes;                     // Split: 1 SEEK per node, no NEXT; the landed key is used
      out.S_miss += f.probes - f.hits;
      out.scan_key += f.hit_key_bytes;
      out.scan_value += f.hit_value_bytes;
      out.seek_miss_key += f.miss_key_bytes;
      out.used_key += f.hit_key_bytes;
      condensed_lanes(eid, dirs[di], c.chase_lists[di], out);
      covered("chase.split_first", f.hits);
      covered("chase.split_absent", f.probes - f.hits);
      covered("chase.condensed_list", c.chase_lists[di].present);
    }
    covered("chase.chunked_probe", double(c.landings.size()));
    out.add(chunk_rule(c));
    return out;
  }

  // read_edge_exists: the plan's reads, each by the layout of the (etype, dir) it names. Condensed / Chunked: the lists
  // of the anchors (or of the candidates), no entry used; Split: one GET per (candidate, anchor) on the edge's key until
  // a hit. A read marked unless_found reads only the candidates not found yet, in a replay group of its own
  PCounts edge_exists(const json& s) {
    const json& a = s.at("args");
    if (a.at("rows").empty()) {   // no candidate: nothing read (the truth-card levels: its counts are 0)
      tset("rows@", 0);
      tset("nothers@", 0);
      for (size_t i = 0; i < a.at("reads").size(); i++) {
        tset("eown" + std::to_string(i) + "@", 0);
        tset("eprobe" + std::to_string(i) + "@", 0);
      }
      return PCounts();
    }
    std::string etype = a.at("etype").get<std::string>(), field = a.at("field").get<std::string>();
    uint8_t eid = uint8_t(m.schema_.edge(etype).id);
    Args ra;
    ra.set("reads", from_json(a.at("reads")));
    std::vector<EdgeRead> reads = edge_reads(ra, "meta");
    PCounts out;
    StepCard all;                                         // every read's landings: one replay
    auto take = [&](const Meta::HopShape& sh, const std::string& et, StepCard& c,
                    const std::map<bool, std::string>& suffix = {}) {
      hop_terms(sh, et, c, suffix);   // the truth-card levels
      out.add(hop_rule(sh, et, c));
      all.landings.insert(all.landings.end(), c.landings.begin(), c.landings.end());
      all.chunk_used_value += c.chunk_used_value;
    };
    std::vector<uint64_t> others;
    if (a.contains("others") && !a["others"].is_null()) {
      for (auto& v : vids_of(s, "others")) others.push_back(v.as_vid());
    } else {                                              // the via walk: a hop, every entry used
      std::string via = a.at("via_etype").get<std::string>();
      Meta::HopShape sh = m.hop_shape(via, "out", {});
      StepCard c;
      hop_truth(sh, via, vids_of(s, "via_srcs"), {}, c, 0, &others);
      take(sh, via, c);
      if (terms) {   // the truth-card levels: the via walk's distinct sources and its entries
        std::set<uint64_t> vs;
        for (auto& v : vids_of(s, "via_srcs")) vs.insert(v.as_vid());
        double e = 0;
        for (auto& k : c.dirs) e += k.entries;
        tset("nvia@", double(vs.size()));
        tset("nvnbrs@", e);
      }
      covered("edge_check.via_walk", 1);
    }
    std::vector<uint64_t> anchors;
    std::set<uint64_t> anchor_set;
    for (uint64_t u : others) if (anchor_set.insert(u).second) anchors.push_back(u);
    std::vector<uint64_t> cands;
    std::set<uint64_t> cand_seen;
    for (auto& r : a.at("rows")) {
      uint64_t u;
      if (!canonical_vid(r.at(field).get<std::string>(), &u)) throw std::runtime_error("predict: a candidate is not a vid");
      if (cand_seen.insert(u).second) cands.push_back(u);
    }
    // the lists of `vids` in `dirs`, no entry used: one pass per class (Chunked first, then the others), as lists_of
    // `idx`: the read each direction is (its terms' part carries "#<read>")
    auto lists_of = [&](const std::vector<uint64_t>& vids, const std::vector<bool>& dirs, uint64_t group,
                        const std::vector<size_t>& idx) {
      std::map<bool, std::string> suffix;
      for (size_t t = 0; t < dirs.size(); t++) suffix[dirs[t]] = "#" + std::to_string(idx[t]);
      std::vector<std::vector<uint64_t>> joined(vids.size());
      ValueList vs;
      for (uint64_t u : vids) vs.push_back(Value::vid(u));
      for (bool packed : {true, false}) {
        Meta::HopShape sh;
        for (bool d : dirs)
          if (m.cfg_.adj_of(etype, d).packed() == packed) sh.dirs.push_back(d);
        if (sh.dirs.empty()) continue;
        sh.two = false;
        sh.packed = packed;
        StepCard c;
        c.entries_used = false;
        std::vector<std::vector<uint64_t>> got;
        hop_truth(sh, etype, vs, {}, c, group, nullptr, &got);
        take(sh, etype, c, suffix);
        for (size_t i = 0; i < got.size(); i++) joined[i].insert(joined[i].end(), got[i].begin(), got[i].end());
      }
      return joined;
    };
    auto list_like = [&](const EdgeRead& r) {
      const Component& al = m.cfg_.adj_of(etype, r.out);
      return al.packed() || al.grouped();
    };
    tset("rows@", double(cands.size()));   // the truth-card levels: the candidates, the anchors, per read its owners and GETs
    tset("nothers@", double(anchors.size()));
    for (size_t i = 0; i < reads.size(); i++) {
      tset("eown" + std::to_string(i) + "@", 0);
      tset("eprobe" + std::to_string(i) + "@", 0);
    }
    std::set<uint64_t> have;
    for (size_t i = 0; i < reads.size(); i++) {
      const EdgeRead& r = reads[i];
      std::vector<uint64_t> todo;
      for (uint64_t u : cands) if (!r.unless_found || !have.count(u)) todo.push_back(u);
      if (todo.empty()) continue;
      if (list_like(r) && r.at_anchor) {
        for (size_t j = i; j < reads.size(); j++) {   // the reads merged below read the anchors' lists
          tset("eown" + std::to_string(j) + "@", double(anchors.size()));
          if (j + 1 >= reads.size() || r.unless_found || !list_like(reads[j + 1]) || !reads[j + 1].at_anchor ||
              reads[j + 1].unless_found)
            break;
        }
      } else if (list_like(r)) {
        tset("eown" + std::to_string(i) + "@", double(todo.size()));
      }
      uint64_t group = r.unless_found ? uint64_t(2 + i) : 1;
      if (list_like(r) && r.at_anchor) {
        std::vector<bool> dirs = {r.out};
        std::vector<size_t> idx = {i};
        while (!r.unless_found && i + 1 < reads.size() && list_like(reads[i + 1]) && reads[i + 1].at_anchor &&
               !reads[i + 1].unless_found) {
          dirs.push_back(reads[++i].out);
          idx.push_back(i);
        }
        for (auto& l : lists_of(anchors, dirs, group, idx)) have.insert(l.begin(), l.end());
        covered("edge_check.anchor_lists", 1);
      } else if (list_like(r)) {
        std::vector<std::vector<uint64_t>> back = lists_of(todo, {r.out}, group, {i});
        for (size_t t = 0; t < todo.size(); t++)
          for (uint64_t n : back[t])
            if (anchor_set.count(n)) { have.insert(todo[t]); break; }
        covered("edge_check.candidate_lists", 1);
      } else {
        PropsCard& k = all.edge_gets[r.out];
        double asked0 = k.asked, hits0 = k.hits;
        for (uint64_t b : todo)
          for (uint64_t au : anchors) {
            k.asked++;
            std::optional<std::string> v = raw.get(r.at_anchor ? edge_key(eid, r.out, au, b) : edge_key(eid, r.out, b, au));
            if (!v) continue;
            k.hits++;
            k.value_bytes += v->size();
            have.insert(b);
            break;
          }
        tset("eprobe" + std::to_string(i) + "@", k.asked - asked0);   // the truth-card levels
        covered("edge_check.split_get_hit", k.hits - hits0);
        covered("edge_check.split_get_miss", (k.asked - asked0) - (k.hits - hits0));
      }
    }
    for (auto& [dir, k] : all.edge_gets)                  // Split: 1 GET per (candidate, anchor); a hit's key is used
      get_lanes(double(edge_key(eid, dir, 0, 0).size()), k, 0, out);
    out.add(chunk_rule(all));
    return out;
  }

  // ---- writes: the truth card ----
  // The run's shadow overlay lives for the whole plan run (exec/executor.cc) and every read merges it, so a write step
  // reads what the request's earlier steps wrote. The card reads the pre-request store merged with `replay`: the items
  // its own rules wrote at those steps, so a wrong write rule also shows on a later step's read.
  WriteReplay* replay = nullptr;
  using MergedKv = std::optional<std::pair<std::string, std::string>>;   // a landing (key, value); none = past the end
  using Chunk = ChunkWriteReader::Chunk;

  // the plant write_replay_off: a step reads the pre-request store only (the earlier steps' writes unseen)
  bool replay_on() { return !planted("write_replay_off"); }
  MergedKv seek_merged(const std::string& target) {   // the first key >= target, the replay winning over the store
    if (!replay_on()) {
      raw.it->Seek(target);
      if (!raw.it->Valid()) return std::nullopt;
      return std::make_pair(std::string(raw.key()), std::string(raw.value()));
    }
    MergedKv base, ov;
    for (raw.it->Seek(target); raw.it->Valid(); raw.it->Next())
      if (!replay->items.count(std::string(raw.key()))) { base = std::make_pair(std::string(raw.key()), std::string(raw.value())); break; }
    for (auto it = replay->items.lower_bound(target); it != replay->items.end(); ++it)
      if (it->second) { ov = std::make_pair(it->first, *it->second); break; }
    if (!base || !ov) return base ? base : ov;
    return ov->first < base->first ? ov : base;
  }
  MergedKv seek_for_prev_merged(const std::string& target) {   // the last key <= target
    if (!replay_on()) {
      raw.it->SeekForPrev(target);
      if (!raw.it->Valid()) return std::nullopt;
      return std::make_pair(std::string(raw.key()), std::string(raw.value()));
    }
    MergedKv base, ov;
    for (raw.it->SeekForPrev(target); raw.it->Valid(); raw.it->Prev())
      if (!replay->items.count(std::string(raw.key()))) { base = std::make_pair(std::string(raw.key()), std::string(raw.value())); break; }
    for (auto it = replay->items.upper_bound(target); it != replay->items.begin();) {
      --it;
      if (it->second) { ov = std::make_pair(it->first, *it->second); break; }
    }
    if (!base || !ov) return base ? base : ov;
    return ov->first > base->first ? ov : base;
  }
  // a SEEK's landing as the run's cursor charges it (store/counting.cc): the key on landing, the value when read; a miss
  // moves the key to seek_miss_key
  static void land_hit(PCounts& o, const MergedKv& l, bool value_read) {
    o.S++;
    o.scan_key += double(l->first.size());
    if (value_read) o.scan_value += double(l->second.size());
  }
  static void land_miss(PCounts& o, const MergedKv& l) {
    o.S++;
    o.S_miss++;
    o.seek_miss_key += l ? double(l->first.size()) : 0;
  }
  // a write task's puts and dels, applied as ChunkWrites::apply does (dels, puts, late dels): counted and replayed
  void apply(PCounts& o, const ChunkWrites& w) {
    auto del = [&](const std::string& k) { o.D++; o.write_key += double(k.size()); replay->items[k] = std::nullopt; };
    for (auto& k : w.dels) del(k);
    for (auto& [k, v] : w.puts) {
      o.P++;
      o.write_key += double(k.size());
      o.write_value += double(v.size());
      replay->items[k] = v;
    }
    for (auto& k : w.late_dels) del(k);
  }

  // a write's Condensed RMW, for the truth-card levels: the owner's list found or not, its entries and bytes (a split
  // list: the part read), as a hop's direction sums them (terms_from_sums: present, list_entries_mean, adj_value_mean);
  // no list: the new one's single entry and bytes (adj_value_mean, the bytes an entry adds)
  void write_list_terms(const std::string& etype, bool out, const MergedKv& at, double entries, double new_bytes) {
    std::string P = adj_term_part(etype, out);
    tadd("present@" + P, at ? 1 : 0);
    tadd("entries@" + P, at ? entries : 1);
    tadd("value_bytes@" + P, at ? double(at->second.size()) : new_bytes);
  }
  // unit::rmw_locate (store/unit_value.cc): the unit's own key first (1 SEEK; a whole unit is that landing), a split
  // unit adds the reverse SEEK to the part holding `id`; absent: a miss. The unit's key is used once
  MergedKv rmw_locate_truth(PCounts& o, const std::string& key, uint64_t id) {
    MergedKv l = seek_merged(key);
    if (!l || !unit::owns(l->first, key)) {
      covered("condensed_write.list_absent", 1);
      land_miss(o, l);
      return std::nullopt;
    }
    o.used_key += double(key.size());
    if (l->first == key) {
      covered("condensed_write.list_whole", 1);
      land_hit(o, l, true);
      return l;
    }
    covered("condensed_write.list_split_part", 1);
    if (planted("condensed_write_one_seek")) {   // the plant: the split unit's first landing taken as the list
      land_hit(o, l, true);
      return l;
    }
    land_hit(o, l, false);
    MergedKv p = seek_for_prev_merged(unit::part_key(key, id));
    if (!p || !unit::owns(p->first, key)) { land_miss(o, p); return std::nullopt; }
    land_hit(o, p, true);
    return p;
  }

  // a charged decode of the chunk at `key` (value `raw`) into the step's cache, as ChunkWriteReader::locate: when it is its
  // family's last chunk (the merged store's final key under `prefix`), the read touch of the last-chunk lane
  Chunk& decode_charged(PCounts& o, std::map<std::string, Chunk>& cache, const std::string& prefix, uint64_t target,
                        const std::string& key, const std::string& raw, int kind, const std::vector<ColDecl>* cols,
                        bool ords, unsigned lb) {
    Chunk c = ChunkWriteReader::decode(m.cc_, kind, cols, ords, raw, lb);
    c.stored_bytes = raw.size();
    MergedKv fin = seek_for_prev_merged(chunk_with_id(prefix, ~0ull));
    if (fin && fin->first == key) {
      c.is_last = true;
      c.target = target;
      o.lc_reads += 1;
      o.lc_read_bytes += double(raw.size());
      o.lc_read_target += double(target);
    }
    return cache.emplace(key, std::move(c)).first->second;
  }
  // chunk_rmw (exec/chunk_write.cc) with ChunkWriteReader::locate's per-step cache: a probe inside a cached chunk's
  // [lo, hi] is free; else 1 SEEK back to the chunk at or below `id` (the whole chunk read, unless already cached);
  // the rewritten chunk moves to its new first id's key (the old key DELed) or, emptied, is DELed
  void chunk_rmw_truth(PCounts& o, ChunkWrites& w, std::map<std::string, Chunk>& cache, const std::string& prefix,
                       uint64_t target, uint64_t id, int kind, const std::vector<ColDecl>* cols, bool with_ords,
                       const std::function<void(Chunk&)>& mutate, unsigned label_bits) {
    std::string key;
    const Chunk* cp = nullptr;
    const std::string fam = terms ? family_of_prefix(prefix) : "";   // the truth-card levels' terms (a write's sizes)
    if (!fam.empty()) {   // the family's last chunk as it stands (the estimate's last_chunk_value; not charged)
      MergedKv fin = seek_for_prev_merged(chunk_with_id(prefix, ~0ull));
      if (fin && chunk_has_prefix(fin->first, prefix)) {
        tadd("lc_bytes@" + fam, double(fin->second.size()));
        tadd("lc_n@" + fam, 1);
      }
    }
    for (auto& [k, c] : cache)
      if (chunk_has_prefix(k, prefix) && c.lo <= id && id <= c.hi) { key = k; cp = &c; break; }
    if (!cp) {
      MergedKv l = seek_for_prev_merged(chunk_with_id(prefix, id));
      if (!l || !chunk_has_prefix(l->first, prefix)) {
        covered("chunk_write.new_chunk", 1);
        land_miss(o, l);
      } else {
        key = l->first;
        auto it = cache.find(key);
        land_hit(o, l, it == cache.end());
        cp = it == cache.end() ? &decode_charged(o, cache, prefix, target, key, l->second, kind, cols, with_ords, label_bits)
                               : &it->second;
        covered("chunk_write.rmw", 1);
        if (!fam.empty() && it == cache.end()) {   // the chunk read (chunk_value_mean), and whether it is the last
          tadd("wc_bytes@" + fam, double(cp->stored_bytes));
          tadd("wc_n@" + fam, 1);
          tadd("lland@" + fam, cp->is_last ? 1 : 0);   // (chunk_last_share)
          tadd("lland_n@" + fam, 1);
        }
      }
    }
    const double before = cp ? double(cp->stored_bytes) : 0.0;   // the chunk's bytes before this rewrite
    Chunk c;
    c.kind = kind;
    if (kind == 3 || kind == 4) c.label_bits = label_bits;
    if (cp) c = *cp;
    mutate(c);
    if (c.adj()) std::stable_sort(c.groups.begin(), c.groups.end(), [](auto& a, auto& b) { return a.src < b.src; });
    else std::stable_sort(c.rows.begin(), c.rows.end(), [](auto& a, auto& b) { return a.first < b.first; });
    if (!key.empty()) cache.erase(key);
    if (!c.empty()) {
      std::string newkey = chunk_with_id(prefix, c.first());
      if (!key.empty() && key != newkey) {
        covered("chunk_write.first_id_moves", 1);
        if (!planted("chunk_write_no_first_id_del")) w.drop_chunk(key);
      }
      std::string value = serialize_chunk(m.cc_, cols, c, with_ords);
      if (!fam.empty()) {   // the bytes this rewrite added (or, a removal, took away)
        if (double(value.size()) > before) {   // an insert's growth: one entry (a delete's d entries are not a term)
          tadd("wgrow@" + fam, double(value.size()) - before);
          tadd("wgrow_n@" + fam, 1);
        }
      }
      if (c.is_last) {   // the write touch of the last-chunk lane (ChunkWrites::apply's note)
        o.lc_writes += 1;
        o.lc_write_bytes += double(value.size());
        o.lc_write_base += double(c.stored_bytes);
        o.lc_write_target += double(c.target);
        c.stored_bytes = value.size();
      }
      w.put_chunk(newkey, std::move(value));
      c.lo = c.first();
      c.hi = c.last();
      cache[newkey] = std::move(c);
    } else if (!key.empty()) {
      w.drop_chunk(key);
    }
  }

  static uint64_t vid_arg(const json& a, const char* k) {
    uint64_t u;
    if (!canonical_vid(a.at(k).get<std::string>(), &u)) throw std::runtime_error("predict: not a canonical vid");
    return u;
  }

  // insert_edge (formats/meta/writes.cc insert_edge): each stored direction by its own layout. Split: one record PUT,
  // no read. Condensed: the list (or the part holding the other end) read and rewritten whole. Chunked: the owner's chunk
  // read and rewritten (own placement: + the next edge-prop ordinal's SEEK and the edge-prop chunk's rewrite)
  PCounts insert_edge_truth(const json& s) {
    const json& a = s.at("args");
    std::string etype = a.at("etype").get<std::string>();
    uint64_t su = vid_arg(a, "src"), du = vid_arg(a, "dst");
    std::string src_label = a.value("src_label", ""), dst_label = a.value("dst_label", "");
    Value props = a.contains("props") && !a["props"].is_null() ? from_json(a["props"]) : Value::row({});
    uint8_t eid = uint8_t(m.schema_.edge(etype).id);
    bool rev = m.reverse_available(etype);
    const std::vector<ColDecl>& ec = m.ecols(etype);
    bool has_ep = !ec.empty();
    auto far_of = [&](bool out, const std::string& label) -> std::optional<uint8_t> {
      if (!m.far_bits(etype, out)) return std::nullopt;
      return m.far_code(etype, out, label);
    };
    std::optional<uint8_t> far_out = far_of(true, dst_label);
    std::optional<uint8_t> far_in = rev ? far_of(false, src_label) : std::nullopt;
    const Component& out_layout = m.cfg_.adj_of(etype, true);
    bool inline_props = !out_layout.props_own();
    int kind = 3;   // codec/lists.h's chunk in both placements (writes.cc adj_kind)
    static const std::vector<ColDecl> no_cols;
    const std::vector<ColDecl>* acols = inline_props ? &ec : &no_cols;
    bool ords = !inline_props && has_ep;
    PCounts o;
    ChunkWrites w;
    std::map<std::string, Chunk> cache;
    auto adj_rmw = [&](bool out, uint64_t owner, const std::function<void(Chunk&)>& mut) {
      chunk_rmw_truth(o, w, cache, padj_prefix(eid, out), m.cfg_.adj_of(etype, out).owners.pack, owner, kind, acols, ords, mut, m.far_bits(etype, out));
    };
    if (out_layout.packed() && !inline_props) {   // Chunked, own placement: out chunk, in chunk (if kept), the ep row
      covered("insert_edge.own_placement", 1);
      covered("chunk_write.ep_ordinal", has_ep ? 1 : 0);
      uint64_t tag = tag_of_uid(su), ordinal = kNoOrdinal;
      if (has_ep) {   // the next free ordinal: the last ep chunk's last row + 1 (1 SEEK, through the cache)
        std::string pre = pep_prefix(eid, uint8_t(tag));
        ordinal = 0;
        const Chunk* last = nullptr;
        for (auto& [k, c] : cache)
          if (chunk_has_prefix(k, pre) && c.lo <= ~0ull && ~0ull <= c.hi) last = &c;
        if (!last && !planted("chunk_write_no_ordinal_seek")) {
          MergedKv l = seek_for_prev_merged(chunk_with_id(pre, ~0ull));
          if (!l || !chunk_has_prefix(l->first, pre)) land_miss(o, l);
          else {
            land_hit(o, l, true);
            last = &decode_charged(o, cache, pre, m.cfg_.adj_of(etype, true).owners.pack, l->first, l->second, 2, &ec, false, 0);
          }
        }
        if (last)
          for (auto& row : last->rows) ordinal = std::max(ordinal, row.first + 1);
      }
      adj_rmw(true, su, [&](Chunk& c) { chunk_adj_add(c.groups, su, du, ordinal, far_out); });
      if (rev) adj_rmw(false, du, [&](Chunk& c) { chunk_adj_add(c.groups, du, su, ordinal, far_in); });
      if (has_ep)
        chunk_rmw_truth(o, w, cache, pep_prefix(eid, uint8_t(tag)), m.cfg_.adj_of(etype, true).owners.pack, ordinal, 2, &ec, false,
                        [&](Chunk& c) { chunk_upsert(c.rows, ordinal, props); }, 0);
      apply(o, w);
      return o;
    }
    std::optional<std::string> val;   // a Split record's value, the same in each Split copy (+ its far label byte)
    auto copy = [&](bool out) {
      uint64_t owner = out ? su : du, other = out ? du : su;
      std::optional<uint8_t> far = out ? far_out : far_in;
      const Component& al = m.cfg_.adj_of(etype, out);
      if (al.packed()) {   // Chunked, inline props: this direction's chunk, with the props
        adj_rmw(out, owner, [&](Chunk& c) { chunk_adj_add_inline(c.groups, owner, other, props, has_ep, far); });
      } else if (al.grouped()) {   // Condensed: the unit (or the part holding `other`) read and rewritten
        std::string key = adj_prefix(eid, out, owner);
        MergedKv at = rmw_locate_truth(o, key, other);
        lists::Entries entries;
        unsigned lb = m.far_bits(etype, out);
        if (at) entries = lists::View(&ec, at->second, lb).entries();
        const double before = double(entries.size());
        auto it = std::lower_bound(entries.begin(), entries.end(), other,
                                   [](const lists::Entry& e, uint64_t x) { return e.id < x; });
        entries.insert(it, {other, props.is_row() ? props : Value::row({}), far.value_or(0)});
        w.puts.emplace_back(at ? at->first : key, lists::encode(entries, lb, ec));
        write_list_terms(etype, out, at, before, double(w.puts.back().second.size()));
        covered("condensed_write.list_rmw", 1);
      } else {   // Split: one record, written blind
        if (!val) {
          val.emplace();
          std::vector<PropRow> rows = {{0, &props}};
          ValueRows rs(ec, rows);
          if (has_ep) columns::encode(*val, ec, rs);
        }
        w.puts.emplace_back(edge_key(eid, out, owner, other), (far ? std::string(1, char(*far)) : std::string()) + *val);
        covered("split_write.edge_record", 1);
      }
    };
    copy(true);
    if (rev && !planted("edge_write_no_reverse")) copy(false);
    apply(o, w);
    return o;
  }

  // a counted GET (CountingStore::get): the key and value on a hit, the key in get_miss_key on a miss
  std::optional<std::string> get_truth(PCounts& o, const std::string& key) {
    std::optional<std::string> v;
    auto it = replay->items.find(key);
    if (it != replay->items.end() && replay_on()) v = it->second;
    else v = raw.get(key);
    o.G++;
    if (v) { o.get_key += double(key.size()); o.get_value += double(v->size()); }
    else { o.G_miss++; o.get_miss_key += double(key.size()); }
    return v;
  }

  // one edge write's shared facts (formats/meta/writes.cc's lambdas, per etype)
  struct EdgeW {
    std::string etype;
    uint8_t eid;
    bool rev, has_ep, inline_props, ords;
    int kind;
    const std::vector<ColDecl>* ec;
    const std::vector<ColDecl>* acols;
  };
  EdgeW edge_w(const std::string& etype) const {
    static const std::vector<ColDecl> no_cols;
    EdgeW e;
    e.etype = etype;
    e.eid = uint8_t(m.schema_.edge(etype).id);
    e.rev = m.reverse_available(etype);
    e.ec = &m.ecols(etype);
    e.has_ep = !e.ec->empty();
    const Component& out = m.cfg_.adj_of(etype, true);
    e.inline_props = !out.props_own();
    e.kind = 3;   // codec/lists.h's chunk in both placements (writes.cc adj_kind)
    e.acols = e.inline_props ? e.ec : &no_cols;
    e.ords = !e.inline_props && e.has_ep;
    return e;
  }
  // a direction's layout: 2 Chunked, 1 Condensed, 0 Split (the names its coverage counters carry)
  static constexpr const char* kLayoutName[3] = {"split", "condensed", "chunked"};
  int layout_kind(const std::string& etype, bool out) const {
    const Component& c = m.cfg_.adj_of(etype, out);
    return c.packed() ? 2 : c.grouped() ? 1 : 0;
  }

  // delete_edge (writes.cc delete_edge): each stored direction by its layout. Split: the record DELed, no read.
  // Condensed: unit::list_remove (the list or part read; rewritten, or DELed when emptied unless it is a split unit's
  // first part). Chunked: the owner's chunk rewritten without the edge (own placement: + the edge-prop row's chunk)
  PCounts delete_edge_truth(const json& s) {
    const json& a = s.at("args");
    EdgeW e = edge_w(a.at("etype").get<std::string>());
    uint64_t su = vid_arg(a, "src"), du = vid_arg(a, "dst");
    PCounts o;
    ChunkWrites w;
    std::map<std::string, Chunk> cache;
    auto adj_rmw = [&](bool out, uint64_t owner, const std::function<void(Chunk&)>& mut) {
      chunk_rmw_truth(o, w, cache, padj_prefix(e.eid, out), m.cfg_.adj_of(e.etype, out).owners.pack, owner, e.kind, e.acols, e.ords, mut, m.far_bits(e.etype, out));
    };
    if (layout_kind(e.etype, true) == 2 && !e.inline_props) {   // Chunked, own placement
      covered("delete_edge.own_placement", 1);
      std::optional<uint64_t> removed;
      adj_rmw(true, su, [&](Chunk& c) { removed = chunk_adj_remove(c.groups, su, du); });
      if (e.rev) adj_rmw(false, du, [&](Chunk& c) { chunk_adj_remove(c.groups, du, su); });
      if (e.has_ep && removed)
        chunk_rmw_truth(o, w, cache, pep_prefix(e.eid, uint8_t(tag_of_uid(su))), m.cfg_.adj_of(e.etype, true).owners.pack, *removed, 2, e.ec, false,
                        [&](Chunk& c) { chunk_drop_row(c.rows, *removed); }, 0);
      apply(o, w);
      return o;
    }
    auto copy = [&](bool out) {
      uint64_t owner = out ? su : du, other = out ? du : su;
      int k = layout_kind(e.etype, out);
      if (k == 2) {
        adj_rmw(out, owner, [&](Chunk& c) { chunk_adj_remove_inline(c.groups, owner, other); });
      } else if (k == 1) {
        std::string key = adj_prefix(e.eid, out, owner);
        MergedKv at = rmw_locate_truth(o, key, other);
        if (!at) return;
        unsigned lb = m.far_bits(e.etype, out);
        lists::Entries entries = lists::View(e.ec, at->second, lb).entries();
        auto it = std::find_if(entries.begin(), entries.end(), [&](auto& x) { return x.id == other; });
        if (it == entries.end()) return;
        entries.erase(it);
        bool first_part = at->first.size() != key.size() && unit::part_start(at->first, key.size()) == 0;
        if (entries.empty() && !first_part) {
          covered("condensed_write.list_emptied", 1);
          w.dels.push_back(at->first);
        } else {
          w.puts.emplace_back(at->first, lists::encode(entries, lb, *e.ec));
        }
        covered("condensed_write.list_rmw", 1);
      } else {
        w.dels.push_back(edge_key(e.eid, out, owner, other));
        covered("split_write.edge_record", 1);
      }
    };
    copy(true);
    if (e.rev && !planted("edge_write_no_reverse")) copy(false);
    apply(o, w);
    return o;
  }

  // update_edge_prop (writes.cc update_edge): the edge's props in every stored copy. Both directions in one layout:
  // Split GETs the forward record and PUTs every copy whole; Condensed sets the entry in each list (unit::list_set);
  // Chunked inline finds the edge in src's out chunk then rewrites both chunks; own placement reads the out chunk for
  // the ordinal, then rewrites the edge-prop row. Directions in two layouts: the out side by its own read, the in copy
  // by its layout (a Split in copy PUT whole from the out side's merged row, unread). An absent edge writes nothing
  PCounts update_edge_truth(const json& s) {
    const json& a = s.at("args");
    EdgeW e = edge_w(a.at("etype").get<std::string>());
    uint64_t su = vid_arg(a, "src"), du = vid_arg(a, "dst");
    std::string src_label = a.value("src_label", "");
    Value props = from_json(a.at("props"));
    PCounts o;
    ChunkWrites w;
    std::map<std::string, Chunk> cache;
    auto far_of = [&](bool out, const std::string& label) -> std::optional<uint8_t> {
      if (!m.far_bits(e.etype, out)) return std::nullopt;
      return m.far_code(e.etype, out, label);
    };
    auto edge_value = [&](const Value& row) {
      std::vector<PropRow> rows = {{0, &row}};
      ValueRows rs(*e.ec, rows);
      std::string v;
      if (e.has_ep) columns::encode(v, *e.ec, rs);
      return v;
    };
    auto adj_rmw = [&](bool out, uint64_t owner, const std::function<void(Chunk&)>& mut) {
      chunk_rmw_truth(o, w, cache, padj_prefix(e.eid, out), m.cfg_.adj_of(e.etype, out).owners.pack, owner, e.kind, e.acols, e.ords, mut, m.far_bits(e.etype, out));
    };
    // the chunk covering `owner` as ChunkWriteReader::locate gives it (a counted SEEK unless cached), no rewrite
    auto locate = [&](bool out, uint64_t owner) -> const Chunk* {
      std::string prefix = padj_prefix(e.eid, out);
      for (auto& [k, c] : cache)
        if (chunk_has_prefix(k, prefix) && c.lo <= owner && owner <= c.hi) return &c;
      MergedKv l = seek_for_prev_merged(chunk_with_id(prefix, owner));
      if (!l || !chunk_has_prefix(l->first, prefix)) { land_miss(o, l); return nullptr; }
      auto it = cache.find(l->first);
      land_hit(o, l, it == cache.end());
      if (it == cache.end())
        return &decode_charged(o, cache, prefix, m.cfg_.adj_of(e.etype, out).owners.pack, l->first, l->second, e.kind,
                               e.acols, e.ords, m.far_bits(e.etype, out));
      return &it->second;
    };
    // unit::list_set: the entry's props merged in; false when the list or the entry is absent
    auto list_set = [&](bool out, uint64_t owner, uint64_t other, Value* merged) {
      std::string key = adj_prefix(e.eid, out, owner);
      MergedKv at = rmw_locate_truth(o, key, other);
      if (!at) return false;
      unsigned lb = m.far_bits(e.etype, out);
      lists::Entries entries = lists::View(e.ec, at->second, lb).entries();
      auto it = std::find_if(entries.begin(), entries.end(), [&](auto& x) { return x.id == other; });
      if (it == entries.end()) return false;
      it->props = row_merge(it->props.is_row() ? it->props : Value::row({}), props);
      if (merged) *merged = it->props;
      w.puts.emplace_back(at->first, lists::encode(entries, lb, *e.ec));
      covered("condensed_write.list_rmw", 1);
      return true;
    };
    int ko = layout_kind(e.etype, true), ki = layout_kind(e.etype, false);
    auto finish = [&]() { apply(o, w); return o; };
    if (e.rev && ko != ki) {
      covered(std::string("update_edge.mixed_out_") + kLayoutName[ko], 1);
      covered(std::string("update_edge.mixed_in_") + kLayoutName[ki], 1);
      Value merged = Value::row({});
      if (ko == 2) {
        const Chunk* c = locate(true, su);
        bool there = false;
        if (c)
          for (auto& g : c->groups)
            if (g.src == su) {
              auto it = std::find(g.dsts.begin(), g.dsts.end(), du);
              there = it != g.dsts.end();
              size_t i = size_t(it - g.dsts.begin());
              if (there && i < g.props.size() && g.props[i].is_row()) merged = g.props[i];
            }
        if (!there) return finish();
        merged = row_merge(merged, props);
        adj_rmw(true, su, [&](Chunk& ch) { chunk_adj_set_inline(ch.groups, su, du, props); });
      } else if (ko == 1) {
        if (!list_set(true, su, du, &merged)) return finish();
      } else {
        std::string fk = edge_key(e.eid, true, su, du);
        std::optional<std::string> blob = get_truth(o, fk);
        if (!blob) return finish();
        o.used_key += double(fk.size());
        std::string head = m.far_bits(e.etype, true) ? blob->substr(0, 1) : std::string();
        std::string cells = blob->substr(head.size());
        merged = row_merge(cells.empty() ? Value::row({}) : columns::View(e.ec, cells, 0, 1).row(0), props);
        w.puts.emplace_back(fk, head + edge_value(merged));
      }
      if (planted("edge_write_no_reverse")) return finish();
      if (ki == 2) adj_rmw(false, du, [&](Chunk& ch) { chunk_adj_set_inline(ch.groups, du, su, props); });
      else if (ki == 1) list_set(false, du, su, nullptr);
      else {
        std::optional<uint8_t> far = far_of(false, src_label);
        w.puts.emplace_back(edge_key(e.eid, false, du, su), (far ? std::string(1, char(*far)) : std::string()) + edge_value(merged));
      }
      return finish();
    }
    if (ko == 2 && e.inline_props) {
      const Chunk* c = locate(true, su);
      bool there = false;
      if (c)
        for (auto& g : c->groups)
          if (g.src == su) there = std::find(g.dsts.begin(), g.dsts.end(), du) != g.dsts.end();
      if (!there) return finish();
      adj_rmw(true, su, [&](Chunk& ch) { chunk_adj_set_inline(ch.groups, su, du, props); });
      if (e.rev && !planted("edge_write_no_reverse"))
        adj_rmw(false, du, [&](Chunk& ch) { chunk_adj_set_inline(ch.groups, du, su, props); });
    } else if (ko == 2) {
      covered("update_edge.own_placement", 1);
      const Chunk* c = locate(true, su);
      std::optional<uint64_t> ordinal = c ? chunk_adj_ordinal(c->groups, su, du) : std::nullopt;
      if (ordinal)
        chunk_rmw_truth(o, w, cache, pep_prefix(e.eid, uint8_t(tag_of_uid(su))), m.cfg_.adj_of(e.etype, true).owners.pack, *ordinal, 2, e.ec, false,
                        [&](Chunk& ch) { chunk_merge_row(ch.rows, *ordinal, props); }, 0);
    } else if (ko == 1) {
      list_set(true, su, du, nullptr);
      if (e.rev && !planted("edge_write_no_reverse")) list_set(false, du, su, nullptr);
    } else {
      std::string fk = edge_key(e.eid, true, su, du);
      std::optional<std::string> blob = get_truth(o, fk);
      covered("split_write.edge_get", 1);
      if (!blob) return finish();
      o.used_key += double(fk.size());
      std::string head = m.far_bits(e.etype, true) ? blob->substr(0, 1) : std::string();
      std::string cells = blob->substr(head.size());
      std::string val = edge_value(row_merge(cells.empty() ? Value::row({}) : columns::View(e.ec, cells, 0, 1).row(0), props));
      w.puts.emplace_back(fk, head + val);
      if (e.rev && !planted("edge_write_no_reverse")) {
        std::optional<uint8_t> far = far_of(false, src_label);
        w.puts.emplace_back(edge_key(e.eid, false, du, su), (far ? std::string(1, char(*far)) : std::string()) + val);
      }
    }
    return finish();
  }

  // ---- vertex writes ----
  // the chunk covering `id` in `prefix`, as ChunkWriteReader::locate gives it: free inside a cached chunk's range, else
  // 1 SEEK back (the chunk read whole unless already cached); ("", null) when the family holds none at or below it
  std::pair<std::string, const Chunk*> locate_chunk(PCounts& o, std::map<std::string, Chunk>& cache,
                                                    const std::string& prefix, uint64_t target, uint64_t id, int kind,
                                                    const std::vector<ColDecl>* cols, bool ords, unsigned lb) {
    for (auto& [k, c] : cache)
      if (chunk_has_prefix(k, prefix) && c.lo <= id && id <= c.hi) return {k, &c};
    MergedKv l = seek_for_prev_merged(chunk_with_id(prefix, id));
    if (!l || !chunk_has_prefix(l->first, prefix)) { land_miss(o, l); return {"", nullptr}; }
    auto it = cache.find(l->first);
    land_hit(o, l, it == cache.end());
    if (it == cache.end()) return {l->first, &decode_charged(o, cache, prefix, target, l->first, l->second, kind, cols, ords, lb)};
    return {it->first, &it->second};
  }
  // unit::rmw_read (store/unit_value.cc): a unit's every part with its key; 1 SEEK, 1 NEXT per further part and the
  // landing past the last (a miss); a whole unit is its one landing; absent: a miss
  std::optional<std::vector<std::pair<std::string, std::string>>> rmw_read_truth(PCounts& o, const std::string& key) {
    MergedKv l = seek_merged(key);
    if (!l) { land_miss(o, l); return std::nullopt; }
    std::vector<std::pair<std::string, std::string>> out;
    if (l->first == key) {
      land_hit(o, l, true);
      o.used_key += double(key.size());
      out.push_back(*l);
      return out;
    }
    if (!unit::owns(l->first, key)) { land_miss(o, l); return std::nullopt; }
    land_hit(o, l, true);
    o.used_key += double(key.size());
    out.push_back(*l);
    while (true) {   // 1 NEXT per further part, then the landing past the last
      MergedKv nx = seek_merged(l->first + std::string(1, '\0'));
      o.N++;
      if (!nx || !unit::owns(nx->first, key)) {
        o.N_miss++;
        o.next_miss_key += nx ? double(nx->first.size()) : 0;
        break;
      }
      covered("delete_vertex.list_further_part", 1);
      o.scan_key += double(nx->first.size());
      o.scan_value += double(nx->second.size());
      out.push_back(*nx);
      l = nx;
    }
    return out;
  }
  // a Split owner's records of (etype, dir): the read path's prefix scan (Meta::neighbors_of_into record_dir): 1 SEEK,
  // each record's value read, 1 NEXT per further record, the landing past the last a miss
  std::vector<uint64_t> split_records_truth(PCounts& o, const std::string& prefix) {
    covered("delete_vertex.split_records", 1);
    std::vector<uint64_t> out;
    MergedKv l = seek_merged(prefix);
    bool from_seek = true;
    while (l && l->first.compare(0, prefix.size(), prefix) == 0) {
      if (from_seek) o.S++; else o.N++;
      o.scan_key += double(l->first.size());
      o.scan_value += double(l->second.size());
      out.push_back(edge_dst(l->first));
      l = seek_merged(l->first + std::string(1, '\0'));
      from_seek = false;
    }
    if (from_seek) { o.S++; o.S_miss++; o.seek_miss_key += l ? double(l->first.size()) : 0; }
    else { o.N++; o.N_miss++; o.next_miss_key += l ? double(l->first.size()) : 0; }
    return out;
  }
  // a list's entries removed as unit::list_remove does (store/unit_list.cc): rewritten, or DELed when emptied unless it
  // is a split unit's first part
  void list_remove_truth(PCounts& o, ChunkWrites& w, const EdgeW& e, bool out, uint64_t owner, uint64_t other) {
    std::string key = adj_prefix(e.eid, out, owner);
    MergedKv at = rmw_locate_truth(o, key, other);
    if (!at) return;
    unsigned lb = m.far_bits(e.etype, out);
    lists::Entries entries = lists::View(e.ec, at->second, lb).entries();
    auto it = std::find_if(entries.begin(), entries.end(), [&](auto& x) { return x.id == other; });
    if (it == entries.end()) return;
    entries.erase(it);
    bool first_part = at->first.size() != key.size() && unit::part_start(at->first, key.size()) == 0;
    if (entries.empty() && !first_part) {
      covered("condensed_write.list_emptied", 1);
      w.dels.push_back(at->first);
    } else {
      w.puts.emplace_back(at->first, lists::encode(entries, lb, *e.ec));
    }
    covered("condensed_write.list_rmw", 1);
  }

  // every edge of a deleted vertex (writes.cc delete_incident): per incident (etype, side), our side read through its
  // layout and emptied, then each neighbour's entry of us removed by the other side's layout (one RMW per touched chunk)
  void delete_incident_truth(PCounts& o, ChunkWrites& w, std::map<std::string, Chunk>& cache, const std::string& label,
                             uint64_t u) {
    for (auto& [etype, out] : m.schema_.incident_etypes(label)) {
      if (!out && !m.reverse_available(etype)) continue;
      EdgeW e = edge_w(etype);
      bool other_side = e.rev;
      int kout = layout_kind(etype, out), kin = layout_kind(etype, !out);
      covered("delete_vertex.incident_side", 1);
      auto adj_rmw = [&](bool d, uint64_t owner, const std::function<void(Chunk&)>& mut) {
        chunk_rmw_truth(o, w, cache, padj_prefix(e.eid, d), m.cfg_.adj_of(etype, d).owners.pack, owner, e.kind, e.acols, e.ords, mut, m.far_bits(etype, d));
      };
      auto packed_nbrs = [&](bool d) {   // our chunk's group of u (the chunk the rewrite reads next: cached)
        std::vector<uint64_t> dsts;
        auto [key, c] = locate_chunk(o, cache, padj_prefix(e.eid, d), m.cfg_.adj_of(etype, d).owners.pack, u, e.kind, e.acols, e.ords, m.far_bits(etype, d));
        if (c)
          for (auto& g : c->groups)
            if (g.src == u) { dsts = g.dsts; break; }
        return dsts;
      };
      auto by_chunk = [&](bool d, const std::vector<uint64_t>& nbrs) {   // the neighbours grouped by their chunk
        std::vector<std::pair<std::string, std::vector<uint64_t>>> out_groups;
        for (uint64_t n : nbrs) {
          auto [key, c] = locate_chunk(o, cache, padj_prefix(e.eid, d), m.cfg_.adj_of(etype, d).owners.pack, n, e.kind, e.acols, e.ords, m.far_bits(etype, d));
          std::string gk = key.empty() ? "\x02" + hex16(n) : key;
          auto it = std::find_if(out_groups.begin(), out_groups.end(), [&](auto& x) { return x.first == gk; });
          if (it == out_groups.end()) out_groups.push_back({gk, {n}});
          else it->second.push_back(n);
        }
        return out_groups;
      };
      auto own_list = [&](bool d, std::vector<uint64_t>* nbrs) {   // our Condensed list read once, every part DELed
        auto own = rmw_read_truth(o, adj_prefix(e.eid, d, u));
        if (!own) return false;
        for (auto& [pk, raw] : *own) {
          lists::View lv(e.ec, raw, m.far_bits(etype, d));
          nbrs->insert(nbrs->end(), lv.dsts().begin(), lv.dsts().end());
        }
        for (auto& [pk, raw] : *own) w.dels.push_back(pk);
        return true;
      };
      auto condensed_other = [&](const std::vector<uint64_t>& nbrs) {
        for (uint64_t nu : nbrs) {
          if (nu == u) {   // a self-loop: our own other side, DELed whole
            covered("delete_vertex.self_loop", 1);
            if (auto other = rmw_read_truth(o, adj_prefix(e.eid, !out, nu)))
              for (auto& [pk, raw] : *other) w.dels.push_back(pk);
          } else if (!planted("edge_write_no_reverse")) {
            list_remove_truth(o, w, e, !out, nu, u);
          }
        }
      };
      if (other_side && kout != kin) {   // our side in one layout, the neighbours' entries of us in another
        covered(std::string("delete_vertex.mixed_ours_") + kLayoutName[kout], 1);
        covered(std::string("delete_vertex.mixed_theirs_") + kLayoutName[kin], 1);
        std::vector<uint64_t> nbrs;
        if (kout == 2) {
          nbrs = packed_nbrs(out);
          if (nbrs.empty()) continue;
          adj_rmw(out, u, [&](Chunk& c) { for (uint64_t n : nbrs) chunk_adj_remove_inline(c.groups, u, n); });
        } else if (kout == 1) {
          if (!own_list(out, &nbrs) || nbrs.empty()) continue;
        } else {
          nbrs = split_records_truth(o, adj_prefix(e.eid, out, u));
          for (uint64_t n : nbrs) w.dels.push_back(edge_key(e.eid, out, u, n));
        }
        if (planted("edge_write_no_reverse")) continue;
        if (kin == 2) {
          for (auto& [gk, nus] : by_chunk(!out, nbrs))
            adj_rmw(!out, nus[0], [&](Chunk& c) { for (uint64_t nu : nus) chunk_adj_remove_inline(c.groups, nu, u); });
        } else if (kin == 1) {
          condensed_other(nbrs);
        } else {
          for (uint64_t nu : nbrs) w.dels.push_back(edge_key(e.eid, !out, nu, u));
        }
        continue;
      }
      if (kout == 2) {
        std::vector<uint64_t> nbrs = packed_nbrs(out);
        if (nbrs.empty()) continue;
        bool own_ep = !e.inline_props && e.has_ep;
        std::vector<std::optional<uint64_t>> ords;
        adj_rmw(out, u, [&](Chunk& c) {
          for (uint64_t n : nbrs) {
            if (e.inline_props) chunk_adj_remove_inline(c.groups, u, n);
            else ords.push_back(chunk_adj_remove(c.groups, u, n));
          }
        });
        if (other_side && !planted("edge_write_no_reverse"))
          for (auto& [gk, nus] : by_chunk(!out, nbrs))
            adj_rmw(!out, nus[0], [&](Chunk& c) {
              for (uint64_t nu : nus) {
                if (e.inline_props) chunk_adj_remove_inline(c.groups, nu, u);
                else chunk_adj_remove(c.groups, nu, u);
              }
            });
        if (own_ep) {   // the ep rows live under the OUT side's group: ours for out, the neighbour's for in
          std::vector<std::pair<std::string, std::pair<uint64_t, std::vector<uint64_t>>>> by_ep;
          for (size_t i = 0; i < nbrs.size() && i < ords.size(); i++) {
            if (!ords[i]) continue;
            uint64_t ot = tag_of_uid(out ? u : nbrs[i]);
            auto [key, c] = locate_chunk(o, cache, pep_prefix(e.eid, uint8_t(ot)), m.cfg_.adj_of(etype, true).owners.pack, *ords[i], 2, e.ec, false, 0);
            std::string gk = std::to_string(ot) + "\x01" + key;
            auto it = std::find_if(by_ep.begin(), by_ep.end(), [&](auto& x) { return x.first == gk; });
            if (it == by_ep.end()) by_ep.push_back({gk, {ot, {*ords[i]}}});
            else it->second.second.push_back(*ords[i]);
          }
          for (auto& [gk, ep] : by_ep)
            chunk_rmw_truth(o, w, cache, pep_prefix(e.eid, uint8_t(ep.first)), m.cfg_.adj_of(etype, true).owners.pack, ep.second[0], 2, e.ec, false,
                            [&](Chunk& c) { for (uint64_t x : ep.second) chunk_drop_row(c.rows, x); }, 0);
        }
      } else if (kout == 1) {
        std::vector<uint64_t> nbrs;
        if (!own_list(out, &nbrs) || nbrs.empty() || !other_side) continue;
        condensed_other(nbrs);
      } else {
        std::vector<uint64_t> nbrs = split_records_truth(o, adj_prefix(e.eid, out, u));
        for (uint64_t nu : nbrs) {
          w.dels.push_back(edge_key(e.eid, out, u, nu));
          if (other_side && !planted("edge_write_no_reverse")) w.dels.push_back(edge_key(e.eid, !out, nu, u));
        }
      }
    }
  }

  // a grouped (Condensed) props read for a rewrite (writes.cc grouped_read_old): 1 GET per row group holding a column
  // in `need`; the label's index columns in the found rows are used
  Value grouped_read_old_truth(PCounts& o, const std::string& label, uint64_t u, const std::vector<std::string>& need) {
    const Meta::RowGroups& rg = m.row_groups(label);
    RowFields old;
    for (size_t g : m.groups_for(label, need)) {
      std::string vk = vertex_group_key(m.vtype_of(label), u, g);
      std::optional<std::string> blob = get_truth(o, vk);
      if (!blob) continue;
      columns::View orv(&rg.cols[g], *blob, 0, 1);
      uint64_t ur = 0;
      for (auto& p : m.schema_.index_cols_of(label)) { int c = orv.col_index(p); if (c >= 0) ur += orv.cell_bytes(size_t(c), 0); }
      o.used_key += double(vk.size());
      o.used_value += double(ur);
      Value r = orv.row(0);
      for (auto& kv : r.as_row()) old.push_back(kv);
    }
    return Value::row(std::move(old));
  }
  // one vertex's props written on an unpacked layout (writes.cc put_vertex): Condensed, one PUT per written row group
  // (an insert: group 0 and every group with a present cell; an update: the groups holding a changed column); Split,
  // one PUT per present (insert) or changed (update) column
  void put_vertex_truth(ChunkWrites& w, const std::string& label, uint64_t u, const Value& row,
                        const std::vector<std::string>& changed) {
    auto is_changed = [&](const std::string& n) { return std::find(changed.begin(), changed.end(), n) != changed.end(); };
    uint8_t vt = m.vtype_of(label);
    const std::vector<ColDecl>& cols = m.vcols(label);
    if (m.cfg_.props_of(label).grouped()) {
      const Meta::RowGroups& rg = m.row_groups(label);
      std::vector<PropRow> rows = {{u, &row}};
      for (size_t g = 0; g < rg.cols.size(); g++) {
        const std::vector<ColDecl>& gc = rg.cols[g];
        bool write = changed.empty() && g == 0;
        for (auto& c : gc) write = write || (changed.empty() ? !row.get(c.name).is_null() : is_changed(c.name));
        if (!write) continue;
        ValueRows src(gc, rows);
        std::string enc;
        columns::encode(enc, gc, src);
        w.puts.emplace_back(vertex_group_key(vt, u, g), std::move(enc));
        covered("props_write.condensed_row", 1);
      }
      return;
    }
    for (size_t c = 0; c < cols.size(); c++) {
      if (!changed.empty() && !is_changed(cols[c].name)) continue;
      Value cell = row.get(cols[c].name);
      if (cell.is_null()) continue;
      w.puts.emplace_back(vertex_attr_key(vt, u, attr_of_col(c)), columns::encode_cell(cols[c], cell));
      covered("props_write.split_cell", 1);
    }
  }

  // one vertex's sorted-index entries, decided here from the schema (the count is the card's, not SortedIndex's): a
  // label with no indexed property keeps one type-index entry (its vid); else one entry per declared index column,
  // a null value included (its null slot). The key bytes come from SortedIndex::write_key (the key codec)
  std::vector<std::string> index_entries(const std::string& label, uint64_t u, const Value& props) {
    const SortedIndex& ix = sorted_index();
    std::vector<std::string> keys;
    if (!m.schema_.prop_indexed(label)) {
      covered("index_write.type_entry", 1);
      keys.push_back(IndexKeys::key(IndexKeys::prefix(m.schema_.type_index_decl(label).id), "", u));
    }
    std::vector<std::string> cols = m.schema_.index_cols_of(label);
    if (planted("index_write_one_short") && !cols.empty()) cols.pop_back();
    for (auto& prop : cols) keys.push_back(ix.write_key(label, prop, props.get(prop), u));
    return keys;
  }

  // insert_vertex: the props by the label's layout (Chunked: the chunk RMW of the label's family), then the index
  // entries: one empty PUT per index key of the new vertex (SortedIndex::w_insert)
  PCounts insert_vertex_truth(const json& s) {
    const json& a = s.at("args");
    std::string label = a.at("label").get<std::string>();
    uint64_t u = vid_arg(a, "vid");
    Value props = from_json(a.at("props"));
    PCounts o;
    ChunkWrites w;
    std::map<std::string, Chunk> cache;
    if (m.cfg_.props_of(label).packed())
      chunk_rmw_truth(o, w, cache, vertex_family(m.vtype_of(label)), m.cfg_.props_of(label).owners.pack, u, 0, &m.vcols(label), false,
                      [&](Chunk& c) { chunk_upsert(c.rows, u, props); }, 0);
    else
      put_vertex_truth(w, label, u, props, {});
    if (!planted("index_write_none"))
      for (auto& k : index_entries(label, u, props)) {
        w.puts.emplace_back(k, "");
        covered("index_write.insert_entry", 1);
      }
    apply(o, w);
    return o;
  }

  // update_prop: Chunked rewrites the vertex's chunk; Condensed reads and rewrites the row groups holding a changed
  // column; Split GETs the old value of a changed INDEXED column only, then PUTs each changed column. Index: per changed
  // indexed column whose key moves, DEL of the old entry + PUT of the new one (SortedIndex::w_update)
  PCounts update_prop_truth(const json& s) {
    const json& a = s.at("args");
    std::string label = a.at("label").get<std::string>();
    uint64_t u = vid_arg(a, "vid");
    std::vector<std::pair<std::string, Value>> cells;
    if (a.contains("props") && !a["props"].is_null()) {
      Value p = from_json(a["props"]);
      for (auto& [name, v] : p.as_row()) cells.push_back({name, v});
    } else {
      cells.push_back({a.at("prop").get<std::string>(), from_json(a.at("value"))});
    }
    std::vector<std::string> changed;
    for (auto& c : cells) changed.push_back(c.first);
    auto with_cells = [&](const Value& row) {
      Value r = row;
      for (auto& [prop, v] : cells) r = row_with(r, prop, v);
      return r;
    };
    PCounts o;
    ChunkWrites w;
    std::map<std::string, Chunk> cache;
    Value old_row = Value::row({});
    const Component& pl = m.cfg_.props_of(label);
    if (pl.packed()) {
      chunk_rmw_truth(o, w, cache, vertex_family(m.vtype_of(label)), m.cfg_.props_of(label).owners.pack, u, 0, &m.vcols(label), false, [&](Chunk& c) {
        for (auto& row : c.rows)
          if (row.first == u) { old_row = row.second; row.second = with_cells(old_row); return; }
        c.rows.emplace_back(u, with_cells(Value::row({})));
      }, 0);
    } else if (pl.grouped()) {
      old_row = grouped_read_old_truth(o, label, u, changed);
      put_vertex_truth(w, label, u, with_cells(old_row), changed);
    } else {
      RowFields olds;
      std::vector<std::string> icols = m.schema_.index_cols_of(label);
      for (auto& [prop, value] : cells) {
        int c = m.attr_id(label, prop);
        if (c < 0) throw std::runtime_error("predict: update_prop: '" + prop + "' is not a column of " + label);
        if (std::find(icols.begin(), icols.end(), prop) == icols.end()) continue;
        covered("props_write.split_old_indexed", 1);
        std::string ok = vertex_attr_key(m.vtype_of(label), u, uint8_t(c));
        std::optional<std::string> old = get_truth(o, ok);
        if (old) {
          o.used_key += double(ok.size());
          o.used_value += double(old->size());
          row_set(olds, prop, columns::decode_cell(m.vcols(label)[size_t(c) - 1], *old));
        }
      }
      old_row = make_row(std::move(olds));
      put_vertex_truth(w, label, u, with_cells(Value::row({})), changed);
    }
    Value new_row = with_cells(old_row);
    const SortedIndex& ix = sorted_index();
    std::vector<std::string> icols = m.schema_.index_cols_of(label);
    for (auto& prop : changed) {
      if (std::find(icols.begin(), icols.end(), prop) == icols.end()) continue;
      std::string ok = ix.write_key(label, prop, old_row.get(prop), u), nk = ix.write_key(label, prop, new_row.get(prop), u);
      if (ok == nk) continue;
      covered("index_write.update_moves", 1);
      if (!planted("index_write_no_old_del")) w.dels.push_back(ok);
      w.puts.emplace_back(nk, "");
    }
    apply(o, w);
    return o;
  }

  // delete_vertex: the props by the label's layout (Chunked: the row out of its chunk; Condensed: the rows naming the
  // index columns read, every row group DELed; Split: the indexed columns read, every declared column DELed), the index
  // entries DELed (SortedIndex::w_delete), then (with_edges, the default) every incident edge
  PCounts delete_vertex_truth(const json& s) {
    const json& a = s.at("args");
    std::string label = a.at("label").get<std::string>();
    uint64_t u = vid_arg(a, "vid");
    bool with_edges = !a.contains("with_edges") || a["with_edges"].is_null() || a["with_edges"].get<bool>();
    uint8_t vt = m.vtype_of(label);
    const std::vector<ColDecl>& cols = m.vcols(label);
    PCounts o;
    ChunkWrites w;
    std::map<std::string, Chunk> cache;
    Value old_props = Value::row({});
    const Component& pl = m.cfg_.props_of(label);
    if (pl.packed()) {
      chunk_rmw_truth(o, w, cache, vertex_family(vt), m.cfg_.props_of(label).owners.pack, u, 0, &cols, false, [&](Chunk& c) {
        for (size_t k = 0; k < c.rows.size(); k++)
          if (c.rows[k].first == u) { old_props = c.rows[k].second; c.rows.erase(c.rows.begin() + long(k)); return; }
      }, 0);
    } else if (pl.grouped()) {
      old_props = grouped_read_old_truth(o, label, u, m.schema_.index_cols_of(label));
      for (size_t g = 0; g < m.row_groups(label).cols.size(); g++) w.dels.push_back(vertex_group_key(vt, u, g));
      covered("props_write.condensed_row", 1);
    } else {
      RowFields f;
      for (auto& p : m.schema_.index_cols_of(label)) {
        int c = m.attr_id(label, p);
        if (c < 0) continue;
        std::string k = vertex_attr_key(vt, u, uint8_t(c));
        std::optional<std::string> raw_v = get_truth(o, k);
        if (raw_v) {
          o.used_key += double(k.size());
          o.used_value += double(raw_v->size());
          row_set(f, p, columns::decode_cell(cols[size_t(c) - 1], *raw_v));
        }
      }
      old_props = make_row(std::move(f));
      for (size_t c = 0; c < cols.size(); c++) w.dels.push_back(vertex_attr_key(vt, u, attr_of_col(c)));
      covered("props_write.split_cell", 1);
    }
    if (!planted("index_write_none"))
      for (auto& k : index_entries(label, u, old_props)) {
        w.dels.push_back(k);
        covered("index_write.delete_entry", 1);
      }
    if (with_edges) delete_incident_truth(o, w, cache, label, u);
    apply(o, w);
    return o;
  }

  PCounts write_step(const json& s, const std::string& op) {
    if (!replay) throw std::runtime_error("predict: a write step needs the request's replay (predict --gate keeps it)");
    if (op == "insert_edge") return insert_edge_truth(s);
    if (op == "delete_edge") return delete_edge_truth(s);
    if (op == "update_edge_prop") return update_edge_truth(s);
    if (op == "insert_vertex") return insert_vertex_truth(s);
    if (op == "update_prop") return update_prop_truth(s);
    if (op == "delete_vertex") return delete_vertex_truth(s);
    throw std::runtime_error("predict: op " + op + " is not covered");
  }

  // the step's cost from its truth card; with `terms` set, its terms too (a write's sums are added as its rules run)
  PCounts step(const json& s) {
    StepCard c;
    PCounts p = step_with(s, c);
    if (terms) {
      if (!is_write_op(s.at("op").get<std::string>())) card_terms(s, c);
      terms_finish();
    }
    return p;
  }

  // the truth-card levels: what step_with's card holds, per op, as term sums (terms_finish makes the terms) and counts
  void card_terms(const json& s, const StepCard& c) {
    const std::string op = s.at("op").get<std::string>();
    const json& a = s.at("args");
    if (op == "read_neighbors") {
      std::string etype = a.at("etype").get<std::string>();
      Meta::HopShape sh = m.hop_shape(etype, kv::dir_of(a), strs(s, "label"));
      hop_terms(sh, etype, c);
      double e = 0;
      for (auto& k : c.dirs) e += k.entries;
      // a Chunked direction's walk counts no entries: then the run's own output rows give the count (cli/predict.cc)
      bool packed = false;
      for (size_t di = 0; di < sh.dirs.size(); di++) packed = packed || m.cfg_.adj_of(etype, sh.dirs[di]).packed();
      if (!packed) tset("entries@", e);
    } else if (op == "read_vertex_props" || op == "read_anchor_props" || op == "read_top_k_by_index") {
      props_card_terms(s, op, c);
    } else if (op == "read_filter_by_label_probe") {
      std::vector<std::string> labs = strs(s, "label");
      props_families({labs.at(0)}, m.cfg_.props_of(labs.at(0)));
      props_terms(m.cfg_.props_of(labs.at(0)), c);
    } else if (op == "read_chase" || op == "read_closure") {
      chase_card_terms(s, op, c);
    } else {
      index_card_terms(s, op, c);
    }
  }
  // an index walk's terms; read_edge_exists records its own, and a scan or a by-id read has none
  void index_card_terms(const json& s, const std::string& op, const StepCard& c) {
    const json& a = s.at("args");
    if (op == "read_index_eq") {
      walk_terms(index_term_part(a.at("label").get<std::string>(), a.at("prop").get<std::string>()), WalkCard(), c.walk);
      tset("matches@", c.walk.matches);
    } else if (op == "read_index_range" || op == "index_range_buckets") {
      tset("matches@", c.walk.matches);   // per label: step_with
    } else if (op == "read_filter_by_label_index" ||
               (op == "read_vertices_by_label" && (!a.contains("props") || a["props"].is_null()))) {
      walk_terms(enumeration_term_part(strs(s, "label").at(0)), WalkCard(), c.walk);
    }
  }
  void props_families(const std::vector<std::string>& labs, const Component& layout) {
    if (layout.packed()) for (auto& l : labs) tfam("props:" + l, vertex_family(m.vtype_of(l)));
  }
  void props_card_terms(const json& s, const std::string& op, const StepCard& c) {
    std::vector<std::string> labs = m.labels_or_throw(op.c_str(), strs(s, op == "read_vertex_props" ? "label" : "labels"));
    const Component& layout = m.props_layout(op.c_str(), labs);
    props_families(labs, layout);
    for (auto& l : labs) tadd("props_n@props:" + l, 0);   // a label no vertex was read under: 0, a count, not unknown
    props_terms(layout, c);
    if (op != "read_top_k_by_index") return;
    std::string P = "index:";
    for (size_t i = 0; i < labs.size(); i++) P += (i ? "+" : "") + labs[i];
    P += "." + s.at("args").at("prop").get<std::string>();
    tset("crossed@", c.direct.N);
    tset("winners@", double(s.at("out_vids").size()));
    if (c.direct.N > 0) tset(predict::term_key("index_key_mean", P), c.direct.scan_key / c.direct.N);
  }
  void chase_card_terms(const json& s, const std::string& op, const StepCard& c) {
    const json& a = s.at("args");
    std::string etype = a.at("etype").get<std::string>();
    // the dirs as the truth walk keeps its cards: a chase's m.dirs_of, a closure's hop shape
    std::vector<bool> dirs = op == "read_chase" ? m.dirs_of(op, etype, kv::dir_of(a)) : m.hop_shape(etype, kv::dir_of(a), {}).dirs;
    uint8_t eid = uint8_t(m.schema_.edge(etype).id);
    double nodes = 0, links = 0;
    for (size_t di = 0; di < dirs.size(); di++) {
      bool d = dirs[di];
      const Component& al = m.cfg_.adj_of(etype, d);
      std::string P = adj_term_part(etype, d);
      if (al.packed()) {
        tfam(etype + (d ? ":out" : ":in"), padj_prefix(eid, d));
        double probes = 0;
        for (auto& L : c.landings) if (L.prefix == padj_prefix(eid, d)) probes++;
        nodes = std::max(nodes, probes);
        continue;
      }
      if (op == "read_chase" && !al.grouped()) {   // Split: the landing on the first record
        const FirstCard& k = c.chase_first.at(di);
        nodes = std::max(nodes, k.probes);
        links += k.hits;
        tadd("present@" + P, k.hits);
        tadd("entries@" + P, k.hits);
        tadd("key_bytes@" + P, k.hit_key_bytes);
        continue;
      }
      const HopDirCard& k = op == "read_chase" ? c.chase_lists.at(di) : c.dirs.at(di);
      nodes = std::max(nodes, k.probes);
      links += k.present;
      tadd("present@" + P, k.present);
      tadd("entries@" + P, k.entries);
      tadd("value_bytes@" + P, k.value_bytes);
      if (!al.grouped()) tadd("key_bytes@" + P, k.entries * double(edge_key(eid, d, 0, 0).size()));
    }
    tland.insert(tland.end(), c.landings.begin(), c.landings.end());
    tset("nodes@", nodes);
    tset("links@", links);
  }

  PCounts step_with(const json& s, StepCard& c) {
    const std::string op = s.at("op").get<std::string>();
    if (is_write_op(op)) return write_step(s, op);
    const json& a = s.at("args");
    if (op == "read_vertex_by_id") {
      if (a.at("vid").is_string()) return PCounts();   // the vid is the key: no read
      return by_id(s, c);
    }
    if (op == "read_neighbors") {
      std::string etype = a.at("etype").get<std::string>();
      std::string dir = kv::dir_of(a);
      std::vector<std::string> owner_labels = strs(s, "label");
      std::vector<std::string> edge_props = strs(s, "edge_props");
      Meta::HopShape sh = m.hop_shape(etype, dir, owner_labels);
      hop_truth(sh, etype, vids_of(s, "srcs"), edge_props, c);
      covered("hop.two_pass", sh.two ? 1 : 0);
      covered("hop.direction_dropped_by_owner_label", double(m.dirs_of("read_neighbors", etype, dir).size() - sh.dirs.size()));
      for (size_t di = 0; di < sh.dirs.size(); di++) {
        const Component& al = m.cfg_.adj_of(etype, sh.dirs[di]);
        const HopDirCard& k = c.dirs[di];
        if (al.packed()) continue;
        std::string r = al.grouped() ? "condensed_hop." : "split_hop.";
        covered(r + "list_absent", k.probes - k.present);
        covered(r + (al.grouped() ? "list_whole" : "list_present"), k.present - k.split_lists);
        if (al.grouped()) covered(r + "list_split", k.split_lists);
        covered(r + "edge_props_asked", edge_props.empty() ? 0 : k.present);
      }
      PCounts out = hop_rule(sh, etype, c);
      out.add(chunk_rule(c));
      return out;
    }
    if (op == "read_vertex_props") {
      std::vector<std::string> labs = m.labels_or_throw("read_vertex_props", strs(s, "label"));
      std::vector<std::string> props = strs(s, "props");
      const Component& layout = m.props_layout("read_vertex_props", labs);
      Meta::PropsPlan plan = m.props_plan(layout, props, labs);
      DistinctVids d = distinct_vids("meta", vids_of(s, "vids"), false);
      for (size_t i = 0; i < d.uids.size(); i++) {
        std::vector<std::string> mine = m.narrow(d.values[i], labs);
        if (mine.empty()) continue;   // of none of the candidates: nothing read
        if (mine.size() == 1) { props_truth(layout, plan, d.uids[i], mine[0], props, c); continue; }
        // candidate labels (props_of): Split's first hit decides; Condensed probes each label until one has the row
        covered("props.candidate_labels", 1);
        if (layout.packed()) {             // Chunked (props_of_packed): each label's chunk probe until the row is found
          if (plan.row_props.empty()) continue;
          for (auto& lab : mine) {
            std::shared_ptr<PropsChunk> chunk;
            int64_t row = chunk_row(0, lab, d.uids[i], c, &chunk);
            if (row < 0) continue;
            for (auto& p : plan.row_props) {
              int col = chunk->col_index(p);
              if (col >= 0) c.chunk_used_value += chunk->cell_bytes(size_t(col), size_t(row));
            }
            break;
          }
          continue;
        }
        if (!layout.grouped()) { split_props_truth(plan, d.uids[i], props, mine, c); continue; }
        if (plan.row_props.empty()) continue;
        for (auto& lab : mine)
          if (probe_truth(lab, d.uids[i], plan.of(lab).groups, c)) break;
      }
      return all_rules(layout, c);
    }
    if (op == "read_anchor_props") return anchor_props(s, c);
    if (op == "read_filter_by_label_probe") return filter_probe(s, c);
    if (op == "read_filter_by_label_index") {
      std::vector<std::string> labs = strs(s, "label");
      if (labs.size() != 1) throw std::runtime_error("predict: read_filter_by_label_index needs one label");
      bool unknown = false;                     // the walk runs only while some vid's label is unknown
      for (auto& v : vids_of(s, "vids")) unknown = unknown || !m.tag_label(v);
      if (unknown) walk_enumerate(labs[0], c.walk);
      walk_covered(c.walk);
      return walk_rule(c.walk);
    }
    if (op == "read_index_eq") {
      walk_eq(a.at("label").get<std::string>(), a.at("prop").get<std::string>(), from_json(a.at("value")), c.walk);
      walk_covered(c.walk);
      return walk_rule(c.walk);
    }
    if (op == "read_index_range" || op == "index_range_buckets") {   // one walk per label
      int64_t lo = a.at("lo").get<int64_t>(), hi = a.at("hi").get<int64_t>();
      if (op == "index_range_buckets") hi = hi == INT64_MAX ? hi : hi + 1;   // its days are inclusive (run_kv_op)
      for (auto& lab : m.labels_or_throw(op.c_str(), strs(s, "labels"))) {
        WalkCard before = c.walk;
        walk_range(lab, a.at("prop").get<std::string>(), lo, hi, c.walk);
        walk_terms(index_term_part(lab, a.at("prop").get<std::string>()), before, c.walk);   // the truth-card levels
      }
      walk_covered(c.walk);
      return walk_rule(c.walk);
    }
    if (op == "read_vertices_by_label") {
      std::string lab = a.at("label").get<std::string>();
      if (!a.contains("props") || a["props"].is_null()) {   // the membership: one walk of the label's index
        walk_enumerate(lab, c.walk);
        walk_covered(c.walk);
        return walk_rule(c.walk);
      }
      scan_truth(s, lab, c.scan);
      covered(std::string("label_scan.") + (m.cfg_.props_of(lab).packed() ? "chunked"
                                            : m.cfg_.props_of(lab).grouped() ? "condensed" : "split"), c.scan.keys);
      return scan_rule(c.scan);
    }
    if (op == "read_chase") return chase(s, c);
    if (op == "read_closure") return closure(s, c);
    if (op == "read_edge_exists") return edge_exists(s);
    if (op == "read_top_k_by_index") {
      if (m.index_->date_granularity() != DateGranularity::full)
        throw std::runtime_error("predict: a top-k over a day index (the resolver's reads are not covered)");
      for (auto& [part, pc] : s.at("parts").items()) {   // the walk: an input (group 3), from its index parts
        if (part.rfind("index:", 0) != 0) continue;
        c.direct.S += pc.at("S").get<double>();
        c.direct.N += pc.at("N").get<double>();
        c.direct.S_miss += pc.at("S_miss").get<double>();
        c.direct.N_miss += pc.at("N_miss").get<double>();
        const json& b = pc.at("bytes");
        c.direct.scan_key += b.at("scan_key").get<double>();
        c.direct.scan_value += b.at("scan_value").get<double>();
        c.direct.seek_miss_key += b.at("seek_miss_key").get<double>();
        c.direct.next_miss_key += b.at("next_miss_key").get<double>();
      }
      std::vector<std::string> labs = strs(s, "labels");
      std::vector<std::string> props = strs(s, "props");
      const Component& layout = m.props_layout("read_top_k_by_index", labs);
      for (auto& w : s.at("out_vids")) {   // the winners, each under its own label (the index it came from)
        uint64_t u;
        if (!canonical_vid(w.get<std::string>(), &u)) throw std::runtime_error("predict: a winner is not a vid");
        std::string lab = label_of(layout, labs, u);
        Meta::PropsPlan plan = m.props_plan(layout, props, {lab});
        props_truth(layout, plan, u, lab, props, c);
      }
      covered("topk.walk_from_index_parts", 1);
      covered("topk.winners", double(s.at("out_vids").size()));
      props_covered(layout, c);
      // the walk's index keys are used as the walk books them (sorted_index.cc): the scan keys it crossed
      PCounts out = c.direct;
      out.used_key += c.direct.scan_key;
      out.add(props_rule(layout, c));
      out.add(chunk_rule(c));
      return out;
    }
    throw std::runtime_error("predict: op " + op + " is not covered");
  }

  // a winner's label, read from the store: the candidate label whose props hold the vertex (truth only)
  std::string label_of(const Component& layout, const std::vector<std::string>& labs, uint64_t u) {
    for (auto& lab : labs) {
      uint8_t vt = m.vtype_of(lab);
      if (layout.packed()) {
        Landing L = land(0, vertex_family(vt), u);
        if (!L.landed || u > L.hi) continue;
        raw.it->SeekForPrev(chunk_with_id(vertex_family(vt), u));
        if (PropsChunk::decode(&m.cc_, &m.vcols(lab), std::string(raw.value()))->find(u) >= 0) return lab;
      } else if (layout.grouped()) {
        if (raw.get(vertex_group_key(vt, u, 0))) return lab;
      } else {
        std::string k = vertex_key(vt, u);
        raw.it->Seek(k);
        if (raw.it->Valid() && has_prefix(raw.key(), k)) return lab;
      }
    }
    throw std::runtime_error("predict: a winner of none of the labels");
  }
};

// ---- the estimate card: the same cards filled from statistics, then the same rules ----
//
// Sources: stats.json (type counts, edge totals, the distinct owners per edge type and side, the anchor's own degrees
// and message count, per (label, prop) the vertices holding it and their bytes) and the tally (per part: keys, key
// bytes, value bytes; a store census stands in for the ingest tally). Chunked is a closed form, not the replay:
// n probes spread uniformly over a part's B chunks touch B (1 - (1 - 1/B)^n) of them (balls in bins).
struct Estimate {
  const Meta& m;
  const predict::DatasetStats& stats;   // every read through its accessors: a missing field is refused by name
  const predict::EstimateInput& in;
  const json& tally;
  StepPredictor rules;   // the rules only (no store)
  // every term a formula read (predict/terms.h): "<name>@<part>" -> (value, from the truth card)
  mutable std::map<std::string, std::pair<double, bool>> reads;
  // a term: the step's true value when the input carries one (the truth-card levels), else the statistic `stat()` gives
  template <class F> double term(const char* name, const std::string& part, F&& stat) const {
    predict::term_def(name);
    std::string k = predict::term_key(name, part);
    if (in.true_terms) {
      auto it = in.true_terms->find(k);
      if (it != in.true_terms->end()) { reads[k] = {it->second, true}; return it->second; }
    }
    double v = stat();
    reads[k] = {v, false};
    return v;
  }
  static std::string adj_name(const std::string& etype, bool d) { return "adj:" + etype + (d ? ":out" : ":in"); }
  // (part, type, direction, detail) -> keys, key bytes, value bytes; an index's detail is its property
  std::map<std::tuple<std::string, std::string, std::string, std::string>, std::array<double, 3>> parts;
  std::map<std::tuple<std::string, std::string, std::string, std::string>, std::array<double, 3>> owner_parts;   // (part, type, dir, owner)
  std::map<std::tuple<std::string, std::string, std::string>, double> owners_of;   // (etype, dir, label) -> owners
  std::map<std::tuple<std::string, std::string, std::string>, double> edges_of;    // (etype, dir, label) -> edges
  // the step's cost per store part, first seen first, under the names a measured capture uses (props:<Label>,
  // adj:<group>:<etype>:<dir>, index:<Label>[.<prop>]). Charged beside the step's own sum, which is computed as
  // before: the parts must add up to it (predict --plans checks). A part with every lane 0 is not kept, as a measured
  // capture has no row for a part it never touched
  mutable std::vector<std::pair<std::string, PCounts>> charged;
  void charge(const std::string& part, const PCounts& p) const {
    if (p.S == 0 && p.G == 0 && p.N == 0 && p.S_miss == 0 && p.G_miss == 0 && p.N_miss == 0 && p.get_key == 0 &&
        p.get_value == 0 && p.scan_key == 0 && p.scan_value == 0 && p.seek_miss_key == 0 && p.next_miss_key == 0 &&
        p.get_miss_key == 0 && p.used_key == 0 && p.used_value == 0 && p.P == 0 && p.D == 0 && p.write_key == 0 &&
        p.write_value == 0)
      return;
    for (auto& [n, c] : charged)
      if (n == part) { c.add(p); return; }
    charged.push_back({part, p});
  }
  static std::string index_part(const std::string& label, const std::string& prop) {
    return "index:" + label + (prop.empty() ? "" : "." + prop);
  }
  // one direction of an edge type's lists, under its owners' vid group. An end whose owner labels span several groups
  // (LDBC: hasTag out, isLocatedIn out) is split over them by the step's inputs: their label mix (the truth-card levels), else the
  // far-label share of the hop that made them, else the owner labels' counts
  void charge_adj(const std::string& etype, bool out, const PCounts& p) const {
    std::string tail = ":" + etype + (out ? ":out" : ":in");
    std::vector<std::string> owners = owner_labels(etype, out);
    std::map<std::string, double> w;   // group -> weight
    for (auto& l : owners) w[m.schema_.group_of(l)] = 0;
    if (w.size() == 1) { charge("adj:" + w.begin()->first + tail, p); return; }
    auto owns = [&](const std::string& l) { return std::find(owners.begin(), owners.end(), l) != owners.end(); };
    if (in.label_mix) {
      for (auto& [k, c] : *in.label_mix)
        if (k != "?" && owns(label_of_id(k))) w[m.schema_.group_of(label_of_id(k))] += c;
    } else if (in.label_share) {
      for (auto& [l, sh] : *in.label_share)
        if (owns(l)) w[m.schema_.group_of(l)] += sh;
    }
    double all = 0;
    for (auto& [g, x] : w) all += x;
    if (all <= 0) {
      for (auto& l : owners) w[m.schema_.group_of(l)] += type_count(l);
      for (auto& [g, x] : w) all += x;
    }
    if (all <= 0) throw std::runtime_error("predict: no owner of " + etype + tail + " to charge its lists to");
    for (auto& [g, x] : w)
      if (x > 0) charge("adj:" + g + tail, times(p, x / all));
  }

  // a write's lanes per store part: the write estimators sum one PCounts per step; what `f` adds to `o` is charged to
  // `part` (or, for a list, to its owner's group: the owner's label when the step names it, else charge_adj's split),
  // so the parts add up to the step as a read's do
  template <class F> void charged_to(PCounts& o, const std::string& part, F&& f) const {
    PCounts before = o;
    f();
    charge(part, plus(o, times(before, -1)));
  }
  template <class F> void charged_to_adj(PCounts& o, const std::string& etype, bool out, const std::string& owner_label,
                                         F&& f) const {
    PCounts before = o;
    f();
    PCounts d = plus(o, times(before, -1));
    if (owner_label.empty()) charge_adj(etype, out, d);
    else charge("adj:" + m.schema_.group_of(owner_label) + ":" + etype + (out ? ":out" : ":in"), d);
  }
  static PCounts plus(PCounts a, const PCounts& b) { a.add(b); return a; }

  Estimate(const Meta& meta, const predict::DatasetStats& st, const json& tally_, const predict::EstimateInput& inputs)
      : m(meta), stats(st), in(inputs), tally(tally_), rules(meta, nullptr, "") {
    for (auto& g : tally_.at("groups")) {   // summed over the owner groups; each owner group also kept
      std::array<double, 3> v = {g.at("keys").get<double>(), g.at("key_bytes").get<double>(), g.at("value_bytes").get<double>()};
      auto& sum = parts[{g.at("part").get<std::string>(), g.at("type").get<std::string>(),
                         g.at("direction").get<std::string>(), g.at("detail").get<std::string>()}];
      for (int i = 0; i < 3; i++) sum[i] += v[i];
      if (g.at("detail").get<std::string>().empty())
        owner_parts[{g.at("part").get<std::string>(), g.at("type").get<std::string>(), g.at("direction").get<std::string>(),
                     g.at("owner").get<std::string>()}] = v;
    }
    for (auto& o : st.owners("the estimate card's owner lists")) {
      std::tuple<std::string, std::string, std::string> k = {o.at("etype").get<std::string>(), o.at("direction").get<std::string>(),
                                                             o.at("owner_label").get<std::string>()};
      owners_of[k] = o.at("owners").get<double>();
      edges_of[k] = o.at("edges").get<double>();
    }
  }
  // an edge type's adjacency part in one direction, and its edges: the owner group of the step's inputs when the label
  // mix says they are all of one label that has its own group, else the sum over the groups (and every edge)
  std::array<double, 3> adj_part(const std::string& etype, bool out, double* edges) const {
    std::string dir = out ? "out" : "in";
    std::set<std::string> labs;
    bool untagged = false;
    if (in.label_mix) {
      for (auto& [k, c] : *in.label_mix) {
        if (k == "?") untagged = true;
        else labs.insert(label_of_id(k));
      }
    } else if (in.label_share) {   // level predicted: the far labels of the hop that made the inputs
      for (auto& [l, sh] : *in.label_share)
        if (sh > 0) labs.insert(l);
    }
    {
      if (!untagged && labs.size() == 1) {
        auto it = owner_parts.find({"adjacency", etype, dir, *labs.begin()});
        auto e = edges_of.find({etype, dir, *labs.begin()});
        if (it != owner_parts.end() && e != edges_of.end()) { *edges = e->second; return it->second; }
      }
    }
    *edges = stats.edge_total(etype, "an adjacency part's edges");
    return part("adjacency", etype, dir);
  }
  const std::array<double, 3>& part(const std::string& p, const std::string& t, const std::string& d,
                                    const std::string& detail = "") const {
    auto it = parts.find({p, t, d, detail});
    if (it == parts.end()) throw std::runtime_error("predict: no tally for " + p + " " + t + " " + d + " " + detail);
    return it->second;
  }
  static double mean(const std::array<double, 3>& t, int i) { return t[0] > 0 ? t[i] / t[0] : 0; }
  // an index walk of `matches` entries over (label, prop)'s index (prop "" = the label's type index): 1 SEEK, a NEXT per
  // match, the end landing a miss (an empty walk: the SEEK's)
  void walk(const std::string& label, const std::string& prop, double matches, WalkCard& w) const {
    const std::array<double, 3>& t = part("index", label, "", prop);
    double key = term("index_key_mean", index_part(label, prop), [&] { return mean(t, 1); });
    w.walks += 1;
    w.matches += matches;
    w.match_key_bytes += matches * key;
    if (matches > 0) w.end_key_bytes += key;
    else { w.empty += 1; w.empty_key_bytes += key; }
  }
  // the index a label's membership walk reads (SortedIndex::enumerate_uids)
  std::string enumeration_prop(const std::string& label) const {
    const Schema& sc = m.schema_;
    return sc.prop_indexed(label) ? sc.enumeration_prop(label) : "";
  }
  double type_count(const std::string& label) const { return stats.type_count(label); }   // refused by name when missing
  double owners(const std::string& etype, bool out) const {   // the population that can own (etype, dir) lists
    std::set<std::string> labs;
    for (auto& [src, dst] : m.schema_.edge(etype).endpoints) labs.insert(out ? src : dst);
    double n = 0;
    for (auto& l : labs) n += type_count(l);
    return n;
  }
  // owners with a list, of the label's vertices (the tally's owners, per owner label)
  double share_label(const std::string& etype, bool out, const std::string& label) const {
    auto it = owners_of.find({etype, out ? "out" : "in", label});
    double tc = type_count(label);
    return it == owners_of.end() || tc <= 0 ? 0.0 : std::min(1.0, it->second / tc);
  }
  double share(const std::string& etype, bool out) const {   // owners with a list, of the whole owner population
    std::set<std::string> labs;
    for (auto& [src, dst] : m.schema_.edge(etype).endpoints) labs.insert(out ? src : dst);
    double d = 0;
    for (auto& l : labs) {
      auto it = owners_of.find({etype, out ? "out" : "in", l});
      if (it != owners_of.end()) d += it->second;
    }
    double p = owners(etype, out);
    return p > 0 ? std::min(1.0, d / p) : 0.0;
  }
  // the probes that find a list: per input label when the step's label mix is known (`label_mix`: label id -> distinct
  // inputs, "?" = no label carried), else the population's share
  double present_of(const std::string& etype, bool out, double n) const {
    if (!in.label_mix && in.label_share) {   // level predicted: by the far-label share of the producer
      double p = 0;
      for (auto& [l, sh] : *in.label_share) p += sh * share_label(etype, out, l);
      return n * p;
    }
    if (!in.label_mix) return n * share(etype, out);
    double p = 0;
    for (auto& [k, c] : *in.label_mix) {
      if (k == "?") { p += c * share(etype, out); continue; }
      p += c * share_label(etype, out, label_of_id(k));
    }
    return p;
  }
  std::string label_of_id(const std::string& k) const {
    Value v = Value::vid(0);
    v.label = int16_t(std::stoi(k));
    std::optional<std::string> lab = m.tag_label(v);
    if (!lab) throw std::runtime_error("predict: label id " + k + " names no label");
    return *lab;
  }
  // the share of a step's inputs a one-label read reads: those carrying `label` or no label (`label_mix`, the truth-card levels), else
  // the label's share of the set (`label_share`: the far labels of the hop that made it, from the owners), else all
  double read_share(const std::string& label) const {
    if (in.label_mix) {
      double all = 0, mine = 0;
      for (auto& [k, c] : *in.label_mix) {
        all += c;
        if (k == "?" || label_of_id(k) == label) mine += c;
      }
      return all > 0 ? mine / all : 1.0;
    }
    // a label the far end's share does not name sits at no edge's far end: 0 is its share, not a missing statistic
    if (in.label_share) {
      auto it = in.label_share->find(label);
      return it == in.label_share->end() ? 0.0 : it->second;
    }
    return 1.0;
  }
  double share_of_label(const std::string& label) const {   // a probed vid holds the label: the label's share of all vertices
    double all = stats.total_vertices;
    return all > 0 ? type_count(label) / all : 0;
  }
  static double balls(double B, double n) { return predict::balls(B, n); }
  // the Chunked chunks the step's reads touch, summed (the closed form), and the families they are in
  mutable double chunks = 0;
  mutable std::set<std::string> chunk_families;
  double landed(double touched, const std::string& family) const {
    chunks += touched;
    chunk_families.insert(family);
    return touched;
  }
  // the term chunks_touched of `family`. From the truth card it is the chunks the whole step's landings on the family
  // charged (one replay), so a step that reads the family more than once (an edge check's two reads of one direction, a
  // via walk beside them) counts it at its first read and 0 at the others; from the statistics each read is its own
  mutable std::set<std::string> chunks_from_card;
  template <class F> double touched_term(const std::string& family, F&& stat) const {
    bool card = in.true_terms && in.true_terms->count(predict::term_key("chunks_touched", family));
    if (card && !chunks_from_card.insert(family).second) return landed(0.0, family);
    return landed(term("chunks_touched", family, stat), family);
  }
  // the chunks of `family` (C of them) n vertices touch (predict/landing.h), from the step's producer when it has one
  double chunks_touched(double C, double n, const std::string& family) const {
    return touched_term(family, [&] {
      return predict::chunks_touched(C, n, family, in.producer ? &*in.producer : nullptr, tally);
    });
  }
  // the same, spread uniformly (no producer applies)
  double chunks_uniform(double C, double n, const std::string& family) const {
    return touched_term(family, [&] { return predict::balls(C, n); });
  }

  // the anchor's own list in one direction: (present, entries), from the anchor statistics (stats.h)
  std::optional<std::pair<double, double>> anchor_list(const std::string& etype, bool out) const {
    if (!in.anchor_vid) return std::nullopt;
    const std::string& vid = *in.anchor_vid;
    // no anchor tier (level predicted, or statistics derived for another scale): priced from the population
    if (!stats.has_anchor_tier()) return std::nullopt;
    // every vertex of the anchor labels has a row (no such edge: 0); a vertex with no row is no anchor: the population
    std::optional<double> a = stats.anchor_degree(vid, etype, out ? "out" : "in");
    if (!a) return std::nullopt;
    double deg = *a;
    return std::make_pair(deg > 0 ? 1.0 : 0.0, deg);
  }

  PCounts hop(const json& s) {
    const json& a = s.at("args");
    std::string etype = a.at("etype").get<std::string>();
    std::string dir = kv::dir_of(a);
    return hop_of(etype, dir, StepPredictor::strs(s, "label"), predict::need(in.n, "n"), predict::need(in.entries, "entries"), !StepPredictor::strs(s, "edge_props").empty(),
                  true);
  }

  // one direction of a hop's shape and card, for pricing that direction's part alone (hop_rule sums its directions)
  static Meta::HopShape one_dir(const Meta::HopShape& sh, bool d) {
    Meta::HopShape one = sh;
    one.dirs = {d};
    return one;
  }
  static StepCard one_card(const StepCard& c, const HopDirCard& k) {
    StepCard one;
    one.dirs = {k};
    one.entries_used = c.entries_used;
    return one;
  }

  // a hop over n sources reaching `entries` entries, every entry used. `use_anchor`: the sources are the plan's anchor
  // when the inputs say so (anchor_list)
  PCounts hop_of(const std::string& etype, const std::string& dir, const std::vector<std::string>& labels, double n,
                 double entries, bool edge_props, bool use_anchor) const {
    Meta::HopShape sh = m.hop_shape(etype, dir, labels);
    StepCard c;
    c.dirs.assign(sh.dirs.size(), HopDirCard());
    PCounts out;
    // a hop's entries split evenly over its directions (one edge type holds the same edges both ways), unless the
    // anchor's own degrees are known per direction
    double per_dir = sh.dirs.empty() ? 0 : entries / double(sh.dirs.size());
    for (size_t di = 0; di < sh.dirs.size(); di++) {
      bool d = sh.dirs[di];
      const Component& al = m.cfg_.adj_of(etype, d);
      if (al.packed() && al.props_own() && edge_props)
        throw std::runtime_error("predict: Chunked edge props in their own chunks (not covered)");
      auto anch = use_anchor ? anchor_list(etype, d) : std::nullopt;
      // a Chunked direction costs its chunks from n alone (hop_dir): it reads no list count
      double present = al.packed() ? 0.0 : term("present", adj_name(etype, d), [&] { return anch ? anch->first * n : present_of(etype, d, n); });
      double ent = al.packed() ? 0.0 : term("entries", adj_name(etype, d), [&] { return anch ? anch->second : per_dir; });
      PCounts pd;   // this direction's part: the chunks it reads, then its card priced alone
      hop_dir(etype, d, n, present, ent, c.dirs[di], pd);
      out.add(pd);
      pd.add(rules.hop_rule(one_dir(sh, d), etype, one_card(c, c.dirs[di])));
      charge_adj(etype, d, pd);
    }
    out.add(rules.hop_rule(sh, etype, c));
    return out;
  }

  // one direction of a hop-like read: n probes, `present` of them find a list, `ent` entries in those lists. Chunked:
  // the distinct chunks the probes touch, each read whole (into `out`); else the card hop_rule prices
  void hop_dir(const std::string& etype, bool d, double n, double present, double ent, HopDirCard& k, PCounts& out) const {
    const Component& al = m.cfg_.adj_of(etype, d);
    double edges = 0;
    std::array<double, 3> t = adj_part(etype, d, &edges);
    if (al.packed()) {
      std::string fam = etype + (d ? ":out" : ":in");
      double touched = chunks_touched(t[0], n, fam);
      out.S += touched;
      out.scan_key += touched * term("chunk_key_mean", fam, [&] { return t[0] > 0 ? t[1] / t[0] : 0; });
      out.scan_value += touched * term("chunk_value_mean", fam, [&] { return t[0] > 0 ? t[2] / t[0] : 0; });
      return;
    }
    std::string P = adj_name(etype, d);
    double key = al.grouped() ? 0 : term("adj_key_mean", P, [&] { return t[0] > 0 ? t[1] / t[0] : 0; });
    k.probes = n;
    k.present = present;
    k.entries = ent;
    if (!al.grouped())   // Split: the mean record
      k.value_bytes = ent * term("adj_value_mean", P, [&] { return t[0] > 0 ? t[2] / t[0] : 0; });
    else {               // Condensed: bytes per entry
      k.value_bytes = ent * term("adj_value_mean", P, [&] { return edges > 0 ? t[2] / edges : 0; });
      key = double(adj_prefix(uint8_t(m.schema_.edge(etype).id), d, 0).size());
    }
    k.absent_key_bytes = (n - present) * key;    // the foreign key: taken as one of the same family
    k.end_key_bytes = al.grouped() ? 0 : present * key;
  }

  // read_edge_exists (the truth path: edge_exists above): the via walk, a hop with every entry used; then each read by
  // the layout of the (etype, dir) it names. Condensed / Chunked: the lists of #eown<i> owners (the anchors, or the
  // candidates still to do), no entry used; the anchor's own degrees when the anchors are the plan's anchor. Split:
  // one GET per (candidate, anchor) pair, #eprobe<i>, priced as misses (a hit stops a candidate's GETs early and
  // reads a small value: both left out, as the layer's #ehits)
  PCounts edge_exists(const json& s) const {
    const json& a = s.at("args");
    PCounts out;
    if (predict::need(in.rows, "rows") <= 0) return out;
    if (predict::need(in.nvia, "nvia") > 0)
      out.add(hop_of(a.at("via_etype").get<std::string>(), "out", {}, predict::need(in.nvia, "nvia"), predict::need(in.nvnbrs, "nvnbrs"), false, false));
    std::string etype = a.at("etype").get<std::string>();
    uint8_t eid = uint8_t(m.schema_.edge(etype).id);
    Args ra;
    ra.set("reads", from_json(a.at("reads")));
    std::vector<EdgeRead> reads = edge_reads(ra, "meta");
    // an owner's list holds this many entries: the edges over the owners with a list (both from the statistics)
    auto per_list = [&](bool d) {
      double lists = 0;
      for (auto& [k, v] : owners_of)
        if (std::get<0>(k) == etype && std::get<1>(k) == (d ? "out" : "in")) lists += v;
      return lists > 0 ? stats.edge_total(etype, "an edge probe's list size") / lists : 0.0;
    };
    // a single anchor: the plan's own, costed from its degrees when the inputs carry it (level predicted_anchor and the truth-card levels)
    bool one_anchor = in.anchor_vid && predict::need(in.nothers, "nothers") == 1;
    for (size_t i = 0; i < reads.size(); i++) {
      const EdgeRead& r = reads[i];
      const Component& al = m.cfg_.adj_of(etype, r.out);
      std::string I = std::to_string(i);
      if (al.packed() || al.grouped()) {
        double n = predict::need(i < in.eown.size() ? in.eown[i] : std::nullopt, ("eown" + I).c_str());
        auto anch = r.at_anchor && one_anchor ? anchor_list(etype, r.out) : std::nullopt;
        const std::string part = adj_name(etype, r.out) + "#" + I;   // this read's own terms (StepPredictor::edge_exists)
        double present = al.packed() ? 0.0 : term("present", part, [&] { return anch ? anch->first * n : present_of(etype, r.out, n); });
        double ent = al.packed() ? 0.0 : term("entries", part, [&] {
          return anch ? anch->second * n
                      : present * term("list_entries_mean", part, [&] { return per_list(r.out); });
        });
        Meta::HopShape sh;
        sh.dirs = {r.out};
        sh.two = false;
        sh.packed = al.packed();
        StepCard c;
        c.dirs.assign(1, HopDirCard());
        c.entries_used = false;
        PCounts pd;
        hop_dir(etype, r.out, n, present, ent, c.dirs[0], pd);
        out.add(pd);
        PCounts rr = rules.hop_rule(sh, etype, c);
        out.add(rr);
        pd.add(rr);
        charge_adj(etype, r.out, pd);
      } else {
        PropsCard k;
        k.asked = predict::need(i < in.eprobe.size() ? in.eprobe[i] : std::nullopt, ("eprobe" + I).c_str());
        PCounts pd;
        rules.get_lanes(double(edge_key(eid, r.out, 0, 0).size()), k, 0, pd);
        out.add(pd);
        charge_adj(etype, r.out, pd);
      }
    }
    return out;
  }

  // the props of n vertices of `lab`, each read once (labelled: no probe)
  PCounts props(const Component& layout, const std::string& lab, const std::vector<std::string>& props, double n) {
    PCounts out;
    std::vector<std::string> row_props;
    for (auto& p : props) if (p != "id") row_props.push_back(p);
    if (row_props.empty()) return out;
    n = term("props_n", "props:" + lab, [&] { return n; });
    if (n <= 0) return out;
    const std::array<double, 3>& t = part("vertex_props", lab, "");
    if (layout.packed()) {
      double touched = chunks_touched(t[0], n, "props:" + lab);
      out.S += touched;
      out.scan_key += touched * term("chunk_key_mean", "props:" + lab, [&] { return t[0] > 0 ? t[1] / t[0] : 0; });
      out.scan_value += touched * term("chunk_value_mean", "props:" + lab, [&] { return t[0] > 0 ? t[2] / t[0] : 0; });
      charge("props:" + lab, out);
      return out;
    }
    StepCard c;
    double tc = type_count(lab);
    Meta::PropsPlan plan = m.props_plan(layout, props, {lab});
    if (layout.grouped()) {
      if (m.row_groups(lab).cols.size() > 1)   // the tally holds one row size per label, not per group
        throw std::runtime_error("predict: an estimate over a label with several row groups (not covered)");
      for (size_t g : plan.of(lab).groups.groups) {
        PropsCard& k = c.props[{lab, g}];
        k.asked = n;
        k.hits = n;                                     // a labelled vertex has its rows
        k.value_bytes = n * term("row_value_mean", "props:" + lab, [&] { return tc > 0 ? t[2] / tc : 0; });   // the mean row (one group per label here)
      }
    } else {
      for (size_t pi = 0; pi < props.size(); pi++) {
        if (props[pi] == "id") continue;
        int at = plan.of(lab).attr[pi];
        if (at < 0) continue;
        auto content = [&] { return stats.prop_content(lab, props[pi], "a props read's hits and bytes"); };
        std::string P = "props:" + lab + ":" + std::to_string(at);
        PropsCard& k = c.props[{lab, size_t(at)}];
        k.asked = n;
        k.hits = n * term("prop_hit_share", P, [&] { double u = content().first; return tc > 0 ? std::min(1.0, u / tc) : 0; });
        // the dataset's bytes per value (not meta's cell encoding)
        k.value_bytes = k.hits * term("prop_value_mean", P, [&] { auto ub = content(); return ub.first > 0 ? ub.second / ub.first : 0; });
      }
    }
    out.add(rules.props_rule(layout, c));
    charge("props:" + lab, out);
    return out;
  }

  // ---- writes: the estimate card ----
  // A write step names its vertices and values (the params), so its key and written bytes are exact from the args
  // where the item is new; what the statistics give is the size of each list, chunk or row a write reads and rewrites.
  // The degree of a list's owner: for the owner of a list an insert grows, the list a new edge from the far end's label
  // meets (degree_by_far_label: an owner drawn by its edges from that label: the
  // population mean would have LDBC SF3's updates read 0.14-0.24x of their Condensed list bytes, and the size-biased
  // mean over all of a label's lists would charge a new Person's City list as a Country's message list, up to 160x); an endpoint of an edge that must exist (both ends of a deleted or updated edge, a deleted vertex's
  // neighbours) the size-biased mean sum d^2 / sum d; else the population mean (degree_hist: presence, then the mean
  // degree of an owner with a list); the anchor statistics' own degree where they hold the vertex (predicted_anchor; level predicted
  // gets them empty); a vertex created earlier in the request: no list, then the edges the request added
  // (`in.created`, `in.added`). A Condensed list past the split size is read and rewritten one part at a time
  // (unit::rmw_locate): a full part (the cap) for an existing far end, the last part for a far vertex created in the
  // request (its id lands past every existing id). Ingest cuts full parts first and leaves the remainder last, so the
  // last part's size is that list's remainder: at level predicted it is unknown, and split / 2 is its expected value (LDBC
  // SF3's IU6/IU7 share one Country whose last part is 41 KB, so they read about 3x their measured bytes there).
  // Not modelled: a chunk's first id moving, a Chunked edge-prop chunk of its own (refused by name).
  static PCounts times(PCounts p, double k) {   // every lane times k (an expected count of like writes)
    for (double* x : {&p.S, &p.G, &p.N, &p.S_miss, &p.G_miss, &p.N_miss, &p.get_key, &p.get_value, &p.scan_key,
                      &p.scan_value, &p.seek_miss_key, &p.next_miss_key, &p.get_miss_key, &p.used_key, &p.used_value,
                      &p.P, &p.D, &p.write_key, &p.write_value, &p.lc_reads, &p.lc_read_bytes, &p.lc_read_target,
                      &p.lc_writes, &p.lc_write_bytes, &p.lc_write_base, &p.lc_write_target})
      *x *= k;
    return p;
  }
  struct Deg {
    double p = 0;   // the share of owners with a list
    double d = 0;   // the mean degree of an owner with a list
    double one = 0; // the share of those lists holding one entry (a removal empties them)
  };
  std::vector<std::string> owner_labels(const std::string& etype, bool out) const {
    std::set<std::string> labs;
    for (auto& [src, dst] : m.schema_.edge(etype).endpoints) labs.insert(out ? src : dst);
    return {labs.begin(), labs.end()};
  }
  // degree_hist rows [bucket, vertices, sum d, sum d^2] of (label, etype, dir), summed over the owner labels when the
  // label is not known; {vertices, vertices with no list, sum d, sum d^2, vertices with one entry}
  std::array<double, 5> hist(const std::string& label, const std::string& etype, bool out) const {
    std::array<double, 5> h = {0, 0, 0, 0, 0};
    std::vector<std::string> labs = label.empty() ? owner_labels(etype, out) : std::vector<std::string>{label};
    for (auto& l : labs) {
      const json* rows = stats.degree_rows(l, etype, out ? "out" : "in", "a write's list sizes");
      if (!rows) continue;   // a label that owns no such list: all of it degree 0
      for (auto& r : *rows) {
        h[0] += r[1].get<double>();
        if (r[0].get<int>() == 0) h[1] += r[1].get<double>();
        if (r[0].get<int>() == 1) h[4] += r[1].get<double>();   // bucket 1: degree exactly 1
        h[2] += r[2].get<double>();
        h[3] += r[3].get<double>();
      }
    }
    return h;
  }
  bool created(const std::string& v) const {   // a vertex an earlier step of the request created
    for (auto& x : in.created) if (x == v) return true;
    return false;
  }
  // the owner `vid`'s list of (etype, dir): created in the request, else the anchor statistics, else the population
  // (or the size-biased mean when the vertex is an endpoint of an edge that must exist)
  Deg degree(const std::string& vid, const std::string& label, const std::string& etype, bool out, bool biased) const {
    std::string dir = out ? "out" : "in";
    if (created(vid)) {
      auto ad = in.added.find(vid + "|" + etype + "|" + dir);
      double added = ad == in.added.end() ? 0.0 : ad->second;
      return {added > 0 ? 1.0 : 0.0, added, added == 1 ? 1.0 : 0.0};
    }
    // the anchor's own degree per edge type and direction (graph-stats --anchor-labels): a vertex of those labels;
    // one it lacks (no edge at all, or another label) is priced as below
    if (!vid.empty())
      if (std::optional<double> a = stats.anchor_degree(vid, etype, dir)) {
        double d = *a;
        if (!(biased && d <= 0)) return {d > 0 ? 1.0 : 0.0, d, d == 1 ? 1.0 : 0.0};
      }
    // vid "" (a vertex the step does not name: a deleted vertex's neighbour), or no anchor row: the population's
    std::array<double, 5> h = hist(label, etype, out);
    // size-biased: an owner drawn by its edges (a degree-1 owner holds h[4] of the sum d)
    if (biased) return {h[2] > 0 ? 1.0 : 0.0, h[2] > 0 ? h[3] / h[2] : 0.0, h[2] > 0 ? h[4] / h[2] : 0.0};
    double with = h[0] - h[1];
    return {h[0] > 0 ? with / h[0] : 0.0, with > 0 ? h[2] / with : 0.0, with > 0 ? h[4] / with : 0.0};
  }
  double etype_edges(const std::string& etype) const { return stats.edge_total(etype, "a write's edges"); }
  double entry_bytes(const std::string& etype, bool out) const {   // a list's or a chunk's bytes per edge (a term)
    return term("adj_value_mean", adj_name(etype, out), [&] {
      double e = etype_edges(etype);
      return e > 0 ? part("adjacency", etype, out ? "out" : "in")[2] / e : 0.0;
    });
  }
  double last_chunk(const std::string& family) const {   // a term
    return term("last_chunk_value", family, [&] {
      if (!tally.contains("chunks") || !tally["chunks"].contains(family) ||
          !tally["chunks"][family].contains("last_chunk_value_bytes"))
        throw std::runtime_error("predict: the tally has no last_chunk_value_bytes for " + family + " (a full tally writes it)");
      return tally["chunks"][family]["last_chunk_value_bytes"].get<double>();
    });
  }
  uint64_t first_id(const std::string& family) const {
    if (!tally.contains("chunks") || !tally["chunks"].contains(family) || !tally["chunks"][family].contains("first_id"))
      throw std::runtime_error("predict: the tally has no first_id for " + family + " (a full tally writes it)");
    uint64_t u;
    if (!canonical_vid(tally["chunks"][family]["first_id"].get<std::string>(), &u))
      throw std::runtime_error("predict: the tally's first_id for " + family + " is not a vid");
    return u;
  }
  static std::string vid_hex(const json& a, const char* k) {   // the canonical vid string the statistics key by
    return hex16(StepPredictor::vid_arg(a, k));
  }
  // a Condensed list of degree d: its value bytes (capped at a split unit's part size)
  double list_bytes(const Component& al, const std::string& etype, bool out, double d) const {
    double v = d * entry_bytes(etype, out);
    return al.split > 0 ? std::min(v, double(al.split)) : v;
  }
  // a Condensed RMW of one owner's list (unit::rmw_locate + the rewrite): present (share p): 1 SEEK on the list (a split
  // unit: + the part's SEEK), the list read and PUT back with `delta` entries; absent: a miss, a new list PUT when adding
  // `last`: the entry goes to a split list's last part (a far vertex created in the request), split / 2 expected
  void condensed_rmw(PCounts& o, const Component& al, const std::string& etype, bool out, uint64_t owner, Deg g,
                     double delta, bool last = false) const {
    double key = double(adj_prefix(uint8_t(m.schema_.edge(etype).id), out, owner).size());
    // the owner's list: found or not, and its degree (terms; the truth-card levels take them from the RMW's read)
    g.p = term("present", adj_name(etype, out), [&] { return g.p; });
    g.d = term("list_entries_mean", adj_name(etype, out), [&] { return g.d; });
    double eb = entry_bytes(etype, out);
    double L = list_bytes(al, etype, out, g.d);
    bool parts = al.split > 0 && g.d * eb > double(al.split);
    if (parts && last) L = double(al.split) / 2;
    double p = g.p;
    o.S += p * (parts ? 2 : 1);
    o.scan_key += p * key * (parts ? 2 : 1);
    o.scan_value += p * L;
    o.used_key += p * key;
    double after = L + delta * eb;
    double emptied = delta < 0 ? g.one : 0.0;   // the last entry removed: the list DELed, not PUT
    o.P += p * (1 - emptied);
    o.D += p * emptied;
    o.write_key += p * key;
    o.write_value += p * (1 - emptied) * std::max(0.0, after);
    if (delta > 0) {   // absent: the probe misses, the new list is PUT
      o.S += 1 - p;
      o.S_miss += 1 - p;
      o.seek_miss_key += (1 - p) * key;
      o.P += 1 - p;
      o.write_key += (1 - p) * key;
      o.write_value += (1 - p) * delta * eb;
    } else {
      o.S += 1 - p;
      o.S_miss += 1 - p;
      o.seek_miss_key += (1 - p) * key;
    }
  }
  // a Chunked RMW of the chunk holding `owner` (chunk_rmw): 1 SEEK back, the chunk read whole, PUT back with
  // `delta_bytes` (exec/chunk_write.cc locate: the chunk whose first id is the highest at or below the owner's). An
  // owner that exists: a chunk of the family's mean. A vertex `created` in the request, of `label`: a new vertex takes
  // an id above every id of its own vid group (workloads/writes/_shared.py synthetic_vid_let: 2^50 + salt + the
  // person's id, inside the group's tag band), so it lands at the end of its group's block -- the family's last chunk
  // when no group with a higher vid tag (the schema's vid_tags) owns lists in the family (the statistics' owners per
  // label), else a middle chunk; either chunk assumed contract::last_chunk_fill() of the pack target (the tally's
  // last_chunk_value_bytes for the last one), a last one a touch of the last-chunk lane. The share of RMWs that land on
  // the last chunk is the term chunk_last_share
  void chunk_rmw(PCounts& o, const std::string& prefix, const std::string& family, uint64_t owner, double mean_chunk,
                 uint64_t target, bool created, double delta_bytes, const std::string& label = "") const {
    double key = double(chunk_with_id(prefix, owner).size());
    if (owner < first_id(family)) {   // below the family's first chunk: the SEEK back misses, a new chunk is PUT
      o.S += 1;
      o.S_miss += 1;
      o.seek_miss_key += key;
      if (delta_bytes > 0) {
        o.P += 1;
        o.write_key += key;
        o.write_value += delta_bytes;
      }
      return;
    }
    const bool at_end = created && !higher_group_owns(family, label);   // the formula's landing
    const double p = term("chunk_last_share", family, [&] { return at_end ? 1.0 : 0.0; });
    const double mid = term("chunk_value_mean", family, [&] {
      return created ? double(target) * contract::last_chunk_fill() : mean_chunk; });
    const double lc = p > 0 ? last_chunk(family) : 0.0;
    const double c = p * lc + (1 - p) * mid;
    o.S += 1;
    o.scan_key += key;
    o.scan_value += c;
    o.P += 1;
    o.write_key += key;
    o.write_value += std::max(0.0, c + delta_bytes);
    if (p > 0) {
      o.lc_reads += p;
      o.lc_read_bytes += p * lc;
      o.lc_read_target += p * double(target);
      o.lc_writes += p;
      o.lc_write_bytes += p * std::max(0.0, lc + delta_bytes);
      o.lc_write_base += p * lc;
      o.lc_write_target += p * double(target);
    }
  }
  // a family "<etype>:<dir>" with owners (the statistics) of a vid group whose tag is above `label`'s: a new vertex of
  // `label` does not land in the family's last chunk. The label comes from the plan, never from the id
  bool higher_group_owns(const std::string& family, const std::string& label) const {
    size_t c = family.rfind(':');
    if (label.empty() || family.rfind("props:", 0) == 0 || c == std::string::npos) return false;
    const std::string etype = family.substr(0, c), dir = family.substr(c + 1);
    const int mine = m.schema_.tag_of_label(label);
    for (auto& [k, n] : owners_of)
      if (std::get<0>(k) == etype && std::get<1>(k) == dir && n > 0 && m.schema_.tag_of_label(std::get<2>(k)) > mine)
        return true;
    return false;
  }
  double adj_chunk_mean(const std::string& etype, bool out) const {
    const std::array<double, 3>& t = part("adjacency", etype, out ? "out" : "in");
    return mean(t, 2);
  }
  struct EdgeWInfo {
    std::string etype;
    uint8_t eid;
    bool rev, has_ep, inline_props, packed_out;
  };
  EdgeWInfo edge_info(const std::string& etype) const {
    EdgeWInfo e;
    e.etype = etype;
    e.eid = uint8_t(m.schema_.edge(etype).id);
    e.rev = m.reverse_available(etype);
    e.has_ep = !m.ecols(etype).empty();
    const Component& out = m.cfg_.adj_of(etype, true);
    e.inline_props = !out.props_own();
    e.packed_out = out.packed();
    return e;
  }
  void refuse_own_ep(const EdgeWInfo& e) const {
    if (e.packed_out && !e.inline_props && e.has_ep)
      throw std::runtime_error("predict: Chunked edge props in their own chunks (not covered)");
  }
  // a Split edge record's value: the far label byte + the encoded props (exact from the args)
  double split_record_value(const std::string& etype, bool out, const Value& props) const {
    const std::vector<ColDecl>& ec = m.ecols(etype);
    std::string v;
    if (!ec.empty()) {
      std::vector<PropRow> rows = {{0, &props}};
      ValueRows rs(ec, rows);
      columns::encode(v, ec, rs);
    }
    return double(v.size()) + (m.far_bits(etype, out) ? 1.0 : 0.0);
  }
  std::string label_arg(const json& a, const char* k) const { return a.contains(k) && a[k].is_string() ? a[k].get<std::string>() : ""; }

  // an insert's owner: created in the request or an anchor, as degree(); else drawn by its edges from the far end's
  // label (degree_by_far_label), every such owner holding a list
  Deg insert_degree(const std::string& vid, const std::string& label, const std::string& far, const std::string& etype,
                    bool out) const {
    if (created(vid) || (!vid.empty() && stats.anchor_degree(vid, etype, out ? "out" : "in")))
      return degree(vid, label, etype, out, false);
    std::array<double, 2> f = stats.far_degree(label, etype, out ? "out" : "in", far, "an insert's list size");
    return {f[0] > 0 ? 1.0 : 0.0, f[0] > 0 ? f[1] / f[0] : 0.0, 0.0};
  }

  // insert_edge: each stored direction by its layout; an owner's list the one a new edge from the far end's label meets
  // (insert_degree), a created vertex from the request
  PCounts insert_edge_est(const json& s) const {
    const json& a = s.at("args");
    EdgeWInfo e = edge_info(a.at("etype").get<std::string>());
    refuse_own_ep(e);
    uint64_t su = StepPredictor::vid_arg(a, "src"), du = StepPredictor::vid_arg(a, "dst");
    std::string sv = vid_hex(a, "src"), dv = vid_hex(a, "dst");
    Value props = a.contains("props") && !a["props"].is_null() ? from_json(a["props"]) : Value::row({});
    std::string sl = label_arg(a, "src_label"), dl = label_arg(a, "dst_label");
    PCounts o;
    auto side = [&](bool out) { charged_to_adj(o, e.etype, out, out ? sl : dl, [&] {
      uint64_t owner = out ? su : du, other = out ? du : su;
      const std::string& ov = out ? sv : dv;
      const Component& al = m.cfg_.adj_of(e.etype, out);
      Deg g = insert_degree(ov, out ? sl : dl, out ? dl : sl, e.etype, out);   // read by the Condensed RMW only
      if (al.packed()) {
        chunk_rmw(o, padj_prefix(e.eid, out), e.etype + (out ? ":out" : ":in"), owner, adj_chunk_mean(e.etype, out),
                  al.owners.pack, created(ov), entry_bytes(e.etype, out), out ? sl : dl);
      } else if (al.grouped()) {
        condensed_rmw(o, al, e.etype, out, owner, g, 1, created(out ? dv : sv));
      } else {
        o.P += 1;
        o.write_key += double(edge_key(e.eid, out, owner, other).size());
        o.write_value += split_record_value(e.etype, out, props);
      }
    }); };
    side(true);
    if (e.rev) side(false);
    return o;
  }

  // delete_edge: the edge exists, so both owners are endpoints of an existing edge (size-biased)
  PCounts delete_edge_est(const json& s) const {
    const json& a = s.at("args");
    EdgeWInfo e = edge_info(a.at("etype").get<std::string>());
    refuse_own_ep(e);
    uint64_t su = StepPredictor::vid_arg(a, "src"), du = StepPredictor::vid_arg(a, "dst");
    std::string sv = vid_hex(a, "src"), dv = vid_hex(a, "dst");
    std::string sl = label_arg(a, "src_label"), dl = label_arg(a, "dst_label");
    PCounts o;
    auto side = [&](bool out) { charged_to_adj(o, e.etype, out, out ? sl : dl, [&] {
      uint64_t owner = out ? su : du, other = out ? du : su;
      const std::string& ov = out ? sv : dv;
      const Component& al = m.cfg_.adj_of(e.etype, out);
      Deg g = degree(ov, "", e.etype, out, true);
      if (al.packed()) {
        chunk_rmw(o, padj_prefix(e.eid, out), e.etype + (out ? ":out" : ":in"), owner, adj_chunk_mean(e.etype, out),
                  al.owners.pack, false, -entry_bytes(e.etype, out));
      } else if (al.grouped()) {
        condensed_rmw(o, al, e.etype, out, owner, g, -1);
      } else {
        o.D += 1;
        o.write_key += double(edge_key(e.eid, out, owner, other).size());
      }
    }); };
    side(true);
    if (e.rev) side(false);
    return o;
  }

  // update_edge_prop: the out side read through its layout (Split: 1 GET of the record), every stored copy rewritten
  PCounts update_edge_est(const json& s) const {
    const json& a = s.at("args");
    EdgeWInfo e = edge_info(a.at("etype").get<std::string>());
    refuse_own_ep(e);
    uint64_t su = StepPredictor::vid_arg(a, "src"), du = StepPredictor::vid_arg(a, "dst");
    std::string sv = vid_hex(a, "src"), dv = vid_hex(a, "dst");
    Value props = from_json(a.at("props"));
    std::string sl = label_arg(a, "src_label"), dl = label_arg(a, "dst_label");
    PCounts o;
    auto side = [&](bool out, bool read_split) { charged_to_adj(o, e.etype, out, out ? sl : dl, [&] {
      uint64_t owner = out ? su : du, other = out ? du : su;
      const std::string& ov = out ? sv : dv;
      const Component& al = m.cfg_.adj_of(e.etype, out);
      if (al.packed()) {
        chunk_rmw(o, padj_prefix(e.eid, out), e.etype + (out ? ":out" : ":in"), owner, adj_chunk_mean(e.etype, out),
                  al.owners.pack, false, 0);
      } else if (al.grouped()) {
        condensed_rmw(o, al, e.etype, out, owner, degree(ov, "", e.etype, out, true), 0);
      } else {
        double key = double(edge_key(e.eid, out, owner, other).size());
        const std::array<double, 3>& t = part("adjacency", e.etype, out ? "out" : "in");
        if (read_split) {   // the record GET (a hit: the edge exists)
          o.G += 1;
          o.get_key += key;
          o.get_value += mean(t, 2);
          o.used_key += key;
        }
        o.P += 1;
        o.write_key += key;
        o.write_value += std::max(mean(t, 2), split_record_value(e.etype, out, props));
      }
    }); };
    side(true, true);
    if (e.rev) side(false, false);
    return o;
  }

  // the props of one vertex written on an unpacked layout: exact from the row (the truth card's own put_vertex)
  void put_vertex_exact(PCounts& o, const std::string& label, uint64_t u, const Value& row,
                        const std::vector<std::string>& changed) {
    ChunkWrites w;
    rules.put_vertex_truth(w, label, u, row, changed);
    for (auto& [k, v] : w.puts) {
      o.P += 1;
      o.write_key += double(k.size());
      o.write_value += double(v.size());
    }
  }
  double props_chunk_mean(const std::string& label) const { return mean(part("vertex_props", label, ""), 2); }
  double props_row_bytes(const std::string& label) const {   // a vertex's props bytes (the part over its vertices; a term)
    return term("row_value_mean", "props:" + label, [&] {
      double n = type_count(label);
      return n > 0 ? part("vertex_props", label, "")[2] / n : 0.0;
    });
  }
  double index_key_mean(const std::string& label, const std::string& prop) const { return mean(part("index", label, "", prop), 1); }

  PCounts insert_vertex_est(const json& s) {
    const json& a = s.at("args");
    std::string label = a.at("label").get<std::string>();
    uint64_t u = StepPredictor::vid_arg(a, "vid");
    Value props = from_json(a.at("props"));
    PCounts o;
    charged_to(o, "props:" + label, [&] {
      if (m.cfg_.props_of(label).packed())   // a new vid: the family's last chunk
        chunk_rmw(o, vertex_family(m.vtype_of(label)), "props:" + label, u, props_chunk_mean(label), m.cfg_.props_of(label).owners.pack, true,
                  props_row_bytes(label));
      else
        put_vertex_exact(o, label, u, props, {});
    });
    // the index entries, in index_entries' order: the type entry (a label with no property index), then each column's
    std::vector<std::string> ix_parts;
    if (!m.schema_.prop_indexed(label)) ix_parts.push_back(index_part(label, ""));
    for (auto& p : m.schema_.index_cols_of(label)) ix_parts.push_back(index_part(label, p));
    std::vector<std::string> keys = rules.index_entries(label, u, props);
    for (size_t i = 0; i < keys.size(); i++)
      charged_to(o, i < ix_parts.size() ? ix_parts[i] : index_part(label, ""), [&] {
        o.P += 1;
        o.write_key += double(keys[i].size());
      });
    return o;
  }

  PCounts update_prop_est(const json& s) {
    const json& a = s.at("args");
    std::string label = a.at("label").get<std::string>();
    uint64_t u = StepPredictor::vid_arg(a, "vid");
    std::vector<std::pair<std::string, Value>> cells;
    if (a.contains("props") && !a["props"].is_null()) {
      Value p = from_json(a["props"]);
      for (auto& [name, v] : p.as_row()) cells.push_back({name, v});
    } else {
      cells.push_back({a.at("prop").get<std::string>(), from_json(a.at("value"))});
    }
    std::vector<std::string> changed;
    RowFields nf;
    for (auto& [n, v] : cells) { changed.push_back(n); row_set(nf, n, v); }
    Value new_cells = make_row(std::move(nf));
    std::vector<std::string> icols = m.schema_.index_cols_of(label);
    auto indexed = [&](const std::string& p) { return std::find(icols.begin(), icols.end(), p) != icols.end(); };
    PCounts o;
    const Component& pl = m.cfg_.props_of(label);
    uint8_t vt = m.vtype_of(label);
    charged_to(o, "props:" + label, [&] {
    if (pl.packed()) {
      chunk_rmw(o, vertex_family(vt), "props:" + label, u, props_chunk_mean(label), m.cfg_.props_of(label).owners.pack, created(vid_hex(a, "vid")), 0);
    } else if (pl.grouped()) {   // each row group holding a changed column: 1 GET (a hit), rewritten at its mean size
      const std::array<double, 3>& t = part("vertex_props", label, "");
      for (size_t g : m.groups_for(label, changed)) {
        double key = double(vertex_group_key(vt, u, g).size());
        o.G += 1;
        o.get_key += key;
        o.get_value += mean(t, 2);
        o.used_key += key;
        o.P += 1;
        o.write_key += key;
        o.write_value += mean(t, 2);
      }
    } else {   // a changed indexed column's old cell: 1 GET (a hit); each changed column PUT (exact)
      const std::array<double, 3>& t = part("vertex_props", label, "");
      for (auto& p : changed) {
        int c = m.attr_id(label, p);
        if (c < 0) throw std::runtime_error("predict: update_prop: '" + p + "' is not a column of " + label);
        if (!indexed(p)) continue;
        double key = double(vertex_attr_key(vt, u, uint8_t(c)).size());
        o.G += 1;
        o.get_key += key;
        o.get_value += mean(t, 2);
        o.used_key += key;
        o.used_value += mean(t, 2);
      }
      put_vertex_exact(o, label, u, new_cells, changed);
    }
    });
    // a changed indexed column's entry moves: the old one DELed (its mean key), the new one PUT (exact)
    const SortedIndex& ix = rules.sorted_index();
    for (auto& p : changed) {
      if (!indexed(p)) continue;
      charged_to(o, index_part(label, p), [&] {
        o.D += 1;
        o.write_key += index_key_mean(label, p);
        o.P += 1;
        o.write_key += double(ix.write_key(label, p, new_cells.get(p), u).size());
      });
    }
    return o;
  }

  // delete_vertex: the props by the layout, the index entries, then (with_edges) every incident list and the
  // neighbours' entries of us (the neighbours are endpoints of existing edges: size-biased)
  PCounts delete_vertex_est(const json& s) {
    const json& a = s.at("args");
    std::string label = a.at("label").get<std::string>();
    uint64_t u = StepPredictor::vid_arg(a, "vid");
    std::string uv = vid_hex(a, "vid");
    bool with_edges = !a.contains("with_edges") || a["with_edges"].is_null() || a["with_edges"].get<bool>();
    uint8_t vt = m.vtype_of(label);
    const std::vector<ColDecl>& cols = m.vcols(label);
    std::vector<std::string> icols = m.schema_.index_cols_of(label);
    PCounts o;
    const Component& pl = m.cfg_.props_of(label);
    const std::array<double, 3>& t = part("vertex_props", label, "");
    charged_to(o, "props:" + label, [&] {
    if (pl.packed()) {
      chunk_rmw(o, vertex_family(vt), "props:" + label, u, props_chunk_mean(label), m.cfg_.props_of(label).owners.pack, false, -props_row_bytes(label));
    } else if (pl.grouped()) {
      for (size_t g : m.groups_for(label, icols)) {   // the rows naming an index column read (hits)
        double key = double(vertex_group_key(vt, u, g).size());
        o.G += 1;
        o.get_key += key;
        o.get_value += mean(t, 2);
        o.used_key += key;
      }
      for (size_t g = 0; g < m.row_groups(label).cols.size(); g++) {
        o.D += 1;
        o.write_key += double(vertex_group_key(vt, u, g).size());
      }
    } else {
      for (auto& p : icols) {
        int c = m.attr_id(label, p);
        if (c < 0) continue;
        double key = double(vertex_attr_key(vt, u, uint8_t(c)).size());
        o.G += 1;
        o.get_key += key;
        o.get_value += mean(t, 2);
        o.used_key += key;
        o.used_value += mean(t, 2);
      }
      for (size_t c = 0; c < cols.size(); c++) {
        o.D += 1;
        o.write_key += double(vertex_attr_key(vt, u, attr_of_col(c)).size());
      }
    }
    });
    // the index entries (the count from the schema; an entry's key bytes: the type entry's exact, a property's mean)
    if (!m.schema_.prop_indexed(label))
      charged_to(o, index_part(label, ""), [&] {
        o.D += 1;
        o.write_key += double(IndexKeys::key(IndexKeys::prefix(m.schema_.type_index_decl(label).id), "", u).size());
      });
    for (auto& p : icols)
      charged_to(o, index_part(label, p), [&] {
        o.D += 1;
        o.write_key += index_key_mean(label, p);
      });
    if (!with_edges) return o;
    for (auto& [etype, out] : m.schema_.incident_etypes(label)) {
      if (!out && !m.reverse_available(etype)) continue;
      EdgeWInfo e = edge_info(etype);
      refuse_own_ep(e);
      Deg g = degree(uv, label, etype, out, false);
      double d = g.d;
      const Component& ours = m.cfg_.adj_of(etype, out);
      double key = double(adj_prefix(e.eid, out, u).size());
      // our side: read, then emptied (charged to our list's part)
      if (ours.packed() && u < first_id(etype + (out ? ":out" : ":in"))) {   // below the first chunk: a miss, no list
        charged_to_adj(o, etype, out, label, [&] {
          o.S += 1;
          o.S_miss += 1;
          o.seek_miss_key += double(chunk_with_id(padj_prefix(e.eid, out), u).size());
        });
        continue;
      }
      charged_to_adj(o, etype, out, label, [&] {
      if (ours.packed()) {
        double c = adj_chunk_mean(etype, out);
        double ckey = double(chunk_with_id(padj_prefix(e.eid, out), u).size());
        o.S += 1;
        o.scan_key += ckey;
        o.scan_value += c;
        o.P += g.p;
        o.write_key += g.p * ckey;
        o.write_value += g.p * std::max(0.0, c - d * entry_bytes(etype, out));
      } else if (ours.grouped()) {
        double L = list_bytes(ours, etype, out, d);
        o.S += 1;
        o.S_miss += 1 - g.p;
        o.seek_miss_key += (1 - g.p) * key;
        o.scan_key += g.p * key;
        o.scan_value += g.p * L;
        o.used_key += g.p * key;
        o.D += g.p;
        o.write_key += g.p * key;
      } else {   // the Split records: 1 SEEK, a NEXT per further record, the landing past the last a miss
        const std::array<double, 3>& ta = part("adjacency", etype, out ? "out" : "in");
        o.S += 1;
        o.S_miss += 1 - g.p;
        o.N += g.p * d;
        o.N_miss += g.p;
        o.scan_key += g.p * d * mean(ta, 1);
        o.scan_value += g.p * d * mean(ta, 2);
        o.seek_miss_key += (1 - g.p) * mean(ta, 1);
        o.next_miss_key += g.p * mean(ta, 1);
        o.D += g.p * d;
        o.write_key += g.p * d * mean(ta, 1);
      }
      });
      if (!e.rev || g.p <= 0) continue;
      // the neighbours' side: each neighbour's entry of us removed, by that side's layout (charged to the far end's
      // lists, split over its owner groups by charge_adj: the neighbours' labels are not known)
      const Component& theirs = m.cfg_.adj_of(etype, !out);
      double n = g.p * d;   // the expected neighbours
      charged_to_adj(o, etype, !out, "", [&] {
      if (theirs.packed()) {
        const std::array<double, 3>& tt = part("adjacency", etype, !out ? "out" : "in");
        double touched = g.p * balls(tt[0], d);
        double ckey = double(chunk_with_id(padj_prefix(e.eid, !out), u).size());
        o.S += touched;
        o.scan_key += touched * ckey;
        o.scan_value += touched * mean(tt, 2);
        o.P += touched;
        o.write_key += touched * ckey;
        o.write_value += touched * mean(tt, 2) - n * entry_bytes(etype, !out);
      } else if (theirs.grouped()) {
        Deg nb = degree("", "", etype, !out, true);   // a neighbour: an endpoint of an existing edge
        PCounts one;
        condensed_rmw(one, theirs, etype, !out, u, nb, -1);
        o.add(times(one, n));
      } else {
        const std::array<double, 3>& tt = part("adjacency", etype, !out ? "out" : "in");
        o.D += n;
        o.write_key += n * mean(tt, 1);
      }
      });
    }
    return o;
  }

  PCounts write_step_est(const json& s, const std::string& op) {
    if (op == "insert_edge") return insert_edge_est(s);
    if (op == "delete_edge") return delete_edge_est(s);
    if (op == "update_edge_prop") return update_edge_est(s);
    if (op == "insert_vertex") return insert_vertex_est(s);
    if (op == "update_prop") return update_prop_est(s);
    if (op == "delete_vertex") return delete_vertex_est(s);
    throw std::runtime_error("predict: op " + op + " is not covered");
  }

  PCounts step(const json& s) {
    const std::string op = s.at("op").get<std::string>();
    if (is_write_op(op)) return write_step_est(s, op);
    if (op == "read_vertex_by_id") {
      if (s.at("args").at("vid").is_string()) return PCounts();
      throw std::runtime_error("predict: read_vertex_by_id over a candidate list (not covered)");
    }
    if (op == "read_neighbors") return hop(s);
    const json& a = s.at("args");
    if (op == "read_vertex_props") {
      std::vector<std::string> labs = m.labels_or_throw("read_vertex_props", StepPredictor::strs(s, "label"));
      const Component& layout = m.props_layout("read_vertex_props", labs);
      // one label: a vertex carrying another label reads nothing (Meta::narrow), so n is cut to the vertices carrying
      // this label or none: by the step's label mix (the truth-card levels), else by the far-label share of the hop that made the set
      if (labs.size() == 1) return props(layout, labs[0], StepPredictor::strs(s, "props"), predict::need(in.n, "n") * read_share(labs[0]));
      // candidate labels: each vertex read under its own label (the label it carries), n split by the step's label mix
      // when known, else by the labels' counts
      PCounts out;
      std::map<std::string, double> per;
      double n = predict::need(in.n, "n"), tc = 0;
      if (in.label_mix) {
        for (auto& [k, c] : *in.label_mix)
          if (k != "?") per[label_of_id(k)] += c;
          else for (auto& l : labs) per[l] += 0;   // a vertex with no label carried: priced as the counts split it below
        double known = 0;
        for (auto& [l, c] : per) known += c;
        if (n > known) for (auto& l : labs) tc += type_count(l);
        if (n > known) for (auto& l : labs) per[l] += (n - known) * type_count(l) / tc;
      } else if (in.label_share) {
        for (auto& l : labs) {
          auto it = in.label_share->find(l);
          per[l] = n * (it == in.label_share->end() ? 0.0 : it->second);
        }
      } else {
        for (auto& l : labs) tc += type_count(l);
        for (auto& l : labs) per[l] = tc > 0 ? n * type_count(l) / tc : 0;
      }
      for (auto& [l, c] : per)
        if (std::find(labs.begin(), labs.end(), l) != labs.end()) out.add(props(layout, l, StepPredictor::strs(s, "props"), c));
      return out;
    }
    if (op == "read_anchor_props") {   // the first candidate holds the anchor: one vertex's props under the first label
      std::vector<std::string> labs = m.labels_or_throw("read_anchor_props", StepPredictor::strs(s, "labels"));
      return props(m.props_layout("read_anchor_props", labs), labs[0], StepPredictor::strs(s, "props"), 1);
    }
    if (op == "read_index_eq") {
      WalkCard w;
      walk(a.at("label").get<std::string>(), a.at("prop").get<std::string>(), predict::need(in.matches, "matches"), w);
      PCounts r = rules.walk_rule(w);
      charge(index_part(a.at("label").get<std::string>(), a.at("prop").get<std::string>()), r);
      return r;
    }
    if (op == "read_index_range") {   // one walk per label, the matches split by the labels' counts
      std::vector<std::string> labs = m.labels_or_throw("read_index_range", StepPredictor::strs(s, "labels"));
      double tc = 0;
      for (auto& l : labs) tc += type_count(l);
      WalkCard w;
      auto matches_of = [&](const std::string& l) {   // the label's share of the matches
        return term("index_matches", index_part(l, a.at("prop").get<std::string>()),
                    [&] { return tc > 0 ? predict::need(in.matches, "matches") * type_count(l) / tc : 0; });
      };
      for (auto& l : labs) walk(l, a.at("prop").get<std::string>(), matches_of(l), w);
      for (auto& l : labs) {   // each label's walk is its own index part
        WalkCard wl;
        walk(l, a.at("prop").get<std::string>(), matches_of(l), wl);
        charge(index_part(l, a.at("prop").get<std::string>()), rules.walk_rule(wl));
      }
      return rules.walk_rule(w);
    }
    if (op == "read_filter_by_label_index") {   // the label's membership walk, when some input carries no label
      std::vector<std::string> labs = StepPredictor::strs(s, "label");
      if (predict::need(in.walk, "walk") <= 0) return PCounts();
      WalkCard w;
      walk(labs.at(0), enumeration_prop(labs.at(0)), type_count(labs.at(0)), w);
      PCounts r = rules.walk_rule(w);
      charge(index_part(labs.at(0), enumeration_prop(labs.at(0))), r);
      return r;
    }
    if (op == "read_filter_by_label_probe") {   // one existence probe per input that carries no label
      std::vector<std::string> labs = StepPredictor::strs(s, "label");
      const Component& pl = m.cfg_.props_of(labs.at(0));
      std::string L = "props:" + labs.at(0);
      double n = predict::need(in.probes, "probes");
      if (n <= 0) return PCounts();
      double hit = n * term("label_hit_share", L, [&] { return share_of_label(labs.at(0)); });
      const std::array<double, 3>& t = part("vertex_props", labs.at(0), "");
      if (pl.packed()) {
        double touched = chunks_uniform(t[0], n, L);
        PCounts out;
        out.S += touched;
        out.scan_key += touched * term("chunk_key_mean", L, [&] { return mean(t, 1); });
        out.scan_value += touched * term("chunk_value_mean", L, [&] { return mean(t, 2); });
        charge("props:" + labs.at(0), out);
        return out;
      }
      StepCard c;
      if (!pl.grouped()) {
        ExistCard& k = c.exist[labs.at(0)];
        k.probes = n;
        k.hits = hit;
        double key = term("props_key_mean", L, [&] { return mean(t, 1); });
        k.hit_key_bytes = hit * key;
        k.miss_key_bytes = (n - hit) * key;
      } else {
        PropsCard& k = c.exist_gets[labs.at(0)];
        k.asked = n;
        k.hits = hit;
        k.value_bytes = hit * term("row_value_mean", L, [&] { return mean(t, 2); });
      }
      PCounts r = rules.exist_rule(c);
      charge("props:" + labs.at(0), r);
      return r;
    }
    if (op == "read_vertices_by_label") {
      std::string lab = a.at("label").get<std::string>();
      if (!a.contains("props") || a["props"].is_null()) {   // the membership walk: every vertex of the label
        WalkCard w;
        walk(lab, enumeration_prop(lab), type_count(lab), w);
        PCounts r = rules.walk_rule(w);
        charge(index_part(lab, enumeration_prop(lab)), r);
        return r;
      }
      const std::array<double, 3>& t = part("vertex_props", lab, "");   // the scan: every key of the label's run
      ScanCard k;
      k.scans = 1;
      k.keys = t[0];
      k.key_bytes = t[1];
      k.value_bytes = t[2];
      k.end_key_bytes = mean(t, 1);
      PCounts r = rules.scan_rule(k);
      charge("props:" + lab, r);
      return r;
    }
    if (op == "read_closure" || op == "read_chase") {
      // a closure reads every vertex it reaches once (`visited`), each one list; a chase reads each chain node once
      // (`nodes`), the nodes with a next (`links`) find a list. Priced as a hop over those reads (a chase's Split read
      // is one SEEK with no NEXT: N is not charged)
      std::string etype = a.at("etype").get<std::string>();
      std::string dir = kv::dir_of(a);
      Meta::HopShape sh;
      sh.dirs = m.dirs_of(op, etype, dir);
      sh.two = false;
      sh.packed = m.cfg_.adj_of(etype, sh.dirs[0]).packed();
      double n = predict::need(in.nodes, "nodes"), links = predict::need(in.links, "links");
      uint8_t eid = uint8_t(m.schema_.edge(etype).id);
      PCounts out;
      StepCard c;
      c.dirs.assign(sh.dirs.size(), HopDirCard());
      for (size_t di = 0; di < sh.dirs.size(); di++) {
        bool d = sh.dirs[di];
        const Component& al = m.cfg_.adj_of(etype, d);
        const std::array<double, 3>& t = part("adjacency", etype, d ? "out" : "in");
        PCounts pd;   // this direction's part: its lanes priced here, then its card priced alone
        if (al.packed()) {
          std::string fam = etype + (d ? ":out" : ":in");
          double touched = chunks_uniform(t[0], n, fam);
          pd.S += touched;
          pd.scan_key += touched * term("chunk_key_mean", fam, [&] { return mean(t, 1); });
          pd.scan_value += touched * term("chunk_value_mean", fam, [&] { return mean(t, 2); });
          out.add(pd);
          charge_adj(etype, d, pd);
          continue;
        }
        HopDirCard& k = c.dirs[di];
        std::string P = adj_name(etype, d);
        k.probes = n;
        k.present = std::min(n, links);
        double per_list = al.grouped() ? term("list_entries_mean", P, [&] {
          return stats.edge_total(etype, "a hop's list size") / std::max(1.0, t[0]); }) : 0.0;
        k.entries = op == "read_chase" && !al.grouped() ? 0 : k.present * (al.grouped() ? per_list : 1.0);
        k.value_bytes = al.grouped() ? k.present * term("list_value_mean", P, [&] { return mean(t, 2); }) : 0;
        double key = al.grouped() ? double(adj_prefix(eid, d, 0).size()) : term("adj_key_mean", P, [&] { return mean(t, 1); });
        k.absent_key_bytes = (n - k.present) * key;
        k.end_key_bytes = al.grouped() || op == "read_chase" ? 0 : k.present * key;
        if (op == "read_chase" && !al.grouped()) {   // Split chase: the landing on the first record is a hit, no NEXT
          pd.S += k.probes;
          pd.S_miss += k.probes - k.present;
          pd.scan_key += k.present * key;
          pd.seek_miss_key += k.absent_key_bytes;
          pd.used_key += k.present * key;
          k = HopDirCard();
        }
        out.add(pd);
        pd.add(rules.hop_rule(one_dir(sh, d), etype, one_card(c, k)));
        charge_adj(etype, d, pd);
      }
      out.add(rules.hop_rule(sh, etype, c));
      return out;
    }
    if (op == "read_top_k_by_index") {
      if (m.index_->date_granularity() != DateGranularity::full)
        throw std::runtime_error("predict: a top-k over a day index (the resolver's reads are not covered)");
      std::vector<std::string> labs = StepPredictor::strs(s, "labels");
      const Component& layout = m.props_layout("read_top_k_by_index", labs);
      PCounts out;
      // the walk: one SEEK per label, `crossed` index entries (the cardinality layer's), the landing past each
      // label's walk a miss; an entry's key = the labels' mean index key
      double ikeys = 0, ibytes = 0, total = 0;
      for (auto& l : labs) {
        const std::array<double, 3>& t = part("index", l, "", s.at("args").at("prop").get<std::string>());
        ikeys += t[0]; ibytes += t[1]; total += type_count(l);
      }
      std::string P = "index:";
      for (size_t i = 0; i < labs.size(); i++) P += (i ? "+" : "") + labs[i];
      P += "." + s.at("args").at("prop").get<std::string>();
      double ikey = term("index_key_mean", P, [&] { return ikeys > 0 ? ibytes / ikeys : 0; }),
             crossed = predict::need(in.crossed, "crossed");
      out.S += double(labs.size());
      out.N += crossed;
      out.N_miss += double(labs.size());
      out.scan_key += crossed * ikey;
      out.next_miss_key += double(labs.size()) * ikey;
      out.used_key += crossed * ikey;
      for (auto& l : labs) {   // each label's walk its own index part: its SEEK and end landing, the crossed by its count
        double sh = total > 0 ? type_count(l) / total : 1.0 / double(labs.size());
        PCounts pw;
        pw.S = 1;
        pw.N = crossed * sh;
        pw.N_miss = 1;
        pw.scan_key = crossed * sh * ikey;
        pw.next_miss_key = ikey;
        pw.used_key = crossed * sh * ikey;
        charge(index_part(l, s.at("args").at("prop").get<std::string>()), pw);
      }
      double w = predict::need(in.winners, "winners");                 // the winners, split over the labels by their counts
      for (auto& l : labs)
        out.add(props(layout, l, StepPredictor::strs(s, "props"), total > 0 ? w * type_count(l) / total : 0));
      return out;
    }
    if (op == "read_edge_exists") return edge_exists(s);
    throw std::runtime_error("predict: op " + op + " is not covered");
  }
};

json Meta::predict_step_estimate(const json& step, const predict::EstimateInput& in, const predict::DatasetStats& stats,
                                 const json& tally) const {
  Estimate e(*this, stats, tally, in);
  PCounts p = e.step(step);
  auto lanes = [](const PCounts& p) -> json {
    return {{"S", p.S}, {"G", p.G}, {"N", p.N}, {"S_miss", p.S_miss}, {"G_miss", p.G_miss}, {"N_miss", p.N_miss},
            {"get_key", p.get_key}, {"get_value", p.get_value}, {"scan_key", p.scan_key}, {"scan_value", p.scan_value},
            {"seek_miss_key", p.seek_miss_key}, {"next_miss_key", p.next_miss_key}, {"get_miss_key", p.get_miss_key},
            {"used_key", p.used_key}, {"used_value", p.used_value}, {"P", p.P}, {"D", p.D},
            {"write_key", p.write_key}, {"write_value", p.write_value},
            // the last-chunk lane, under its capture columns (inputs/contract/capture_columns.json "last_chunk")
            {"last_chunk_reads", p.lc_reads}, {"last_chunk_read_bytes", p.lc_read_bytes},
            {"last_chunk_read_target_bytes", p.lc_read_target}, {"last_chunk_writes", p.lc_writes},
            {"last_chunk_write_bytes", p.lc_write_bytes}, {"last_chunk_write_base_bytes", p.lc_write_base},
            {"last_chunk_write_target_bytes", p.lc_write_target}};
  };
  json out = lanes(p);
  out["chunks"] = e.chunks;   // the Chunked chunks the reads touch (the closed form)
  out["chunk_families"] = double(e.chunk_families.size());
  // the terms its formulas read ([name@part, value, from the truth card]), and how many came from the statistics although
  // the input carried true terms (the truth-card levels: a term no item of the step filled)
  json terms = json::array();
  double from_stats = 0;
  for (auto& [k, v] : e.reads) {
    terms.push_back({k, v.first, v.second});
    if (in.level && !v.second && predict::from_card(*in.level, predict::term_def(k.substr(0, k.find('@'))).kind)) from_stats++;
  }
  out["terms"] = terms;
  out["terms_from_statistics"] = from_stats;
  // the step's lanes per store part, first charged first ([name, lanes]): a read's parts, a write's (the part each of
  // its PUTs, DELs and own reads lands on)
  json parts = json::array();
  for (auto& [name, c] : e.charged) parts.push_back({name, lanes(c)});
  out["parts"] = parts;
  return out;
}

const std::vector<std::string>& Meta::predict_rules() {
  static const std::vector<std::string> rules = {
      "anchor_props.candidates",
      "by_id.candidates",
      "chase.chunked_probe",
      "chase.condensed_list",
      "chase.split_absent",
      "chase.split_first",
      "chunk_write.ep_ordinal",
      "chunk_write.first_id_moves",
      "chunk_write.new_chunk",
      "chunk_write.rmw",
      "chunked.probe_before_first_chunk",
      "chunked.probe_charged",
      "chunked.probe_free",
      "closure.level",
      "condensed_hop.edge_props_asked",
      "condensed_hop.list_absent",
      "condensed_hop.list_split",
      "condensed_hop.list_whole",
      "condensed_write.list_absent",
      "condensed_write.list_emptied",
      "condensed_write.list_rmw",
      "condensed_write.list_split_part",
      "condensed_write.list_whole",
      "delete_edge.own_placement",
      "delete_vertex.incident_side",
      "delete_vertex.list_further_part",
      "delete_vertex.mixed_ours_chunked",
      "delete_vertex.mixed_ours_condensed",
      "delete_vertex.mixed_ours_split",
      "delete_vertex.mixed_theirs_chunked",
      "delete_vertex.mixed_theirs_condensed",
      "delete_vertex.mixed_theirs_split",
      "delete_vertex.self_loop",
      "delete_vertex.split_records",
      "edge_check.anchor_lists",
      "edge_check.candidate_lists",
      "edge_check.split_get_hit",
      "edge_check.split_get_miss",
      "edge_check.via_walk",
      "exist_condensed.hit",
      "exist_condensed.miss",
      "exist_split.hit",
      "exist_split.miss",
      "hop.direction_dropped_by_owner_label",
      "hop.two_pass",
      "index_walk.empty",
      "index_walk.match",
      "index_write.delete_entry",
      "index_write.insert_entry",
      "index_write.type_entry",
      "index_write.update_moves",
      "insert_edge.own_placement",
      "label_scan.chunked",
      "label_scan.condensed",
      "label_scan.split",
      "props.candidate_labels",
      "props_condensed.get_hit",
      "props_condensed.get_miss",
      "props_split.get_hit",
      "props_split.get_miss",
      "props_write.condensed_row",
      "props_write.split_cell",
      "props_write.split_old_indexed",
      "split_hop.edge_props_asked",
      "split_hop.list_absent",
      "split_hop.list_present",
      "split_write.edge_get",
      "split_write.edge_record",
      "topk.walk_from_index_parts",
      "topk.winners",
      "update_edge.mixed_in_chunked",
      "update_edge.mixed_in_condensed",
      "update_edge.mixed_in_split",
      "update_edge.mixed_out_chunked",
      "update_edge.mixed_out_condensed",
      "update_edge.mixed_out_split",
      "update_edge.own_placement"};
  return rules;
}

const std::vector<std::string>& Meta::predict_plants() {
  static const std::vector<std::string> plants = {"split_next_per_entry", "condensed_list_key", "props_get_per_vertex",
                                                  "chunked_batch", "split_exists_value", "index_walk_end_hit",
                                                  "edge_write_no_reverse", "condensed_write_one_seek",
                                                  "chunk_write_no_first_id_del", "chunk_write_no_ordinal_seek",
                                                  "index_write_none", "index_write_no_old_del",
                                                  "index_write_one_short", "write_replay_off"};
  return plants;
}

Counts Meta::predict_step_truth(const json& step, Store& store, const std::string& plant, json* coverage,
                                std::set<std::string>* plants_asked, WriteReplay* replay,
                                std::map<std::string, double>* terms_out) const {
  if (!plant.empty() && std::find(predict_plants().begin(), predict_plants().end(), plant) == predict_plants().end())
    throw std::runtime_error("predict: no rule named '" + plant + "' to plant");
  StepPredictor p(*this, &store, plant);
  p.replay = replay;
  p.coverage = coverage;
  p.asked = plants_asked;
  p.terms = terms_out;
  return p.step(step).whole();
}

}  // namespace evalcpp::meta
