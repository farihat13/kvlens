// meta's vertex and edge reads: props, neighbours, chase, edge exists (the dispatch is in format.cc).
#include "formats/meta/format.h"
#include "formats/meta/format_shared.h"
#include "codec/decode_counts.h"

#include <memory>
#include <algorithm>
#include <iterator>
#include <optional>
#include <unordered_map>
#include <unordered_set>
#include <stdexcept>
#include <vector>

#include "codec/columns.h"
#include "codec/lists.h"
#include "formats/common.h"
#include "formats/meta/keys.h"
#include "store/unit_value.h"
#include "util/uidset.h"
#include "util/vid.h"

namespace evalcpp::meta {

// --- reads ---------------------------------------------------------------------------------------
// a grouped posting, whole or overflow-split into parts (store/unit_value.h, the same unit dgraph
// uses): 1 SEEK for a whole unit -- the same count as seek_exact -- plus 1 NEXT per further part.
std::optional<std::vector<std::string>> Meta::read_unit(CountingStore& st, const std::string& key) {
  return unit::read(st, key);
}

// register every prefix a packed op can probe (its chunk buckets), resolved once before the fan-out.
// props ops touch a vertex_family per label; adjacency ops touch padj (both dirs) + pep per vid group.
// Format-specific (meta's keys), the counterpart to pertype_csr's register_props/register_adj.
void register_props(FanOut& f, const Schema& schema) {
  for (auto& v : schema.vertices()) f.table.add(f.cache, vertex_family(uint8_t(v.id)));
}
void register_adj(FanOut& f, const Schema& schema, const std::string& etype) {
  uint8_t eid = uint8_t(schema.edge(etype).id);
  f.table.add(f.cache, padj_prefix(eid, true));
  f.table.add(f.cache, padj_prefix(eid, false));
  std::set<int> tags;
  for (auto& v : schema.vertices()) tags.insert(v.tag);
  for (int t : tags) f.table.add(f.cache, pep_prefix(eid, uint8_t(t)));
}

// props (or none) for vid under the candidate label(s): one chunk lookup per label tried via the
// shared cache (ChunkReader::locate), first find() >= 0 wins (a message of unknown type may miss
// first). The reader records a Landing per probe; the neutral replay derives the logical S/N + bytes.
Value Meta::props_of_packed(CountingStore& st, ChunkReader& rd, uint64_t u, const std::vector<std::string>& props,
                            const std::vector<std::string>& labs, const RowBuilder& rb, std::string* hit) {
  RowBuilder::Row f = rb.start();
  f.push(Value::vid(u));
  std::vector<std::string> row_props;
  bool want_id = false;
  for (auto& p : props) (p == "id") ? (void)(want_id = true) : (void)row_props.push_back(p);
  if (want_id) f.push(key_id_value(u));
  if (!row_props.empty()) {
    bool found = false;
    for (auto& lab : labs) {   // probe each candidate label's chunk run; first find() >= 0 wins (pertype_csr)
      const std::vector<ColDecl>* cols = &vcols(lab);
      const PropsChunk* chunk = rd.locate<PropsChunk>(vertex_family(vtype_of(lab)), u,
          [cols, this](std::string_view raw) { return PropsChunk::decode(&cc_, cols, std::string(raw)); });
      if (!chunk) continue;
      int64_t row = chunk->find(u);
      if (row < 0) continue;   // landed on this label's chunk run, but the vid isn't in it -> next label
      uint64_t uv = 0;         // the asked cells' framed bytes (Condensed's project_groups)
      for (auto& p : row_props) {
        int c = chunk->col_index(p);
        f.push(c >= 0 ? chunk->cell(size_t(c), size_t(row)) : Value());
        if (c >= 0) uv += chunk->cell_bytes(size_t(c), size_t(row));
      }
      st.count_used(0, uv);
      found = true;
      if (hit) { *hit = lab; f[0] = tagged(f[0], lab); }   // the probe's row carries the label
      break;
    }
    if (!found)
      for (size_t i = 0; i < row_props.size(); i++) f.push(Value());
  }
  return f.done();
}

Value Meta::edge_props_of_packed(ChunkReader& rd, uint64_t out_su, const std::string& etype,
                                 uint64_t ordinal, const std::vector<std::string>& wanted, const RowBuilder& rb,
                                 uint64_t* used) {
  const std::vector<ColDecl>* cols = &ecols(etype);
  uint8_t eid = uint8_t(schema_.edge(etype).id);
  uint64_t su = out_su;                          // the ep chunks are under the out-src's vid group
  if (!group_owns(su, etype, true)) return Value::row({});   // no label of its group sources the edge: no probe
  const PropsChunk* ep = rd.locate<PropsChunk>(pep_prefix(eid, uint8_t(tag_of_uid(su))), ordinal,
      [cols, this](std::string_view raw) { return PropsChunk::decode(&cc_, cols, std::string(raw)); });
  if (!ep) return Value::row({});
  int64_t r = ep->find(ordinal);
  if (r < 0) return Value::row({});
  RowBuilder::Row f = rb.start();
  for (auto& p : wanted) {
    int c = ep->col_index(p);
    f.push(c >= 0 ? ep->cell(size_t(c), size_t(r)) : Value());
  }
  if (used)
    for (size_t c = 0; c < cols->size(); c++) *used += ep->cell_bytes(c, size_t(r));
  return f.done();
}

ValueList Meta::neighbors_of_packed(CountingStore& st, ChunkReader& rd, uint64_t su, const std::string& etype,
                                    const std::vector<bool>& dirs, const std::vector<std::string>& edge_props,
                                    bool pairs, size_t used_first_n, const Value* src_val) {
  ValueList out;
  neighbors_of_packed_into(st, rd, su, etype, dirs, edge_props, pairs, used_first_n, src_val, NeighborItems(edge_props, pairs),
                           out);
  return out;
}

void Meta::neighbors_of_packed_into(CountingStore& st, ChunkReader& rd, uint64_t su, const std::string& etype,
                                    const std::vector<bool>& dirs, const std::vector<std::string>& edge_props,
                                    bool pairs, size_t used_first_n, const Value* src_val,
                                    const NeighborItems& item, ValueList& out) {
  // used, as Condensed's list (neighbors_of): per direction its first used_first_n entries, each its id bytes + (an
  // edge prop asked) its framed cells; the chunk key books no used_key
  uint64_t used = 0;
  uint8_t eid = uint8_t(schema_.edge(etype).id);
  bool has_ep = !ecols(etype).empty();      // the adj chunk was encoded with ordinals iff the etype carries props
  bool want_ep = !edge_props.empty();
  std::optional<RowBuilder> epb;                         // and one for its edges' props rows
  if (!edge_props.empty()) epb.emplace(edge_props);
  if (!cfg_.adj_of(etype, true).props_own()) {   // inline placement: the chunk holds the edges' props (codec/lists.h)
    const std::vector<ColDecl>* cols = &ecols(etype);
    for (bool d : dirs) {                                    // one (etype, dir) chunk run, no label (keys.h)
      unsigned bits = far_bits(etype, d);
      const lists::ChunkView* chunk = rd.locate<lists::ChunkView>(padj_prefix(eid, d), su,
          [cols, bits](std::string_view raw) { return std::make_shared<lists::ChunkView>(cols, std::string(raw), bits); });
      if (!chunk) continue;
      int64_t i = chunk->find(su);
      if (i < 0) continue;                                   // no such owner: an empty list
      std::vector<uint64_t> dsts = chunk->dsts(size_t(i));
      std::vector<uint8_t> w = used_first_n ? chunk->id_widths(size_t(i)) : std::vector<uint8_t>{};
      const FarIds* far = src_val ? &far_ids(etype, d) : nullptr;
      for (size_t j = 0; j < dsts.size(); j++) {
        int lid = far ? far->of(chunk->label(size_t(i), j)) : -1;
        if (j < used_first_n) used += w[j] + (want_ep ? chunk->props_bytes(size_t(i), j) : 0);
        if (want_ep) {
          Value facets = chunk->props_row(size_t(i), j);
          out.push_back(nbr_item(item, src_val, su, dsts[j], lid, &facets));
        } else {
          out.push_back(nbr_item(item, src_val, su, dsts[j], lid, nullptr));
        }
      }
    }
    st.count_used(0, used);
    return;
  }
  // own placement: the same chunk (codec/lists.h) with one ordinal per edge where the columns block is
  for (bool d : dirs) {                                      // one (etype, dir) chunk run, no label (keys.h)
    unsigned bits = far_bits(etype, d);
    const lists::ChunkView* chunk = rd.locate<lists::ChunkView>(padj_prefix(eid, d), su,
        [bits, has_ep](std::string_view raw) { return std::make_shared<lists::ChunkView>(&kNoCols, std::string(raw), bits, has_ep); });
    if (!chunk) continue;
    int64_t i = chunk->find(su);
    if (i < 0) continue;                                     // no such owner: an empty list
    std::vector<uint64_t> dsts = chunk->dsts(size_t(i));
    std::vector<uint8_t> w = used_first_n ? chunk->id_widths(size_t(i)) : std::vector<uint8_t>{};
    const FarIds* far = src_val ? &far_ids(etype, d) : nullptr;
    for (size_t j = 0; j < dsts.size(); j++) {
      int lid = far ? far->of(chunk->label(size_t(i), j)) : -1;
      if (j < used_first_n) used += w[j];
      if (want_ep) {   // the ep chunk is keyed by the OUT-src's vid group (s for out, dst for in) + the ordinal
        Value facets = edge_props_of_packed(rd, d ? su : dsts[j], etype, chunk->ordinal(size_t(i), j), edge_props, *epb,
                                            j < used_first_n ? &used : nullptr);
        out.push_back(nbr_item(item, src_val, su, dsts[j], lid, &facets));
      } else {
        out.push_back(nbr_item(item, src_val, su, dsts[j], lid, nullptr));
      }
    }
  }
  st.count_used(0, used);
  return;
}

Meta::PropsPlan Meta::props_plan(const Component& layout, const std::vector<std::string>& props,
                                 const std::vector<std::string>& labels) const {
  PropsPlan plan;
  plan.grouped = layout.grouped();
  for (auto& p : props) (p == "id") ? (void)(plan.want_id = true) : (void)plan.row_props.push_back(p);
  for (auto& lab : labels) {
    PropsPlan::Label l;
    l.label = lab;
    if (plan.grouped) {
      if (!plan.row_props.empty()) l.groups = group_plan(lab, plan.row_props);
    } else {
      l.vtype = vtype_of(lab);
      l.cols = &vcols(lab);
      for (auto& p : props) l.attr.push_back(p == "id" ? -1 : attr_id(lab, p));
    }
    plan.labels.push_back(std::move(l));
  }
  return plan;
}
const Meta::PropsPlan::Label& Meta::PropsPlan::of(const std::string& label) const {
  for (auto& l : labels)
    if (l.label == label) return l;
  throw std::runtime_error("meta props read: label " + label + " is not one of the plan's candidates");
}
Meta::PropsScratch& Meta::props_scratch(WorkerSlot& slot, const PropsPlan& plan) const {
  auto& p = slot.scratch[&plan];   // keyed by the call's plan: the pool clears every worker's scratch when a job starts
  if (!p) p = std::make_shared<PropsScratch>();
  return *static_cast<PropsScratch*>(p.get());
}

Value Meta::props_of(CountingStore& st, uint64_t u, const std::vector<std::string>& props,
                     const std::vector<std::string>& labs, const PropsPlan& plan, PropsScratch& scratch,
                     const RowBuilder& rb, std::string* hit) {
  RowBuilder::Row f = rb.start();
  f.push(Value::vid(u));
  if (plan.grouped) {   // group=owner: the label's row(s) per (vtype, vid) -> GET them
    if (plan.want_id) f.push(key_id_value(u));
    if (!plan.row_props.empty()) {
      std::vector<Value>& cells = scratch.cells;   // one per row prop, in order; null until a row answers it
      cells.assign(plan.row_props.size(), Value());
      if (labs.size() == 1) {  // one label: its touched rows (a miss is an absent group, nothing to decide)
        if (project_planned(st, labs[0], u, plan.of(labs[0]).groups, {}, scratch.view, cells) && hit) *hit = labs[0];
      } else {                 // candidate labels: probe each (<= 2 GETs for a wrong one) until the vertex's label
        for (auto& lab : labs)
          if (probe_planned(st, lab, u, plan.of(lab).groups, scratch.view, cells)) { if (hit) *hit = lab; break; }
      }
      if (hit && !hit->empty()) f[0] = tagged(f[0], *hit);   // the row carries the label
      for (auto& c : cells) f.push(std::move(c));
    }
    return f.done();
  }
  ValueList vals;
  std::string decided = split_props(st, u, props, labs, plan, vals);   // singleton
  for (auto& v : vals) f.push(std::move(v));
  if (hit && !decided.empty()) { *hit = decided; f[0] = tagged(f[0], decided); }
  return f.done();
}

std::string Meta::split_props(CountingStore& st, uint64_t u, const std::vector<std::string>& props,
                              const std::vector<std::string>& labs, const PropsPlan& plan, ValueList& vals) {
  // one record per (vtype, vid, attr) holding the cell's payload: one GET per prop (one KV, whatever its type). Over
  // candidate labels the first HIT decides the vertex's label (a vid has one), so later props read that label only --
  // the once-per-vertex label decision Condensed (probe_label) and Chunked (props_of_packed) make
  std::string decided;
  // round trips: with one label every prop's key is known, one batch; over candidates the probes are a chain until the
  // first hit decides the label, and the later props are one batch after it
  std::optional<RtParallel> par;
  if (labs.size() == 1) par.emplace(st);
  for (size_t pi = 0; pi < props.size(); pi++) {
    if (props[pi] == "id") { vals.push_back(key_id_value(u)); continue; }
    if (!par && !decided.empty()) par.emplace(st);
    if (par) par->member();
    std::optional<std::string> raw;
    const ColDecl* col = nullptr;
    for (auto& lab : labs) {
      if (!decided.empty() && lab != decided) continue;
      const PropsPlan::Label& l = plan.of(lab);
      int a = l.attr[pi];   // attr_id(lab, prop), resolved once per call
      if (a < 0) continue;
      std::string k = vertex_attr_key(l.vtype, u, uint8_t(a));
      raw = st.get(k);
      if (raw) { st.count_used(k.size(), raw->size()); col = &(*l.cols)[size_t(a) - 1]; decided = lab; break; }
    }
    vals.push_back(raw ? columns::decode_cell(*col, *raw) : Value());
  }
  return decided;
}

bool Meta::split_exists(CountingStore& st, const std::string& label, uint64_t u) {
  // singleton existence: 1 SEEK to 'v'·vtype·vid -- the label is in the key, so any attr record under that prefix
  // proves membership (ingest refuses a vertex with no stored attr)
  std::string k = vertex_key(vtype_of(label), u);
  Cursor cur = st.seek(k);
  bool hit = cur.valid() && has_prefix(cur.key(), k);
  if (hit) st.count_used(k.size(), 0);   // the prefix decides; the landed attr value = amp
  else cur.mark_miss();
  return hit;
}

ValueList Meta::neighbors_of(CountingStore& st, const std::string& src, const std::string& etype,
                             const std::vector<bool>& dirs, const std::vector<std::string>& edge_props, bool pairs,
                             size_t used_first_n) {
  return neighbors_of(st, uid_or_throw("meta", src), etype, dirs, edge_props, pairs, used_first_n);
}

ValueList Meta::neighbors_of(CountingStore& st, uint64_t su, const std::string& etype,
                             const std::vector<bool>& dirs, const std::vector<std::string>& edge_props, bool pairs,
                             size_t used_first_n, const Value* src_val) {
  ValueList out;
  neighbors_of_into(st, su, etype, dirs, edge_props, pairs, used_first_n, src_val, NeighborItems(edge_props, pairs), out);
  return out;
}

void Meta::neighbors_of_into(CountingStore& st, uint64_t su, const std::string& etype,
                             const std::vector<bool>& dirs, const std::vector<std::string>& edge_props, bool pairs,
                             size_t used_first_n, const Value* src_val, const NeighborItems& item, ValueList& out) {
  uint8_t eid = uint8_t(schema_.edge(etype).id);
  // each walked direction by its own layout (config.h adj_of), in the caller's direction order
  const std::vector<ColDecl>* cols = &ecols(etype);
  auto list_dir = [&](bool d) {   // group=owner: one list per (owner, etype, dir), codec/lists.h
    unsigned bits = far_bits(etype, d);
    std::string k = adj_prefix(eid, d, su);
    std::optional<std::vector<std::string>> parts = read_unit(st, k);
    if (!parts) return;
    st.count_used(k.size(), 0);
    uint64_t used = 0;
    size_t j = 0;
    const FarIds* far = src_val ? &far_ids(etype, d) : nullptr;
    for (auto& raw : *parts) {
      lists::View lv(cols, raw, bits);
      for (size_t e = 0; e < lv.size(); e++, j++) {
        int lid = far ? far->of(lv.label(e)) : -1;
        if (j < used_first_n) used += lv.id_bytes(e) + (edge_props.empty() ? 0 : lv.props_bytes(e));
        if (edge_props.empty()) {                // no prop asked: the entries' rows are not decoded (as the other layouts)
          out.push_back(nbr_item(item, src_val, su, lv.dsts()[e], lid, nullptr));
          continue;
        }
        Value facets = lv.props_row(e);
        out.push_back(nbr_item(item, src_val, su, lv.dsts()[e], lid, &facets));
      }
    }
    st.count_used(0, used);
  };
  auto record_dir = [&](bool d) {   // singleton_per_edge: one record per directed edge; prefix scan
    bool coded = far_bits(etype, d) > 0;                     // the record's first byte is the far label's code
    std::string prefix = adj_prefix(eid, d, su);
    Cursor cur = st.seek(prefix);
    size_t j = 0;
    const FarIds* far = src_val ? &far_ids(etype, d) : nullptr;
    while (cur.valid() && has_prefix(cur.key(), prefix)) {
      if (j < used_first_n) st.count_used(cur.key().size(), 0);
      std::string_view raw = cur.value();
      // the decode counters (codec/decode_counts.h): a Split record whose value holds a byte is ONE decoded entry (a
      // cell record, one field; its code byte is the stored byte parsed here, its props add theirs below). An empty
      // record is a key crossed (the store's rule: nothing to read) -- the entry's id came from the key
      if (!raw.empty()) { DecodeCounts& dc = decode_counts(); dc.cells++; dc.fields++; dc.bytes += coded ? 1 : 0; }
      uint8_t code = coded ? uint8_t(raw.at(0)) : 0;
      if (coded) raw.remove_prefix(1);
      int lid = far ? far->of(code) : -1;
      if (edge_props.empty()) {
        out.push_back(nbr_item(item, src_val, su, edge_dst(cur.key()), lid, nullptr));
      } else {
        columns::View cv(cols, raw, 0, 1);   // the edge's columns, one row
        Value props = cv.row(0);
        if (j < used_first_n) {
          uint64_t uv = 0;
          for (auto& ep : edge_props) {
            int c = cv.col_index(ep);
            if (c >= 0) uv += cv.cell_bytes(size_t(c), 0);
          }
          st.count_used(0, uv);
        }
        out.push_back(nbr_item(item, src_val, su, edge_dst(cur.key()), lid, &props));
      }
      j++;
      cur.next();
    }
    end_scan(cur);
  };
  RtParallel par(st);   // the walked directions are the plan's: their reads go together
  for (bool d : dirs) {
    par.member();
    if (cfg_.adj_of(etype, d).grouped()) list_dir(d);
    else record_dir(d);
  }
  return;
}

Value Meta::read_vertex_by_id(StepExec& ex, const Value& vid, const std::vector<std::string>& labels) {
  std::vector<std::string> labs = labels_or_throw("read_vertex_by_id", labels);
  ValueList cands = vid.is_text() ? ValueList{vid} : vid.as_list();
  bool packed = props_layout("read_vertex_by_id", labs).packed();   // packed: existence via the chunk locate, not a point GET
  FanOut fo;
  if (packed) register_props(fo, schema_);
  std::vector<Value> r = ex.run_range_map(1, [&](WorkerSlot& slot, size_t, std::vector<Landing>& log) {
      ChunkReader rd(fo.table, slot, log, 0);
      ValueList out;
      for (auto& cand : cands) {   // grouped: existence by a probe on 'v'·vtype·vid
        uint64_t u = uid_or_throw("meta", cand);
        std::string hit;
        // the label the candidate's value carries decides it, no probe -- existence too, unless a vertex write ran
        // earlier in this run (a tag states a label, not that the vertex is still there)
        if (std::optional<std::string> k = tag_label(cand); k && !ex.after_vertex_write) {
          if (std::find(labs.begin(), labs.end(), *k) != labs.end()) { out.push_back(cand); break; }
          continue;
        }
        for (auto& tag : labs) {
          if (packed) {
            const std::vector<ColDecl>* cols = &vcols(tag);
            const PropsChunk* chunk = rd.locate<PropsChunk>(vertex_family(vtype_of(tag)), u,
                [cols, this](std::string_view raw) { return PropsChunk::decode(&cc_, cols, std::string(raw)); });
            if (chunk && chunk->find(u) >= 0) { hit = tag; break; }
          } else {
            std::string k = vertex_key(vtype_of(tag), u);
            if (slot.store.get(k)) { slot.store.count_used(k.size(), 0); hit = tag; break; }
          }
        }
        if (!hit.empty()) { out.push_back(ex.tags.probes ? tagged(cand, hit) : cand); break; }
      }
      return Value::list(std::move(out));
  }, /*additive_counts=*/!packed);
  return r[0];
}

Value Meta::read_vertex_by_id_split(StepExec& ex, const ValueList& cands, const std::vector<std::string>& labels) {
  // singleton existence: the vertex-prefix SEEK per label (none given: every label of the schema); the first
  // candidate that exists wins, as in every layout
  std::vector<std::string> all_labels;
  for (auto& v : schema_.vertices()) all_labels.push_back(v.label);
  std::vector<Value> r = ex.run_range_map(1, [&](WorkerSlot& slot, size_t, std::vector<Landing>&) {
      for (auto& cand : cands) {
        uint64_t u = uid_or_throw("meta", cand);
        const std::vector<std::string>& labs = labels.empty() ? all_labels : labels;
        if (std::optional<std::string> k = tag_label(cand); k && !ex.after_vertex_write) {   // its value's label decides it
          if (std::find(labs.begin(), labs.end(), *k) != labs.end()) return Value::list({cand});
          continue;
        }
        for (auto& lab : labs)   // no label given: every label (the vid is opaque)
          if (split_exists(slot.store, lab, u)) return Value::list({ex.tags.probes ? tagged(cand, lab) : cand});
      }
      return Value::list({});
  }, /*additive_counts=*/true);
  return r[0];
}

Value Meta::read_vertex_props(StepExec& ex, const ValueList& vids, const std::vector<std::string>& props,
                              const std::vector<std::string>& labels) {
  std::vector<std::string> labs = labels_or_throw("read_vertex_props", labels);
  DistinctVids d = distinct_vids("meta", vids, ex.strict_tags);   // each vid as it arrived (its label, if carried)
  bool packed = props_layout("read_vertex_props", labs).packed();   // chunked reads feed landings (replay), not summed counts
  FanOut fo;   // one per op call, its buckets shared across the op's tasks (thread-safe; unused when !packed)
  if (packed) register_props(fo, schema_);
  const Component& layout = props_layout("read_vertex_props", labs);
  RowBuilder rb(props_row_names(layout, props));          // the call's one shape
  PropsPlan plan = props_plan(layout, props, labs);       // the per-label work, once per call (not per vertex)
  std::vector<Value> res = ex.run_range_map(d.uids.size(), [&](WorkerSlot& slot, size_t ti, std::vector<Landing>& log) {
      std::vector<std::string> mine = narrow(d.values[ti], labs);   // the label its row carries: no probing
      if (mine.empty()) return absent_row(layout, d.uids[ti], props, rb);  // of none of the candidates: nothing to read
      std::string hit;   // the label whose row was found: the output row's _vid carries it (probes tag)
      if (!packed)
        return props_of(slot.store, d.uids[ti], props, mine, plan, props_scratch(slot, plan), rb,
                        ex.tags.probes ? &hit : nullptr);
      ChunkReader rd(fo.table, slot, log, 0);
      return props_of_packed(slot.store, rd, d.uids[ti], props, mine, rb, ex.tags.probes ? &hit : nullptr);
  }, /*additive_counts=*/!packed);
  ValueList out; out.reserve(vids.size());
  for (size_t i = 0; i < vids.size(); i++) out.push_back(res[d.at.empty() ? i : d.at[i]]);
  return Value::list(std::move(out));
}

Value Meta::read_anchor_props(StepExec& ex, const ValueList& cands, const std::vector<std::string>& props,
                              const std::vector<std::string>& labels) {
  std::vector<std::string> labs = labels_or_throw("read_anchor_props", labels);
  const Component& pl = props_layout("read_anchor_props", labs);
  if (!pl.grouped()) {   // singleton: per candidate the first hit decides its label (split_props); a candidate with no
                         // hit on a wanted prop gets the existence probe per label (Condensed's group-0 rule); the
                         // first candidate that exists ends the read
    PropsPlan plan = props_plan(pl, props, labs);   // the attr ids per label, once per call
    std::vector<Value> r = ex.run_range_map(1, [&](WorkerSlot& slot, size_t, std::vector<Landing>&) {
        for (auto& cand : cands) {
          uint64_t u = uid_or_throw("meta", cand);
          std::vector<std::string> mine = narrow(cand, labs);   // a learned label: no probing
          if (mine.empty()) continue;                                          // of none of the candidates
          RowFields f;
          f.emplace_back("_vid", cand);
          ValueList vals;
          std::string decided = split_props(slot.store, u, props, mine, plan, vals);
          for (size_t i = 0; i < props.size(); i++) row_set(f, props[i], std::move(vals[i]));   // one row: no shape kept
          if (decided.empty()) {
            for (auto& lab : mine)
              if (split_exists(slot.store, lab, u)) { decided = lab; break; }
            if (decided.empty()) continue;
          }
          if (ex.tags.probes) f.front().second = tagged(f.front().second, decided);   // the row carries the label
          return Value::list({Value::row(std::move(f))});
        }
        return Value::list({});
    }, /*additive_counts=*/true);
    return r[0];
  }
  if (pl.packed()) {   // packed: locate each cand's chunk (probe labels), first found wins
    FanOut fo;
    register_props(fo, schema_);
    std::vector<Value> r = ex.run_range_map(1, [&](WorkerSlot& slot, size_t, std::vector<Landing>& log) {
        ChunkReader rd(fo.table, slot, log, 0);
        for (auto& cand : cands) {
          uint64_t u = uid_or_throw("meta", cand);
          for (auto& tag : narrow(cand, labs)) {   // a learned label: no probing
            const std::vector<ColDecl>* cols = &vcols(tag);
            const PropsChunk* chunk = rd.locate<PropsChunk>(vertex_family(vtype_of(tag)), u,
                [cols, this](std::string_view raw) { return PropsChunk::decode(&cc_, cols, std::string(raw)); });
            if (!chunk) continue;
            int64_t row = chunk->find(u);
            if (row < 0) continue;
            RowFields f;
            f.emplace_back("_vid", cand);
            uint64_t uv = 0;   // the asked cells' framed bytes (Condensed's project_groups)
            for (auto& p : props) {
              if (p == "id") { row_set(f, p, key_id_value(u)); continue; }   // key-derived, free
              int c = chunk->col_index(p);
              row_set(f, p, c >= 0 ? chunk->cell(size_t(c), size_t(row)) : Value());
              if (c >= 0) uv += chunk->cell_bytes(size_t(c), size_t(row));
            }
            slot.store.count_used(0, uv);
            if (ex.tags.probes) f.front().second = tagged(f.front().second, tag);   // the row carries the label
            return Value::list({Value::row(std::move(f))});
          }
        }
        return Value::list({});
    }, /*additive_counts=*/false);
    return r[0];
  }
  // grouped (nebula, All-Condensed): the first cand+tag whose existence row (group 0) exists wins; its other touched
  // group rows follow (a one-group label: the one row GET, as Condensed)
  std::vector<std::string> row_props;
  for (auto& p : props) if (p != "id") row_props.push_back(p);
  std::vector<Value> r = ex.run_range_map(1, [&](WorkerSlot& slot, size_t, std::vector<Landing>&) {
      for (auto& cand : cands) {
        uint64_t u = uid_or_throw("meta", cand);
        for (auto& tag : narrow(cand, labs)) {   // a learned label: no probing
          std::map<std::string, Value> cells;
          if (!probe_label(slot.store, tag, u, row_props, cells)) continue;   // not this label
          RowFields f;
          f.emplace_back("_vid", ex.tags.probes ? tagged(cand, tag) : cand);   // the row carries the label it found
          for (auto& p : props) {
            if (p == "id") { row_set(f, p, key_id_value(u)); continue; }   // key-derived, free
            auto c = cells.find(p);
            row_set(f, p, c != cells.end() ? c->second : Value());
          }
          return Value::list({Value::row(std::move(f))});
        }
      }
      return Value::list({});
  }, /*additive_counts=*/true);
  return r[0];
}

Meta::HopShape Meta::hop_shape(const std::string& etype, const std::string& dir,
                               const std::vector<std::string>& owner_labels) const {
  HopShape s;
  // a direction is skipped only when the PLAN names its sources' labels and none of them owns it by the schema (the
  // vid is opaque: no read takes a type from a vid's bits -- python/kvevaluator/schema/vids.py)
  for (bool d : dirs_of("read_neighbors", etype, dir)) {
    bool owned = owner_labels.empty();
    for (auto& lab : owner_labels) owned = owned || schema_.owns_edge(lab, etype, d);
    if (owned) s.dirs.push_back(d);
  }
  // chunked adjacency feeds landings (replay), not summed counts, and one fan-out is one or the other: the walked
  // directions by class. One class (every etype whose directions share a layout): one pass, as ever. Two classes
  // (one direction Chunked, the other not): one pass each, a source's neighbours joined in the directions' order
  bool any_packed = false, any_unpacked = false;
  for (bool d : s.dirs) (cfg_.adj_of(etype, d).packed() ? any_packed : any_unpacked) = true;
  s.two = any_packed && any_unpacked;
  s.packed = s.dirs.empty() ? cfg_.adj_of(etype, true).packed() : !any_unpacked;   // of the one pass
  return s;
}

Value Meta::read_neighbors(StepExec& ex, const ValueList& srcs, const std::string& etype, const std::string& dir,
                           const std::vector<std::string>& edge_props, bool pairs, size_t used_first_n,
                           uint64_t group, const std::vector<std::string>& owner_labels, FanOut* shared) {
  HopShape shape = hop_shape(etype, dir, owner_labels);   // the decisions (predict.cc walks the same shape)
  const std::vector<bool>& dirs = shape.dirs;
  bool two = shape.two, packed = shape.packed;
  // each distinct src as it arrived (its first tagged occurrence's label); strict only where the labels are read
  DistinctVids d = distinct_vids("meta", srcs, ex.strict_tags && ex.tags.hops);
  FanOut own_fo;   // one per op call (the passed group), its buckets shared across tasks (adj + ep prefixes);
  FanOut& fo = shared ? *shared : own_fo;   // a caller that spans levels (closure) passes its one cache scope
  if ((packed || two) && !shared) register_adj(fo, schema_, etype);
  // the hop tags each neighbour with what its entry tells (far_label_id): the label travels on the row
  bool tag = ex.tags.hops;
  auto pass = [&](bool packed, const std::vector<bool>& dirs) {
    return ex.run_range_map(d.uids.size(), [&](WorkerSlot& slot, size_t ti, std::vector<Landing>& log) {
        const Value* sv = tag ? &d.values[ti] : nullptr;
        if (!packed)
          return Value::list(neighbors_of(slot.store, d.uids[ti], etype, dirs, edge_props, pairs, used_first_n, sv));
        ChunkReader rd(fo.table, slot, log, group);
        return Value::list(neighbors_of_packed(slot.store, rd, d.uids[ti], etype, dirs, edge_props, pairs, used_first_n, sv));
    }, /*additive_counts=*/!packed);
  };
  if (!two) {
    // one pass: each source's items appended in index order (no list per source), over one shape per worker
    WorkerItems items(ex.P(), edge_props, pairs);
    return ex.run_range_append(d.uids.size(), [&](WorkerSlot& slot, size_t ti, ValueList& out, std::vector<Landing>& log) {
        const Value* sv = tag ? &d.values[ti] : nullptr;
        if (!packed) {
          neighbors_of_into(slot.store, d.uids[ti], etype, dirs, edge_props, pairs, used_first_n, sv, items.of(slot), out);
        } else {
          ChunkReader rd(fo.table, slot, log, group);
          neighbors_of_packed_into(slot.store, rd, d.uids[ti], etype, dirs, edge_props, pairs, used_first_n, sv, items.of(slot), out);
        }
    }, /*additive_counts=*/!packed);
  }
  // two passes (the directions' layouts differ): a source's neighbours joined in the directions' order
  std::vector<Value> per_src = pass(cfg_.adj_of(etype, dirs[0]).packed(), std::vector<bool>{dirs[0]});
  std::vector<Value> second = pass(cfg_.adj_of(etype, dirs[1]).packed(), {dirs[1]});
  return joined_lists(per_src, &second);
}

Value Meta::closure_level(StepExec& ex, uint64_t level, const ValueList& frontier, const std::string& etype,
                          const std::string& dir) {
  // ONE BATCH = one level (its frontier): read_neighbors opens the level's own cache, the level is its replay group,
  // so a chunk a later level needs again is a new request (the once-per-batch rule)
  return read_neighbors(ex, frontier, etype, dir, {}, false, SIZE_MAX, level, {}, nullptr);
}

ChaseReads Meta::chase_reads(StepExec& ex, const std::string& etype, const std::string& dir) {
  // level by level over the frontier of chain heads (meta-v4 row 1u): a node is read once (two chains that meet share
  // it). ONE BATCH = one level (its frontier's keys are known before the level's first read): one chunk cache and one
  // replay group per level, so a chunk the next level needs again is a new request (the once-per-batch rule,
  // before: one cache for the whole call). A hop takes the node's FIRST neighbour, so Split lands on the
  // first record and stops (1 SEEK, no NEXT)
  std::vector<bool> dirs = dirs_of("read_chase", etype, dir);
  // a chase reads one direction in every plan. Both directions of an etype with one direction Chunked and the
  // other not would need a counted and a replayed read inside one task: not built, refused loudly
  bool packed = cfg_.adj_of(etype, dirs[0]).packed();
  for (bool d : dirs)
    if (cfg_.adj_of(etype, d).packed() != packed)
      throw std::runtime_error("meta read_chase: '" + etype + "' has one direction Chunked and the other not "
                               "(adjacency_by_etype_dir); a chase over both directions of it is not built");
  uint8_t eid = uint8_t(schema_.edge(etype).id);
  const std::vector<ColDecl>* cols = &ecols(etype);
  bool tag = ex.tags.hops;   // a hop tags its first neighbour like any hop (read_neighbors)
  auto fo = std::make_shared<std::unique_ptr<FanOut>>();   // this level's cache
  ChaseReads rd;
  rd.fmt = "meta";
  rd.additive_counts = !packed;
  rd.level = [this, etype, packed, fo](int64_t) {
    *fo = std::make_unique<FanOut>();
    if (packed) register_adj(**fo, schema_, etype);
  };
  rd.dirs = dirs;
  // the node's first neighbour in direction `d` (chase_over tries the directions in order)
  rd.first = [this, etype, packed, eid, cols, tag, fo](WorkerSlot& slot, std::vector<Landing>& log,
                                                     const Value& node, int64_t hops, bool d) {
    uint64_t su = uid_or_throw("meta", node);
    auto found = [&](uint64_t nbr, uint8_t code) {
      Value v = Value::vid(nbr);
      int lid = tag ? far_label_id(etype, d, code) : -1;
      if (lid >= 0) v.label = int16_t(lid);
      return v;
    };
    Value self = Value::vid(su);
    if (packed) {
      ChunkReader rd_((*fo)->table, slot, log, uint64_t(hops));
      ValueList n = neighbors_of_packed(slot.store, rd_, su, etype, {d}, {}, false, 1, tag ? &self : nullptr);
      if (!n.empty()) return n[0];   // the first neighbour, tagged by the hop
    } else if (cfg_.adj_of(etype, d).grouped()) {   // Condensed: the unit's first entry (1 SEEK, + parts)
      std::string k = adj_prefix(eid, d, su);
      auto parts = read_unit(slot.store, k);
      if (!parts) return Value();
      for (auto& raw : *parts) {
        lists::View lv(cols, raw, far_bits(etype, d));
        if (lv.size()) { slot.store.count_used(k.size(), lv.id_bytes(0)); return found(lv.dsts()[0], lv.label(0)); }
      }
    } else {                                       // Split: 1 SEEK landing on the first record, no NEXT
      std::string prefix = adj_prefix(eid, d, su);
      Cursor c = slot.store.seek(prefix);
      if (c.valid() && has_prefix(c.key(), prefix)) {
        slot.store.count_used(c.key().size(), 0);
        uint8_t code = far_bits(etype, d) ? uint8_t(c.value().at(0)) : 0;   // the landed record's first byte
        if (far_bits(etype, d)) { DecodeCounts& dc = decode_counts(); dc.cells++; dc.fields++; dc.bytes++; }   // as neighbors_of's Split record
        return found(edge_dst(c.key()), code);
      }
      c.mark_miss();
    }
    return Value();
  };
  return rd;
}

EdgeExistsReads Meta::edge_exists_reads(StepExec& ex, const Args& a) {
  // The plan names the reads (formats/common.h EdgeRead); each is done by the layout of the ONE (etype, dir) family
  // it names:
  //   Split                one GET per (candidate, anchor) on that family's key, until a hit
  //   Condensed / Chunked  the family's list of each distinct owner (the anchors, or the candidates), read once
  // A run of consecutive anchor-side list reads that are not `unless_found` is one fan-out (each anchor's lists
  // together, as a hop over those directions reads them).
  std::string etype = a.str("etype");
  uint8_t eid = uint8_t(schema_.edge(etype).id);
  bool any_packed = false;
  for (auto& r : edge_reads(a, "meta")) {
    if (!r.out && !reverse_available(etype))
      throw std::runtime_error("meta read_edge_exists: this store keeps no in lists of '" + etype + "' (a read with dir in "
                               "needs the reverse_adjacency capability)");
    any_packed = any_packed || cfg_.adj_of(etype, r.out).packed();
  }
  // packed: one cache scope (one replay group) for the reads of the step whose keys are known before its first read
  // (ONE BATCH): a chunk two of them need is read once. A read marked unless_found reads only the candidates no
  // earlier read found: its keys depend on them, so it is a batch of its own, with its own cache and group (the
  // once-per-batch rule; Per-type CSR's edge check follows the same rule).
  auto fo = std::make_shared<FanOut>();
  if (any_packed) register_adj(*fo, schema_, etype);
  // the lists of `owners` in `dirs`: one pass per class (a Chunked direction is replayed, another counted directly),
  // one pass when the directions share a class
  auto lists_of = [this, &ex, etype, any_packed, fo](const ValueList& owners, const std::vector<bool>& dirs, uint64_t group) {
    std::vector<uint64_t> vids;
    vids.reserve(owners.size());
    for (auto& o : owners) vids.push_back(o.as_vid());
    FanOut own;                                           // a batch of its own (group != 1): its own cache
    if (group != 1 && any_packed) register_adj(own, schema_, etype);
    std::vector<Value> all;
    for (bool packed : {true, false}) {
      std::vector<bool> ds;
      for (bool d : dirs)
        if (cfg_.adj_of(etype, d).packed() == packed) ds.push_back(d);
      if (ds.empty()) continue;
      std::vector<Value> got = ex.run_range_map(vids.size(), [&](WorkerSlot& slot, size_t ti, std::vector<Landing>& log) {
          if (!packed) return Value::list(neighbors_of(slot.store, vids[ti], etype, ds, {}, false, /*used_first_n=*/0));
          ChunkReader rd((group == 1 ? *fo : own).table, slot, log, group);
          return Value::list(neighbors_of_packed(slot.store, rd, vids[ti], etype, ds, {}, false, 0));
      }, /*additive_counts=*/!packed);
      if (all.empty()) { all = std::move(got); continue; }
      for (size_t i = 0; i < got.size(); i++) {   // a second class: the same vid's lists joined
        ValueList both = all[i].as_list();
        for (auto& n : got[i].as_list()) both.push_back(n);
        all[i] = Value::list(std::move(both));
      }
    }
    return all;
  };
  EdgeExistsReads io;
  io.fmt = "meta";
  // the via walk reads the VIA etype's lists, every entry used, in every layout
  io.via = [this, &ex](const ValueList& srcs, const std::string& via_etype) {
    return read_neighbors(ex, srcs, via_etype, "out", {}, false, SIZE_MAX, /*group=*/0).as_list();
  };
  io.list_like = [this, etype](const EdgeRead& r) {
    const Component& c = cfg_.adj_of(etype, r.out);
    return c.packed() || c.grouped();
  };
  io.anchor_lists = [lists_of](const ValueList& anchors, const std::vector<bool>& outs, uint64_t group) {
    ValueList all;
    for (auto& v : lists_of(anchors, outs, group))
      for (auto& n : v.as_list()) all.push_back(n);
    return all;
  };
  io.cand_lists = [lists_of](const ValueList& todo, bool out, const UidSet& anchors, uint64_t group) {
    std::vector<Value> back = lists_of(todo, {out}, group);
    std::vector<bool> found(todo.size(), false);
    for (size_t t = 0; t < todo.size(); t++)
      for (auto& n : back[t].as_list())
        if (anchors.contains(n.as_vid())) { found[t] = true; break; }
    return found;
  };
  // Split: the edge's own key in this family, a point GET per (candidate, anchor)
  io.probe = [&ex, eid](const std::vector<uint64_t>& todo, const std::vector<uint64_t>& anchor_uids,
                 const std::vector<EdgeRead>& rs) {
    std::vector<Value> res = ex.run_range_map(todo.size(), [&](WorkerSlot& slot, size_t ti, std::vector<Landing>&) {
        uint64_t b = todo[ti];
        bool any = false;
        RtParallel par(slot.store);   // one member per read: the candidate's reads go together
        for (const EdgeRead& r : rs) {
          par.member();
          for (uint64_t au : anchor_uids) {
            std::string k = r.at_anchor ? edge_key(eid, r.out, au, b) : edge_key(eid, r.out, b, au);
            if (slot.store.get(k).has_value()) { slot.store.count_used(k.size(), 0); any = true; break; }
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

}  // namespace evalcpp::meta
