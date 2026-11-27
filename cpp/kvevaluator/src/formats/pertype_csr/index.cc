// Per-type CSR's lookups by label or property value: a label's vertices, the two label filters, top k.
#include "formats/pertype_csr/format.h"
#include "ingest/chunker.h"
#include "schema/far_labels.h"

#include <memory>
#include <unordered_map>
#include <algorithm>
#include <stdexcept>
#include <numeric>

#include "formats/common.h"
#include "formats/pertype_csr/keys.h"
#include "ir/expr.h"
#include "util/uidset.h"
#include "util/vid.h"

namespace evalcpp::pertype_csr {

// keep (a vid set, optional, with props): rows of other vertices are not built; tested in memory at no cost, every
// chunk is still crossed and counted (as meta's scan)
Value PerTypeCsr::read_vertices_by_label(StepExec& ex, const std::string& label, const Value& props, const Value& keep) {
  if (!keep.is_null() && props.is_null())
    throw std::runtime_error("pertype_csr read_vertices_by_label: keep needs props (the scan of the label's rows)");
  bool filtered = !keep.is_null();
  std::unordered_set<uint64_t> kept;
  if (filtered)
    for (auto& v : keep.as_list()) kept.insert(uid_or_throw("pertype_csr", v));
  std::string prefix = props_prefix(schema_.vertex(label).id);
  const std::vector<ColDecl>* cols = &vcols(label);
  bool want_id = false;
  std::vector<std::string> row_props;
  if (!props.is_null())
    for (auto& p : props.as_list()) (p.as_str() == "id" ? want_id = true : (row_props.push_back(p.as_str()), false));
  bool vids_only = props.is_null();
  RowBuilder rb(props_names_id_first(want_id, row_props));   // the call's one shape
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {   // one chunk scan
    Counts before = slot.store.counts();
    ValueList out;
    Cursor cur = slot.store.seek(prefix);
    while (cur.valid() && has_prefix(cur.key(), prefix)) {
      std::string_view raw = cur.value();
      if (raw.size() > slot.scan_chunk_bytes_max) slot.scan_chunk_bytes_max = raw.size();   // held one at a time
      if (vids_only) {
        for (uint64_t u : chunk_ids(cc_, raw)) out.push_back(tagged(schema_, Value::vid(u), label));   // the label's own run
      } else {
        std::shared_ptr<PropsChunk> chunk = PropsChunk::decode(&cc_, cols, std::string(raw));
        chunk->bulk();   // every row is wanted: one whole-chunk decode (Python _chunk_rows_all)
        std::vector<int> cidx;
        for (auto& p : row_props) cidx.push_back(chunk->col_index(p));
        uint64_t used_v = 0;   // a kept row's asked cells' framed bytes; the key: none
        for (size_t i = 0; i < chunk->size(); i++) {
          if (filtered && !kept.count(chunk->ids()[i])) continue;
          uint64_t u = chunk->ids()[i];
          RowBuilder::Row f = rb.start();
          f.push(tagged(schema_, Value::vid(u), label));
          if (want_id) f.push(key_id_value(u));
          for (size_t k = 0; k < row_props.size(); k++) {
            f.push(cidx[k] >= 0 ? chunk->cell(size_t(cidx[k]), i) : Value());   // like a dict
            if (cidx[k] >= 0) used_v += chunk->cell_bytes(size_t(cidx[k]), i);
          }
          out.push_back(f.done());
        }
        slot.store.count_used(0, used_v);
      }
      cur.next();
    }
    end_scan(cur);
    direct_counts(slot, before, r);   // the scan's counts are logical: its histogram entries are committed with them
    r.out = Value::list(std::move(out));
  }};
  return ex.run(tasks)[0].out;
}

Value PerTypeCsr::read_filter_by_label_index(StepExec& ex, const ValueList& vids, const std::vector<std::string>& labels) {
  if (vids.empty()) return Value::list({});
  if (labels.size() != 1) throw std::runtime_error("read_filter_by_label_index needs one label");
  // every candidate carries a label already: decided by them, the index is not walked (one untagged candidate and
  // the whole walk is paid, since the walk answers for all). Not after a vertex write: a label says which family,
  // not that the vertex is there
  bool use_labels = !ex.after_vertex_write;
  DistinctVids d = distinct_vids("pertype_csr", vids, ex.strict_tags && use_labels);
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
  // Probe each enumerated member against the (small) CANDIDATE set in uid space, during the walk --
  // never materialize the whole type membership (20M hex-string Values at sf30: 6-8 s of pure CPU
  // per cell). We still enumerate+charge the whole index (the plan's choice; accounting unchanged).
  UidSet want(d.uids.size());                     // flat, no node per candidate (util/uidset.h)
  for (uint64_t u : d.uids) want.insert(u);
  UidSet found(vids.size());
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    index_->enumerate_uids(slot.store, labels[0], [&](uint64_t u) {   // the same walk, in uid space
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

// The per-candidate probe: one chunk lookup per distinct vid (the layout's own membership test).
Value PerTypeCsr::read_filter_by_label_probe(StepExec& ex, const ValueList& vids, const std::vector<std::string>& labels) {
  if (vids.empty()) return Value::list({});
  if (labels.empty()) throw std::runtime_error("pertype_csr read_filter_by_label_probe needs a label");
  // a vid whose row carries a label is decided by it, no chunk lookup -- unless a vertex write ran in this run (a
  // label says which family, not that the vertex is there). Every other vid is probed under the asked label(s)
  bool use_labels = !ex.after_vertex_write;
  DistinctVids d = distinct_vids("pertype_csr", vids, ex.strict_tags && use_labels);
  FanOut fo;
  register_props(fo, schema_);
  std::vector<Value> res = ex.run_range_map(d.uids.size(), [&](WorkerSlot& slot, size_t ti, std::vector<Landing>& log) {   // one vid: is it of the label?
      if (int16_t carried = use_labels ? d.values[ti].label : int16_t(-1); carried >= 0) {
        for (auto& l : labels)
          if (carried == int16_t(schema_.vertex(l).id)) return Value(l);
        return Value();
      }
      Reader rd(fo.table, slot, log, 0);
      RowHit h;
      return row_lookup(rd, d.uids[ti], labels, h) ? Value(h.label) : Value();   // the label that held it, or none
  }, /*additive_counts=*/false);
  ValueList out;
  for (size_t i = 0; i < vids.size(); i++) {
    const Value& hit = res[d.at.empty() ? i : d.at[i]];
    if (!hit.is_null()) out.push_back(ex.tags.probes ? tagged(schema_, vids[i], hit.as_str()) : vids[i]);
  }
  return Value::list(std::move(out));
}

TopKReads PerTypeCsr::top_k_reads(StepExec&, const kv::TopKArgs& t) {
  auto fo = std::make_shared<FanOut>();   // the winners' row chunks: one cache for the call
  register_props(*fo, schema_);
  TopKReads rd;
  rd.fmt = "pertype_csr";
  rd.winners_replayed = true;   // the winners' chunk lookups replay from the cache
  // the merged reverse walk is a seek->prev chain over the labels' indexes
  rd.walk = [this, &t](WorkerSlot& slot, std::vector<Landing>&, const TopKScope& scope) {
    std::vector<TopKHit> hits;
    for (auto& h : index_->top_k_desc(slot.store, t.labels, t.prop, t.k, scope.hi, scope.cands(), {}))
      hits.push_back({tagged(schema_, Value::vid(h.vid), h.label), h.value, int64_t(id_of_uid(h.vid)), h.label});   // the per-label index it came from
    return hits;
  };
  // a winner's row: its chunk lookup; used: the asked cells' framed bytes
  rd.winner_props = [this, &t, fo](WorkerSlot& slot, std::vector<Landing>& log, const TopKHit& h) {
    Reader rdr(fo->table, slot, log, 0);
    uint64_t vid = h.vid.as_vid();
    RowHit hit;
    bool found = row_lookup(rdr, vid, {h.label}, hit);
    uint64_t uv = 0;
    std::vector<Value> cells;
    for (auto& p : t.props) {
      if (p == "id") { cells.push_back(key_id_value(vid)); continue; }   // key-derived, free
      Value cell;
      if (found) {
        int c = hit.chunk->col_index(p);
        if (c >= 0) { cell = hit.chunk->cell(size_t(c), hit.row); uv += hit.chunk->cell_bytes(size_t(c), hit.row); }
      }
      cells.push_back(std::move(cell));
    }
    slot.store.count_used(0, uv);
    return cells;
  };
  return rd;
}

}  // namespace evalcpp::pertype_csr
