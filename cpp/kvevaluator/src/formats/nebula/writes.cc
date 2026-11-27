// nebula's write ops (Python nebula.py _w_* + base._apply_writes, same puts/dels, same counts).
// The row-store shape: a vertex is ONE key plus one index entry per DECLARED index of the label
// (absent values under the empty token), an edge is two keys; an update rewrites the whole row
// plus the touched column's index entry (real nebula re-maintains every index -- mb2: MB81 ==
// MB82 -- an engine choice, not priced here). Each op is one task; writes land in the run's
// shadow overlay; DELS apply before puts, exactly as base._apply_writes does since T2 -- a
// same-key del+put behaves like an engine batch (the put wins), so an update to the CURRENT
// value leaves its index entry in place.
#include <string>
#include <vector>

#include "formats/common.h"
#include "formats/nebula/format.h"
#include "formats/nebula/keys.h"
#include "codec/rowv2.h"
#include "util/vid.h"

namespace evalcpp::nebula {

namespace {
struct Writes {   // dels before puts, as base._apply_writes applies them (T2)
  std::vector<std::pair<std::string, std::string>> puts;
  std::vector<std::string> dels;
  void apply(CountingStore& st) {
    // dels BEFORE puts (T2): a same-key del+put applies like an engine batch -- the put
    // wins, so an update's re-put of an unchanged index entry survives (base._apply_writes)
    for (auto& k : dels) st.del_key(k);
    for (auto& [k, v] : puts) st.put(k, v);
  }
};
}  // namespace

// --- the composition hooks (used by delete_vertex below) --------------------------------

void Nebula::w_delete_vertex_own(CountingStore& st, const std::string& vid, const std::string& label,
                                 WriteDels& dels) {
  uint64_t u = uid_or_throw("nebula", vid);
  std::string vkey = vertex_key(schema_.vertex(label).id, u);
  std::optional<std::string> blob = st.get(vkey);            // the values name the index keys: 1 GET
  Value row = blob ? RowV2(&vcols(label), *blob).row() : Value::row({});
  dels.push_back(vkey);
  for (auto& k : index_.vertex_write_keys(label, u, row)) dels.push_back(k);
}

void Nebula::w_delete_incident(CountingStore& st, const std::string& vid, const std::string& etype,
                               bool out, WriteDels& dels) {
  uint64_t u = uid_or_throw("nebula", vid);
  // the incident edges through the layout's own access path (1 SEEK per direction + 1 NEXT
  // per edge, as read_neighbors charges them), then both key orientations per edge
  ValueList nbrs = neighbors_of(st, vid, etype, {out}, {}, false, /*used_first_n=*/0);   // case (c): locate scan, used 0
  int eid = schema_.edge(etype).id;
  for (auto& n : nbrs) {
    uint64_t nu = uid_or_throw("nebula", n);
    dels.push_back(edge_key(eid, out, u, nu));               // our side
    dels.push_back(edge_key(eid, !out, nu, u));              // the same edge, reverse key
  }
}

Value Nebula::run_write_op(const Step& step, const Args& a, StepExec& ex) {
  const std::string& op = step.op;
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {   // one write = one task
    CountingStore& st = slot.store;
    Counts before = st.counts();
    Writes w;
    std::string target;
    if (op == "insert_vertex") {
      std::string vid = a.str("vid"), label = a.str("label");
      uint64_t u = uid_or_throw("nebula", vid);
      Value props = a.get("props");
      std::vector<PropRow> rows = {{u, &props}};
      ValueRows src(vcols(label), rows);
      std::string vk = vertex_key(schema_.vertex(label).id, u);
      std::string enc = rowv2_encode(vcols(label), src, 0, cfg_.row_timestamp);
      {  // used_write: the present fields' framed cells; row header/bitmap/timestamp = amp
        RowV2 rv(&vcols(label), enc);
        uint64_t uv = 0;
        for (size_t c = 0; c < vcols(label).size(); c++) uv += rv.cell_bytes(c);
        st.count_used_write(vk.size(), uv);
      }
      w.puts.emplace_back(std::move(vk), std::move(enc));
      for (auto& k : index_.vertex_write_keys(label, u, props)) {
        st.count_used_write(k.size(), 0);        // entry-granular index put: ~ideal
        w.puts.emplace_back(k, "");
      }
      target = vid;
    } else if (op == "insert_edge") {
      std::string src = a.str("src"), dst = a.str("dst"), etype = a.str("etype");
      uint64_t su = uid_or_throw("nebula", src), du = uid_or_throw("nebula", dst);
      int eid = schema_.edge(etype).id;
      Value props = a.has("props") ? a.get("props") : Value::row({});
      std::vector<PropRow> rows = {{0, &props}};
      ValueRows rs(ecols(etype), rows);
      std::string val = rowv2_encode(ecols(etype), rs, 0, cfg_.row_timestamp);
      uint64_t uv = 0;
      {
        RowV2 rv(&ecols(etype), val);
        for (size_t c = 0; c < ecols(etype).size(); c++) uv += rv.cell_bytes(c);
      }
      // both orientations, each a full copy. The MIRROR rule: the forward copy is the
      // logical edge (used); the REVERSE copy is used only when the schema DECLARES the
      // reverse traversal -- an undeclared mirror is pure write amplification
      std::string fk = edge_key(eid, true, su, du), rk = edge_key(eid, false, du, su);
      st.count_used_write(fk.size(), uv);
      if (schema_.edge(etype).reverse) st.count_used_write(rk.size(), uv);
      w.puts.emplace_back(std::move(fk), val);
      w.puts.emplace_back(std::move(rk), val);
      target = src + "-" + etype + "->" + dst;
    } else if (op == "update_prop") {
      // the whole row read once and rewritten once, whatever the number of changed columns
      std::string vid = a.str("vid"), label = a.str("label");
      std::vector<UpdateCell> cells = update_cells(a);
      auto changed = [&](const std::string& p) {
        for (auto& c : cells) if (c.prop == p) return true;
        return false;
      };
      uint64_t u = uid_or_throw("nebula", vid);
      std::string vkey = vertex_key(schema_.vertex(label).id, u);
      std::optional<std::string> blob = st.get(vkey);            // READ-MODIFY-WRITE: 1 GET, charged
      // used_read: the changed columns' OLD cells when they are indexed (they name the del keys,
      // case b) -- the rest of the fetched row is the whole-row RMW's read amplification
      if (blob) {
        RowV2 orv(&vcols(label), *blob);
        uint64_t ur = 0;
        for (auto& p : schema_.index_cols_of(label)) {
          if (!changed(p)) continue;
          int c = orv.col_index(p);
          if (c >= 0) ur += orv.cell_bytes(size_t(c));
        }
        st.count_used(vkey.size(), ur);
      }
      Value row = blob ? RowV2(&vcols(label), *blob).row() : Value::row({});
      Value nrow = row_with_cells(row, cells);
      std::vector<PropRow> rows = {{u, &nrow}};
      ValueRows rs(vcols(label), rows);
      std::string enc = rowv2_encode(vcols(label), rs, 0, cfg_.row_timestamp);
      {  // used_write: the CHANGED fields' cells; the re-encoded rest = the row-RMW write tax
        RowV2 nrv(&vcols(label), enc);
        uint64_t uv = 0;
        for (auto& cell : cells) {
          int c = nrv.col_index(cell.prop);
          if (c >= 0) uv += nrv.cell_bytes(size_t(c));
        }
        st.count_used_write(vkey.size(), uv);
      }
      w.puts.emplace_back(vkey, std::move(enc));
      // only the changed columns' index entries move: every other entry's key holds an unchanged
      // value. (Real nebula rewrites them all -- mb2 MB81 == MB82 -- an engine choice, not a
      // layout cost; Python's nebula_engine models it.)
      for (auto& p : schema_.index_cols_of(label)) {
        if (!changed(p)) continue;
        std::string dk = index_.write_key(label, p, row.get(p), u);
        std::string ik = index_.write_key(label, p, nrow.get(p), u);
        st.count_used_write(dk.size(), 0);
        st.count_used_write(ik.size(), 0);
        w.dels.push_back(std::move(dk));
        w.puts.emplace_back(std::move(ik), "");
      }
      target = update_target(vid, a, cells);
    } else if (op == "update_edge_prop") {
      // the edge value is one row per orientation: 1 GET of the forward copy (the reverse copy
      // holds the same value) keeps the untouched props, then both copies PUT whole. An absent
      // edge: the GET misses, nothing is written.
      std::string src = a.str("src"), dst = a.str("dst"), etype = a.str("etype");
      uint64_t su = uid_or_throw("nebula", src), du = uid_or_throw("nebula", dst);
      int eid = schema_.edge(etype).id;
      const Value& props = edge_update_props(a, schema_.edge(etype));
      std::string fk = edge_key(eid, true, su, du), rk = edge_key(eid, false, du, su);
      std::optional<std::string> blob = st.get(fk);             // READ-MODIFY-WRITE: 1 GET, charged
      if (blob) {
        st.count_used(fk.size(), 0);                             // the key locates the edge; its old cells are amp
        Value nrow = row_merge(blob->empty() ? Value::row({}) : RowV2(&ecols(etype), *blob).row(), props);
        std::vector<PropRow> rows = {{0, &nrow}};
        ValueRows rs(ecols(etype), rows);
        std::string val = rowv2_encode(ecols(etype), rs, 0, cfg_.row_timestamp);
        uint64_t uv = 0;                                         // used_write: the changed cells
        {
          RowV2 rv(&ecols(etype), val);
          for (auto& [name, v] : props.as_row()) {
            int c = rv.col_index(name);
            if (c >= 0) uv += rv.cell_bytes(size_t(c));
          }
        }
        // the MIRROR rule, as insert_edge: the reverse copy is used only where the schema declares it
        st.count_used_write(fk.size(), uv);
        if (schema_.edge(etype).reverse) st.count_used_write(rk.size(), uv);
        w.puts.emplace_back(std::move(fk), val);
        w.puts.emplace_back(std::move(rk), std::move(val));
      }
      target = src + "-" + etype + "->" + dst;
    } else if (op == "delete_vertex") {
      // composed from the S8 hooks
      std::string vid = a.str("vid"), label = a.str("label");
      bool with_edges = a.flag("with_edges", true);
      w_delete_vertex_own(st, vid, label, w.dels);
      if (with_edges)
        for (auto& [etype, out] : schema_.incident_etypes(label))
          w_delete_incident(st, vid, etype, out, w.dels);
      target = vid;
    } else if (op == "delete_edge") {
      std::string src = a.str("src"), dst = a.str("dst"), etype = a.str("etype");
      uint64_t su = uid_or_throw("nebula", src), du = uid_or_throw("nebula", dst);
      int eid = schema_.edge(etype).id;
      // addressed directly, no search: the edge key IS <src, etype, dst>
      std::string fk = edge_key(eid, true, su, du), rk = edge_key(eid, false, du, su);
      st.count_used_write(fk.size(), 0);          // the logical deletion
      if (schema_.edge(etype).reverse) st.count_used_write(rk.size(), 0);   // mirror rule
      w.dels.push_back(std::move(fk));
      w.dels.push_back(std::move(rk));
      target = src + "-" + etype + "->" + dst;
    } else {
      throw std::runtime_error("nebula: unknown write op '" + op + "'");
    }
    w.apply(st);
    direct_counts(slot, before, r);
    r.out = write_result(op, target);
  }};
  return ex.run(tasks)[0].out;
}

}  // namespace evalcpp::nebula
