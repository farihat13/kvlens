#include "formats/dgraph/engine.h"

#include <algorithm>
#include <cstdio>
#include <optional>
#include <stdexcept>
#include <unordered_map>

#include "codec/prim.h"
#include "formats/common.h"
#include "formats/dgraph/keys.h"
#include "util/uidset.h"
#include "util/vid.h"

namespace evalcpp::dgraph {


DgraphEngine::DgraphEngine(const Schema& schema, const Config& cfg) : Dgraph(schema, cfg) {
  // the engine twin's own reads build bare predicate keys: per-label predicates are the dgraph format's only
  if (schema.predicates_per_label()) throw std::runtime_error("dgraph_engine: per-label predicates (`predicates per_label`) are built for the dgraph format only");
  // the walks below read one KV per index token and per posting (real dgraph's unbounded layout): a split
  // posting would need the part-aware walks the base's ops carry
  if (cfg.chunked()) throw std::runtime_error("dgraph_engine: the engine store is unbounded (no chunk_target)");
  // the engine's own facet codec (every facet name per edge, as real dgraph and Python's twin store them): the
  // props-codec knob normalizes the COMPARED formats, never the engine reproduction
  if (cfg_.props_codec != PropsCodec::named)
    throw std::runtime_error("dgraph_engine: the engine store holds dgraph's named facets (--props-codec named)");
  // the engine's `id @index(int)` tokens live under the `id` predicate; a schema that also indexes `id` as a
  // value would put str(id) terms in the same band
  if (indexed_.count("id") || date_preds_.count("id"))
    throw std::runtime_error("dgraph_engine: the schema indexes `id`; the engine's id index would share its band");
  // the engine plans' sort_topk / ineq_root_scan walk the predicate-global date band (real dgraph's @index(day)): a
  // type-scoped date design moves Post / Comment out of it -- refuse rather than miss them silently
  for (auto& [label, prop] : type_scoped_)
    if (date_preds_.count(prop))
      throw std::runtime_error("dgraph_engine: the engine twin reads dgraph's predicate-global date index; a type-scoped "
                               "date design (" + label + "." + prop + ") is not the engine's");
}

std::string DgraphEngine::id_token(int64_t id) {
  if (id < 0) throw std::runtime_error("dgraph_engine: a negative id has no order-preserving token");
  char buf[32];
  snprintf(buf, sizeof buf, "%020lld", (long long)id);
  return buf;
}

// --- ingest -----------------------------------------------------------------------------------

// dgraph's vertex, plus what the engine's `eq(id, N)` anchor needs (Python add_vertex, _id_as_uid = False):
// the fqid record and the per-vid fqid index entry the anchor SEEKs, `id` stored as a scalar, and the
// `id @index(int)` posting per id value (shared by every type with that id, in value order)
void DgraphEngine::add_vertex(KvSink& sink, uint64_t uid, const std::string& label, const Value& props) {
  Dgraph::add_vertex(sink, uid, label, props);   // flushes on a label change; this vertex's label is current
  std::string vid = hex16(uid);
  Value id = key_id_value(vid);
  // Python writes the id keys from the record's `id`; every record carries one, and it must be the vid's
  const Value* given = props.is_row() ? props.find("id") : nullptr;
  if (!given || given->is_null())
    throw std::runtime_error("dgraph_engine ingest: a " + label + " record without `id` (" + vid + ")");
  if (!equals(*given, id))
    throw std::runtime_error("dgraph_engine ingest: the record's id " + repr(*given) + " is not its vid's " + repr(id));
  cur_scalars_["fqid"].emplace_back(uid, prim::encode_leaf(Value(vid)));
  index_[{"fqid", vid}].push_back(uid);
  cur_scalars_["id"].emplace_back(uid, prim::encode_leaf(id));
  index_[{"id", id_token(id.as_int())}].push_back(uid);
}

// --- reads --------------------------------------------------------------------------------------

Value DgraphEngine::read_value(CountingStore& st, const std::string& prop, uint64_t uid) {
  std::optional<std::string> raw = read_list(st, uid_key(prop, kData, uid));
  return raw ? prim::decode_leaf(*raw) : Value();
}

Value DgraphEngine::props_of(CountingStore& st, uint64_t u, const std::vector<std::string>& props,
                             const RowBuilder& rb) {
  if (std::find(props.begin(), props.end(), "id") == props.end()) return Dgraph::props_of(st, u, props, rb);
  RowBuilder::Row f = rb.start();
  f.push(Value::vid(u));
  for (auto& p : props) {
    if (p == "id") {
      std::string k = uid_key("id", kData, u);
      std::optional<std::string> raw = st.get(k);                  // the engine stores id: 1 GET
      if (raw) st.count_used(k.size(), raw->size());
      f.push(raw ? prim::decode_leaf(*raw) : Value());
    } else {
      f.push(Dgraph::props_of(st, u, {p}, RowBuilder({"_vid", p})).field(p));   // the base's read of this one prop
    }
  }
  return f.done();
}

// `eq(id, N)`: ONE fqid index point-lookup SEEK per candidate, until a hit (misses paid)
Value DgraphEngine::read_vertex_by_id(StepExec& ex, const Value& vid) {
  ValueList cands = vid.is_text() ? ValueList{vid} : vid.as_list();
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    ValueList out;
    for (auto& c : cands) {
      std::string k = term_key("fqid", c.text());                  // the engine's fqid: the vid's text
      std::optional<std::string> raw = read_list(slot.store, k);
      if (!raw) continue;
      for (uint64_t u : decode_posting(*raw)) out.push_back(Value::vid(u));
      if (!out.empty()) break;
    }
    direct_counts(slot, before, r);
    r.out = Value::list(std::move(out));
  }};
  return ex.run(tasks)[0].out;
}

// resolve which candidate exists (an fqid SEEK per try, misses paid), then read that one's props
Value DgraphEngine::read_anchor_props(StepExec& ex, const ValueList& cands, const std::vector<std::string>& props) {
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    ValueList out;
    for (auto& c : cands) {
      std::optional<std::string> raw = read_list(slot.store, term_key("fqid", c.text()));
      if (raw && !decode_posting(*raw).empty()) {                  // Python: a non-empty posting is the hit
        out.push_back(props_of(slot.store, uid_or_throw("dgraph_engine", c), props));
        break;
      }
    }
    direct_counts(slot, before, r);
    r.out = Value::list(std::move(out));
  }};
  return ex.run(tasks)[0].out;
}

// Python `x or 0`: a falsy value (null, 0, "") sorts as 0
static Value or_zero(const Value& v) { return v.truthy() ? v : Value(int64_t(0)); }

// `orderdesc: <prop>[, orderasc: <second_key>], first: k` (Python sort_topk, worker/sort.go): ONE iterator over
// `prop`'s index (or `index`'s), bucket by bucket from the newest (desc) or oldest token; per bucket 1 SEEK for
// its posting, then sortByValue's 1 SEEK per intersected candidate; stop after the bucket that reaches k, before
// the iterator advances (1 NEXT per step taken). With a second key, multiSort fetches it over the result matrix,
// which the uint64 overflow widens to every candidate the walk never reached whenever the walk overshot k.
Value DgraphEngine::sort_topk(StepExec& ex, const ValueList& rows, const std::string& prop, int64_t k,
                              const std::string& second_key, bool id_desc, const std::string& index, bool desc) {
  if (k < 0) throw std::runtime_error("sort_topk: k must be >= 0");
  if (second_key == prop) throw std::runtime_error("sort_topk: the second key is the first (" + prop + ")");
  // the predicate whose index it walks: declared, or refused. `id` is the engine's own `id @index(int)`, built at
  // ingest whatever the schema says (add_vertex; the constructor refuses a schema that indexes `id` too)
  if ((index.empty() ? prop : index) != "id") require_index({}, index.empty() ? prop : index);
  UidSet cand(rows.size());
  std::unordered_map<uint64_t, size_t> by_vid;                     // the LAST row per vid (Python's dict)
  std::vector<uint64_t> row_vids;                                  // distinct, first-occurrence order
  for (size_t i = 0; i < rows.size(); i++) {
    uint64_t v = uid_or_throw("dgraph_engine", rows[i].field("_vid"));
    cand.insert(v);
    if (!by_vid.count(v)) row_vids.push_back(v);
    by_vid[v] = i;
  }
  std::vector<TaskFn> tasks = {[&, k, desc, id_desc](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    CountingStore& st = slot.store;
    std::string pre = prefix(index.empty() ? prop : index, kIndex);
    // "~" sorts after every token, "" before every one (Python's (idx, "I", "~") / (idx, "I", ""))
    Cursor cur = desc ? st.seek_for_prev(pre + "~") : st.seek(pre);   // itr.Seek(seekKey)
    std::vector<uint64_t> acc;
    std::unordered_map<uint64_t, Value> vals;
    UidSet seen;
    while (cur.valid() && has_prefix(cur.key(), pre)) {
      std::optional<std::string> bucket = read_list(st, std::string(cur.key()));   // GetNoStore(IndexKey)
      std::vector<uint64_t> inter;
      if (bucket)
        for (uint64_t u : decode_posting(*bucket))
          if (cand.contains(u) && seen.insert(u)) inter.push_back(u);   // removeDuplicates
      for (uint64_t u : inter) vals[u] = read_value(st, prop, u);       // fetchValue: GetNoStore(DataKey)
      std::stable_sort(inter.begin(), inter.end(), [&](uint64_t a, uint64_t b) {   // sortByValue
        int c = compare(or_zero(vals[a]), or_zero(vals[b]));
        return desc ? c > 0 : c < 0;
      });
      acc.insert(acc.end(), inter.begin(), inter.end());
      if (int64_t(acc.size()) >= k) break;                           // errDone, before the iterator advances
      if (desc) cur.prev(); else cur.next();                         // itr.Next()
    }
    std::unordered_map<uint64_t, Value> second;
    if (!second_key.empty()) {
      UidSet in_acc(acc.size());
      for (uint64_t u : acc) in_acc.insert(u);
      std::vector<uint64_t> nulls;
      for (uint64_t v : row_vids)
        if (!in_acc.contains(v)) nulls.push_back(v);
      int64_t remaining = k - int64_t(acc.size());
      size_t can = remaining < 0 ? nulls.size() : std::min(size_t(remaining), nulls.size());   // the overflow
      std::vector<uint64_t> fetch = acc;
      fetch.insert(fetch.end(), nulls.begin(), nulls.begin() + can);
      const std::vector<std::string> second_list = {second_key};
      RowBuilder sb(props_names(second_list));           // the fetched {second_key} rows: one shape
      for (uint64_t v : fetch) second[v] = props_of(st, v, second_list, sb).field(second_key);   // distinct already
    }
    struct Out { Value row; Value key; Value second; };
    std::vector<Out> out;
    RowWith with_key(prop), with_second(second_key);     // the output shape made once per input shape
    for (uint64_t u : acc) {
      uint64_t v = u;
      Out o{with_key(rows[by_vid.at(v)], vals[u]), vals[u], Value()};
      if (!second_key.empty()) {
        o.second = second.count(v) ? second[v] : Value();
        o.row = with_second(o.row, o.second);
      }
      out.push_back(std::move(o));
    }
    // the second key (nulls last ascending; Python's reversed tuple sort puts them first), then the first,
    // both stable -- the ordering is exact on both keys
    std::stable_sort(out.begin(), out.end(), [id_desc](const Out& a, const Out& b) {
      bool an = a.second.is_null(), bn = b.second.is_null();
      int c = an != bn ? (an ? 1 : -1) : (an ? 0 : compare(a.second, b.second));
      return id_desc ? c > 0 : c < 0;
    });
    std::stable_sort(out.begin(), out.end(), [desc](const Out& a, const Out& b) {
      int c = compare(or_zero(a.key), or_zero(b.key));
      return desc ? c > 0 : c < 0;
    });
    if (int64_t(out.size()) > k) out.resize(size_t(k));
    ValueList res;
    for (auto& o : out) res.push_back(std::move(o.row));
    direct_counts(slot, before, r);
    r.out = Value::list(std::move(res));
  }};
  return ex.run(tasks)[0].out;
}

// a one-key sort on an UNINDEXED predicate (sortWithoutIndex): 1 SEEK per distinct uid (fetchValue), an
// in-memory sort, the top k. Valueless uids go last, by vid; ties by vid (sort.Sort is not stable).
Value DgraphEngine::sort_without_index(StepExec& ex, const ValueList& vids, const std::string& prop, int64_t k,
                                       bool desc) {
  if (k < 0) throw std::runtime_error("sort_without_index: k must be >= 0");
  std::vector<uint64_t> distinct = distinct_vids("dgraph_engine", vids, /*strict=*/false).uids;
  std::vector<TaskFn> tasks = {[&, k, desc](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    std::vector<std::pair<uint64_t, Value>> have, none;
    for (uint64_t v : distinct) {
      Value val = read_value(slot.store, prop, v);
      (val.is_null() ? none : have).push_back({v, val});
    }
    auto by_vid = [](const auto& a, const auto& b) { return a.first < b.first; };   // uid order = the hex16 text's
    std::sort(have.begin(), have.end(), by_vid);
    std::stable_sort(have.begin(), have.end(), [desc](const auto& a, const auto& b) {
      int c = compare(a.second, b.second);
      return desc ? c > 0 : c < 0;
    });
    std::sort(none.begin(), none.end(), by_vid);
    have.insert(have.end(), none.begin(), none.end());
    ValueList out;
    RowBuilder rb({"_vid", prop});
    for (size_t i = 0; i < have.size() && int64_t(i) < k; i++) {
      RowBuilder::Row o = rb.start();
      o.push(Value::vid(have[i].first));
      o.push(have[i].second);
      out.push_back(o.done());
    }
    direct_counts(slot, before, r);
    r.out = Value::list(std::move(out));
  }};
  return ex.run(tasks)[0].out;
}

// a NESTED block's `(orderasc: <unindexed pred>)`: each parent's list sorted on its own -- 1 SEEK per
// (parent, child) row, no dedup across parents; ordered by (parent, value, child), valueless last
Value DgraphEngine::sort_without_index_per_src(StepExec& ex, const ValueList& rows, const std::string& prop,
                                               const std::string& src, const std::string& dst) {
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    struct Keyed { const Value* row; Value val; };
    std::vector<Keyed> keyed;
    for (auto& row : rows)
      keyed.push_back({&row, read_value(slot.store, prop, uid_or_throw("dgraph_engine", row.field(dst)))});
    std::stable_sort(keyed.begin(), keyed.end(), [&](const Keyed& a, const Keyed& b) {
      if (int c = compare(a.row->field(src), b.row->field(src))) return c < 0;
      bool an = a.val.is_null(), bn = b.val.is_null();
      if (an != bn) return bn;                                       // a value sorts before none
      if (!an)
        if (int c = compare(a.val, b.val)) return c < 0;
      return compare(a.row->field(dst), b.row->field(dst)) < 0;
    });
    ValueList out;
    RowWith with(prop);                                  // the output shape made once per input shape
    for (auto& kr : keyed) out.push_back(with(*kr.row, kr.val));
    direct_counts(slot, before, r);
    r.out = Value::list(std::move(out));
  }};
  return ex.run(tasks)[0].out;
}

// `func: has(<pred>)`: a key-only scan of the predicate's range -- 1 SEEK + 1 NEXT per subject key; the landing
// past the range is an absence miss. Returns the subject vids.
Value DgraphEngine::has_scan(StepExec& ex, const std::string& etype, const std::string& dir) {
  if (dir != "out" && dir != "in") throw std::runtime_error("has_scan: dir must be out or in, got '" + dir + "'");
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    std::string pre = prefix(etype, dir == "out" ? kData : kReverse);
    ValueList out;
    Cursor cur = slot.store.seek(pre);
    while (cur.valid() && has_prefix(cur.key(), pre)) {
      std::string_view key = cur.key();
      if (key.size() != pre.size() + 8) throw std::runtime_error("has_scan: a non-uid key under " + etype);
      uint64_t u = 0;
      for (size_t i = pre.size(); i < key.size(); i++) u = (u << 8) | uint8_t(key[i]);
      out.push_back(Value::vid(u));                                  // the KEY only: has() reads no posting
      cur.next();
    }
    end_scan(cur);
    direct_counts(slot, before, r);
    r.out = Value::list(std::move(out));
  }};
  return ex.run(tasks)[0].out;
}

// an inequality at the query ROOT over the day-token index (tokens.go getInequalityTokens, task.go
// handleUidPostings + handleCompareFunction): a key-only token walk (1 SEEK + 1 NEXT per token taken; `between`
// forward from lo's token, `lt` backward from hi's to the start), 1 SEEK per token's posting, then the exact
// re-check of every uid in the edge buckets (1 SEEK each): `between` the first and last, `lt` the first only when
// it is hi's own day
Value DgraphEngine::ineq_root_scan(StepExec& ex, const std::string& prop, const std::string& fname, const Value& lo,
                                   const Value& hi) {
  if (fname != "between" && fname != "lt")
    throw std::runtime_error("ineq_root_scan: unsupported function '" + fname + "'");
  require_index({}, prop);
  int64_t hi_v = hi.as_int();
  int64_t lo_v = fname == "between" ? lo.as_int() : 0;
  std::vector<TaskFn> tasks = {[&, hi_v, lo_v](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    CountingStore& st = slot.store;
    std::string pre = prefix(prop, kIndex);
    std::string hi_tok = day_token(hi_v);
    std::vector<std::string> tokens;
    if (fname == "between") {
      Cursor cur = st.seek(term_key(prop, day_token(lo_v)));
      while (cur.valid() && has_prefix(cur.key(), pre)) {
        std::string tok(cur.key().substr(pre.size()));
        if (tok > hi_tok) break;                                     // past hi's token: no Next
        tokens.push_back(tok);
        cur.next();
      }
      end_scan(cur);
    } else {
      Cursor cur = st.seek_for_prev(term_key(prop, hi_tok));          // Reverse=true
      while (cur.valid() && has_prefix(cur.key(), pre)) {
        tokens.push_back(std::string(cur.key().substr(pre.size())));
        cur.prev();
      }
      end_scan(cur);
    }
    std::vector<std::vector<uint64_t>> buckets;
    for (auto& t : tokens) {
      std::optional<std::string> raw = read_list(st, term_key(prop, t));   // GetUids(IndexKey)
      buckets.push_back(raw ? decode_posting(*raw) : std::vector<uint64_t>{});
    }
    auto recheck = [&](size_t i, auto keep) {                        // filterRow(i): 1 SEEK per uid
      std::vector<uint64_t> kept;
      for (uint64_t u : buckets[i]) {
        Value v = read_value(st, prop, u);
        if (!v.is_null() && keep(v.as_int())) kept.push_back(u);
      }
      buckets[i] = std::move(kept);
    };
    if (!buckets.empty() && fname == "between") {
      auto in = [&](int64_t v) { return lo_v <= v && v <= hi_v; };   // CompareBetween: inclusive at both ends
      recheck(0, in);
      recheck(buckets.size() - 1, in);
    } else if (!buckets.empty() && fname == "lt" && tokens[0] == hi_tok) {
      recheck(0, [&](int64_t v) { return v < hi_v; });
    }
    ValueList out;
    UidSet seen;
    for (auto& b : buckets)
      for (uint64_t u : b)
        if (seen.insert(u)) out.push_back(Value::vid(u));
    direct_counts(slot, before, r);
    r.out = Value::list(std::move(out));
  }};
  return ex.run(tasks)[0].out;
}

// `first: k` over a uid list: the k items with the smallest LIVE Dgraph uid (in memory, 0 KV). The loader
// assigned those uids, so the map comes with the request ([vid, uid] pairs, captured with the live counts).
Value DgraphEngine::first_k_by_uid(const ValueList& items, int64_t k, const ValueList& uid_of) {
  std::unordered_map<std::string, int64_t> uid;                    // the request's vids, as its text
  for (auto& p : uid_of) uid[p.as_list().at(0).text()] = p.as_list().at(1).as_int();
  std::vector<std::pair<int64_t, Value>> keyed;
  size_t missing = 0;
  for (auto& v : items) {
    auto it = uid.find(v.text());
    if (it == uid.end()) missing++;
    else keyed.push_back({it->second, v});
  }
  if (missing)
    throw std::runtime_error("first_k_by_uid: " + std::to_string(missing) + " vids have no uid in the Dgraph uid map");
  std::stable_sort(keyed.begin(), keyed.end(), [](const auto& a, const auto& b) { return a.first < b.first; });
  ValueList out;
  for (size_t i = 0; i < keyed.size() && int64_t(i) < k; i++) out.push_back(keyed[i].second);
  return Value::list(std::move(out));
}

// --- dispatch -----------------------------------------------------------------------------------

static const std::set<std::string> kEngineOps = {"sort_topk", "sort_without_index", "sort_without_index_per_src",
                                                 "has_scan", "ineq_root_scan", "first_k_by_uid"};

bool DgraphEngine::has_kv_op(const std::string& op) const { return kEngineOps.count(op) > 0 || Dgraph::has_kv_op(op); }

Value DgraphEngine::run_kv_op(const Step& step, const Args& a, StepExec& ex) {
  const std::string& op = step.op;
  if (is_write_op(op))
    throw std::runtime_error("dgraph_engine: reads only (the engine-fidelity check measures reads; '" + op + "')");
  if (op == "read_vertex_by_id") return read_vertex_by_id(ex, a.get("vid"));
  if (op == "read_anchor_props") return read_anchor_props(ex, a.list("cands"), a.str_list("props"));
  if (op == "read_filter_by_label_index") {
    // `type(<label>)` at typefilterpath=index: ONE SEEK of the dgraph.type posting whatever the schema's caps (the
    // engine plans name this op), an empty input free -- the base's read, pinned here as Python's twin pins it
    // (OVERRIDES) so a base change is seen. A design with no type index has no posting to read: refuse, as Python.
    for (auto& l : a.labels("label"))
      if (!a.list("vids").empty() && !schema_.has_type_index(l))
        throw std::runtime_error("dgraph_engine read_filter_by_label_index('" + l + "'): this design declares no "
                                 "type index for it");
    return Dgraph::run_kv_op(step, a, ex);
  }
  if (op == "sort_topk")
    return sort_topk(ex, a.list("rows"), a.str("prop"), a.integer("k"), a.has("second_key") ? a.str("second_key") : "",
                     a.has("id_order") && a.str("id_order") == "desc", a.has("index") ? a.str("index") : "",
                     a.flag("desc", true));
  if (op == "sort_without_index")
    return sort_without_index(ex, a.list("vids"), a.str("prop"), a.integer("k"), a.flag("desc", false));
  if (op == "sort_without_index_per_src")
    return sort_without_index_per_src(ex, a.list("rows"), a.str("prop"), a.has("src") ? a.str("src") : "src",
                                      a.has("dst") ? a.str("dst") : "dst");
  if (op == "has_scan") return has_scan(ex, a.str("etype"), a.has("dir") ? a.str("dir") : "out");
  if (op == "ineq_root_scan") return ineq_root_scan(ex, a.str("prop"), a.str("fname"), a.opt("lo"), a.get("hi"));
  if (op == "first_k_by_uid") {
    if (!a.has("uid_of"))
      throw std::runtime_error("first_k_by_uid needs the live Dgraph uid map (the step's uid_of arg): validate.py "
                               "loads it from the fidelity snapshot");
    return first_k_by_uid(a.list("items"), a.integer("k"), a.list("uid_of"));
  }
  return Dgraph::run_kv_op(step, a, ex);
}

}  // namespace evalcpp::dgraph
