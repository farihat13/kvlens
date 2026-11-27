// meta's write ops: the 6 write ops for EVERY layout meta builds, dispatched per COMPONENT exactly as the
// reads are (the vertex write by its LABEL's props layout, the edge write by its ETYPE's adjacency layout,
// the index write by the index backend) -- so an interior cell, or a per-type mix, is its components'
// write paths composed. Each mode is the
// reference format's write, reimplemented on meta's own keys (keys.h) and the shared codecs/helpers:
//   props   item  (Split)      one prim record per column, written blind            (dgraph's shape)
//           owner (Condensed)  one columns row, rewritten whole on an update (RMW)  (nebula's shape)
//           {pack} (Chunked)   chunk RMW (exec/chunk_write.h)                        (pertype_csr)
//   adj     item  (Split)      one record per edge per direction (its columns)       (nebula's shape)
//           owner (Condensed)  the list unit RMW, over its parts                     (store/unit_list)
//           {pack} (Chunked)   chunk RMW of the adjacency + edge-prop chunks         (pertype_csr)
//   index   index_->w_*        the backend's own write surface (sorted_keys / posting; the packed index throws)
// Every write = one task: collect puts/dels, apply dels BEFORE puts (T2), count directly.
// Two rules meta applies uniformly (the fair format, not an engine):
//  - a deleted RECORD is a real del_key, never dgraph's tombstone-PUT (a BadgerDB merge behavior), so dgraph's
//    deletes are the write controlled set; nebula / pertype_csr reproduce. A vertex delete reads its own
//    Condensed list once and DELs it with its parts, as Split DELs its records (a part emptied by an edge delete
//    stays, as the empty list, so the parts stay contiguous).
//  - a REVERSE record is written / fixed up iff the store keeps it (reverse_available: direction both, or
//    schema @reverse); its bytes count as used only where the schema declares the reverse (the mirror rule).
// Chunked writes never re-split on overflow, so an incrementally written store is compared against the
// reference's incremental write, never against an ingest.
#include <algorithm>
#include <functional>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "codec/columns.h"
#include "exec/chunk_write.h"
#include "formats/common.h"
#include "formats/meta/format.h"
#include "formats/meta/keys.h"
#include "formats/rows.h"
#include "store/unit_list.h"
#include "store/unit_value.h"
#include "util/vid.h"

namespace evalcpp::meta {

Value Meta::run_write_op(const Step& step, const Args& a, StepExec& ex) {
  const std::string& op = step.op;
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {   // one write = one task
    CountingStore& st = slot.store;
    Counts before = st.counts();
    ChunkWrites w;
    ChunkWriteReader rd(cc_, st);   // the op's chunk reader (packed components only)
    std::string target;
    // is this prop maintained by the label's secondary index (value or sorted -- not a bare type index)?
    auto is_indexed_prop = [&](const std::string& label, const std::string& p) {
      std::vector<std::string> cols = schema_.index_cols_of(label);
      return std::find(cols.begin(), cols.end(), p) != cols.end();
    };
    // ---- packed (chunked) component helpers: chunk RMW on meta's chunk families ----
    // each family's chunk target (the last-chunk lane records it): props per label, adjacency per direction, and the
    // own-placement edge-prop chunks at their etype's out adjacency target (as ingest cuts them, meta/ingest.cc)
    auto props_target = [&](const std::string& label) { return cfg_.props_of(label).owners.pack; };
    auto adj_target = [&](const std::string& etype, bool out) { return cfg_.adj_of(etype, out).owners.pack; };
    auto vertex_rmw = [&](const std::string& label, uint64_t u, const std::function<void(ChunkWriteReader::Chunk&)>& m) {
      chunk_rmw(cc_, rd, w, vertex_family(vtype_of(label)), props_target(label), u, 0, &vcols(label), false, m);
    };
    // a packed adjacency chunk's write kind is 3 (codec/lists.h) in both placements: adjacency placement holds the
    // edges' props (its columns), own placement their ordinals when the etype carries props (no columns)
    static const std::vector<ColDecl> no_cols;
    auto inline_props = [&](const std::string& etype) { return !cfg_.adj_of(etype, true).props_own(); };
    auto adj_kind = [](const std::string&) { return 3; };
    auto adj_cols = [&](const std::string& etype) -> const std::vector<ColDecl>* {
      return inline_props(etype) ? &ecols(etype) : &no_cols;
    };
    auto adj_ords = [&](const std::string& etype) { return !inline_props(etype) && !ecols(etype).empty(); };
    auto adj_rmw = [&](const std::string& etype, bool out, uint64_t id, const std::function<void(ChunkWriteReader::Chunk&)>& m) {
      chunk_rmw(cc_, rd, w, padj_prefix(uint8_t(schema_.edge(etype).id), out), adj_target(etype, out), id, adj_kind(etype), adj_cols(etype),
                adj_ords(etype), m, far_bits(etype, out));
    };
    // the far end's label code an entry of (etype, out) stores, from the plan's labels (nullopt: the family stores none);
    // a family that stores labels and a step that names none fails loudly (far_code)
    auto far_of = [&](const std::string& etype, bool out, const std::string& label) -> std::optional<uint8_t> {
      if (!far_bits(etype, out)) return std::nullopt;
      return far_code(etype, out, label);
    };
    auto coded = [&](std::optional<uint8_t> code) { return code ? std::string(1, char(*code)) : std::string(); };
    // own placement: the ep chunks of the out-src's vid group (keys.h pep_prefix)
    auto ep_rmw = [&](uint64_t tag, const std::string& etype, uint64_t ordinal,
                      const std::function<void(ChunkWriteReader::Chunk&)>& m) {
      chunk_rmw(cc_, rd, w, pep_prefix(uint8_t(schema_.edge(etype).id), uint8_t(tag)), adj_target(etype, true), ordinal, 2, &ecols(etype), false, m);
    };
    // the next free edge-prop ordinal of (label, etype): the last ep chunk's last row + 1 (1 SEEK)
    auto next_ordinal = [&](uint64_t tag, const std::string& etype) -> uint64_t {
      auto [key, c] = rd.locate(pep_prefix(uint8_t(schema_.edge(etype).id), uint8_t(tag)), adj_target(etype, true), ~0ull, 2, &ecols(etype), false);
      if (!c || c->rows.empty()) return 0;
      uint64_t mx = 0;
      for (auto& row : c->rows) mx = std::max(mx, row.first);
      return mx + 1;
    };
    // one vertex's packed adjacency list, read through the op's reader: the chunk it lands on is the one the delete
    // rewrites next (adj_rmw), so it is read once -- as Condensed reads its own list once
    auto neighbors_packed = [&](uint64_t u, const std::string& etype, bool out) {
      std::vector<uint64_t> dsts;
      auto [key, c] = rd.locate(padj_prefix(uint8_t(schema_.edge(etype).id), out), adj_target(etype, out), u, adj_kind(etype),
                                   adj_cols(etype), adj_ords(etype), far_bits(etype, out));
      if (c)
        for (auto& g : c->groups)
          if (g.src == u) { dsts = g.dsts; break; }
      return dsts;
    };

    // ---- unpacked vertex props ----
    // the grouped-row (nebula) RMW read: GET the rows holding `need` (one group: the row), charge the INDEX cols
    // as used (the rest of the fetched rows is the row-RMW's read amplification). Returns the decoded old cells.
    auto grouped_read_old = [&](const std::string& label, uint64_t u, const std::vector<std::string>& need) -> Value {
      const RowGroups& rg = row_groups(label);
      RowFields old;
      for (size_t g : groups_for(label, need)) {
        std::string vk = vertex_group_key(vtype_of(label), u, g);
        std::optional<std::string> blob = st.get(vk);   // RMW read: 1 GET per touched row, charged
        if (!blob) continue;
        columns::View orv(&rg.cols[g], *blob, 0, 1);
        uint64_t ur = 0;
        for (auto& p : schema_.index_cols_of(label)) { int c = orv.col_index(p); if (c >= 0) ur += orv.cell_bytes(size_t(c), 0); }
        st.count_used(vk.size(), ur);
        Value r = orv.row(0);   // bind the row first: a range-for over a temporary's as_row() dangles
        for (auto& kv : r.as_row()) old.push_back(kv);
      }
      return Value::row(std::move(old));
    };
    // write one vertex's props (an unpacked props layout). grouped = one row put; `changed` empty (an insert) charges the WHOLE present
    // row as used, else (an update) only the changed cells -- the rest is the row-RMW write tax (nebula's rule).
    // singleton = one prim put per present col (`changed` names the updated cols on an update).
    auto put_vertex = [&](const std::string& label, uint64_t u, const Value& row, const std::vector<std::string>& changed) {
      auto is_changed = [&](const std::string& n) { return std::find(changed.begin(), changed.end(), n) != changed.end(); };
      uint8_t vt = vtype_of(label);
      const std::vector<ColDecl>& cols = vcols(label);
      if (cfg_.props_of(label).grouped()) {   // one row put per written group (one group = the Condensed row)
        const RowGroups& rg = row_groups(label);
        std::vector<PropRow> rows = {{u, &row}};
        for (size_t g = 0; g < rg.cols.size(); g++) {
          const std::vector<ColDecl>& gc = rg.cols[g];
          // an insert writes group 0 (the existence row) and every group with a present cell; an update
          // rewrites only the groups holding a changed column
          bool write = changed.empty() && g == 0;
          for (auto& c : gc) write = write || (changed.empty() ? !row.get(c.name).is_null() : is_changed(c.name));
          if (!write) continue;
          ValueRows src(gc, rows);
          std::string vk = vertex_group_key(vt, u, g), enc;
          columns::encode(enc, gc, src);
          columns::View rv(&gc, enc, 0, 1);
          uint64_t uv = 0;
          if (changed.empty()) { for (size_t c = 0; c < gc.size(); c++) uv += rv.cell_bytes(c, 0); }
          else for (auto& n : changed) { int c = rv.col_index(n); if (c >= 0) uv += rv.cell_bytes(size_t(c), 0); }
          st.count_used_write(vk.size(), uv);
          w.puts.emplace_back(std::move(vk), std::move(enc));
        }
      } else {   // singleton: one prim record per present column
        for (size_t c = 0; c < cols.size(); c++) {
          if (!changed.empty() && !is_changed(cols[c].name)) continue;   // an update touches only its columns
          Value cell = row.get(cols[c].name);
          if (cell.is_null()) continue;
          std::string k = vertex_attr_key(vt, u, attr_of_col(c)), v = columns::encode_cell(cols[c], cell);
          st.count_used_write(k.size(), v.size());   // the per-record put IS the field: amp ~1
          w.puts.emplace_back(std::move(k), std::move(v));
        }
      }
    };

    // ---- edges, by their etype's adjacency layout ----
    auto edge_value = [&](const std::string& etype, const Value& props) {   // a per-edge record: its columns, one row
      std::vector<PropRow> rows = {{0, &props}};
      ValueRows rs(ecols(etype), rows);
      std::string v;
      if (!ecols(etype).empty()) columns::encode(v, ecols(etype), rs);
      return v;
    };
    // the framed bytes of a per-edge value's cells (all, or the named ones): what its write charges as used
    auto edge_cell_bytes = [&](const std::string& etype, const std::string& val, const Value* only) {
      columns::View cv(&ecols(etype), val, 0, 1);
      uint64_t uv = 0;
      if (!only) {
        for (size_t c = 0; c < cv.cols(); c++) uv += cv.cell_bytes(c, 0);
      } else {
        for (auto& [name, v] : only->as_row()) { int c = cv.col_index(name); if (c >= 0) uv += cv.cell_bytes(size_t(c), 0); }
      }
      return uv;
    };
    // one stored direction's layout (config.h adj_of): Chunked (a chunk RMW), a Condensed list (the unit RMW) or
    // Split (the record). An edge write treats each stored copy by ITS direction's layout, out first.
    auto is_chunk = [&](const std::string& etype, bool out) { return cfg_.adj_of(etype, out).packed(); };
    auto is_list = [&](const std::string& etype, bool out) {
      const Component& c = cfg_.adj_of(etype, out);
      return c.grouped() && !c.packed();
    };
    auto insert_edge = [&](const std::string& etype, uint64_t su, uint64_t du, const Value& props,
                           const std::string& src_label, const std::string& dst_label) {
      uint8_t eid = uint8_t(schema_.edge(etype).id);
      bool rev = reverse_available(etype), declared = reverse_.count(etype) > 0;
      bool has_ep = !ecols(etype).empty();
      std::optional<uint8_t> far_out = far_of(etype, true, dst_label);   // the out entry's far end is the dst
      std::optional<uint8_t> far_in = rev ? far_of(etype, false, src_label) : std::nullopt;
      if (is_chunk(etype, true) && !inline_props(etype)) {   // chunked, own (per etype): the out chunk, the in chunk (if kept), the ep row
        uint64_t tag = tag_of_uid(su);           // the ep row lives with the out-src's group
        uint64_t ordinal = has_ep ? next_ordinal(tag, etype) : kNoOrdinal;
        adj_rmw(etype, true, su, [&](ChunkWriteReader::Chunk& c) { chunk_adj_add(c.groups, su, du, ordinal, far_out); });
        if (rev) adj_rmw(etype, false, du, [&](ChunkWriteReader::Chunk& c) { chunk_adj_add(c.groups, du, su, ordinal, far_in); });
        if (has_ep) ep_rmw(tag, etype, ordinal, [&](ChunkWriteReader::Chunk& c) { chunk_upsert(c.rows, ordinal, props); });
        return;
      }
      std::optional<std::string> val;   // a Split record's value: the same in each Split copy (+ its own far label byte)
      uint64_t uv = 0;
      auto copy = [&](bool out) {       // the entry (owner -> other) of direction `out`
        uint64_t owner = out ? su : du, other = out ? du : su;
        std::optional<uint8_t> far = out ? far_out : far_in;
        if (is_chunk(etype, out)) {            // chunked, inline: this direction's chunk, with the props
          adj_rmw(etype, out, owner, [&](ChunkWriteReader::Chunk& c) { chunk_adj_add_inline(c.groups, owner, other, props, has_ep, far); });
        } else if (is_list(etype, out)) {      // posting: the shared unit RMW (the one part whose id range holds other)
          unit::list_add(st, adj_prefix(eid, out, owner), other, far.value_or(0), props, w.puts, ecols(etype), far_bits(etype, out));
        } else {                               // per-edge: one record, a full copy (+ its far label byte)
          if (!val) { val = edge_value(etype, props); uv = edge_cell_bytes(etype, *val, nullptr); }
          std::string k = edge_key(eid, out, owner, other);
          if (out || declared) st.count_used_write(k.size(), uv);   // mirror rule: an undeclared mirror = amp
          w.puts.emplace_back(std::move(k), coded(far) + *val);
        }
      };
      copy(true);
      if (rev) copy(false);
    };
    auto delete_edge = [&](const std::string& etype, uint64_t su, uint64_t du) {
      uint8_t eid = uint8_t(schema_.edge(etype).id);
      bool rev = reverse_available(etype), declared = reverse_.count(etype) > 0;
      if (is_chunk(etype, true) && !inline_props(etype)) {   // chunked, own (per etype)
        bool has_ep = !ecols(etype).empty();
        std::optional<uint64_t> removed;
        adj_rmw(etype, true, su, [&](ChunkWriteReader::Chunk& c) { removed = chunk_adj_remove(c.groups, su, du); });
        if (rev) adj_rmw(etype, false, du, [&](ChunkWriteReader::Chunk& c) { chunk_adj_remove(c.groups, du, su); });
        if (has_ep && removed) ep_rmw(tag_of_uid(su), etype, *removed, [&](ChunkWriteReader::Chunk& c) { chunk_drop_row(c.rows, *removed); });
        return;
      }
      auto copy = [&](bool out) {
        uint64_t owner = out ? su : du, other = out ? du : su;
        if (is_chunk(etype, out)) {            // inline: the edge (and its props) out of this direction's chunk
          adj_rmw(etype, out, owner, [&](ChunkWriteReader::Chunk& c) { chunk_adj_remove_inline(c.groups, owner, other); });
        } else if (is_list(etype, out)) {      // the unit is shared: take only THIS edge's entry out
          unit::list_remove(st, adj_prefix(eid, out, owner), other, w.puts, w.dels, ecols(etype), far_bits(etype, out));
        } else {                               // addressed directly: the key IS <owner, etype, other>
          std::string k = edge_key(eid, out, owner, other);
          if (out || declared) st.count_used_write(k.size(), 0);
          w.dels.push_back(std::move(k));
        }
      };
      copy(true);
      if (rev) copy(false);
    };
    // update_edge_prop: the edge's props in every stored copy, by its etype's adjacency layout. An absent edge
    // writes nothing.
    auto update_edge = [&](const std::string& etype, uint64_t su, uint64_t du, const Value& props, const std::string& src_label) {
      uint8_t eid = uint8_t(schema_.edge(etype).id);
      bool rev = reverse_available(etype), declared = reverse_.count(etype) > 0;
      const Component& al = cfg_.adj_of(etype, true);
      auto kind = [&](bool out) { return is_chunk(etype, out) ? 2 : is_list(etype, out) ? 1 : 0; };
      if (rev && kind(true) != kind(false)) {
        // The two copies in different layouts (never own placement: per etype). The OUT side finds the edge by its
        // own layout's read and rewrites its copy; an absent edge writes nothing and reads nothing more. The in copy
        // is then set by ITS layout: a chunk / list RMW, or a Split record PUT whole from the out side's merged row
        // (no read of the in record, as when both are Split).
        if (ecols(etype).empty() && (kind(true) == 2 || kind(false) == 2))
          throw std::runtime_error("update_edge_prop: '" + etype + "' declares no edge properties");
        Value merged = Value::row({});
        if (kind(true) == 2) {
          auto [key, c] = rd.locate(padj_prefix(eid, true), adj_target(etype, true), su, 3, &ecols(etype), false, far_bits(etype, true));
          bool there = false;
          if (c)
            for (auto& g : c->groups)
              if (g.src == su) {
                auto it = std::find(g.dsts.begin(), g.dsts.end(), du);
                there = it != g.dsts.end();
                size_t i = size_t(it - g.dsts.begin());
                if (there && i < g.props.size() && g.props[i].is_row()) merged = g.props[i];
              }
          if (!there) return;
          merged = row_merge(merged, props);
          adj_rmw(etype, true, su, [&](ChunkWriteReader::Chunk& ch) { chunk_adj_set_inline(ch.groups, su, du, props); });
        } else if (kind(true) == 1) {
          if (!unit::list_set(st, adj_prefix(eid, true, su), du, props, w.puts, ecols(etype), far_bits(etype, true))) return;
          for (auto& e : lists::View(&ecols(etype), w.puts.back().second, far_bits(etype, true)).entries())
            if (e.id == du && e.props.is_row()) merged = e.props;   // the entry as just rewritten
        } else {
          std::string fk = edge_key(eid, true, su, du);
          std::optional<std::string> blob = st.get(fk);
          if (!blob) return;
          st.count_used(fk.size(), 0);
          std::string head = far_bits(etype, true) ? blob->substr(0, 1) : std::string();
          std::string cells = blob->substr(head.size());
          merged = row_merge(cells.empty() ? Value::row({}) : columns::View(&ecols(etype), cells, 0, 1).row(0), props);
          std::string val = edge_value(etype, merged);
          st.count_used_write(fk.size(), edge_cell_bytes(etype, val, &props));
          w.puts.emplace_back(std::move(fk), head + val);
        }
        if (kind(false) == 2) {
          adj_rmw(etype, false, du, [&](ChunkWriteReader::Chunk& ch) { chunk_adj_set_inline(ch.groups, du, su, props); });
        } else if (kind(false) == 1) {
          unit::list_set(st, adj_prefix(eid, false, du), su, props, w.puts, ecols(etype), far_bits(etype, false));
        } else {
          std::string val = edge_value(etype, merged);
          std::string rk = edge_key(eid, false, du, su);
          if (declared) st.count_used_write(rk.size(), edge_cell_bytes(etype, val, &props));   // mirror rule
          w.puts.emplace_back(std::move(rk), coded(far_of(etype, false, src_label)) + val);
        }
        return;
      }
      if (al.packed() && inline_props(etype)) {   // inline: find the edge in src's out chunk (counted), then set it in
        if (ecols(etype).empty())                 // both chunks (each copy carries the props)
          throw std::runtime_error("update_edge_prop: '" + etype + "' declares no edge properties");
        auto [key, c] = rd.locate(padj_prefix(eid, true), adj_target(etype, true), su, 3, &ecols(etype), false, far_bits(etype, true));
        bool there = false;
        if (c)
          for (auto& g : c->groups)
            if (g.src == su) there = std::find(g.dsts.begin(), g.dsts.end(), du) != g.dsts.end();
        if (!there) return;                    // an absent edge writes nothing
        adj_rmw(etype, true, su, [&](ChunkWriteReader::Chunk& ch) { chunk_adj_set_inline(ch.groups, su, du, props); });
        if (rev) adj_rmw(etype, false, du, [&](ChunkWriteReader::Chunk& ch) { chunk_adj_set_inline(ch.groups, du, su, props); });
      } else if (al.packed()) {                // chunked, own: the ordinal from src's out chunk (counted), then the ep row's RMW
        if (ecols(etype).empty())
          throw std::runtime_error("update_edge_prop: '" + etype + "' declares no edge properties");
        auto [key, c] = rd.locate(padj_prefix(eid, true), adj_target(etype, true), su, adj_kind(etype), adj_cols(etype), adj_ords(etype), far_bits(etype, true));
        std::optional<uint64_t> ordinal = c ? chunk_adj_ordinal(c->groups, su, du) : std::nullopt;
        if (ordinal) ep_rmw(tag_of_uid(su), etype, *ordinal, [&](ChunkWriteReader::Chunk& ch) { chunk_merge_row(ch.rows, *ordinal, props); });
      } else if (al.grouped()) {               // list: the unit RMW of the entry (read counted: unit::rmw_locate, 1-2 SEEKs)
        unit::list_set(st, adj_prefix(eid, true, su), du, props, w.puts, ecols(etype), far_bits(etype, true));
        if (rev) unit::list_set(st, adj_prefix(eid, false, du), su, props, w.puts, ecols(etype), far_bits(etype, false));
      } else {                                 // per-edge: GET the forward record (RMW read), PUT every copy whole
        std::string fk = edge_key(eid, true, su, du);
        std::optional<std::string> blob = st.get(fk);
        if (!blob) return;                     // absent edge: the GET is the whole cost
        st.count_used(fk.size(), 0);
        std::string head = far_bits(etype, true) ? blob->substr(0, 1) : std::string();   // the forward record's code
        std::string cells = blob->substr(head.size());
        Value nrow = row_merge(cells.empty() ? Value::row({}) : columns::View(&ecols(etype), cells, 0, 1).row(0), props);
        std::string val = edge_value(etype, nrow);
        uint64_t uv = edge_cell_bytes(etype, val, &props);   // used_write: the changed cells
        st.count_used_write(fk.size(), uv);
        w.puts.emplace_back(std::move(fk), head + val);
        // the reverse record's code is the src's label. It is NOT in hand: only the forward record is read (it holds
        // the dst's code), and reading the reverse one too would charge Split an extra GET per update that no other
        // layout pays. So it comes from the plan's src_label -- the source insert_edge trusts; a missing or
        // out-of-endpoint label throws (far_code). test_meta's far-label update case checks the stored code
        if (rev) {
          std::string rk = edge_key(eid, false, du, su);
          if (declared) st.count_used_write(rk.size(), uv);   // mirror rule: an undeclared mirror = amp
          w.puts.emplace_back(std::move(rk), coded(far_of(etype, false, src_label)) + val);
        }
      }
    };
    // every edge of the deleted vertex, per incident (etype, side), through the layout's own read path,
    // then our side emptied and every neighbour's reverse entry fixed up (where the store keeps it)
    auto delete_incident = [&](const std::string& label, uint64_t u) {
      std::string vid = hex16(u);
      for (auto& [etype, out] : schema_.incident_etypes(label)) {
        if (!out && !reverse_available(etype)) continue;   // no in-list kept: unreachable, survives dangling
        uint8_t eid = uint8_t(schema_.edge(etype).id);
        bool other_side = reverse_available(etype);         // the neighbour's record of the same edge
        const Component& al = cfg_.adj_of(etype, out);          // our side's layout (the other side's is the same
        auto kind = [&](bool d) { return is_chunk(etype, d) ? 2 : is_list(etype, d) ? 1 : 0; };   // below this block)
        if (other_side && kind(out) != kind(!out)) {
          // Our lists of (etype, out) in one layout, the neighbours' entries of us in (etype, !out) in another
          // (never own placement: per etype). Our side: read once through its layout and emptied, as below; then
          // every neighbour's entry of us by the other side's layout.
          const std::vector<ColDecl>& cols = ecols(etype);
          std::vector<uint64_t> nbrs;
          if (kind(out) == 2) {
            nbrs = neighbors_packed(u, etype, out);
            if (nbrs.empty()) continue;
            adj_rmw(etype, out, u, [&](ChunkWriteReader::Chunk& c) {
              for (uint64_t n : nbrs) chunk_adj_remove_inline(c.groups, u, n);
            });
          } else if (kind(out) == 1) {
            auto own = unit::rmw_read(st, adj_prefix(eid, out, u));
            if (!own) continue;
            for (auto& [pk, raw] : *own) {
              lists::View lv(&cols, raw, far_bits(etype, out));   // held: dsts() is a reference into the view
              nbrs.insert(nbrs.end(), lv.dsts().begin(), lv.dsts().end());
            }
            for (auto& [pk, raw] : *own) w.dels.push_back(pk);   // DELed even when empty: no key outlives its vertex
            if (nbrs.empty()) continue;
          } else {
            for (auto& n : neighbors_of(st, vid, etype, {out}, {}, false, /*used_first_n=*/0)) {
              nbrs.push_back(uid_or_throw("meta", n));
              w.dels.push_back(edge_key(eid, out, u, nbrs.back()));
            }
          }
          if (kind(!out) == 2) {   // the neighbours' entries, grouped by the chunk holding them (ONE rmw per touched chunk)
            std::vector<std::pair<std::string, std::vector<uint64_t>>> by_chunk;   // insertion order
            for (uint64_t n : nbrs) {
              auto [key, c] = rd.locate(padj_prefix(eid, !out), adj_target(etype, !out), n, adj_kind(etype), adj_cols(etype), adj_ords(etype),
                                        far_bits(etype, !out));
              std::string gk = key.empty() ? "\x02" + hex16(n) : key;
              auto it = std::find_if(by_chunk.begin(), by_chunk.end(), [&](auto& e) { return e.first == gk; });
              if (it == by_chunk.end()) by_chunk.push_back({gk, {n}});
              else it->second.push_back(n);
            }
            for (auto& [gk, nus] : by_chunk)
              adj_rmw(etype, !out, nus[0], [&](ChunkWriteReader::Chunk& c) {
                for (uint64_t nu : nus) chunk_adj_remove_inline(c.groups, nu, u);
              });
          } else if (kind(!out) == 1) {
            for (uint64_t nu : nbrs) {
              if (nu == u) {   // a self-loop's "neighbour" unit is OUR OWN other side: DEL it whole
                if (auto other = unit::rmw_read(st, adj_prefix(eid, !out, nu)))
                  for (auto& [pk, raw] : *other) w.dels.push_back(pk);
              } else {
                unit::list_remove(st, adj_prefix(eid, !out, nu), u, w.puts, w.dels, cols, far_bits(etype, !out));
              }
            }
          } else {
            for (uint64_t nu : nbrs) w.dels.push_back(edge_key(eid, !out, nu, u));
          }
          continue;
        }
        if (al.packed()) {
          std::vector<uint64_t> nbrs = neighbors_packed(u, etype, out);
          if (nbrs.empty()) continue;
          bool inl = inline_props(etype);
          bool own_ep = !inl && !ecols(etype).empty();   // own placement: the ep rows go too (inline: they are in the chunks)
          std::vector<std::optional<uint64_t>> ords;
          adj_rmw(etype, out, u, [&](ChunkWriteReader::Chunk& c) {
            for (uint64_t n : nbrs) {
              if (inl) chunk_adj_remove_inline(c.groups, u, n);
              else ords.push_back(chunk_adj_remove(c.groups, u, n));
            }
          });
          if (other_side) {
            // the neighbours' entries, grouped by the chunk holding them (ONE rmw per touched chunk)
            std::vector<std::pair<std::string, std::vector<uint64_t>>> by_chunk;   // insertion order
            for (uint64_t n : nbrs) {
              auto [key, c] = rd.locate(padj_prefix(eid, !out), adj_target(etype, !out), n, adj_kind(etype), adj_cols(etype), adj_ords(etype),
                                        far_bits(etype, !out));
              std::string gk = key.empty() ? "\x02" + hex16(n) : key;
              auto it = std::find_if(by_chunk.begin(), by_chunk.end(), [&](auto& e) { return e.first == gk; });
              if (it == by_chunk.end()) by_chunk.push_back({gk, {n}});
              else it->second.push_back(n);
            }
            for (auto& [gk, nus] : by_chunk) {
              adj_rmw(etype, !out, nus[0], [&](ChunkWriteReader::Chunk& c) {
                for (uint64_t nu : nus) {
                  if (inl) chunk_adj_remove_inline(c.groups, nu, u);
                  else chunk_adj_remove(c.groups, nu, u);
                }
              });
            }
          }
          if (own_ep) {   // the ep rows live under the OUT side's group: ours for out, the neighbour's for in
            std::vector<std::pair<std::string, std::pair<uint64_t, std::vector<uint64_t>>>> by_ep;
            for (size_t i = 0; i < nbrs.size() && i < ords.size(); i++) {
              if (!ords[i]) continue;
              uint64_t ot = tag_of_uid(out ? u : nbrs[i]);
              auto [key, c] = rd.locate(pep_prefix(eid, uint8_t(ot)), adj_target(etype, true), *ords[i], 2, &ecols(etype), false);
              std::string gk = std::to_string(ot) + "\x01" + key;
              auto it = std::find_if(by_ep.begin(), by_ep.end(), [&](auto& e) { return e.first == gk; });
              if (it == by_ep.end()) by_ep.push_back({gk, {ot, {*ords[i]}}});
              else it->second.second.push_back(*ords[i]);
            }
            for (auto& [gk, e] : by_ep)
              ep_rmw(e.first, etype, e.second[0], [&](ChunkWriteReader::Chunk& c) {
                for (uint64_t o : e.second) chunk_drop_row(c.rows, o);
              });
          }
        } else if (al.grouped()) {             // our list read ONCE (its parts' keys) and DELed; each neighbour's entry out
          const std::vector<ColDecl>& cols = ecols(etype);
          auto own = unit::rmw_read(st, adj_prefix(eid, out, u));
          if (!own) continue;
          std::vector<uint64_t> nbrs;
          for (auto& [pk, raw] : *own) {
            lists::View lv(&cols, raw, far_bits(etype, out));   // held: dsts() is a reference into the view
            nbrs.insert(nbrs.end(), lv.dsts().begin(), lv.dsts().end());
          }
          for (auto& [pk, raw] : *own) w.dels.push_back(pk);   // DELed even when empty: no key outlives its vertex
          if (nbrs.empty()) continue;
          if (!other_side) continue;
          for (uint64_t nu : nbrs) {
            if (nu == u) {   // a self-loop's "neighbour" unit is OUR OWN other side: DEL it too
              if (auto other = unit::rmw_read(st, adj_prefix(eid, !out, nu)))
                for (auto& [pk, raw] : *other) w.dels.push_back(pk);
            } else {
              unit::list_remove(st, adj_prefix(eid, !out, nu), u, w.puts, w.dels, cols, far_bits(etype, !out));
            }
          }
        } else {                               // per-edge: both records of each incident edge
          ValueList nbrs = neighbors_of(st, vid, etype, {out}, {}, false, /*used_first_n=*/0);
          for (auto& n : nbrs) {
            uint64_t nu = uid_or_throw("meta", n);
            w.dels.push_back(edge_key(eid, out, u, nu));
            if (other_side) w.dels.push_back(edge_key(eid, !out, nu, u));
          }
        }
      }
    };

    if (op == "insert_vertex") {
      std::string vid = a.str("vid"), label = a.str("label");
      uint64_t u = uid_or_throw("meta", vid);
      Value props = a.get("props");
      if (cfg_.props_of(label).packed()) vertex_rmw(label, u, [&](ChunkWriteReader::Chunk& c) { chunk_upsert(c.rows, u, props); });
      else put_vertex(label, u, props, {});      // insert: the whole present row is used
      index_->w_insert(st, label, u, props, {w.puts, w.dels});
      target = vid;
    } else if (op == "update_prop") {
      // the multi form rewrites every changed column in ONE row write: a packed chunk / a grouped row is
      // read and rewritten once; singleton props write one record per changed column
      std::string vid = a.str("vid"), label = a.str("label");
      uint64_t u = uid_or_throw("meta", vid);
      std::vector<UpdateCell> cells = update_cells(a);
      std::vector<std::string> changed;
      for (auto& c : cells) changed.push_back(c.prop);
      Value old_row = Value::row({});
      const Component& pl = cfg_.props_of(label);
      if (pl.packed()) {
        vertex_rmw(label, u, [&](ChunkWriteReader::Chunk& c) {
          for (auto& row : c.rows)
            if (row.first == u) { old_row = row.second; row.second = row_with_cells(old_row, cells); return; }
          c.rows.emplace_back(u, row_with_cells(Value::row({}), cells));
        });
      } else if (pl.grouped()) {
        for (auto& n : changed)   // as Split: an update names declared columns only
          if (!row_groups(label).group_of.count(n))
            throw std::runtime_error("meta update_prop: '" + n + "' is not a column of " + label);
        old_row = grouped_read_old(label, u, changed);   // row RMW: the old rows holding the changed columns
        put_vertex(label, u, row_with_cells(old_row, cells), changed);   // re-encode those rows; used = the changed cells
      } else {   // singleton: each changed record written blind; read an OLD value only for an indexed column
        RowFields olds;
        for (auto& [prop, value] : cells) {
          int c = attr_id(label, prop);
          if (c < 0) throw std::runtime_error("meta update_prop: '" + prop + "' is not a column of " + label);
          if (is_indexed_prop(label, prop)) {                   // an indexed update must drop the OLD token: 1 GET
            std::string ok = vertex_attr_key(vtype_of(label), u, uint8_t(c));
            std::optional<std::string> old = st.get(ok);
            if (old) { st.count_used(ok.size(), old->size()); row_set(olds, prop, columns::decode_cell(vcols(label)[size_t(c) - 1], *old)); }
          }
        }
        old_row = make_row(std::move(olds));
        put_vertex(label, u, row_with_cells(Value::row({}), cells), changed);   // one prim put per changed column
      }
      Value new_row = row_with_cells(old_row, cells);
      for (auto& prop : changed) index_->w_update(st, label, u, old_row, new_row, prop, {w.puts, w.dels});
      target = update_target(vid, a, cells);
    } else if (op == "update_edge_prop") {
      std::string src = a.str("src"), dst = a.str("dst"), etype = a.str("etype");
      uint64_t su = uid_or_throw("meta", src), du = uid_or_throw("meta", dst);
      const Value& props = edge_update_props(a, schema_.edge(etype));
      // no label in the key (keys.h); the plan's src label gives a Split reverse record's far-label byte
      update_edge(etype, su, du, props, a.has("src_label") ? a.str("src_label") : "");
      target = src + "-" + etype + "->" + dst;
    } else if (op == "insert_edge") {
      std::string src = a.str("src"), dst = a.str("dst"), etype = a.str("etype");
      uint64_t su = uid_or_throw("meta", src), du = uid_or_throw("meta", dst);
      Value props = a.has("props") ? a.get("props") : Value::row({});
      // no label in the key (keys.h); the plan's labels give the entries' far-end labels
      insert_edge(etype, su, du, props, a.has("src_label") ? a.str("src_label") : "",
                  a.has("dst_label") ? a.str("dst_label") : "");
      target = src + "-" + etype + "->" + dst;
    } else if (op == "delete_edge") {
      std::string src = a.str("src"), dst = a.str("dst"), etype = a.str("etype");
      uint64_t su = uid_or_throw("meta", src), du = uid_or_throw("meta", dst);
      delete_edge(etype, su, du);   // no label: every adjacency key is etype-first (keys.h)
      target = src + "-" + etype + "->" + dst;
    } else if (op == "delete_vertex") {
      std::string vid = a.str("vid"), label = a.str("label");
      bool with_edges = a.flag("with_edges", true);
      uint64_t u = uid_or_throw("meta", vid);
      uint8_t vt = vtype_of(label);
      const std::vector<ColDecl>& cols = vcols(label);
      Value old_props = Value::row({});
      const Component& pl = cfg_.props_of(label);
      if (pl.packed()) {
        vertex_rmw(label, u, [&](ChunkWriteReader::Chunk& c) {
          for (size_t k = 0; k < c.rows.size(); k++)
            if (c.rows[k].first == u) { old_props = c.rows[k].second; c.rows.erase(c.rows.begin() + k); return; }
        });
      } else if (pl.grouped()) {
        old_props = grouped_read_old(label, u, schema_.index_cols_of(label));   // the rows naming the index terms
        for (size_t g = 0; g < row_groups(label).cols.size(); g++)
          w.dels.push_back(vertex_group_key(vt, u, g));  // the fair delete: del_key every row (an absent later group too)
      } else {   // singleton: read the indexed cols (for the index removal), del_key every declared col
        RowFields f;
        for (auto& p : schema_.index_cols_of(label)) {
          int c = attr_id(label, p);
          if (c < 0) continue;
          std::string k = vertex_attr_key(vt, u, uint8_t(c));
          std::optional<std::string> raw = st.get(k);   // the old values locate the index entries
          if (raw) { st.count_used(k.size(), raw->size()); row_set(f, p, columns::decode_cell(vcols(label)[size_t(c) - 1], *raw)); }
        }
        old_props = make_row(std::move(f));
        for (size_t c = 0; c < cols.size(); c++) w.dels.push_back(vertex_attr_key(vt, u, attr_of_col(c)));
      }
      index_->w_delete(st, label, u, old_props, {w.puts, w.dels});
      if (with_edges) delete_incident(label, u);
      target = vid;
    } else {
      throw std::runtime_error("meta: unknown write op '" + op + "'");
    }

    w.apply(st);
    direct_counts(slot, before, r);
    r.out = write_result(op, target);
  }};
  return ex.run(tasks)[0].out;
}

}  // namespace evalcpp::meta
