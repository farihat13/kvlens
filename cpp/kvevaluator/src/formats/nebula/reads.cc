// NebulaGraph's vertex and edge reads: tag rows, edge keys, neighbours, props, chase, edge exists (the dispatch is in format.cc).
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

// --- reads ------------------------------------------------------------------------------------

Value Nebula::read_vertex_by_id(StepExec& ex, const Value& vid, const std::vector<std::string>& labels) {
  // the vid is the key: 0 ops. The plan's label is NOT carried on: nothing was read, and a carried label decides a
  // later label filter with no GET -- an absent vid, or one of another tag, would pass it on the plan's word
  if (vid.is_text()) return Value::list({vid});
  // a candidate message anchor: try each candidate TAG on each vid until one exists (real GETs,
  // misses included -- nGQL prop access is tag-qualified)
  std::vector<std::string> labs = labels_or_throw("read_vertex_by_id", labels);
  std::vector<TaskFn> tasks = {[&, labs](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    ValueList out;
    for (auto& cand : vid.as_list()) {
      uint64_t u = uid_or_throw("nebula", cand);
      const std::string* hit = nullptr;
      for (auto& tag : labs) {
        std::string k = vertex_key(schema_.vertex(tag).id, u);
        if (slot.store.get(k)) {
          slot.store.count_used(k.size(), 0);   // existence by key: the hit's key; a tag miss = the
          hit = &tag;                            // not-found lane, 0 used (like read_edge_exists)
          break;
        }
      }
      if (hit) { out.push_back(ex.tags.probes ? tagged(schema_, cand, *hit) : cand); break; }   // the tag that held it
    }
    direct_counts(slot, before, r);
    r.out = Value::list(std::move(out));
  }};
  return ex.run(tasks)[0].out;
}

ValueList Nebula::neighbors_of(CountingStore& st, const std::string& src, const std::string& etype,
                               const std::vector<bool>& dirs, const std::vector<std::string>& edge_props, bool pairs,
                               size_t used_first_n) {
  return neighbors_of(st, Value(src), etype, dirs, edge_props, pairs, /*tag=*/false, used_first_n);
}

ValueList Nebula::neighbors_of(CountingStore& st, const Value& src, const std::string& etype,
                               const std::vector<bool>& dirs, const std::vector<std::string>& edge_props, bool pairs,
                               bool tag, size_t used_first_n, bool first_only) {
  ValueList out;
  neighbors_of_into(st, src, etype, dirs, edge_props, pairs, tag, used_first_n, NeighborItems(edge_props, pairs), out,
                    first_only);
  return out;
}

void Nebula::neighbors_of_into(CountingStore& st, const Value& src, const std::string& etype, const std::vector<bool>& dirs,
                         const std::vector<std::string>& edge_props, bool pairs, bool tag, size_t used_first_n,
                         const NeighborItems& item, ValueList& out, bool first_only) {
  uint64_t su = uid_or_throw("nebula", src);
  int eid = schema_.edge(etype).id;
  const std::vector<ColDecl>* cols = &ecols(etype);
  RtParallel par(st);                                               // one member per direction's scan
  for (bool d : dirs) {
    par.member();
    // the far end's label, when the schema gives this direction ONE (source (b) of the rule). More than one:
    // none, the edge key has no tag for the far vertex (format.h)
    int16_t far = -1;
    if (tag) {
      std::vector<std::string> fl = schema_.far_labels(etype, d);
      if (fl.size() == 1) far = int16_t(schema_.vertex(fl[0]).id);
    }
    std::string prefix = edge_prefix(eid, d, su);
    Cursor cur = st.seek(prefix);                                   // 1 SEEK per direction
    size_t j = 0;
    while (cur.valid() && has_prefix(cur.key(), prefix)) {
      // used: the edge KEY (the neighbour rides it) for every consumed entry (used_first_n
      // bounds it -- the chase uses only the first); the unconditionally-fetched edge VALUE
      // is amplification unless edge props were requested (nebula's read_neighbors tax)
      if (j < used_first_n) st.count_used(cur.key().size(), 0);
      Value dst = Value::vid(edge_dst(cur.key()));                  // the neighbor vid from the KEY
      dst.label = far;
      std::string_view raw = cur.value();                           // the inline value is read on every edge
      if (edge_props.empty()) {
        out.push_back(item(src, dst, nullptr));
      } else {
        RowV2 rv(cols, raw);
        Value props = rv.row();
        if (j < used_first_n) {
          uint64_t uv = 0;
          for (auto& ep : edge_props) {
            int c = rv.col_index(ep);
            if (c >= 0) uv += rv.cell_bytes(size_t(c));             // the requested fields' framed bytes
          }
          st.count_used(0, uv);
        }
        out.push_back(item(src, dst, &props));
      }
      j++;
      if (first_only) break;                                        // on the first edge: no NEXT past it
      cur.next();                                                   // 1 NEXT per edge
    }
    if (!(first_only && j > 0)) end_scan(cur);                      // the landing that ended the scan: a miss
  }
  return;
}

Value Nebula::read_neighbors(StepExec& ex, const ValueList& srcs, const std::string& etype, const std::string& dir,
                             const std::vector<std::string>& edge_props, bool pairs) {
  std::vector<bool> dirs = parse_dirs("read_neighbors", dir);
  ecols(etype);
  DistinctVids d = distinct_vids("nebula", srcs, ex.strict_tags);   // a src keeps its label
  WorkerItems items(ex.P(), edge_props, pairs);   // one shape per worker; each src's items appended in index order
  return ex.run_range_append(d.uids.size(), [&](WorkerSlot& slot, size_t ti, ValueList& out, std::vector<Landing>&) {
      neighbors_of_into(slot.store, d.values[ti], etype, dirs, edge_props, pairs, ex.tags.hops, SIZE_MAX, items.of(slot), out);
  }, /*additive_counts=*/true);
}

Value Nebula::read_vertex_props(StepExec& ex, const ValueList& vids, const std::vector<std::string>& props,
                                const std::vector<std::string>& label) {
  std::vector<std::string> labs = labels_or_throw("read_vertex_props", label);
  std::vector<std::string> row_props;
  bool want_id = false;
  for (auto& p : props) (p == "id" ? want_id = true : (row_props.push_back(p), false));
  DistinctVids d = distinct_vids("nebula", vids, ex.strict_tags);
  RowBuilder rb(props_names_id_first(want_id, row_props));   // the call's one shape
  // each distinct vid as the input carries it: a label on the row (a per-tag index read, a label scan, an earlier
  // read's hit, a one-label schema end) names its tag, so the other tags are not tried; with none, every tag of the
  // plan's list is tried in order, a miss GET each (a vid that came through a hop to an open far end: the layout's price)
  const std::vector<Value>& vals = d.values;
  std::vector<Value> res = ex.run_range_map(d.uids.size(), [&](WorkerSlot& slot, size_t ti, std::vector<Landing>& log) {   // one vid: its tag row
      uint64_t u = d.uids[ti];
      RowBuilder::Row f = rb.start();
      f.push(vals[ti]);
      if (want_id) f.push(key_id_value(u));                       // key-derived, free
      if (!row_props.empty()) {
        std::optional<std::string> blob;
        const std::vector<ColDecl>* cols = nullptr;
        for (auto& tag : narrow(schema_, vals[ti], labs)) {         // 1 GET per tag tried, found or not
          std::string k = vertex_key(schema_.vertex(tag).id, u);
          blob = slot.store.get(k);
          if (blob) {                                               // the hit's key; a tag miss = the not-found lane
            slot.store.count_used(k.size(), 0);
            cols = &vcols(tag);
            if (ex.tags.probes) f[0] = tagged(schema_, vals[ti], tag);   // the row carries the tag on
            break;
          }
        }
        if (blob) {
          RowV2 row(cols, *blob);
          uint64_t uv = 0;
          for (auto& p : row_props) {
            int c = row.col_index(p);
            if (c >= 0) uv += row.cell_bytes(size_t(c));            // the requested fields' framed bytes
            f.push(c >= 0 ? row.cell(size_t(c)) : Value());          // a repeated name overwrites, like a dict
          }
          slot.store.count_used(0, uv);                             // the rest of the row = amp
        } else {
          for (size_t i = 0; i < row_props.size(); i++) f.push(Value());
        }
      }
      return f.done();
  }, /*additive_counts=*/true);
  ValueList out;                                            // input order, duplicates included
  out.reserve(vids.size());
  for (size_t i = 0; i < vids.size(); i++) out.push_back(res[d.at.empty() ? i : d.at[i]]);
  return Value::list(std::move(out));
}

Value Nebula::read_anchor_props(StepExec& ex, const ValueList& cands, const std::vector<std::string>& props,
                                const std::vector<std::string>& labels) {
  std::vector<std::string> labs = labels_or_throw("read_anchor_props", labels);
  std::vector<TaskFn> tasks = {[&, labs](WorkerSlot& slot, TaskResult& r) {   // the anchor try IS the read
    Counts before = slot.store.counts();
    r.out = Value::list({});
    for (auto& cand : cands) {
      uint64_t u = uid_or_throw("nebula", cand);
      for (auto& tag : labs) {
        std::string k = vertex_key(schema_.vertex(tag).id, u);
        std::optional<std::string> blob = slot.store.get(k);
        if (!blob) continue;                          // a miss decides "not this tag": the not-found lane
        slot.store.count_used(k.size(), 0);           // the hit's key
        RowV2 row(&vcols(tag), *blob);
        RowFields f;
        f.emplace_back("_vid", ex.tags.probes ? tagged(schema_, cand, tag) : cand);   // the tag that held it
        uint64_t uv = 0;
        for (auto& p : props) {
          if (p == "id") { row_set(f, p, key_id_value(u)); continue; }   // key-derived, free
          int c = row.col_index(p);
          if (c >= 0) uv += row.cell_bytes(size_t(c));
          row_set(f, p, c >= 0 ? row.cell(size_t(c)) : Value());
        }
        slot.store.count_used(0, uv);
        r.out = Value::list({Value::row(std::move(f))});
        direct_counts(slot, before, r);
        return;
      }
    }
    direct_counts(slot, before, r);
  }};
  return ex.run(tasks)[0].out;
}

Value Nebula::closure_level(StepExec& ex, uint64_t, const ValueList& frontier, const std::string& etype,
                            const std::string& dir) {
  return read_neighbors(ex, frontier, etype, dir, {}, false);
}

ChaseReads Nebula::chase_reads(StepExec& ex, const std::string& etype, const std::string& dir) {
  std::vector<bool> dirs = parse_dirs("read_chase", dir);
  ecols(etype);
  ChaseReads rd;
  rd.fmt = "nebula";
  rd.per_source = name() != "nebula";   // its engine twin
  // chase used-rule (P5a): only the FIRST edge continues the chain
  rd.dirs = dirs;
  // the format stops on the first edge (1 SEEK, no NEXT: meta Split's chase); the engine twin scans the whole list
  bool first_only = !rd.per_source;
  rd.first = [this, &ex, etype, first_only](WorkerSlot& slot, std::vector<Landing>&, const Value& node, int64_t, bool out) {
    ValueList nxt = neighbors_of(slot.store, node, etype, {out}, {}, false, ex.tags.hops, /*used_first_n=*/1, first_only);
    return nxt.empty() ? Value() : nxt[0];
  };
  return rd;
}

EdgeExistsReads Nebula::edge_exists_reads(StepExec& ex, const Args& a) {
  std::string etype = a.str("etype");
  int eid = schema_.edge(etype).id;
  EdgeExistsReads io;
  io.fmt = "nebula";
  io.via = [this, &ex](const ValueList& srcs, const std::string& via_etype) {
    return read_neighbors(ex, srcs, via_etype, "out", {}, false).as_list();
  };
  // one read = the exact edge key of the named family per (candidate, anchor) pair, each distinct candidate once, in
  // first-seen order, until a hit -- real GETs, misses included
  io.list_like = [](const EdgeRead&) { return false; };
  io.probe = [this, &ex, etype, eid](const std::vector<uint64_t>& todo, const std::vector<uint64_t>& anchor_uids,
                 const std::vector<EdgeRead>& rs) {
    std::vector<Value> res = ex.run_range_map(todo.size(), [&](WorkerSlot& slot, size_t ti, std::vector<Landing>& log) {
        uint64_t b = todo[ti];
        bool any = false;
        RtParallel par(slot.store);                          // one member per read: the candidate's reads go together
        for (const EdgeRead& r : rs) {
          par.member();
          for (uint64_t au : anchor_uids) {
            uint64_t owner = r.at_anchor ? au : b, other = r.at_anchor ? b : au;
            // a dated etype's REVERSE key carries the date (forward keys don't); the probe uses a date-0 reverse key,
            // exactly as Python's ("-etype","0") tuple -- it MISSES on a dated store (its real date is the edge's
            // date field), so an in read never finds an edge of a dated etype
            std::string k = (!r.out && dated_key_etype(etype)) ? edge_key_dated(eid, false, owner, 0, other)
                                                             : edge_key(eid, r.out, owner, other);
            bool got = slot.store.get(k).has_value();
            if (got) slot.store.count_used(k.size(), 0);   // existence by key probe; a fetched value = amp;
            if (got) { any = true; break; }                // a miss = the not-found lane, 0 used
          }
        }
        return Value(any);
    }, /*additive_counts=*/true);
    std::vector<bool> found(todo.size());
    for (size_t t = 0; t < todo.size(); t++) found[t] = res[t].as_bool();
    return found;
  };
  return io;
}

}  // namespace evalcpp::nebula
