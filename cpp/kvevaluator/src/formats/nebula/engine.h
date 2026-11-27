// nebula_engine: what the REAL NebulaGraph engine does over nebula's layout (Python formats/nebula_engine.py), for
// the engine-fidelity check only (a variant of nebula, so it lives here beside the hybrid, never in a sibling dir).
// The store is nebula's, unchanged; the engine's own choices are the ops below: storaged's prefix scan after a
// missed tag read (AppendVertices), the Traverse first-step tag probe (no scan), the heterogeneous traverse's read of
// every LISTED tag, an index LOOKUP / ScanVertices sent to every one of the space's partitions, and GetEdges. It is
// never a compared format: never fed to the layout search, the pins or the paper's format comparisons, and absent
// from key_layouts() (engine_key_layouts() lists it). Reads only. Our keys are tag-major (keys.h), so the engine's
// prefix scan of a vid after a missed tag has no single range here: its SEEK count is the engine's, its landing bytes
// are not (the twins' bytes are not compared).
#pragma once
#include <string>
#include <vector>

#include "formats/nebula/format.h"

namespace evalcpp::nebula {

class NebulaEngine : public Nebula {
 public:
  NebulaEngine(const Schema& schema, const Config& cfg) : Nebula(schema, cfg) {}
  std::string name() const override { return "nebula_engine"; }
  bool has_kv_op(const std::string& op) const override;
  Value run_kv_op(const Step& step, const Args& a, StepExec& ex) override;

  // Partitions in the live LDBC space (nebulagraph/scripts/ldbc/load.sh, PARTITION_NUM = 100; Python NEBULA_PARTS).
  // Our layout has no partitions, which is right for point reads and per-vertex scans; a whole-space read (an index
  // LOOKUP, a ScanVertices) is sent to EVERY partition, and each opens its own prefix scan: 1 SEEK per partition.
  static constexpr int kParts = 100;

 protected:
  Value read_vertex_props(StepExec& ex, const ValueList& vids, const std::vector<std::string>& props,
                          const std::vector<std::string>& labels) override;

 private:
  Value all_tag_props(StepExec& ex, const ValueList& vids, const std::vector<std::string>& tags, bool dedup);
  Value tag_probe(StepExec& ex, const ValueList& vids, const std::string& label, const std::vector<std::string>& props);
  Value tag_index_scan(StepExec& ex, const std::string& label, const std::string& prop, const Value& value,
                       const Value& lo, const Value& hi, bool base_row, const std::vector<std::string>& props);
  Value scan_vertices(StepExec& ex, const std::string& label);
  Value get_edge(StepExec& ex, const std::string& src, const std::string& dst, const std::string& etype);
  // the other partitions' prefix seeks, folded into the one walk the caller did: each lands and stops (a miss)
  static void other_partition_seeks(CountingStore& st, const std::string& key, int n);
};

}  // namespace evalcpp::nebula
