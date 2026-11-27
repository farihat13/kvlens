#include "formats/nebula/engine.h"

#include <optional>
#include <set>
#include <stdexcept>

#include "codec/rowv2.h"
#include "formats/common.h"
#include "formats/nebula/keys.h"
#include "util/uidset.h"
#include "util/vid.h"

namespace evalcpp::nebula {

void NebulaEngine::other_partition_seeks(CountingStore& st, const std::string& key, int n) {
  for (int i = 0; i < n; i++) {
    Cursor cur = st.seek(key);
    end_scan(cur);
  }
}

// AppendVertices with storaged's miss behaviour: 1 GET per tag tried (the layout's), and after each MISSED tag a
// prefix scan of the vid to discover which tags it has (GetPropNode.h:47-68) -- 1 SEEK, the engine's choice, not the
// layout's. Our keys are tag-major (keys.h), so a vid's rows are not one range here: the SEEK lands just past the
// missed row. Its count is the engine's; its landing bytes are not (bytes are not compared for the engine twins).
Value NebulaEngine::read_vertex_props(StepExec& ex, const ValueList& vids, const std::vector<std::string>& props,
                                      const std::vector<std::string>& label) {
  std::vector<std::string> labs = labels_or_throw("read_vertex_props", label);
  std::vector<std::string> row_props;
  bool want_id = false;
  for (auto& p : props) (p == "id" ? want_id = true : (row_props.push_back(p), false));
  DistinctVids d = distinct_vids("nebula_engine", vids, /*strict=*/false);   // the row's _vid carries no label here
  RowBuilder rb(props_names_id_first(want_id, row_props));   // the call's one shape
  std::vector<Value> res = ex.run_range_map(d.uids.size(), [&](WorkerSlot& slot, size_t ti, std::vector<Landing>&) {
    uint64_t u = d.uids[ti];
    RowBuilder::Row f = rb.start();
    f.push(Value::vid(u));
    if (want_id) f.push(key_id_value(u));                         // key-derived, free
    if (!row_props.empty()) {
      std::optional<std::string> blob;
      const std::vector<ColDecl>* cols = nullptr;
      for (auto& tag : labs) {
        std::string k = vertex_key(schema_.vertex(tag).id, u);
        blob = slot.store.get(k);                                     // 1 GET per probe, found or not
        if (blob) { slot.store.count_used(k.size(), 0); cols = &vcols(tag); break; }
        slot.store.seek(k);                                           // the miss: storaged's tag-discovery scan
      }
      if (blob) {
        RowV2 row(cols, *blob);
        for (auto& p : row_props) {
          int c = row.col_index(p);
          f.push(c >= 0 ? row.cell(size_t(c)) : Value());
        }
      } else {
        for (size_t i = 0; i < row_props.size(); i++) f.push(Value());
      }
    }
    return f.done();
  }, /*additive_counts=*/true);
  ValueList out;
  out.reserve(vids.size());
  for (size_t i = 0; i < vids.size(); i++) out.push_back(res[d.at.empty() ? i : d.at[i]]);
  return Value::list(std::move(out));
}

// Every tag the plan node LISTS, on each visited vertex, no short-circuit (a heterogeneous Traverse lists all the
// space's tags; an AppendVertices over a union type lists its members): len(tags) GETs per vertex -- per DISTINCT
// vertex with the node's frontier dedup, per input row without it. No prefix scan: one of the listed tags exists.
// Each found row with a present cell merges into the row and `_tag` names the last such row (Python `if blob:`).
Value NebulaEngine::all_tag_props(StepExec& ex, const ValueList& vids, const std::vector<std::string>& tags, bool dedup) {
  for (auto& t : tags) vcols(t);
  std::vector<uint64_t> seq;
  if (dedup)
    seq = distinct_vids("nebula_engine", vids, /*strict=*/false).uids;
  else
    for (auto& v : vids) seq.push_back(uid_or_throw("nebula_engine", v));
  std::vector<Value> res = ex.run_range_map(seq.size(), [&](WorkerSlot& slot, size_t ti, std::vector<Landing>&) {
    uint64_t u = seq[ti];
    RowFields f;
    f.emplace_back("_vid", Value::vid(u));
    f.emplace_back("id", key_id_value(u));
    for (auto& tag : tags) {
      std::string k = vertex_key(schema_.vertex(tag).id, u);
      std::optional<std::string> blob = slot.store.get(k);           // 1 GET per LISTED tag
      if (!blob) continue;
      RowV2 row(&vcols(tag), *blob);
      Value cells_v = row.row();                                      // a local: as_row() of a temporary dangles
      RowView cells = cells_v.as_row();
      if (cells.empty()) continue;                                    // Python `if blob:` -- an empty row merges nothing
      slot.store.count_used(k.size(), 0);
      for (auto& [name, val] : cells) row_set(f, name, val);
      row_set(f, "_tag", Value(tag));
    }
    return Value::row(std::move(f));
  }, /*additive_counts=*/true);
  return Value::list(std::move(res));
}

// A Traverse's `first step filter: X._tag IS NOT EMPTY`: 1 GET per distinct source, and NO prefix scan on a miss
// (a point lookup answers "is this a Post?" with absent). With `props` the same GET yields the values (IS1's rule).
Value NebulaEngine::tag_probe(StepExec& ex, const ValueList& vids, const std::string& label,
                              const std::vector<std::string>& props) {
  int tag = schema_.vertex(label).id;
  const std::vector<ColDecl>& cols = vcols(label);
  std::vector<uint64_t> distinct = distinct_vids("nebula_engine", vids, /*strict=*/false).uids;
  RowBuilder rb(props_names(props));                        // the call's one shape
  std::vector<Value> res = ex.run_range_map(distinct.size(), [&](WorkerSlot& slot, size_t ti, std::vector<Landing>&) {
    uint64_t u = distinct[ti];
    std::string k = vertex_key(tag, u);
    std::optional<std::string> blob = slot.store.get(k);             // found or not-found
    if (!blob) return Value();
    slot.store.count_used(k.size(), 0);
    if (props.empty()) return Value::vid(u);
    RowV2 row(&cols, *blob);
    RowBuilder::Row f = rb.start();
    f.push(Value::vid(u));
    for (auto& p : props) {
      if (p == "id") { f.push(key_id_value(u)); continue; }       // the key
      int c = row.col_index(p);
      f.push(c >= 0 ? row.cell(size_t(c)) : Value());
    }
    return f.done();
  }, /*additive_counts=*/true);
  ValueList out;
  for (auto& r : res)
    if (!r.is_null()) out.push_back(r);
  return Value::list(std::move(out));
}

// LOOKUP ON <tag>: TagIndexPrefixScan (value), TagIndexRangeScan (lo / hi; hi exclusive, no lo = from 0) or
// TagIndexFullScan (neither) over the index on (label, prop) -- the layout's walk, run once per partition
// (LookupProcessor: plan->execute(part) per part, each its own iterator: S = kParts), plus a base-row GET per entry
// when the query needs a column the index lacks (base_row; IndexScanNode needAccessBase_).
Value NebulaEngine::tag_index_scan(StepExec& ex, const std::string& label, const std::string& prop, const Value& value,
                                   const Value& lo, const Value& hi, bool base_row,
                                   const std::vector<std::string>& props) {
  const std::vector<ColDecl>& cols = vcols(label);
  int tag = schema_.vertex(label).id;
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    CountingStore& st = slot.store;
    std::string pre = IndexKeys::prefix(schema_.index_decl(label, prop).id);
    ValueList vids;
    if (!value.is_null()) {
      vids = index_.eq(st, label, prop, value);
    } else if (!lo.is_null() || !hi.is_null()) {
      if (hi.is_null()) throw std::runtime_error("tag_index_scan: a range needs hi (Python's read_index_range does)");
      vids = index_.range(st, {label}, prop, lo.is_null() ? 0 : lo.as_int(), hi.as_int());
    } else {
      Cursor cur = st.seek(pre);                                      // TagIndexFullScan: the whole (label, prop) index
      while (cur.valid() && has_prefix(cur.key(), pre)) {
        st.count_used(cur.key().size(), 0);
        vids.push_back(Value::vid(IndexKeys::vid_of(cur.key())));
        cur.next();
      }
      end_scan(cur);
    }
    other_partition_seeks(st, pre, kParts - 1);
    ValueList out;
    std::vector<std::string> names = {"_vid", "id"};
    if (base_row)
      for (auto& p : props) if (p != "id") names.push_back(p);
    RowBuilder rb(names);                                   // the call's one shape
    for (auto& v : vids) {
      RowBuilder::Row f = rb.start();
      f.push(v);
      uint64_t u = uid_or_throw("nebula_engine", v);
      f.push(key_id_value(u));
      if (base_row) {
        std::string k = vertex_key(tag, u);
        std::optional<std::string> blob = st.get(k);                 // getBaseData: 1 GET per entry
        if (blob) st.count_used(k.size(), 0);
        std::optional<RowV2> row;
        if (blob) row.emplace(&cols, *blob);
        for (auto& p : props) {
          if (p == "id") continue;
          int c = row ? row->col_index(p) : -1;
          f.push(c >= 0 ? row->cell(size_t(c)) : Value());
        }
      }
      out.push_back(f.done());
    }
    direct_counts(slot, before, r);
    r.out = Value::list(std::move(out));
  }};
  return ex.run(tasks)[0].out;
}

// MATCH (v:<label>) with no index on it: ScanVertices -- every TAG ROW of every partition, other tags' rows dropped.
// The walk is the Python reference's (each tag's type enumeration, whose keys hold no tag-row family): kParts SEEKs
// (the tag walks + the other partitions'), one NEXT per vertex, and each tag walk's end landing a miss -- so the
// counts, miss lanes included, are the reference's. (Here the 'v' prefix IS every tag row and a direct row walk
// would read the engine's row bytes, but it would end in one miss, not one per tag: not the reference's lanes.)
Value NebulaEngine::scan_vertices(StepExec& ex, const std::string& label) {
  vcols(label);
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    CountingStore& st = slot.store;
    ValueList out;
    for (auto& v : schema_.vertices()) {      // every tag of the space: the schema's (Python ALL_TAGS, the live space's 8
                                              // tags, on every shipped LDBC schema; another schema would charge its own)
      ValueList vids = index_.enumerate(st, v.label);
      if (v.label == label) out = std::move(vids);
    }
    int64_t walked = int64_t(st.counts().S - before.S);
    std::string pre = schema_.prop_indexed(label)
                          ? IndexKeys::prefix(schema_.index_decl(label, schema_.enumeration_prop(label)).id)
                          : IndexKeys::prefix(schema_.type_index_decl(label).id);
    other_partition_seeks(st, pre, int(kParts - walked));            // the partitions the tag walks did not stand for
    direct_counts(slot, before, r);
    r.out = Value::list(std::move(out));
  }};
  return ex.run(tasks)[0].out;
}

// FETCH PROP ON <etype> src -> dst: GetEdges -- 1 GET of the forward edge key, found or not, no follow-up scan
Value NebulaEngine::get_edge(StepExec& ex, const std::string& src, const std::string& dst, const std::string& etype) {
  int eid = schema_.edge(etype).id;
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    std::string k = edge_key(eid, true, uid_or_throw("nebula_engine", src), uid_or_throw("nebula_engine", dst));
    bool found = slot.store.get(k).has_value();
    if (found) slot.store.count_used(k.size(), 0);
    direct_counts(slot, before, r);
    r.out = found ? Value::list({make_row({{"src", Value(src)}, {"dst", Value(dst)}})}) : Value::list({});
  }};
  return ex.run(tasks)[0].out;
}

static const std::set<std::string> kEngineOps = {"all_tag_props", "tag_probe", "tag_index_scan", "scan_vertices",
                                                 "get_edge"};

bool NebulaEngine::has_kv_op(const std::string& op) const { return kEngineOps.count(op) > 0 || Nebula::has_kv_op(op); }

Value NebulaEngine::run_kv_op(const Step& step, const Args& a, StepExec& ex) {
  const std::string& op = step.op;
  if (is_write_op(op))
    throw std::runtime_error("nebula_engine: reads only (the engine-fidelity check measures reads; '" + op + "')");
  if (op == "all_tag_props") {
    std::vector<std::string> tags;
    if (a.has("tags")) tags = a.str_list("tags");
    else for (auto& v : schema_.vertices()) tags.push_back(v.label);   // every tag of the space (= Python ALL_TAGS on LDBC)
    return all_tag_props(ex, a.list("vids"), tags, a.flag("dedup", true));
  }
  if (op == "tag_probe")
    return tag_probe(ex, a.list("vids"), a.str("label"), a.has("props") ? a.str_list("props") : std::vector<std::string>{});
  if (op == "tag_index_scan")
    return tag_index_scan(ex, a.str("label"), a.str("prop"), a.opt("value"), a.opt("lo"), a.opt("hi"),
                          a.flag("base_row", false), a.has("props") ? a.str_list("props") : std::vector<std::string>{});
  if (op == "scan_vertices") return scan_vertices(ex, a.str("label"));
  if (op == "get_edge") return get_edge(ex, a.str("src"), a.str("dst"), a.str("etype"));
  return Nebula::run_kv_op(step, a, ex);
}

}  // namespace evalcpp::nebula
