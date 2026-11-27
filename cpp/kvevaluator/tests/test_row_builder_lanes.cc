// A reader's one row builder is shared by the lanes of its call: the same plan at P=1 and P=8 must give
// the same rows, in the same order. Each format ingests 300 Persons and their knows edges into a temp store; three
// reads (props of repeated vids, a hop with pairs and an edge prop, a label scan with props) run at both P with a task
// batch of 1, so the lanes really split the vids. `--control` changes one P=8 result: the test must then fail.
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
  return {{"plan", name}, {"for", "test_row_builder_lanes"}, {"params", {{"vids", json::object()}}},
          {"steps", json::array({step})}, {"output", "s"}};
}

TEST_CASE(test_row_builder_lanes) {
  bool control = argc > 1 && std::string(argv[1]) == "--control";   // the check must then fail
  Schema s = Schema::load(ldbc_schema_dir() + "/schema.json");
  json vids = json::array();
  for (int i = 1; i <= kN; i++) vids.push_back(hex16(uint64_t(i)));
  for (int i = kN; i >= 1; i -= 3) vids.push_back(hex16(uint64_t(i)));   // repeats: the distinct vids' rows fan back out
  std::vector<json> plans = {
      one_step("props", {{"op", "read_vertex_props"}, {"vids", {{"param", "vids"}}},
                         {"props", {"firstName", "id", "creationDate", "firstName"}}, {"label", "Person"}}),
      one_step("hop", {{"op", "read_neighbors"}, {"srcs", {{"param", "vids"}}}, {"etype", "knows"}, {"dir", "out"},
                       {"edge_props", {"creationDate"}}, {"pairs", true}}),
      one_step("scan", {{"op", "read_vertices_by_label"}, {"label", "Person"}, {"props", {"id", "lastName"}}})};
  char tmpl[] = "/tmp/test_row_builder_lanes_XXXXXX";
  std::string dir = kvt::temp_dir(tmpl);
  int compared = 0;
  for (std::string name : {"dgraph", "nebula", "pertype_csr", "arangodb", "meta"}) {
    std::unique_ptr<Format> fmt = make_format(name, s, json::object());
    MemSink sink;
    for (int i = 1; i <= kN; i++)
      fmt->add_vertex(sink, uint64_t(i), "Person",
                      Value::row({{"firstName", Value("F" + std::to_string(i))}, {"lastName", Value("L" + std::to_string(i % 17))},
                                  {"creationDate", Value(int64_t(1000 * i))}}));
    for (int i = 1; i <= kN; i++)
      for (int j : std::set<int>{i % kN + 1, (i * 7) % kN + 1})   // distinct, ascending
        if (j != i) fmt->add_edge(sink, "knows", uint64_t(i), uint64_t(j), Value::row({{"creationDate", Value(int64_t(i + j))}}),
                                     "Person", "Person");
    fmt->finalize(sink);
    StoreOptions so;
    auto store = Store::open_read_write(dir + "/" + name, so, true);
    for (auto& [k, v] : sink.kv) store->db()->Put(rocksdb::WriteOptions(), k, v);
    for (auto& ir : plans) {
      json params = {{"vids", vids}};
      std::string got[2];
      for (int pi = 0; pi < 2; pi++) {
        RunConfig cfg;
        cfg.P = pi ? 8 : 1;
        cfg.task_batch = 1;
        try {
          Plan plan = bind_plan(ir, &params, fmt->capabilities(), s);
          json resp = execute(*fmt, *store, plan, cfg);
          got[pi] = resp.contains("rows") ? resp["rows"].dump() : "<no rows>";
        } catch (const std::exception& e) {
          got[pi] = std::string("<error> ") + e.what();
        }
      }
      if (got[0].rfind("<error>", 0) == 0) {                      // not this format's op (a capability): both sides alike
        std::cout << "  skipped " << name << " " << ir["plan"].get<std::string>() << ": " << got[0].substr(0, 100) << "\n";
        CHECK(got[0] == got[1], name << " " << ir["plan"] << ": P=1 errs, P=8 does not");
        continue;
      }
      compared++;
      if (control && compared == 1) got[1] += " ";               // the control: one P=8 result changed
      CHECK(got[0] == got[1] && got[0].size() > 100,
            name << " " << ir["plan"] << ": P=1 and P=8 rows differ (" << got[0].size() << " vs " << got[1].size() << " chars)");
    }
  }
  std::filesystem::remove_all(dir);
  CHECK(compared >= 12, "only " << compared << " (format, read) cells compared");
  if (fails) { std::cerr << fails << " of " << checks << " checks failed\n"; return 1; }
  std::cout << "test_row_builder_lanes ok (" << compared << " cells at P=1 vs P=8, " << checks << " checks)\n";
  return 0;
}

}  // namespace
