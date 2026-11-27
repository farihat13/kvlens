#include <iostream>
#include <string>
#include <vector>

#include "cli/commands.h"
#include "cli/tally_version.h"
#include "formats/format.h"
#include "allocator.h"
#include "provenance.h"
#include "ops/kv_contract.h"

static int usage() {
  std::cerr << "usage: kvevaluator <command> [args]\n"
               "  --version                       build provenance as JSON\n"
               "  ingest --schema S --out DIR ... build store(s) from a record stream on stdin\n"
               "  run <request.json | ->          run one plan request, response on stdout\n"
               "  serve                           JSON lines: one request per line, one response per line\n"
               "  dump <store-dir>                  sha256 over every (key, value) in key order\n"
               "  checkpoint <store-dir> <arena>    hard-link write arena for the apply-writes mode\n"
               "  size <store-dir>                  physical SST split + cold-scan tickers as JSON\n"
               "  census <store-dir>                keys + bytes per (part, type, direction), read-only, as JSON\n"
               "  sst-props <store-dir>             per-SST table properties (index / filter / data bytes), JSON lines\n"
               "  records <--schema> <--root>       LDBC CSVs -> the neutral record stream (jsonl)\n"
               "  schema-path record|resolve <p>    a manifest's schema path rule on one path (JSON)\n"
               "  predict --gate <request.json>...  meta: each KV step priced from its truth card == the run, every lane\n"
               "  tally --schema S --spec P --records R  meta: per-part keys and bytes of a layout, no store (predict mode)\n"
               "  graph-stats --schema S --records R  the graph statistics predict mode reads (format-free, no store)\n"
               "  ops                               the KV-op contract: every op, its args and defaults, its refusals (JSON)\n";
  return 2;
}

int main(int argc, char** argv) {
  if (argc < 2) return usage();
  std::string cmd = argv[1];
  std::vector<std::string> args(argv + 2, argv + argc);
  try {
    evalcpp::allocator_init();   // tcmalloc: release rate 0; refuse to run on a link that kept another malloc
    if (cmd == "--version") {
      nlohmann::json p = evalcpp::provenance();
      p["key_roles_version"] = evalcpp::kKeyRolesVersion;   // the size --roles classifiers' version (the cache key)
      p["tally_version"] = evalcpp::kTallyVersion;   // the tally's version (the tally library's reuse key)
      p["key_layouts"] = evalcpp::key_layouts();   // the layouts this binary reads: a store's manifest must match
      p["engine_key_layouts"] = evalcpp::engine_key_layouts();   // the engine-fidelity formats', kept apart
      p["counting"] = evalcpp::counting_versions();   // the seam's and each format's counting versions (the capture reuse key)
      p["build_key"] = evalcpp::kBuildKey;   // the build cache entry it came from (run/build_cpp.py), "" for a build in place
      std::cout << p.dump(2) << "\n";
      return 0;
    }
    if (cmd == "ingest") return evalcpp::cmd_ingest(args);
    if (cmd == "run") return evalcpp::cmd_run(args);
    if (cmd == "serve") return evalcpp::cmd_serve(args);
    if (cmd == "dump") return evalcpp::cmd_dump(args);
    if (cmd == "checkpoint") return evalcpp::cmd_checkpoint(args);
    if (cmd == "size") return evalcpp::cmd_size(args);
    if (cmd == "census") return evalcpp::cmd_census(args);
    if (cmd == "sst-props") return evalcpp::cmd_sst_props(args);
    if (cmd == "records") return evalcpp::cmd_records(args);
    if (cmd == "caps") return evalcpp::cmd_caps(args);
    if (cmd == "schema-path") return evalcpp::cmd_schema_path(args);
    if (cmd == "predict") return evalcpp::cmd_predict(args);
    if (cmd == "tally") return evalcpp::cmd_tally(args);
    if (cmd == "graph-stats") return evalcpp::cmd_graph_stats(args);
    if (cmd == "ops") {   // ops/kv_contract.h: the table of inputs/contract/kv_ops.json, compiled in
      std::cout << evalcpp::kv::ops_json().dump(2) << "\n";
      return 0;
    }
  } catch (const std::exception& e) {
    std::cerr << "error: " << e.what() << "\n";
    return 1;
  }
  return usage();
}
