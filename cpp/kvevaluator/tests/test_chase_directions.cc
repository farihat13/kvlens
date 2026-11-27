// read_chase over both directions (formats/common.h chase_over): a node's first neighbour is its first direction's,
// out then in, and a later direction is read only when the earlier ones have none. Each format ingests six Persons
// and knows edges 1->2, 2->3, 5->4; a one-hop chase from 1 (out has a neighbour) must cost what the out chase costs,
// and one from 4 (out has none) what the out and in chases cost together, with the out / in answer. The engine twins
// keep the engine's plan: every direction read, so both from 1 costs out + in. `--control` expects the old rule
// (every direction read) on the formats: the test must then fail.
#include <filesystem>
#include <iostream>
#include <string>

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

// one chase step's rows and counts {S, G, N}
static json chase(Format& fmt, Store& store, const Schema& s, int src, const char* dir) {
  json ir = {{"plan", std::string("chase_") + dir}, {"for", "test_chase_directions"}, {"params", json::object()},
             {"steps", json::array({{{"name", "c"}, {"op", "read_chase"}, {"srcs", json::array({hex16(uint64_t(src))})},
                                     {"etype", "knows"}, {"dir", dir}, {"max_hops", 1}}})},
             {"output", "c"}};
  json params = json::object();
  Plan plan = bind_plan(ir, &params, fmt.capabilities(), s);
  RunConfig cfg;
  json resp = execute(fmt, store, plan, cfg);
  return {{"rows", resp["rows"]}, {"S", resp["steps"][0]["S"]}, {"G", resp["steps"][0]["G"]}, {"N", resp["steps"][0]["N"]}};
}

static int64_t sum(const json& a, const json& b, const char* c) { return a[c].get<int64_t>() + b[c].get<int64_t>(); }

TEST_CASE(test_chase_directions) {
  bool control = argc > 1 && std::string(argv[1]) == "--control";
  Schema s = Schema::load(ldbc_schema_dir() + "/schema.json");
  char tmpl[] = "/tmp/test_chase_directions_XXXXXX";
  std::string dir = kvt::temp_dir(tmpl);
  int formats = 0;
  for (std::string name : {"dgraph", "nebula", "pertype_csr", "arangodb", "meta", "dgraph_engine", "nebula_engine",
                           "arangodb_engine"}) {
    bool twin = name.find("_engine") != std::string::npos;
    // the engine stores hold the engines' own edge-property codec (named facets)
    json opts = twin ? json{{"props_codec", "named"}} : json::object();
    std::unique_ptr<Format> fmt = make_format(name, s, opts);
    MemSink sink;
    for (int i = 1; i <= 6; i++)
      fmt->add_vertex(sink, uint64_t(i), "Person",
                      Value::row({{"id", Value(int64_t(i))}, {"firstName", Value("F" + std::to_string(i))}, {"lastName", Value("L")},
                                  {"creationDate", Value(int64_t(1000 * i))}}));
    for (auto [a, b] : std::vector<std::pair<int, int>>{{1, 2}, {2, 3}, {5, 4}})
      fmt->add_edge(sink, "knows", uint64_t(a), uint64_t(b), Value::row({{"creationDate", Value(int64_t(a + b))}}),
                    "Person", "Person");
    fmt->finalize(sink);
    StoreOptions so;
    auto store = Store::open_read_write(dir + "/" + name, so, true);
    for (auto& [k, v] : sink.kv) store->db()->Put(rocksdb::WriteOptions(), k, v);
    // from 1: out has a neighbour, so the in direction is not read
    json b1 = chase(*fmt, *store, s, 1, "both"), o1 = chase(*fmt, *store, s, 1, "out"), i1 = chase(*fmt, *store, s, 1, "in");
    CHECK(b1["rows"] == o1["rows"], name << ": both from 1 answers " << b1["rows"].dump() << ", out " << o1["rows"].dump());
    for (const char* c : {"S", "G", "N"}) {
      int64_t want = control || twin ? sum(o1, i1, c) : o1[c].get<int64_t>();
      CHECK(b1[c].get<int64_t>() == want, name << ": both from 1 costs " << c << " " << b1[c] << ", want " << want);
    }
    // from 4: out has none, so the in direction gives the neighbour; both are read
    json b4 = chase(*fmt, *store, s, 4, "both"), o4 = chase(*fmt, *store, s, 4, "out"), i4 = chase(*fmt, *store, s, 4, "in");
    CHECK(b4["rows"] == i4["rows"], name << ": both from 4 answers " << b4["rows"].dump() << ", in " << i4["rows"].dump());
    CHECK(b4["rows"].dump().find(hex16(5)) != std::string::npos, name << ": both from 4 must reach 5, got " << b4["rows"].dump());
    for (const char* c : {"S", "G", "N"})
      CHECK(b4[c].get<int64_t>() == sum(o4, i4, c), name << ": both from 4 costs " << c << " " << b4[c] << ", want out + in "
                                                          << sum(o4, i4, c));
    // the case must bite: the in read from 1 costs something
    CHECK(i1["S"].get<int64_t>() + i1["G"].get<int64_t>() + i1["N"].get<int64_t>() > 0, name << ": the in chase from 1 reads nothing");
    formats++;
  }
  std::filesystem::remove_all(dir);
  CHECK(formats == 8, "only " << formats << " formats checked");
  if (fails) { std::cerr << fails << " of " << checks << " checks failed\n"; return 1; }
  std::cout << "test_chase_directions ok (" << formats << " formats and twins, " << checks << " checks)\n";
  return 0;
}

}  // namespace
