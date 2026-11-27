// read_neighbors appends each source's items to its morsel's list (pool.h run_range_append): the output must be the
// per-source order exactly, at every P and morsel size. Each format ingests 300 Persons, their knows edges and one hub
// (Person 1 knows every other); a hop (pairs with an edge prop, and bare vids both ways) over every vid with repeats
// runs at P=1 and at P=8 with task batches 1, 3 and 64, and must equal the reference: each distinct source read alone,
// the results joined in input order. `--control` changes one P=8 result: the test must then fail.
#include <filesystem>
#include <iostream>
#include <map>
#include <set>
#include <string>
#include <unistd.h>

#include "exec/executor.h"
#include "formats/format.h"
#include "ir/plan.h"
#include "schema/schema.h"
#include "store/rocksdb_store.h"
#include "util/vid.h"
#include "value/value.h"
#include "repo_paths.h"
#include "harness.h"

namespace {

using namespace evalcpp;


using MemSink = kvt::MemSink;

static const int kN = 300;

static json one_step(const std::string& name, json step) {
  step["name"] = "s";
  return {{"plan", name}, {"for", "test_neighbors_append_order"}, {"params", {{"vids", json::object()}}},
          {"steps", json::array({step})}, {"output", "s"}};
}

static std::string rows_of(Format& fmt, Store& store, const Schema& s, const json& ir, const json& vids, int P, size_t batch) {
  RunConfig cfg;
  cfg.P = P;
  cfg.task_batch = batch;
  json params = {{"vids", vids}};
  try {
    Plan plan = bind_plan(ir, &params, fmt.capabilities(), s);
    json resp = execute(fmt, store, plan, cfg);
    return resp.contains("rows") ? resp["rows"].dump() : "<no rows>";
  } catch (const std::exception& e) {
    return std::string("<error> ") + e.what();
  }
}

TEST_CASE(test_neighbors_append_order) {
  bool control = argc > 1 && std::string(argv[1]) == "--control";
  Schema s = Schema::load(ldbc_schema_dir() + "/schema.json");
  json vids = json::array();
  for (int i = kN; i >= 1; i--) vids.push_back(hex16(uint64_t(i)));   // the hub last: its morsel is not the first
  for (int i = 1; i <= kN; i += 7) vids.push_back(hex16(uint64_t(i)));   // repeats: distinct sources keep first-seen order
  std::vector<json> plans = {
      one_step("pairs", {{"op", "read_neighbors"}, {"srcs", {{"param", "vids"}}}, {"etype", "knows"}, {"dir", "out"},
                         {"edge_props", {"creationDate"}}, {"pairs", true}}),
      one_step("bare", {{"op", "read_neighbors"}, {"srcs", {{"param", "vids"}}}, {"etype", "knows"}, {"dir", "both"}})};
  char tmpl[] = "/tmp/test_neighbors_append_order_XXXXXX";
  std::string dir = kvt::temp_dir(tmpl);
  int compared = 0;
  bool controlled = false;
  for (std::string name : {"dgraph", "nebula", "pertype_csr", "arangodb", "meta"}) {
    std::unique_ptr<Format> fmt = make_format(name, s, json::object());
    MemSink sink;
    for (int i = 1; i <= kN; i++)
      fmt->add_vertex(sink, uint64_t(i), "Person",
                      Value::row({{"firstName", Value("F" + std::to_string(i))}, {"lastName", Value("L")},
                                  {"creationDate", Value(int64_t(1000 * i))}}));
    for (int i = 1; i <= kN; i++) {
      std::set<int> dsts = {i % kN + 1, (i * 7) % kN + 1};
      if (i == 1)
        for (int j = 2; j <= kN; j++) dsts.insert(j);   // the hub: 299 edges
      for (int j : dsts)
        if (j != i) fmt->add_edge(sink, "knows", uint64_t(i), uint64_t(j), Value::row({{"creationDate", Value(int64_t(i + j))}}),
                                  "Person", "Person");
    }
    fmt->finalize(sink);
    StoreOptions so;
    auto store = Store::open_read_write(dir + "/" + name, so, true);
    for (auto& [k, v] : sink.kv) store->db()->Put(rocksdb::WriteOptions(), k, v);
    for (auto& ir : plans) {
      std::string what = name + " " + ir["plan"].get<std::string>();
      // the reference: each distinct source alone, in first-seen order, the rows joined
      std::set<std::string> seen;
      json ref = json::array();
      bool err = false;
      for (auto& v : vids) {
        if (!seen.insert(v.get<std::string>()).second) continue;
        std::string one = rows_of(*fmt, *store, s, ir, json::array({v}), 1, 1);
        if (one.rfind("<", 0) == 0) { err = true; ref = one; break; }
        for (auto& r : json::parse(one)) ref.push_back(r);
      }
      if (err) {
        std::cout << "  skipped " << what << ": " << ref.get<std::string>().substr(0, 100) << "\n";
        continue;
      }
      std::string want = ref.dump();
      CHECK(ref.size() > 400, what << ": the reference has " << ref.size() << " rows (the hub alone has 299)");
      for (auto [P, batch] : std::vector<std::pair<int, size_t>>{{1, 1}, {8, 1}, {8, 3}, {8, 64}}) {
        std::string got = rows_of(*fmt, *store, s, ir, vids, P, batch);
        if (control && !controlled && P == 8) { got += " "; controlled = true; }
        CHECK(got == want, what << " at P=" << P << " batch " << batch << ": the rows differ from the per-source order ("
                                << got.size() << " vs " << want.size() << " chars)");
        compared++;
      }
    }
  }
  std::filesystem::remove_all(dir);
  CHECK(compared >= 40, "only " << compared << " (format, plan, P, batch) cells compared");
  if (fails) { std::cerr << fails << " of " << checks << " checks failed\n"; return 1; }
  std::cout << "test_neighbors_append_order ok (" << compared << " cells against the per-source order, " << checks
            << " checks)\n";
  return 0;
}

}  // namespace
