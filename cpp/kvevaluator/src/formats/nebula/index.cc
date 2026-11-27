// NebulaGraph's lookups by label or property value: a tag's vertices, the two label filters, top k.
#include "formats/nebula/format.h"

#include <algorithm>
#include <numeric>
#include <optional>
#include <stdexcept>

#include <unordered_map>

#include "exec/label_tags.h"
#include "formats/common.h"
#include "formats/nebula/keys.h"
#include "codec/rowv2.h"
#include "util/uidset.h"
#include "util/vid.h"

namespace evalcpp::nebula {

Value Nebula::read_vertices_by_label(StepExec& ex, const std::string& label, const Value& props) {
  if (!props.is_null()) throw std::runtime_error("nebula read_vertices_by_label takes no props (nebula.py)");
  vcols(label);
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {   // one index walk
    Counts before = slot.store.counts();
    r.out = tag_all(schema_, Value::list(index_.enumerate(slot.store, label)), label);   // the tag's own index
    direct_counts(slot, before, r);
  }};
  return ex.run(tasks)[0].out;
}

Value Nebula::read_filter_by_label_probe(StepExec& ex, const ValueList& vids, const std::vector<std::string>& labels) {
  // one existence GET per DISTINCT vid whose label is not known (nebula.py); a repeated vid is kept at every occurrence
  if (vids.empty()) return Value::list({});
  if (labels.size() != 1) throw std::runtime_error("nebula read_filter_by_label_probe needs one label");
  int tag = schema_.vertex(labels[0]).id;
  // a vid whose row carries a label (its first tagged occurrence in the input) is decided by it, 0 GETs -- unless a
  // vertex write ran in this run: a label states the tag, not that the vertex is there. Every other vid is probed.
  // (the carried label: the distinct vid's, its first tagged occurrence's; labels are not read after a vertex write)
  bool use_labels = !ex.after_vertex_write;
  DistinctVids d = distinct_vids("nebula", vids, ex.strict_tags && use_labels);
  std::vector<Value> res = ex.run_range_map(d.uids.size(), [&](WorkerSlot& slot, size_t di, std::vector<Landing>& log) {   // one distinct vid
      int16_t carried = use_labels ? d.values[di].label : int16_t(-1);
      if (carried >= 0) return Value(carried == int16_t(tag));
      std::string k = vertex_key(tag, d.uids[di]);
      bool got = slot.store.get(k).has_value();
      if (got) slot.store.count_used(k.size(), 0);   // existence decides by key; a fetched row = amp;
      return Value(got);                            // a miss = the not-found lane, 0 used
  }, /*additive_counts=*/true);
  ValueList out;
  for (size_t i = 0; i < vids.size(); i++)
    if (res[d.at.empty() ? i : d.at[i]].as_bool()) out.push_back(ex.tags.probes ? tagged(schema_, vids[i], labels[0]) : vids[i]);
  return Value::list(std::move(out));
}

Value Nebula::read_filter_by_label_index(StepExec& ex, const ValueList& vids, const std::vector<std::string>& labels) {
  if (vids.empty()) return Value::list({});
  if (labels.size() != 1) throw std::runtime_error("nebula read_filter_by_label_index needs one label");
  // Probe each enumerated member against the (small) CANDIDATE set in uid space, during the walk --
  // never materialize the whole type membership (20M hex-string Values at sf30: 6-8 s of pure CPU
  // per cell). We still enumerate+charge the whole index (the plan's choice; accounting unchanged).
  // every candidate carries a label already: the filter is decided by them, the index is not walked (as the probe
  // form above; one untagged candidate and the whole walk is paid, since the walk answers for all of them)
  bool use_labels = !ex.after_vertex_write;
  DistinctVids d = distinct_vids("nebula", vids, ex.strict_tags && use_labels);
  auto di_of = [&](size_t i) { return d.at.empty() ? i : d.at[i]; };
  if (use_labels) {
    bool all = true;
    for (auto& dv : d.values) all = all && dv.label >= 0;
    if (all) {
      int16_t want_tag = int16_t(schema_.vertex(labels[0]).id);
      ValueList kept;
      for (size_t i = 0; i < vids.size(); i++)
        if (d.values[di_of(i)].label == want_tag) kept.push_back(ex.tags.probes ? tagged(schema_, vids[i], labels[0]) : vids[i]);
      return Value::list(std::move(kept));
    }
  }
  UidSet want(d.uids.size());                     // flat, no node per candidate (util/uidset.h)
  for (uint64_t u : d.uids) want.insert(u);
  UidSet found(vids.size());
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {   // 1 SEEK + M NEXT, then intersect
    Counts before = slot.store.counts();
    index_.enumerate_uids(slot.store, labels[0], [&](uint64_t u) {   // the same walk, in uid space
      if (want.contains(u)) found.insert(u);       // integer probe of the small candidate set
    });
    direct_counts(slot, before, r);
    r.out = Value::list({});
  }};
  ex.run(tasks);
  ValueList out;                                   // keep candidates that ARE members, in input order
  for (size_t i = 0; i < vids.size(); i++)
    if (found.contains(d.uids[di_of(i)])) out.push_back(ex.tags.probes ? tagged(schema_, vids[i], labels[0]) : vids[i]);
  return Value::list(std::move(out));
}

TopKReads Nebula::top_k_reads(StepExec&, const kv::TopKArgs& t) {
  labels_or_throw("read_top_k_by_index", t.labels);   // each label is the schema's
  TopKReads rd;
  rd.fmt = "nebula";
  // the merged reverse walk over the tags' indexes; a hit carries the tag whose index held it
  rd.walk = [this, &t](WorkerSlot& slot, std::vector<Landing>&, const TopKScope& scope) {
    std::vector<TopKHit> hits;
    for (auto& h : index_.top_k_desc(slot.store, t.labels, t.prop, t.k, scope.hi, scope.cands(), {}))
      hits.push_back({tagged(schema_, Value::vid(h.vid), h.label), h.value, key_id_value(h.vid).as_int(), h.label});
    return hits;
  };
  // a winner's row: 1 GET (the tag is known), parsed once whatever the number of props asked
  rd.winner_props = [this, &t](WorkerSlot& slot, std::vector<Landing>&, const TopKHit& h) {
    uint64_t vid = h.vid.as_vid();
    std::string k = vertex_key(schema_.vertex(h.label).id, vid);
    std::optional<std::string> blob = slot.store.get(k);
    if (blob) slot.store.count_used(k.size(), 0);
    std::optional<RowV2> rv;
    if (blob) rv.emplace(&vcols(h.label), *blob);
    std::vector<Value> cells;
    for (auto& p : t.props) {
      if (p == "id") { cells.push_back(key_id_value(vid)); continue; }   // key-derived, free
      Value cell;
      if (rv) {
        int c = rv->col_index(p);
        if (c >= 0) { cell = rv->cell(size_t(c)); slot.store.count_used(0, rv->cell_bytes(size_t(c))); }
      }
      cells.push_back(std::move(cell));
    }
    return cells;
  };
  return rd;
}

}  // namespace evalcpp::nebula
