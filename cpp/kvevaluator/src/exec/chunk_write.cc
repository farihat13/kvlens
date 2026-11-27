#include "exec/chunk_write.h"

#include <algorithm>
#include <memory>

#include "codec/lists.h"        // kind 3: meta-v4's inline-props adjacency chunk
#include "exec/chunk_cache.h"   // chunk_with_id / chunk_has_prefix: the chunk key shape

namespace evalcpp {

std::pair<std::string, ChunkWriteReader::Chunk*> ChunkWriteReader::locate(
    const std::string& prefix, uint64_t target, uint64_t id, int kind, const std::vector<ColDecl>* cols, bool with_ords,
    unsigned label_bits) {
  for (auto& [k, c] : cache_)
    if (chunk_has_prefix(k, prefix) && c.lo <= id && id <= c.hi) return {k, &c};
  Cursor cur = st_.seek_for_prev(chunk_with_id(prefix, id));      // 1 SEEK, charged
  if (!cur.valid() || !chunk_has_prefix(cur.key(), prefix)) { cur.mark_miss(); return {"", nullptr}; }
  std::string key(cur.key());
  auto it = cache_.find(key);
  if (it != cache_.end()) return {key, &it->second};
  std::string_view raw = cur.value();                                       // read once: the charged value bytes
  Chunk c = decode(cc_, kind, cols, with_ords, raw, label_bits);             // the whole chunk, charged
  c.stored_bytes = raw.size();
  // the last-chunk lane: is this landing the family's final key? Asked of the store (one uncounted, audited seek),
  // never of the id. Why a last-chunk touch is kept apart: python/kvcost/layout_cost/compose.py chunk_view
  if (st_.kvpair_stats()) {
    std::optional<std::string> fin = st_.raw_last_key(prefix, chunk_with_id(prefix, ~0ull));
    if (fin && *fin == key) {
      c.is_last = true;
      c.target = target;
      st_.note_last_chunk_read(key, c.stored_bytes, target);
    }
  }
  auto jt = cache_.emplace(std::move(key), std::move(c)).first;
  return {jt->first, &jt->second};
}

ChunkWriteReader::Chunk ChunkWriteReader::decode(const ChunkCodecs& cc, int kind, const std::vector<ColDecl>* cols,
                                                 bool with_ords, std::string_view raw, unsigned label_bits) {
  Chunk c;
  c.kind = kind;
  if (kind == 3) {
    c.label_bits = label_bits;
    lists::ChunkView cv(cols, std::string(raw), label_bits, with_ords);
    std::vector<uint64_t> srcs = cv.owners();   // one pass (src(i) per owner would walk up to 15 gaps each)
    for (size_t i = 0; i < cv.size(); i++) {
      AdjG g{srcs[i], cv.dsts(i), {}, {}, {}};
      if (with_ords)
        for (size_t j = 0; j < g.dsts.size(); j++) g.ords.push_back(cv.ordinal(i, j));
      if (!cols->empty())
        for (size_t j = 0; j < g.dsts.size(); j++) g.props.push_back(cv.props_row(i, j));
      if (label_bits)
        for (size_t j = 0; j < g.dsts.size(); j++) g.labels.push_back(cv.label(i, j));
      c.groups.push_back(std::move(g));
    }
  } else if (kind == 1 || kind == 4) {
    if (kind == 4) c.label_bits = label_bits;
    std::shared_ptr<AdjChunk> ac = AdjChunk::decode(&cc, std::string(raw), with_ords, c.label_bits);
    for (size_t i = 0; i < ac->size(); i++) {
      AdjG g{ac->src(i), ac->dsts(i), {}, {}};
      if (with_ords) g.ords = ac->ordinals(i);
      if (c.label_bits) g.labels = ac->labels(i);
      c.groups.push_back(std::move(g));
    }
  } else {
    std::shared_ptr<PropsChunk> pc = PropsChunk::decode(&cc, cols, std::string(raw));
    for (size_t r = 0; r < pc->size(); r++) c.rows.emplace_back(pc->ids()[r], pc->row(r));
  }
  if (!c.empty()) { c.lo = c.first(); c.hi = c.last(); }
  return c;
}

std::string serialize_chunk(const ChunkCodecs& cc, const std::vector<ColDecl>* cols,
                            const ChunkWriteReader::Chunk& c, bool with_ords) {
  if (c.kind == 3) {
    std::vector<lists::ChunkOwner> owners;
    std::vector<PropRow> pr;
    for (auto& g : c.groups) {
      owners.push_back({g.src, g.dsts, g.labels});
      for (auto& p : g.props) pr.push_back({g.src, &p});
    }
    if (with_ords) {   // own placement: the ordinals block
      std::vector<uint64_t> ords;
      for (auto& g : c.groups) ords.insert(ords.end(), g.ords.begin(), g.ords.end());
      return lists::encode_chunk(owners, c.label_bits, *cols, nullptr, &ords);
    }
    if (cols->empty()) return lists::encode_chunk(owners, c.label_bits, *cols, nullptr);
    ValueRows rows(*cols, pr);
    return lists::encode_chunk(owners, c.label_bits, *cols, &rows);
  }
  if (c.kind == 1 || c.kind == 4) {
    std::vector<AdjGroup> gs;
    for (auto& g : c.groups) gs.push_back({g.src, g.dsts, g.ords, c.label_bits ? g.labels : std::vector<uint8_t>{}});
    return encode_adj(cc, gs, with_ords, c.kind == 4 ? c.label_bits : 0);
  }
  std::vector<PropRow> pr;
  for (auto& [k, v] : c.rows) pr.push_back({k, &v});
  ValueRows rows(*cols, pr);
  return c.kind == 0 ? encode_props(cc, *cols, rows) : encode_ep(cc, *cols, rows);
}

void chunk_rmw(const ChunkCodecs& cc, ChunkWriteReader& rd, ChunkWrites& w, const std::string& prefix, uint64_t target,
               uint64_t id, int kind, const std::vector<ColDecl>* cols, bool with_ords,
               const std::function<void(ChunkWriteReader::Chunk&)>& mutate, unsigned label_bits) {
  auto [key, cp] = rd.locate(prefix, target, id, kind, cols, with_ords, label_bits);
  ChunkWriteReader::Chunk c;
  c.kind = kind;
  if (kind == 3 || kind == 4) c.label_bits = label_bits;   // a new chunk (no landing) stores labels at the family's width
  if (cp) c = *cp;
  mutate(c);
  if (c.adj()) std::stable_sort(c.groups.begin(), c.groups.end(), [](auto& a, auto& b) { return a.src < b.src; });
  else std::stable_sort(c.rows.begin(), c.rows.end(), [](auto& a, auto& b) { return a.first < b.first; });
  if (!key.empty()) rd.forget(key);
  if (!c.empty()) {
    std::string newkey = chunk_with_id(prefix, c.first());
    if (!key.empty() && key != newkey) w.drop_chunk(key);   // the first owner went: the chunk moves to its new first owner's key
    std::string value = serialize_chunk(cc, cols, c, with_ords);
    ChunkWrites::LastPut lp{0, c.stored_bytes, c.target};   // the family's last chunk: its bytes before this rewrite
    uint64_t put_bytes = value.size();
    w.put_chunk(newkey, std::move(value), c.is_last ? &lp : nullptr);
    c.stored_bytes = put_bytes;                              // a second rewrite in this op starts from this put
    c.lo = c.first();
    c.hi = c.last();
    rd.remember(std::move(newkey), std::move(c));
  } else if (!key.empty()) {               // emptied: the key goes
    w.drop_chunk(key);
  }
}

void chunk_upsert(std::vector<std::pair<uint64_t, Value>>& rows, uint64_t key, Value value) {
  for (auto& r : rows)
    if (r.first == key) { r.second = std::move(value); return; }
  rows.emplace_back(key, std::move(value));
}
void chunk_drop_row(std::vector<std::pair<uint64_t, Value>>& rows, uint64_t key) {
  rows.erase(std::remove_if(rows.begin(), rows.end(), [&](auto& r) { return r.first == key; }), rows.end());
}
void chunk_adj_add(std::vector<ChunkWriteReader::AdjG>& groups, uint64_t src, uint64_t dst, uint64_t ordinal,
                   std::optional<uint8_t> label) {
  for (auto& g : groups) {
    if (g.src != src) continue;
    if (std::find(g.dsts.begin(), g.dsts.end(), dst) != g.dsts.end()) return;
    size_t pos = size_t(std::count_if(g.dsts.begin(), g.dsts.end(), [&](uint64_t d) { return d < dst; }));
    g.dsts.insert(g.dsts.begin() + pos, dst);
    if (!g.ords.empty() || (g.dsts.size() == 1 && ordinal != kNoOrdinal))
      g.ords.insert(g.ords.begin() + pos, ordinal);
    if (label) g.labels.insert(g.labels.begin() + pos, *label);
    return;
  }
  ChunkWriteReader::AdjG g{src, {dst}, {}, {}};
  if (ordinal != kNoOrdinal) g.ords.push_back(ordinal);
  if (label) g.labels.push_back(*label);
  groups.push_back(std::move(g));
}
std::optional<uint64_t> chunk_adj_remove(std::vector<ChunkWriteReader::AdjG>& groups, uint64_t src, uint64_t dst) {
  for (size_t k = 0; k < groups.size(); k++) {
    auto& g = groups[k];
    if (g.src != src) continue;
    auto it = std::find(g.dsts.begin(), g.dsts.end(), dst);
    if (it == g.dsts.end()) continue;
    size_t pos = size_t(it - g.dsts.begin());
    g.dsts.erase(it);
    std::optional<uint64_t> o;
    if (!g.ords.empty()) { o = g.ords[pos]; g.ords.erase(g.ords.begin() + pos); }
    if (!g.labels.empty()) g.labels.erase(g.labels.begin() + pos);
    if (g.dsts.empty()) groups.erase(groups.begin() + k);
    return o;
  }
  return std::nullopt;
}

void chunk_adj_add_inline(std::vector<ChunkWriteReader::AdjG>& groups, uint64_t src, uint64_t dst, const Value& props,
                          bool has_props, std::optional<uint8_t> label) {
  Value row = props.is_row() ? props : Value::row({});
  for (auto& g : groups) {
    if (g.src != src) continue;
    if (std::find(g.dsts.begin(), g.dsts.end(), dst) != g.dsts.end()) return;
    size_t pos = size_t(std::count_if(g.dsts.begin(), g.dsts.end(), [&](uint64_t d) { return d < dst; }));
    g.dsts.insert(g.dsts.begin() + pos, dst);
    if (has_props) g.props.insert(g.props.begin() + pos, row);
    if (label) g.labels.insert(g.labels.begin() + pos, *label);
    return;
  }
  ChunkWriteReader::AdjG g{src, {dst}, {}, {}, {}};
  if (has_props) g.props.push_back(row);
  if (label) g.labels.push_back(*label);
  groups.push_back(std::move(g));
}
bool chunk_adj_remove_inline(std::vector<ChunkWriteReader::AdjG>& groups, uint64_t src, uint64_t dst) {
  for (size_t k = 0; k < groups.size(); k++) {
    auto& g = groups[k];
    if (g.src != src) continue;
    auto it = std::find(g.dsts.begin(), g.dsts.end(), dst);
    if (it == g.dsts.end()) continue;
    size_t pos = size_t(it - g.dsts.begin());
    g.dsts.erase(it);
    if (!g.props.empty()) g.props.erase(g.props.begin() + pos);
    if (!g.labels.empty()) g.labels.erase(g.labels.begin() + pos);
    if (g.dsts.empty()) groups.erase(groups.begin() + k);
    return true;
  }
  return false;
}
bool chunk_adj_set_inline(std::vector<ChunkWriteReader::AdjG>& groups, uint64_t src, uint64_t dst, const Value& props) {
  for (auto& g : groups) {
    if (g.src != src) continue;
    auto it = std::find(g.dsts.begin(), g.dsts.end(), dst);
    if (it == g.dsts.end() || g.props.empty()) return false;
    Value& p = g.props[size_t(it - g.dsts.begin())];
    p = row_merge(p.is_row() ? p : Value::row({}), props);
    return true;
  }
  return false;
}

std::optional<uint64_t> chunk_adj_ordinal(const std::vector<ChunkWriteReader::AdjG>& groups, uint64_t src, uint64_t dst) {
  for (auto& g : groups) {
    if (g.src != src) continue;
    auto it = std::find(g.dsts.begin(), g.dsts.end(), dst);
    if (it == g.dsts.end() || g.ords.empty()) return std::nullopt;
    return g.ords[size_t(it - g.dsts.begin())];
  }
  return std::nullopt;
}

void chunk_merge_row(std::vector<std::pair<uint64_t, Value>>& rows, uint64_t key, const Value& props) {
  for (auto& r : rows)
    if (r.first == key) { r.second = row_merge(r.second.is_row() ? r.second : Value::row({}), props); return; }
  rows.emplace_back(key, props);
}

}  // namespace evalcpp
