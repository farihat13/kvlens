// Dgraph's vertex and edge reads: postings, neighbours, props, chase, edge exists (the dispatch is in format.cc).
#include "formats/dgraph/format.h"
#include "codec/decode_counts.h"

#include <algorithm>
#include <cstring>
#include <numeric>
#include <optional>
#include <stdexcept>

#include "codec/prim.h"
#include "codec/rowv2.h"
#include "codec/varint.h"
#include "formats/common.h"
#include "formats/dgraph/keys.h"
#include "codec/posting.h"
#include "index/sorted_index.h"
#include "store/unit_value.h"
#include "util/uidset.h"
#include "util/vid.h"

namespace evalcpp::dgraph {

// --- reads ------------------------------------------------------------------------------------

std::optional<std::string> Dgraph::read_list(CountingStore& st, const std::string& key) {
  return st.seek_exact(key);                    // 1 SEEK, whole value at one key, 0 NEXT; a miss
}                                               // (the SEEK landed on a foreign key) = a miss lane

std::optional<std::vector<std::string>> Dgraph::read_unit(CountingStore& st, const std::string& key) {
  if (!cfg_.chunked()) {                        // dgraph-c∞: today's single-KV read, verbatim
    std::optional<std::string> raw = read_list(st, key);
    if (!raw) return std::nullopt;
    return std::vector<std::string>{std::move(*raw)};
  }
  return unit::read(st, key);                   // dgraph-c64k: the parts (1 SEEK + N = parts)
}

std::vector<char> Dgraph::kinds_of(const std::string& op, const std::string& etype, const std::string& dir) const {
  if (!etypes_.count(etype)) throw std::runtime_error("dgraph: unknown etype '" + etype + "'");
  std::vector<char> kinds;
  for (bool out : parse_dirs(op, dir)) kinds.push_back(out ? kData : kReverse);
  if (std::count(kinds.begin(), kinds.end(), kReverse) && !reverse_.count(etype))
    throw std::runtime_error("dgraph: predicate '" + etype + "' has no @reverse; a plan may only traverse it forward");
  return kinds;
}

ValueList Dgraph::neighbors_of(CountingStore& st, const std::string& src, const std::string& etype,
                               const std::vector<char>& kinds, const std::vector<std::string>& edge_props, bool pairs,
                               size_t used_first_n) {
  return neighbors_of(st, uid_or_throw("dgraph", src), etype, kinds, edge_props, pairs, used_first_n);
}

ValueList Dgraph::neighbors_of(CountingStore& st, uint64_t su, const std::string& etype,
                               const std::vector<char>& kinds, const std::vector<std::string>& edge_props, bool pairs,
                               size_t used_first_n) {
  ValueList out;
  neighbors_of_into(st, su, etype, kinds, edge_props, pairs, used_first_n, NeighborItems(edge_props, pairs), out);
  return out;
}

void Dgraph::neighbors_of_into(CountingStore& st, uint64_t su, const std::string& etype, const std::vector<char>& kinds,
                         const std::vector<std::string>& edge_props, bool pairs, size_t used_first_n,
                         const NeighborItems& item, ValueList& out) {
  Value src = Value::vid(su);
  RtParallel par(st);                                      // one member per direction's posting
  for (char kind : kinds) {
    par.member();
    std::string k = uid_key(etype, kind, su);
    std::optional<std::vector<std::string>> parts = read_unit(st, k);   // 1 SEEK each (+ parts)
    if (!parts) continue;                                // a SEEK miss: the not-found lane, 0 used
    st.count_used(k.size(), 0);                          // the posting key located its posting
    uint64_t used = 0;
    size_t j = 0;
    for (auto& raw : *parts) {
      std::vector<uint8_t> uw;
      std::vector<uint32_t> fw;
      size_t j0 = j;
      for (auto& [other, facets] : posting::decode_adjacency_w(raw, &uw, &fw, adj_codec(etype, kind))) {
        // used: every consumed neighbour id (used_first_n bounds it -- the chase uses only
        // the FIRST); its facet only when edge props were requested
        if (j < used_first_n) used += uw[j - j0] + (edge_props.empty() ? 0 : fw[j - j0]);
        j++;
        out.push_back(item(src, Value::vid(other), &facets));
      }
    }
    st.count_used(0, used);
  }
  return;
}

Value Dgraph::read_neighbors(StepExec& ex, const ValueList& srcs, const std::string& etype, const std::string& dir,
                             const std::vector<std::string>& edge_props, bool pairs, size_t used_first_n) {
  std::vector<char> kinds = kinds_of("read_neighbors", etype, dir);
  std::vector<uint64_t> distinct = distinct_vids("dgraph", srcs, /*strict=*/false).uids;   // labels play no part
  // one src per index: its posting(s); the counts are order-independent (direct), no landings
  WorkerItems items(ex.P(), edge_props, pairs);   // one shape per worker; each src's items appended in index order
  return ex.run_range_append(distinct.size(), [&](WorkerSlot& slot, size_t ti, ValueList& out, std::vector<Landing>&) {
    neighbors_of_into(slot.store, distinct[ti], etype, kinds, edge_props, pairs, used_first_n, items.of(slot), out);
  }, /*additive_counts=*/true);
}

std::string Dgraph::data_pred(const std::string& label, const std::string& prop) const {
  if (!per_label_) return prop;
  if (label.empty())   // never guessed: a bare predicate holds nothing under per-label predicates
    throw std::runtime_error("dgraph: a property read under per-label predicates needs the step's label (" + prop + ")");
  return label + "." + prop;
}

Value Dgraph::props_of(CountingStore& st, uint64_t u, const std::vector<std::string>& props) {
  return props_of(st, u, props, RowBuilder(props_names(props)));   // virtual: an override's read
}

Value Dgraph::props_of(CountingStore& st, uint64_t u, const std::vector<std::string>& props, const RowBuilder& rb) {
  return props_at(st, u, props, "", rb);
}

Value Dgraph::props_at(CountingStore& st, uint64_t u, const std::vector<std::string>& props, const std::string& label,
                       const RowBuilder& rb) {
  RowBuilder::Row f = rb.start();
  f.push(Value::vid(u));
  RtParallel par(st);                                              // one member per predicate
  for (auto& p : props) {
    par.member();
    if (p == "id") f.push(key_id_value(u));                        // the vid IS the uid: derived, 0 ops, 0 used
    else if (list_preds_.count(p)) {
      std::string k = uid_key(data_pred(label, p), kData, u);
      std::optional<std::string> raw = read_list(st, k);           // list predicate: 1 SEEK
      // the whole list value IS the requested field -> used == fetched (amp ~1, the anchor)
      if (raw) st.count_used(k.size(), raw->size());               // a SEEK miss = the not-found lane
      f.push(raw ? prim::decode_leaf(*raw) : Value());           // a repeated name overwrites, like a dict
    } else {
      std::string k = uid_key(data_pred(label, p), kData, u);
      std::optional<std::string> raw = st.get(k);                  // scalar: 1 GET
      if (raw) st.count_used(k.size(), raw->size());               // fetch == field; a miss (a null
                                                                   // field) is the not-found lane, 0 used
      f.push(raw ? prim::decode_leaf(*raw) : Value());
    }
  }
  return f.done();
}

Value Dgraph::read_vertex_props(StepExec& ex, const ValueList& vids, const std::vector<std::string>& props,
                                const std::string& label) {
  DistinctVids d = distinct_vids("dgraph", vids, /*strict=*/false);   // the row's _vid carries no label here
  RowBuilder rb(props_names(props));                        // the call's one shape
  std::vector<Value> res = ex.run_range_map(d.uids.size(), [&](WorkerSlot& slot, size_t ti, std::vector<Landing>& log) {   // one vid: a key per predicate
      if (per_label_) return props_at(slot.store, d.uids[ti], props, label, rb);   // the label names the predicates
      return props_of(slot.store, d.uids[ti], props, rb);
  }, /*additive_counts=*/true);
  ValueList out;                                            // input order, duplicates included
  out.reserve(vids.size());
  for (size_t i = 0; i < vids.size(); i++) out.push_back(res[d.at.empty() ? i : d.at[i]]);
  return Value::list(std::move(out));
}

Value Dgraph::closure_level(StepExec& ex, uint64_t, const ValueList& frontier, const std::string& etype,
                            const std::string& dir) {
  return read_neighbors(ex, frontier, etype, dir, {}, false);
}

ChaseReads Dgraph::chase_reads(StepExec&, const std::string& etype, const std::string& dir) {
  kinds_of("read_chase", etype, dir);   // the etype is known, and a dir in has its @reverse (throws)
  ChaseReads rd;
  rd.fmt = "dgraph";
  rd.per_source = name() != "dgraph";   // its engine twin
  // chase used-rule (P5a): only the FIRST entry continues the chain -- annotate just its width; the rest of each
  // hop's posting is the per-hop granularity amplification
  rd.dirs = parse_dirs("read_chase", dir);
  rd.first = [this, etype](WorkerSlot& slot, std::vector<Landing>&, const Value& node, int64_t, bool out) {
    ValueList nxt = neighbors_of(slot.store, uid_or_throw("dgraph", node), etype, {out ? kData : kReverse}, {}, false,
                                 /*used_first_n=*/1);
    return nxt.empty() ? Value() : nxt[0];
  };
  return rd;
}

EdgeExistsReads Dgraph::edge_exists_reads(StepExec& ex, const Args& a) {
  std::string etype = a.str("etype");
  for (auto& r : edge_reads(a, "dgraph"))
    if (!r.out && !reverse_.count(etype))
      throw std::runtime_error("dgraph read_edge_exists needs @reverse on '" + etype + "' for a read with dir in");
  // one read = the named posting (forward for out, reverse for in) of each distinct owner, once (1 SEEK, + parts);
  // membership is free. A run of anchor-side reads is one pass (each anchor's postings together)
  auto postings_of = [this, &ex, etype](const ValueList& owners, const std::vector<bool>& outs) {
    return ex.run_range_map(owners.size(), [&](WorkerSlot& slot, size_t ti, std::vector<Landing>& log) {
        uint64_t u = uid_or_throw("dgraph", owners[ti]);
        ValueList nbrs;
        RtParallel par(slot.store);                        // one member per direction's posting
        for (bool out : outs) {
          char kind = out ? kData : kReverse;
          par.member();
          std::string k = uid_key(etype, kind, u);
          std::optional<std::vector<std::string>> parts = read_unit(slot.store, k);
          if (parts) slot.store.count_used(k.size(), 0);   // existence: key-only in the ideal
                                                           // store; every posting byte = amp
          if (parts)
            for (auto& raw : *parts)
              for (auto& [other, f] : posting::decode_adjacency(raw, adj_codec(etype, kind))) nbrs.push_back(Value::vid(other));
        }
        return Value::list(std::move(nbrs));
    }, /*additive_counts=*/true);
  };
  EdgeExistsReads io;
  io.fmt = "dgraph";
  // the via walk's entries are the anchors the step reads: every entry used, as every format
  io.via = [this, &ex](const ValueList& srcs, const std::string& via_etype) {
    return read_neighbors(ex, srcs, via_etype, "out", {}, false).as_list();
  };
  io.anchor_lists = [postings_of](const ValueList& anchors, const std::vector<bool>& outs, uint64_t) {
    ValueList all;
    for (auto& v : postings_of(anchors, outs))
      for (auto& n : v.as_list()) all.push_back(n);
    return all;
  };
  io.cand_lists = [postings_of](const ValueList& todo, bool out, const UidSet& anchors, uint64_t) {
    std::vector<Value> back = postings_of(todo, {out});
    std::vector<bool> found(todo.size(), false);
    for (size_t t = 0; t < todo.size(); t++)
      for (auto& n : back[t].as_list())
        if (anchors.contains(n.as_vid())) { found[t] = true; break; }
    return found;
  };
  return io;
}

}  // namespace evalcpp::dgraph
