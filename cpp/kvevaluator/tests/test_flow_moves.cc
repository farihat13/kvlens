// A flow step takes its input by move when it is that input's last reader (executor.cc `owned`, Args::owned_list):
// its rows are handed on, and a renamed row it holds alone is re-shaped in place. That must never be seen by any other
// holder of the input or of its rows. Plans of flow ops over a param list of 50 rows, each run by the executor and
// compared with the answer written out by hand:
//   in place      derive (fresh rows) -> rename: the renamed rows
//   shared rows   derive -> filter (its rows shared with a list read later) -> rename: copies; the filter's rows keep x
//   two readers   derive -> filter and rename both read it: the first reader moves nothing
//   one ref twice concat(a = s1, b = s1): no move
//   consumers     filter / filter_in / dedup / dedup_rows / exclude / concat over owned inputs
// `--control` runs one plan with its answer changed: the test must then fail.
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


static const int kN = 50;

static json ref(const std::string& n) { return {{"ref", n}}; }
static json step(const std::string& name, const std::string& op, json args) {
  args["name"] = name;
  args["op"] = op;
  return args;
}
static json plan(const std::string& name, std::vector<json> steps, const std::string& output) {
  return {{"plan", name}, {"for", "test_flow_moves"}, {"params", {{"rows", json::object()}, {"vids", json::object()}}},
          {"steps", steps}, {"output", output}};
}

TEST_CASE(test_flow_moves) {
  bool control = argc > 1 && std::string(argv[1]) == "--control";
  Schema s = Schema::load(ldbc_schema_dir() + "/schema.json");
  std::unique_ptr<Format> fmt = make_format("dgraph", s, json::object());
  char tmpl[] = "/tmp/test_flow_moves_XXXXXX";
  std::string dir = kvt::temp_dir(tmpl);
  StoreOptions so;
  auto store = Store::open_read_write(dir + "/db", so, true);   // flow ops only: an empty store
  json rows = json::array(), vids = json::array();
  for (int i = 1; i <= kN; i++) {
    rows.push_back({{"_vid", hex16(uint64_t(i))}, {"v", i % 7}});
    vids.push_back(hex16(uint64_t(i)));
  }
  json params = {{"rows", rows}, {"vids", vids}};
  // the expected rows, by hand: derive copies v into x
  auto with_x = [&](const std::string& name) {
    json out = json::array();
    for (auto& r : rows) out.push_back({{"_vid", r["_vid"]}, {"v", r["v"]}, {name, r["v"]}});
    return out;
  };
  json x_rows = with_x("x"), y_rows = with_x("y");
  json derive_x = {{"rows", {{"param", "rows"}}}, {"into", "x"}, {"fn", {{"field", "v"}}}};
  json keep_all = {{"rows", ref("s1")}, {"pred", {{"eq", json::array({{{"field", "x"}}, {{"field", "v"}}})}}}};
  json to_y = {{"rows", ref("s1")}, {"names", {{"x", "y"}}}};
  auto cat = [](const json& a, const json& b) { json o = a; for (auto& r : b) o.push_back(r); return o; };
  json evens = json::array(), firsts = json::array();
  for (auto& r : x_rows) if (r["v"].get<int>() % 2 == 0) evens.push_back(r);
  std::vector<std::tuple<std::string, json, json>> cases = {
      {"in place", plan("in_place", {step("s1", "derive", derive_x), step("s2", "rename", to_y)}, "s2"), y_rows},
      {"shared rows",
       plan("shared", {step("s1", "derive", derive_x), step("s2", "filter", keep_all), step("s3", "rename", to_y),
                       step("s4", "concat", {{"a", ref("s3")}, {"b", ref("s2")}})}, "s4"),
       cat(y_rows, x_rows)},
      {"two readers",
       plan("two", {step("s1", "derive", derive_x), step("s2", "filter", keep_all), step("s3", "rename", to_y),
                    step("s4", "concat", {{"a", ref("s2")}, {"b", ref("s3")}})}, "s4"),
       cat(x_rows, y_rows)},
      {"one ref twice",
       plan("twice", {step("s1", "derive", derive_x), step("s2", "concat", {{"a", ref("s1")}, {"b", ref("s1")}})}, "s2"),
       cat(x_rows, x_rows)},
      {"consumers",
       plan("consumers",
            {step("s1", "derive", derive_x),
             step("s2", "filter", {{"rows", ref("s1")}, {"pred", {{"in", json::array({{{"field", "v"}}, {{"lit", json::array({0, 2, 4, 6})}}})}}}}),
             step("s3", "dedup_rows", {{"rows", ref("s2")}}),
             step("s4", "pluck", {{"rows", ref("s3")}, {"field", "_vid"}}),
             step("s5", "dedup", {{"items", ref("s4")}}),
             step("s6", "exclude", {{"items", ref("s5")}, {"remove", json::array()}}),
             step("s7", "filter_in", {{"rows", {{"param", "rows"}}}, {"field", "_vid"}, {"allowed", ref("s6")}}),
             step("s8", "derive", {{"rows", ref("s7")}, {"into", "x"}, {"fn", {{"field", "v"}}}}),
             step("s9", "concat", {{"a", ref("s8")}, {"b", json::array()}})},
            "s9"),
       evens},
  };
  int compared = 0;
  for (auto& [what, ir, want] : cases) {
    std::string got;
    try {
      Plan p = bind_plan(ir, &params, fmt->capabilities(), s);
      RunConfig cfg;
      cfg.emit_row_digest = true;          // the step-end digest walk runs too (a counts run's emit)
      json resp = execute(*fmt, *store, p, cfg);
      got = resp.contains("rows") ? resp["rows"].dump() : "<no rows>";
    } catch (const std::exception& e) {
      got = std::string("<error> ") + e.what();
    }
    std::string w = want.dump();
    if (control && compared == 0) w += " ";
    CHECK(got == w, what << ": got " << got.substr(0, 300) << "\n   want " << w.substr(0, 300));
    compared++;
  }
  std::filesystem::remove_all(dir);
  CHECK(compared == 5, "only " << compared << " plans ran");
  if (fails) { std::cerr << fails << " of " << checks << " checks failed\n"; return 1; }
  std::cout << "test_flow_moves ok (" << compared << " plans, " << checks << " checks)\n";
  return 0;
}

}  // namespace
