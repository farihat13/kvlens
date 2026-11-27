// arangodb's write ops (Python arangodb.py _w_* + base._apply_writes). The document-store
// shape: an insert is FLAT in property count (doc + pk + one entry per declared index; mb2
// MB83 == MB84), an update reads pk + doc back (the id-indirection paid on every access) and
// moves the touched column's index entry (real arango re-maintains every index: an engine
// choice, not priced here); deleting an EDGE by its endpoints must first FIND
// its document by scanning the covering index (the edge id is not a function of its endpoints;
// mb2 MB88's 3-243 NEXTs) -- a vertex delete skips that search, its own scan already yields each
// edge id. Write-inserted docids live in their own space (the top bit), as Python's "w"
// prefix does. One write = one task; dels apply before puts (T2) (base._apply_writes order).
#include <algorithm>
#include <optional>
#include <string>
#include <vector>

#include "formats/arangodb/format.h"
#include "formats/arangodb/keys.h"
#include "formats/arangodb/vpack.h"
#include "formats/common.h"
#include "util/vid.h"

namespace evalcpp::arangodb {

namespace {
struct Writes {
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

Value Arango::run_write_op(const Step& step, const Args& a, StepExec& ex) {
  const std::string& op = step.op;
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {   // one write = one task
    CountingStore& st = slot.store;
    Counts before = st.counts();
    Writes w;
    std::string target;
    // an edge's ends: the plan names their collections (a vid does not; a plan without them is refused)
    auto end_label = [&](const std::string& vid, const char* arg) -> std::string {
      if (a.has(arg)) return a.str(arg);
      throw std::runtime_error(std::string("arangodb insert_edge needs ") + arg + " (the collection of " + vid + ")");
    };
    // remove one edge: locate its document by the covering-index scan, then doc + both entries
    auto remove_edge = [&](const std::string& etype, const std::string& src, const std::string& dst) {
      uint64_t su = uid_or_throw("arangodb", src), du = uid_or_throw("arangodb", dst);
      std::optional<uint64_t> eid = locate_edge(st, etype, su, dst);   // the search is paid either way
      if (!eid) return;
      int coll = schema_.edge(etype).id;
      w.dels.push_back(doc_key(edge_doc_coll(coll), *eid));
      w.dels.push_back(pk_key(edge_doc_coll(coll), *eid));      // the edge document's own pk entry
      w.dels.push_back(edge_key(true, coll, su, *eid));
      w.dels.push_back(edge_key(false, coll, du, *eid));
    };
    if (op == "insert_vertex") {
      std::string vid = a.str("vid"), label = a.str("label");
      uint64_t u = uid_or_throw("arangodb", vid);
      Value props = without_key_id(a.get("props"));   // the vid is the id: never a member
      uint64_t docid = kWriteDoc | ++writen_;
      int coll = schema_.vertex(label).id;
      note_vertex(u, label);
      std::string doc = doc_encode(vdoc_cols(label), {{"_key", Value(vid)}}, props, label);
      {  // used_write: the members' attributable bytes (doc_used); the object frame / row header = amp
        std::vector<std::string> names = {"_key"};
        for (auto& kv : props.as_row()) names.push_back(kv.first);
        std::string dk = doc_key(coll, docid);
        st.count_used_write(dk.size(), doc_used(vdoc_cols(label), doc, names));
        w.puts.emplace_back(std::move(dk), doc);
      }
      {  // the pk entry: arango's OWN index of the doc -- entry-granular, the locator IS
         // the logical content of this maintenance write
        std::string pk = pk_key(coll, u);
        std::string lv = loc_value(coll, docid);
        st.count_used_write(pk.size(), lv.size());
        w.puts.emplace_back(std::move(pk), std::move(lv));
      }
      for (auto& p : schema_.index_cols_of(label)) {       // one entry per DECLARED index: flat
        std::string ik = index_write_key(label, p, props.get(p), docid);
        st.count_used_write(ik.size(), 0);
        w.puts.emplace_back(std::move(ik), "");
      }
      target = vid;
    } else if (op == "insert_edge") {
      std::string src = a.str("src"), dst = a.str("dst"), etype = a.str("etype");
      std::string sl = end_label(src, "src_label"), dl = end_label(dst, "dst_label");
      Value props = a.has("props") ? a.get("props") : Value::row({});
      uint64_t eid = kWriteDoc | ++writen_;
      int coll = schema_.edge(etype).id;
      uint64_t su = uid_or_throw("arangodb", src), du = uid_or_throw("arangodb", dst);
      std::string doc = doc_encode(edoc_cols(etype), {{"_key", Value(hex16(eid))}, {"_from", Value(sl + "/" + src)},
                                                       {"_to", Value(dl + "/" + dst)}}, props, etype);
      // edge doc + its pk entry + both covering entries: four keys, flat in degree
      {
        std::vector<std::string> names = {"_key", "_from", "_to"};
        if (props.is_row())
          for (auto& kv : props.as_row()) names.push_back(kv.first);
        std::string dk = doc_key(edge_doc_coll(coll), eid);
        st.count_used_write(dk.size(), doc_used(edoc_cols(etype), doc, names));
        w.puts.emplace_back(std::move(dk), doc);
      }
      {  // the edge document's own pk entry, as a vertex document's (entry-granular: the locator is its content)
        std::string pk = pk_key(edge_doc_coll(coll), eid);
        std::string lv = loc_value(edge_doc_coll(coll), eid);
        st.count_used_write(pk.size(), lv.size());
        w.puts.emplace_back(std::move(pk), std::move(lv));
      }
      std::string fv = vp_encode_str(dl + "/" + dst), rv = vp_encode_str(sl + "/" + src);
      std::string fk = edge_key(true, coll, su, eid), rk = edge_key(false, coll, du, eid);
      // mirror rule: the reverse covering entry is used only when the schema declares it
      st.count_used_write(fk.size(), fv.size());
      if (schema_.edge(etype).reverse) st.count_used_write(rk.size(), rv.size());
      w.puts.emplace_back(std::move(fk), std::move(fv));
      w.puts.emplace_back(std::move(rk), std::move(rv));
      target = src + "-" + etype + "->" + dst;
    } else if (op == "update_prop") {
      // the document read once and rewritten once, whatever the number of changed members
      std::string vid = a.str("vid"), label = a.str("label");
      std::vector<UpdateCell> cells = update_cells(a);
      std::vector<std::string> changed;
      for (auto& c : cells) changed.push_back(c.prop);
      uint64_t u = uid_or_throw("arangodb", vid);
      int coll = schema_.vertex(label).id;
      std::string pk = pk_key(coll, u);
      std::optional<std::string> loc = st.get(pk);    // 1 GET: pk -> document id
      // RULING: the ideal store addresses by KEY on write as on read -- the locator
      // VALUE is the id-indirection's amplification on BOTH paths (key used, value amp);
      // a miss is the not-found lane, 0 used
      if (loc) st.count_used(pk.size(), 0);
      if (loc) {
        Loc l = loc_of(*loc);
        std::string dk = doc_key(l.coll_id, l.docid);
        std::optional<std::string> raw = st.get(dk);   // 1 GET: the doc
        // only the changed columns' index entries move: every other entry's key holds an
        // unchanged value (real arango rewrites them all -- mb2: 82 == 81 -- an engine choice,
        // not a layout cost; Python's arangodb_engine models it)
        std::vector<std::string> idx_cols;
        for (auto& p : schema_.index_cols_of(label))
          if (std::count(changed.begin(), changed.end(), p)) idx_cols.push_back(p);
        // used_read: the changed indexed columns' old members (the del keys) -- rest = RMW amp
        const std::vector<ColDecl>& vc = vdoc_cols(label);
        if (raw) st.count_used(dk.size(), doc_used(vc, *raw, idx_cols));
        Value doc = raw ? doc_row(vc, *raw) : Value::row({});
        Value ndoc = row_with_cells(doc, cells);
        std::string enc;
        if (cfg_.props_codec == PropsCodec::named) {
          vp_encode_obj(enc, ndoc);                            // arango's document, member order kept
        } else {
          RowFields sys, user;                                 // the system members aside, the rest is the row
          for (auto& kv : ndoc.as_row()) (kv.first == "_key" ? sys : user).push_back(kv);
          enc = doc_encode(vc, std::move(sys), Value::row(std::move(user)), label);
        }
        // used_write: the CHANGED members (named: their framed bytes; schema: their cells); the rest = the
        // doc-RMW write tax
        st.count_used_write(dk.size(), doc_used(vc, enc, changed));
        w.puts.emplace_back(dk, std::move(enc));
        for (auto& p : idx_cols) {
          std::string xk = index_write_key(label, p, doc.get(p), l.docid);
          std::string nk = index_write_key(label, p, ndoc.get(p), l.docid);
          st.count_used_write(xk.size(), 0);
          st.count_used_write(nk.size(), 0);
          w.dels.push_back(std::move(xk));
          w.puts.emplace_back(std::move(nk), "");
        }
      }
      target = update_target(vid, a, cells);
    } else if (op == "update_edge_prop") {
      // the edge props live in the edge DOCUMENT, whose id is not a function of the endpoints:
      // find it through the covering index as delete_edge does (the scan is paid either way),
      // then read the document back and rewrite it whole (1 GET + 1 PUT). The covering entries
      // hold only the neighbour id, so they do not change.
      std::string src = a.str("src"), dst = a.str("dst"), etype = a.str("etype");
      uint64_t su = uid_or_throw("arangodb", src);
      const Value& props = edge_update_props(a, schema_.edge(etype));
      std::optional<uint64_t> eid = locate_edge(st, etype, su, dst);
      if (eid) {
        std::string dk = doc_key(edge_doc_coll(schema_.edge(etype).id), *eid);
        std::optional<std::string> raw = st.get(dk);           // 1 GET: the edge doc
        if (raw) st.count_used(dk.size(), 0);                  // the key is the target; its old members are amp
        Value doc = raw ? doc_row(edoc_cols(etype), *raw) : Value::row({});
        Value ndoc = row_merge(doc, props);
        std::string enc;
        if (cfg_.props_codec == PropsCodec::named) {
          vp_encode_obj(enc, ndoc);                            // arango's document, member order kept
        } else {
          RowFields user;                                      // the doc's props, the system members aside
          std::map<std::string, Value> sysm;                   // _key (every edge doc), _from, _to
          for (auto& kv : ndoc.as_row())
            if (kv.first == "_key" || kv.first == "_from" || kv.first == "_to") sysm.emplace(kv.first, kv.second);
            else user.push_back(kv);
          RowFields sys;                                       // in the ingest's order
          for (const char* n : {"_key", "_from", "_to"})
            if (auto it = sysm.find(n); it != sysm.end()) sys.push_back({n, it->second});
          enc = doc_encode(edoc_cols(etype), std::move(sys), Value::row(std::move(user)), etype);
        }
        std::vector<std::string> names;
        for (auto& [name, v] : props.as_row()) names.push_back(name);
        st.count_used_write(dk.size(), doc_used(edoc_cols(etype), enc, names));   // the changed members
        w.puts.emplace_back(std::move(dk), std::move(enc));
      }
      target = src + "-" + etype + "->" + dst;
    } else if (op == "delete_vertex") {
      std::string vid = a.str("vid"), label = a.str("label");
      bool with_edges = a.flag("with_edges", true);
      uint64_t u = uid_or_throw("arangodb", vid);
      int coll = schema_.vertex(label).id;
      std::string pkk = pk_key(coll, u);
      std::optional<std::string> loc = st.get(pkk);    // 1 GET: pk
      if (loc) st.count_used(pkk.size(), 0);   // key used; the locator value = indirection amp (ruling)
      if (loc) {
        Loc l = loc_of(*loc);
        std::string ddk = doc_key(l.coll_id, l.docid);
        std::optional<std::string> raw = st.get(ddk);   // 1 GET: the doc
        if (raw) {   // the indexed members locate the index entries to delete (case b)
          const auto& ics = schema_.index_cols_of(label);
          std::vector<std::string> idx_cols(ics.begin(), ics.end());
          st.count_used(ddk.size(), doc_used(vdoc_cols(label), *raw, idx_cols));
        }
        Value doc = raw ? doc_row(vdoc_cols(label), *raw) : Value::row({});
        for (auto& dkey : {doc_key(l.coll_id, l.docid), pk_key(coll, u)}) {
          st.count_used_write(dkey.size(), 0);   // the logical deletions
          w.dels.push_back(dkey);
        }
        for (auto& p : schema_.index_cols_of(label)) {
          std::string xk = index_write_key(label, p, doc.get(p), l.docid);
          st.count_used_write(xk.size(), 0);
          w.dels.push_back(std::move(xk));
        }
        if (with_edges) {
          // one covering-index scan per incident (etype, dir). Each entry names the edge doc
          // (key suffix) and the other endpoint (value), so the doc and BOTH covering entries
          // are addressed directly -- no per-edge locate_edge: that search is delete_edge's tax,
          // paid only when the caller knows just the endpoints
          for (auto& [etype, out] : schema_.incident_etypes(label)) {
            int ecoll = schema_.edge(etype).id;
            std::string prefix = edge_prefix(out, ecoll, u);
            Cursor cur = st.seek(prefix);                  // 1 SEEK; case (c): locate scan, used 0
            while (cur.valid() && has_prefix(cur.key(), prefix)) {
              uint64_t eid = trailing_u64(cur.key());
              uint64_t ou = vp_ref_uid(cur.value());
              w.dels.push_back(doc_key(edge_doc_coll(ecoll), eid));
              w.dels.push_back(pk_key(edge_doc_coll(ecoll), eid));   // its pk entry
              w.dels.push_back(edge_key(out, ecoll, u, eid));     // our covering entry
              w.dels.push_back(edge_key(!out, ecoll, ou, eid));   // the other side's entry
              cur.next();                                  // 1 NEXT per edge
            }
            end_scan(cur);
          }
        }
      }
      target = vid;
    } else if (op == "delete_edge") {
      std::string src = a.str("src"), dst = a.str("dst"), etype = a.str("etype");
      remove_edge(etype, src, dst);
      target = src + "-" + etype + "->" + dst;
    } else {
      throw std::runtime_error("arangodb: unknown write op '" + op + "'");
    }
    w.apply(st);
    direct_counts(slot, before, r);
    r.out = write_result(op, target);
  }};
  return ex.run(tasks)[0].out;
}

}  // namespace evalcpp::arangodb
