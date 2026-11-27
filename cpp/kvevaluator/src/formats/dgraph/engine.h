// dgraph_engine: the plan the REAL Dgraph engine runs over dgraph's layout (Python formats/dgraph_engine.py), for
// the engine-fidelity check only (a variant of dgraph, so it lives here beside the hybrid, never in a sibling dir).
// It reproduces the engine's own layout and plan behaviour -- the fqid index the `eq(id, N)` anchor resolves
// through, `id` stored as a scalar with its `@index(int)`, the multi-key sort that re-reads the second key (the
// multiSort uint64 overflow) -- so its counts can be checked against a measured `/kvstats`. It is never a compared
// format: never fed to the layout search, the pins or the paper's format comparisons, and absent from key_layouts()
// (engine_key_layouts() lists it).
//
// Reads only. The store is dgraph's plus the engine's id keys (add_vertex); every inherited op is the base's
// own read path, and the ops below are transcribed from Python's DgraphEngineFormat (itself from dgraph's
// worker/sort.go, tokens.go, task.go). Python's engine twin carries the delta+rollup writes, whose difference is a
// write strategy: no read differs.
#pragma once
#include <string>
#include <string_view>
#include <vector>

#include "formats/dgraph/format.h"

namespace evalcpp::dgraph {

class DgraphEngine : public Dgraph {
 public:
  DgraphEngine(const Schema& schema, const Config& cfg);
  std::string name() const override { return "dgraph_engine"; }
  // its own layout: dgraph's keys plus the fqid index, the `id` scalars and the `id` index
  static constexpr const char* kKeyLayout = "realistic-engine-v1";
  // a tripwire: the twin's layout is dgraph's plus the engine keys -- bump it whenever the base's moves. The engine
  // store is unbounded (it refuses chunk_target), so "realistic-part-start" (split parts' keys) left it unchanged
  static_assert(std::string_view(Dgraph::kKeyLayout) == "realistic-part-start", "dgraph's key layout moved: bump DgraphEngine::kKeyLayout");
  std::string key_codec() const override { return kKeyLayout; }
  bool has_kv_op(const std::string& op) const override;
  Value run_kv_op(const Step& step, const Args& a, StepExec& ex) override;
  void add_vertex(KvSink& sink, uint64_t uid, const std::string& label, const Value& props) override;

 protected:
  // `id` is a real GET of (id, D, uid) here (the engine stores it); every other prop is the base's read
  using Dgraph::props_of;
  Value props_of(CountingStore& st, uint64_t u, const std::vector<std::string>& props, const RowBuilder& rb) override;

 private:
  // the engine's `id @index(int)` token: 20-digit zero-padded, so key order is numeric order (Python _id_token)
  static std::string id_token(int64_t id);
  Value read_vertex_by_id(StepExec& ex, const Value& vid);
  Value read_anchor_props(StepExec& ex, const ValueList& cands, const std::vector<std::string>& props);
  Value sort_topk(StepExec& ex, const ValueList& rows, const std::string& prop, int64_t k,
                  const std::string& second_key, bool id_desc, const std::string& index, bool desc);
  Value sort_without_index(StepExec& ex, const ValueList& vids, const std::string& prop, int64_t k, bool desc);
  Value sort_without_index_per_src(StepExec& ex, const ValueList& rows, const std::string& prop,
                                   const std::string& src, const std::string& dst);
  Value has_scan(StepExec& ex, const std::string& etype, const std::string& dir);
  Value ineq_root_scan(StepExec& ex, const std::string& prop, const std::string& fname, const Value& lo,
                       const Value& hi);
  Value first_k_by_uid(const ValueList& items, int64_t k, const ValueList& uid_of);
  // one scalar value through the posting-list read path (Python _read_list): 1 SEEK, decoded, null when absent
  Value read_value(CountingStore& st, const std::string& prop, uint64_t uid);
};

}  // namespace evalcpp::dgraph
