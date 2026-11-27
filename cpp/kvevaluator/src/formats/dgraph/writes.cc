// dgraph's write ops (Python dgraph.py _w_* + base._apply_writes; Python is the reference).
// The posting-store shape: everything is a posting list, so a write is "which posting keys did
// this dirty?" -- one PUT per dirtied key carrying the WHOLE merged posting (read-modify-write;
// the current value is read first, a counted point SEEK through CountingStore::rmw_seek -- every read a
// write does is charged), and every removal is a PUT, so D stays 0 (mb2 L4). A removal's put value is OWN-vs-
// SHARED (the shared-vs-own posting split, mirrored from Python's _merge modes): the removed vertex's own keys
// empty whole (tomb); a posting OTHER vertices share -- a neighbour's adjacency, an index
// token -- is rewritten minus one entry, so the co-tenants survive.
// dgraph-c64k (Config::chunk_target finite): the SAME writes over a unit's parts -- the part
// holding the entry is the one rewritten (the shared unit RMW, store/unit_posting.h). dgraph's
// blind-rewrite rule is kept on purpose (a removal with nothing to take out still re-puts, as
// dgraph-∞ does: the arm must differ from dgraph by chunk size ONLY), so the removes locate
// the covering part through the shared seam and apply dgraph's own rewrite to it; the shared
// remove helpers (0 writes there: the posting index's rule) are not used.
// Divergences from the Python overlay's in-RAM objects, counts unaffected: a merged
// adjacency / index posting is stored uid-SORTED (real dgraph's
// invariant; Python's overlay holds an unsorted append because its values never meet the
// codec), and a tombstoned key holds the canonical EMPTY value of its kind.
#include <algorithm>
#include <string>
#include <vector>

#include "codec/prim.h"
#include "codec/varint.h"
#include "formats/common.h"
#include "formats/dgraph/format.h"
#include "formats/dgraph/keys.h"
#include "store/unit_posting.h"
#include "util/vid.h"

namespace evalcpp::dgraph {

namespace {

struct Writes {
  std::vector<std::pair<std::string, std::string>> puts;
  std::vector<std::string> dels;   // only the key-sorted index's key-only entries (full_index): nebula's rule
  void apply(CountingStore& st) {
    for (auto& k : dels) st.del_key(k);   // dels before puts, as the row formats apply them
    for (auto& [k, v] : puts) st.put(k, v);
  }
};

// insert `u` into a sorted uid list; NO-OP when already present. The present case is only
// reachable by an update_prop to the CURRENT value (the old-token removal and the re-add hit
// one key; both read the pre-put store, so the add sees the uid still there) -- a degenerate
// op no workload issues. Python's reference appends a duplicate there (its documented quirk);
// this codec cannot represent one, so a stray same-value update must not CRASH where Python
// returns a row. P counts identical either way (the put still happens). A known divergence.
void sorted_insert(std::vector<uint64_t>& uids, uint64_t u) {
  auto it = std::lower_bound(uids.begin(), uids.end(), u);
  if (it != uids.end() && *it == u) return;
  uids.insert(it, u);
}

}  // namespace

// --- the composition hooks (used by delete_vertex below; kept beside the op so the shared helpers stay in one
// file) ------------------------------------------------------------------------------------------------------

// the tombstone PUT of a scalar ('s': "" = removed) or an index posting ('U': the empty posting); `used_key` =
// the logical key charged as used (a part's unit key; default the key written)
static void w_tomb(CountingStore& st, Dgraph::WritePuts& puts, const std::string& key, char kind_empty,
                   size_t used_key = SIZE_MAX) {
  std::string v;
  if (kind_empty == 'U') v = "U";
  st.count_used_write(used_key == SIZE_MAX ? key.size() : used_key, 0);   // a removal writes no logical value bytes
  puts.emplace_back(key, v);
}

// the tombstone PUT of an adjacency: the empty posting in the store's facet codec ('A'+0 or 'R'+0),
// at ONE key (chunked: every part of the own unit)
static void w_adj_tomb(CountingStore& st, Dgraph::WritePuts& puts, const std::string& key,
                       const posting::AdjCodec& codec, bool chunked = false, size_t used_key = SIZE_MAX) {
  if (chunked) { unit::adjacency_tomb(st, key, puts, codec); return; }
  st.count_used_write(used_key == SIZE_MAX ? key.size() : used_key, 0);
  puts.emplace_back(key, posting::empty_adjacency(codec));
}

// `target` = the key rewritten: the whole (∞), or the covering part (chunked); an emptied
// part stays as the empty posting (part contiguity), an absent unit gets one at the bare key
static void w_index_remove(CountingStore& st, Dgraph::WritePuts& puts, const std::string& key, uint64_t u,
                           bool chunked = false) {
  std::vector<uint64_t> uids;
  std::string target = key;
  if (chunked) {
    std::optional<unit::Located> at = unit::locate_unit(st, key, u);
    if (at) { target = at->key; uids = Dgraph::decode_posting(at->raw); }
  } else if (auto raw = st.rmw_seek(key)) uids = Dgraph::decode_posting(*raw);
  uids.erase(std::remove(uids.begin(), uids.end(), u), uids.end());
  if (uids.empty()) w_tomb(st, puts, target, 'U', key.size());
  else {
    st.count_used_write(key.size(), 0);        // removal: co-tenant rewrite = write amp; the unit's key used
    puts.emplace_back(target, Dgraph::encode_posting(uids));
  }
}

static void w_adj_remove(CountingStore& st, Dgraph::WritePuts& puts, const std::string& key,
                         uint64_t other, bool ordered, const posting::AdjCodec& codec, bool chunked = false) {
  std::vector<std::pair<uint64_t, Value>> postings;
  std::string target = key;
  if (chunked) {
    std::optional<unit::Located> at = unit::locate_unit(st, key, other);
    if (at) { target = at->key; postings = posting::decode_adjacency(at->raw, codec); }
  } else if (auto raw = st.rmw_seek(key)) postings = posting::decode_adjacency(*raw, codec);
  postings.erase(std::remove_if(postings.begin(), postings.end(),
                                [&](auto& p) { return p.first == other; }),
                 postings.end());
  if (postings.empty()) w_adj_tomb(st, puts, target, codec, false, key.size());   // one key: the whole, or the one emptied part
  else {
    st.count_used_write(key.size(), 0);        // removal: co-tenant rewrite = write amp; the unit's key used
    puts.emplace_back(target, posting::encode_adjacency(postings, ordered, codec));
  }
}

// update_edge_prop on one side: the entry of `other` takes the new facets ({**old, **props}) in
// the posting that holds it -- the whole (∞) or the covering part (chunked) -- re-encoded in its
// stored form. False = the entry is not there: nothing written (an absent edge costs no write).
static bool w_adj_set(CountingStore& st, Dgraph::WritePuts& puts, const std::string& key, uint64_t other,
                      const Value& props, bool ordered, const posting::AdjCodec& codec, bool chunked = false) {
  std::vector<std::pair<uint64_t, Value>> postings;
  std::string target = key;
  if (chunked) {
    std::optional<unit::Located> at = unit::locate_unit(st, key, other);
    if (at) { target = at->key; postings = posting::decode_adjacency(at->raw, codec); }
  } else if (auto raw = st.rmw_seek(key)) postings = posting::decode_adjacency(*raw, codec);
  auto it = std::find_if(postings.begin(), postings.end(), [&](auto& p) { return p.first == other; });
  if (it == postings.end()) return false;
  size_t idx = size_t(it - postings.begin());
  it->second = row_merge(it->second.is_row() ? it->second : Value::row({}), props);
  std::string enc = posting::encode_adjacency(postings, ordered, codec);
  // used_write: the entry's changed facet bytes (posting::update_used_bytes, meta's rule too);
  // the rest of the rewritten posting = write amp
  std::vector<uint8_t> uw;
  std::vector<uint32_t> fw;
  posting::decode_adjacency_w(enc, &uw, &fw, codec);
  st.count_used_write(key.size(), posting::update_used_bytes(it->second, props, codec, idx < fw.size() ? fw[idx] : 0));
  puts.emplace_back(target, std::move(enc));
  return true;
}

void Dgraph::w_delete_vertex_own(CountingStore& st, const std::string& vid, const std::string& label,
                                 WritePuts& puts, WriteDels& dels) {
  uint64_t u = uid_or_throw("dgraph", vid);
  // the index tokens are keyed by VALUE: one counted GET per indexed property of the label
  std::vector<std::pair<std::string, Value>> vals;
  for (auto& p : schema_.index_cols_of(label)) {
    std::string k = uid_key(p, kData, u);
    std::optional<std::string> raw = st.get(k);
    if (raw) st.count_used(k.size(), raw->size());    // case (b): the old values locate the index
    if (raw) vals.emplace_back(p, prim::decode_leaf(*raw));   // entries to remove; a miss (no such
                                                               // prop) = the not-found lane, 0 used
  }
  // one tombstone per DECLARED column + the type record (the row on disk has every column)
  w_tomb(st, puts, uid_key("dgraph.type", kData, u), 's');
  for (auto& p : schema_.vertex(label).props) w_tomb(st, puts, uid_key(p.name, kData, u), 's');
  if (schema_.has_type_index(label)) w_index_remove(st, puts, term_key("dgraph.type", label), u, cfg_.chunked());
  for (auto& [p, v] : vals) {
    // a key-sorted entry is the vertex's OWN key (ms + uid): one DELETE, no co-tenant rewrite
    if (is_key_sorted(label, p)) { dels.push_back(term_key(index_pred(label, p), key_sorted_term(v.as_int(), u))); continue; }
    w_index_remove(st, puts, term_key(index_pred(label, p), indexed_.count(p) ? index_term(label, p, v)
                                                                              : day_token(v.as_int())), u, cfg_.chunked());
  }
}

void Dgraph::w_delete_incident(CountingStore& st, const std::string& vid, const std::string&,
                               const std::string& etype, bool out, WritePuts& puts) {
  uint64_t u = uid_or_throw("dgraph", vid);
  if (!out && !reverse_.count(etype)) return;                // no @reverse: unreachable, survives dangling
  std::vector<char> kinds = {out ? kData : kReverse};
  char own = out ? kData : kReverse, far = out ? kReverse : kData;
  std::vector<uint64_t> nbrs;
  if (cfg_.chunked()) {
    // dgraph-c64k: our own unit read ONCE (every part): its entries name the incident edges, the same part keys emptied
    std::optional<std::vector<uint64_t>> got = unit::adjacency_tomb_read(st, uid_key(etype, own, u), puts, adj_codec(etype, own));
    if (got) nbrs = std::move(*got);
    if (nbrs.empty()) return;
  } else {
    // the whole unit: 1 SEEK, charged (case (c): locate scan, used 0), then the tomb a blind put at the one key
    for (auto& n : neighbors_of(st, vid, etype, kinds, {}, false, /*used_first_n=*/0)) nbrs.push_back(uid_or_throw("dgraph", n));
    if (nbrs.empty()) return;
    w_adj_tomb(st, puts, uid_key(etype, own, u), adj_codec(etype, own));   // our own side: ONE unit
  }
  for (uint64_t nu : nbrs) {                                         // ...the NEIGHBOR's is SHARED
    // the other side exists only where the predicate is @reverse: without it there is no
    // reverse posting to fix up, and a tomb there would materialize a PHANTOM empty posting
    // ingest never wrote and no read ever queries (the same guard as the posting index's)
    if (!reverse_.count(etype)) continue;
    // self-loop: that "neighbour" key is OUR OWN posting -- an entry-removal put here
    // would overwrite the own tombstone (last put wins), resurrecting it
    if (nu == u) w_adj_tomb(st, puts, uid_key(etype, far, nu), adj_codec(etype, far), cfg_.chunked());
    else w_adj_remove(st, puts, uid_key(etype, far, nu), u, adj_ordered(etype), adj_codec(etype, far), cfg_.chunked());
  }
}

Value Dgraph::run_write_op(const Step& step, const Args& a, StepExec& ex) {
  const std::string& op = step.op;
  // a value-sorted day bucket is not maintained on a write: refuse (Python's DgraphFormat._apply_writes the same),
  // never write a uid-only posting into a (uid, ms) bucket
  if (!value_sorted_.empty())
    throw std::runtime_error("dgraph " + op + ": writes on a value-sorted date-index design are not modelled");
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {   // one write = one task
    CountingStore& st = slot.store;
    Counts before = st.counts();
    Writes w;
    std::string target;
    const bool ch = cfg_.chunked();                                  // dgraph-c64k
    // the merged index posting at (pred, term) with `u` added -- current value read by rmw_seek (counted)
    auto index_put = [&](const std::string& pred, const std::string& term, uint64_t u) {
      if (ch) {                                                      // the covering part, shared RMW
        if (term.find('\0') != std::string::npos)
          throw std::runtime_error("dgraph write: an index term of " + pred + " contains a NUL byte; chunked terms cannot be told from parts");
        unit::posting_add(st, term_key(pred, term), u, w.puts);
        return;
      }
      std::vector<uint64_t> uids;
      if (auto raw = st.rmw_seek(term_key(pred, term))) uids = decode_posting(*raw);
      sorted_insert(uids, u);
      std::string enc = encode_posting(uids);
      // used_write (case b, one insert entry): the entry's PACKED width in the new
      // posting (the read-side widths machinery) -- fresh/new-block FRAMING stays amp,
      // exactly as on read; the co-tenant rewrite is dgraph's index-granularity tax
      std::vector<uint8_t> wd;
      decode_posting_w(enc, &wd);
      size_t idx = size_t(std::lower_bound(uids.begin(), uids.end(), u) - uids.begin());
      st.count_used_write(term_key(pred, term).size(), idx < wd.size() ? wd[idx] : 0);
      w.puts.emplace_back(term_key(pred, term), std::move(enc));
    };
    // Remove ONE entry from a SHARED posting (a neighbour's adjacency, an index token): the
    // co-tenants survive and the put carries the honest posting-minus-one rewrite. Emptying
    // the whole key is ONLY for the removed vertex's OWN postings (tomb, above) -- the same
    // caller-named split as Python's _merge modes (the shared-posting fix; Python is the
    // reference). Current value read by rmw_seek (counted), the same RMW seam as index_put.
    auto index_remove = [&](const std::string& key, uint64_t u) { w_index_remove(st, w.puts, key, u, ch); };
    // a key-sorted entry (full_index): the vertex's own key-only entry, written blind (amp ~1)
    auto key_sorted_put = [&](const std::string& label, const std::string& prop, int64_t ms, uint64_t u) {
      std::string k = term_key(index_pred(label, prop), key_sorted_term(ms, u));
      st.count_used_write(k.size(), 0);
      w.puts.emplace_back(std::move(k), "");
    };
    // entry removal preserves the STORED order (decode gives it, the erase keeps it), so the
    // re-encode mode must match the store's form for the etype -- date-desc postings ('S')
    // re-encode as 'S', ascending ones as 'A'
    auto adj_remove = [&](const std::string& etype, char kind, uint64_t owner, uint64_t other) {
      w_adj_remove(st, w.puts, uid_key(etype, kind, owner), other, adj_ordered(etype), adj_codec(etype, kind), ch);
    };
    auto term_of = [&](const std::string& label, const std::string& p, const Value& v) {
      return date_preds_.count(p) ? day_token(v.as_int()) : index_term(label, p, v);
    };
    if (op == "insert_vertex") {
      std::string vid = a.str("vid"), label = a.str("label");
      uint64_t u = uid_or_throw("dgraph", vid);
      Value props = a.get("props");
      {
        std::string k = uid_key("dgraph.type", kData, u);
        std::string v = prim::encode_leaf(Value::list({Value(label)}));
        st.count_used_write(k.size(), v.size());   // per-predicate put IS the field: amp ~1
        w.puts.emplace_back(std::move(k), std::move(v));
      }
      for (auto& [name, val] : props.as_row()) {
        if (name == "id" || val.is_null()) continue;             // id derived; absent has no posting
        std::string k = uid_key(name, kData, u);
        std::string v = prim::encode_leaf(val);
        st.count_used_write(k.size(), v.size());
        w.puts.emplace_back(std::move(k), std::move(v));
      }
      if (schema_.has_type_index(label)) index_put("dgraph.type", label, u);
      for (auto& [name, val] : props.as_row()) {
        if (name == "id" || val.is_null() || !index_entry(label, name)) continue;
        if (indexed_.count(name)) index_put(index_pred(label, name), index_term(label, name, val), u);
        else if (date_preds_.count(name)) {
          if (is_key_sorted(label, name)) key_sorted_put(label, name, val.as_int(), u);
          else index_put(index_pred(label, name), day_token(val.as_int()), u);   // per type when type-scoped
        }
      }
      target = vid;
    } else if (op == "insert_edge") {
      std::string src = a.str("src"), dst = a.str("dst"), etype = a.str("etype");
      uint64_t su = uid_or_throw("dgraph", src), du = uid_or_throw("dgraph", dst);
      Value props = a.has("props") ? a.get("props") : Value::row({});
      // one posting key per stored direction, whatever the degree; bytes are O(degree): the
      // whole adjacency rewritten with the new entry merged in
      // uid-position insert: valid ONLY on the ascending 'A' form. No format writes the
      // date-position insert for 'S' postings; the format
      // refuses a dated-etype write rather than lower_bound a date-desc posting.
      if (!adj_ordered(etype))
        throw std::runtime_error("dgraph write: date-sorted (" + etype + ") insert position "
                                 "is not supported (no format sorts an adjacency list by date)");
      struct Side { char kind; uint64_t owner, other; };
      std::vector<Side> sides = {{kData, su, du}};
      if (reverse_.count(etype)) sides.push_back({kReverse, du, su});
      for (auto& s : sides) {
        if (ch) {                                                    // the covering part, shared RMW
          unit::adjacency_add(st, uid_key(etype, s.kind, s.owner), s.other, props, w.puts, adj_codec(etype, s.kind));
          continue;
        }
        std::vector<std::pair<uint64_t, Value>> postings;
        posting::AdjCodec codec = adj_codec(etype, s.kind);
        if (auto raw = st.rmw_seek(uid_key(etype, s.kind, s.owner))) postings = posting::decode_adjacency(*raw, codec);
        auto it = std::lower_bound(postings.begin(), postings.end(), s.other,
                                   [](auto& p, uint64_t x) { return p.first < x; });
        if (it != postings.end() && it->first == s.other)
          throw std::runtime_error("dgraph write: edge already present (the write params pick new pairs)");
        size_t idx = size_t(it - postings.begin());
        postings.insert(it, {s.other, props.is_row() ? props : Value::row({})});
        std::string k = uid_key(etype, s.kind, s.owner);
        std::string enc = posting::encode_adjacency(postings, true, codec);
        // used_write: the new entry's PACKED width + its own facet bytes (its logical
        // content); framing + the whole-posting rewrite = write amplification
        std::vector<uint8_t> uw;
        std::vector<uint32_t> fw;
        posting::decode_adjacency_w(enc, &uw, &fw, codec);
        st.count_used_write(k.size(), (idx < uw.size() ? uw[idx] : 0) + (idx < fw.size() ? fw[idx] : 0));
        w.puts.emplace_back(std::move(k), std::move(enc));
      }
      target = src + "-" + etype + "->" + dst;
    } else if (op == "update_prop") {
      // Split props: each column is its own posting, so the multi form is the single-column
      // update once per column (blind put; the old value read only for an indexed column)
      std::string vid = a.str("vid");
      std::string label = a.has("label") ? a.str("label") : "";
      uint64_t u = uid_or_throw("dgraph", vid);
      std::vector<UpdateCell> cells = update_cells(a);
      for (auto& [prop, value] : cells) {
        if (label.empty() && !type_scoped_.empty() && (indexed_.count(prop) || date_preds_.count(prop)))
          throw std::runtime_error("dgraph update_prop: a type-scoped index needs the step's label");
        {
          std::string k = uid_key(prop, kData, u);
          std::string v = prim::encode_leaf(value);
          st.count_used_write(k.size(), v.size());   // the changed field, written blind: amp ~1
          w.puts.emplace_back(std::move(k), std::move(v));   // (mb2 MB81)
        }
        if (index_entry(label, prop)) {
          // an indexed update must drop the OLD token's posting and add the new one: 1 counted GET
          std::string ok = uid_key(prop, kData, u);
          std::optional<std::string> old = st.get(ok);
          // case (b): the old-value read is NECESSARY (an ideal store also reads the old
          // value to find which index entry to remove) -> used, exactly the field; a miss
          // (no old value) = the not-found lane, 0 used
          if (old) st.count_used(ok.size(), old->size());
          if (old) {
            Value ov = prim::decode_leaf(*old);
            if (is_key_sorted(label, prop)) w.dels.push_back(term_key(index_pred(label, prop), key_sorted_term(ov.as_int(), u)));   // OWN entry: 1 DELETE
            else index_remove(term_key(index_pred(label, prop), term_of(label, prop, ov)), u);    // SHARED old token: this uid only
          }
          if (is_key_sorted(label, prop)) key_sorted_put(label, prop, value.as_int(), u);
          else index_put(index_pred(label, prop), term_of(label, prop, value), u);
        }
      }
      target = update_target(vid, a, cells);
    } else if (op == "update_edge_prop") {
      // the edge props ride in the posting ENTRY: each stored side holding the edge is one RMW of
      // its posting (current value read uncounted, as every posting rewrite); a side without the
      // entry is not written, so an absent edge writes nothing
      std::string src = a.str("src"), dst = a.str("dst"), etype = a.str("etype");
      uint64_t su = uid_or_throw("dgraph", src), du = uid_or_throw("dgraph", dst);
      const Value& props = edge_update_props(a, schema_.edge(etype));
      w_adj_set(st, w.puts, uid_key(etype, kData, su), du, props, adj_ordered(etype), adj_codec(etype, kData), ch);
      if (reverse_.count(etype))
        w_adj_set(st, w.puts, uid_key(etype, kReverse, du), su, props, adj_ordered(etype), adj_codec(etype, kReverse), ch);
      target = src + "-" + etype + "->" + dst;
    } else if (op == "delete_vertex") {
      // composed from the shared delete hooks
      std::string vid = a.str("vid"), label = a.str("label");
      bool with_edges = a.flag("with_edges", true);
      w_delete_vertex_own(st, vid, label, w.puts, w.dels);
      if (with_edges)
        for (auto& [etype, out] : schema_.incident_etypes(label))
          w_delete_incident(st, vid, label, etype, out, w.puts);
      target = vid;
    } else if (op == "delete_edge") {
      std::string src = a.str("src"), dst = a.str("dst"), etype = a.str("etype");
      uint64_t su = uid_or_throw("dgraph", src), du = uid_or_throw("dgraph", dst);
      // both postings are SHARED (src's other edges; dst's other reverse entries): remove
      // only THIS edge's entry -- the far endpoint -- from each
      adj_remove(etype, kData, su, du);
      if (reverse_.count(etype)) adj_remove(etype, kReverse, du, su);
      target = src + "-" + etype + "->" + dst;
    } else {
      throw std::runtime_error("dgraph: unknown write op '" + op + "'");
    }
    w.apply(st);
    direct_counts(slot, before, r);
    r.out = write_result(op, target);
  }};
  return ex.run(tasks)[0].out;
}

}  // namespace evalcpp::dgraph
