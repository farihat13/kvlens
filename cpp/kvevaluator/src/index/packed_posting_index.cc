#include "index/packed_posting_index.h"
#include "ingest/chunker.h"
#include "codec/decode_counts.h"

#include <algorithm>
#include <memory>
#include <stdexcept>

#include "codec/posting.h"
#include "codec/varint.h"
#include "index/date_buckets.h"
#include "index/sorted_index.h"   // IndexKeys
#include "store/unit_value.h"
#include "util/vid.h"

namespace evalcpp {

// --- value <-> chunk-src bookkeeping. The chunk key is prefix + IndexKeys::sorted_bytes(value) (8
// order-preserving bytes); the AdjChunk stores that same u64 as the group's `src`, so the chunk's
// ascending srcs == ascending values, and val_of_src recovers the exact value. ------------------------
static uint64_t src_of_bytes(std::string_view b8) {
  uint64_t u = 0;
  for (int i = 0; i < 8; i++) u = (u << 8) | uint8_t(b8[i]);
  return u;
}
static std::string bytes_of_src(uint64_t u) {
  std::string s(8, '\0');
  for (int i = 7; i >= 0; i--) { s[size_t(i)] = char(u & 0xff); u >>= 8; }
  return s;
}
static int64_t val_of_src(uint64_t u) { return IndexKeys::sorted_value(bytes_of_src(u)); }

static bool has_pfx(std::string_view key, const std::string& prefix) {
  return key.size() >= prefix.size() && key.substr(0, prefix.size()) == prefix;
}
// the used VALUE bytes of one value's vids == what the posting index charges for the same term: the
// posting-encoded per-uid widths. Derived from the OUTPUT vids (dsts) -- so right vids => right used.
static uint64_t posting_used_bytes(const std::vector<uint64_t>& sorted_vids) {
  if (sorted_vids.empty()) return 0;
  std::vector<uint8_t> w;
  posting::decode_posting_w(posting::encode_posting(sorted_vids), &w);
  uint64_t u = 0;
  for (uint8_t x : w) u += x;
  return u;
}

// --- the VALUE-index chunk: sorted (value_bytes -> posting) pairs. Values are index_value_bytes (vpack,
// variable-width), so a u64 AdjChunk slot cannot hold them -- this is the string counterpart of AdjChunk.
// Format: [n:uvarint] then n x [ vlen:uvarint | value_bytes | plen:uvarint | posting ]. ------------------
static std::string encode_value_chunk(const std::vector<std::pair<std::string, std::vector<uint64_t>>>& entries) {
  std::string out;
  put_uvarint(out, entries.size());
  for (auto& [val, vids] : entries) {
    put_uvarint(out, val.size());
    out.append(val);
    std::string p = posting::encode_posting(vids);
    put_uvarint(out, p.size());
    out.append(p);
  }
  return out;
}
// find `want` (exact value_bytes) in a value chunk; returns its vids + used = the matched value's posting
// widths (derived from the OUTPUT vids). found=false, used=0 if absent (a gap within the covering chunk).
static std::vector<uint64_t> value_chunk_find(std::string_view raw, std::string_view want, uint64_t& used, bool& found) {
  // codec/decode_counts.h: one chunk walked (its bytes once); the matched value's posting counts itself
  { DecodeCounts& d = decode_counts(); d.chunks++; d.bytes += raw.size(); }
  uint64_t bytes_after = decode_counts().bytes;
  size_t i = 0;
  uint64_t n = get_uvarint(raw, i);
  for (uint64_t e = 0; e < n; e++) {
    uint64_t vlen = get_uvarint(raw, i);
    std::string_view val = raw.substr(i, vlen); i += vlen;
    uint64_t plen = get_uvarint(raw, i);
    std::string_view post = raw.substr(i, plen); i += plen;
    if (val == want) {
      found = true;
      std::vector<uint8_t> w;
      std::vector<uint64_t> vids = posting::decode_posting_w(post, &w);
      decode_counts().bytes = bytes_after;        // the posting's bytes are inside the chunk's
      for (uint8_t x : w) used += x;
      return vids;
    }
  }
  found = false;
  return {};
}

PackedPostingIndex::PackedPostingIndex(const Schema& s, uint64_t chunk_target, DateGranularity g)
    : PostingIndex(s, chunk_target, g) {
  // pack the SORTED (u64 date -> AdjChunk) and VALUE (variable-width string -> value chunk) indexes;
  // the type index is one value (nothing to pack) and stays a plain posting (delegated to PostingIndex).
  for (auto& ix : s.indexes())
    if (ix.kind == "sorted" || ix.kind == "value") kind_by_id_[ix.id] = ix.kind;
}

// --- ingest: sorted -> AdjChunk chunks; value -> value chunks; type/unpacked -> plain posting. All
// greedy-close-after, one KV per chunk keyed by the first value's bytes. -------------------------------
void PackedPostingIndex::finalize(KvSink& sink) {
  int cur_id = -1;
  std::string cur_kind;                                 // "" = posting (type/unpacked), "sorted", "value"
  std::string prefix, first_bytes;
  std::vector<AdjGroup> spack;                          // sorted pack (AdjChunk: u64 date -> vids)
  std::vector<std::pair<std::string, std::vector<uint64_t>>> vpack;   // value pack (value chunk: str -> vids)
  GreedyChunker chunk(chunk_target_);
  auto flush = [&]() {
    if (cur_kind == "sorted" && !spack.empty()) sink.put(prefix + first_bytes, encode_adj(cc_, spack, false));
    else if (cur_kind == "value" && !vpack.empty()) sink.put(prefix + first_bytes, encode_value_chunk(vpack));
    spack.clear(); vpack.clear(); chunk.reset();
  };
  for (auto& [term, uids_ref] : buffered_) {   // map order == key order (id, then value bytes)
    std::vector<uint64_t>& uids = uids_ref;
    if (term.first != cur_id) {
      flush();
      if (cur_id >= 0) sink.end_run();
      cur_id = term.first;
      auto it = kind_by_id_.find(cur_id);
      cur_kind = it != kind_by_id_.end() ? it->second : "";
      prefix = IndexKeys::prefix(cur_id);
      sink.begin_run("index-" + std::to_string(cur_id));
    }
    std::sort(uids.begin(), uids.end());
    if (cur_kind == "sorted") {
      if (spack.empty()) first_bytes = term.second;      // this chunk's key value = its first term's bytes
      AdjGroup g;
      g.src = src_of_bytes(term.second);
      g.dsts = uids;
      spack.push_back(std::move(g));
      if (chunk.add(est_adj(uids.size(), false))) flush();   // greedy-close-after
    } else if (cur_kind == "value") {
      if (vpack.empty()) first_bytes = term.second;
      vpack.emplace_back(term.second, uids);
      if (chunk.add(term.second.size() + 3 + uids.size())) flush();   // rough: value bytes + framing + ~1B/vid
    } else {                                             // type index / unpacked: plain posting
      // NEVER-split (not chunk_target_): a packed index is owners:{pack} + split:never, so the delegated type
      // posting must honor split:never too. chunk_target_ is the PACK target for the sorted/value chunks above,
      // NOT a split target for the type posting -- splitting it at pack made the packed store internally
      // inconsistent (type posting split at 65536 while a value's own group never splits), which no single-split
      // baseline could match at scale. One un-split type posting = split:never honored.
      const std::vector<uint64_t>& u = uids;
      unit::write(sink, term_key(cur_id, term.second), u.size(),
                  [&](size_t lo, size_t hi) { return posting::encode_posting(std::vector<uint64_t>(u.begin() + lo, u.begin() + hi)); },
                  UINT64_MAX, [&](size_t e) { return u[e]; });
    }
  }
  flush();
  if (cur_id >= 0) sink.end_run();
  buffered_.clear();
}

// --- reads: walk CHUNKS, direct cursor counting (S=1 seek + N next per chunk). The type index is not
// packed, so enumerate/enumerate_uids fall through to PostingIndex. -------------------------------------

ValueList PackedPostingIndex::eq(CountingStore& st, const std::string& label, const std::string& prop, const Value& value) const {
  const IndexDecl& ix = schema_.index_decl(label, prop);
  if (!is_packed(ix.id)) return PostingIndex::eq(st, label, prop, value);   // type / unpacked -> base
  std::string prefix = IndexKeys::prefix(ix.id);
  if (ix.kind == "value") {                            // string value index: the value chunk
    std::string vb = index_value_bytes(schema_, ix, label, value, date_granularity_);
    Cursor cur = st.seek_for_prev(prefix + vb);         // the chunk with first value <= vb; find exact within
    ValueList out;
    if (cur.valid() && has_pfx(cur.key(), prefix)) {
      uint64_t used = 0; bool found = false;
      for (uint64_t u : value_chunk_find(cur.value(), vb, used, found)) out.push_back(Value::vid(u));
      st.count_used(cur.key().size(), used);
    } else {
      cur.mark_miss();
    }
    return out;
  }
  if (date_granularity_ != DateGranularity::full)      // a coarse (day) sorted eq is ambiguous -> base throws
    return PostingIndex::eq(st, label, prop, value);
  int64_t b = value.as_int();                          // full sorted: the bucket IS the value
  Cursor cur = st.seek_for_prev(prefix + IndexKeys::sorted_bytes(b));
  ValueList out;
  if (cur.valid() && has_pfx(cur.key(), prefix)) {
    std::shared_ptr<AdjChunk> chunk = AdjChunk::decode(&cc_, std::string(cur.value()), false);
    int64_t r = chunk->find(src_of_bytes(IndexKeys::sorted_bytes(b)));   // exact value in this chunk
    uint64_t used = 0;
    if (r >= 0) {
      std::vector<uint64_t> d = chunk->dsts(size_t(r));
      for (uint64_t u : d) out.push_back(Value::vid(u));
      used = posting_used_bytes(d);
    }
    st.count_used(cur.key().size(), used);
  } else {
    cur.mark_miss();
  }
  return out;
}

// a cursor at the chunk covering `first` under `prefix` -- ONE physical SEEK (seek_for_prev), matching
// the posting index's S=1. On undershoot (`first` below all this index's chunks) the seek lands off-prefix;
// we recover with a NEXT to the first chunk (NOT a 2nd seek, which would double S). The caller checks
// cur.valid() && has_pfx. (A rare residual: on undershoot the off-prefix landing's foreign key is charged
// to scan_key -- a few bytes on a path the LDBC ranges don't hit; a full fix needs a forward-seekable key.)
static Cursor seek_covering(CountingStore& st, const std::string& prefix, int64_t first) {
  Cursor cur = st.seek_for_prev(prefix + IndexKeys::sorted_bytes(first));
  if (cur.valid() && has_pfx(cur.key(), prefix)) return cur;
  if (cur.valid()) cur.next();                         // undershoot: the first chunk is the next key (an N, not a SEEK)
  return cur;
}

ValueList PackedPostingIndex::range(CountingStore& st, const std::vector<std::string>& labels, const std::string& prop,
                                    int64_t lo, int64_t hi) const {
  ValueList out;
  RtParallel par(st);   // one walk per label, the labels are the plan's: their reads go together
  for (auto& label : labels) {
    par.member();
    const IndexDecl& ix = schema_.index_decl(label, prop);
    if (ix.kind != "sorted") throw std::runtime_error("read_index_range: " + label + "." + prop + " is not a sorted index");
    std::string prefix = IndexKeys::prefix(ix.id);
    auto [first, last] = date_bucket_window(lo, hi, date_granularity_);
    Cursor cur = seek_covering(st, prefix, first);
    bool past = false;
    while (!past && cur.valid() && has_pfx(cur.key(), prefix)) {
      std::shared_ptr<AdjChunk> chunk = AdjChunk::decode(&cc_, std::string(cur.value()), false);
      uint64_t used = 0;
      for (size_t i = 0; i < chunk->size(); i++) {
        int64_t v = val_of_src(chunk->src(i));
        if (v < first) continue;
        if (v > last) { past = true; break; }
        std::vector<uint64_t> d = chunk->dsts(i);
        for (uint64_t u : d) out.push_back(Value::vid(u));
        used += posting_used_bytes(d);
      }
      st.count_used(cur.key().size(), used);            // 1 chunk key + the in-window vids' bytes
      if (!past) cur.next();                            // 1 NEXT per further chunk
    }
    if (!past) cur.mark_miss();                         // walked off the end = a miss; the `past` chunk is a hit
  }
  return out;
}

std::vector<IndexHit> PackedPostingIndex::range_hits(CountingStore& st, const std::vector<std::string>& labels,
                                                     const std::string& prop, int64_t lo, int64_t hi) const {
  std::vector<IndexHit> out;
  RtParallel par(st);   // one walk per label, the labels are the plan's: their reads go together
  for (auto& label : labels) {
    par.member();
    const IndexDecl& ix = schema_.index_decl(label, prop);
    if (ix.kind != "sorted") throw std::runtime_error("index_range_buckets: " + label + "." + prop + " is not a sorted index");
    std::string prefix = IndexKeys::prefix(ix.id);
    auto [first, last] = date_bucket_window(lo, hi, date_granularity_);
    Cursor cur = seek_covering(st, prefix, first);
    bool past = false;
    while (!past && cur.valid() && has_pfx(cur.key(), prefix)) {
      std::shared_ptr<AdjChunk> chunk = AdjChunk::decode(&cc_, std::string(cur.value()), false);
      uint64_t used = 0;
      for (size_t i = 0; i < chunk->size(); i++) {
        int64_t v = val_of_src(chunk->src(i));
        if (v < first) continue;
        if (v > last) { past = true; break; }
        std::vector<uint64_t> d = chunk->dsts(i);
        for (uint64_t u : d) out.push_back({u, v, v, label});
        used += posting_used_bytes(d);
      }
      st.count_used(cur.key().size(), used);
      if (!past) cur.next();
    }
    if (!past) cur.mark_miss();
  }
  return out;
}

std::vector<IndexHit> PackedPostingIndex::top_k_desc(CountingStore& st, const std::vector<std::string>& labels,
                                                     const std::string& prop, int64_t k, std::optional<int64_t> hi,
                                                     const UidSet* candidates, const TopKResolver& resolve) const {
  if (k < 1) throw std::runtime_error("read_top_k_by_index: k must be >= 1");
  struct Walker {
    std::string label, prefix;
    Cursor cur;
    std::shared_ptr<AdjChunk> chunk;
    int gi = -1;   // current group (descending); -1 = advance to the previous chunk / done
  };
  // decode the walker's current chunk and set gi to the newest group with value <= cap (or size-1)
  auto load = [&](Walker& w, std::optional<int64_t> cap) {
    if (!w.cur.valid() || !has_pfx(w.cur.key(), w.prefix)) { w.chunk = nullptr; w.gi = -1; return; }
    w.chunk = AdjChunk::decode(&cc_, std::string(w.cur.value()), false);
    st.count_used(w.cur.key().size(), 0);              // the chunk key, once per chunk decoded
    w.gi = int(w.chunk->size()) - 1;
    while (cap && w.gi >= 0 && val_of_src(w.chunk->src(size_t(w.gi))) > *cap) w.gi--;
  };
  std::vector<Walker> walkers;
  walkers.reserve(labels.size());
  RtParallel par(st);   // one walk per label, the labels are the plan's: their reads go together
  for (auto& label : labels) {
    par.member();
    const IndexDecl& ix = schema_.index_decl(label, prop);
    if (ix.kind != "sorted") throw std::runtime_error("read_top_k_by_index: " + label + "." + prop + " is not a sorted index");
    std::string prefix = IndexKeys::prefix(ix.id);
    // hi is EXCLUSIVE; the newest bucket to consider = bucket(hi-1). full => hi-1; day => the day of hi-1
    // (the resolver drops boundary-day entries whose exact ms is >= hi).
    int64_t cap = hi ? date_bucket_of(*hi - 1, date_granularity_) : INT64_MAX;
    walkers.push_back(Walker{label, prefix, st.seek_for_prev(prefix + IndexKeys::sorted_bytes(cap)), nullptr, -1});  // 1 SEEK/label
    load(walkers.back(), hi ? std::optional<int64_t>(cap) : std::nullopt);
  }
  std::vector<IndexHit> hits;
  while (true) {
    int best = -1;
    int64_t best_value = 0;
    for (size_t i = 0; i < walkers.size(); i++) {
      if (walkers[i].gi < 0) continue;
      int64_t v = val_of_src(walkers[i].chunk->src(size_t(walkers[i].gi)));
      if (best < 0 || v > best_value) { best = int(i); best_value = v; }
    }
    if (best < 0) break;
    if (int64_t(hits.size()) >= k && best_value != hits.back().bucket) break;   // ties at the k-th kept
    Walker& w = walkers[size_t(best)];
    std::vector<uint64_t> d = w.chunk->dsts(size_t(w.gi));
    for (uint64_t u : d) {
      if (candidates && !candidates->contains(u)) continue;
      int64_t value = best_value;                      // full: the bucket IS the exact value
      if (resolve) {                                   // day: resolve the exact value from the vertex, or drop
        std::optional<int64_t> r = resolve(u, best_value, w.label);   // (a boundary-day entry past the exclusive hi)
        if (!r) continue;
        value = *r;
      }
      hits.push_back({u, value, best_value, w.label});
    }
    st.count_used(0, posting_used_bytes(d));           // the group's vids (key charged once at decode)
    w.gi--;
    if (w.gi < 0) {                                    // this chunk exhausted: to the previous chunk
      w.cur.prev();                                    // 1 NEXT per further chunk
      load(w, std::nullopt);
    }
  }
  // only a walker that WALKED OFF its index (chunk==nullptr, cursor on a miss position) records the
  // boundary miss; one resting on a partly-consumed chunk read a value (a hit), so leave it.
  for (auto& w : walkers) if (!w.chunk) w.cur.mark_miss();
  return hits;
}

// --- writes: a chunked index has no per-term posting to read-modify-write; fail loud (the inherited
// PostingIndex versions would write orphaned postings at term keys and desync the store). ------------
static void no_packed_writes() {
  throw std::runtime_error("meta packed index (owners:{pack}): index writes are not built "
                           "(a chunked index has no per-term posting to RMW)");
}
void PackedPostingIndex::w_insert(CountingStore&, const std::string&, uint64_t, const Value&, IndexWrites) { no_packed_writes(); }
void PackedPostingIndex::w_delete(CountingStore&, const std::string&, uint64_t, const Value&, IndexWrites) { no_packed_writes(); }
void PackedPostingIndex::w_update(CountingStore&, const std::string&, uint64_t, const Value&, const Value&,
                                  const std::string&, IndexWrites) { no_packed_writes(); }

}  // namespace evalcpp
