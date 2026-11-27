#include <algorithm>
#include "index/sorted_index.h"

#include <stdexcept>

#include "formats/format.h"

#include "codec/varint.h"
#include "util/vid.h"

namespace evalcpp {

std::string IndexKeys::prefix(int index_id) {
  std::string k = "i";
  put_u32be(k, uint32_t(index_id));
  return k;
}

std::string IndexKeys::sorted_bytes(int64_t v) {
  std::string k;
  put_u64be(k, uint64_t(v) ^ (uint64_t(1) << 63));
  return k;
}

int64_t IndexKeys::sorted_value(std::string_view b) {
  return int64_t(get_u64be(b, 0) ^ (uint64_t(1) << 63));
}

std::string IndexKeys::vpack_str_bytes(const std::string& s) {
  std::string k;
  if (s.size() <= 126) {
    k.push_back(char(0x40 + s.size()));
  } else {
    k.push_back(char(0xbf));
    uint64_t n = s.size();
    for (int i = 0; i < 8; i++) k.push_back(char((n >> (8 * i)) & 0xff));   // VPack: little-endian
  }
  k += s;
  return k;
}

std::string IndexKeys::key(const std::string& prefix, std::string_view value_bytes, uint64_t vid) {
  std::string k = prefix;
  k.append(value_bytes.data(), value_bytes.size());
  put_u64be(k, vid);
  return k;
}

const std::string& IndexKeys::refuse_null_slot(const std::string& term, const std::string& what) {
  if (is_null_sorted(term)) throw std::runtime_error(what + ": the value INT64_MIN is the null slot of a sorted index term");
  return term;
}

uint64_t IndexKeys::vid_of(std::string_view key) { return get_u64be(key, key.size() - 8); }
uint32_t IndexKeys::index_id_of(std::string_view key) { return get_u32be(key, 1); }

std::string index_null_bytes(const Schema& schema, const IndexDecl& ix, const std::string& label) {
  if (ix.kind != "sorted")
    for (auto& p : schema.vertex(label).props)
      if (p.name == ix.prop && p.type == "str") return std::string(1, IndexKeys::kNullStr);
  return std::string(8, '\0');
}

std::string index_value_bytes(const Schema& schema, const IndexDecl& ix, const std::string& label, const Value& v,
                              DateGranularity date_granularity) {
  // a sorted index is a date column's: its term is the timestamp's BUCKET at the index's
  // granularity (full = the ms itself), a fixed-width order-preserving int
  if (ix.kind == "sorted") return IndexKeys::sorted_bytes(date_bucket_of(v.as_int(), date_granularity));
  // a value index: the column's declared type decides the bytes
  std::string type;
  for (auto& p : schema.vertex(label).props)
    if (p.name == ix.prop) type = p.type;
  if (type == "str") {
    // a vid is its hex16 str (a named-codec document decodes a 16-hex str as one): the same term as ingest's str
    std::string s = v.is_text() ? v.text() : (v.is_int() ? std::to_string(v.as_int()) : repr(v));
    return IndexKeys::vpack_str_bytes(s);   // ingest and query encode identically
  }
  return IndexKeys::sorted_bytes(v.as_int());
}

std::string SortedIndex::value_bytes(const IndexDecl& ix, const std::string& label, const Value& v, bool) const {
  return index_value_bytes(schema_, ix, label, v);
}

std::vector<std::string> SortedIndex::vertex_keys(const std::string& label, uint64_t uid, const Value& props) const {
  std::vector<std::string> keys;
  if (!schema_.prop_indexed(label))
    keys.push_back(IndexKeys::key(IndexKeys::prefix(schema_.type_index_decl(label).id), "", uid));
  for (auto& kv : props.as_row()) {
    const IndexDecl* ix = schema_.find_index(label, kv.first);
    if (!ix || ix->kind == "type") continue;
    keys.push_back(IndexKeys::key(IndexKeys::prefix(ix->id),
                                  IndexKeys::refuse_null_slot(value_bytes(*ix, label, kv.second, false), "ingest " + label + "." + kv.first),
                                  uid));
  }
  return keys;
}

std::string SortedIndex::write_key(const std::string& label, const std::string& prop, const Value& val, uint64_t uid) const {
  const IndexDecl& ix = schema_.index_decl(label, prop);
  return IndexKeys::key(IndexKeys::prefix(ix.id),
                        val.is_null() ? index_null_bytes(schema_, ix, label)
                                      : IndexKeys::refuse_null_slot(value_bytes(ix, label, val, false), "write " + label + "." + prop),
                        uid);
}

std::vector<std::string> SortedIndex::vertex_write_keys(const std::string& label, uint64_t uid, const Value& props) const {
  std::vector<std::string> keys;
  if (!schema_.prop_indexed(label))
    keys.push_back(IndexKeys::key(IndexKeys::prefix(schema_.type_index_decl(label).id), "", uid));
  for (auto& prop : schema_.index_cols_of(label))
    keys.push_back(write_key(label, prop, props.get(prop), uid));
  return keys;
}

ValueList SortedIndex::eq(CountingStore& st, const std::string& label, const std::string& prop, const Value& value) const {
  const IndexDecl& ix = schema_.index_decl(label, prop);
  std::string prefix = IndexKeys::prefix(ix.id) + value_bytes(ix, label, value, true);
  ValueList out;
  Cursor cur = st.seek(prefix);
  while (cur.valid() && cur.key().size() >= prefix.size() && cur.key().substr(0, prefix.size()) == prefix) {
    st.count_used(cur.key().size(), 0);   // every emitted entry rides its key; values empty
    out.push_back(Value::vid(IndexKeys::vid_of(cur.key())));
    cur.next();
  }
  cur.mark_miss();                        // the landing that ended the scan: an absence miss
  return out;
}

std::vector<IndexHit> SortedIndex::range_hits(CountingStore& st, const std::vector<std::string>& labels,
                                              const std::string& prop, int64_t lo, int64_t hi) const {
  std::vector<IndexHit> out;                // the same walk as range, each entry with its key's value
  RtParallel par(st);   // one walk per label, the labels are the plan's: their reads go together
  for (auto& label : labels) {
    par.member();
    const IndexDecl& ix = schema_.index_decl(label, prop);
    if (ix.kind != "sorted") throw std::runtime_error("index_range_buckets: " + label + "." + prop + " is not a sorted index");
    std::string prefix = IndexKeys::prefix(ix.id);
    Cursor cur = st.seek(prefix + IndexKeys::sorted_bytes(std::max(lo, INT64_MIN + 1)));   // past the null block
    while (cur.valid() && cur.key().size() == prefix.size() + 16 && cur.key().substr(0, prefix.size()) == prefix &&
           IndexKeys::sorted_value(cur.key().substr(prefix.size(), 8)) < hi) {
      int64_t v = IndexKeys::sorted_value(cur.key().substr(prefix.size(), 8));
      st.count_used(cur.key().size(), 0);
      out.push_back({IndexKeys::vid_of(cur.key()), v, v, label});
      cur.next();
    }
    cur.mark_miss();
  }
  return out;
}

ValueList SortedIndex::range(CountingStore& st, const std::vector<std::string>& labels, const std::string& prop,
                             int64_t lo, int64_t hi) const {
  ValueList out;
  RtParallel par(st);   // one walk per label, the labels are the plan's: their reads go together
  for (auto& label : labels) {
    par.member();
    const IndexDecl& ix = schema_.index_decl(label, prop);
    if (ix.kind != "sorted") throw std::runtime_error("read_index_range: " + label + "." + prop + " is not a sorted index");
    std::string prefix = IndexKeys::prefix(ix.id);
    Cursor cur = st.seek(prefix + IndexKeys::sorted_bytes(std::max(lo, INT64_MIN + 1)));   // past the null block
    while (cur.valid() && cur.key().size() == prefix.size() + 16 && cur.key().substr(0, prefix.size()) == prefix &&
           IndexKeys::sorted_value(cur.key().substr(prefix.size(), 8)) < hi) {
      st.count_used(cur.key().size(), 0);   // every in-window entry is output
      out.push_back(Value::vid(IndexKeys::vid_of(cur.key())));
      cur.next();
    }
    cur.mark_miss();                        // the landing past the window: an absence miss
  }
  return out;
}

ValueList SortedIndex::enumerate(CountingStore& st, const std::string& label) const {
  ValueList out;
  enumerate_uids(st, label, [&](uint64_t u) { out.push_back(Value::vid(u)); });
  return out;
}

void SortedIndex::enumerate_uids(CountingStore& st, const std::string& label,
                                 const std::function<void(uint64_t)>& fn) const {
  std::string prefix = schema_.prop_indexed(label)
                           ? IndexKeys::prefix(schema_.index_decl(label, schema_.enumeration_prop(label)).id)
                           : IndexKeys::prefix(schema_.type_index_decl(label).id);
  Cursor cur = st.seek(prefix);
  while (cur.valid() && cur.key().size() >= prefix.size() && cur.key().substr(0, prefix.size()) == prefix) {
    st.count_used(cur.key().size(), 0);   // every member entry is output
    fn(IndexKeys::vid_of(cur.key()));
    cur.next();
  }
  cur.mark_miss();                        // the landing that ended the scan: an absence miss
}

std::vector<SortedIndex::Hit> SortedIndex::top_k_desc(CountingStore& st, const std::vector<std::string>& labels,
                                                      const std::string& prop, int64_t k, std::optional<int64_t> hi,
                                                      const UidSet* candidates, const TopKResolver& resolve) const {
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
    // "~" past all digits = the largest sortable value; hi is exclusive (an entry AT hi extends
    // the 4-tuple and sorts after it, so seek_for_prev lands strictly below)
    // no hi: a bound past every key of this index (value + vid all 0xff), so INT64_MAX is included
    std::string bound = hi ? IndexKeys::sorted_bytes(*hi) : std::string(16, '\xff');
    walkers.push_back({label, prefix, st.seek_for_prev(prefix + bound)});
  }
  auto current = [](Walker& w, int64_t& value) {
    if (!w.cur.valid()) return false;
    std::string_view key = w.cur.key();
    if (key.size() != w.prefix.size() + 16 || key.substr(0, w.prefix.size()) != w.prefix) return false;
    if (IndexKeys::is_null_sorted(key.substr(w.prefix.size(), 8))) return false;   // the null block ends a downward walk
    value = IndexKeys::sorted_value(key.substr(w.prefix.size(), 8));
    return true;
  };
  if (k < 1) throw std::runtime_error("read_top_k_by_index: k must be >= 1");
  std::vector<Hit> hits;
  while (true) {
    int best = -1;
    int64_t best_value = 0;
    for (size_t i = 0; i < walkers.size(); i++) {
      int64_t v;
      if (current(walkers[i], v) && (best < 0 || v > best_value)) { best = int(i); best_value = v; }
    }
    if (best < 0) break;
    if (int64_t(hits.size()) >= k && best_value != hits.back().bucket) break;
    Walker& w = walkers[size_t(best)];
    // RULED: every crossed entry is inspected (ordering + candidacy decisions) -> USED
    st.count_used(w.cur.key().size(), 0);
    uint64_t uid = IndexKeys::vid_of(w.cur.key());               // probe in uid space
    if (!candidates || candidates->contains(uid)) {
      int64_t value = best_value;                             // exact: one entry per (value, vid)
      if (resolve) {                                          // a caller-side check (e.g. an exclusive hi)
        std::optional<int64_t> r = resolve(uid, best_value, w.label);
        if (!r) { w.cur.prev(); continue; }
        value = *r;
      }
      hits.push_back({uid, value, best_value, w.label});
    }
    w.cur.prev();
  }
  // every walker rests on an unconsumed entry (a k-stop) or off its index: an absence miss
  for (auto& w : walkers) w.cur.mark_miss();
  return hits;
}

void SortedIndex::ingest_vertex(KvSink& sink, const std::string& label, uint64_t uid, const Value& props) {
  for (auto& k : vertex_keys(label, uid, props)) sink.put_index(int(IndexKeys::index_id_of(k)), k);
}

void SortedIndex::w_insert(CountingStore&, const std::string& label, uint64_t uid, const Value& props, IndexWrites w) {
  for (auto& k : vertex_write_keys(label, uid, props)) w.puts.emplace_back(k, "");
}

void SortedIndex::w_delete(CountingStore&, const std::string& label, uint64_t uid, const Value& old_props, IndexWrites w) {
  for (auto& k : vertex_write_keys(label, uid, old_props)) w.dels.push_back(k);
}

void SortedIndex::w_update(CountingStore&, const std::string& label, uint64_t uid, const Value& old_row,
                           const Value& new_row, const std::string& prop, IndexWrites w) {
  // only the CHANGED column's entry moves (PostingIndex's rule; the cheapest the layout allows). Re-writing
  // every index column on any update is NebulaGraph's engine behavior, modeled only by Python's nebula_engine;
  // the hand-written nebula format moves only the changed columns too (formats/nebula/writes.cc). Needs only
  // the changed column's old value, so a caller that read just that column (singleton props) is correct.
  std::vector<std::string> cols = schema_.index_cols_of(label);
  if (std::find(cols.begin(), cols.end(), prop) == cols.end()) return;   // an unindexed prop: no index change
  std::string ok = write_key(label, prop, old_row.get(prop), uid), nk = write_key(label, prop, new_row.get(prop), uid);
  if (ok == nk) return;                                                   // the same token: nothing moves
  w.dels.push_back(std::move(ok));
  w.puts.emplace_back(std::move(nk), "");
}

}  // namespace evalcpp
