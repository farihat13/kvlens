// The codec clocks (codec/timers.h: DecodeTimer, the zstd_column decompress clock) run only when the run asks
// (run.decode_timing, or run.kv_timing): off, they take no clock and each step's codec block reports null. Checks, on
// Per-type CSR (its chunk reads go through the timed decoders): a DecodeTimer adds nothing with the switch off and adds
// time with it on; a hop over 300 Persons decodes chunks in every mode, and its codec block reads null (off), > 0
// (decode_timing), > 0 (kv_timing alone); the run echo names decode_timing only when on; the switch is off after a run.
// `--control` leaves the switch on for the "off" checks: the test must then fail.
#include <filesystem>
#include <iostream>
#include <map>
#include <set>
#include <string>
#include <unistd.h>

#include "codec/timers.h"
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

TEST_CASE(test_decode_timing) {
  bool control = argc > 1 && std::string(argv[1]) == "--control";
  // 1. the timer itself
  decode_timing_on() = control;
  codec_reset();
  { DecodeTimer t; volatile uint64_t x = 0; for (int i = 0; i < 100000; i++) x = x + i; }
  CHECK(codec_stats().decode_ns == 0, "a DecodeTimer with the switch off added " << codec_stats().decode_ns << " ns");
  decode_timing_on() = true;
  { DecodeTimer t; volatile uint64_t x = 0; for (int i = 0; i < 100000; i++) x = x + i; }
  CHECK(codec_stats().decode_ns > 0, "a DecodeTimer with the switch on added nothing");
  decode_timing_on() = false;

  // 2. a run in each mode
  Schema s = Schema::load(ldbc_schema_dir() + "/schema.json");
  std::unique_ptr<Format> fmt = make_format("pertype_csr", s, json::object());
  MemSink sink;
  for (int i = 1; i <= kN; i++)
    fmt->add_vertex(sink, uint64_t(i), "Person",
                    Value::row({{"firstName", Value("F" + std::to_string(i))}, {"lastName", Value("L")},
                                {"creationDate", Value(int64_t(1000 * i))}}));
  for (int i = 1; i <= kN; i++)
    for (int j : std::set<int>{i % kN + 1, (i * 7) % kN + 1})
      if (j != i) fmt->add_edge(sink, "knows", uint64_t(i), uint64_t(j), Value::row({{"creationDate", Value(int64_t(i + j))}}),
                                "Person", "Person");
  fmt->finalize(sink);
  char tmpl[] = "/tmp/test_decode_timing_XXXXXX";
  std::string dir = kvt::temp_dir(tmpl);
  StoreOptions so;
  auto store = Store::open_read_write(dir + "/db", so, true);
  for (auto& [k, v] : sink.kv) store->db()->Put(rocksdb::WriteOptions(), k, v);
  json vids = json::array();
  for (int i = 1; i <= kN; i++) vids.push_back(hex16(uint64_t(i)));
  json ir = {{"plan", "hop"}, {"for", "test_decode_timing"}, {"params", {{"vids", json::object()}}},
             {"steps", json::array({{{"name", "s"}, {"op", "read_neighbors"}, {"srcs", {{"param", "vids"}}}, {"etype", "knows"},
                                     {"dir", "out"}, {"edge_props", {"creationDate"}}, {"pairs", true}}})},
             {"output", "s"}};
  json params = {{"vids", vids}};
  int runs = 0;
  for (std::string mode : {"off", "decode_timing", "kv_timing"}) {
    json run = {{"cache", "asis"}};
    if (mode != "off") run[mode] = true;
    RunConfig cfg = RunConfig::from_json(run);
    if (control && mode == "off") cfg.decode_timing = true;   // the old always-on clocks
    Plan p = bind_plan(ir, &params, fmt->capabilities(), s);
    json resp = execute(*fmt, *store, p, cfg);
    runs++;
    uint64_t chunks = 0;
    bool any_null = false, any_timed = false;
    for (auto& st : resp["steps"]) {
      const json& c = st["codec"];
      chunks += c.value("chunks_decoded", uint64_t(0));
      if (c["decode_ns"].is_null() || c["decompress_ns"].is_null()) any_null = true;
      if (c["decode_ns"].is_number() && c["decode_ns"].get<uint64_t>() > 0) any_timed = true;
    }
    CHECK(chunks > 0, mode << ": the hop decoded no chunk (the test reads nothing timed)");
    bool echoed = resp["run"].contains("decode_timing");
    if (mode == "off") {
      CHECK(any_null && !any_timed, "off: the codec clocks are not null (" << resp["steps"][0]["codec"].dump() << ")");
      CHECK(!echoed, "off: the run echo names decode_timing");
    } else {
      CHECK(!any_null && any_timed, mode << ": the codec clocks read " << resp["steps"][0]["codec"].dump());
      CHECK(echoed, mode << ": the run echo does not name decode_timing");
    }
    CHECK(!decode_timing_on().load(), mode << ": the switch is still on after the run");
  }
  std::filesystem::remove_all(dir);
  CHECK(runs == 3, "only " << runs << " runs");
  if (fails) { std::cerr << fails << " of " << checks << " checks failed\n"; return 1; }
  std::cout << "test_decode_timing ok (" << runs << " runs, " << checks << " checks)\n";
  return 0;
}

}  // namespace
