// `kvevaluator caps --ops`: per op name, "flow", "kv" or "neither" on a format, in the executor's order. The
// listing tests/unit/check_plan_ops_supported.py reads: meta at a day-index corner runs index_range_buckets
// (it lacked it once), so does dgraph, a flow op is flow everywhere, an unknown name is neither, an empty
// name is refused.
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "cli/commands.h"
#include "meta_corners.h"
#include "repo_paths.h"
#include "harness.h"

namespace {

using namespace evalcpp;


static const std::string kSchema = ldbc_schema_dir() + "/schema.json";
static const std::string kOps = "index_range_buckets,read_index_range,filter,insert_vertex,no_such_op";

// cmd_caps with its stdout captured: (rc, the parsed output, or null when it printed none)
static std::pair<int, json> caps(const std::vector<std::string>& args) {
  std::ostringstream buf;
  std::streambuf* old = std::cout.rdbuf(buf.rdbuf());
  int rc = cmd_caps(args);
  std::cout.rdbuf(old);
  return {rc, buf.str().empty() ? json() : json::parse(buf.str())};
}

TEST_CASE(test_caps_ops) {
  auto [rc, m] = caps({"--schema", kSchema, "--format", "meta", "--codec-config", test::meta_corner("dgraph").dump(),
                       "--ops", kOps});
  CHECK(rc == 0, "meta caps --ops exits 0");
  CHECK(m["ops"]["index_range_buckets"] == "kv", "meta (corner_dgraph): index_range_buckets is a kv op");
  CHECK(m["ops"]["read_index_range"] == "kv", "meta: read_index_range is a kv op");
  CHECK(m["ops"]["filter"] == "flow", "filter is a flow op");
  CHECK(m["ops"]["insert_vertex"] == "kv", "a write op is a kv op");
  CHECK(m["ops"]["no_such_op"] == "neither", "an unknown name is neither");
  CHECK(m["ops"].size() == 5, "one answer per name");
  bool day = false;
  for (auto& c : m["capabilities"]) day = day || c == "unsorted_range_index";
  CHECK(day, "the day corner advertises unsorted_range_index (the cap whose body needs the op)");

  auto [rc2, d] = caps({"--schema", kSchema, "--format", "dgraph", "--ops", kOps});
  CHECK(rc2 == 0 && d["ops"]["index_range_buckets"] == "kv", "dgraph runs index_range_buckets");

  auto [rc3, n] = caps({"--schema", kSchema, "--format", "nebula"});
  CHECK(rc3 == 0 && !n.contains("ops"), "no --ops, no ops listing (the output is unchanged)");

  auto [rc4, e] = caps({"--schema", kSchema, "--format", "nebula", "--ops", "filter,"});
  CHECK(rc4 == 2, "an empty name in --ops is refused");

  if (fails) { std::cerr << fails << " failures\n"; return 1; }
  std::cout << "test_caps_ops ok\n";
  return 0;
}

}  // namespace
