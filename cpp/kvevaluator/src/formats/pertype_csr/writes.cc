// pertype_csr's write ops (Python pertype_csr.py _w_* + base._apply_writes): every write is a
// chunk READ-MODIFY-WRITE through the shared exec/chunk_write.h (locate, mutate, re-serialize the
// whole chunk byte-identical to Python's, PUT under the new first row's key, DELETE a moved/emptied
// key; never re-split on overflow). The index entries follow the index backend's write rules (index/index_backend.h; posting by default).
// One write = one task; dels apply before puts (T2) (base._apply_writes order).
#include <algorithm>
#include <functional>
#include <map>
#include <optional>
#include <string>
#include <vector>

#include "formats/common.h"
#include "codec/chunks.h"
#include "exec/chunk_write.h"
#include "formats/pertype_csr/format.h"
#include "formats/pertype_csr/keys.h"
#include "util/vid.h"

namespace evalcpp::pertype_csr {

namespace {
// the chunk RMW machinery is shared with meta (exec/chunk_write.h); pertype_csr keeps its key builders
// and the write policy (which chunks each op touches)
using Writes = ChunkWrites;
constexpr uint64_t kNoOrd = kNoOrdinal;
}  // namespace

Value PerTypeCsr::run_write_op(const Step& step, const Args& a, StepExec& ex) {
  const std::string& op = step.op;
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {   // one write = one task
    CountingStore& st = slot.store;
    Counts before = st.counts();
    WReader rd(cc_, st);
    Writes w;
    std::string target;

    auto hint = [&](const char* arg) {
      return a.has(arg) ? std::vector<std::string>{a.str(arg)} : std::vector<std::string>{};
    };
    // an endpoint's label: the hint; else the candidates the SCHEMA gives that end of the edge type (never the
    // vid's bits) -- one = it, several = the first props chunk holding the row (1 SEEK per label tried), falling
    // back to the first candidate
    auto resolve_label = [&](const std::string& vid, const std::vector<std::string>& hinted,
                             const std::vector<std::string>& schema_side) {
      const std::vector<std::string>& labels = hinted.empty() ? schema_side : hinted;
      if (labels.empty()) throw std::runtime_error("pertype_csr write: no label for " + vid + " (the step names none and the schema gives none)");
      if (labels.size() == 1) return labels[0];
      uint64_t u = uid_or_throw("pertype_csr", vid);
      for (auto& lab : labels) {
        auto [key, c] = rd.locate(props_prefix(schema_.vertex(lab).id), cfg_.chunk_target, u, 0, &vcols(lab), false);
        if (c)
          for (auto& row : c->rows)
            if (row.first == u) return lab;
      }
      return labels[0];
    };
    // Python _next_ordinal: the last ep chunk's last row + 1 (1 SEEK)
    auto next_ordinal = [&](const std::string& label, const std::string& etype) -> uint64_t {
      auto [key, c] = rd.locate(ep_prefix(schema_.vertex(label).id, schema_.edge(etype).id), cfg_.chunk_target, ~0ull, 2, &ecols(etype), false);
      if (!c || c->rows.empty()) return 0;
      uint64_t mx = 0;
      for (auto& row : c->rows) mx = std::max(mx, row.first);
      return mx + 1;
    };
    // an adjacency chunk's write kind and label width: kind 4 (the label block) where the family stores far-end
    // labels, else pertype_csr's plain chunk
    auto adj_kind = [&](const std::string& etype, bool out) { return far_bits(etype, out) ? 4 : 1; };
    auto adj_locate = [&](WReader& r, const std::string& label, const std::string& etype, bool out, uint64_t id) {
      return r.locate(adj_prefix(schema_.vertex(label).id, schema_.edge(etype).id, out), cfg_.chunk_target, id, adj_kind(etype, out), nullptr,
                      !ecols(etype).empty(), far_bits(etype, out));
    };
    // the labels in schema-vertex order that are far labels of (etype, out): what a neighbour of unknown label is
    // probed under when no code is stored
    auto far_in_schema_order = [&](const std::string& etype, bool out) {
      const std::vector<std::string>& far = far_names(etype, out);
      std::vector<std::string> o;
      for (auto& v : schema_.vertices())
        if (std::find(far.begin(), far.end(), v.label) != far.end()) o.push_back(v.label);
      return o;
    };
    // the deleted vertex's own list of (etype, out), read through THIS write's reader: its neighbours and, where the
    // family stores them, their label codes. The reader holds what the write already changed (a self-loop's entry
    // the other direction's pass removed), and the chunk it lands on is the one the delete rewrites next, so it is
    // read once. (A fresh reader on the store saw neither: a deleted vertex's edges came back.) The
    // vertex's label is the step's
    struct OwnList { std::vector<uint64_t> dsts; std::vector<uint8_t> codes; };
    auto neighbors_write = [&](const std::string& label, const std::string& vid, const std::string& etype, bool out) {
      uint64_t u = uid_or_throw("pertype_csr", vid);
      OwnList got;
      if (!schema_.owns_edge(label, etype, out)) return got;   // no such structure: no probe (as read_neighbors)
      auto [key, c] = adj_locate(rd, label, etype, out, u);
      if (c)
        for (auto& g : c->groups)
          if (g.src == u) { got.dsts = g.dsts; got.codes = g.labels; break; }
      return got;
    };
    auto vertex_rmw = [&](const std::string& label, uint64_t u, const std::function<void(WReader::Chunk&)>& m) {
      chunk_rmw(cc_, rd, w, props_prefix(schema_.vertex(label).id), cfg_.chunk_target, u, 0, &vcols(label), false, m);
    };
    auto adj_rmw = [&](const std::string& label, const std::string& etype, bool out, uint64_t id,
                       const std::function<void(WReader::Chunk&)>& m) {
      bool has_ep = !ecols(etype).empty();
      chunk_rmw(cc_, rd, w, adj_prefix(schema_.vertex(label).id, schema_.edge(etype).id, out), cfg_.chunk_target, id, adj_kind(etype, out), nullptr,
                has_ep, m, far_bits(etype, out));
    };
    // the code a new entry of (etype, out) stores for a far end of `label` (nullopt: the family stores none)
    auto far_of = [&](const std::string& etype, bool out, const std::string& label) -> std::optional<uint8_t> {
      if (!far_bits(etype, out)) return std::nullopt;
      return far_code(etype, out, label, "insert_edge");
    };
    auto ep_rmw = [&](const std::string& label, const std::string& etype, uint64_t ordinal,
                      const std::function<void(WReader::Chunk&)>& m) {
      chunk_rmw(cc_, rd, w, ep_prefix(schema_.vertex(label).id, schema_.edge(etype).id), cfg_.chunk_target, ordinal, 2, &ecols(etype), false, m);
    };

    if (op == "insert_vertex") {
      std::string vid = a.str("vid"), label = a.str("label");
      uint64_t u = uid_or_throw("pertype_csr", vid);
      Value props = a.get("props");
      vertex_rmw(label, u, [&](WReader::Chunk& c) { chunk_upsert(c.rows, u, props); });
      index_->w_insert(st, label, u, props, {w.puts, w.dels});
      target = vid;
    } else if (op == "update_prop") {
      // every changed column in ONE chunk RMW; the index backend moves each changed column's entry
      std::string vid = a.str("vid"), label = a.str("label");
      std::vector<UpdateCell> cells = update_cells(a);
      uint64_t u = uid_or_throw("pertype_csr", vid);
      Value old = Value::row({});
      vertex_rmw(label, u, [&](WReader::Chunk& c) {
        for (auto& row : c.rows)
          if (row.first == u) { old = row.second; row.second = row_with_cells(old, cells); return; }
        c.rows.emplace_back(u, row_with_cells(Value::row({}), cells));
      });
      Value nrow = row_with_cells(old, cells);
      for (auto& cell : cells) index_->w_update(st, label, u, old, nrow, cell.prop, {w.puts, w.dels});
      target = update_target(vid, a, cells);
    } else if (op == "update_edge_prop") {
      // the edge props live in the edge-prop chunk at the edge's ORDINAL, and the ordinal in src's
      // out-adjacency chunk: locate that chunk (a counted read, as insert/delete do), find the
      // ordinal, RMW the edge-prop chunk at it. The adjacency chunks do not change.
      std::string src = a.str("src"), dst = a.str("dst"), etype = a.str("etype");
      if (ecols(etype).empty())
        throw std::runtime_error("update_edge_prop: '" + etype + "' declares no edge properties");
      std::string sl = resolve_label(src, hint("src_label"), owners_of(etype, true));
      uint64_t su = uid_or_throw("pertype_csr", src), du = uid_or_throw("pertype_csr", dst);
      const Value& props = edge_update_props(a, schema_.edge(etype));
      auto [key, c] = adj_locate(rd, sl, etype, true, su);
      std::optional<uint64_t> ordinal = c ? chunk_adj_ordinal(c->groups, su, du) : std::nullopt;
      if (ordinal) ep_rmw(sl, etype, *ordinal, [&](WReader::Chunk& ch) { chunk_merge_row(ch.rows, *ordinal, props); });
      target = src + "-" + etype + "->" + dst;
    } else if (op == "insert_edge") {
      std::string src = a.str("src"), dst = a.str("dst"), etype = a.str("etype");
      std::string sl = resolve_label(src, hint("src_label"), owners_of(etype, true));
      std::string dl = resolve_label(dst, hint("dst_label"), owners_of(etype, false));
      uint64_t su = uid_or_throw("pertype_csr", src), du = uid_or_throw("pertype_csr", dst);
      bool has_ep = !ecols(etype).empty();
      uint64_t ordinal = has_ep ? next_ordinal(sl, etype) : kNoOrd;
      Value props = a.has("props") ? a.get("props") : Value::row({});
      // each side's entry stores the OTHER end's label
      std::optional<uint8_t> out_far = far_of(etype, true, dl), in_far = far_of(etype, false, sl);
      adj_rmw(sl, etype, true, su, [&](WReader::Chunk& c) { chunk_adj_add(c.groups, su, du, ordinal, out_far); });
      adj_rmw(dl, etype, false, du, [&](WReader::Chunk& c) { chunk_adj_add(c.groups, du, su, ordinal, in_far); });
      if (has_ep) ep_rmw(sl, etype, ordinal, [&](WReader::Chunk& c) { chunk_upsert(c.rows, ordinal, props); });
      target = src + "-" + etype + "->" + dst;
    } else if (op == "delete_edge") {
      std::string src = a.str("src"), dst = a.str("dst"), etype = a.str("etype");
      std::string sl = resolve_label(src, hint("src_label"), owners_of(etype, true));
      std::string dl = resolve_label(dst, hint("dst_label"), owners_of(etype, false));
      uint64_t su = uid_or_throw("pertype_csr", src), du = uid_or_throw("pertype_csr", dst);
      bool has_ep = !ecols(etype).empty();
      std::optional<uint64_t> removed;
      adj_rmw(sl, etype, true, su, [&](WReader::Chunk& c) { removed = chunk_adj_remove(c.groups, su, du); });
      adj_rmw(dl, etype, false, du, [&](WReader::Chunk& c) { chunk_adj_remove(c.groups, du, su); });
      if (has_ep && removed) ep_rmw(sl, etype, *removed, [&](WReader::Chunk& c) { chunk_drop_row(c.rows, *removed); });
      target = src + "-" + etype + "->" + dst;
    } else if (op == "delete_vertex") {
      std::string vid = a.str("vid"), label = a.str("label");
      bool with_edges = a.flag("with_edges", true);
      uint64_t u = uid_or_throw("pertype_csr", vid);
      Value old = Value::row({});
      vertex_rmw(label, u, [&](WReader::Chunk& c) {
        for (size_t k = 0; k < c.rows.size(); k++)
          if (c.rows[k].first == u) { old = c.rows[k].second; c.rows.erase(c.rows.begin() + k); return; }
      });
      index_->w_delete(st, label, u, old, {w.puts, w.dels});
      if (with_edges) {
        for (auto& [etype, out] : schema_.incident_etypes(label)) {
          OwnList own = neighbors_write(label, vid, etype, out);   // the layout's own access path
          const std::vector<uint64_t>& nbrs = own.dsts;
          if (nbrs.empty()) continue;
          bool has_ep = !ecols(etype).empty();
          // a neighbour's label: its entry's stored code; the schema's where it gives this direction one far label;
          // else (far_label off) the props probe under the schema's far labels
          const std::vector<std::string>& far = far_names(etype, out);
          std::vector<std::string> far_probe = own.codes.empty() && far.size() > 1 ? far_in_schema_order(etype, out) : std::vector<std::string>{};
          auto nbr_label = [&](size_t i) {
            if (!own.codes.empty()) return far.at(own.codes[i]);
            if (far.size() == 1) return far[0];
            return resolve_label(hex16(nbrs[i]), {}, far_probe);
          };
          // our own side: every neighbor removed from the chunks holding vid's list
          std::vector<std::optional<uint64_t>> ords;
          adj_rmw(label, etype, out, u, [&](WReader::Chunk& c) {
            for (uint64_t n : nbrs) ords.push_back(chunk_adj_remove(c.groups, u, n));
          });
          // the neighbors' reverse entries, grouped by the chunk that holds them (ONE rmw per
          // touched chunk); a neighbor with no chunk stands alone
          std::vector<std::pair<std::string, std::vector<uint64_t>>> by_chunk;   // insertion order
          for (size_t ni = 0; ni < nbrs.size(); ni++) {
            uint64_t n = nbrs[ni];
            std::string nvid = hex16(n);
            std::string nl = nbr_label(ni);
            auto [key, c] = adj_locate(rd, nl, etype, !out, n);
            std::string gk = nl + "\x01" + (key.empty() ? "\x02" + nvid : key);
            auto it = std::find_if(by_chunk.begin(), by_chunk.end(), [&](auto& e) { return e.first == gk; });
            if (it == by_chunk.end()) by_chunk.push_back({gk, {n}});
            else it->second.push_back(n);
          }
          for (auto& [gk, nus] : by_chunk) {
            std::string nl = gk.substr(0, gk.find('\x01'));
            adj_rmw(nl, etype, !out, nus[0], [&](WReader::Chunk& c) {
              for (uint64_t nu : nus) chunk_adj_remove(c.groups, nu, u);
            });
          }
          if (has_ep) {
            // the ep rows live under the OUT side's label: ours for out, the neighbor's for in
            std::vector<std::pair<std::string, std::pair<std::string, std::vector<uint64_t>>>> by_ep;
            for (size_t i = 0; i < nbrs.size() && i < ords.size(); i++) {
              if (!ords[i]) continue;
              std::string ol = out ? label : nbr_label(i);
              auto [key, c] = rd.locate(ep_prefix(schema_.vertex(ol).id, schema_.edge(etype).id), cfg_.chunk_target, *ords[i], 2, &ecols(etype), false);
              std::string gk = ol + "\x01" + key;
              auto it = std::find_if(by_ep.begin(), by_ep.end(), [&](auto& e) { return e.first == gk; });
              if (it == by_ep.end()) by_ep.push_back({gk, {ol, {*ords[i]}}});
              else it->second.second.push_back(*ords[i]);
            }
            for (auto& [gk, e] : by_ep) {
              ep_rmw(e.first, etype, e.second[0], [&](WReader::Chunk& c) {
                for (uint64_t o : e.second) chunk_drop_row(c.rows, o);
              });
            }
          }
        }
      }
      target = vid;
    } else {
      throw std::runtime_error("pertype_csr: unknown write op '" + op + "'");
    }
    w.apply(st);
    direct_counts(slot, before, r);
    r.out = write_result(op, target);
  }};
  return ex.run(tasks)[0].out;
}

}  // namespace evalcpp::pertype_csr
