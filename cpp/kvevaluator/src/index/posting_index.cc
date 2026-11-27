#include "index/posting_index.h"

#include <algorithm>
#include <stdexcept>

#include "codec/posting.h"
#include "formats/common.h"
#include "index/sorted_index.h"
#include "store/unit_posting.h"
#include "store/unit_value.h"
#include "util/vid.h"

namespace evalcpp {

std::string PostingIndex::term_key(int index_id, std::string_view value_bytes) {
  std::string k = IndexKeys::prefix(index_id);
  k.append(value_bytes.data(), value_bytes.size());
  return k;
}

bool PostingIndex::single_posting_type_index() const {
  bool all = !schema_.vertices().empty();
  for (auto& v : schema_.vertices()) all = all && schema_.has_type_index(v.label);
  return all;
}

std::vector<std::pair<int, std::string>> PostingIndex::terms_of(const std::string& label, const Value& props) const {
  std::vector<std::pair<int, std::string>> out;
  if (schema_.has_type_index(label)) out.emplace_back(schema_.type_index_decl(label).id, "");
  for (auto& kv : props.as_row()) {
    if (kv.second.is_null()) continue;
    const IndexDecl* ix = schema_.find_index(label, kv.first);
    if (!ix || ix->kind == "type") continue;
    out.emplace_back(ix->id, index_value_bytes(schema_, *ix, label, kv.second, date_granularity_));
  }
  return out;
}

// --- reads --------------------------------------------------------------------------------------

std::vector<uint64_t> PostingIndex::decode_landing(std::string_view raw, uint64_t& used) {
  std::vector<uint8_t> w;
  std::vector<uint64_t> uids = posting::decode_posting_w(raw, &w);
  for (uint8_t x : w) used += x;   // every uid of a crossed term is inspected/emitted -> USED; framing = amp
  return uids;
}

// used_key of a cursor landing: the LOGICAL term key, charged once per term -- on the whole
// unit or on part 0 (a walk in either direction meets part 0 exactly once), 0 on any other
// part. eq charges the same logical key once (read_term), so a split term costs the same
// used_key whichever way it is reached; the part suffixes are the layout's amplification.
// `term_len` = the landing's whole term key length (prefix + the term's bytes), told by the
// caller from the term's encoding: a key's tail alone cannot tell a part (a sorted term's 8
// bytes, a short str term, an index id can all hold the 0x00 a part suffix starts with).
static size_t landing_used_key(std::string_view key, size_t term_len) {
  if (unit::is_part_key(key, term_len))
    return unit::part_start(key, term_len) == 0 ? term_len : 0;
  return key.size();
}

// the whole term key length of a landing under `prefix`: a sorted / int term is 8 bytes; a str
// term is its VelocyPack string (IndexKeys::vpack_str_bytes: a 0x40+len header, or 0xbf + an
// 8-byte little-endian length, then the bytes)
static size_t term_len_at(std::string_view key, size_t prefix_len, bool str_term) {
  if (!str_term) return prefix_len + 8;
  if (key.size() <= prefix_len) return key.size();
  uint8_t h = uint8_t(key[prefix_len]);
  if (h != 0xbf) return prefix_len + 1 + (h - 0x40);
  uint64_t n = 0;
  for (int i = 7; i >= 0 && prefix_len + 1 + size_t(i) < key.size(); i--) n = (n << 8) | uint8_t(key[prefix_len + 1 + size_t(i)]);
  return prefix_len + 9 + size_t(n);
}

bool PostingIndex::read_term(CountingStore& st, const std::string& key, const std::function<void(uint64_t)>& fn) const {
  std::optional<std::vector<std::string>> parts = unit::read(st, key);
  if (!parts) return false;                                   // the miss lane, 0 used
  uint64_t used = 0;
  for (auto& raw : *parts)
    for (uint64_t u : decode_landing(raw, used)) fn(u);
  st.count_used(key.size(), used);                            // the term key located its posting
  return true;
}

ValueList PostingIndex::eq(CountingStore& st, const std::string& label, const std::string& prop, const Value& value) const {
  const IndexDecl& ix = schema_.index_decl(label, prop);
  // a coarse date term holds a whole day: an equality on it would silently answer with the
  // day's members -- a plan wanting one timestamp must range [v, v+1) and re-filter
  if (ix.kind == "sorted" && date_granularity_ != DateGranularity::full)
    throw std::runtime_error("read_index_eq on " + label + "." + prop + ": the date index is " +
                             date_granularity_name(date_granularity_) + "-granular; use read_index_range [v, v+1) + a precise filter");
  ValueList out;
  read_term(st, term_key(ix.id, index_value_bytes(schema_, ix, label, value, date_granularity_)), [&](uint64_t u) { out.push_back(Value::vid(u)); });
  return out;
}

// a landing under `prefix` is a sorted term (prefix + 8) or one of its parts (prefix + 8 + suffix)
static bool sorted_landing(std::string_view key, const std::string& prefix, int64_t& value) {
  if (key.size() < prefix.size() + 8 || key.substr(0, prefix.size()) != prefix) return false;
  if (key.size() != prefix.size() + 8 && !unit::is_part_key(key, prefix.size() + 8)) return false;
  value = IndexKeys::sorted_value(key.substr(prefix.size(), 8));
  return true;
}

ValueList PostingIndex::range(CountingStore& st, const std::vector<std::string>& labels, const std::string& prop,
                              int64_t lo, int64_t hi) const {
  ValueList out;
  RtParallel par(st);   // one walk per label, the labels are the plan's: their reads go together
  for (auto& label : labels) {
    par.member();
    const IndexDecl& ix = schema_.index_decl(label, prop);
    if (ix.kind != "sorted") throw std::runtime_error("read_index_range: " + label + "." + prop + " is not a sorted index");
    std::string prefix = IndexKeys::prefix(ix.id);
    // the terms a [lo, hi) window covers: [bucket(lo), bucket(hi-1)] -- exact under full; under
    // day the two boundary days whole (a SUPERSET: the plan's precise date filter trims it)
    auto [first, last] = date_bucket_window(lo, hi, date_granularity_);
    Cursor cur = st.seek(prefix + IndexKeys::sorted_bytes(first));   // 1 SEEK: the first term >= lo
    int64_t v;
    while (cur.valid() && sorted_landing(cur.key(), prefix, v) && v <= last) {
      uint64_t used = 0;
      for (uint64_t u : decode_landing(cur.value(), used)) out.push_back(Value::vid(u));
      st.count_used(landing_used_key(cur.key(), prefix.size() + 8), used);   // every in-window uid is output
      cur.next();                                             // 1 NEXT per term (or part)
    }
    end_scan(cur);                                            // the landing past the window
  }
  return out;
}

std::vector<IndexHit> PostingIndex::range_hits(CountingStore& st, const std::vector<std::string>& labels,
                                               const std::string& prop, int64_t lo, int64_t hi) const {
  std::vector<IndexHit> out;                 // the same walk as range, each vid with its term (bucket)
  RtParallel par(st);   // one walk per label, the labels are the plan's: their reads go together
  for (auto& label : labels) {
    par.member();
    const IndexDecl& ix = schema_.index_decl(label, prop);
    if (ix.kind != "sorted") throw std::runtime_error("index_range_buckets: " + label + "." + prop + " is not a sorted index");
    std::string prefix = IndexKeys::prefix(ix.id);
    auto [first, last] = date_bucket_window(lo, hi, date_granularity_);
    Cursor cur = st.seek(prefix + IndexKeys::sorted_bytes(first));
    int64_t v;
    while (cur.valid() && sorted_landing(cur.key(), prefix, v) && v <= last) {
      uint64_t used = 0;
      for (uint64_t u : decode_landing(cur.value(), used)) out.push_back({u, v, v, label});
      st.count_used(landing_used_key(cur.key(), prefix.size() + 8), used);
      cur.next();
    }
    end_scan(cur);
  }
  return out;
}

ValueList PostingIndex::enumerate(CountingStore& st, const std::string& label) const {
  ValueList out;
  enumerate_uids(st, label, [&](uint64_t u) { out.push_back(Value::vid(u)); });
  return out;
}

void PostingIndex::enumerate_uids(CountingStore& st, const std::string& label,
                                  const std::function<void(uint64_t)>& fn) const {
  if (schema_.has_type_index(label)) {                        // the type posting: ONE unit
    read_term(st, term_key(schema_.type_index_decl(label).id, ""), fn);
    return;
  }
  if (!schema_.prop_indexed(label))
    throw std::runtime_error("posting index: label '" + label + "' has neither a type index nor a required "
                             "property index to enumerate through");
  const std::string eprop = schema_.enumeration_prop(label);
  const IndexDecl& eix = schema_.index_decl(label, eprop);
  bool str_term = false;                                      // index_value_bytes' rule: a value index of a str column
  if (eix.kind != "sorted")
    for (auto& p : schema_.vertex(label).props) if (p.name == eprop) str_term = p.type == "str";
  std::string prefix = IndexKeys::prefix(eix.id);
  Cursor cur = st.seek(prefix);                               // every term of the enumeration prop
  while (cur.valid() && cur.key().size() >= prefix.size() && cur.key().substr(0, prefix.size()) == prefix) {
    uint64_t used = 0;
    for (uint64_t u : decode_landing(cur.value(), used)) fn(u);
    st.count_used(landing_used_key(cur.key(), term_len_at(cur.key(), prefix.size(), str_term)), used);
    cur.next();
  }
  end_scan(cur);
}

std::vector<IndexHit> PostingIndex::top_k_desc(CountingStore& st, const std::vector<std::string>& labels,
                                               const std::string& prop, int64_t k, std::optional<int64_t> hi,
                                               const UidSet* candidates, const TopKResolver& resolve) const {
  if (k < 1) throw std::runtime_error("read_top_k_by_index: k must be >= 1");
  struct Walker {
    std::string label;
    std::string prefix;
    Cursor cur;
  };
  std::vector<Walker> walkers;
  RtParallel par(st);   // one walk per label, the labels are the plan's: their reads go together
  for (auto& label : labels) {
    par.member();
    const IndexDecl& ix = schema_.index_decl(label, prop);
    if (ix.kind != "sorted") throw std::runtime_error("read_top_k_by_index: " + label + "." + prop + " is not a sorted index");
    std::string prefix = IndexKeys::prefix(ix.id);
    // hi is EXCLUSIVE: land on the last key of the newest term <= hi-1 (its last part, if split);
    // no hi: past every key of the index. hi at the type's minimum: nothing qualifies -- a target
    // that sorts before every key of this prefix (the bare prefix) leaves the walker off-prefix.
    std::string target = prefix;
    if (!hi) target += std::string(8 + unit::kPartSuffix, '\xff');
    else if (*hi > INT64_MIN) target += IndexKeys::sorted_bytes(date_bucket_of(*hi - 1, date_granularity_)) + std::string(unit::kPartSuffix, '\xff');
    walkers.push_back({label, prefix, st.seek_for_prev(target)});   // 1 SEEK per label
  }
  auto current = [](Walker& w, int64_t& value) {
    return w.cur.valid() && sorted_landing(w.cur.key(), w.prefix, value);
  };
  std::vector<IndexHit> hits;
  while (true) {
    int best = -1;
    int64_t best_value = 0;
    for (size_t i = 0; i < walkers.size(); i++) {
      int64_t v;
      if (current(walkers[i], v) && (best < 0 || v > best_value)) { best = int(i); best_value = v; }
    }
    if (best < 0) break;
    // ties at the k-th TERM kept: under day the whole k-th day, the superset an exact re-rank needs
    if (int64_t(hits.size()) >= k && best_value != hits.back().bucket) break;
    Walker& w = walkers[size_t(best)];
    uint64_t used = 0;
    for (uint64_t u : decode_landing(w.cur.value(), used)) {   // every uid of the term is a candidate
      if (candidates && !candidates->contains(u)) continue;
      int64_t value = best_value;
      if (resolve) {                                          // coarse term: the caller supplies the
        std::optional<int64_t> r = resolve(u, best_value, w.label);   // exact value, or drops the entry
        if (!r) continue;                                     // (past an exclusive hi on the boundary day)
        value = *r;
      }
      hits.push_back({u, value, best_value, w.label});
    }
    st.count_used(landing_used_key(w.cur.key(), w.prefix.size() + 8), used);
    w.cur.prev();                                             // 1 NEXT per term (or part) crossed
  }
  for (auto& w : walkers) w.cur.mark_miss();                  // rests unconsumed, or off its index
  return hits;
}

// --- ingest ------------------------------------------------------------------------------------

void PostingIndex::ingest_vertex(KvSink&, const std::string& label, uint64_t uid, const Value& props) {
  for (auto& t : terms_of(label, props)) buffered_[t].push_back(uid);
}

void PostingIndex::finalize(KvSink& sink) {
  int cur_id = -1;
  for (auto& [term, uids] : buffered_) {                      // map order == key order (id, then bytes)
    if (term.first != cur_id) {
      if (cur_id >= 0) sink.end_run();
      cur_id = term.first;
      sink.begin_run("index-" + std::to_string(cur_id));
    }
    std::sort(uids.begin(), uids.end());                      // encode_posting throws on a duplicate
    const std::vector<uint64_t>& u = uids;
    unit::write(sink, term_key(term.first, term.second), u.size(),
                [&](size_t lo, size_t hi) { return posting::encode_posting(std::vector<uint64_t>(u.begin() + lo, u.begin() + hi)); },
                chunk_target_, [&](size_t e) { return u[e]; });
  }
  if (cur_id >= 0) sink.end_run();
  buffered_.clear();
}

// --- writes: the unit RMW is shared infra (store/unit_posting.h); the index only names the terms --

void PostingIndex::w_insert(CountingStore& st, const std::string& label, uint64_t uid, const Value& props, IndexWrites w) {
  for (auto& t : terms_of(label, props)) unit::posting_add(st, term_key(t.first, t.second), uid, w.puts);
}

void PostingIndex::w_delete(CountingStore& st, const std::string& label, uint64_t uid, const Value& old_props, IndexWrites w) {
  for (auto& t : terms_of(label, old_props)) unit::posting_remove(st, term_key(t.first, t.second), uid, w.puts);
}

void PostingIndex::w_update(CountingStore& st, const std::string& label, uint64_t uid, const Value& old_row,
                            const Value& new_row, const std::string& prop, IndexWrites w) {
  const IndexDecl* ix = schema_.find_index(label, prop);
  if (!ix || ix->kind == "type") return;                      // an unindexed prop: no index change
  Value ov = old_row.get(prop), nv = new_row.get(prop);
  if (equals(ov, nv)) return;                                 // a same-value update moves no entry: 0 writes
  std::string ot = ov.is_null() ? "" : term_key(ix->id, index_value_bytes(schema_, *ix, label, ov, date_granularity_));
  std::string nt = nv.is_null() ? "" : term_key(ix->id, index_value_bytes(schema_, *ix, label, nv, date_granularity_));
  if (ot == nt) return;                                       // the same TERM (a date moved within its day
  if (!ov.is_null()) unit::posting_remove(st, ot, uid, w.puts);   // bucket): the posting is unchanged, 0 writes
  if (!nv.is_null()) unit::posting_add(st, nt, uid, w.puts);
}

}  // namespace evalcpp
