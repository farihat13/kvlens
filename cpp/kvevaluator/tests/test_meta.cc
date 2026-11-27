// meta reproduction gate. meta has its OWN consistent key ('v'·vtype·vid·[attr],
// 'e'·owner·dir·etype·[dst]) -- not a reference's arbitrary spelling -- so it is validated on the
// FAIR CURRENCY, not exact key bytes: for each engine-shaped corner spec (inputs/specs/corners/, the retired
// presets' full specs) it must match its reference on per-step
// S/G/N (the access pattern, set by co-location) + the VALUE-byte channels (get_value/scan_value)
// + the output rows. Key bytes are meta's own (the normalized band). Run across the three
// structurally different points (dgraph singleton / nebula grouped+per-edge / dgneb composition)
// plus an INTERIOR point no engine occupies.
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <map>
#include <set>
#include <sstream>
#include <string>
#include <tuple>
#include <unistd.h>
#include <vector>

#include "exec/label_tags.h"
#include "cli/session.h"
#include "formats/format.h"
#include "formats/meta/config.h"
#include "meta_corners.h"
#include "formats/meta/format.h"
#include "ingest/ingest.h"
#include "store/rocksdb_store.h"
#include "schema/schema.h"
#include "provenance.h"
#include "util/vid.h"
#include "value/value.h"
#include "repo_paths.h"
#include "harness.h"

namespace {

using namespace evalcpp;
using evalcpp::test::meta_corner;

static uint64_t make_uid(const Schema& s, const std::string& label, uint64_t id) {
  return (uint64_t(s.tag_of_label(label)) << kVidShift) | id;
}

static std::string ingest_store(const Schema& s, const std::string& schema_path, const std::string& records,
                                const std::string& fmt, json cfg) {
  std::string t = "/tmp/evalcpp-meta-XXXXXX";
  std::vector<char> b(t.begin(), t.end()); b.push_back('\0');
  std::string dir = kvt::temp_dir(b.data());
  // the decided codec, as a store named with no codec gets it (store_config.json's rocksdb.compression)
  IngestOptions o; o.format = fmt; o.codec_config = cfg;
  o.targets = {{dir + "/store", rocksdb_setting<std::string>("compression")}};
  std::istringstream in(records); ingest(in, s, schema_path, o);
  return dir;
}

// a small Person + knows(creationDate) graph as a neutral record stream
static std::string graph(const Schema& s) {
  auto person = [&](int i) { return hex16(make_uid(s, "Person", uint64_t(i))); };
  std::string r;
  for (int i = 1; i <= 5; i++)
    r += "[\"v\",\"" + person(i) + "\",\"Person\",{\"id\":" + std::to_string(i) +
         ",\"firstName\":\"Bo\",\"lastName\":\"L" + std::to_string(i) +
         "\",\"creationDate\":" + std::to_string(1000 * i) + "}]\n";
  for (auto& [a, b] : std::vector<std::pair<int, int>>{{1, 2}, {1, 3}, {1, 4}, {2, 4}, {3, 5}, {4, 5}})
    r += "[\"e\",\"knows\",\"" + person(a) + "\",\"" + person(b) + "\",{\"creationDate\":" +
         std::to_string(100 * b) + "},\"Person\",\"Person\"]\n";
  return r;
}

// graph() + two Persons with larger ids (50, 100) and their edges. An owner's first neighbour id in 8..127 is where
// meta's own chunk and pertype_csr's chunk differ (meta's codes a first id from the vid group's base, id << 4: 2 B
// from 8 on; pertype_csr's writes it as it is: 1 B up to 127). Here 50's first out-neighbour is 100 and 100's first
// in-neighbour is 50, so both directions' chunks differ and a test on this graph cannot pass on the wrong chunk
static std::string graph_wide(const Schema& s) {
  auto person = [&](int i) { return hex16(make_uid(s, "Person", uint64_t(i))); };
  std::string r;   // graph()'s records, with the two Persons before the edges (records group by label)
  for (int i : {1, 2, 3, 4, 5, 50, 100})
    r += "[\"v\",\"" + person(i) + "\",\"Person\",{\"id\":" + std::to_string(i) +
         ",\"firstName\":\"Bo\",\"lastName\":\"L" + std::to_string(i) +
         "\",\"creationDate\":" + std::to_string(1000 * i) + "}]\n";
  for (auto& [a, b] : std::vector<std::pair<int, int>>{{1, 2}, {1, 3}, {1, 4}, {2, 4}, {3, 5}, {4, 5}, {1, 50}, {50, 100}})
    r += "[\"e\",\"knows\",\"" + person(a) + "\",\"" + person(b) + "\",{\"creationDate\":" +
         std::to_string(100 * b) + "},\"Person\",\"Person\"]\n";
  return r;
}

// read_edge_exists names its reads (formats/common.h EdgeRead). The two orders the plans' default lowering writes
// (planir/lowering.py): lists with the in lists kept, and everything else
static json edge_read(const char* dir, const char* at, bool unless_found = false) {
  json r = {{"dir", dir}, {"at", at}};
  if (unless_found) r["unless_found"] = true;
  return r;
}
static const json kReadsBothLists = json::array({edge_read("out", "anchor"), edge_read("in", "anchor")});
static const json kReadsOutKeys = json::array({edge_read("out", "anchor"), edge_read("out", "cand", true)});

static json run(const std::string& dir, const std::string& fmt, const std::string& schema_path, json steps) {
  Session sess;   // local: a global Session holding RocksDB handles crashes at static teardown
  return sess.handle({{"store", {{"dir", dir + "/store"}, {"schema", schema_path}, {"format", fmt}}},
                      {"plan", {{"plan", "t"}, {"params", json::object()}, {"steps", steps}, {"output", "out"}}},
                      {"run", json::object()}});
}

// meta(corner spec F) vs the reference F on the fair currency: S/G/N + rows, and value bytes unless `values` is
// false -- meta-v4 encodes a part differently from the engine (codec/columns.h, codec/lists.h) where the step reads
// one, so there only the work and the answers must match (the bytes are T1's, checked against the tag's grid)
static void fair_equal(const char* tag, const json& rm, const json& rd, bool values = true) {
  CHECK(rm.contains("rows") && rd.contains("rows") && rm["rows"] == rd["rows"], std::string(tag) + ": rows differ");
  CHECK(rm["steps"].size() == rd["steps"].size(), std::string(tag) + ": step count differs");
  for (size_t i = 0; i < rm["steps"].size() && i < rd["steps"].size(); i++) {
    for (const char* c : {"S", "G", "N", "P", "D"})
      CHECK(rm["steps"][i][c] == rd["steps"][i][c],
            std::string(tag) + " step " + std::to_string(i) + " " + c + ": " + rm["steps"][i][c].dump() + " vs " + rd["steps"][i][c].dump());
    for (const char* vc : {"get_value", "scan_value"})   // value bytes; key bytes are meta's own band
      CHECK(!values || rm["steps"][i]["bytes"][vc] == rd["steps"][i]["bytes"][vc],
            std::string(tag) + " step " + std::to_string(i) + " " + vc + " differs");
  }
}

// At the All-Condensed corner meta's reference is dgneb, retired: its answers and work are read from a
// saved copy, tests/fixtures/meta_corner_dgneb_reference.json (written by dgneb itself while it was built, one entry
// per comparison: rows; per step S G N P D and the value bytes, all that fair_equal and the rows-only checks read),
// keyed "<check>/<what>". It is a regression copy, not an independent reference: a correct change to meta's counts at
// this corner updates the entry from meta's own output, with the reason in the commit. Every
// other reference runs live.
static const char* kRefGolden = FIXTURE_DIR "/meta_corner_dgneb_reference.json";
static const char* kSavedRef = "dgneb";     // the reference read from the saved copy, not run
static json g_golden;                       // the saved copy's "calls"
static std::set<std::string> g_used;        // the keys this run compared, each once

static json ref_run(const std::string& key, const std::string& dir, const std::string& ref,
                    const std::string& schema_path, json steps) {
  if (ref != kSavedRef) return run(dir, ref, schema_path, steps);
  CHECK(g_used.insert(key).second, "golden: the key '" + key + "' is compared twice");
  CHECK(g_golden.contains(key), "golden: no saved entry '" + key + "' in " + kRefGolden);
  return g_golden.contains(key) ? g_golden[key] : json{{"error", "no saved entry"}};
}

// the reference's store: none for the saved reference
static std::string ref_store(const Schema& s, const std::string& schema_path, const std::string& records,
                             const std::string& ref, const json& cfg) {
  return ref == kSavedRef ? std::string() : ingest_store(s, schema_path, records, ref, cfg);
}

static void golden_begin() {
  std::ifstream f(kRefGolden);
  CHECK(f.good(), std::string("golden: cannot read ") + kRefGolden);
  if (f.good()) g_golden = json::parse(f)["calls"];
}

static void golden_end() {
  for (auto& [key, v] : g_golden.items())
    CHECK(g_used.count(key), "golden: the saved entry '" + key + "' was not compared (stale)");
  std::cerr << "[golden] " << g_used.size() << " comparisons checked against " << kRefGolden << "\n";
}

static void check_reads(const Schema& s, const std::string& schema_path, const std::string& corner, const std::string& ref) {
  std::string records = graph(s);
  std::string dm = ingest_store(s, schema_path, records, "meta", meta_corner(corner));
  std::string dr = ref_store(s, schema_path, records, ref, json::object());
  auto person = [&](int i) { return hex16(make_uid(s, "Person", uint64_t(i))); };
  auto cmp = [&](const char* what, json steps, bool values = true) {
    json rm = run(dm, "meta", schema_path, steps), rd = ref_run(std::string("reads/") + what, dr, ref, schema_path, steps);
    fair_equal((corner + std::string(" ") + what).c_str(), rm, rd, values);
    std::cerr << "[" << corner << "] " << what << ": rows=" << (rm.contains("rows") ? rm["rows"].size() : 0)
              << " S=" << rm["steps"][0]["S"] << " G=" << rm["steps"][0]["G"] << " N=" << rm["steps"][0]["N"] << "\n";
  };
  json L = json::array({"Person"});
  // every step reads a part meta-v4 encodes its own way (S1a: lists, per-edge columns; S1b: rows of columns, a
  // Split record = its cell's payload) -- the work and the answers only
  cmp("vertex by id", json::array({{{"name", "out"}, {"op", "read_vertex_by_id"}, {"vid", person(1)}, {"label", L}}}), false);
  // adjacency: meta-v4's list / per-edge encodings (rows 4, 6, 1a) -- the work and the answers only
  cmp("neighbors out", json::array({{{"name", "out"}, {"op", "read_neighbors"}, {"srcs", json::array({person(1)})}, {"etype", "knows"}, {"dir", "out"}}}), false);
  cmp("neighbors in", json::array({{{"name", "out"}, {"op", "read_neighbors"}, {"srcs", json::array({person(4)})}, {"etype", "knows"}, {"dir", "in"}}}), false);
  cmp("neighbors+facet", json::array({{{"name", "out"}, {"op", "read_neighbors"}, {"srcs", json::array({person(1)})}, {"etype", "knows"}, {"dir", "out"}, {"edge_props", json::array({"creationDate"})}, {"pairs", true}}}), false);
  cmp("vertex props", json::array({{{"name", "out"}, {"op", "read_vertex_props"}, {"vids", json::array({person(1), person(2), person(3)})}, {"props", json::array({"firstName", "lastName", "creationDate"})}, {"label", L}}}), false);
  {   // the chase: answers only -- meta-v4 row 1u reads each node once per op call and lands on a Split hop's first
      // record; the engines read per (source, hop) and scan, so the work differs by design
    json steps = json::array({{{"name", "out"}, {"op", "read_chase"}, {"srcs", json::array({person(1)})}, {"etype", "knows"}, {"dir", "out"}, {"max_hops", 3}}});
    json rm = run(dm, "meta", schema_path, steps), rd = ref_run("reads/chase", dr, ref, schema_path, steps);
    CHECK(rm.contains("rows") && rd.contains("rows") && rm["rows"] == rd["rows"], corner + " chase: rows differ");
    std::cerr << "[" << corner << "] chase (answers only): S " << rm["steps"][0]["S"] << " vs " << rd["steps"][0]["S"]
              << ", N " << rm["steps"][0]["N"] << " vs " << rd["steps"][0]["N"] << "\n";
  }
  cmp("anchor props", json::array({{{"name", "out"}, {"op", "read_anchor_props"}, {"cands", json::array({person(1)})}, {"props", json::array({"firstName", "lastName"})}, {"labels", L}}}), false);
  json rows_in = json::array({json{{"x", person(2)}}, json{{"x", person(5)}}});
  cmp("edge exists", json::array({{{"name", "out"}, {"op", "read_edge_exists"}, {"rows", rows_in}, {"field", "x"}, {"etype", "knows"}, {"into", "exists"}, {"others", json::array({person(1)})},
                                   {"reads", corner == "nebula" ? kReadsOutKeys : kReadsBothLists}}}), false);
}

// an INTERIOR point no engine occupies: dgraph's singleton props + nebula's per-edge adjacency.
// Validate by decomposition -- its prop cost == dgraph's, its adjacency cost == nebula's.
static void check_interior(const Schema& s, const std::string& schema_path) {
  std::string records = graph(s);
  std::string di = ingest_store(s, schema_path, records, "meta",
                                json{{"vertex_props", {{"group", "item"}}}, {"adjacency", {{"group", "item"}}}});
  std::string dg = ingest_store(s, schema_path, records, "dgraph", json::object());
  std::string nb = ingest_store(s, schema_path, records, "nebula", json::object());
  auto person = [&](int i) { return hex16(make_uid(s, "Person", uint64_t(i))); };
  json L = json::array({"Person"});
  json vprops = json::array({{{"name", "out"}, {"op", "read_vertex_props"}, {"vids", json::array({person(1), person(2)})}, {"props", json::array({"firstName", "lastName"})}, {"label", L}}});
  json nbrs = json::array({{{"name", "out"}, {"op", "read_neighbors"}, {"srcs", json::array({person(1)})}, {"etype", "knows"}, {"dir", "out"}, {"edge_props", json::array({"creationDate"})}, {"pairs", true}}});
  fair_equal("interior props==dgraph", run(di, "meta", schema_path, vprops), run(dg, "dgraph", schema_path, vprops), false);
  fair_equal("interior adjacency==nebula", run(di, "meta", schema_path, nbrs), run(nb, "nebula", schema_path, nbrs), false);
  json ri = run(di, "meta", schema_path, nbrs);
  std::cerr << "[interior] dgraph-props + nebula-adjacency: props match dgraph, adjacency S="
            << ri["steps"][0]["S"] << " N=" << ri["steps"][0]["N"] << "\n";
}

// Two-label group coverage. Post and Comment share one group but have DIFFERENT columns. meta keys by the
// EXACT label (vtype-first), so each is a direct GET decoded with its own columns. The reproduction gate's
// own graph is Person-only, so this reproduces nebula on a Post+Comment graph -- covering the per-label
// decode vtype-first must get right (a Post row read as Post cols, a Comment row as Comment cols).
static void check_two_label_group(const Schema& s, const std::string& schema_path) {
  auto post = [&](int i) { return hex16(make_uid(s, "Post", uint64_t(i))); };
  auto comment = [&](int i) { return hex16(make_uid(s, "Comment", uint64_t(i))); };
  std::string r;
  for (int i : {10, 11})   // all Post rows together, then all Comment rows (ingest groups by label)
    r += "[\"v\",\"" + post(i) + "\",\"Post\",{\"id\":" + std::to_string(i) + ",\"content\":\"post" +
         std::to_string(i) + "\",\"creationDate\":" + std::to_string(i) + ",\"length\":" + std::to_string(i * 2) + "}]\n";
  for (int i : {20, 21})
    r += "[\"v\",\"" + comment(i) + "\",\"Comment\",{\"id\":" + std::to_string(i) + ",\"content\":\"cmt" +
         std::to_string(i) + "\",\"creationDate\":" + std::to_string(i) + ",\"length\":" + std::to_string(i * 3) + "}]\n";
  std::string dm = ingest_store(s, schema_path, r, "meta", meta_corner("nebula"));
  std::string dn = ingest_store(s, schema_path, r, "nebula", json::object());
  auto vprops = [&](json vids, const char* lab) {
    return json::array({{{"name", "out"}, {"op", "read_vertex_props"}, {"vids", vids},
                         {"props", json::array({"content", "length"})}, {"label", json::array({lab})}}});
  };
  for (auto& [lab, vids] : std::vector<std::pair<const char*, json>>{
           {"Post", json::array({post(10), post(11)})},
           {"Comment", json::array({comment(20), comment(21)})}}) {
    json steps = vprops(vids, lab);
    json rm = run(dm, "meta", schema_path, steps), rn = run(dn, "nebula", schema_path, steps);
    fair_equal((std::string("two-label ") + lab).c_str(), rm, rn, false);
    std::cerr << "[two-label] " << lab << ": rows=" << rm["rows"].size() << " G=" << rm["steps"][0]["G"] << "\n";
  }
}

// Index reproduction: meta's index is the shared SortedIndex, so its index ops must reproduce nebula
// on the fair currency. Direct op calls (not plan-lowered) so caps don't select a variant -- the walk
// itself is what's checked. Person has a firstName value index, a creationDate sorted index, a type index.
static void check_index(const Schema& s, const std::string& schema_path) {
  std::string records = graph(s);
  std::string dm = ingest_store(s, schema_path, records, "meta", meta_corner("nebula"));
  std::string dn = ingest_store(s, schema_path, records, "nebula", json::object());
  auto cmp = [&](const char* what, json steps, bool values = true) {
    json rm = run(dm, "meta", schema_path, steps), rn = run(dn, "nebula", schema_path, steps);
    fair_equal((std::string("index ") + what).c_str(), rm, rn, values);
    std::cerr << "[index] " << what << ": rows=" << (rm.contains("rows") ? rm["rows"].size() : 0)
              << " S=" << rm["steps"][0]["S"] << " G=" << rm["steps"][0]["G"] << " N=" << rm["steps"][0]["N"] << "\n";
  };
  cmp("eq", json::array({{{"name", "out"}, {"op", "read_index_eq"}, {"label", "Person"}, {"prop", "firstName"}, {"value", "Bo"}}}));
  cmp("range", json::array({{{"name", "out"}, {"op", "read_index_range"}, {"labels", json::array({"Person"})}, {"prop", "creationDate"}, {"lo", 1000}, {"hi", 4000}}}));
  cmp("vertices_by_label", json::array({{{"name", "out"}, {"op", "read_vertices_by_label"}, {"label", "Person"}}}));
  cmp("top_k", json::array({{{"name", "out"}, {"op", "read_top_k_by_index"}, {"prop", "creationDate"}, {"k", 3}, {"labels", json::array({"Person"})}, {"props", json::array({"firstName"})}}}), false);
}

// Packed (chunked) vertex props: meta with owners:{pack} lays vertices into chunks via the neutral chunk
// codec and reads them by seek_for_prev + a per-op chunk memo + the neutral replay. It must reproduce
// pertype_csr (the packed reference) on read_vertex_props -- the same chunk value bytes and the same
// logical S/N (one seek per distinct chunk, the rest free because the memo/replay already read it).
static void check_packed_vertex(const Schema& s, const std::string& schema_path) {
  std::string records = graph(s);
  json vpacked = {{"vertex_props", {{"group", "owner"}, {"owners", {{"pack", 65536}}}}},
                  {"adjacency", {{"group", "owner"}}}};
  std::string dm = ingest_store(s, schema_path, records, "meta", vpacked);
  std::string dp = ingest_store(s, schema_path, records, "pertype_csr", json::object());
  auto person = [&](int i) { return hex16(make_uid(s, "Person", uint64_t(i))); };
  json steps = json::array({{{"name", "out"}, {"op", "read_vertex_props"},
                             {"vids", json::array({person(1), person(2), person(3)})},
                             {"props", json::array({"firstName", "lastName", "creationDate"})},
                             {"label", json::array({"Person"})}}});
  json rm = run(dm, "meta", schema_path, steps), rp = run(dp, "pertype_csr", schema_path, steps);
  fair_equal("packed vertex == pertype_csr", rm, rp);
  std::cerr << "[packed] vertex props vs pertype_csr: rows=" << (rm.contains("rows") ? rm["rows"].size() : 0)
            << " S=" << rm["steps"][0]["S"] << " N=" << rm["steps"][0]["N"]
            << " scan_value=" << rm["steps"][0]["bytes"]["scan_value"] << "\n";
}

// Packed (chunked) adjacency: meta(pertype_csr) packs edges into adjacency chunks + edge-prop chunks and
// reads them via seek_for_prev + the chunk memo + replay. It must reproduce pertype_csr on read_neighbors
// (with and without facets) -- the same rows and the same S/G/N; read_chase: the same answers (row 1u). The corner
// uses meta's own chunk (own_chunk list_ordinals, codec/lists.h), so the chunk bytes differ from pertype_csr's
// (codec/chunks.h) on every read of this graph: the corner check lists bytes as differing everywhere.
static void check_packed_adjacency(const Schema& s, const std::string& schema_path) {
  std::string records = graph_wide(s);
  json corner = meta_corner("pertype_csr");
  CHECK(meta::Config::from_json(corner).to_json()["adjacency"]["own_chunk"] == "list_ordinals",
        "the Per-type CSR corner's own chunk is list_ordinals");
  std::string dm = ingest_store(s, schema_path, records, "meta", corner);
  std::string dp = ingest_store(s, schema_path, records, "pertype_csr", json::object());
  auto person = [&](int i) { return hex16(make_uid(s, "Person", uint64_t(i))); };
  int bytes_differ = 0, compared = 0;
  auto cmp = [&](const char* what, json steps) {
    json rm = run(dm, "meta", schema_path, steps), rp = run(dp, "pertype_csr", schema_path, steps);
    fair_equal((std::string("packed adj ") + what).c_str(), rm, rp, false);   // rows and ops
    compared++;
    bytes_differ += rm["steps"][0]["bytes"]["scan_value"] != rp["steps"][0]["bytes"]["scan_value"];
    std::cerr << "[packed-adj] " << what << ": rows=" << (rm.contains("rows") ? rm["rows"].size() : 0)
              << " S=" << rm["steps"][0]["S"] << " N=" << rm["steps"][0]["N"]
              << " scan_value=" << rm["steps"][0]["bytes"]["scan_value"] << " vs " << rp["steps"][0]["bytes"]["scan_value"] << "\n";
  };
  json srcs = json::array({person(1), person(2), person(4), person(50)});
  cmp("neighbors out", json::array({{{"name", "out"}, {"op", "read_neighbors"}, {"srcs", srcs}, {"etype", "knows"}, {"dir", "out"}}}));
  cmp("neighbors both", json::array({{{"name", "out"}, {"op", "read_neighbors"}, {"srcs", srcs}, {"etype", "knows"}, {"dir", "both"}}}));
  cmp("neighbors+facet", json::array({{{"name", "out"}, {"op", "read_neighbors"}, {"srcs", srcs}, {"etype", "knows"}, {"dir", "out"}, {"edge_props", json::array({"creationDate"})}, {"pairs", true}}}));
  {   // the chase: answers only -- meta-v4 row 1u shares one chunk cache over the hops of an op call; pertype_csr
      // reads per (source, hop), so the S differs by design
    json steps = json::array({{{"name", "out"}, {"op", "read_chase"}, {"srcs", json::array({person(1)})}, {"etype", "knows"}, {"dir", "out"}, {"max_hops", 3}}});
    json rm = run(dm, "meta", schema_path, steps), rp = run(dp, "pertype_csr", schema_path, steps);
    CHECK(rm.contains("rows") && rp.contains("rows") && rm["rows"] == rp["rows"], "packed adj chase: rows differ");
    std::cerr << "[packed-adj] chase (answers only): S " << rm["steps"][0]["S"] << " vs " << rp["steps"][0]["S"] << "\n";
  }
  CHECK(compared == 3 && bytes_differ == 3,
        "meta's own chunk and pertype_csr's chunk must differ in bytes on ids >= 8: differed on " << bytes_differ << " of " << compared << " reads");
  std::cerr << "[packed-adj] corner == pertype_csr in rows and ops; chunk bytes differ on " << bytes_differ << " of "
            << compared << " reads\n";
}

// the format-definition language must round-trip (it is store identity): from_json(to_json()) == to_json().
static void check_spec_roundtrip() {
  for (auto& p : {"dgraph", "nebula", "dgneb", "pertype_csr"}) {
    json spec = meta::Config::from_json(meta_corner(p)).to_json();
    json back = meta::Config::from_json(spec).to_json();
    CHECK(spec == back, std::string("spec round-trip failed for ") + p);
    std::cerr << "[spec] " << p << ": " << spec.dump() << "\n";
  }
}

// validate(): the cells meta builds are accepted; every combination it does not build is REJECTED
// (before this, e.g. item+pack silently built the chunked layout). A spec table, not a re-derived rule.
static void check_validate() {
  auto accepted = [](const json& j) {
    try { meta::Config::from_json(j).validate(); return true; } catch (const std::exception&) { return false; }
  };
  json split_ = {{"group", "item"}}, cond = {{"group", "owner"}}, chunk = {{"group", "owner"}, {"owners", {{"pack", 65536}}}};
  json cond_adj = {{"group", "owner"}, {"split", 524288}};
  std::vector<std::pair<const char*, json>> ok = {
      {"corner dgraph", meta_corner("dgraph")}, {"corner nebula", meta_corner("nebula")},
      {"corner dgneb", meta_corner("dgneb")}, {"corner pertype_csr", meta_corner("pertype_csr")},
      {"interior item/item", {{"vertex_props", split_}, {"adjacency", split_}}},
      {"chunked, own placement (the pertype_csr corner)", meta_corner("pertype_csr")},
      {"chunked, inline placement (the default)", {{"adjacency", chunk}}},
      {"chunked, inline placement stated", {{"adjacency", {{"group", "owner"}, {"owners", {{"pack", 65536}}}, {"edge_props", "adjacency"}}}}},
      {"chunked, the chunk header stated", {{"adjacency", {{"group", "owner"}, {"owners", {{"pack", 65536}}}, {"chunk_header", "skip16"}}}}},
  };
  for (auto& vp : {split_, cond, chunk})   // the paper grid: 3 x 3, index fixed (item/exact), declared
    for (auto& adj : {split_, cond_adj, chunk})
      ok.push_back({"grid cell", {{"vertex_props", vp}, {"adjacency", adj}, {"direction", "declared"},
                                  {"index", {{"group", "item"}, {"term", "exact"}}}}});
  std::vector<std::pair<const char*, json>> rejected = {
      {"vertex_props item+pack (columnar)", {{"vertex_props", {{"group", "item"}, {"owners", {{"pack", 65536}}}}}}},
      {"vertex_props split", {{"vertex_props", {{"group", "owner"}, {"split", 4096}}}}},
      {"adjacency item+pack", {{"adjacency", {{"group", "item"}, {"owners", {{"pack", 65536}}}}}}},
      {"adjacency item+split", {{"adjacency", {{"group", "item"}, {"split", 4096}}}}},
      {"adjacency pack+split", {{"adjacency", {{"group", "owner"}, {"owners", {{"pack", 65536}}}, {"split", 4096}}}}},
      {"index item+pack", {{"index", {{"group", "item"}, {"owners", {{"pack", 65536}}}}}}},
      {"index item+day", {{"index", {{"group", "item"}, {"term", "day"}}}}},
      {"index pack+split", {{"index", {{"group", "owner"}, {"owners", {{"pack", 65536}}}, {"split", 4096}}}}},
      {"packed vertex + day index", meta_corner("pertype_csr", {{"index", {{"term", "day"}}}})},
      {"unknown top-level key", meta_corner("nebula", {{"adjacncy", split_}})},
      {"unknown component key", {{"adjacency", {{"group", "item"}, {"pak", 1}}}}},
      {"bad direction", {{"direction", "reverse"}}},
      {"own placement on Split adjacency (not built)", {{"adjacency", {{"group", "item"}, {"edge_props", "own"}}}}},
      {"own placement on Condensed adjacency (not built)", {{"adjacency", {{"group", "owner"}, {"edge_props", "own"}}}}},
      {"a bad placement", {{"adjacency", {{"group", "owner"}, {"owners", {{"pack", 65536}}}, {"edge_props", "elsewhere"}}}}},
      {"own_chunk plain (refused: one own chunk form)", {{"adjacency", {{"group", "owner"}, {"owners", {{"pack", 65536}}}, {"edge_props", "own"}, {"own_chunk", "plain"}}}}},
      {"own_chunk on inline placement", {{"adjacency", {{"group", "owner"}, {"owners", {{"pack", 65536}}}, {"own_chunk", "list_ordinals"}, {"far_label", false}}}}},
      {"chunk_header on Condensed adjacency (no lists chunk)", {{"adjacency", {{"group", "owner"}, {"chunk_header", "skip16"}}}}},
      {"an unknown chunk_header", {{"adjacency", {{"group", "owner"}, {"owners", {{"pack", 65536}}}, {"chunk_header", "u32_ends"}}}}},
      {"an unknown own_chunk", {{"adjacency", {{"group", "owner"}, {"owners", {{"pack", 65536}}}, {"edge_props", "own"}, {"own_chunk", "csr"}, {"far_label", false}}}}},
      {"the top-level edge_props (refused)", {{"edge_props", {{"in", "adjacency"}}}}},
      {"a placement on a vertex-props override", {{"vertex_props_by_label", {{"Tag", {{"group", "item"}, {"edge_props", "own"}}}}}}},
      {"pack 0", {{"adjacency", {{"group", "owner"}, {"owners", {{"pack", 0}}}}}}},
      {"split 0", {{"adjacency", {{"group", "owner"}, {"split", 0}}}}},
      {"shared index scope", {{"index", {{"scope", "shared"}}}}},
      {"negative split (would wrap)", {{"adjacency", {{"group", "owner"}, {"split", -1}}}}},
      {"float pack (would truncate)", {{"vertex_props", {{"group", "owner"}, {"owners", {{"pack", 1.5}}}}}}},
      {"non-object spec", json("nebula")},
      {"a preset (refused)", {{"preset", "nebula"}}},
      {"value (refused in meta-v4): row_timestamp", meta_corner("nebula", {{"value", {{"row_timestamp", 0}}}})},
      {"value (refused in meta-v4): the list codec", meta_corner("dgraph", {{"value", {{"edge_props", "schema"}}}})},
  };
  for (auto& [what, j] : ok) CHECK(accepted(j), std::string("validate rejected a built cell: ") + what + " " + j.dump());
  for (auto& [what, j] : rejected) CHECK(!accepted(j), std::string("validate accepted an unbuilt/invalid spec: ") + what);
  std::cerr << "[validate] " << ok.size() << " built specs accepted, " << rejected.size() << " unbuilt/invalid rejected\n";
}

// adjacency.split: a Condensed list over the target is written as parts (store/unit_value.h) and read with 1 SEEK +
// 1 NEXT per further part. A 16-byte target makes the tiny graph split (the LDBC sf0.1 gate never reads a list that
// large). meta-v4's list encoding differs from dgneb's posting, and parts are cut by ENCODED bytes, so the parts
// differ from dgneb's at the same target (row 1c, made a logical boundary in S4): the answers must match and each
// read must count 1 SEEK + 1 NEXT per part of its own lists.
static void check_split(const Schema& s, const std::string& schema_path) {
  std::string records = graph(s);
  json mcfg = meta_corner("dgneb", {{"adjacency", {{"group", "owner"}, {"split", 16}}},
               {"index", {{"group", "owner"}, {"split", 16}, {"term", "day"}}}});
  std::string dm = ingest_store(s, schema_path, records, "meta", mcfg);
  std::string dr = ref_store(s, schema_path, records, "dgneb", json{{"chunk_target", 16}});
  json man = json::parse(std::ifstream(dm + "/store/manifest.json"));
  uint64_t split_units = man["ingest_counts"].value("adjacency_split_units", uint64_t(0));
  CHECK(split_units > 0, "split: the 16-byte target split no posting (the test would prove nothing)");
  auto person = [&](int i) { return hex16(make_uid(s, "Person", uint64_t(i))); };
  auto cmp = [&](const char* what, json steps) {
    json rm = run(dm, "meta", schema_path, steps), rd = ref_run(std::string("split/") + what, dr, "dgneb", schema_path, steps);
    CHECK(rm.contains("rows") && rd.contains("rows") && rm["rows"] == rd["rows"], std::string("split ") + what + ": rows differ");
    std::cerr << "[split] " << what << ": rows=" << (rm.contains("rows") ? rm["rows"].size() : 0)
              << " S=" << rm["steps"][0]["S"] << " N=" << rm["steps"][0]["N"] << "\n";
  };
  json srcs = json::array({person(1), person(2), person(4)});
  cmp("neighbors out", json::array({{{"name", "out"}, {"op", "read_neighbors"}, {"srcs", srcs}, {"etype", "knows"}, {"dir", "out"}}}));
  cmp("neighbors+facet", json::array({{{"name", "out"}, {"op", "read_neighbors"}, {"srcs", srcs}, {"etype", "knows"}, {"dir", "out"}, {"edge_props", json::array({"creationDate"})}, {"pairs", true}}}));
  cmp("chase", json::array({{{"name", "out"}, {"op", "read_chase"}, {"srcs", json::array({person(1)})}, {"etype", "knows"}, {"dir", "out"}, {"max_hops", 3}}}));
  json rows_in = json::array({json{{"x", person(2)}}, json{{"x", person(5)}}});
  cmp("edge exists", json::array({{{"name", "out"}, {"op", "read_edge_exists"}, {"rows", rows_in}, {"field", "x"}, {"etype", "knows"}, {"into", "exists"}, {"others", json::array({person(1)})},
                                   {"reads", kReadsBothLists}}}));
  std::cerr << "[split] " << split_units << " postings split into " << man["ingest_counts"]["adjacency_parts"] << " parts\n";
}

// Writes on Split (singleton) props + the sorted_keys index (the grid's index): an update must move ONLY the
// changed column's index entry. Regression check: the update must not re-write
// every index column from an old row holding only the changed prop, planting a bogus empty-token entry -- the
// type enumeration then listed the vertex twice.
static void check_singleton_update_index(const Schema& s, const std::string& schema_path) {
  std::string records = graph(s);
  std::string d = ingest_store(s, schema_path, records, "meta",
                               json{{"vertex_props", {{"group", "item"}}}, {"adjacency", {{"group", "item"}}}});
  auto person = [&](int i) { return hex16(make_uid(s, "Person", uint64_t(i))); };
  json L = json::array({"Person"});
  for (auto& [prop, value] : std::vector<std::pair<std::string, json>>{{"lastName", "Upd"}, {"firstName", "Zed"},
                                                                         {"creationDate", 99999}}) {
    json steps = json::array({
        {{"name", "w"}, {"op", "update_prop"}, {"vid", person(1)}, {"label", "Person"}, {"prop", prop}, {"value", value}},
        {{"name", "out"}, {"op", "read_vertices_by_label"}, {"label", "Person"}}});
    json r = run(d, "meta", schema_path, steps);
    std::set<std::string> seen;
    size_t n = r.contains("rows") ? r["rows"].size() : 0;
    for (auto& row : r["rows"]) seen.insert(row.dump());
    CHECK(n == 5 && seen.size() == 5, "singleton update of " + prop + ": the type enumeration has " +
                                          std::to_string(n) + " rows (" + std::to_string(seen.size()) + " distinct), want 5");
    std::cerr << "[singleton-update] " << prop << ": enumeration " << n << " rows, P=" << r["steps"][0]["P"]
              << " D=" << r["steps"][0]["D"] << "\n";
  }
  // the index follows the new value and forgets the old one
  json eq_new = json::array({
      {{"name", "w"}, {"op", "update_prop"}, {"vid", person(1)}, {"label", "Person"}, {"prop", "firstName"}, {"value", "Zed"}},
      {{"name", "out"}, {"op", "read_index_eq"}, {"label", "Person"}, {"prop", "firstName"}, {"value", "Zed"}}});
  json eq_old = json::array({
      {{"name", "w"}, {"op", "update_prop"}, {"vid", person(1)}, {"label", "Person"}, {"prop", "firstName"}, {"value", "Zed"}},
      {{"name", "out"}, {"op", "read_index_eq"}, {"label", "Person"}, {"prop", "firstName"}, {"value", "Bo"}}});
  json rn = run(d, "meta", schema_path, eq_new), ro = run(d, "meta", schema_path, eq_old);
  CHECK(rn["rows"].size() == 1, "after firstName -> Zed, the index finds " + std::to_string(rn["rows"].size()) + " Zed, want 1");
  CHECK(ro["rows"].size() == 4, "after firstName -> Zed, the index still finds " + std::to_string(ro["rows"].size()) + " Bo, want 4");
}

// update_edge_prop and the multi-column update_prop: meta(corner) must do its reference's write work on
// the fair currency (ops + value bytes + the rows read after). update_edge_prop on every corner (nebula's
// per-edge RMW, dgraph / dgneb's posting RMW, pertype_csr's ordinal locate + edge-prop chunk RMW); the
// multi update on dgneb and pertype_csr (meta-nebula moves only the changed index entries, hand-written
// nebula re-writes every index column: controlled, as for the single-column update). An absent edge
// writes nothing anywhere.
static void check_update_ops(const Schema& s, const std::string& schema_path) {
  std::string records = graph_wide(s);   // ids >= 8: the Per-type CSR corner's bytes equal the format's only in its own chunk form
  auto person = [&](int i) { return hex16(make_uid(s, "Person", uint64_t(i))); };
  json upd_edge = {{"name", "w"}, {"op", "update_edge_prop"}, {"etype", "knows"}, {"src", person(1)}, {"dst", person(3)},
                   {"props", {{"creationDate", 777}}}, {"src_label", "Person"}, {"dst_label", "Person"}};
  json absent = {{"name", "w"}, {"op", "update_edge_prop"}, {"etype", "knows"}, {"src", person(3)}, {"dst", person(1)},
                 {"props", {{"creationDate", 777}}}, {"src_label", "Person"}, {"dst_label", "Person"}};
  json multi = {{"name", "w"}, {"op", "update_prop"}, {"vid", person(2)}, {"label", "Person"},
                {"props", {{"firstName", "Zed"}, {"lastName", "Q"}, {"creationDate", 99999}}}};
  auto nbrs = [&](int who, const char* dir) {
    return json{{"name", "out"}, {"op", "read_neighbors"}, {"srcs", json::array({person(who)})}, {"etype", "knows"},
                {"dir", dir}, {"edge_props", json::array({"creationDate"})}, {"pairs", true}};
  };
  json by_new_name = {{"name", "t"}, {"op", "read_index_eq"}, {"label", "Person"}, {"prop", "firstName"}, {"value", "Zed"}};
  json props_of_t = {{"name", "out"}, {"op", "read_vertex_props"}, {"vids", {{"ref", "t"}}},
                     {"props", json::array({"firstName", "lastName", "creationDate"})}, {"label", json::array({"Person"})}};
  for (auto& [corner, ref] : std::vector<std::pair<std::string, std::string>>{
           {"dgraph", "dgraph"}, {"nebula", "nebula"}, {"dgneb", "dgneb"}, {"pertype_csr", "pertype_csr"}}) {
    std::string dm = ingest_store(s, schema_path, records, "meta", meta_corner(corner));
    std::string dr = ref_store(s, schema_path, records, ref, json::object());
    // no corner's adjacency bytes are its engine's: the chunked corner stores meta's own chunk (codec/lists.h)
    bool adj_values = false;
    auto cmp = [&](const std::string& what, json steps, bool values) {
      json rm = run(dm, "meta", schema_path, steps), rd = ref_run("update/" + what, dr, ref, schema_path, steps);
      fair_equal((corner + " " + what).c_str(), rm, rd, values);
      std::cerr << "[update-ops] " << corner << " " << what << ": S=" << rm["steps"][0]["S"] << " G=" << rm["steps"][0]["G"]
                << " P=" << rm["steps"][0]["P"] << " D=" << rm["steps"][0]["D"] << " rows=" << (rm.contains("rows") ? rm["rows"].size() : 0) << "\n";
      return rm;
    };
    json fwd = cmp("update_edge_prop, src side", json::array({upd_edge, nbrs(1, "out")}), adj_values);
    int hits = 0;
    for (auto& r : fwd["rows"]) hits += r["dst"] == person(3) && r["creationDate"] == 777;
    CHECK(hits == 1, corner + ": the forward copy carries the new date: " + fwd["rows"].dump().substr(0, 200));
    json rev = cmp("update_edge_prop, dst side", json::array({upd_edge, nbrs(3, "in")}), adj_values);
    CHECK(rev["rows"].size() == 1 && rev["rows"][0]["creationDate"] == 777,
          corner + ": the reverse copy carries it too: " + rev["rows"].dump().substr(0, 200));
    json ab = cmp("update_edge_prop, absent edge", json::array({absent, nbrs(3, "out")}), adj_values);
    CHECK(ab["steps"][0]["P"] == 0 && ab["steps"][0]["D"] == 0, corner + ": an absent edge writes nothing");
    if (corner == "dgneb" || corner == "pertype_csr") {
      json mu = cmp("multi update_prop", json::array({multi, by_new_name, props_of_t}), corner == "pertype_csr");
      CHECK(mu["rows"].size() == 1 && mu["rows"][0]["lastName"] == "Q" && mu["rows"][0]["creationDate"] == 99999,
            corner + ": every changed column reads back under the new token: " + mu["rows"].dump().substr(0, 200));
    } else {   // controlled on the counts: the rows must still agree
      json steps = json::array({multi, by_new_name, props_of_t});
      json rm = run(dm, "meta", schema_path, steps), rd = run(dr, ref, schema_path, steps);
      CHECK(rm.contains("rows") && rd.contains("rows") && rm["rows"] == rd["rows"] && rm["rows"].size() == 1,
            corner + " multi update_prop: rows differ: " + rm.dump().substr(0, 200) + " vs " + rd.dump().substr(0, 200));
    }
  }
}

// PER-TYPE layouts (config.h: vertex_props_by_label / adjacency_by_etype). The spec language round-trips
// with the maps and keeps the exact old shape without them; validate() checks every override; the Meta
// ctor refuses names the schema does not have and a vid group whose labels differ in props layout.
static void check_per_type_spec(const Schema& s) {
  json chunk = {{"group", "owner"}, {"owners", {{"pack", 65536}}}, {"split", "never"}};
  json mix = {{"vertex_props", {{"group", "owner"}}}, {"adjacency", {{"group", "item"}}},
              {"adjacency_by_etype", {{"knows", chunk}}},
              {"vertex_props_by_label", {{"Tag", {{"group", "item"}}}}}};
  json spec = meta::Config::from_json(mix).to_json();
  CHECK(spec == meta::Config::from_json(spec).to_json(), "per-type spec round-trip failed");
  json chunk_stated = chunk;
  chunk_stated["edge_props"] = "adjacency";   // to_json states every adjacency component's placement (the default here)
  chunk_stated["far_label"] = true;           // ... and whether its entries carry the far end's label (the default)
  chunk_stated["chunk_header"] = "skip16";    // ... and, on a lists chunk, its header form (config.h kChunkHeader)
  CHECK(spec.contains("adjacency_by_etype") && spec["adjacency_by_etype"]["knows"] == chunk_stated,
        "to_json lost the adjacency_by_etype override: " + spec.dump());
  CHECK(!meta::Config::from_json(meta_corner("nebula")).to_json().contains("adjacency_by_etype") &&
            !meta::Config::from_json(meta_corner("nebula")).to_json().contains("vertex_props_by_label"),
        "a uniform spec's to_json must keep its old shape (store identity)");
  meta::Config c = meta::Config::from_json(mix);
  CHECK(c.adj_of("knows", true).packed() && c.adj_of("knows", false).packed() && !c.adj_of("likes", true).packed() &&
            c.props_of("Person").grouped() && !c.props_of("Tag").grouped(),
        "props_of / adj_of do not return the override / the default");
  auto accepted = [](const json& j) {
    try { meta::Config::from_json(j).validate(); return true; } catch (const std::exception&) { return false; }
  };
  auto built = [&](const json& j) {
    try { meta::Meta m(s, meta::Config::from_json(j)); return true; } catch (const std::exception&) { return false; }
  };
  CHECK(accepted(mix) && built(mix), "a valid per-type spec was refused");
  std::vector<std::pair<const char*, json>> rejected = {
      {"override item+pack", {{"adjacency_by_etype", {{"knows", {{"group", "item"}, {"owners", {{"pack", 65536}}}}}}}}},
      {"override props split", {{"vertex_props_by_label", {{"Person", {{"group", "owner"}, {"split", 4096}}}}}}},
      {"override bad key", {{"adjacency_by_etype", {{"knows", {{"group", "item"}, {"pak", 1}}}}}}},
      {"map not an object", {{"adjacency_by_etype", json::array({"knows"})}}},
      {"packed override + day index", meta_corner("dgneb", {{"vertex_props_by_label", {{"Tag", chunk}}}})},
  };
  for (auto& [what, j] : rejected) CHECK(!accepted(j), std::string("validate accepted an invalid override: ") + what);
  std::vector<std::pair<const char*, json>> unbuilt = {
      {"unknown label", {{"vertex_props_by_label", {{"Persn", {{"group", "owner"}}}}}}},
      {"unknown etype", {{"adjacency_by_etype", {{"knws", {{"group", "item"}}}}}}},
      {"Post and Comment (one vid group) differ", {{"vertex_props_by_label", {{"Post", {{"group", "owner"}}}}}}},
  };
  for (auto& [what, j] : unbuilt) CHECK(accepted(j) && !built(j), std::string("the Meta ctor accepted: ") + what);
  std::cerr << "[per-type spec] round-trip + " << rejected.size() << " invalid overrides + " << unbuilt.size()
            << " schema-invalid specs refused\n";
}

// PER-DIRECTION adjacency (config.h adjacency_by_etype_dir): the lookup order (the direction's override, else the
// etype's, else the default), the round trip, the old shape without the map, and every refusal: a direction key
// that is not out / in, an etype with no direction, a half-own etype, an "in" where no in lists are kept.
static void check_per_direction_spec(const Schema& s) {
  json chunk = {{"group", "owner"}, {"owners", {{"pack", 65536}}}, {"split", "never"}};
  json list = {{"group", "owner"}}, split = {{"group", "item"}};
  json own = chunk;
  own["edge_props"] = "own";
  own["far_label"] = false;
  json mix = {{"direction", "both"}, {"adjacency", split}, {"adjacency_by_etype", {{"likes", list}}},
              {"adjacency_by_etype_dir", {{"knows", {{"out", chunk}, {"in", list}}}, {"likes", {{"in", chunk}}}}}};
  meta::Config c = meta::Config::from_json(mix);
  CHECK(c.adj_of("knows", true).packed() && c.adj_of("knows", false).grouped() && !c.adj_of("knows", false).packed(),
        "adj_of(knows) does not return each direction's override");
  CHECK(c.adj_of("likes", false).packed() && c.adj_of("likes", true).grouped() && !c.adj_of("likes", true).packed(),
        "a direction the map does not name must keep the etype's component (adjacency_by_etype)");
  CHECK(!c.adj_of("hasTag", true).grouped() && !c.adj_of("hasTag", false).grouped(),
        "an etype the maps do not name must keep the default");
  json spec = c.to_json();
  CHECK(spec == meta::Config::from_json(spec).to_json(), "per-direction spec round-trip failed");
  CHECK(spec.contains("adjacency_by_etype_dir") && spec["adjacency_by_etype_dir"]["knows"].size() == 2 &&
            spec["adjacency_by_etype_dir"]["likes"].size() == 1 && !spec["adjacency_by_etype_dir"]["likes"].contains("out"),
        "to_json must state the per-direction map as written: " + spec.dump());
  CHECK(!meta::Config::from_json(meta_corner("nebula")).to_json().contains("adjacency_by_etype_dir"),
        "a spec without the per-direction map must keep its old shape (store identity)");
  auto accepted = [](const json& j) {
    try { meta::Config::from_json(j).validate(); return true; } catch (const std::exception&) { return false; }
  };
  auto built = [&](const json& j) {
    try { meta::Meta m(s, meta::Config::from_json(j)); return true; } catch (const std::exception&) { return false; }
  };
  json same = {{"direction", "both"}, {"adjacency_by_etype_dir", {{"knows", {{"out", list}, {"in", list}}}}}};
  CHECK(accepted(same) && built(same), "a valid per-direction spec (both directions alike) was refused");
  std::vector<std::pair<const char*, json>> rejected = {
      {"a direction typo", {{"adjacency_by_etype_dir", {{"knows", {{"otu", list}}}}}}},
      {"no direction", {{"adjacency_by_etype_dir", {{"knows", json::object()}}}}},
      {"a component where the directions belong", {{"adjacency_by_etype_dir", {{"knows", list}}}}},
      {"map not an object", {{"adjacency_by_etype_dir", json::array({"knows"})}}},
      {"direction item+pack", {{"adjacency_by_etype_dir", {{"knows", {{"in", {{"group", "item"}, {"owners", {{"pack", 65536}}}}}}}}}}},
      {"direction own on a list", {{"adjacency_by_etype_dir", {{"knows", {{"out", {{"group", "owner"}, {"edge_props", "own"}}}}}}}}},
  };
  for (auto& [what, j] : rejected) CHECK(!accepted(j), std::string("validate accepted an invalid per-direction spec: ") + what);
  std::vector<std::pair<const char*, json>> unbuilt = {
      {"unknown etype", {{"direction", "both"}, {"adjacency_by_etype_dir", {{"knws", {{"out", list}}}}}}},
      {"half-own etype", {{"direction", "both"}, {"adjacency_by_etype_dir", {{"knows", {{"out", own}, {"in", chunk}}}}}}},
  };
  std::string no_reverse;   // an etype whose in lists a `declared` store does not keep
  for (auto& e : s.edges())
    if (!e.reverse && no_reverse.empty()) no_reverse = e.etype;
  if (no_reverse.empty()) { fails++; std::cerr << "FAIL: [per-direction spec] the test schema has no etype without @reverse (the in refusal is untested)\n"; }
  else unbuilt.push_back({"in on an etype with no in lists", {{"direction", "declared"}, {"adjacency_by_etype_dir", {{no_reverse, {{"in", list}}}}}}});
  for (auto& [what, j] : unbuilt) CHECK(accepted(j) && !built(j), std::string("the Meta ctor accepted: ") + what);
  std::cerr << "[per-direction spec] lookup order + round-trip + " << rejected.size() << " invalid specs + " << unbuilt.size()
            << " schema-invalid specs refused\n";
}

// A PER-DIRECTION store: knows' out lists in one layout, its in lists in another, for all 6 ordered pairs of
// Split / Condensed / Chunked. Each direction must read exactly as the uniform store of ITS layout does (rows,
// S / G / N, value bytes); a both-direction hop gives the out rows then the in rows per source, at the sum of the two
// uniform stores' counts; the edge-exists check and every edge write leave the same answers as the uniform Split
// store. Pinned too: an update of an ABSENT edge stops at the out side, and a chase over both directions of an etype
// with one direction Chunked is refused.
static void check_per_direction_store(const Schema& s, const std::string& schema_path) {
  auto person = [&](int i) { return hex16(make_uid(s, "Person", uint64_t(i))); };
  std::string r = graph(s);
  // a run whose answer has no steps (a refused or failed plan) is printed, with zero counts in place of none
  auto runx = [&](const std::string& dir, json steps) {
    json got = run(dir, "meta", schema_path, steps);
    if (!got.contains("steps") || got["steps"].empty() || !got["steps"][0].contains("S")) {
      std::cerr << "[per-direction] a run returned no counts: " << got.dump().substr(0, 400) << " for " << steps.dump().substr(0, 200) << "\n";
      got["steps"] = json::array({{{"S", 0}, {"G", 0}, {"N", 0}, {"P", 0}, {"D", 0}, {"bytes", json::object()}}});
      got["error"] = true;
    }
    return got;
  };
  std::map<std::string, json> layout = {{"split", {{"group", "item"}}}, {"condensed", {{"group", "owner"}}},
                                        {"chunked", {{"group", "owner"}, {"owners", {{"pack", 65536}}}}}};
  json base = {{"vertex_props", {{"group", "owner"}}}, {"adjacency", {{"group", "item"}}}, {"direction", "both"}};
  std::map<std::string, std::string> uniform;
  for (auto& [name, comp] : layout) {
    json j = base;
    j["adjacency_by_etype"] = {{"knows", comp}};
    uniform[name] = ingest_store(s, schema_path, r, "meta", j);
  }
  json srcs = json::array({person(1), person(2), person(4), person(5)});
  auto hop = [&](const char* dir) {
    return json::array({{{"name", "out"}, {"op", "read_neighbors"}, {"srcs", srcs}, {"etype", "knows"}, {"dir", dir},
                         {"edge_props", json::array({"creationDate"})}, {"pairs", true}}});
  };
  json rows_in = json::array({json{{"x", person(2)}}, json{{"x", person(5)}}, json{{"x", person(1)}}, json{{"x", person(3)}}});
  // the edge-exists check under every order of reads a plan may name (optimizer/rules.py EDGE_PROBE_READS), on TWO
  // anchors: a read runs over every anchor before the next read starts, and `unless_found` depends only on which
  // candidates the earlier reads found -- so the answer (the found set) is the same in every layout, for every order
  std::map<std::string, json> orders = {
      {"out@anchor + in@anchor", kReadsBothLists},
      {"out@anchor, out@cand", kReadsOutKeys},
      {"out@anchor, in@anchor", json::array({edge_read("out", "anchor"), edge_read("in", "anchor", true)})},
      {"in@anchor, in@cand", json::array({edge_read("in", "anchor"), edge_read("in", "cand", true)})},
      {"out@cand, in@cand", json::array({edge_read("out", "cand"), edge_read("in", "cand", true)})},
      {"in@anchor, out@anchor", json::array({edge_read("in", "anchor"), edge_read("out", "anchor", true)})},
      {"out@cand, out@anchor", json::array({edge_read("out", "cand"), edge_read("out", "anchor", true)})},
      {"in@cand, in@anchor", json::array({edge_read("in", "cand"), edge_read("in", "anchor", true)})},
      {"in@cand, out@cand", json::array({edge_read("in", "cand"), edge_read("out", "cand", true)})}};
  auto exists_with = [&](const json& reads) {
    return json::array({{{"name", "out"}, {"op", "read_edge_exists"}, {"rows", rows_in}, {"field", "x"}, {"etype", "knows"},
                         {"into", "exists"}, {"others", json::array({person(2), person(3)})}, {"reads", reads}}});   // 2, 3: not every candidate's neighbours
  };
  json truth = run(uniform["split"], "meta", schema_path, exists_with(kReadsOutKeys));
  {
    size_t yes = 0;
    for (auto& row : truth["rows"]) yes += row["exists"].get<bool>();
    CHECK(truth["rows"].size() == rows_in.size() && yes > 0 && yes < rows_in.size(),
          "the edge-exists test rows must hold both answers: " + truth.dump().substr(0, 300));
    for (auto& [lname, d] : uniform)
      for (auto& [oname, reads] : orders) {
        json got = run(d, "meta", schema_path, exists_with(reads));
        CHECK(got.contains("rows") && got["rows"] == truth["rows"],
              "edge-exists on uniform " + lname + " with reads [" + oname + "]: " + got.dump().substr(0, 300));
      }
  }
  json read_k = hop("both")[0];
  json ins = {{"name", "w"}, {"op", "insert_edge"}, {"etype", "knows"}, {"src", person(2)}, {"dst", person(5)},
              {"props", {{"creationDate", 777}}}, {"src_label", "Person"}, {"dst_label", "Person"}};
  json del = {{"name", "w"}, {"op", "delete_edge"}, {"etype", "knows"}, {"src", person(1)}, {"dst", person(3)}};
  json upd = {{"name", "w"}, {"op", "update_edge_prop"}, {"etype", "knows"}, {"src", person(1)}, {"dst", person(4)},
              {"props", {{"creationDate", 4242}}}, {"src_label", "Person"}};
  json upd_absent = upd;
  upd_absent["dst"] = person(5);   // 1 -> 5 does not exist (5's in list does: 3 -> 5, 4 -> 5)
  json del_v = {{"name", "w"}, {"op", "delete_vertex"}, {"vid", person(4)}, {"label", "Person"}, {"with_edges", true}};
  json loop = {{"name", "w"}, {"op", "insert_edge"}, {"etype", "knows"}, {"src", person(4)}, {"dst", person(4)},
               {"props", {{"creationDate", 44}}}, {"src_label", "Person"}, {"dst_label", "Person"}};
  size_t pairs = 0;
  for (auto& [xo, co] : layout)
    for (auto& [xi, ci] : layout) {
      if (xo == xi) continue;
      pairs++;
      std::string tag = "per-direction knows out " + xo + " / in " + xi;
      json j = base;
      j["adjacency_by_etype_dir"] = {{"knows", {{"out", co}, {"in", ci}}}};
      std::string dm = ingest_store(s, schema_path, r, "meta", j);
      // each direction alone == the uniform store of its layout
      fair_equal((tag + ": out").c_str(), runx(dm, hop("out")), runx(uniform[xo], hop("out")));
      fair_equal((tag + ": in").c_str(), runx(dm, hop("in")), runx(uniform[xi], hop("in")));
      // both directions: the rows in the uniform order, the counts the sum of the two directions' uniform counts
      json both = runx(dm, hop("both"));
      json uo = runx(uniform[xo], hop("out")), ui = runx(uniform[xi], hop("in"));
      json ref = runx(uniform["split"], hop("both"));
      CHECK(both.contains("rows") && both["rows"] == ref["rows"] && !both["rows"].empty(), tag + ": both-direction rows or their order differ");
      for (const char* c : {"S", "G", "N"})
        CHECK(both["steps"][0][c].get<int64_t>() == uo["steps"][0][c].get<int64_t>() + ui["steps"][0][c].get<int64_t>(),
              tag + ": both-direction " + c + " = " + both["steps"][0][c].dump() + ", the two directions' uniform counts sum to " +
                  std::to_string(uo["steps"][0][c].get<int64_t>() + ui["steps"][0][c].get<int64_t>()));
      // the edge-exists check: every order of reads finds the same candidates as every uniform store does; and each
      // read costs what the uniform store of ITS direction's layout charges for it (a read is one family's)
      for (auto& [oname, reads] : orders) {
        json ex = runx(dm, exists_with(reads));
        CHECK(ex.contains("rows") && ex["rows"] == truth["rows"], tag + ": edge-exists with reads [" + oname + "]: " + ex.dump().substr(0, 300));
      }
      for (auto& [dname, lay] : std::vector<std::pair<const char*, std::string>>{{"out", xo}, {"in", xi}})
        for (const char* at : {"anchor", "cand"}) {
          json one = exists_with(json::array({edge_read(dname, at)}));
          json got = runx(dm, one), want = runx(uniform[lay], one);
          for (const char* c : {"S", "G", "N"})
            CHECK(got["steps"][0][c] == want["steps"][0][c],
                  tag + ": one " + dname + "@" + at + " read costs " + c + " " + got["steps"][0][c].dump() + ", the uniform " + lay +
                      " store charges " + want["steps"][0][c].dump());
        }
      // writes, then the read that must see them
      for (auto& [what, w] : std::vector<std::pair<const char*, json>>{
               {"insert", ins}, {"delete", del}, {"update", upd}, {"update absent", upd_absent}, {"delete vertex", del_v}}) {
        json rm = runx(dm, json::array({w, read_k})), rs = runx(uniform["split"], json::array({w, read_k}));
        CHECK(rm.contains("rows") && rs.contains("rows") && rm["rows"] == rs["rows"],
              tag + " write " + what + ": rows differ: " + rm.dump().substr(0, 300) + " vs " + rs.dump().substr(0, 300));
      }
      {   // a self-loop, then its vertex deleted: no entry of it may survive on either side
        json rm = runx(dm, json::array({loop, del_v, read_k}));
        json rs = runx(uniform["split"], json::array({loop, del_v, read_k}));
        CHECK(rm.contains("rows") && rs.contains("rows") && rm["rows"] == rs["rows"], tag + " self-loop + delete vertex: rows differ");
      }
      // an absent edge: the out side decides. Its own read is the whole cost unless BOTH sides are lists
      // (uniform Condensed reads the in list too; not this store: its directions differ)
      json ab = runx(dm, json::array({upd_absent, read_k})), abo = runx(uniform[xo], json::array({upd_absent, read_k}));   // step 0 = the write
      int64_t s_ab = ab["steps"][0]["S"].get<int64_t>(), g_ab = ab["steps"][0]["G"].get<int64_t>();
      if (xo == "split") CHECK(s_ab == 0 && g_ab == 1, tag + ": update of an absent edge must cost the out GET alone, got S " + std::to_string(s_ab) + " G " + std::to_string(g_ab));
      else CHECK(g_ab == 0 && s_ab <= abo["steps"][0]["S"].get<int64_t>(),
                 tag + ": update of an absent edge must read the out side only, got S " + std::to_string(s_ab) + " G " + std::to_string(g_ab));
      // a chase over both directions: built when the two share a class, refused when one is Chunked
      json chase = json::array({{{"name", "out"}, {"op", "read_chase"}, {"srcs", json::array({person(1)})}, {"etype", "knows"}, {"dir", "both"}, {"max_hops", 3}}});
      bool threw = false;
      json rc;
      try { rc = run(dm, "meta", schema_path, chase); } catch (const std::exception&) { threw = true; }
      bool refused = threw || (rc.contains("error") && !rc["error"].is_null());
      bool one_chunked = (xo == "chunked") != (xi == "chunked");
      CHECK(refused == one_chunked, tag + ": a both-direction chase must be " + (one_chunked ? "refused" : "run") + ": " + rc.dump().substr(0, 200));
      std::cerr << "[" << tag << "] out / in == their uniform stores; both: rows=" << both["rows"].size() << " S=" << both["steps"][0]["S"]
                << " G=" << both["steps"][0]["G"] << " N=" << both["steps"][0]["N"] << "; edge-exists, 5 writes, self-loop, absent update, chase\n";
    }
  CHECK(pairs == 6, "the per-direction store test must cover the 6 ordered layout pairs");
}

// PER-DIRECTION, THE SAME KIND WITH DIFFERENT SIZES: an edge type whose two directions are both Chunked with different
// pack sizes, or both Condensed with different split sizes. check_per_direction_store covers the six pairs of
// different kinds only; here each direction must read as the uniform store of ITS component does (rows and counts),
// and every write must leave what the uniform Split store holds.
static void check_per_direction_sizes(const Schema& s, const std::string& schema_path) {
  auto person = [&](int i) { return hex16(make_uid(s, "Person", uint64_t(i))); };
  std::string r = graph(s);
  auto runx = [&](const std::string& dir, json steps) {
    json got = run(dir, "meta", schema_path, steps);
    if (!got.contains("steps") || got["steps"].empty() || !got["steps"].back().contains("S")) {
      std::cerr << "[per-direction sizes] a run returned no counts: " << got.dump().substr(0, 400) << "\n";
      fails++;
    }
    return got;
  };
  std::map<std::string, json> comp = {
      {"chunk-big", {{"group", "owner"}, {"owners", {{"pack", 65536}}}}}, {"chunk-tiny", {{"group", "owner"}, {"owners", {{"pack", 1}}}}},
      {"list-whole", {{"group", "owner"}}}, {"list-parts", {{"group", "owner"}, {"split", 1}}}, {"split", {{"group", "item"}}}};
  json base = {{"vertex_props", {{"group", "owner"}}}, {"adjacency", {{"group", "item"}}}, {"direction", "both"}};
  std::map<std::string, std::string> uniform;
  for (auto& [name, c] : comp) {
    json j = base;
    j["adjacency_by_etype"] = {{"knows", c}};
    uniform[name] = ingest_store(s, schema_path, r, "meta", j);
  }
  // the sizes must bite, or the test proves nothing: the tiny settings make more keys than the big ones
  auto hop = [&](const char* dir) {
    json srcs = json::array({person(1), person(2), person(3), person(4), person(5)});
    return json::array({{{"name", "out"}, {"op", "read_neighbors"}, {"srcs", srcs}, {"etype", "knows"}, {"dir", dir},
                         {"edge_props", json::array({"creationDate"})}, {"pairs", true}}});
  };
  auto ops = [&](const json& got) { return got["steps"].back()["S"].get<int64_t>() + got["steps"].back()["N"].get<int64_t>(); };
  CHECK(ops(runx(uniform["chunk-tiny"], hop("out"))) > ops(runx(uniform["chunk-big"], hop("out"))),
        "[per-direction sizes] pack 1 reads no more chunks than pack 65536: the sizes do not bite");
  CHECK(ops(runx(uniform["list-parts"], hop("out"))) > ops(runx(uniform["list-whole"], hop("out"))),
        "[per-direction sizes] split 1 reads no more parts than no split: the sizes do not bite");
  json read_k = hop("both")[0];
  json ins = {{"name", "w"}, {"op", "insert_edge"}, {"etype", "knows"}, {"src", person(2)}, {"dst", person(5)},
              {"props", {{"creationDate", 777}}}, {"src_label", "Person"}, {"dst_label", "Person"}};
  json del = {{"name", "w"}, {"op", "delete_edge"}, {"etype", "knows"}, {"src", person(1)}, {"dst", person(3)}};
  json upd = {{"name", "w"}, {"op", "update_edge_prop"}, {"etype", "knows"}, {"src", person(1)}, {"dst", person(4)},
              {"props", {{"creationDate", 4242}}}, {"src_label", "Person"}};
  json upd_absent = upd;
  upd_absent["dst"] = person(5);
  json del_v = {{"name", "w"}, {"op", "delete_vertex"}, {"vid", person(4)}, {"label", "Person"}, {"with_edges", true}};
  json loop = {{"name", "w"}, {"op", "insert_edge"}, {"etype", "knows"}, {"src", person(4)}, {"dst", person(4)},
               {"props", {{"creationDate", 44}}}, {"src_label", "Person"}, {"dst_label", "Person"}};
  size_t pairs = 0;
  for (auto& [xo, xi] : std::vector<std::pair<std::string, std::string>>{
           {"chunk-big", "chunk-tiny"}, {"chunk-tiny", "chunk-big"}, {"list-whole", "list-parts"}, {"list-parts", "list-whole"}}) {
    pairs++;
    std::string tag = "per-direction sizes: knows out " + xo + " / in " + xi;
    json j = base;
    j["adjacency_by_etype_dir"] = {{"knows", {{"out", comp[xo]}, {"in", comp[xi]}}}};
    std::string dm = ingest_store(s, schema_path, r, "meta", j);
    // each direction alone == the uniform store of its component: rows, counts and value bytes
    fair_equal((tag + ": out").c_str(), runx(dm, hop("out")), runx(uniform[xo], hop("out")));
    fair_equal((tag + ": in").c_str(), runx(dm, hop("in")), runx(uniform[xi], hop("in")));
    // writes, then both directions read: what the uniform Split store holds after the same write; and each
    // direction read alone after the write still costs what its component's uniform store costs after it
    std::vector<std::pair<const char*, json>> writes = {
        {"insert", json::array({ins})}, {"delete", json::array({del})}, {"update", json::array({upd})},
        {"update absent", json::array({upd_absent})}, {"delete vertex", json::array({del_v})},
        {"self-loop + delete vertex", json::array({loop, del_v})}};
    for (auto& [what, w] : writes) {
      json both = w, o = w, i = w;
      both.push_back(read_k); o.push_back(hop("out")[0]); i.push_back(hop("in")[0]);
      json rm = runx(dm, both), rs = runx(uniform["split"], both);
      CHECK(rm.contains("rows") && rs.contains("rows") && rm["rows"] == rs["rows"],
            tag + " write " + what + ": rows differ: " + rm["rows"].dump() + " vs " + rs["rows"].dump());
      for (auto& [dname, lay, plan] : std::vector<std::tuple<const char*, std::string, json>>{{"out", xo, o}, {"in", xi, i}}) {
        json got = runx(dm, plan), want = runx(uniform[lay], plan);
        CHECK(got.contains("rows") && want.contains("rows") && got["rows"] == want["rows"],
              tag + " write " + what + ", then " + dname + ": rows differ from the uniform " + lay + " store");
        for (const char* c : {"S", "G", "N"})
          CHECK(got["steps"].back()[c] == want["steps"].back()[c],
                tag + " write " + what + ", then " + dname + ": " + c + " " + got["steps"].back()[c].dump() + ", the uniform " +
                    lay + " store reads " + want["steps"].back()[c].dump());
      }
    }
    std::cerr << "[" << tag << "] out / in == their uniform stores; 6 writes: both directions == the uniform Split store, "
              << "each direction == its component's uniform store after the write (rows, S / G / N)\n";
  }
  CHECK(pairs == 4, "the per-direction sizes test must cover its 4 pairs");
}

// A WRITE THAT REWRITES ONE CHUNK TWICE AND MOVES OR EMPTIES IT (exec/chunk_write.h ChunkWrites, chunk_rmw). Found
// delete_vertex of a chunk's FIRST owner whose neighbours' lists sit in the same chunk left the old chunk
// behind (a put of the old key, then its delete applied before that put). Uniform Chunked-adjacency stores at the grid's pack, a small pack and pack 1,
// against the uniform Split store: after each write sequence a both-direction read of every person must return
// Split's rows (two chunks with overlapping owner ranges would make that read fail: the executor refuses them).
static void check_chunk_rekey_writes(const Schema& s, const std::string& schema_path) {
  auto person = [&](int i) { return hex16(make_uid(s, "Person", uint64_t(i))); };
  // persons 2..9 stored (1 is inserted by a write: an owner sorting before every chunk's first owner); 2 is the
  // stored first owner, with in-neighbours 5 and 9 and out-neighbours 3 and 4 in its own chunk
  std::string r;
  for (int i = 2; i <= 9; i++)
    r += "[\"v\",\"" + person(i) + "\",\"Person\",{\"id\":" + std::to_string(i) + ",\"firstName\":\"Bo\",\"lastName\":\"L" +
         std::to_string(i) + "\",\"creationDate\":" + std::to_string(1000 * i) + "}]\n";
  for (auto& [a, b] : std::vector<std::pair<int, int>>{{2, 3}, {2, 4}, {3, 4}, {3, 5}, {4, 5}, {6, 4}, {7, 4}, {4, 8}, {5, 2}, {9, 2}})
    r += "[\"e\",\"knows\",\"" + person(a) + "\",\"" + person(b) + "\",{\"creationDate\":" + std::to_string(100 * b) +
         "},\"Person\",\"Person\"]\n";
  json base = {{"vertex_props", {{"group", "owner"}}}, {"adjacency", {{"group", "item"}}}, {"direction", "both"}};
  // both edge-prop placements of a Chunked list: with the props inline in the adjacency chunk (no `edge_props` key:
  // what the grid's Chunked-adjacency cells and mixes use) and in their own ordinal-keyed chunks ("own": the adjacency
  // chunk AND the edge-prop chunk are rewritten in both direction passes of a delete)
  std::map<std::string, std::string> store;
  std::vector<std::string> chunked;
  store["split"] = ingest_store(s, schema_path, r, "meta", [&] { json j = base; j["adjacency_by_etype"] = {{"knows", {{"group", "item"}}}}; return j; }());
  for (const char* placement : {"inline", "own"})
    for (uint64_t pack : {65536, 64, 1}) {
      json comp = {{"group", "owner"}, {"owners", {{"pack", pack}}}};
      if (std::string(placement) == "own") comp["edge_props"] = "own";
      json j = base;
      j["adjacency_by_etype"] = {{"knows", comp}};
      std::string name = "pack " + std::to_string(pack) + (std::string(placement) == "own" ? " own" : "");
      store[name] = ingest_store(s, schema_path, r, "meta", j);
      chunked.push_back(name);
    }
  auto ins = [&](int a, int b) {
    return json{{"name", "i" + std::to_string(a) + std::to_string(b)}, {"op", "insert_edge"}, {"etype", "knows"}, {"src", person(a)},
                {"dst", person(b)}, {"props", {{"creationDate", 7}}}, {"src_label", "Person"}, {"dst_label", "Person"}};
  };
  auto insv = [&](int a) {
    return json{{"name", "iv" + std::to_string(a)}, {"op", "insert_vertex"}, {"vid", person(a)}, {"label", "Person"},
                {"props", {{"id", a}, {"firstName", "Bo"}, {"lastName", "N"}, {"creationDate", 5}}}};
  };
  auto dele = [&](int a, int b) {
    return json{{"name", "d" + std::to_string(a) + std::to_string(b)}, {"op", "delete_edge"}, {"etype", "knows"}, {"src", person(a)}, {"dst", person(b)}};
  };
  auto delv = [&](int a) {
    return json{{"name", "dv" + std::to_string(a)}, {"op", "delete_vertex"}, {"vid", person(a)}, {"label", "Person"}, {"with_edges", true}};
  };
  json all = json::array();
  for (int i = 1; i <= 9; i++) all.push_back(person(i));
  json read = {{"name", "out"}, {"op", "read_neighbors"}, {"srcs", all}, {"etype", "knows"}, {"dir", "both"}, {"pairs", true}};
  std::vector<std::pair<std::string, json>> seqs = {
      {"delete the stored first owner (its neighbours' lists in its chunk)", json::array({delv(2)})},
      {"delete a middle owner", json::array({delv(4)})},
      {"delete the last owner", json::array({delv(9)})},
      {"a new in entry of 4, then delete 4", json::array({ins(5, 4), delv(4)})},
      {"a self-loop on 4, then delete 4", json::array({ins(4, 4), delv(4)})},
      {"a self-loop on the first owner, then delete it", json::array({ins(2, 2), delv(2)})},
      {"insert vertex 1 (a new first owner) with edges both ways, then delete it", json::array({insv(1), ins(1, 3), ins(3, 1), delv(1)})},
      {"insert vertex 1 with five edges whose far ends share a chunk", json::array({insv(1), ins(1, 3), ins(1, 4), ins(1, 5), ins(3, 1), ins(4, 1)})},
      {"the first owner's whole out group deleted edge by edge", json::array({dele(2, 3), dele(2, 4)})},
      {"the first owner's whole in group deleted edge by edge", json::array({dele(5, 2), dele(9, 2)})},
      {"delete the first owner, then the next one", json::array({delv(2), delv(3)})}};
  size_t cells = 0;
  for (auto& [what, w] : seqs) {
    json plan = w;
    plan.push_back(read);
    json truth = run(store["split"], "meta", schema_path, plan);
    if (!truth.contains("rows") || truth["rows"].is_null()) {
      fails++; std::cerr << "FAIL: [chunk re-key] " << what << ": the Split store gave no rows: " << truth.dump().substr(0, 300) << "\n";
      continue;
    }
    for (auto& name : chunked) {
      json got = run(store[name], "meta", schema_path, plan);
      CHECK(got.contains("rows") && got["rows"] == truth["rows"],
            "[chunk re-key] " + name + ", " + what + ": " + (got.contains("error") && !got["error"].is_null()
                ? got["error"].dump() : "rows differ from Split: " + got["rows"].dump().substr(0, 400)));
      cells++;
    }
  }
  CHECK(cells == seqs.size() * 6, "[chunk re-key] " + std::to_string(cells) + " cells checked of " + std::to_string(seqs.size() * 6));
  // the delete of the stored first owner at the grid's pack (refused before the fix): its counts, pinned. 10 SEEKs (8
  // of them misses: the Person's other edge-type families hold no chunk; 2 hits: the knows out and in chunks), 4 chunk
  // puts (each of the two knows chunks rewritten in both direction passes) and 5 deletes: the vertex's row and its 2
  // index entries -- the 3 a middle owner's delete also does -- plus the old keys of the two knows chunks, which
  // moved to their new first owner (the late deletes)
  json first = run(store["pack 65536"], "meta", schema_path, json::array({delv(2), read}));
  json mid = run(store["pack 65536"], "meta", schema_path, json::array({delv(4), read}));
  const json& f0 = first["steps"][0];
  CHECK(f0["S"] == 10 && f0["S_miss"] == 8 && f0["P"] == 4 && f0["D"] == 5,
        "[chunk re-key] delete of the first owner at pack 65536: S " + f0["S"].dump() + " S_miss " + f0["S_miss"].dump() +
            " P " + f0["P"].dump() + " D " + f0["D"].dump() + " (expected 10 / 8 / 4 / 5)");
  CHECK(mid["steps"][0]["S"] == f0["S"] && mid["steps"][0]["P"] == f0["P"],
        "[chunk re-key] the first owner's delete must read and rewrite as many chunks as a middle owner's");
  std::cerr << "[chunk re-key] " << cells << " cells (" << seqs.size() << " write sequences x 3 pack sizes x 2 edge-prop placements) == the uniform Split store; "
            << "delete of the first owner at pack 65536: S " << f0["S"] << " S_miss " << f0["S_miss"] << " P " << f0["P"] << " D " << f0["D"]
            << " (a middle owner: P " << mid["steps"][0]["P"] << " D " << mid["steps"][0]["D"] << ")\n";
}

// PER-TYPE CSR, the same family of defect, fixed: its write path read a vertex's neighbours with a FRESH
// reader on the store, which did not see the write's pending deletes. On delete_vertex of a chunk's first or sole
// owner that has a SELF-LOOP, the in pass reloaded the out chunk the out pass had already moved or emptied and put it
// back: the deleted vertex's edges returned. Now the neighbours are read through the write's own reader, which holds
// what the first direction's pass changed: the second pass no longer sees the self-loop, so it never goes back to the
// chunk the first pass left. Every cell must equal the Split store, the one whose only in-edge is the self-loop
// included (at chunk 1 its in chunk empties first).
static void check_pertype_csr_selfloop_delete(const Schema& s, const std::string& schema_path) {
  auto person = [&](int i) { return hex16(make_uid(s, "Person", uint64_t(i))); };
  std::string r;
  for (int i = 2; i <= 9; i++)
    r += "[\"v\",\"" + person(i) + "\",\"Person\",{\"id\":" + std::to_string(i) + ",\"firstName\":\"Bo\",\"lastName\":\"L" +
         std::to_string(i) + "\",\"creationDate\":" + std::to_string(1000 * i) + "}]\n";
  for (auto& [a, b] : std::vector<std::pair<int, int>>{{2, 3}, {2, 4}, {3, 4}, {3, 5}, {4, 5}, {6, 4}, {7, 4}, {4, 8}, {5, 2}, {9, 2}})
    r += "[\"e\",\"knows\",\"" + person(a) + "\",\"" + person(b) + "\",{\"creationDate\":" + std::to_string(100 * b) +
         "},\"Person\",\"Person\"]\n";
  json split = {{"vertex_props", {{"group", "owner"}}}, {"adjacency", {{"group", "item"}}}, {"direction", "both"}};
  std::string ds = ingest_store(s, schema_path, r, "meta", split);
  json all = json::array();
  for (int i = 2; i <= 9; i++) all.push_back(person(i));
  json read = {{"name", "out"}, {"op", "read_neighbors"}, {"srcs", all}, {"etype", "knows"}, {"dir", "both"}};
  auto loop = [&](int a) {
    return json{{"name", "l"}, {"op", "insert_edge"}, {"etype", "knows"}, {"src", person(a)}, {"dst", person(a)},
                {"props", {{"creationDate", 7}}}, {"src_label", "Person"}, {"dst_label", "Person"}};
  };
  auto delv = [&](int a) {
    return json{{"name", "dv"}, {"op", "delete_vertex"}, {"vid", person(a)}, {"label", "Person"}, {"with_edges", true}};
  };
  auto rows_of = [&](const std::string& dir, const std::string& fmt, const json& plan) {
    json got = run(dir, fmt, schema_path, plan);
    if (!got.contains("rows") || got["rows"].is_null()) return json("ERROR " + got.dump().substr(0, 200));
    json rows = got["rows"];
    std::sort(rows.begin(), rows.end(), [](const json& a, const json& b) { return a.dump() < b.dump(); });
    return rows;
  };
  size_t cells = 0;
  for (auto& [cname, cfg] : std::vector<std::pair<std::string, json>>{{"chunk 65536", json::object()}, {"chunk 1", {{"chunk_target", 1}}}}) {
    std::string dp = ingest_store(s, schema_path, r, "pertype_csr", cfg);
    for (auto& [what, plan] : std::vector<std::pair<std::string, json>>{
             {"delete the first owner", json::array({delv(2), read})},
             {"delete a middle owner", json::array({delv(4), read})},
             {"self-loop on the first owner, then delete it", json::array({loop(2), delv(2), read})},
             {"self-loop on a middle owner, then delete it", json::array({loop(4), delv(4), read})},
             {"self-loop on an owner with no other in-edge, then delete it", json::array({loop(6), delv(6), read})}}) {
      json want = rows_of(ds, "meta", plan), got = rows_of(dp, "pertype_csr", plan);
      cells++;
      CHECK(want.is_array() && !want.empty(), "[pertype_csr delete] " + cname + ", " + what + ": the Split store gave no rows to compare");
      CHECK(got == want, "[pertype_csr delete] " + cname + ", " + what + ": differs from the Split store: " + got.dump().substr(0, 300));
    }
  }
  CHECK(cells == 10, "[pertype_csr delete] " + std::to_string(cells) + " cells (expected 10)");
  std::cerr << "[pertype_csr delete] " << cells << " cells equal the Split store (a self-loop on a chunk's first, middle or sole "
               "owner, then its delete)\n";
}

// GROUPED props (config.h vertex_groups): the spec round-trips in canonical form (props sorted inside a group,
// the group order kept), a spec without groups keeps its old shape, and the Meta ctor refuses a groups list that
// does not partition a label's declared props exactly (or sits on a non-Condensed layout).
static void check_grouped_spec(const Schema& s) {
  std::vector<std::string> ps;
  for (auto& p : s.vertex("Person").props) ps.push_back(p.name);
  if (ps.size() < 3) { fails++; std::cerr << "FAIL: [grouped spec] the test schema's Person has < 3 props (the test would prove nothing)\n"; return; }
  json rest = json::array();
  for (size_t i = 2; i < ps.size(); i++) rest.push_back(ps[i]);
  json g = {{"Person", json::array({json::array({ps[1], ps[0]}), rest})}};   // group 0 listed out of order
  json spec = {{"vertex_props", {{"group", "owner"}, {"groups", g}}}};
  json canon = meta::Config::from_json(spec).to_json();
  CHECK(canon == meta::Config::from_json(canon).to_json(), "grouped spec round-trip failed: " + canon.dump());
  std::vector<std::string> first = canon["vertex_props"]["groups"]["Person"][0].get<std::vector<std::string>>();
  CHECK(std::is_sorted(first.begin(), first.end()) && first.size() == 2, "group props not canonical (sorted): " + canon.dump());
  CHECK(!meta::Config::from_json(meta_corner("nebula")).to_json()["vertex_props"].contains("groups"),
        "a spec without groups must keep its old shape (store identity)");
  auto built = [&](const json& j) {
    try { meta::Meta m(s, meta::Config::from_json(j)); return true; } catch (const std::exception&) { return false; }
  };
  CHECK(built(spec), "a valid grouped spec was refused: " + spec.dump());
  auto with = [&](json groups, json vp = {{"group", "owner"}}) { vp["groups"] = groups; return json{{"vertex_props", vp}}; };
  json dup = rest; dup.push_back(ps[0]);
  json missing = json::array({ps[0], ps[1]});
  std::vector<std::pair<const char*, json>> refused = {
      {"unknown prop", with({{"Person", json::array({json::array({ps[0], "nosuch"}), rest})}})},
      {"a prop listed twice", with({{"Person", json::array({json::array({ps[0], ps[1]}), dup})}})},
      {"props missing", with({{"Person", json::array({missing})}})},
      {"id listed", with({{"Person", json::array({json::array({"id", ps[0], ps[1]}), rest})}})},
      {"unknown label", with({{"Persn", json::array({json::array({ps[0]})})}})},
      {"on Split props", with(g, {{"group", "item"}})},
      {"an empty group", with({{"Person", json::array({json::array(), json::array({ps[0]})})}})},
  };
  for (auto& [what, j] : refused) CHECK(!built(j), std::string("a bad groups spec was built: ") + what);
  std::cerr << "[grouped spec] canonical round-trip + " << refused.size() << " bad groups specs refused\n";
}

// A MIXED store: knows chunked, every other edge type Split, props Condensed (+ Tag props Split). Each
// part must read exactly as the uniform store with that part's layout does (knows == the all-chunked
// store, hasInterest == the all-split store, Tag props == all-split props), and writes on both kinds of
// list must be visible to the reads after them (rows == the uniform split store's).
static void check_per_type_store(const Schema& s, const std::string& schema_path) {
  auto person = [&](int i) { return hex16(make_uid(s, "Person", uint64_t(i))); };
  auto tag = [&](int i) { return hex16(make_uid(s, "Tag", uint64_t(i))); };
  std::string r = graph(s);
  for (int i = 1; i <= 3; i++)
    r += "[\"v\",\"" + tag(i) + "\",\"Tag\",{\"id\":" + std::to_string(i) + ",\"name\":\"t" + std::to_string(i) +
         "\",\"url\":\"u" + std::to_string(i) + "\"}]\n";
  for (auto& [p, t] : std::vector<std::pair<int, int>>{{1, 1}, {1, 2}, {2, 1}, {3, 3}})
    r += "[\"e\",\"hasInterest\",\"" + person(p) + "\",\"" + tag(t) + "\",{},\"Person\",\"Tag\"]\n";
  json cond = {{"group", "owner"}}, split_ = {{"group", "item"}}, chunk = {{"group", "owner"}, {"owners", {{"pack", 65536}}}};
  json base = {{"vertex_props", cond}, {"adjacency", split_}, {"direction", "declared"}};
  json mixj = base;
  mixj["adjacency_by_etype"] = {{"knows", chunk}};
  mixj["vertex_props_by_label"] = {{"Tag", split_}};
  json all_chunk = base, all_split = base, props_split = base;
  all_chunk["adjacency"] = chunk;
  props_split["vertex_props"] = split_;
  std::string dm = ingest_store(s, schema_path, r, "meta", mixj);
  std::string dc = ingest_store(s, schema_path, r, "meta", all_chunk);
  std::string ds = ingest_store(s, schema_path, r, "meta", all_split);
  std::string dp = ingest_store(s, schema_path, r, "meta", props_split);
  auto cmp = [&](const char* what, const std::string& dref, json steps) {
    json rm = run(dm, "meta", schema_path, steps), rr = run(dref, "meta", schema_path, steps);
    fair_equal((std::string("per-type ") + what).c_str(), rm, rr);
    std::cerr << "[per-type] " << what << ": rows=" << (rm.contains("rows") ? rm["rows"].size() : 0)
              << " S=" << rm["steps"][0]["S"] << " G=" << rm["steps"][0]["G"] << " N=" << rm["steps"][0]["N"] << "\n";
  };
  json srcs = json::array({person(1), person(2), person(4)});
  cmp("knows == chunked", dc, json::array({{{"name", "out"}, {"op", "read_neighbors"}, {"srcs", srcs}, {"etype", "knows"}, {"dir", "both"}}}));
  cmp("knows+facet == chunked", dc, json::array({{{"name", "out"}, {"op", "read_neighbors"}, {"srcs", srcs}, {"etype", "knows"}, {"dir", "out"}, {"edge_props", json::array({"creationDate"})}, {"pairs", true}}}));
  cmp("hasInterest == split", ds, json::array({{{"name", "out"}, {"op", "read_neighbors"}, {"srcs", srcs}, {"etype", "hasInterest"}, {"dir", "out"}}}));
  cmp("Person props == condensed", ds, json::array({{{"name", "out"}, {"op", "read_vertex_props"}, {"vids", srcs}, {"props", json::array({"firstName", "lastName"})}, {"label", json::array({"Person"})}}}));
  cmp("Tag props == split", dp, json::array({{{"name", "out"}, {"op", "read_vertex_props"}, {"vids", json::array({tag(1), tag(3)})}, {"props", json::array({"name", "url"})}, {"label", json::array({"Tag"})}}}));
  // the capabilities are the intersection: Tag is Split, so no whole_row_read, no fused scan
  {
    meta::Meta m(s, meta::Config::from_json(mixj));
    std::set<std::string> caps = m.capabilities();
    CHECK(!caps.count("whole_row_read") && !caps.count("label_scan_carries_props"),
          "a mixed store advertised a props capability one of its labels does not have");
    // per label (what a variant's `on` checks): each label answers for its own layout
    CHECK(m.capabilities_for("Person").count("whole_row_read") && !m.capabilities_for("Tag").count("whole_row_read"),
          "per-label whole_row_read: a condensed label has it, a split label does not");
    // one label packed: only that label carries props on a scan; the store-wide set is the intersection
    json one_packed = base;
    json packed_props = {{"group", "owner"}, {"owners", {{"pack", 65536}}}, {"split", "never"}};
    one_packed["vertex_props_by_label"] = {{"Person", packed_props}};
    meta::Meta mp(s, meta::Config::from_json(one_packed));
    CHECK(mp.capabilities_for("Person").count("label_scan_carries_props") &&
              !mp.capabilities_for("Forum").count("label_scan_carries_props") &&
              !mp.capabilities().count("label_scan_carries_props"),
          "per-label label_scan_carries_props: the packed label only, and not store-wide");
  }
  // writes on both kinds of list, then the reads that must see them (rows vs the uniform split store)
  auto rows_equal = [&](const char* what, json steps) {
    json rm = run(dm, "meta", schema_path, steps), rs = run(ds, "meta", schema_path, steps);
    CHECK(rm.contains("rows") && rs.contains("rows") && rm["rows"] == rs["rows"],
          std::string("per-type write ") + what + ": rows differ: " + rm.dump().substr(0, 300) + " vs " + rs.dump().substr(0, 300));
    std::cerr << "[per-type] write " << what << ": rows=" << (rm.contains("rows") ? rm["rows"].size() : 0) << "\n";
  };
  json ins_k = {{"name", "w"}, {"op", "insert_edge"}, {"etype", "knows"}, {"src", person(2)}, {"dst", person(5)},
                {"props", {{"creationDate", 777}}}, {"src_label", "Person"}, {"dst_label", "Person"}};
  json ins_h = {{"name", "w"}, {"op", "insert_edge"}, {"etype", "hasInterest"}, {"src", person(4)}, {"dst", tag(2)},
                {"src_label", "Person"}, {"dst_label", "Tag"}};
  json del_v = {{"name", "w"}, {"op", "delete_vertex"}, {"vid", person(1)}, {"label", "Person"}, {"with_edges", true}};
  json read_k = {{"name", "out"}, {"op", "read_neighbors"}, {"srcs", json::array({person(1), person(2), person(4), person(5)})},
                 {"etype", "knows"}, {"dir", "both"}, {"edge_props", json::array({"creationDate"})}, {"pairs", true}};
  json read_h = {{"name", "out"}, {"op", "read_neighbors"}, {"srcs", json::array({person(1), person(2), person(4)})},
                 {"etype", "hasInterest"}, {"dir", "out"}};
  rows_equal("insert knows", json::array({ins_k, read_k}));
  rows_equal("insert hasInterest", json::array({ins_h, read_h}));
  rows_equal("delete vertex (knows)", json::array({del_v, read_k}));
  rows_equal("delete vertex (hasInterest)", json::array({del_v, read_h}));
  // parts in a mixed store: every key decodes by its SHAPE to its own family (knows packed, hasInterest
  // unpacked), never 'unknown', and the parts sum to the step
  {
    Session sess;
    json both = json::array({{{"name", "k"}, {"op", "read_neighbors"}, {"srcs", srcs}, {"etype", "knows"}, {"dir", "both"},
                              {"edge_props", json::array({"creationDate"})}, {"pairs", true}},
                             {{"name", "out"}, {"op", "read_neighbors"}, {"srcs", srcs}, {"etype", "hasInterest"}, {"dir", "out"}}});
    json rr = sess.handle({{"store", {{"dir", dm + "/store"}, {"schema", schema_path}, {"format", "meta"}}},
                           {"plan", {{"plan", "t"}, {"params", json::object()}, {"steps", both}, {"output", "out"}}},
                           {"run", {{"parts", true}}}});
    std::set<std::string> names;
    for (auto& st : rr["steps"])
      if (st.contains("parts"))
        for (auto& [name, c] : st["parts"].items()) names.insert(name);
    // knows is Chunked with its props inline (the default placement): no eprops part
    CHECK(names.count("adj:Person:knows:out") && names.count("adj:Person:knows:in") &&
              !names.count("eprops:Person:knows") && names.count("adj:Person:hasInterest:out") && !names.count("unknown"),
          "mixed-store parts: " + json(names).dump());
    std::cerr << "[per-type] parts in the mixed store: " << json(names).dump() << "\n";
  }
  // one key family (meta-v4, keys.h): a mixed store has no second key shape to collide with, so an owner id that
  // the v3 guard refused ('a' << 48) is written and read back like any other
  {
    uint64_t big = (uint64_t(s.tag_of_label("Person")) << kVidShift) | (uint64_t('a') << 48);
    json ins = json::array({{{"name", "w"}, {"op", "insert_edge"}, {"etype", "hasInterest"}, {"src", hex16(big)},
                             {"dst", tag(1)}, {"src_label", "Person"}, {"dst_label", "Tag"}},
                            {{"name", "out"}, {"op", "read_neighbors"}, {"srcs", json::array({hex16(big)})},
                             {"etype", "hasInterest"}, {"dir", "out"}}});
    json rm = run(dm, "meta", schema_path, ins);
    CHECK((!rm.contains("error") || rm["error"].is_null()) && rm["rows"].size() == 1,
          "the mixed store refused or lost an owner id >= 'a' << 48: " + rm.dump().substr(0, 200));
    std::cerr << "[per-type] one key family: a mixed store writes and reads an owner id >= 'a' << 48\n";
  }
}

// edge-prop PLACEMENT on Chunked adjacency: inline (both directions' chunks carry the props) vs own (pertype_csr's
// ordinal-keyed ep chunks). The answers must be the same for every edge-prop read and after every edge write; the
// work differs (no ep-chunk locates inline; the chunks are larger), which is the setting's effect, printed.
static void check_placement(const Schema& s, const std::string& schema_path) {
  std::string records = graph(s);
  json chunk = {{"group", "owner"}, {"owners", {{"pack", 65536}}}};
  json own = chunk;
  own["edge_props"] = "own";
  json base = {{"vertex_props", {{"group", "owner"}}}, {"direction", "both"}};
  json ji = base, jo = base;
  ji["adjacency"] = chunk;
  jo["adjacency"] = own;
  std::string di = ingest_store(s, schema_path, records, "meta", ji), dov = ingest_store(s, schema_path, records, "meta", jo);
  auto person = [&](int i) { return hex16(make_uid(s, "Person", uint64_t(i))); };
  auto nbrs = [&](std::vector<int> who, const char* dir) {
    json srcs = json::array();
    for (int w : who) srcs.push_back(person(w));
    return json{{"name", "out"}, {"op", "read_neighbors"}, {"srcs", srcs}, {"etype", "knows"}, {"dir", dir},
                {"edge_props", json::array({"creationDate"})}, {"pairs", true}};
  };
  json ins = {{"name", "w"}, {"op", "insert_edge"}, {"etype", "knows"}, {"src", person(2)}, {"dst", person(5)},
              {"props", {{"creationDate", 777}}}, {"src_label", "Person"}, {"dst_label", "Person"}};
  json upd = {{"name", "w"}, {"op", "update_edge_prop"}, {"etype", "knows"}, {"src", person(1)}, {"dst", person(3)},
              {"props", {{"creationDate", 555}}}, {"src_label", "Person"}, {"dst_label", "Person"}};
  json del = {{"name", "w"}, {"op", "delete_edge"}, {"etype", "knows"}, {"src", person(1)}, {"dst", person(2)},
              {"src_label", "Person"}, {"dst_label", "Person"}};
  json delv = {{"name", "w"}, {"op", "delete_vertex"}, {"vid", person(4)}, {"label", "Person"}, {"with_edges", true}};
  int n = 0;
  for (auto& [what, steps] : std::vector<std::pair<std::string, json>>{
           {"read out", json::array({nbrs({1, 2, 3, 4}, "out")})}, {"read in", json::array({nbrs({2, 3, 4, 5}, "in")})},
           {"insert, then in", json::array({ins, nbrs({5}, "in")})}, {"update, then out", json::array({upd, nbrs({1}, "out")})},
           {"update, then in", json::array({upd, nbrs({3}, "in")})}, {"delete, then both", json::array({del, nbrs({1, 2}, "both")})},
           {"delete vertex, then both", json::array({delv, nbrs({1, 2, 3, 5}, "both")})}}) {
    json ri = run(di, "meta", schema_path, steps), ro = run(dov, "meta", schema_path, steps);
    CHECK(ri.contains("rows") && ro.contains("rows") && ri["rows"] == ro["rows"] && !ri["rows"].empty(),
          "placement " + what + ": rows differ: " + ri.dump().substr(0, 300) + " vs " + ro.dump().substr(0, 300));
    n++;
    auto ops = [](const json& r) {
      uint64_t t = 0;
      for (auto& st : r["steps"]) t += st["S"].get<uint64_t>() + st["G"].get<uint64_t>() + st["N"].get<uint64_t>();
      return t;
    };
    std::cerr << "[placement] " << what << ": rows=" << ri["rows"].size() << " ops inline " << ops(ri) << " / own " << ops(ro) << "\n";
  }
  // the update of an ABSENT edge writes nothing in either placement
  json absent = upd;
  absent["src"] = person(3);
  absent["dst"] = person(1);
  for (auto& d : {di, dov}) {
    json r = run(d, "meta", schema_path, json::array({absent, nbrs({3}, "out")}));
    CHECK(r["steps"][0]["P"] == 0 && r["steps"][0]["D"] == 0, "placement: an absent edge's update writes nothing");
  }
  std::cerr << "[placement] inline == own on " << n << " read / write cases (rows)\n";
}

// op rules latent in the grid: each op returns the same answer on every layout of
// the component it reads, and a probe the schema rules out costs nothing
static void check_op_rules(const Schema& s, const std::string& schema_path) {
  auto person = [&](int i) { return hex16(make_uid(s, "Person", uint64_t(i))); };
  auto org = [&](int i) { return hex16(make_uid(s, "Organisation", uint64_t(i))); };
  std::string records = graph(s);
  for (auto& [p, o] : std::vector<std::pair<int, int>>{{1, 1}, {2, 1}, {3, 2}})   // studyAt: declared, NO reverse
    records += "[\"e\",\"studyAt\",\"" + person(p) + "\",\"" + org(o) + "\",{\"classYear\":" + std::to_string(2000 + p) +
               "},\"Person\",\"Organisation\"]\n";
  json layouts = {{"split", {{"group", "item"}}}, {"condensed", {{"group", "owner"}}},
                  {"chunked", {{"group", "owner"}, {"owners", {{"pack", 65536}}}}}};
  json L = json::array({"Person"});
  std::string tag_vid = hex16(make_uid(s, "Tag", 7));
  auto ops = [](const json& r) {   // an op that threw has no steps: count it as unbounded
    if (!r.contains("steps")) return UINT64_MAX;
    uint64_t t = 0;
    for (auto& st : r["steps"]) t += st["S"].get<uint64_t>() + st["G"].get<uint64_t>() + st["N"].get<uint64_t>();
    return t;
  };
  // props reads on each props layout (adjacency fixed: Condensed)
  std::map<std::string, json> by_id, anchor, anchor_absent;
  for (auto& [name, props] : layouts.items()) {
    std::string d = ingest_store(s, schema_path, records, "meta", json{{"vertex_props", props}, {"adjacency", {{"group", "owner"}}}});
    // 1f: the first candidate that exists, in every layout (Split probes the vertex prefix)
    by_id[name] = run(d, "meta", schema_path, json::array({{{"name", "out"}, {"op", "read_vertex_by_id"},
        {"vid", json::array({person(99), person(2), person(3)})}, {"label", L}}}));
    // 1e: the first existing candidate ends the read; a wanted prop absent everywhere still returns the vertex
    anchor[name] = run(d, "meta", schema_path, json::array({{{"name", "out"}, {"op", "read_anchor_props"},
        {"cands", json::array({person(99), person(1)})}, {"props", json::array({"firstName", "lastName"})}, {"labels", L}}}));
    anchor_absent[name] = run(d, "meta", schema_path, json::array({{{"name", "out"}, {"op", "read_anchor_props"},
        {"cands", json::array({person(1)})}, {"props", json::array({"locationIP"})}, {"labels", L}}}));
  }
  for (auto& name : {"condensed", "chunked"}) {
    CHECK(by_id[name]["rows"] == by_id["split"]["rows"] && by_id["split"]["rows"].size() == 1,
          std::string("1f: read_vertex_by_id over candidates differs on ") + name + ": " + by_id[name].dump().substr(0, 200) +
          " vs split " + by_id["split"].dump().substr(0, 200));
    CHECK(anchor[name]["rows"] == anchor["split"]["rows"] && anchor["split"]["rows"].size() == 1,
          std::string("1e: read_anchor_props differs on ") + name);
    CHECK(anchor_absent[name]["rows"] == anchor_absent["split"]["rows"] && anchor_absent["split"]["rows"].size() == 1,
          std::string("1e: an absent wanted prop drops the vertex on ") + name + " or split");
  }
  // adjacency on each adjacency layout (props fixed: Condensed), direction as declared
  std::map<std::string, json> ex_fwd, ex_back, ruled;
  for (auto& [name, adj] : layouts.items()) {
    std::string d = ingest_store(s, schema_path, records, "meta", json{{"vertex_props", {{"group", "owner"}}}, {"adjacency", adj}});
    // 1i: edge existence on an etype with no reverse kept, from either end
    ex_fwd[name] = run(d, "meta", schema_path, json::array({{{"name", "out"}, {"op", "read_edge_exists"},
        {"rows", json::array({json{{"x", org(1)}}, json{{"x", org(2)}}})}, {"field", "x"}, {"etype", "studyAt"},
        {"into", "e"}, {"others", json::array({person(1)})}, {"reads", kReadsOutKeys}}}));
    ex_back[name] = run(d, "meta", schema_path, json::array({{{"name", "out"}, {"op", "read_edge_exists"},
        {"rows", json::array({json{{"x", person(1)}}, json{{"x", person(3)}}})}, {"field", "x"}, {"etype", "studyAt"},
        {"into", "e"}, {"others", json::array({org(1)})}, {"reads", kReadsOutKeys}}}));
    // 1t under the opaque-vid rule: the PLAN's label rules a family out (a Tag owns no hasModerator family) -> no
    // probe; without a label the probe happens (the vid's bits decide nothing), with the same empty answer
    ruled[name] = run(d, "meta", schema_path, json::array({{{"name", "out"}, {"op", "read_neighbors"},
        {"srcs", json::array({tag_vid})}, {"etype", "hasModerator"}, {"dir", "both"}, {"label", json::array({"Tag"})}}}));
    CHECK(ruled[name].contains("rows") && ruled[name]["rows"].empty() && ops(ruled[name]) == 0,
          std::string("1t: a probe the plan's label rules out costs ops on ") + name + ": " + ruled[name].dump().substr(0, 200));
    json blind = run(d, "meta", schema_path, json::array({{{"name", "out"}, {"op", "read_neighbors"},
        {"srcs", json::array({tag_vid})}, {"etype", "hasModerator"}, {"dir", "both"}}}));
    CHECK(blind.contains("rows") && blind["rows"].empty() && ops(blind) > 0 && ops(blind) != UINT64_MAX,
          std::string("1t: with no label the probe must happen (the vid is opaque) on ") + name + ": " + blind.dump().substr(0, 200));
    std::cerr << "[op rules] " << name << " adjacency: edge exists fwd/back ops " << ops(ex_fwd[name]) << " / "
              << ops(ex_back[name]) << "\n";
  }
  for (auto& name : {"condensed", "chunked"}) {
    CHECK(ex_fwd[name].contains("rows") && ex_fwd[name]["rows"] == ex_fwd["split"]["rows"],
          std::string("1i: edge exists (anchor -> candidate) differs on ") + name + ": " + ex_fwd[name].dump().substr(0, 200));
    CHECK(ex_back[name].contains("rows") && ex_back[name]["rows"] == ex_back["split"]["rows"],
          std::string("1i: edge exists (candidate -> anchor) differs on ") + name + ": " + ex_back[name].dump().substr(0, 200));
  }
  CHECK(ex_fwd["split"]["rows"].size() == 2 && ex_fwd["split"]["rows"][0]["e"] == true && ex_fwd["split"]["rows"][1]["e"] == false,
        "1i: split's own answer is wrong: " + ex_fwd["split"].dump().substr(0, 200));
  CHECK(ex_back["split"]["rows"].size() == 2 && ex_back["split"]["rows"][0]["e"] == true && ex_back["split"]["rows"][1]["e"] == false,
        "1i: split's own answer (reverse orientation) is wrong: " + ex_back["split"].dump().substr(0, 200));
}

// a unique edge type holds at most one (src, etype, dst) -- every adjacency layout refuses a
// parallel edge at ingest (Split would fold it into one key, a list keep both)
static void check_parallel_refused(const Schema& s, const std::string& schema_path) {
  auto person = [&](int i) { return hex16(make_uid(s, "Person", uint64_t(i))); };
  std::string dup = graph(s) + "[\"e\",\"knows\",\"" + person(1) + "\",\"" + person(2) + "\",{\"creationDate\":1},\"Person\",\"Person\"]\n";
  json layouts = {{"split", {{"group", "item"}}}, {"condensed", {{"group", "owner"}}},
                  {"chunked", {{"group", "owner"}, {"owners", {{"pack", 65536}}}}}};
  for (auto& [name, adj] : layouts.items()) {
    bool refused = false;
    try {
      ingest_store(s, schema_path, dup, "meta", json{{"vertex_props", {{"group", "owner"}}}, {"adjacency", adj}});
    } catch (const std::exception& e) {
      refused = std::string(e.what()).find("parallel") != std::string::npos;
    }
    CHECK(refused, std::string("1j: a parallel knows edge was ingested on ") + name);
  }
  std::cerr << "[op rules] a parallel edge is refused on split / condensed / chunked adjacency\n";
  // multiplicity: `multi` is declared, then refused by meta (not built); an unknown value fails the schema load
  json sj = json::parse(std::ifstream(schema_path));
  for (auto& e : sj["edges"]) if (e["etype"] == "knows") e["multiplicity"] = "multi";
  std::string multi_path = std::string("/tmp/evalcpp-meta-multi-") + std::to_string(getpid()) + ".json";
  std::ofstream(multi_path) << sj.dump();
  bool refused = false;
  try {
    Schema sm = Schema::load(multi_path);
    ingest_store(sm, multi_path, graph(s), "meta", json{{"vertex_props", {{"group", "owner"}}}, {"adjacency", {{"group", "owner"}}}});
  } catch (const std::exception& e) {
    refused = std::string(e.what()).find("multiplicity multi") != std::string::npos;
  }
  CHECK(refused, "1j: meta built a store whose knows edge type is declared multi");
  for (auto& e : sj["edges"]) if (e["etype"] == "knows") e["multiplicity"] = "many";
  std::ofstream(multi_path) << sj.dump();
  bool bad = false;
  try { Schema::load(multi_path); } catch (const std::exception&) { bad = true; }
  CHECK(bad, "1j: the schema loaded an unknown multiplicity");
  std::remove(multi_path.c_str());
}

// FAR-END LABELS (adjacency far_label, default on): a hasCreator-in entry stores its message's label (Post | Comment:
// k = 2, 1 bit); a hop tags each neighbour value with its label (Value::label), so a Post | Comment props read and a
// Post filter on those values stop probing -- what reaches them on the row, nothing else: another run, and the
// measured iteration after a warmup that hopped, probe again. OFF = the store without the block (1 B smaller here) and the probes back. An etype with no
// endpoints fails at spec load, named.
static void check_far_label(const Schema& s, const std::string& schema_path) {
  auto vid = [&](const char* lab, int i) { return hex16(make_uid(s, lab, uint64_t(i))); };
  std::string r = "[\"v\",\"" + vid("Person", 1) + "\",\"Person\",{\"id\":1,\"firstName\":\"A\",\"creationDate\":1}]\n";
  r += "[\"v\",\"" + vid("Post", 10) + "\",\"Post\",{\"id\":10,\"content\":\"p\",\"creationDate\":10,\"length\":1}]\n";
  r += "[\"v\",\"" + vid("Comment", 20) + "\",\"Comment\",{\"id\":20,\"content\":\"c\",\"creationDate\":20,\"length\":1}]\n";
  for (auto [lab, i] : std::vector<std::pair<const char*, int>>{{"Post", 10}, {"Comment", 20}})
    r += std::string("[\"e\",\"hasCreator\",\"") + vid(lab, i) + "\",\"" + vid("Person", 1) + "\",{},\"" + lab +
         "\",\"Person\"]\n";
  json on = {{"direction", "declared"}, {"vertex_props", {{"group", "owner"}}}, {"adjacency", {{"group", "owner"}}}};
  json off = on;
  off["adjacency"]["far_label"] = false;
  std::string d_on = ingest_store(s, schema_path, r, "meta", on), d_off = ingest_store(s, schema_path, r, "meta", off);
  json hop = {{"name", "m"}, {"op", "read_neighbors"}, {"srcs", json::array({vid("Person", 1)})}, {"etype", "hasCreator"},
              {"dir", "in"}};
  json props = {{"name", "out"}, {"op", "read_vertex_props"}, {"vids", {{"ref", "m"}}}, {"props", json::array({"content"})},
                {"label", json::array({"Post", "Comment"})}};
  // 1. a hop, then a Post | Comment props read: ON reads each message's own row (2 GETs); OFF probes Post first, so the
  //    Comment costs a Post miss + its own row (3 GETs). The same answer.
  json r_on = run(d_on, "meta", schema_path, json::array({hop, props}));
  json r_off = run(d_off, "meta", schema_path, json::array({hop, props}));
  CHECK(r_on["rows"] == r_off["rows"] && r_on["rows"].size() == 2, "far label: props answers differ ON vs OFF");
  CHECK(r_on["steps"][1]["G"] == 2 && r_off["steps"][1]["G"] == 3,
        "far label: the props read after the hop: G " << r_on["steps"][1]["G"] << " ON (want 2), " << r_off["steps"][1]["G"]
        << " OFF (want 3)");
  // 2. a hop, then a Post filter: decided by the learned labels, no probe (OFF: one GET per message)
  json filt = {{"name", "out"}, {"op", "read_filter_by_label_probe"}, {"vids", {{"ref", "m"}}}, {"label", "Post"}};
  json f_on = run(d_on, "meta", schema_path, json::array({hop, filt}));
  json f_off = run(d_off, "meta", schema_path, json::array({hop, filt}));
  CHECK(f_on["rows"] == json::array({vid("Post", 10)}) && f_on["rows"] == f_off["rows"], "far label: filter answers");
  CHECK(f_on["steps"][1]["G"] == 0 && f_off["steps"][1]["G"] == 2,
        "far label: the filter after the hop: G " << f_on["steps"][1]["G"] << " ON (want 0), " << f_off["steps"][1]["G"] << " OFF (want 2)");
  // 3. leak control, across runs: the same filter in a run with no hop probes again (2 GETs)
  json lit = {{"name", "out"}, {"op", "read_filter_by_label_probe"}, {"vids", json::array({vid("Post", 10), vid("Comment", 20)})},
              {"label", "Post"}};
  json l1 = run(d_on, "meta", schema_path, json::array({lit}));
  CHECK(l1["steps"][0]["G"] == 2, "far label leak (across runs): a run with no hop probed " << l1["steps"][0]["G"] << " (want 2)");
  // 4. leak control, across iterations: the filter BEFORE the hop, after a warmup iteration that hopped -- the measured
  //    iteration starts with nothing known, so it probes (2 GETs)
  json lit2 = lit;
  lit2["name"] = "f";
  json hop2 = hop;
  hop2["name"] = "out";
  Session sess;
  json l2 = sess.handle({{"store", {{"dir", d_on + "/store"}, {"schema", schema_path}, {"format", "meta"}}},
                         {"plan", {{"plan", "t"}, {"params", json::object()}, {"steps", json::array({lit2, hop2})}, {"output", "out"}}},
                         {"run", {{"iterations", {{"warmup", 1}, {"measured", 1}}}}}});
  CHECK(l2.contains("steps") && l2["steps"][0]["G"] == 2,
        "far label leak (across iterations): the measured filter probed " << (l2.contains("steps") ? l2["steps"][0]["G"].dump() : l2.dump().substr(0, 200)) << " (want 2)");
  // 5. bytes: the one hasCreator-in list holds 2 entries at 1 bit = 1 B of labels; nothing else differs
  auto logical = [](const std::string& d) { return json::parse(std::ifstream(d + "/store/manifest.json"))["logical_bytes"].get<uint64_t>(); };
  CHECK(logical(d_on) == logical(d_off) + 1, "far label: ON stores " << logical(d_on) << " B vs OFF " << logical(d_off) << " (want +1)");
  // 6. an etype with no endpoints: refused at spec load, named (OFF: accepted)
  json sj = json::parse(std::ifstream(schema_path));
  for (auto& e : sj["edges"]) if (e["etype"] == "knows") e["endpoints"] = json::array();
  Schema bare = Schema::from_json(sj);
  std::string err;
  try { meta::Meta m(bare, meta::Config::from_json(on)); } catch (const std::exception& ex) { err = ex.what(); }
  CHECK(err.find("knows") != std::string::npos && err.find("endpoints") != std::string::npos,
        "far label: missing endpoints not refused by name: '" << err << "'");
  bool off_ok = true;
  try { meta::Meta m(bare, meta::Config::from_json(off)); } catch (const std::exception&) { off_ok = false; }
  CHECK(off_ok, "far label OFF: an etype with no endpoints must load");
  // 7. own placement (the edge props in their own chunks) stores the block too, in its adjacency chunk beside the
  //    ordinals: likes carries a prop, and its out entries end at a Post or a Comment (k = 2)
  std::string r2 = r;
  for (auto [lab, i] : std::vector<std::pair<const char*, int>>{{"Post", 10}, {"Comment", 20}})
    r2 += std::string("[\"e\",\"likes\",\"") + vid("Person", 1) + "\",\"" + vid(lab, i) + "\",{\"creationDate\":" +
          std::to_string(100 + i) + "},\"Person\",\"" + lab + "\"]\n";
  json chunk = {{"group", "owner"}, {"owners", {{"pack", 65536}}}};
  json own_on = {{"direction", "both"}, {"vertex_props", {{"group", "owner"}}}, {"adjacency", chunk}};
  own_on["adjacency"]["edge_props"] = "own";
  json own_off = own_on, inl_on = own_on;
  own_off["adjacency"]["far_label"] = false;
  inl_on["adjacency"].erase("edge_props");
  std::string o_on = ingest_store(s, schema_path, r2, "meta", own_on), o_off = ingest_store(s, schema_path, r2, "meta", own_off),
              i_on = ingest_store(s, schema_path, r2, "meta", inl_on);
  json likes = {{"name", "m"}, {"op", "read_neighbors"}, {"srcs", json::array({vid("Person", 1)})}, {"etype", "likes"}, {"dir", "out"}};
  json likes_ep = likes;
  likes_ep["name"] = "out";
  likes_ep["edge_props"] = json::array({"creationDate"});
  likes_ep["pairs"] = true;
  for (auto& [what, h] : std::vector<std::pair<std::string, json>>{{"hasCreator in", hop}, {"likes out", likes}}) {
    json p_on = run(o_on, "meta", schema_path, json::array({h, props})), p_off = run(o_off, "meta", schema_path, json::array({h, props}));
    json p_inl = run(i_on, "meta", schema_path, json::array({h, props}));
    CHECK(p_on["rows"] == p_off["rows"] && p_on["rows"] == p_inl["rows"] && p_on["rows"].size() == 2, "own far label, " << what << ": answers differ");
    CHECK(p_on["steps"][1]["G"] == p_inl["steps"][1]["G"] && p_on["steps"][1]["G"] < p_off["steps"][1]["G"],
          "own far label, " << what << ": the props read after the hop: G " << p_on["steps"][1]["G"] << " own ON, "
          << p_inl["steps"][1]["G"] << " inline ON (want equal), " << p_off["steps"][1]["G"] << " own OFF (want more)");
    json q_on = run(o_on, "meta", schema_path, json::array({h, filt})), q_off = run(o_off, "meta", schema_path, json::array({h, filt}));
    CHECK(q_on["rows"] == q_off["rows"] && q_on["steps"][1]["G"] == 0 && q_off["steps"][1]["G"] == 2,
          "own far label, " << what << ": the filter after the hop: G " << q_on["steps"][1]["G"] << " ON (want 0), "
          << q_off["steps"][1]["G"] << " OFF (want 2)");
  }
  json e_on = run(o_on, "meta", schema_path, json::array({likes_ep})), e_off = run(o_off, "meta", schema_path, json::array({likes_ep}));
  json e_inl = run(i_on, "meta", schema_path, json::array({likes_ep}));
  CHECK(e_on["rows"] == e_off["rows"] && e_on["rows"] == e_inl["rows"] && e_on["rows"].size() == 2, "own far label: the edge props read differs");
  // bytes: the hasCreator-in chunk and the likes-out chunk hold 2 entries each at 1 bit = 1 B each; and hasCreator-in,
  // which stores nothing else per edge, gains the skip16 edge column (codec/lists.h: 1 B mode 0 + 1 B degree, its owners'
  // degrees being equal). likes-out has its ordinals' edge column with the label off too. Nothing else differs
  CHECK(logical(o_on) == logical(o_off) + 4, "own far label: ON stores " << logical(o_on) << " B vs OFF " << logical(o_off) << " (want +4)");
  std::cerr << "[far label] props after a hop G 2 vs 3 OFF; filter G 0 vs 2; no leak across runs or iterations; +1 B; "
               "missing endpoints refused; own placement: the same probes saved as inline, +4 B\n";
}

// Labels on the rows beyond hops (the far-end label step, row-level form): a probe that hit tags
// what it outputs (A, both settings); a k = 1 hop tags its neighbours with the setting off too (B); a tag states a
// label, not existence -- after a write step in the run an existence check probes (C); a Split update writes the
// reverse record's code from the plan's src label, checked on a schema where both sides of an etype with props are
// open (D). Nothing is remembered outside the plan's dataflow: the same vids reaching a step by a literal probe again.
static void check_label_tags(const Schema& s, const std::string& schema_path) {
  auto vid = [&](const char* lab, int i) { return hex16(make_uid(s, lab, uint64_t(i))); };
  std::string r = "[\"v\",\"" + vid("Person", 1) + "\",\"Person\",{\"id\":1,\"firstName\":\"A\",\"creationDate\":1}]\n";
  r += "[\"v\",\"" + vid("Post", 10) + "\",\"Post\",{\"id\":10,\"content\":\"p\",\"creationDate\":10,\"length\":1}]\n";
  r += "[\"v\",\"" + vid("Comment", 20) + "\",\"Comment\",{\"id\":20,\"content\":\"c\",\"creationDate\":20,\"length\":1}]\n";
  for (auto [lab, i] : std::vector<std::pair<const char*, int>>{{"Post", 10}, {"Comment", 20}})
    r += std::string("[\"e\",\"hasCreator\",\"") + vid(lab, i) + "\",\"" + vid("Person", 1) + "\",{},\"" + lab +
         "\",\"Person\"]\n";
  json on = {{"direction", "declared"}, {"vertex_props", {{"group", "owner"}}}, {"adjacency", {{"group", "owner"}}}};
  json off = on;
  off["adjacency"]["far_label"] = false;
  for (auto [name, spec] : std::vector<std::pair<const char*, json>>{{"on", on}, {"off", off}}) {
    std::string d = ingest_store(s, schema_path, r, "meta", spec);
    auto run_tags = [&](json steps, json tags) {
      Session sess;   // local: a live Session holds the store's LOCK
      return sess.handle({{"store", {{"dir", d + "/store"}, {"schema", schema_path}, {"format", "meta"}}},
                          {"plan", {{"plan", "t"}, {"params", json::object()}, {"steps", steps}, {"output", "out"}}},
                          {"run", {{"label_tags", tags}}}});
    };
    json msgs = json::array({vid("Post", 10), vid("Comment", 20)});
    json read = {{"name", "a"}, {"op", "read_vertex_props"}, {"vids", msgs}, {"props", json::array({"content"})},
                 {"label", json::array({"Post", "Comment"})}};
    json pl = {{"name", "v"}, {"op", "pluck"}, {"rows", {{"ref", "a"}}}, {"field", "_vid"}};
    json reread = read, filt = {{"name", "out"}, {"op", "read_filter_by_label_probe"}, {"vids", {{"ref", "v"}}}, {"label", "Post"}};
    reread["name"] = "b";
    reread["vids"] = {{"ref", "v"}};
    // A: the first read probes (Post first: the Comment costs a Post miss, 3 GETs); its rows carry the labels it found,
    // so a re-read of them reads each one's own row (2 GETs) and a Post filter on them probes nothing
    json ra = run_tags(json::array({read, pl, reread, filt}), json::object());
    CHECK(ra.contains("steps") && ra["steps"][0]["G"] == 3 && ra["steps"][2]["G"] == 2 && ra["steps"][3]["G"] == 0 &&
              ra["rows"] == json::array({vid("Post", 10)}),
          "label tags (" << name << "): a probe's rows carry the label: " << ra.dump().substr(0, 300));
    json rn = run_tags(json::array({read, pl, reread, filt}), {{"probes", false}});
    CHECK(rn["steps"][2]["G"] == 3 && rn["steps"][3]["G"] == 2,
          "label tags (" << name << "): probes off: G " << rn["steps"][2]["G"] << " / " << rn["steps"][3]["G"] << " (want 3 / 2)");
    // nothing outside the dataflow: the same vids as LITERALS (not the read's rows) carry nothing -- probed again
    json lre = read, lfi = filt;
    lre["name"] = "b";
    lfi["vids"] = msgs;
    json ro = run_tags(json::array({read, lre, lfi}), json::object());
    CHECK(ro["steps"][1]["G"] == 3 && ro["steps"][2]["G"] == 2,
          "label tags (" << name << "): literal vids carry no label: G " << ro["steps"][1]["G"] << " / " << ro["steps"][2]["G"]
          << " (want 3 / 2)");
    // B: a k = 1 hop (hasCreator out: the far end is a Person) tags its neighbours in either setting
    json hop = {{"name", "p"}, {"op", "read_neighbors"}, {"srcs", msgs}, {"etype", "hasCreator"}, {"dir", "out"}};
    json pf = {{"name", "out"}, {"op", "read_filter_by_label_probe"}, {"vids", {{"ref", "p"}}}, {"label", "Person"}};
    json rb = run_tags(json::array({hop, pf}), json::object());
    CHECK(rb["steps"][1]["G"] == 0 && rb["rows"].size() == 2,   // both messages share the one creator
          "label tags (" << name << "): a k = 1 hop tags its neighbours: filter G " << rb["steps"][1]["G"] << " (want 0)");
    // C: a hop's tags decide an existence check -- until a write runs: hop -> delete the Comment -> by-id must probe (2
    // GETs: the Post is not a Comment, the Comment is gone) and find nothing; without the delete, ON decides with 0
    json hin = {{"name", "m"}, {"op", "read_neighbors"}, {"srcs", json::array({vid("Person", 1)})}, {"etype", "hasCreator"},
                {"dir", "in"}};
    json del = {{"name", "d"}, {"op", "delete_vertex"}, {"vid", vid("Comment", 20)}, {"label", "Comment"}, {"with_edges", true}};
    json byid = {{"name", "out"}, {"op", "read_vertex_by_id"}, {"vid", {{"ref", "m"}}}, {"label", json::array({"Comment"})}};
    json rc = run_tags(json::array({hin, del, byid}), json::object());
    CHECK(rc.contains("rows") && rc["rows"].empty() && rc["steps"][2]["G"] == 2,
          "label tags (" << name << "): after a write an existence check probes: rows " << rc.value("rows", json()).dump()
          << ", G " << rc["steps"][2]["G"] << " (want [] and 2)");
    json rk = run_tags(json::array({hin, byid}), json::object());
    CHECK(rk["rows"] == json::array({vid("Comment", 20)}) && rk["steps"][1]["G"] == (std::string(name) == "on" ? 0 : 2),
          "label tags (" << name << "): before any write the tag decides existence: G " << rk["steps"][1]["G"]);
    // C2: only a vertex write can change existence: an edge write between the hop and the by-id leaves the tag deciding
    json ins = {{"name", "w"}, {"op", "insert_edge"}, {"etype", "likes"}, {"src", vid("Person", 1)},
                {"dst", vid("Comment", 20)}, {"props", {{"creationDate", 9}}}, {"src_label", "Person"},
                {"dst_label", "Comment"}};
    json re2 = run_tags(json::array({hin, ins, byid}), json::object());
    CHECK(re2["rows"] == json::array({vid("Comment", 20)}) && re2["steps"][2]["G"] == (std::string(name) == "on" ? 0 : 2),
          "label tags (" << name << "): after an edge write the tag still decides existence: G " << re2["steps"][2]["G"]
          << " (want " << (std::string(name) == "on" ? 0 : 2) << ")");
  }
  // D: knows with both sides open (Person | Comment at either end) and an edge prop; a Split update of Person -> Comment
  // rewrites the reverse record (Comment's in list) with the code of its far end, the Person
  json sj = json::parse(std::ifstream(schema_path));
  for (auto& e : sj["edges"])
    if (e["etype"] == "knows") e["endpoints"] = json::array({json::array({"Person", "Person"}), json::array({"Person", "Comment"}),
                                                             json::array({"Comment", "Person"})});
  std::string sp = std::string(kvt::temp_dir(std::vector<char>({'/', 't', 'm', 'p', '/', 'k', 'n', 'X', 'X', 'X', 'X', 'X', 'X', '\0'}).data())) + "/schema.json";
  { std::ofstream(sp) << sj.dump(); }
  Schema s2 = Schema::load(sp);
  std::string r2 = "[\"v\",\"" + vid("Person", 1) + "\",\"Person\",{\"id\":1,\"firstName\":\"A\",\"creationDate\":1}]\n";
  r2 += "[\"v\",\"" + vid("Comment", 20) + "\",\"Comment\",{\"id\":20,\"content\":\"c\",\"creationDate\":20,\"length\":1}]\n";
  r2 += "[\"e\",\"knows\",\"" + vid("Person", 1) + "\",\"" + vid("Comment", 20) + "\",{\"creationDate\":7},\"Person\",\"Comment\"]\n";
  std::string d2 = ingest_store(s2, sp, r2, "meta", {{"direction", "declared"}, {"adjacency", {{"group", "item"}}}});
  json upd = {{"name", "u"}, {"op", "update_edge_prop"}, {"etype", "knows"}, {"src", vid("Person", 1)}, {"dst", vid("Comment", 20)},
              {"props", {{"creationDate", 9}}}, {"src_label", "Person"}, {"dst_label", "Comment"}};
  json back = {{"name", "n"}, {"op", "read_neighbors"}, {"srcs", json::array({vid("Comment", 20)})}, {"etype", "knows"}, {"dir", "in"}};
  // a props read over [Comment, Person] (Comment first) on the hop's output: the reverse record's code tags the value,
  // so the read goes straight to the Person row (1 GET, firstName "A"); a wrong code (Comment) would read the Comment
  // row and find nothing (null); no code would probe Comment first (2 GETs)
  json rp = {{"name", "out"}, {"op", "read_vertex_props"}, {"vids", {{"ref", "n"}}}, {"props", json::array({"firstName"})},
             {"label", json::array({"Comment", "Person"})}};
  json rd = run(d2, "meta", sp, json::array({upd, back, rp}));
  CHECK(rd.contains("rows") && rd["rows"].size() == 1 && rd["rows"][0].value("firstName", json()) == "A" &&
            rd["steps"][2]["G"] == 1,
        "label tags D: after a Split update the reverse record still says Person: " << rd.dump().substr(0, 300));
  // E: the reported case -- a hop tags Comment X; a filter DROPS X's row; a later chain downstream of both reaches X again
  // over a k > 1 family stored without codes (likes, far_label false by override): the read must probe (2 GETs), the
  // label the dropped row carried is gone. And the concat variant: X tagged and untagged in one input -- the props
  // read takes the tagged twin (1 GET for X); so does the probe filter: X is decided by its first tagged occurrence, no
  // probe for the untagged twin (both ops: a vid's first tagged occurrence)
  {
    std::string r3 = r;
    r3 += std::string("[\"e\",\"likes\",\"") + vid("Person", 1) + "\",\"" + vid("Comment", 20) +
          "\",{\"creationDate\":5},\"Person\",\"Comment\"]\n";
    json sp3 = {{"direction", "declared"}, {"vertex_props", {{"group", "owner"}}}, {"adjacency", {{"group", "owner"}}},
                {"adjacency_by_etype", {{"likes", {{"group", "owner"}, {"far_label", false}}}}}};
    std::string d3 = ingest_store(s, schema_path, r3, "meta", sp3);
    json h = {{"name", "h"}, {"op", "read_neighbors"}, {"srcs", json::array({vid("Person", 1)})}, {"etype", "hasCreator"},
              {"dir", "in"}};
    json f = {{"name", "f"}, {"op", "read_filter_by_label_probe"}, {"vids", {{"ref", "h"}}}, {"label", "Post"}};
    json pc = {{"name", "pc"}, {"op", "read_neighbors"}, {"srcs", {{"ref", "f"}}}, {"etype", "hasCreator"}, {"dir", "out"}};
    json lk = {{"name", "lk"}, {"op", "read_neighbors"}, {"srcs", {{"ref", "pc"}}}, {"etype", "likes"}, {"dir", "out"}};
    json rd = {{"name", "out"}, {"op", "read_vertex_props"}, {"vids", {{"ref", "lk"}}}, {"props", json::array({"content"})},
               {"label", json::array({"Post", "Comment"})}};
    json re = run(d3, "meta", schema_path, json::array({h, f, pc, lk, rd}));
    CHECK(re.contains("steps") && re["steps"][1]["G"] == 0 && re["steps"][4]["G"] == 2 && re["rows"].size() == 1,
          "label tags E: a dropped row's label is gone, the later read probes: " << re.dump().substr(0, 300));
    json cc = {{"name", "cc"}, {"op", "concat"}, {"a", {{"ref", "h"}}}, {"b", {{"ref", "lk"}}}};
    json rp = {{"name", "out"}, {"op", "read_vertex_props"}, {"vids", {{"ref", "cc"}}}, {"props", json::array({"content"})},
               {"label", json::array({"Post", "Comment"})}};
    json rc2 = run(d3, "meta", schema_path, json::array({h, f, pc, lk, cc, rp}));
    CHECK(rc2.contains("steps") && rc2["steps"][5]["G"] == 2 && rc2["rows"].size() == 3,   // the Post and X: 1 GET each
          "label tags E (concat): the props read takes X's tagged twin: " << rc2.dump().substr(0, 300));
    json fp = {{"name", "out"}, {"op", "read_filter_by_label_probe"}, {"vids", {{"ref", "cc"}}}, {"label", "Comment"}};
    json rf = run(d3, "meta", schema_path, json::array({h, f, pc, lk, cc, fp}));
    CHECK(rf.contains("steps") && rf["steps"][5]["G"] == 0 && rf["rows"].size() == 2,
          "label tags E (concat): the probe filter takes X's tagged twin, no probe for the untagged one: "
          << rf.dump().substr(0, 300));
  }
  {   // F: one step's input carries one vid twice with two labels (a bug upstream): first_tags keeps the first, and in
      // the checkers' trace mode (strict) it throws
    Value x1("0000000000000001"), x2("0000000000000001"), y("0000000000000002");
    x1.label = 1; x2.label = 2; y.label = 1;
    ValueList vs{x1, y, x2};
    CHECK(first_tags(vs, false).at(x1.as_str()) == 1 && first_tags(ValueList{x1, y, x1}, true).size() == 2,
          "label tags F: the first tagged occurrence wins; a repeated vid with the same label is no conflict");
    bool threw = false;
    try { first_tags(vs, true); } catch (const std::runtime_error&) { threw = true; }
    CHECK(threw, "label tags F: strict mode must throw on a vid carrying two labels");
  }
  std::cerr << "[label tags] a probe's rows carry the label (G 3 / 2 / 0), literals carry none, a k = 1 hop tags with the "
               "setting off, after a vertex write existence probes (an edge write leaves the tag deciding), a Split update keeps the reverse record's label\n";
}

// A LABEL READ BY ONE SCAN OF ITS ROWS (read_vertices_by_label with props; label_rows_one_run): on every props layout
// the scan's rows equal the label's enumeration + a point read of the same props; it costs 1 SEEK + 1 NEXT per key of
// the label's run whatever is asked -- the run's keys counted INDEPENDENTLY, by a raw iterator over the store -- and
// the NEXT that leaves the run is a miss on the neighbouring key; used bytes are a point read's; `keep` drops rows
// and moves no count.
static void check_label_row_scan(const Schema& s, const std::string& schema_path) {
  auto person = [&](int i) { return hex16(make_uid(s, "Person", uint64_t(i))); };
  // 5 Persons: firstName and creationDate on all (the enumeration the scan is compared with walks an index over
  // creationDate); lastName not on 3 and 4: a missing Split property, and no row in the later props group. Other
  // labels around Person: the scan must stop at its run.
  std::string r;
  for (int i = 1; i <= 5; i++) {
    r += "[\"v\",\"" + person(i) + "\",\"Person\",{\"id\":" + std::to_string(i) + ",\"firstName\":\"Bo\"";
    if (i != 3 && i != 4) r += ",\"lastName\":\"L" + std::to_string(i) + "\"";
    r += ",\"creationDate\":" + std::to_string(1000 * i);
    r += "}]\n";
  }
  for (auto& v : s.vertices())   // one vertex of every other label that has a string prop to store
    if (v.label != "Person" && v.label != "Tag")
      for (auto& p : v.props)
        if (p.name == "title" || p.name == "name" || p.name == "content") {
          r += "[\"v\",\"" + hex16(make_uid(s, v.label, 1)) + "\",\"" + v.label + "\",{\"id\":1,\"" + p.name + "\":\"x\"}]\n";
          break;
        }
  std::vector<std::string> ps;
  for (auto& p : s.vertex("Person").props) ps.push_back(p.name);
  json rest = json::array();
  for (auto& p : ps)
    if (p != "firstName" && p != "creationDate") rest.push_back(p);
  // grouped: firstName and creationDate in group 0, every other prop in group 1
  json grouped = {{"group", "owner"}, {"groups", {{"Person", json::array({json::array({"firstName", "creationDate"}), rest})}}}};
  // the keys of Person's run per layout: Split one per stored prop (3 + 3 + 2 + 2 + 3), Condensed one per vertex,
  // grouped one per vertex + one per vertex with a group-1 prop (3), Chunked one chunk
  std::vector<std::tuple<std::string, json, int64_t>> layouts = {
      {"split", {{"group", "item"}}, 13}, {"condensed", {{"group", "owner"}}, 5}, {"grouped", grouped, 8},
      {"chunked", {{"group", "owner"}, {"owners", {{"pack", 65536}}}}, 1}};
  json all = json::array({"firstName", "lastName", "creationDate", "id"});
  auto scan = [&](const std::string& label, const json& props, const json* keep) {
    json st = {{"name", "out"}, {"op", "read_vertices_by_label"}, {"label", label}, {"props", props}};
    if (keep) st["keep"] = *keep;
    return json::array({st});
  };
  auto point = [&](const json& props) {
    return json::array({{{"name", "v"}, {"op", "read_vertices_by_label"}, {"label", "Person"}},
                        {{"name", "out"}, {"op", "read_vertex_props"}, {"vids", {{"ref", "v"}}}, {"props", props}, {"label", "Person"}}});
  };
  auto by_vid = [](json rows) {
    std::sort(rows.begin(), rows.end(), [](const json& a, const json& b) { return a["_vid"].dump() < b["_vid"].dump(); });
    return rows;
  };
  size_t cells = 0;
  for (auto& [name, comp, keys] : layouts) {
    std::string tag = "[label row scan] " + name;
    json j = {{"vertex_props", comp}, {"adjacency", {{"group", "item"}}}, {"direction", "both"}};
    CHECK(meta::Meta(s, meta::Config::from_json(j)).capabilities_for("Person").count("label_rows_one_run"),
          tag + ": the label does not advertise label_rows_one_run");
    std::string d = ingest_store(s, schema_path, r, "meta", j);
    // the independent count: Person's keys under 'v'·vtype, and the key just after the run, by a raw iterator
    int64_t raw_keys = 0, after_key = -1;
    {
      std::string prefix = std::string(1, 'v') + std::string(1, char(uint8_t(s.vertex("Person").id)));
      auto store = Store::open_read_only(d + "/store/db", StoreOptions());
      std::unique_ptr<rocksdb::Iterator> it(store->db()->NewIterator(rocksdb::ReadOptions()));
      for (it->Seek(prefix); it->Valid(); it->Next()) {
        if (it->key().starts_with(prefix)) raw_keys++;
        else { after_key = int64_t(it->key().size()); break; }
      }
    }
    CHECK(raw_keys == keys, tag + ": the store holds " + std::to_string(raw_keys) + " keys in Person's run, the test expects " + std::to_string(keys));
    CHECK(after_key > 0, tag + ": no key after Person's run (the test graph must surround it)");
    json pt = run(d, "meta", schema_path, point(all));
    json full = run(d, "meta", schema_path, scan("Person", all, nullptr));
    if (!full.contains("rows") || !pt.contains("rows") || full["steps"].empty()) {
      fails++; std::cerr << "FAIL: " << tag << ": no rows: " << full.dump().substr(0, 300) << " / " << pt.dump().substr(0, 200) << "\n";
      continue;
    }
    CHECK(full["rows"].size() == 5 && by_vid(full["rows"]) == by_vid(pt["rows"]),
          tag + ": the scan's rows differ from enumerate + read_vertex_props: " + full["rows"].dump().substr(0, 400) +
              " vs " + pt["rows"].dump().substr(0, 400));
    CHECK(std::is_sorted(full["rows"].begin(), full["rows"].end(),
                         [](const json& a, const json& b) { return a["_vid"].dump() < b["_vid"].dump(); }),
          tag + ": the scan's rows are not in key (vid) order");
    const json& c = full["steps"][0];
    CHECK(c["S"] == 1 && c["G"] == 0 && c["N"] == raw_keys && c["S_miss"] == 0,
          tag + ": expected 1 SEEK (a hit), 0 GET and one NEXT per key of the run (" + std::to_string(raw_keys) +
              "), got S " + c["S"].dump() + " S_miss " + c["S_miss"].dump() + " G " + c["G"].dump() + " N " + c["N"].dump());
    // the NEXT that leaves the run lands on the neighbouring key: one miss, that key's bytes in the miss lane
    CHECK(c["N_miss"] == 1 && c["bytes"]["next_miss_key"] == after_key,
          tag + ": the terminating NEXT: N_miss " + c["N_miss"].dump() + ", next_miss_key " +
              c["bytes"]["next_miss_key"].dump() + " (the key after the run has " + std::to_string(after_key) + " B)");
    // used bytes: what a point read of the same props counts as used (Chunked counts none on either path)
    for (const json& props : {all, json::array({"lastName"}), json::array({"id"})}) {
      json sc = run(d, "meta", schema_path, scan("Person", props, nullptr)), po = run(d, "meta", schema_path, point(props));
      if (name == "chunked") continue;
      for (const char* k : {"used_key", "used_value"})
        CHECK(sc["steps"][0]["bytes"][k] == po["steps"][1]["bytes"][k],
              tag + " props " + props.dump() + ": " + k + " " + sc["steps"][0]["bytes"][k].dump() + " vs the point read's " +
                  po["steps"][1]["bytes"][k].dump());
      CHECK(by_vid(sc["rows"]) == by_vid(po["rows"]), tag + " props " + props.dump() + ": rows differ from the point read");
      // one prop asked: the same keys crossed, the same bytes read
      for (const char* k : {"S", "G", "N"})
        CHECK(sc["steps"][0][k] == c[k], tag + " props " + props.dump() + ": moved " + k);
      for (const char* k : {"scan_key", "scan_value"})
        CHECK(sc["steps"][0]["bytes"][k] == c["bytes"][k], tag + " props " + props.dump() + ": moved " + k + " bytes");
    }
    // keep: rows of the kept vertices only (a duplicate and a vid that is no Person change nothing), no count moved
    json keep = json::array({person(4), person(2), person(4), hex16(make_uid(s, "Forum", 1))});
    json kept = run(d, "meta", schema_path, scan("Person", all, &keep));
    json want = json::array();
    for (auto& row : by_vid(pt["rows"]))
      if (row["_vid"] == person(2) || row["_vid"] == person(4)) want.push_back(row);
    CHECK(kept.contains("rows") && by_vid(kept["rows"]) == want, tag + ": keep returned " + kept.dump().substr(0, 300));
    if (kept.contains("steps") && !kept["steps"].empty()) {
      for (const char* k : {"S", "G", "N", "N_miss"})
        CHECK(kept["steps"][0][k] == c[k], tag + ": keep moved " + k);
      for (const char* k : {"scan_key", "scan_value", "next_miss_key"})
        CHECK(kept["steps"][0]["bytes"][k] == c["bytes"][k], tag + ": keep moved " + k + " bytes");
    }
    json none = json::array();
    json empty_keep = run(d, "meta", schema_path, scan("Person", all, &none));
    CHECK(empty_keep.contains("rows") && empty_keep["rows"].empty() && empty_keep["steps"][0]["N"] == c["N"],
          tag + ": an empty keep must give no row and the same scan");
    // a label with no vertex: the SEEK lands outside its run (a miss), nothing more is read
    json empty = run(d, "meta", schema_path, scan("Tag", json::array({"name"}), nullptr));
    CHECK(empty.contains("rows") && empty["rows"].empty() && empty["steps"][0]["S"] == 1 && empty["steps"][0]["S_miss"] == 1 &&
              empty["steps"][0]["N"] == 0 && empty["steps"][0]["bytes"]["scan_value"] == 0,
          tag + ": an empty label: " + empty.dump().substr(0, 300));
    // keep without props is refused (the enumeration walks an index, not the rows)
    json bad = {{"name", "out"}, {"op", "read_vertices_by_label"}, {"label", "Person"}, {"keep", keep}};
    bool threw = false;
    json rb;
    try { rb = run(d, "meta", schema_path, json::array({bad})); } catch (const std::exception&) { threw = true; }
    CHECK(threw || (rb.contains("error") && !rb["error"].is_null()), tag + ": keep without props was not refused");
    cells++;
    std::cerr << tag << ": rows == enumerate + point read; S 1, G 0, N " << c["N"] << " == the run's " << raw_keys
              << " keys (raw count); 1 miss on the next key (" << after_key << " B); used bytes == the point read's; "
              << "one prop, keep and an empty keep move no count; an empty label = one SEEK miss\n";
  }
  CHECK(cells == layouts.size(), "[label row scan] " + std::to_string(cells) + " of " + std::to_string(layouts.size()) + " layouts checked");
  // the formats with no scan of a label's rows refuse keep loudly
  std::string dn = ingest_store(s, schema_path, graph(s), "nebula", json::object());
  json keep1 = json::array({person(1)});
  bool threw = false;
  json rn;
  try { rn = run(dn, "nebula", schema_path, json::array({{{"name", "out"}, {"op", "read_vertices_by_label"}, {"label", "Person"}, {"keep", keep1}}})); }
  catch (const std::exception&) { threw = true; }
  CHECK(threw || (rn.contains("error") && !rn["error"].is_null()), "[label row scan] nebula did not refuse keep");
}

// census decodes every value whole (empty_value): a packed adjacency chunk (lists::ChunkView) with one trailing byte
// must throw there, though the lazy view builds its ordinals and its edge props only on first use. Both of the view's
// forms: own placement (list_ordinals, the default own chunk) and inline edge props (the grid's Chunked adjacency).
static void check_census_rejects_corrupt_chunk(const Schema& s, const std::string& schema_path) {
  json packed = {{"group", "owner"}, {"owners", {{"pack", 65536}}}, {"split", "never"}};
  json own = packed;
  own["edge_props"] = "own";
  for (auto& [form, adj] : std::vector<std::pair<std::string, json>>{{"own list_ordinals", own}, {"inline", packed}}) {
    json cfg = meta_corner("pertype_csr", {{"adjacency", adj}});
    std::string d = ingest_store(s, schema_path, graph_wide(s), "meta", cfg);
    meta::Meta m(s, meta::Config::from_json(cfg));
    auto store = Store::open_read_only(d + "/store/db", StoreOptions());
    std::unique_ptr<rocksdb::Iterator> it(store->db()->NewIterator(rocksdb::ReadOptions()));
    size_t chunks = 0, caught = 0;
    for (it->SeekToFirst(); it->Valid(); it->Next()) {
      std::string_view k(it->key().data(), it->key().size());
      if (m.part_scheme()->name(m.part_scheme()->part_of(k)).rfind("adj:", 0) != 0) continue;
      std::string v(it->value().data(), it->value().size());
      bool intact_ok = true;
      try { m.empty_value(k, v); } catch (const std::exception&) { intact_ok = false; }
      CHECK(intact_ok, "census " + form + ": an intact adjacency chunk throws");
      try { m.empty_value(k, v + '\0'); } catch (const std::exception&) { caught++; }
      chunks++;
    }
    CHECK(chunks > 0 && caught == chunks, "census " + form + ": " + std::to_string(caught) + " of " +
                                              std::to_string(chunks) + " adjacency chunks with a trailing byte rejected");
    std::cerr << "[census-corrupt] " << form << ": " << caught << " of " << chunks
              << " adjacency chunks with a trailing byte rejected\n";
  }
}

// the chunk header stamp (config.h kChunkHeader): Config writes it on every lists chunk and nowhere else, and a store
// whose manifest lacks it (ingested before the skip16 header: the old header) is refused by the one check the session and census
// both run (ingest/ingest.h check_store_codec)
static void check_chunk_header_stamp(const Schema& s, const std::string& schema_path) {
  json packed = {{"group", "owner"}, {"owners", {{"pack", 65536}}}, {"split", "never"}};
  json own = packed;
  own["edge_props"] = "own";
  json condensed = {{"group", "owner"}, {"split", "never"}};
  size_t refused = 0;
  for (auto& [form, adj, stamped] : std::vector<std::tuple<std::string, json, bool>>{
           {"inline", packed, true}, {"own list_ordinals", own, true},
           {"condensed", condensed, false}}) {
    json cfg = meta_corner("pertype_csr", {{"adjacency", adj}});
    CHECK(meta::Config::from_json(cfg).to_json()["adjacency"].contains("chunk_header") == stamped,
          "chunk_header " + form + ": written " + (stamped ? "" : "not ") + "expected");
    std::string d = ingest_store(s, schema_path, graph_wide(s), "meta", cfg);
    json m = read_manifest(d + "/store");
    meta::Meta fmt(s, meta::Config::from_json(m.at("value_codec")));
    bool ok = true;
    try { check_store_codec(m, fmt, d, "test"); } catch (const std::exception&) { ok = false; }
    CHECK(ok, "chunk_header " + form + ": a fresh store is refused by check_store_codec");
    if (!stamped) continue;
    json old = m;
    old["value_codec"]["adjacency"].erase("chunk_header");   // what a store from before the stamp records
    meta::Meta ofmt(s, meta::Config::from_json(old.at("value_codec")));   // it still parses: the default fills it
    for (const char* who : {"session", "census"}) {
      bool threw = false;
      try { check_store_codec(old, ofmt, d, who); } catch (const std::exception& e) {
        threw = std::string(e.what()).find("re-ingest") != std::string::npos;
      }
      CHECK(threw, "chunk_header " + form + ": a manifest without the stamp is not refused (" + who + ")");
      refused += threw;
    }
  }
  CHECK(refused == 4, "chunk_header: " + std::to_string(refused) + " of 4 old-manifest openings refused");
  std::cerr << "[chunk-header-stamp] written on inline + own list_ordinals, not on condensed; " << refused
            << " of 4 old-manifest openings refused\n";
}

// a store ingested with no layout named is partitioned (store_config.json's rocksdb.index_filter_blocks): the manifest says so and every SST
// carries a two-level index (a top level over partitions) and a filter
static void check_default_ingest_partitioned(const Schema& s, const std::string& schema_path) {
  std::string d = ingest_store(s, schema_path, graph(s), "meta", meta_corner("nebula"));
  std::ifstream mf(d + "/store/manifest.json");
  json m = json::parse(mf);
  CHECK(m.value("index_filter_blocks", "") == "partitioned",
        "a default ingest's manifest says index_filter_blocks " + m.value("index_filter_blocks", "<none>"));
  StoreOptions so = store_options_of(m, d + "/store");
  CHECK(so.build_settings_known && so.bloom_bits_per_key == rocksdb_setting<double>("bloom_bits_per_key") &&
            so.partition_bytes == rocksdb_setting<size_t>("partition_bytes"),
        "a new store's manifest records the bloom bits and the partition size it was built with");
  CHECK(so.rocksdb_record()["bloom_bits_per_key"] == so.bloom_bits_per_key, "the run record takes them from the store");
  auto store = Store::open_read_only(d + "/store/db", so);
  rocksdb::TablePropertiesCollection props;
  CHECK(store->db()->GetPropertiesOfAllTables(&props).ok() && !props.empty(), "a default ingest wrote no SST");
  // each SST as store_config.json decides: a two-level index, a filter only when bloom_bits_per_key > 0 (0: none), and
  // the decided codec (RocksDB names it in the SST's own properties: "Snappy" for snappy)
  const bool want_filter = rocksdb_setting<double>("bloom_bits_per_key") > 0;
  std::string want_codec = rocksdb_setting<std::string>("compression");
  CHECK(m.value("compression", "") == want_codec, "a default ingest's manifest says compression " + m.value("compression", "<none>"));
  rocksdb::CompressionType ct = compression_by_name(want_codec).type;
  std::string want_name = ct == rocksdb::kNoCompression ? "NoCompression" : ct == rocksdb::kSnappyCompression ? "Snappy"
                          : ct == rocksdb::kLZ4Compression ? "LZ4" : ct == rocksdb::kZSTD ? "ZSTD" : "?";
  size_t as_decided = 0;
  for (auto& [f, tp] : props)
    as_decided += tp->top_level_index_size > 0 && (tp->filter_size > 0) == want_filter && tp->compression_name == want_name;
  CHECK(as_decided == props.size(), "default ingest: " + std::to_string(as_decided) + " of " + std::to_string(props.size()) +
                                        " SSTs with a two-level index, " + (want_filter ? "a" : "no") + " filter and " +
                                        want_name + " blocks");
  std::cerr << "[default-ingest] " << as_decided << " of " << props.size() << " SSTs partitioned, "
            << (want_filter ? "with a filter, " : "no filter, ") << want_name << "\n";
}

TEST_CASE(test_meta) {
  std::string schema_path = ldbc_schema_dir() + "/schema.json";
  Schema s = Schema::load(schema_path);
  golden_begin();
  check_spec_roundtrip();
  check_validate();
  for (auto& [corner, ref] : std::vector<std::pair<std::string, std::string>>{
           {"dgraph", "dgraph"}, {"nebula", "nebula"}, {"dgneb", "dgneb"}})
    check_reads(s, schema_path, corner, ref);
  check_interior(s, schema_path);
  check_two_label_group(s, schema_path);
  check_index(s, schema_path);
  check_packed_vertex(s, schema_path);
  check_packed_adjacency(s, schema_path);
  check_split(s, schema_path);
  check_singleton_update_index(s, schema_path);
  check_per_type_spec(s);
  check_per_direction_spec(s);
  check_grouped_spec(s);
  check_per_type_store(s, schema_path);
  check_per_direction_store(s, schema_path);
  check_per_direction_sizes(s, schema_path);
  check_chunk_rekey_writes(s, schema_path);
  check_pertype_csr_selfloop_delete(s, schema_path);
  check_update_ops(s, schema_path);
  check_placement(s, schema_path);
  check_op_rules(s, schema_path);
  check_parallel_refused(s, schema_path);
  check_far_label(s, schema_path);
  check_label_tags(s, schema_path);
  check_label_row_scan(s, schema_path);
  check_census_rejects_corrupt_chunk(s, schema_path);
  check_chunk_header_stamp(s, schema_path);
  check_default_ingest_partitioned(s, schema_path);
  golden_end();
  if (fails == 0)
    std::cerr << "PASS: meta-v4 at the engine-shaped corners does dgraph's, nebula's, dgneb's (its saved copy), pertype_csr's work with "
                 "their answers (value bytes equal where the encodings are the same)\n";
  return fails ? 1 : 0;
}

}  // namespace
