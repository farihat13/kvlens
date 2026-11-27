// `kvevaluator caps --schema schema.json --format <name> [--codec-config <json>]`: the
// capability set this binary's format grants on that schema (store-wide + by_label), as JSON; `caps --vocabulary`: every
// capability name the bundle selector accepts; `--ops a,b,...`: per op name, how the executor would run it on this
// format ("flow", "kv" or "neither": exec/executor.cc's order), for tests/unit/check_plan_ops_supported.py. The planner's Python side
// (kvevaluator.formats.caps.caps_for) declares the same sets; tests/unit/check_caps_parity.py holds the
// two equal for every format x design -- a drift here is a plan selected on stale facts.
#include <fstream>
#include <iostream>
#include <stdexcept>

#include "cli/commands.h"
#include "formats/format.h"
#include "ops/flow.h"
#include "ir/plan.h"
#include "schema/schema.h"

namespace evalcpp {

int cmd_caps(const std::vector<std::string>& args) {
  std::string schema_path, format;
  json codec_config = json::object();
  std::vector<std::string> ops;
  bool ops_given = false;
  for (size_t i = 0; i < args.size(); i++) {
    const std::string& k = args[i];
    if (k == "--vocabulary") {                 // the names a bundle may use (inputs/contract/capabilities.json)
      json out = {{"vocabulary", json::array()}};
      for (auto& c : capability_vocabulary()) out["vocabulary"].push_back(c);
      std::cout << out.dump(2) << "\n";
      return 0;
    }
    if (k == "--ops" && i + 1 < args.size()) {   // comma list; an empty name is refused below
      ops_given = true;
      std::string list = args[++i];
      size_t start = 0;
      while (start <= list.size()) {
        size_t end = list.find(',', start);
        if (end == std::string::npos) end = list.size();
        ops.push_back(list.substr(start, end - start));
        start = end + 1;
      }
      continue;
    }
    if ((k == "--schema" || k == "--format" || k == "--codec-config") && i + 1 < args.size()) {
      const std::string& v = args[++i];
      if (k == "--schema") schema_path = v;
      else if (k == "--format") format = v;
      else codec_config = json::parse(v);
    } else {
      std::cerr << "caps: unknown arg " << k << "\n";
      return 2;
    }
  }
  if (schema_path.empty() || format.empty()) {
    std::cerr << "usage: kvevaluator caps --schema schema.json --format <name> [--codec-config '{...}'] [--ops a,b,...]"
                 " | caps --vocabulary\n";
    return 2;
  }
  Schema schema = Schema::load(schema_path);
  std::unique_ptr<Format> f = make_format(format, schema, codec_config);
  // codec_config: the canonical form this binary's Config gives the input (the session's round-trip guard)
  json out = {{"format", format}, {"schema", schema.name()}, {"codec_config", f->codec_config()},
              {"capabilities", json::array()}};
  // the STORE's set (coarse_chunks when the format's pack_target() >= contract::chunk_coarse()), what a session selects with
  for (auto& c : f->store_capabilities()) out["capabilities"].push_back(c);
  // per vertex label (Format::capabilities_for: what a variant's `on` checks) -- equal to the store-wide set except
  // where a format's caps differ per label (dgraph's per_type_index / sorted_range_index)
  out["by_label"] = json::object();
  for (auto& v : schema.vertices()) {
    out["by_label"][v.label] = json::array();
    for (auto& c : f->store_capabilities_for(v.label)) out["by_label"][v.label].push_back(c);
  }
  // per edge type (Format::edge_capabilities_for: what an `on` naming an edge type checks)
  out["by_etype"] = json::object();
  for (auto& e : schema.edges()) {
    out["by_etype"][e.etype] = json::array();
    for (auto& c : f->store_capabilities_for(e.etype)) out["by_etype"][e.etype].push_back(c);
  }
  // the executor's dispatch order (exec/executor.cc): a flow op first, then the format's kv ops, else "unknown op"
  if (ops_given) {
    out["ops"] = json::object();
    for (auto& op : ops) {
      if (op.empty()) {
        std::cerr << "caps: --ops holds an empty name\n";
        return 2;
      }
      out["ops"][op] = is_flow_op(op) ? "flow" : f->has_kv_op(op) ? "kv" : "neither";
    }
  }
  std::cout << out.dump(2) << "\n";
  return 0;
}

}  // namespace evalcpp
