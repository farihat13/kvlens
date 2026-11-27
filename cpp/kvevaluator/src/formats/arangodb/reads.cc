// ArangoDB's vertex and edge reads: documents, edge lists, props, chase, edge exists (the dispatch is in format.cc).
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

// --- reads ------------------------------------------------------------------------------------

Value Arango::read_vertex_by_id(StepExec& ex, const Value& vid, const std::vector<std::string>& labels) {
  // probe each candidate COLLECTION's pk for each candidate vid, misses paid (DOCUMENT('Post/x')
  // then DOCUMENT('Comment/x')); even a known label costs its 1 pk GET
  if (labels.empty()) throw std::runtime_error("arangodb read_vertex_by_id needs label (the collection)");
  ValueList cands = vid.is_text() ? ValueList{vid} : vid.as_list();
  std::vector<TaskFn> tasks = {[&, cands](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    ValueList out;
    for (auto& cand : cands) {
      uint64_t u = uid_or_throw("arangodb", cand);
      bool hit = false;
      const std::string* hit_coll = nullptr;
      for (auto& coll : labels) {
        std::string k = pk_key(schema_.vertex(coll).id, u);
        bool got = slot.store.get(k).has_value();
        if (got) slot.store.count_used(k.size(), 0);   // existence probe: a hit's key; the pk locator
        if (got) { hit = true; hit_coll = &coll; break; }   // value = amp; a miss = the not-found lane
      }
      // the collection that holds it travels on the row: the plan's own label when it names one, else the probe's hit
      if (hit) { out.push_back(labels.size() == 1 || ex.tags.probes ? tagged(schema_, cand, *hit_coll) : cand); break; }
    }
    direct_counts(slot, before, r);
    r.out = Value::list(std::move(out));
  }};
  return ex.run(tasks)[0].out;
}

ValueList Arango::neighbors_of(CountingStore& st, const Value& src, const std::string& etype,
                               const std::vector<bool>& dirs, const std::vector<std::string>& edge_props, bool pairs,
                               bool tag, size_t used_first_n, bool first_only) {
  ValueList out;
  neighbors_of_into(st, src, etype, dirs, edge_props, pairs, tag, used_first_n, NeighborItems(edge_props, pairs), out,
                    first_only);
  return out;
}

void Arango::neighbors_of_into(CountingStore& st, const Value& src, const std::string& etype, const std::vector<bool>& dirs, const std::vector<std::string>& edge_props,
                         bool pairs, bool tag, size_t used_first_n,
                         const NeighborItems& item, ValueList& out, bool first_only) {
  uint64_t su = uid_or_throw("arangodb", src);
  int eid_c = schema_.edge(etype).id;
  const std::vector<ColDecl>* cols = &ecols_.at(etype);
  (void)cols;
  RtParallel par(st);   // one edge-index scan per direction, the directions are the plan's
  for (bool d : dirs) {
    par.member();
    std::string prefix = edge_prefix(d, eid_c, su);
    Cursor cur = st.seek(prefix);                                   // 1 SEEK (edge index)
    RtParallel per_edge(st);   // one edge doc per entry of the scan (only with edge props)
    size_t j = 0;
    while (cur.valid() && has_prefix(cur.key(), prefix)) {
      // covering: the value is the opposite vertex's "Label/vid" id -- no edge doc read
      std::string_view raw = cur.value();
      // used (for consumed entries): the edge key + the WHOLE covering value (the ref IS
      // the neighbour output) -- near-zero amp, arango's covering index earning its keep
      if (j < used_first_n) st.count_used(cur.key().size(), raw.size());
      // the value names the far vertex's collection: the hop puts it on the row (no table: what a step knows travels on the row)
      Value other = Value::vid(vp_ref_uid(raw));
      if (tag) other.label = int16_t(schema_.vertex(vp_ref_label(raw)).id);
      if (!edge_props.empty()) {
        uint64_t e = trailing_u64(cur.key());
        std::string dk = doc_key(edge_doc_coll(eid_c), e);
        per_edge.member();
        std::optional<std::string> doc = st.get(dk);   // the edge doc, only for props
        uint64_t used = 0;
        RowFields props = doc ? doc_fields(edoc_cols(etype), *doc, edge_props, &used) : RowFields{};   // the wanted props only
        if (j < used_first_n && doc) st.count_used(dk.size(), used);
        out.push_back(item(src, other, props));
      } else {
        out.push_back(item(src, other, nullptr));
      }
      j++;
      if (first_only) break;                                        // on the first edge: no NEXT past it
      cur.next();                                                   // 1 NEXT per edge
    }
    if (!(first_only && j > 0)) end_scan(cur);                      // the landing that ended the scan: a miss
  }
  return;
}

Value Arango::read_neighbors(StepExec& ex, const ValueList& srcs, const std::string& etype, const std::string& dir,
                             const std::vector<std::string>& edge_props, bool pairs, size_t used_first_n) {
  std::vector<bool> dirs;   // arango sides: "f" = out first, then "t"
  for (bool out : parse_dirs("read_neighbors", dir)) dirs.push_back(out);
  if (!ecols_.count(etype)) throw std::runtime_error("arangodb: unknown etype '" + etype + "'");
  DistinctVids d = distinct_vids("arangodb", srcs, ex.strict_tags);   // a src keeps its label
  WorkerItems items(ex.P(), edge_props, pairs);   // one shape per worker; each src's items appended in index order
  return ex.run_range_append(d.uids.size(), [&](WorkerSlot& slot, size_t ti, ValueList& out, std::vector<Landing>&) {
      neighbors_of_into(slot.store, d.values[ti], etype, dirs, edge_props, pairs, ex.tags.hops, used_first_n, items.of(slot), out);
  }, /*additive_counts=*/true);
}

Value Arango::read_vertex_props(StepExec& ex, const ValueList& vids, const std::vector<std::string>& props,
                                const std::vector<std::string>& label) {
  // pk + doc = 2 GETs per distinct vid (id-indirection); a pk miss costs ONE get.
  // rows carry _label (the concrete collection: the plan's, the one the row carried in, or the probe's hit)
  if (label.empty()) throw std::runtime_error("arangodb read_vertex_props needs label (the collection)");
  std::vector<std::string> row_props;
  bool want_id = false;
  for (auto& p : props) (p == "id" ? want_id = true : (row_props.push_back(p), false));
  bool single = label.size() == 1;
  DistinctVids d = distinct_vids("arangodb", vids, ex.strict_tags);
  // each distinct vid as the input carries it: a label on the row (a hop's edge value, an index, an earlier read)
  // names its collection; with none, the candidate collections are probed in the plan's order, a miss GET each
  const std::vector<Value>& vals = d.values;
  std::vector<std::string> names = {"_vid", "_label"};
  if (want_id) names.push_back("id");
  names.insert(names.end(), row_props.begin(), row_props.end());
  RowBuilder rb(names);                                     // the call's one shape
  std::vector<Value> res = ex.run_range_map(d.uids.size(), [&](WorkerSlot& slot, size_t ti, std::vector<Landing>&) {   // one vid: pk then doc
      uint64_t u = d.uids[ti];
      std::vector<std::string> cands = narrow(schema_, vals[ti], label);   // the carried label, else the plan's list
      std::optional<std::string> carried = tag_label(schema_, vals[ti]);
      // the collection the row reports (_label), when it is known without a read of its own: the plan's one label,
      // the carried one, or the hit of the pk GET a props read does anyway. An id-only read of a vid that carries
      // none reports no _label: no GET is spent to learn a field no step asked for
      std::string coll = single ? label[0] : (cands.size() == 1 && carried ? cands[0] : std::string());
      bool hit = false;
      std::optional<std::string> loc;
      if (!row_props.empty()) {
        for (auto& c : cands) {
          std::string pk = pk_key(schema_.vertex(c).id, u);
          loc = slot.store.get(pk);                            // 1 GET: pk (a miss decides "not this collection")
          if (!loc) continue;
          slot.store.count_used(pk.size(), 0);   // the id-indirection: the locator VALUE is amp
          coll = c;
          hit = true;
          break;
        }
      }
      RowBuilder::Row f = rb.start();
      f.push(hit && ex.tags.probes ? tagged(schema_, vals[ti], coll) : vals[ti]);   // the row carries it on
      f.push(coll.empty() ? Value() : Value(coll));
      if (want_id) f.push(key_id_value(u));
      if (!row_props.empty()) {
        RowFields doc;
        if (loc) {
          Loc l = loc_of(*loc);
          std::string dk = doc_key(l.coll_id, l.docid);
          std::optional<std::string> raw = slot.store.get(dk);        // 1 GET: the doc
          uint64_t used = 0;
          if (raw) doc = doc_fields(vdoc_cols(coll), *raw, row_props, &used);   // the wanted members only
          if (raw) slot.store.count_used(dk.size(), used);
        }
        for (auto& p : row_props) f.push(field_or_null(doc, p));
      }
      return f.done();
  }, /*additive_counts=*/true);
  ValueList out;                                            // input order, duplicates included
  out.reserve(vids.size());
  for (size_t i = 0; i < vids.size(); i++) out.push_back(res[d.at.empty() ? i : d.at[i]]);
  return Value::list(std::move(out));
}

Value Arango::read_anchor_props(StepExec& ex, const ValueList& cands, const std::vector<std::string>& props,
                                const std::vector<std::string>& labels) {
  // probe each candidate collection for the vid until one exists (misses paid), then ONE doc
  // read -- the bare-id anchor genuinely does not know the type, so no _coll consult here
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    r.out = Value::list({});
    for (auto& cand : cands) {
      uint64_t u = uid_or_throw("arangodb", cand);
      for (auto& coll : labels) {
        std::string pk = pk_key(schema_.vertex(coll).id, u);
        std::optional<std::string> loc = slot.store.get(pk);
        if (!loc) continue;                    // a miss decides "not this collection": the not-found lane
        slot.store.count_used(pk.size(), 0);   // the hit's key; the locator value = amp
        Loc l = loc_of(*loc);
        std::string dk = doc_key(l.coll_id, l.docid);
        std::optional<std::string> raw = slot.store.get(dk);
        uint64_t used = 0;
        Value doc = raw ? Value::row(doc_fields(vdoc_cols(coll), *raw, props, &used)) : Value::row({});
        if (raw) slot.store.count_used(dk.size(), used);
        RowFields f;
        f.emplace_back("_vid", ex.tags.probes ? tagged(schema_, cand, coll) : cand);   // the hit's collection, on the row
        f.emplace_back("_label", Value(coll));
        for (auto& p : props) row_set(f, p, p == "id" ? key_id_value(u) : doc.get(p));   // id: key-derived
        r.out = Value::list({Value::row(std::move(f))});
        direct_counts(slot, before, r);
        return;
      }
    }
    direct_counts(slot, before, r);
  }};
  return ex.run(tasks)[0].out;
}

Value Arango::closure_level(StepExec& ex, uint64_t, const ValueList& frontier, const std::string& etype,
                            const std::string& dir) {
  return read_neighbors(ex, frontier, etype, dir, {}, false);
}

ChaseReads Arango::chase_reads(StepExec& ex, const std::string& etype, const std::string& dir) {
  std::vector<bool> dirs = parse_dirs("read_chase", dir);
  ChaseReads rd;
  rd.fmt = "arangodb";
  rd.per_source = name() != "arangodb";   // its engine twin
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

EdgeExistsReads Arango::edge_exists_reads(StepExec& ex, const Args& a) {
  std::string etype = a.str("etype");
  // one read = a scan of the named edge list (from for out, to for in) of each distinct owner, once, membership in
  // memory. Existence used-rule: the queried set is the PAIR list -- the scanned edge entries are the format's
  // implementation of point probes, so nothing here is used (all amp); used_first_n=0 suppresses the per-entry
  // annotation of the inner read_neighbors. A run of anchor-side reads is one walk
  EdgeExistsReads io;
  io.fmt = "arangodb";
  // the via walk's entries are the anchors the step reads: every entry used, as every format
  io.via = [this, &ex](const ValueList& srcs, const std::string& via_etype) {
    return read_neighbors(ex, srcs, via_etype, "out", {}, false).as_list();
  };
  io.anchor_lists = [this, &ex, etype](const ValueList& anchors, const std::vector<bool>& outs, uint64_t) {
    bool out = false, in = false;
    for (bool o : outs) (o ? out : in) = true;
    return read_neighbors(ex, anchors, etype, out && in ? "both" : out ? "out" : "in", {}, false, 0).as_list();
  };
  io.cand_lists = [this, &ex, etype](const ValueList& todo, bool out, const UidSet& anchors, uint64_t) {
    std::unordered_map<uint64_t, size_t> at;
    for (size_t t = 0; t < todo.size(); t++) at.emplace(uid_or_throw("arangodb", todo[t]), t);
    std::vector<bool> found(todo.size(), false);
    Value pairs = read_neighbors(ex, todo, etype, out ? "out" : "in", {}, /*pairs=*/true, 0);
    for (auto& p : pairs.as_list())
      if (anchors.contains(uid_or_throw("arangodb", p.field("dst")))) found[at.at(uid_or_throw("arangodb", p.field("src")))] = true;
    return found;
  };
  return io;
}

std::optional<uint64_t> Arango::locate_edge(CountingStore& st, const std::string& etype, uint64_t su,
                                            const std::string& dst) {
  // scan src's forward covering index until the entry whose value names dst -- the edge doc's
  // id is not a function of its endpoints (mb2 MB88: 3-243 NEXTs for exactly this)
  std::string prefix = edge_prefix(true, schema_.edge(etype).id, su);
  Cursor cur = st.seek(prefix);
  while (cur.valid() && has_prefix(cur.key(), prefix)) {
    size_t i = 0;
    std::string_view raw = cur.value();
    std::string ref = vp_decode(raw, i).as_str();
    if (ref.size() > dst.size() && ref.compare(ref.size() - dst.size() - 1, 1, "/") == 0 &&
        ref.compare(ref.size() - dst.size(), dst.size(), dst) == 0)
      return trailing_u64(cur.key());
    cur.next();
  }
  end_scan(cur);                                 // not found: the scan ran off the prefix
  return std::nullopt;
}

}  // namespace evalcpp::arangodb
