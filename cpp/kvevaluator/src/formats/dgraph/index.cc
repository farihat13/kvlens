// Dgraph's lookups by label or property value: index eq and range, the day walks, the three top-k walks, a label's
// vertices and the two label filters.
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

std::string term_of_value(const Value& v) {   // Python str(value)
  if (v.is_int()) return std::to_string(v.as_int());
  if (v.is_text()) return v.text();                 // a vid is its hex16 str
  if (v.is_bool()) return v.as_bool() ? "True" : "False";
  throw std::runtime_error("dgraph: an index term must be an int or a str, got " + repr(v));
}

Value Dgraph::read_index_eq(StepExec& ex, const std::string& label, const std::string& prop, const Value& value) {
  require_index(label.empty() ? std::vector<std::string>{} : std::vector<std::string>{label}, prop);
  if (label.empty() && !type_scoped_.empty())   // a bare predicate has no posting under a type-scoped design: refuse, never []
    throw std::runtime_error("dgraph read_index_eq: a type-scoped index needs the step's label (" + prop + ")");
  if (is_key_sorted(label, prop))
    throw std::runtime_error("dgraph read_index_eq: " + label + "." + prop + " is a key-sorted date index; use read_index_range [v, v+1)");
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {   // one point lookup: uids in the value
    Counts before = slot.store.counts();
    std::string k = term_key(index_pred(label, prop), index_term(label, prop, value));   // per type when type-scoped
    std::optional<std::vector<std::string>> parts = read_unit(slot.store, k);
    ValueList out;
    if (parts) {
      uint64_t used = 0;
      for (auto& raw : *parts) {
        std::vector<uint8_t> w;
        for (uint64_t u : decode_posting_w(raw, &w)) out.push_back(Value::vid(u));
        for (uint8_t x : w) used += x;                   // every uid is output
      }
      slot.store.count_used(k.size(), used);
    }
    direct_counts(slot, before, r);
    r.out = Value::list(std::move(out));
  }};
  return ex.run(tasks)[0].out;
}

// a landing under a day-token predicate's index prefix: the 7-digit token, or (chunked) one of
// its parts (store/unit_value.h) -- `tok` = the token either way. At ∞ no part key exists, so
// this accepts exactly the keys the prefix test did (every key there is a 7-digit token:
// day_token refuses any other width).
static bool day_landing(std::string_view key, const std::string& pre, std::string_view& tok) {
  if (!has_prefix(key, pre)) return false;
  size_t n = pre.size() + 7;
  if (key.size() != n && !unit::is_part_key(key, n)) return false;
  tok = key.substr(pre.size(), 7);
  return true;
}
// walking BACKWARDS, is this landing the last of its token -- the whole, or part 0? (a split
// token's parts sort after its bare key, so a backward walk meets part n-1 first)
static bool token_done(std::string_view key, const std::string& pre) {
  size_t n = pre.size() + 7;
  return !unit::is_part_key(key, n) || unit::part_start(key, n) == 0;
}
// used_key of a landing: the LOGICAL token key, once per token (on the whole, or on part 0 --
// met exactly once by a walk in either direction), 0 on any other part: a split token costs
// the same used_key as an unsplit one; the part suffixes are the layout's amplification
// (the posting index's convention; read_index_eq charges the logical key once too).
static size_t landing_used_key(std::string_view key, const std::string& pre) {
  return token_done(key, pre) ? pre.size() + 7 : 0;
}

std::vector<std::string> Dgraph::day_walk_preds(const std::vector<std::string>& labels, const std::string& prop,
                                                const char* op) const {
  if (labels.empty() && scoped_props_.count(prop))
    throw std::runtime_error(std::string(op) + ": " + prop + " is type-scoped; the step must name its labels");
  size_t n = 0;
  for (auto& l : labels) n += type_scoped_.count({l, prop}) ? 1 : 0;
  if (n && n != labels.size())
    throw std::runtime_error(std::string(op) + ": " + prop + " is type-scoped on some of the labels but not all -- one walk per op");
  if (!n) return {prop};
  std::vector<std::string> preds;
  for (auto& l : labels) preds.push_back(index_pred(l, prop));
  return preds;
}

// the day-token walk: per band (day_walk_preds -- predicate-global unless every label type-scopes the prop), seek the
// lo day, step to the hi day, union each token's whole posting (chunked: each landing is a token or one of its parts;
// 1 NEXT per landing); a value-sorted bucket yields its uids
Value Dgraph::read_index_range(StepExec& ex, const std::vector<std::string>& labels, const std::string& prop,
                               int64_t lo, int64_t hi, bool buckets) {
  require_index(labels, prop);
  if (key_sorted_walk(labels, prop, buckets ? "index_range_buckets" : "read_index_range")) {
    // the key-sorted walk (SortedIndex::range, verbatim in structure): per label, seek the lo ms
    // under its per-type predicate and step while the key's ms < hi -- every entry crossed is in
    // the window (exact, no boundary superset), key-only (0 value bytes), 1 NEXT each; the
    // landing past the window is an absence miss. `_bucket` keeps the op's contract: the DAY number.
    RowBuilder bk({"_vid", "_bucket"});
    std::vector<TaskFn> tasks = {[&, lo, hi, buckets](WorkerSlot& slot, TaskResult& r) {
      Counts before = slot.store.counts();
      ValueList out;
      RtParallel par(slot.store);                          // one member per label's walk
      for (auto& label : labels) {
        par.member();
        std::string pre = prefix(index_pred(label, prop), kIndex);
        Cursor cur = slot.store.seek(pre + IndexKeys::sorted_bytes(lo));
        while (cur.valid() && key_sorted_landing(cur.key(), pre) && key_sorted_ms(cur.key(), pre) < hi) {
          slot.store.count_used(cur.key().size(), 0);              // every in-window entry is output
          uint64_t u = key_sorted_uid(cur.key());
          if (buckets) {
            RowBuilder::Row b = bk.start();
            b.push(Value::vid(u));
            b.push(Value(day_number(key_sorted_ms(cur.key(), pre))));
            out.push_back(b.done());
          }
          else out.push_back(Value::vid(u));
          cur.next();
        }
        end_scan(cur);
      }
      direct_counts(slot, before, r);
      r.out = Value::list(std::move(out));
    }};
    return ex.run(tasks)[0].out;
  }
  std::vector<std::string> preds = day_walk_preds(labels, prop, buckets ? "index_range_buckets" : "read_index_range");
  RowBuilder bk({"_vid", "_bucket"});
  std::vector<TaskFn> tasks = {[&, lo, hi, buckets](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    std::string hi_tok = day_token(hi - 1);   // hi is exclusive: a midnight hi must not add its own day
    ValueList out;
    RtParallel par(slot.store);                            // one member per band's walk
    for (auto& pred : preds) {
      par.member();
      std::string pre = prefix(pred, kIndex);
      bool sorted_value = value_sorted_preds_.count(pred) > 0;
      Cursor cur = slot.store.seek(term_key(pred, day_token(lo)));   // a split lo token: its part 0
      std::string_view tok;
      while (cur.valid() && day_landing(cur.key(), pre, tok) && tok <= hi_tok) {
        int64_t day = std::stoll(std::string(tok));
        std::vector<uint64_t> uids;
        uint64_t used = 0;
        if (sorted_value) {
          for (auto& [u, ms] : decode_value_sorted(cur.value())) uids.push_back(u);
          used = 8 * uids.size();                            // the uids are output; the ms ride along unread
        } else {
          std::vector<uint8_t> w;
          uids = decode_posting_w(cur.value(), &w);
          for (uint8_t x : w) used += x;                     // every uid in the window IS output
        }
        for (uint64_t u : uids) {
          if (buckets) {
            RowBuilder::Row b = bk.start();
            b.push(Value::vid(u));
            b.push(Value(day));
            out.push_back(b.done());
          }
          else out.push_back(Value::vid(u));
        }
        slot.store.count_used(landing_used_key(cur.key(), pre), used);   // the token key = the windowing decision
        cur.next();                                                    // 1 NEXT per landing
      }
      end_scan(cur);
    }
    direct_counts(slot, before, r);
    r.out = Value::list(std::move(out));
  }};
  return ex.run(tasks)[0].out;
}

// The label read with its properties in one run per property (per-label predicates only): the label's type posting
// (1 SEEK: every vertex of the label, so a vertex that lacks a property still gets its row), then per wanted
// property ONE scan of its predicate `<Label>.<prop>`: 1 SEEK + 1 NEXT per stored value, key and value read. Rows as
// read_vertex_props builds them, in uid order. Python: Dgraph.read_vertices_by_label(props=...).
Value Dgraph::read_label_run(StepExec& ex, const std::string& label, const std::vector<std::string>& props) {
  Value vids = read_vertices_by_label(ex, label, Value());
  // one column per wanted property: (uid, value) in key order = uid order, as the type posting is; merged below
  // without a map (a 36 M-vertex label would hold a map node per value)
  std::vector<std::vector<std::pair<uint64_t, Value>>> cols(props.size());
  std::vector<TaskFn> tasks;
  for (size_t pi = 0; pi < props.size(); pi++) {
    if (props[pi] == "id") continue;                       // the vid is the uid: derived
    tasks.push_back([&, pi](WorkerSlot& slot, TaskResult& r) {
      Counts before = slot.store.counts();
      std::string pre = prefix(data_pred(label, props[pi]), kData);
      Cursor cur = slot.store.seek(pre);
      while (cur.valid() && has_prefix(cur.key(), pre)) {
        if (cur.key().size() != pre.size() + 8)
          throw std::runtime_error("dgraph read_vertices_by_label(props): a key under " + props[pi] + " is not predicate + uid");
        uint64_t u = 0;
        for (size_t i = pre.size(); i < cur.key().size(); i++) u = (u << 8) | uint8_t(cur.key()[i]);
        auto raw = cur.value();                                        // read once at this landing
        slot.store.count_used(cur.key().size(), raw.size());           // every value of the run is a requested field
        cols[pi].emplace_back(u, prim::decode_leaf(raw));
        cur.next();
      }
      end_scan(cur);                                                   // the landing past the predicate: a miss
      direct_counts(slot, before, r);
    });
  }
  if (!tasks.empty()) ex.run(tasks);
  ValueList out;
  out.reserve(vids.as_list().size());
  std::vector<size_t> at(props.size(), 0);
  RowBuilder rb(props_names(props));                       // the call's one shape
  uint64_t prev = 0;
  bool first = true;
  for (auto& v : vids.as_list()) {
    uint64_t u = uid_or_throw("dgraph", v);
    if (!first && u <= prev) throw std::runtime_error("dgraph read_vertices_by_label(props): the type posting is not uid-ascending");
    prev = u; first = false;
    RowBuilder::Row f = rb.start();
    f.push(v);
    for (size_t pi = 0; pi < props.size(); pi++) {
      if (props[pi] == "id") { f.push(key_id_value(u)); continue; }
      auto& col = cols[pi];
      while (at[pi] < col.size() && col[at[pi]].first < u) at[pi]++;   // a value of a vertex the posting lacks: skipped
      bool hit = at[pi] < col.size() && col[at[pi]].first == u;
      f.push(hit ? col[at[pi]].second : Value());
    }
    out.push_back(f.done());
  }
  return Value::list(std::move(out));
}

Value Dgraph::read_vertices_by_label(StepExec& ex, const std::string& label, const Value& props) {
  if (!props.is_null()) {
    if (!per_label_) throw std::runtime_error("dgraph read_vertices_by_label takes no props (dgraph.py) unless the schema names predicates per label");
    std::vector<std::string> names;
    for (auto& p : props.as_list()) names.push_back(p.as_str());
    return read_label_run(ex, label, names);
  }
  if (!schema_.has_type_index(label))
    throw std::runtime_error("dgraph read_vertices_by_label('" + label + "'): this design declares no per-type index "
                             "for it, and dgraph has no other way to enumerate one type (its value indexes are "
                             "per-predicate and mix types)");
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {   // one posting: every uid of the type
    Counts before = slot.store.counts();
    std::string k = term_key("dgraph.type", label);
    std::optional<std::vector<std::string>> parts = read_unit(slot.store, k);
    ValueList out;
    if (parts) {
      uint64_t used = 0;
      for (auto& raw : *parts) {
        std::vector<uint8_t> w;
        for (uint64_t u : decode_posting_w(raw, &w)) out.push_back(Value::vid(u));
        for (uint8_t x : w) used += x;
      }
      slot.store.count_used(k.size(), used);
    }
    direct_counts(slot, before, r);
    r.out = Value::list(std::move(out));
  }};
  return ex.run(tasks)[0].out;
}

Value Dgraph::read_filter_by_label_probe(StepExec& ex, const ValueList& vids, const std::vector<std::string>& labels) {
  // the per-candidate probe: one dgraph.type list read per DISTINCT vid (S = distinct vids); a repeated vid is kept at
  // every occurrence
  if (vids.empty()) return Value::list({});
  if (labels.size() != 1) throw std::runtime_error("dgraph read_filter_by_label_probe needs one label");
  DistinctVids d = distinct_vids("dgraph", vids, /*strict=*/false);
  std::vector<Value> res = ex.run_range_map(d.uids.size(), [&](WorkerSlot& slot, size_t ti, std::vector<Landing>& log) {
      std::string k = uid_key("dgraph.type", kData, d.uids[ti]);
      std::optional<std::string> raw = read_list(slot.store, k);
      // the type record IS the decision field (any store must read a type FIELD to decide)
      if (raw) slot.store.count_used(k.size(), raw->size());
      bool hit = false;
      if (raw) {
        Value types = prim::decode_leaf(*raw);   // a local: a range-for over a temporary's list dangles
        for (auto& t : types.as_list())
          if (t.as_str() == labels[0]) hit = true;
      }
      return Value(hit);
  }, /*additive_counts=*/true);
  ValueList out;
  for (size_t i = 0; i < vids.size(); i++)
    if (res[d.at.empty() ? i : d.at[i]].as_bool()) out.push_back(vids[i]);
  return Value::list(std::move(out));
}

Value Dgraph::read_filter_by_label_index(StepExec& ex, const ValueList& vids, const std::vector<std::string>& labels) {
  if (vids.empty()) return Value::list({});
  if (labels.size() != 1) throw std::runtime_error("dgraph read_filter_by_label_index needs one label");
  // Probe each scanned member against the (small) CANDIDATE set in uid space -- do NOT materialize the
  // whole type membership as hex-string Values + a members-sized hash set (that was O(type-size) with a
  // heavy per-uid string/Value constant, ~2s for a 135K posting). We still scan+charge the whole posting
  // (the plan's choice; accounting below is unchanged); only the in-memory filter is cheap now.
  UidSet want(vids.size());                       // flat, no node per candidate (util/uidset.h)
  for (auto& v : vids) want.insert(uid_or_throw("dgraph", v));
  UidSet found(vids.size());
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {   // 1 SEEK, the whole posting
    Counts before = slot.store.counts();
    std::string k = term_key("dgraph.type", labels[0]);
    std::optional<std::vector<std::string>> parts = read_unit(slot.store, k);
    if (parts) {
      uint64_t used = 0;
      for (auto& raw : *parts) {
        std::vector<uint8_t> w;
        std::vector<uint64_t> uids = decode_posting_w(raw, &w);
        for (size_t j = 0; j < uids.size(); j++) {
          // RULED: every scanned member is INSPECTED for a candidacy decision -> USED
          // (the whole-type-scan cost is the plan's choice, B3 -> the absolute channels);
          // only the posting FRAMING stays amp
          used += w[j];
          if (want.contains(uids[j])) found.insert(uids[j]);   // integer probe of the small candidate set
        }
      }
      slot.store.count_used(k.size(), used);
    }
    direct_counts(slot, before, r);
    r.out = Value::list({});
  }};
  ex.run(tasks);
  ValueList out;                                          // keep candidates that ARE members, in input order
  for (auto& v : vids)
    if (found.contains(uid_or_throw("dgraph", v))) out.push_back(v);
  return Value::list(std::move(out));
}

// The ranking walk real dgraph runs for `orderdesc: creationDate, first: k` (dgraph.py, verbatim
// in structure): walk the day-token index newest-first from hi's day, intersect each token with
// the candidate set until k candidates are collected, GET the exact ms of the crossed candidates,
// derive id only for the date-tie group at the k-th boundary, props for the final k only.
TopKReads Dgraph::top_k_reads(StepExec&, const kv::TopKArgs& t) {
  const std::string& prop = t.prop;
  const std::vector<std::string>& labels = t.labels;
  require_index(labels, prop);
  TopKReads rd;
  rd.fmt = "dgraph";
  // a winner's props: its predicates' postings (1 GET each), the winners' reads together
  rd.winner_props = [this, &t](WorkerSlot& slot, std::vector<Landing>&, const TopKHit& h) {
    RowBuilder pb(props_names(t.props));
    Value extra = props_of(slot.store, h.vid.as_vid(), t.props, pb);
    std::vector<Value> cells;
    for (auto& p : t.props) cells.push_back(extra.field(p));
    return cells;
  };
  if (key_sorted_walk(labels, prop, "read_top_k_by_index")) { rd.walk = top_k_walk_key_sorted(t); return rd; }
  // per-type day walks when EVERY label scopes the prop (Python's order: value-sorted, then type-scoped)
  auto all_labels = [&](const std::set<std::pair<std::string, std::string>>& s) {
    if (labels.empty()) return false;
    for (auto& l : labels)
      if (!s.count({l, prop})) return false;
    return true;
  };
  if (all_labels(value_sorted_)) { rd.walk = top_k_walk_day_scoped(t, true); return rd; }
  if (all_labels(type_scoped_)) { rd.walk = top_k_walk_day_scoped(t, false); return rd; }
  // the default walk reads the predicate-global band: refuse no labels / a label mix on a type-scoped prop (that band
  // is empty for the scoped types -- a silent []), the range walk's rule
  day_walk_preds(labels, prop, "read_top_k_by_index");
  if (t.vids.is_null())
    throw std::runtime_error("dgraph read_top_k_by_index needs a candidate set: its date index mixes all types");
  rd.walk = top_k_walk_day_global(t);
  return rd;
}

// The predicate-global walk: one band holds every label's values, so a hit's label is known only when the step names
// one label (else the winner's row is untagged)
TopKReads::Walk Dgraph::top_k_walk_day_global(const kv::TopKArgs& t) {
  return [this, &t](WorkerSlot& slot, std::vector<Landing>&, const TopKScope& scope) {
    const std::string& prop = t.prop;
    const int64_t k = t.k;
    const UidSet& cand = *scope.cand;                             // uid space, flat: no node per candidate
    CountingStore& st = slot.store;
    const std::vector<std::string> one_prop_list = {prop};
    RowBuilder msb(props_names(one_prop_list));             // the hits' {prop} rows: one shape for the call
    std::string pre = prefix(prop, kIndex);
    struct Row { uint64_t vid = 0; int64_t ms = 0; int64_t id = 0; };
    std::vector<Row> rows, pending_rows;
    std::vector<uint64_t> pending;
    auto ms_of = [&](uint64_t vid) {   // read_vertex_props(vid, [prop]): the exact ms, 1 GET
      Value ms = props_of(st, vid, one_prop_list, msb).field(prop);
      if (!ms.is_null() && !ms.is_int()) throw std::runtime_error("read_top_k_by_index: " + prop + " of " + hex16(vid) + " is not an int");
      if (ms.is_null()) throw std::runtime_error("read_top_k_by_index: " + hex16(vid) + " is in the " + prop + " index but has no value");
      return ms.as_int();
    };
    auto distinct = [](std::vector<uint64_t>& v) {   // read_vertex_props reads each distinct vid once
      UidSet seen(v.size());
      std::vector<uint64_t> out;
      for (uint64_t x : v) if (seen.insert(x)) out.push_back(x);
      v = std::move(out);
    };
    // per-token used (RULED): EVERY uid in a crossed token is inspected for candidacy ->
    // USED; the mixed-index tax lives in the absolute channels (B3), only framing = amp.
    // decode the posting ONCE, returning its uids and (via out-param) the summed widths for
    // the used-accounting -- not twice (once for widths, once for uids).
    auto decode_used = [&](std::string_view raw, uint64_t& used) {
      std::vector<uint8_t> w;
      std::vector<uint64_t> uids = decode_posting_w(raw, &w);
      used = 0;
      for (uint8_t x : w) used += x;
      return uids;
    };
    // hi is exclusive: start at the day of hi - 1 (a midnight hi's own day is entirely >= hi)
    std::string start = scope.hi ? day_token(*scope.hi - 1) : "~";   // "~" sorts after digits
    // land on the LAST key of the start token: a split token's parts sort after its bare key,
    // so the target carries the largest part suffix (PostingIndex's rule). At ∞ this is the
    // bare token's own landing: every key under a day predicate's prefix is a 7-digit token.
    Cursor cur = st.seek_for_prev(term_key(prop, start) + std::string(unit::kPartSuffix, '\xff'));
    std::string_view tok;
    // one landing (a token, or one of its parts): read it ONCE, charge its uids as used, and
    // collect the candidates it holds -- the two walks below differ only in where the hits go
    // and in their exit condition
    auto probe_landing = [&](std::vector<uint64_t>& hits) {
      uint64_t used = 0;
      std::vector<uint64_t> uids = decode_used(cur.value(), used);   // ONE decode: uids + summed widths
      st.count_used(landing_used_key(cur.key(), pre), used);
      for (uint64_t u : uids)
        if (cand.contains(u)) hits.push_back(u);                      // probe in uid space
    };
    if (scope.hi && cur.valid() && day_landing(cur.key(), pre, tok) && tok == start) {
      std::vector<uint64_t> bhits;
      // the boundary token: EVERY part of it (parts are contiguous and part 0 exists by
      // construction, so the walk ends there; the landing test guards a broken store)
      while (cur.valid() && day_landing(cur.key(), pre, tok) && tok == start) {
        probe_landing(bhits);
        bool done = token_done(cur.key(), pre);
        cur.prev();                                       // (1 NEXT per landing)
        if (done) break;
      }
      distinct(bhits);
      RtParallel par(st);                                         // one member per boundary hit's ms
      for (uint64_t v : bhits) {                                  // exact-ms check of the boundary day
        par.member();
        int64_t ms = ms_of(v);
        if (ms < *scope.hi) rows.push_back({v, ms, 0});
      }
    }
    // lower day tokens only while the boundary did not already supply k survivors. A split
    // token's parts are ALL consumed before the k-check (mid_token): a same-day candidate in an
    // unread part could outrank a collected one (the exact-ms sort below ranks within a day).
    bool consumed = false;                                // the landing the loop ends on: read, or not
    bool mid_token = false;
    while (cur.valid() && day_landing(cur.key(), pre, tok) &&
           (mid_token || int64_t(rows.size() + pending.size()) < k)) {
      probe_landing(pending);
      mid_token = !token_done(cur.key(), pre);
      if (!mid_token && int64_t(rows.size() + pending.size()) >= k) { consumed = true; break; }
      cur.prev();
    }
    if (!consumed) end_scan(cur);
    distinct(pending);
    {
      RtParallel par(st);                                   // one member per hit's ms
      for (uint64_t v : pending) { par.member(); pending_rows.push_back({v, ms_of(v), 0}); }
    }
    std::vector<Row> ranked = rows;
    ranked.insert(ranked.end(), pending_rows.begin(), pending_rows.end());
    std::stable_sort(ranked.begin(), ranked.end(), [](const Row& a, const Row& b) { return a.ms > b.ms; });
    std::vector<Row> need;
    if (int64_t(ranked.size()) >= k) {
      int64_t cut = ranked[size_t(k - 1)].ms;
      for (auto& x : ranked)
        if (x.ms >= cut) need.push_back(x);
    } else {
      need = ranked;
    }
    std::string label = t.labels.size() == 1 ? t.labels[0] : "";
    std::vector<TopKHit> hits;
    for (auto& x : need)   // id is key-derived: 0 ops
      hits.push_back({tagged(schema_, Value::vid(x.vid), label), x.ms, key_id_value(x.vid).as_int(), label});
    return hits;
  };
}

// Top-k over the KEY-SORTED date index (Python _read_top_k_key_sorted / SortedIndex::top_k_desc,
// verbatim in structure): one cursor per label's per-type index, seek_for_prev on the bare 8 B hi
// bound (lands strictly below hi: an entry AT hi extends the bound with its uid and sorts after),
// merged newest-first, 1 NEXT per entry crossed and no value read, stopping once k hits are in
// hand and the next entry is older (the k-th date's tie group drains first, as nebula's walk).
// Candidate-free is legal (the per-type index IS the type restriction); a candidate set, if
// given, is intersected. Ranking, the hi filter and the result date are key-derived; only the K
// winners pay a props GET.
TopKReads::Walk Dgraph::top_k_walk_key_sorted(const kv::TopKArgs& t) {
  return [this, &t](WorkerSlot& slot, std::vector<Landing>&, const TopKScope& scope) {
    const std::string& prop = t.prop;
    const int64_t k = t.k;
    CountingStore& st = slot.store;
    struct Walker { std::string pre; Cursor cur; std::string label; };   // one per label's index: a hit knows its label
    std::vector<Walker> walkers;
    std::string bound = scope.hi ? IndexKeys::sorted_bytes(*scope.hi) : std::string(16, '\xff');
    {
      RtParallel par(st);                                                     // one member per label's seek
      for (auto& label : t.labels) {
        par.member();
        std::string pre = prefix(index_pred(label, prop), kIndex);
        walkers.push_back({pre, st.seek_for_prev(pre + bound), label});      // 1 SEEK per label
      }
    }
    struct Row { uint64_t vid = 0; int64_t ms = 0; int64_t id = 0; const std::string* label = nullptr; };
    std::vector<Row> hits;
    while (true) {
      int best = -1;
      int64_t best_ms = 0;
      for (size_t i = 0; i < walkers.size(); i++) {
        Walker& w = walkers[i];
        if (!w.cur.valid() || !key_sorted_landing(w.cur.key(), w.pre)) continue;
        int64_t ms = key_sorted_ms(w.cur.key(), w.pre);
        if (best < 0 || ms > best_ms) { best = int(i); best_ms = ms; }
      }
      if (best < 0) break;
      if (int64_t(hits.size()) >= k && best_ms != hits.back().ms) break;   // past the k-th date: the tie group is complete
      Walker& w = walkers[size_t(best)];
      st.count_used(w.cur.key().size(), 0);                                 // key-only: every crossed key is inspected
      uint64_t u = key_sorted_uid(w.cur.key());
      if (!scope.cand || scope.cand->contains(u)) hits.push_back({u, best_ms, 0, &w.label});
      w.cur.prev();                                                         // 1 NEXT per entry crossed
    }
    for (auto& w : walkers) end_scan(w.cur);                                // each rests past its window: an absence miss
    std::vector<TopKHit> out;
    for (auto& x : hits)   // id is key-derived: 0 ops
      out.push_back({tagged(schema_, Value::vid(x.vid), *x.label), x.ms, key_id_value(x.vid).as_int(), *x.label});
    return out;
  };
}

// Top-k over a TYPE-SCOPED day-token date index (Python _read_top_k_type_scoped) or a VALUE-SORTED one
// (_read_top_k_value_sorted), verbatim in structure: one cursor per label's per-type band (seek_for_prev at hi's day,
// or past every token), merged newest-first by day token; at each day, every cursor on it reads its whole bucket and
// steps back (1 NEXT); stop once >= k survivors are in hand at a DAY boundary. Candidate-free is legal (the per-type
// index IS the type restriction); a candidate set, if given, is intersected. The type-scoped walk GETs each hit's
// exact ms (read_vertex_props) and drops the hi day's entries at/after hi; the value-sorted one reads both from the
// bucket. Then rank (date desc, id asc/desc); only the k winners pay a props read. Each cursor ends on an unconsumed
// entry (or off its band): an absence miss.
TopKReads::Walk Dgraph::top_k_walk_day_scoped(const kv::TopKArgs& t, bool value_sorted) {
  return [this, &t, value_sorted](WorkerSlot& slot, std::vector<Landing>&, const TopKScope& scope) {
    const std::string& prop = t.prop;
    const int64_t k = t.k;
    CountingStore& st = slot.store;
    const std::vector<std::string> one_prop_list = {prop};
    RowBuilder msb(props_names(one_prop_list));             // the hits' {prop} rows: one shape for the call
    // hi is exclusive: its last day is (hi - 1)'s; "~" sorts after every digit token
    std::optional<int64_t> hi_day;
    if (scope.hi) hi_day = day_number(*scope.hi - 1);
    std::string start = scope.hi ? day_token(*scope.hi - 1) : "~";
    struct Walker { std::string pre; Cursor cur; std::string label; };   // one per label's band: a hit knows its label
    std::vector<Walker> walkers;
    {
      RtParallel par(st);                                           // one member per label's seek
      for (auto& label : t.labels) {
        par.member();
        std::string pred = index_pred(label, prop);
        walkers.push_back({prefix(pred, kIndex), st.seek_for_prev(term_key(pred, start)), label});   // 1 SEEK each
      }
    }
    auto cur_day = [&](Walker& w) -> std::optional<int64_t> {
      std::string_view tok;
      if (w.cur.valid() && day_landing(w.cur.key(), w.pre, tok)) return std::stoll(std::string(tok));
      return std::nullopt;
    };
    struct Row { uint64_t vid = 0; int64_t ms = 0; int64_t id = 0; const std::string* label = nullptr; };
    std::vector<Row> ranked;
    while (true) {
      std::optional<int64_t> best;
      for (auto& w : walkers)
        if (auto d = cur_day(w); d && (!best || *d > *best)) best = d;
      if (!best) break;
      std::vector<uint64_t> day_uids;
      std::vector<const std::string*> day_labels;                   // the band each of day_uids came from
      for (auto& w : walkers) {
        if (cur_day(w) != best) continue;
        std::string_view raw = w.cur.value();                       // the whole day bucket (scan value bytes)
        uint64_t used = 0;
        if (value_sorted) {
          for (auto& [u, ms] : decode_value_sorted(raw)) {
            if (scope.hi && ms >= *scope.hi) continue;                      // the exact intra-day hi filter, free
            if (!scope.cand || scope.cand->contains(u)) ranked.push_back({u, ms, 0, &w.label});
          }
          used = raw.size();                                        // every (uid, ms) is inspected
        } else {
          std::vector<uint8_t> wd;
          for (uint64_t u : decode_posting_w(raw, &wd)) { day_uids.push_back(u); day_labels.push_back(&w.label); }
          for (uint8_t x : wd) used += x;
        }
        st.count_used(w.cur.key().size(), used);
        w.cur.prev();                                               // 1 NEXT per token stepped
      }
      if (!value_sorted) {
        std::vector<uint64_t> hits;
        std::vector<const std::string*> hit_labels;
        UidSet seen(day_uids.size());
        for (size_t i = 0; i < day_uids.size(); i++) {
          uint64_t u = day_uids[i];
          if ((!scope.cand || scope.cand->contains(u)) && seen.insert(u)) { hits.push_back(u); hit_labels.push_back(day_labels[i]); }
        }
        RtParallel par(st);                                         // one member per hit's ms
        for (size_t i = 0; i < hits.size(); i++) {                  // the exact ms: read_vertex_props(hits, [prop])
          uint64_t v = hits[i];
          par.member();
          Value ms = props_of(st, v, one_prop_list, msb).field(prop);
          if (!ms.is_int()) throw std::runtime_error("read_top_k_by_index: " + hex16(v) + " is in the " + prop + " index without an int value");
          if (scope.hi && *best == *hi_day && ms.as_int() >= *scope.hi) continue;   // within the hi day: keep < hi only
          ranked.push_back({v, ms.as_int(), 0, hit_labels[i]});
        }
      }
      if (int64_t(ranked.size()) >= k) break;                       // the k-th DAY boundary crossed: done
    }
    for (auto& w : walkers) end_scan(w.cur);
    std::vector<Row> need = ranked;
    if (!value_sorted) {                                            // the boundary tie group only (Python's cut)
      std::stable_sort(need.begin(), need.end(), [](const Row& a, const Row& b) { return a.ms > b.ms; });
      if (int64_t(need.size()) >= k) {
        int64_t cut = need[size_t(k - 1)].ms;
        std::vector<Row> keep;
        for (auto& x : need)
          if (x.ms >= cut) keep.push_back(x);
        need = std::move(keep);
      }
    }
    std::vector<TopKHit> out;
    for (auto& x : need)   // id is key-derived: 0 ops
      out.push_back({tagged(schema_, Value::vid(x.vid), *x.label), x.ms, key_id_value(x.vid).as_int(), *x.label});
    return out;
  };
}

}  // namespace evalcpp::dgraph
