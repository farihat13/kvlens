// Per-type CSR's vertex and edge reads: row chunks, adjacency chunks, neighbours, props, chase, closure, edge exists (the dispatch is in format.cc).
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

std::vector<std::string> PerTypeCsr::owners_of(const std::string& etype, bool out) const {
  std::vector<std::string> o;
  for (auto& v : schema_.vertices())
    if (schema_.owns_edge(v.label, etype, out)) o.push_back(v.label);
  return o;
}

std::vector<std::string> PerTypeCsr::props_labels(const char* op, const Value& vid, const std::vector<std::string>& labels) const {
  if (!labels.empty()) return narrow(schema_, vid, labels);
  if (std::optional<std::string> k = tag_label(schema_, vid)) return {*k};
  throw std::runtime_error(std::string("pertype_csr ") + op + " needs a label (the plan names none and the row carries none)");
}

// (row or none) for vid under the given label(s) in order: one chunk lookup per label tried,
// the first hit wins (a message of unknown type may cost a miss first).
bool PerTypeCsr::row_lookup(Reader& rd, uint64_t uid, const std::vector<std::string>& labels, RowHit& hit) {
  for (auto& lab : labels) {
    const std::vector<ColDecl>* cols = &vcols(lab);
    const PropsChunk* chunk = rd.locate<PropsChunk>(props_prefix(schema_.vertex(lab).id), uid,
        [cols, this](std::string_view raw) { return PropsChunk::decode(&cc_, cols, std::string(raw)); });
    if (chunk) {
      int64_t r = chunk->find(uid);
      if (r >= 0) { hit = {chunk, size_t(r), lab}; return true; }
    }
  }
  return false;
}

Value PerTypeCsr::read_vertex_props(StepExec& ex, const ValueList& vids, const std::vector<std::string>& props,
                                    const std::vector<std::string>& label) {
  std::vector<std::string> row_props;
  bool want_id = false;
  for (auto& p : props) (p == "id" ? want_id = true : (row_props.push_back(p), false));
  // each distinct vid as the input carries it (its first tagged occurrence): a label on the row opens ONE family
  DistinctVids d = distinct_vids("pertype_csr", vids, ex.strict_tags);
  const std::vector<Value>& vals = d.values;
  RowBuilder rb(props_names_id_first(want_id, row_props));   // the call's one shape
  FanOut fo;
  register_props(fo, schema_);
  std::vector<Value> res = ex.run_range_map(d.uids.size(), [&](WorkerSlot& slot, size_t ti, std::vector<Landing>& log) {   // one vid: its row
      uint64_t u = d.uids[ti];
      Reader rd(fo.table, slot, log, 0);
      RowBuilder::Row f = rb.start();
      f.push(vals[ti]);
      if (want_id) f.push(key_id_value(u));                                  // key-derived, free
      if (!row_props.empty()) {
        RowHit h;
        bool found = row_lookup(rd, u, props_labels("read_vertex_props", vals[ti], label), h);
        if (found && ex.tags.probes) f[0] = tagged(schema_, vals[ti], h.label);   // the family that held it
        uint64_t uv = 0;   // used: the asked cells' framed bytes (the configurable format's Condensed rule)
        for (auto& p : row_props) {
          Value cell;
          if (found) {
            int c = h.chunk->col_index(p);
            if (c >= 0) { cell = h.chunk->cell(size_t(c), h.row); uv += h.chunk->cell_bytes(size_t(c), h.row); }
          }
          f.push(std::move(cell));          // a repeated name overwrites, like a dict (as nebula/dgraph/arango)
        }
        slot.store.count_used(0, uv);
      }
      return f.done();
  }, /*additive_counts=*/false);
  ValueList out;                                            // input order, duplicates included
  out.reserve(vids.size());
  for (size_t i = 0; i < vids.size(); i++) out.push_back(res[d.at.empty() ? i : d.at[i]]);
  return Value::list(std::move(out));
}

Value PerTypeCsr::read_anchor_props(StepExec& ex, const ValueList& cands, const std::vector<std::string>& props,
                                    const std::vector<std::string>& labels) {
  FanOut fo;
  register_props(fo, schema_);
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {
    Reader rd(fo.table, slot, r.landings, 0);
    for (auto& cand : cands) {                                   // candidates in order, first hit wins
      RowHit h;
      uint64_t u = uid_or_throw("pertype_csr", cand);
      if (row_lookup(rd, u, props_labels("read_anchor_props", cand, labels), h)) {
        RowFields f;
        f.emplace_back("_vid", ex.tags.probes ? tagged(schema_, cand, h.label) : cand);   // the family that held it
        uint64_t uv = 0;   // used: the asked cells' framed bytes
        for (auto& p : props) {
          if (p == "id") { row_set(f, p, key_id_value(u)); continue; }   // key-derived, free
          int c = h.chunk->col_index(p);
          row_set(f, p, c >= 0 ? h.chunk->cell(size_t(c), h.row) : Value());   // a repeated name overwrites, like a dict
          if (c >= 0) uv += h.chunk->cell_bytes(size_t(c), h.row);
        }
        slot.store.count_used(0, uv);
        r.out = Value::list({Value::row(std::move(f))});
        return;
      }
    }
    r.out = Value::list({});
  }};
  return ex.run(tasks)[0].out;
}

Value PerTypeCsr::edge_props_of(Reader& rd, const std::vector<std::string>& labels, const std::string& etype, uint64_t ordinal,
                                const std::vector<std::string>& wanted, const RowBuilder& rb, uint64_t* used) {
  const std::vector<ColDecl>* cols = &ecols(etype);
  int etype_id = schema_.edge(etype).id;
  for (auto& lab : labels) {
    if (!schema_.owns_edge(lab, etype, true)) continue;   // this label never sources the edge: no probe
    const PropsChunk* ep = rd.locate<PropsChunk>(ep_prefix(schema_.vertex(lab).id, etype_id), ordinal,
        [cols, this](std::string_view raw) { return PropsChunk::decode(&cc_, cols, std::string(raw)); });
    if (ep) {
      int64_t r = ep->find(ordinal);
      if (r >= 0) {
        RowBuilder::Row f = rb.start();
        for (auto& p : wanted) {
          int c = ep->col_index(p);
          f.push(c >= 0 ? ep->cell(size_t(c), size_t(r)) : Value());   // a repeated name overwrites, like a dict
        }
        if (used)
          for (size_t c = 0; c < cols->size(); c++) *used += ep->cell_bytes(c, size_t(r));
        return f.done();
      }
    }
  }
  return Value::row({});
}

std::vector<PerTypeCsr::HopDir> PerTypeCsr::hop_dirs(const char* op, const std::string& etype, const std::vector<bool>& dirs,
                                                    const LabelTagPolicy& tags, bool want_ep) const {
  std::vector<HopDir> plan;
  for (bool d : dirs) {
    HopDir h{d, owners_of(etype, d), -1, {}};
    // the far end's label: the schema's where it gives this direction ONE; more than one: the entry's stored code
    // (far_label), else none
    const std::vector<std::string>& far = far_names(etype, d);
    if (tags.hops && far.size() == 1) h.far = int16_t(schema_.vertex(far[0]).id);
    h.bits = far_bits(etype, d);
    h.names = &far;
    if (h.bits)
      for (auto& l : far) h.ids.push_back(int16_t(schema_.vertex(l).id));
    if (want_ep && !d) {
      // an in hop reads each edge's props under the edge's OUT side's label (the far end). The ordinals restart
      // per (source label, edge type), so with several source labels an ordinal exists in every family and the far
      // end's label must be KNOWN: the entry's stored code says it; without one (far_label off) refuse rather than
      // read another edge's props
      if (far.size() != 1 && !h.bits)
        throw std::runtime_error(std::string("pertype_csr ") + op + ": edge props of '" + etype + "' on an in hop need the "
                                 "far end's label, and the schema gives it " + std::to_string(far.size()) + " (far_label is off)");
      if (far.size() == 1) h.ep_labels = far;
    }
    plan.push_back(std::move(h));
  }
  return plan;
}

void PerTypeCsr::neighbors_of(CountingStore& st, Reader& rd, const Value& s, const std::string& etype,
                              const std::vector<HopDir>& dirs, const std::vector<std::string>& edge_props, bool pairs,
                              bool has_ep, bool want_ep, int etype_id, const LabelTagPolicy& tags, ValueList& out,
                              size_t used_first_n) {
  uint64_t su = uid_or_throw("pertype_csr", s);
  uint64_t used = 0;
  NeighborItems item(edge_props, pairs);                 // one shape for this list's rows
  std::optional<RowBuilder> epb;                         // and one for its edges' props rows
  if (want_ep) epb.emplace(edge_props);
  std::optional<std::string> known = tag_label(schema_, s);
  for (const HopDir& h : dirs) {
    bool d = h.out;
    // the families to probe: the row's label opens ONE (none when the schema says that label holds no such list:
    // catalog knowledge, no KV op); a vid with no label tries the schema's owners of (etype, d) in order
    std::vector<std::string> one;
    if (known && schema_.owns_edge(*known, etype, d)) one = {*known};
    const std::vector<std::string>& cands = known ? one : h.owners;
    for (auto& lab : cands) {
      unsigned bits = h.bits;
      AdjChunk* chunk = const_cast<AdjChunk*>(rd.locate<AdjChunk>(adj_prefix(schema_.vertex(lab).id, etype_id, d), su,
          [has_ep, bits, this](std::string_view raw) { return AdjChunk::decode(&cc_, std::string(raw), has_ep, bits); }));
      if (!chunk) continue;
      int64_t i = chunk->find(su);
      if (i < 0) continue;
      Value src = tags.probes ? tagged(schema_, s, lab) : s;   // the family that held its list names its label
      std::vector<uint64_t> dsts = chunk->dsts(size_t(i));
      std::vector<uint8_t> w = used_first_n ? chunk->id_widths(size_t(i)) : std::vector<uint8_t>{};
      const std::vector<uint64_t>* ords = want_ep ? &chunk->ordinals(size_t(i)) : nullptr;
      std::vector<uint8_t> stored;                             // each entry's far-end label code, when stored
      if (bits) stored = chunk->labels(size_t(i));
      const std::vector<uint8_t>* codes = bits ? &stored : nullptr;
      std::vector<std::string> own = {lab};                    // an out hop: the edge's props live under this family
      for (size_t j = 0; j < dsts.size(); j++) {
        Value dst = Value::vid(dsts[j]);
        dst.label = codes && tags.hops ? h.ids.at((*codes)[j]) : h.far;
        if (j < used_first_n) used += w[j];
        if (want_ep) {
          // an in hop's edge lives under its far end's family: the stored label names it
          std::vector<std::string> far_own;
          if (!d && codes) far_own = {h.names->at((*codes)[j])};
          Value props = edge_props_of(rd, d ? own : codes ? far_own : h.ep_labels, etype, (*ords)[j], edge_props, *epb,
                                      j < used_first_n ? &used : nullptr);
          out.push_back(item(src, dst, &props));
        } else {
          out.push_back(item(src, dst, nullptr));
        }
      }
      break;   // found under this label
    }
  }
  st.count_used(0, used);
}

Value PerTypeCsr::read_neighbors(StepExec& ex, uint64_t group, const ValueList& srcs, const std::string& etype,
                                 const std::string& dir, const std::vector<std::string>& edge_props, bool pairs,
                                 FanOut* shared, size_t used_first_n) {
  std::vector<bool> dirs = parse_dirs("read_neighbors", dir);   // true = out
  bool has_ep = !ecols(etype).empty();
  bool want_ep = !edge_props.empty() && has_ep;
  int etype_id = schema_.edge(etype).id;
  DistinctVids d = distinct_vids("pertype_csr", srcs, ex.strict_tags);   // each src as its row carries it
  FanOut own;
  if (!shared) register_adj(own, schema_, etype);
  FanOut& fo = shared ? *shared : own;
  // one src per index; its probes land in the morsel's log (index order), which the replay
  // turns into the logical counts -- so no additive counts here
  std::vector<HopDir> hdirs = hop_dirs("read_neighbors", etype, dirs, ex.tags, want_ep);
  return ex.run_range_append(d.uids.size(), [&](WorkerSlot& slot, size_t ti, ValueList& out, std::vector<Landing>& log) {
    Reader rd(fo.table, slot, log, group);
    neighbors_of(slot.store, rd, d.values[ti], etype, hdirs, edge_props, pairs, has_ep, want_ep, etype_id, ex.tags, out,
                 used_first_n);   // appended to the morsel's list: no list per src
  }, /*additive_counts=*/false);
}

ChaseReads PerTypeCsr::chase_reads(StepExec& ex, const std::string& etype, const std::string& dir) {
  std::vector<bool> dirs = parse_dirs("read_chase", dir);   // true = out
  bool has_ep = !ecols(etype).empty();
  int etype_id = schema_.edge(etype).id;
  std::vector<HopDir> hdirs = hop_dirs("read_chase", etype, dirs, ex.tags, false);
  // level by level over the distinct chain heads not read before (chase_over: a node is read once per call). ONE
  // BATCH = one hop (the hop's keys are all known once the hop before is read): one chunk cache and one landing group
  // per hop, so a chunk two heads need in the same hop is read and charged once, and a chunk met again at a later hop
  // is a new request (the once-per-batch rule).
  auto fo = std::make_shared<std::unique_ptr<FanOut>>();   // this hop's cache
  ChaseReads rd;
  rd.fmt = "pertype_csr";
  rd.additive_counts = false;
  rd.level = [this, etype, fo](int64_t) {
    *fo = std::make_unique<FanOut>();
    register_adj(**fo, schema_, etype);
  };
  // used: the head's first entry (the replay charges its chunk once per hop)
  rd.dirs = dirs;
  rd.first = [this, &ex, etype, hdirs, has_ep, etype_id, fo](WorkerSlot& slot, std::vector<Landing>& log,
                                                            const Value& node, int64_t hops, bool out) {
    Reader r((*fo)->table, slot, log, uint64_t(hops));
    ValueList nxt;
    std::vector<HopDir> one;                        // this direction's hop
    for (auto& h : hdirs)
      if (h.out == out) one.push_back(h);
    neighbors_of(slot.store, r, node, etype, one, {}, false, has_ep, false, etype_id, ex.tags, nxt,
                 /*used_first_n=*/1);   // the chase takes the first neighbour
    return nxt.empty() ? Value() : nxt[0];
  };
  return rd;
}

Value PerTypeCsr::closure_level(StepExec& ex, uint64_t level, const ValueList& frontier, const std::string& etype,
                                const std::string& dir) {
  return read_neighbors(ex, level, frontier, etype, dir, {}, false);   // one level = one call
}

EdgeExistsReads PerTypeCsr::edge_exists_reads(StepExec& ex, const Args& a) {
  std::string etype = a.str("etype");
  // one read = the named direction's adjacency chunk of each distinct owner, once, membership in memory. A run of
  // anchor-side reads that are not unless_found is one hop (each anchor's chunks together). Every read of the step
  // shares ONE chunk cache and ONE landing group: a chunk two reads touch (the anchor's list and a candidate's list
  // of one family in one chunk) is read and charged once (the configurable format's rule).
  // Group 0 = the via walk (another edge type: its own cache), group 1 = the step's reads whose keys are known before
  // its first read (ONE BATCH). A read marked unless_found reads only the candidates no earlier read found: its keys
  // depend on them, so it is a batch of its own (its own cache and group; the once-per-batch rule)
  struct Caches {
    FanOut step;                                // group 1's
    std::unordered_map<uint64_t, FanOut> own;   // an unless_found read's own group's
  };
  auto caches = std::make_shared<Caches>();
  register_adj(caches->step, schema_, etype);
  // the cache of `group`: the step's for group 1, a new one for an unless_found read's own group
  auto cache_of = [this, etype, caches](uint64_t group) -> FanOut* {
    if (group == 1) return &caches->step;
    auto [it, fresh] = caches->own.try_emplace(group);
    if (fresh) register_adj(it->second, schema_, etype);
    return &it->second;
  };
  EdgeExistsReads io;
  io.fmt = "pertype_csr";
  io.strict_tags = ex.strict_tags;
  io.carry_labels = true;
  io.via = [this, &ex](const ValueList& srcs, const std::string& via_etype) {
    return read_neighbors(ex, 0, srcs, via_etype, "out", {}, false).as_list();
  };
  io.anchor_lists = [this, &ex, etype, cache_of](const ValueList& anchors, const std::vector<bool>& outs, uint64_t group) {
    bool out = false, in = false;
    for (bool o : outs) (o ? out : in) = true;
    return read_neighbors(ex, group, anchors, etype, out && in ? "both" : out ? "out" : "in", {}, false,
                          cache_of(group), /*used_first_n=*/0).as_list();   // membership only: no entry used
  };
  io.cand_lists = [this, &ex, etype, cache_of](const ValueList& todo, bool out, const UidSet& anchors, uint64_t group) {
    std::unordered_map<uint64_t, size_t> at;
    for (size_t t = 0; t < todo.size(); t++) at.emplace(todo[t].as_vid(), t);
    std::vector<bool> found(todo.size(), false);
    Value pairs = read_neighbors(ex, group, todo, etype, out ? "out" : "in", {}, /*pairs=*/true, cache_of(group),
                                 /*used_first_n=*/0);   // membership only
    for (auto& p : pairs.as_list())
      if (anchors.contains(p.field("dst").as_vid())) found[at.at(p.field("src").as_vid())] = true;
    return found;
  };
  return io;
}

}  // namespace evalcpp::pertype_csr
