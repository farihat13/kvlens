// Dgraph refuses an index read on an index its schema does not declare (never an empty answer from an empty key
// range), as the other formats do at Schema::index_decl. Each schema (canonical, label_only) ingests 60 Persons into a
// temp store; the index reads run on dgraph and its engine twin: on label_only every one must throw "no index", on
// canonical every one must answer. The engine's sort_topk over its own id index answers on both.
#include <filesystem>
#include <iostream>
#include <map>
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

static const int kN = 60;

static json one_step(json step) {
  step["name"] = "s";
  return {{"plan", "p"}, {"for", "test_dgraph_index_refusal"}, {"params", {{"vids", json::object()}}},
          {"steps", json::array({step})}, {"output", "s"}};
}

TEST_CASE(test_dgraph_index_refusal) {
  const std::string ldbc = ldbc_schema_dir() + "/";
  json vids = json::array();
  for (int i = 1; i <= kN; i++) vids.push_back(hex16(uint64_t(i)));
  json rows = json::array();
  for (int i = 1; i <= kN; i++) rows.push_back({{"_vid", hex16(uint64_t(i))}});
  // (format, the read, the step): the five entry points that read an index key, and the engine's own id index
  std::vector<std::tuple<std::string, std::string, json>> reads = {
      {"dgraph", "read_index_eq", {{"op", "read_index_eq"}, {"label", "Person"}, {"prop", "firstName"}, {"value", "F7"}}},
      {"dgraph", "read_index_eq (bare)", {{"op", "read_index_eq"}, {"prop", "firstName"}, {"value", "F7"}}},
      {"dgraph", "read_index_range", {{"op", "read_index_range"}, {"labels", {"Person"}}, {"prop", "creationDate"},
                                      {"lo", 0}, {"hi", 40000}}},
      {"dgraph", "read_top_k_by_index", {{"op", "read_top_k_by_index"}, {"vids", {{"param", "vids"}}}, {"labels", {"Person"}},
                                         {"prop", "creationDate"}, {"k", 5}}},
      {"dgraph_engine", "sort_topk", {{"op", "sort_topk"}, {"rows", {{"param", "rows"}}}, {"prop", "creationDate"}, {"k", 5}}},
      // the engine's own id index: built whatever the schema declares, so it answers on both schemas
      {"dgraph_engine", "sort_topk over id", {{"op", "sort_topk"}, {"rows", {{"param", "rows"}}}, {"prop", "id"},
                                              {"index", "id"}, {"k", 3}}},
      {"dgraph_engine", "ineq_root_scan", {{"op", "ineq_root_scan"}, {"prop", "creationDate"}, {"fname", "lt"},
                                           {"hi", 40000}}}};
  char tmpl[] = "/tmp/test_dgraph_index_refusal_XXXXXX";
  std::string dir = kvt::temp_dir(tmpl);
  int refused = 0, answered = 0;
  for (std::string sname : {"schema.json", "schema_label_only.json"}) {
    Schema s = Schema::load(ldbc + sname);
    bool declared = sname == "schema.json";
    for (std::string fmt_name : {"dgraph", "dgraph_engine"}) {
      // the engine twin reads dgraph's named facets; the fair format keeps its default codec
      json opts = fmt_name == "dgraph_engine" ? json{{"props_codec", "named"}} : json::object();
      std::unique_ptr<Format> fmt = make_format(fmt_name, s, opts);
      MemSink sink;
      for (int i = 1; i <= kN; i++)
        fmt->add_vertex(sink, uint64_t(i), "Person",
                        Value::row({{"id", Value(int64_t(i))}, {"firstName", Value("F" + std::to_string(i % 10))}, {"lastName", Value("L")},
                                    {"creationDate", Value(int64_t(1000 * i))}}));
      fmt->finalize(sink);
      StoreOptions so;
      auto store = Store::open_read_write(dir + "/" + fmt_name + "-" + sname, so, true);
      for (auto& [k, v] : sink.kv) store->db()->Put(rocksdb::WriteOptions(), k, v);
      for (auto& [rfmt, what, step] : reads) {
        if (rfmt != fmt_name) continue;
        json params = {{"vids", vids}, {"rows", rows}};
        std::string err;
        size_t n = 0;
        try {
          Plan plan = bind_plan(one_step(step), &params, fmt->capabilities(), s);
          json resp = execute(*fmt, *store, plan, RunConfig{});
          if (resp.contains("error") && !resp["error"].is_null()) err = resp["error"].dump();
          else n = resp.contains("rows") ? resp["rows"].size() : 0;
        } catch (const std::exception& e) {
          err = e.what();
        }
        if (declared || what == "sort_topk over id") {
          CHECK(err.empty() && n > 0, fmt_name << " " << what << " on " << sname << ": wants an answer, got "
                                                << (err.empty() ? std::to_string(n) + " rows" : err.substr(0, 160)));
          answered += err.empty() && n > 0;
        } else {
          CHECK(err.find("no index") != std::string::npos,
                fmt_name << " " << what << " on " << sname << ": wants a refusal, got "
                         << (err.empty() ? std::to_string(n) + " rows" : err.substr(0, 160)));
          refused += err.find("no index") != std::string::npos;
        }
      }
    }
  }
  std::filesystem::remove_all(dir);
  std::cout << "[dgraph-index-refusal] " << answered << " of 8 answered (6 reads on canonical, the id walk on both), "
            << refused << " of 6 refused on label_only\n";
  if (fails) { std::cerr << fails << " of " << checks << " checks failed\n"; return 1; }
  std::cout << "test_dgraph_index_refusal ok (" << checks << " checks)\n";
  return 0;
}

}  // namespace
