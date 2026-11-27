#include "formats/arangodb/engine.h"

#include <algorithm>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <stdexcept>
#include <unordered_map>
#include <unordered_set>

#include "formats/arangodb/keys.h"
#include "formats/arangodb/vpack.h"
#include "formats/common.h"
#include "ir/expr.h"
#include "util/vid.h"

namespace evalcpp::arangodb {

ArangoEngine::ArangoEngine(const Schema& schema, const Config& cfg) : Arango(schema, cfg) {
  // the engine's own documents (every property a named member, as real arango and Python's twin store them): the
  // props-codec knob normalizes the COMPARED formats, never the engine reproduction
  if (cfg_.props_codec != PropsCodec::named)
    throw std::runtime_error("arangodb_engine: the engine store holds arango's named documents (--props-codec named)");
}

// --- the vid -> collection map: the engine's "collection/key" id (engine.h) -------------------------------------

void ArangoEngine::warm(Store& store) {
  std::lock_guard<std::mutex> lk(coll_mutex_);
  if (coll_loaded_ || !coll_.empty()) return;
  std::map<int, std::string> by_id;
  for (auto& v : schema_.vertices()) by_id[v.id] = v.label;
  std::unique_ptr<rocksdb::Iterator> it(store.db()->NewIterator(rocksdb::ReadOptions()));
  for (it->Seek("P"); it->Valid() && it->key()[0] == 'P'; it->Next()) {
    std::string_view k(it->key().data(), it->key().size());
    uint32_t c = 0;
    for (size_t i = 1; i < 5; i++) c = (c << 8) | uint8_t(k[i]);
    if (c >= kEdgeDocColl) continue;                        // an edge document's own pk entry: not a vertex
    coll_[trailing_u64(k)] = by_id.at(int(c));
  }
  coll_loaded_ = true;
}

void ArangoEngine::ensure_coll(CountingStore& st) {
  if (!coll_loaded_ && coll_.empty()) warm(st.store());   // fallback; the session warms at open
}

const std::string& ArangoEngine::coll_of(uint64_t uid) {
  auto it = coll_.find(uid);
  if (it == coll_.end()) throw std::runtime_error("arangodb_engine: no collection known for vid " + hex16(uid));
  return it->second;
}

void ArangoEngine::note_vertex(uint64_t uid, const std::string& label) {
  std::lock_guard<std::mutex> lk(coll_mutex_);
  coll_[uid] = label;
}

ArangoEngine::DocLabel ArangoEngine::doc_label(const Value& v, const char* op) {
  if (v.is_null()) throw std::runtime_error(std::string("arangodb_engine ") + op + " needs label (collection or candidates)");
  if (v.is_str()) return {true, v.as_str()};
  v.as_list();                                                       // a list: by vid (throws on any other type)
  return {false, ""};
}

ArangoEngine::Walk ArangoEngine::walk(CountingStore& st, const std::vector<uint64_t>& srcs, const std::string& etype,
                                      const std::string& dir, int64_t depth) {
  std::vector<bool> sides;                                          // "f" (from = out) first, then "t"
  if (dir == "out") sides = {true};
  else if (dir == "in") sides = {false};
  else if (dir == "both") sides = {true, false};
  else throw std::runtime_error("arangodb_engine walk: dir must be out, in or both, got '" + dir + "'");
  int eid_c = schema_.edge(etype).id;
  Walk w;
  struct Front { uint64_t vid; size_t path; };                      // the path this frontier entry ends
  std::vector<Front> frontier;
  for (auto& s : srcs) {                                            // NOT deduped: one expansion per driving row
    w.path_verts.push_back({s});
    w.path_eids.push_back({});
    frontier.push_back({s, w.path_verts.size() - 1});
  }
  for (int64_t level = 1; level <= depth; level++) {
    std::vector<Front> nxt;
    for (auto& f : frontier) {
      w.sources.push_back(f.vid);
      uint64_t u = f.vid;
      for (bool from : sides) {
        std::string prefix = edge_prefix(from, eid_c, u);
        Cursor cur = st.seek(prefix);                               // 1 SEEK per (vertex, side)
        while (cur.valid() && has_prefix(cur.key(), prefix)) {
          uint64_t eid = trailing_u64(cur.key());
          uint64_t other = vp_ref_uid(cur.value());
          cur.next();                                               // 1 NEXT per entry
          const std::vector<uint64_t>& used = w.path_eids[f.path];
          if (std::find(used.begin(), used.end(), eid) != used.end()) continue;   // uniqueEdges: "path"
          std::vector<uint64_t> pv = w.path_verts[f.path];
          std::vector<uint64_t> pe = used;
          pv.push_back(other);
          pe.push_back(eid);
          w.path_verts.push_back(std::move(pv));
          w.path_eids.push_back(std::move(pe));
          size_t p = w.path_verts.size() - 1;
          w.steps.push_back({f.vid, eid, other, level, p});         // one emission PER PATH
          nxt.push_back({other, p});
        }
        // no end_scan: Python's walk declares no absence miss on its scans' last landings
      }
    }
    frontier = std::move(nxt);
  }
  return w;
}

ValueList ArangoEngine::docs(CountingStore& st, const std::vector<uint64_t>& vids, const std::vector<std::string>& props,
                             const DocLabel& label) {
  if (!label.named) ensure_coll(st);
  std::vector<std::string> row_props;
  for (auto& p : props)
    if (p != "id") row_props.push_back(p);
  std::vector<std::string> names = {"_vid", "_label", "id"};
  names.insert(names.end(), row_props.begin(), row_props.end());
  RowBuilder rb(names);                                             // the call's one shape
  ValueList out;
  for (uint64_t u : vids) {                                         // NOT deduped: repeats are real
    std::string coll = label.named ? label.coll : coll_of(u);
    std::string pk = pk_key(schema_.vertex(coll).id, u);
    std::optional<std::string> loc = st.get(pk);                    // 1 GET: pk
    RowFields doc;
    if (loc) {                                                      // the doc read only when it happens
      st.count_used(pk.size(), 0);
      Loc l = loc_of(*loc);
      std::string dk = doc_key(l.coll_id, l.docid);
      std::optional<std::string> raw = st.get(dk);                  // 1 GET: the doc
      uint64_t used = 0;
      if (raw) doc = doc_fields(vdoc_cols(coll), *raw, row_props, &used);
      if (raw) st.count_used(dk.size(), used);
    }
    RowBuilder::Row f = rb.start();
    f.push(Value::vid(u));
    Value lab;                                                      // the concrete collection (free: the _id held it)
    if (label.named) lab = Value(label.coll);
    else if (auto it = coll_.find(u); it != coll_.end()) lab = Value(it->second);
    f.push(lab);
    f.push(key_id_value(u));
    for (auto& p : row_props) f.push(field_or_null(doc, p));
    out.push_back(f.done());
  }
  return out;
}

Value ArangoEngine::edge_doc(CountingStore& st, const std::string& etype, uint64_t eid,
                             const std::vector<std::string>& props) {
  std::string dk = doc_key(edge_doc_coll(schema_.edge(etype).id), eid);
  std::optional<std::string> raw = st.get(dk);                      // 1 GET, the id is in the index key
  if (!raw) return Value::row({});
  uint64_t used = 0;
  Value doc = Value::row(doc_fields(edoc_cols(etype), *raw, props, &used));
  st.count_used(dk.size(), used);
  return doc;
}

// the ONE extra SEEK arango's first traversal pays (measured: +1 on the first traversal, whatever its index count):
// it positions at the head of `etype`'s edge index; the items pass through
Value ArangoEngine::traversal_open(StepExec& ex, const Value& items, const std::string& etype) {
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    std::string k = "f";
    put_u32be(k, uint32_t(schema_.edge(etype).id));
    slot.store.seek(k);
    direct_counts(slot, before, r);
    r.out = items;
  }};
  return ex.run(tasks)[0].out;
}

// a variable-depth TraversalNode: PATHS, not distinct vertices -- one row per path step
Value ArangoEngine::traverse_paths(StepExec& ex, const ValueList& srcs, const std::string& etype,
                                   const std::string& dir, int64_t depth, bool pairs) {
  std::vector<uint64_t> s;
  for (auto& v : srcs) s.push_back(uid_or_throw("arangodb_engine", v));
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    Walk w = walk(slot.store, s, etype, dir, depth);
    ValueList out;
    RowBuilder pb({"src", "dst"});                                  // the call's one shape
    for (auto& st : w.steps) {
      if (!pairs) { out.push_back(Value::vid(st.dst)); continue; }
      RowBuilder::Row p = pb.start();
      p.push(Value::vid(st.src));
      p.push(Value::vid(st.dst));
      out.push_back(p.done());
    }
    direct_counts(slot, before, r);
    r.out = Value::list(std::move(out));
  }};
  return ex.run(tasks)[0].out;
}

static std::unordered_set<uint64_t> vid_set(const Value& v) {
  std::unordered_set<uint64_t> s;
  for (auto& x : v.as_list()) s.insert(uid_or_throw("arangodb_engine", x));
  return s;
}

// a TraversalNode with a postFilter: it reads every vertex it EXPANDS FROM plus every step it EMITS (the filter
// evaluation), then every SURVIVING step again as it emits it; `path_out` also reads each surviving path's vertices
// and edges to build the path variable. Python traverse_filtered's rule, read for read.
Value ArangoEngine::traverse_filtered(const Step& step, const Args& a, StepExec& ex) {
  std::vector<uint64_t> srcs;
  for (auto& v : a.list("srcs")) srcs.push_back(uid_or_throw("arangodb_engine", v));
  std::string etype = a.str("etype"), dir = a.str("dir");
  int64_t depth = a.integer("depth");
  std::vector<std::string> props = a.has("props") ? a.str_list("props") : std::vector<std::string>{};
  std::vector<std::string> edge_props = a.has("edge_props") ? a.str_list("edge_props") : std::vector<std::string>{};
  bool path_out = a.flag("path_out", false);
  const Expr& pred = *step.exprs.at("pred");
  std::optional<std::unordered_set<uint64_t>> keep;
  if (a.has("allowed")) keep = vid_set(a.get("allowed"));
  std::unordered_set<uint64_t> drop;
  if (a.has("excluded")) drop = vid_set(a.get("excluded"));
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    CountingStore& st = slot.store;
    Walk w = walk(st, srcs, etype, dir, depth);
    std::vector<RowFields> rows;
    for (auto& s : w.steps)
      rows.push_back({{"_vid", Value::vid(s.dst)}, {"_src", Value::vid(s.src)}, {"id", key_id_value(s.dst)}, {"_depth", Value(s.depth)}});
    if (!props.empty()) {                                           // the filter evaluation: sources, then steps
      DocLabel label = doc_label(a.opt("label"), "traverse_filtered");
      docs(st, w.sources, props, DocLabel{});                       // the sources: by vid (BY_VID)
      std::vector<uint64_t> dsts;
      for (auto& s : w.steps) dsts.push_back(s.dst);
      ValueList checked = docs(st, dsts, props, label);
      for (size_t i = 0; i < rows.size(); i++)
        for (auto& [k, v] : checked[i].as_row())
          if (k != "_vid") row_set(rows[i], k, v);
    }
    if (!edge_props.empty())
      for (size_t i = 0; i < rows.size(); i++) {
        Value doc = edge_doc(st, etype, w.steps[i].eid, edge_props);
        for (auto& p : edge_props) row_set(rows[i], p, doc.get(p));
      }
    std::vector<size_t> kept_i;
    for (size_t i = 0; i < rows.size(); i++) {
      uint64_t d = w.steps[i].dst;
      if (pred.eval(Value::row(rows[i])).truthy() && (!keep || keep->count(d)) && !drop.count(d)) kept_i.push_back(i);
    }
    // the emission: every survivor, read a second time
    if (!props.empty()) {
      std::vector<uint64_t> kv;
      for (size_t i : kept_i) kv.push_back(w.steps[i].dst);
      docs(st, kv, props, doc_label(a.opt("label"), "traverse_filtered"));
    }
    if (!edge_props.empty()) {
      std::unordered_map<uint64_t, uint64_t> eid_of;                // Python {d: eid}: the LAST step per vertex
      for (auto& s : w.steps) eid_of[s.dst] = s.eid;
      for (size_t i : kept_i) edge_doc(st, etype, eid_of.at(w.steps[i].dst), edge_props);
    }
    // the path variable: each surviving path's vertices (pk + doc) and edges (1 GET each)
    if (path_out)
      for (size_t i : kept_i) {
        size_t p = w.steps[i].path;
        docs(st, w.path_verts[p], props.empty() ? std::vector<std::string>{"_key"} : props, DocLabel{});
        for (uint64_t eid : w.path_eids[p]) edge_doc(st, etype, eid, {});
      }
    ValueList out;
    for (size_t i : kept_i) out.push_back(Value::row(rows[i]));
    direct_counts(slot, before, r);
    r.out = Value::list(std::move(out));
  }};
  return ex.run(tasks)[0].out;
}

// a 1..1 TraversalNode with a postFilter cut short by its LIMIT: per driving row, every edge is read (one batch), the
// start vertex validated, then the steps popped (DFS: last read first) and validated ONE AT A TIME until `limit`
// survive; each survivor is read again as it is emitted
Value ArangoEngine::traverse_filtered_limit(const Step& step, const Args& a, StepExec& ex) {
  std::vector<uint64_t> srcs;
  for (auto& v : a.list("srcs")) srcs.push_back(uid_or_throw("arangodb_engine", v));
  std::string etype = a.str("etype"), dir = a.str("dir");
  int64_t limit = a.integer("limit");
  std::vector<std::string> props = a.has("props") ? a.str_list("props") : std::vector<std::string>{};
  std::vector<std::string> edge_props = a.has("edge_props") ? a.str_list("edge_props") : std::vector<std::string>{};
  std::string order = a.has("order") ? a.str("order") : "dfs";
  if (order != "dfs" && order != "bfs") throw std::runtime_error("traverse_filtered_limit: order must be dfs or bfs, got '" + order + "'");
  const Expr& pred = *step.exprs.at("pred");
  std::optional<std::unordered_set<uint64_t>> keep;
  if (a.has("allowed")) keep = vid_set(a.get("allowed"));
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    CountingStore& st = slot.store;
    ValueList out;
    for (auto& src : srcs) {                                        // one subquery run per driving row
      Walk w = walk(st, {src}, etype, dir, 1);
      std::vector<WalkStep> steps = w.steps;
      if (order == "dfs") std::reverse(steps.begin(), steps.end());   // LIFO: the last edge read pops first
      if (!props.empty()) docs(st, w.sources, props, DocLabel{});  // the start vertex, validated first
      std::vector<std::pair<RowFields, uint64_t>> kept;
      for (auto& s : steps) {
        if (int64_t(kept.size()) >= limit) break;                   // the LimitNode is satisfied: no more pops
        RowFields row = {{"_vid", Value::vid(s.dst)}, {"_src", Value::vid(s.src)}, {"id", key_id_value(s.dst)}, {"_depth", Value(int64_t(1))}};
        if (!props.empty()) {
          ValueList got = docs(st, {s.dst}, props, doc_label(a.opt("label"), "traverse_filtered_limit"));
          for (auto& [k, v] : got[0].as_row())
            if (k != "_vid") row_set(row, k, v);
        }
        if (!edge_props.empty()) {
          Value doc = edge_doc(st, etype, s.eid, edge_props);
          for (auto& p : edge_props) row_set(row, p, doc.get(p));
        }
        if (pred.eval(Value::row(row)).truthy() && (!keep || keep->count(s.dst))) kept.push_back({row, s.eid});
      }
      for (auto& [row, eid] : kept) {                               // the emission: every survivor again
        if (!props.empty())
          docs(st, {row.front().second.as_vid()}, props, doc_label(a.opt("label"), "traverse_filtered_limit"));
        if (!edge_props.empty()) edge_doc(st, etype, eid, {});
      }
      for (auto& [row, eid] : kept) out.push_back(Value::row(row));
    }
    direct_counts(slot, before, r);
    r.out = Value::list(std::move(out));
  }};
  return ex.run(tasks)[0].out;
}

// the document behind EVERY traversal output, no dedup, fetched even when only the key is wanted: pk + doc = 2 GETs
// per step, 1 when the pk misses
Value ArangoEngine::docs_per_step(StepExec& ex, const ValueList& vids, const std::vector<std::string>& props,
                                  const Value& label) {
  DocLabel lab = doc_label(label, "docs_per_step");
  std::vector<uint64_t> v;
  for (auto& x : vids) v.push_back(uid_or_throw("arangodb_engine", x));
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    ValueList out = docs(slot.store, v, props, lab);
    direct_counts(slot, before, r);
    r.out = Value::list(std::move(out));
  }};
  return ex.run(tasks)[0].out;
}

static const std::set<std::string> kEngineOps = {"traversal_open", "traverse_paths", "traverse_filtered",
                                                 "traverse_filtered_limit", "docs_per_step"};

bool ArangoEngine::has_kv_op(const std::string& op) const { return kEngineOps.count(op) > 0 || Arango::has_kv_op(op); }

Value ArangoEngine::run_kv_op(const Step& step, const Args& a, StepExec& ex) {
  const std::string& op = step.op;
  if (is_write_op(op))
    throw std::runtime_error("arangodb_engine: reads only (the engine-fidelity check measures reads; '" + op + "')");
  if (op == "traversal_open") return traversal_open(ex, a.get("items"), a.str("etype"));
  if (op == "traverse_paths")
    return traverse_paths(ex, a.list("srcs"), a.str("etype"), a.has("dir") ? a.str("dir") : "both",
                          a.has("depth") ? a.integer("depth") : 3, a.flag("pairs", false));
  if (op == "traverse_filtered") return traverse_filtered(step, a, ex);
  if (op == "traverse_filtered_limit") return traverse_filtered_limit(step, a, ex);
  if (op == "docs_per_step") return docs_per_step(ex, a.list("vids"), a.str_list("props"), a.opt("label"));
  return Arango::run_kv_op(step, a, ex);
}

}  // namespace evalcpp::arangodb
