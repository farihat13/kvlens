// ArangoDB's lookups by label or property value: index eq and range, a collection's documents, the label probe, top k.
#include "formats/arangodb/format.h"

#include <algorithm>
#include <optional>
#include <stdexcept>
#include <unordered_map>
#include <unordered_set>

#include "exec/label_tags.h"

#include "codec/rowv2.h"
#include "formats/arangodb/keys.h"
#include "formats/arangodb/vpack.h"
#include "formats/common.h"
#include "index/sorted_index.h"
#include "ir/expr.h"
#include "util/uidset.h"
#include "util/vid.h"

namespace evalcpp::arangodb {

Value Arango::read_index_eq(StepExec& ex, const std::string& label, const std::string& prop, const Value& value) {
  // the index key holds the DOCID: recovering the vid costs a doc GET per match
  int coll = schema_.vertex(label).id;
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    std::string prefix = index_prefix(label, prop) + term_bytes(label, prop, value, true);
    ValueList out;
    Cursor cur = slot.store.seek(prefix);
    RtParallel par(slot.store);   // one doc per matching entry of the scan
    while (cur.valid() && has_prefix(cur.key(), prefix)) {
      slot.store.count_used(cur.key().size(), 0);   // every matching entry is output
      uint64_t docid = trailing_u64(cur.key());
      cur.next();
      std::string dk = doc_key(coll, docid);
      par.member();
      std::optional<std::string> raw = slot.store.get(dk);   // 1 GET: resolve _key
      // arango's docid indirection: only the _key member is used; the rest of the
      // fetched doc is the indirection's amplification
      uint64_t used = 0;
      RowFields doc = raw ? doc_fields(vdoc_cols(label), *raw, {"_key"}, &used) : RowFields{};
      if (raw) slot.store.count_used(dk.size(), used);
      Value key = field_or_null(doc, "_key");
      out.push_back(key.is_null() ? key : tagged(schema_, key, label));   // the collection's own index
    }
    end_scan(cur);
    direct_counts(slot, before, r);
    r.out = Value::list(std::move(out));
  }};
  return ex.run(tasks)[0].out;
}

Value Arango::read_index_range(StepExec& ex, const std::vector<std::string>& labels, const std::string& prop,
                               int64_t lo, int64_t hi) {
  if (labels.empty()) throw std::runtime_error("arangodb read_index_range needs labels");
  std::vector<TaskFn> tasks = {[&, lo, hi](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    ValueList out;
    RtParallel par(slot.store);   // one index walk per label, the labels are the plan's
    for (auto& label : labels) {
      par.member();
      int coll = schema_.vertex(label).id;
      std::string pre = index_prefix(label, prop);
      std::string hi_b = IndexKeys::sorted_bytes(hi);
      Cursor cur = slot.store.seek(pre + IndexKeys::sorted_bytes(std::max(lo, INT64_MIN + 1)));   // past the null block
      RtParallel per_entry(slot.store);   // one doc per in-window entry of the walk
      while (cur.valid() && has_prefix(cur.key(), pre) &&
             std::string_view(cur.key()).substr(pre.size(), 8) < std::string_view(hi_b)) {
        slot.store.count_used(cur.key().size(), 0);   // in-window entry = output
        uint64_t docid = trailing_u64(cur.key());
        cur.next();
        std::string dk = doc_key(coll, docid);
        per_entry.member();
        std::optional<std::string> raw = slot.store.get(dk);   // 1 GET: resolve _key
        uint64_t used = 0;
        RowFields doc = raw ? doc_fields(vdoc_cols(label), *raw, {"_key"}, &used) : RowFields{};
        if (raw) slot.store.count_used(dk.size(), used);
        Value key = field_or_null(doc, "_key");
        out.push_back(key.is_null() ? key : tagged(schema_, key, label));   // each entry's own collection
      }
      end_scan(cur);
    }
    direct_counts(slot, before, r);
    r.out = Value::list(std::move(out));
  }};
  return ex.run(tasks)[0].out;
}

// With `props`: ONE scan of the collection's documents ('D' coll docid is one key run): 1 SEEK + 1 NEXT per document,
// every document's key and value read; rows as read_vertex_props builds them, in docid order. `keep` (a vid set,
// optional): rows of other vertices are not built; the set is tested in memory, every document is still crossed.
// Used bytes: a kept row's key, its _key member (what names the row) and the wanted members.
// The capability: label_rows_one_run.
Value Arango::read_vertices_by_label(StepExec& ex, const std::string& label, const Value& props, const Value& keep) {
  int coll = schema_.vertex(label).id;
  if (!props.is_null()) {
    bool filtered = !keep.is_null();
    UidSet kept;
    if (filtered)
      for (auto& v : keep.as_list()) kept.insert(uid_or_throw("arangodb", v));
    bool want_id = false;
    std::vector<std::string> row_props, fields = {"_key"};
    for (auto& p : props.as_list())
      (p.as_str() == "id" ? want_id = true : (row_props.push_back(p.as_str()), fields.push_back(p.as_str()), false));
    std::vector<std::string> names = {"_vid", "_label"};
    if (want_id) names.push_back("id");
    names.insert(names.end(), row_props.begin(), row_props.end());
    RowBuilder rb(names);                                   // the call's one shape
    std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {
      Counts before = slot.store.counts();
      std::string prefix = doc_prefix(coll);
      ValueList out;
      Cursor cur = slot.store.seek(prefix);
      while (cur.valid() && has_prefix(cur.key(), prefix)) {
        std::string_view raw = cur.value();          // every crossed document's value is charged
        uint64_t used = 0;
        RowFields doc = doc_fields(vdoc_cols(label), raw, fields, &used);
        Value key = field_or_null(doc, "_key");      // a vid (a non-canonical key stays a str)
        if (!filtered || kept.contains(uid_or_throw("arangodb", key))) {
          slot.store.count_used(cur.key().size(), used);
          RowBuilder::Row f = rb.start();
          f.push(key.is_null() ? key : tagged(schema_, key, label));   // the scanned collection is the row's label (as the pk scan's)
          f.push(Value(label));
          if (want_id) f.push(Expr::key_id(key));
          for (auto& p : row_props) f.push(field_or_null(doc, p));
          out.push_back(f.done());
        }
        cur.next();
      }
      end_scan(cur);
      direct_counts(slot, before, r);
      r.out = Value::list(std::move(out));
    }};
    return ex.run(tasks)[0].out;
  }
  if (!keep.is_null())
    throw std::runtime_error("arangodb read_vertices_by_label: keep needs props (the scan of the collection's documents)");
  // the pk index IS the enumeration: vid in the KEY, 0 GETs
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    std::string prefix = pk_prefix(coll);
    ValueList out;
    Cursor cur = slot.store.seek(prefix);
    while (cur.valid() && has_prefix(cur.key(), prefix)) {
      slot.store.count_used(cur.key().size(), 0);   // every member key is output
      out.push_back(tagged(schema_, Value::vid(trailing_u64(cur.key())), label));   // the collection's pk run
      cur.next();
    }
    end_scan(cur);
    direct_counts(slot, before, r);
    r.out = Value::list(std::move(out));
  }};
  return ex.run(tasks)[0].out;
}

Value Arango::read_filter_by_label_probe(StepExec& ex, const ValueList& vids, const std::vector<std::string>& labels) {
  // a vid whose row carries its collection (the hop's edge value names it) is decided by it, 0 KV ops; one that
  // carries none is probed: 1 pk GET under the asked collection. After a vertex write in the run a label on a row
  // still states the collection, but not that the vertex is there: then every vid is probed.
  if (labels.size() != 1) throw std::runtime_error("arangodb read_filter_by_label_probe needs one label");
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    DistinctVids d = distinct_vids("arangodb", vids, ex.strict_tags);   // a vid's carried label: its first tagged one
    int16_t want = int16_t(schema_.vertex(labels[0]).id);
    std::vector<int8_t> probed(d.uids.size(), -1);   // one probe per distinct untagged vid (-1: not probed yet)
    ValueList out;
    RtParallel par(slot.store);   // one pk probe per distinct untagged vid, the vids are the step's input
    for (size_t i = 0; i < vids.size(); i++) {
      const Value& v = vids[i];
      size_t di = d.at.empty() ? i : d.at[i];
      bool keep;
      if (d.values[di].label >= 0 && !ex.after_vertex_write) {
        keep = d.values[di].label == want;
      } else if (probed[di] >= 0) {
        keep = probed[di] == 1;
      } else {
        std::string k = pk_key(want, d.uids[di]);
        par.member();
        keep = slot.store.get(k).has_value();
        if (keep) slot.store.count_used(k.size(), 0);   // an existence probe: a hit's key
        probed[di] = keep ? 1 : 0;
      }
      if (keep) out.push_back(ex.tags.probes ? tagged(schema_, v, labels[0]) : v);
    }
    direct_counts(slot, before, r);
    r.out = Value::list(std::move(out));
  }};
  return ex.run(tasks)[0].out;
}

TopKReads Arango::top_k_reads(StepExec&, const kv::TopKArgs& t) {
  // a crossed entry's doc, kept for the winner rows: ONLY the `wanted` members (_key + the projection), not the whole
  // document; props_used = the projection's used bytes, taken at the ONE parse (charged to the winners only)
  struct Doc { RowFields fields; uint64_t props_used; };
  auto docs = std::make_shared<std::vector<Doc>>();
  TopKReads rd;
  rd.fmt = "arangodb";
  rd.walk = [this, &t, docs](WorkerSlot& slot, std::vector<Landing>&, const TopKScope& scope) {
    const std::string& prop = t.prop;
    const int64_t k = t.k;
    const Value& hi = t.hi;
    CountingStore& st = slot.store;
    // per-collection sorted index cursors, newest-first; membership = 1 doc GET per entry
    // crossed (the docid is in the key, the _key in the doc)
    struct Cur { std::string label; int coll; std::string pre; Cursor cur; };
    std::vector<Cur> cursors;
    {
      RtParallel par(st);   // one cursor per label, the labels are the plan's
      for (auto& label : t.labels) {
        par.member();
        std::string pre = index_prefix(label, prop);
        std::string bound = hi.is_null() ? std::string(8, char(0xFF)) : IndexKeys::sorted_bytes(hi.as_int());
        cursors.push_back({label, schema_.vertex(label).id, pre, st.seek_for_prev(pre + bound)});
      }
    }
    std::vector<TopKHit> hits;
    std::vector<std::string> terms;              // each hit's index term: the walk stops past the k-th one
    std::vector<std::string> wanted = t.props;   // the members a hit's row needs: _key + the projection
    wanted.push_back("_key");
    auto term_of = [&](Cur& c) -> std::optional<std::string> {
      if (c.cur.valid() && has_prefix(c.cur.key(), c.pre)) {
        std::string_view tm = std::string_view(c.cur.key()).substr(c.pre.size(), 8);
        if (!IndexKeys::is_null_sorted(tm)) return std::string(tm);   // the null block ends a downward walk
      }
      return std::nullopt;
    };
    while (true) {
      Cur* best = nullptr;
      std::string best_term;
      for (auto& c : cursors) {
        auto tm = term_of(c);
        if (tm && (!best || *tm > best_term)) { best = &c; best_term = *tm; }
      }
      if (!best) break;
      if (int64_t(hits.size()) >= k && best_term != terms.back()) break;   // past the k-th date
      st.count_used(best->cur.key().size(), 0);   // RULED: every crossed entry is inspected
      uint64_t docid = trailing_u64(best->cur.key());
      std::string dk = doc_key(best->coll, docid);
      std::optional<std::string> raw = st.get(dk);     // membership: docid -> _key
      // the _key member decides candidacy (inspected); the doc's other members are used
      // only if this hit survives to the top-k (annotated by the winner read)
      uint64_t key_used = raw ? doc_used(vdoc_cols(best->label), *raw, {"_key"}) : 0, all_used = 0;
      if (raw) st.count_used(dk.size(), key_used);
      RowFields doc = raw ? doc_fields(vdoc_cols(best->label), *raw, wanted, &all_used) : RowFields{};
      Value key_v = field_or_null(doc, "_key");                // a vid (a non-canonical key stays a str)
      if (!scope.cand || (!key_v.is_null() && scope.cand->contains(uid_or_throw("arangodb", key_v)))) {   // parse only the crossed few
        // the index it came from is its collection's
        hits.push_back({tagged(schema_, key_v, best->label), IndexKeys::sorted_value(best_term),
                        Expr::key_id(key_v).as_int(), best->label, docs->size()});
        terms.push_back(best_term);
        docs->push_back({std::move(doc), all_used - key_used});   // wanted = the projection + _key
      }
      best->cur.prev();                                                        // 1 NEXT per entry crossed
    }
    for (auto& c : cursors) end_scan(c.cur);   // every cursor rests on an unconsumed entry (or off its index)
    return hits;
  };
  // the WINNERS' projected members become used now (the doc was already fetched in the walk): no read
  rd.winner_props = [&t, docs](WorkerSlot& slot, std::vector<Landing>&, const TopKHit& h) {
    const Doc& d = (*docs)[h.at];
    if (d.props_used) slot.store.count_used(0, d.props_used);
    std::vector<Value> cells;
    for (auto& p : t.props) cells.push_back(p == "id" ? Expr::key_id(h.vid) : field_or_null(d.fields, p));   // id from the key
    return cells;
  };
  return rd;
}

}  // namespace evalcpp::arangodb
