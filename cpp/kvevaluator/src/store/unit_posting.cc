#include "store/unit_posting.h"

#include <algorithm>
#include <optional>
#include <stdexcept>

#include "codec/posting.h"
#include "codec/rowv2.h"
#include "store/unit_value.h"

namespace evalcpp::unit {

using Parts = std::optional<std::vector<std::pair<std::string, std::string>>>;
using Postings = std::vector<std::pair<uint64_t, Value>>;

std::optional<Located> locate_unit(CountingStore& st, const std::string& key, uint64_t uid) {
  auto at = rmw_locate(st, key, uid);
  if (!at) return std::nullopt;
  return Located{std::move(at->first), std::move(at->second)};
}

void posting_add(CountingStore& st, const std::string& key, uint64_t uid, Puts& puts) {
  std::optional<Located> at = locate_unit(st, key, uid);
  std::string target = at ? at->key : key;                    // the key rewritten: the whole, or ONE part
  std::vector<uint64_t> uids;
  if (at) uids = posting::decode_posting(at->raw);
  auto it = std::lower_bound(uids.begin(), uids.end(), uid);
  if (it == uids.end() || *it != uid) uids.insert(it, uid);
  std::string enc = posting::encode_posting(uids);
  std::vector<uint8_t> wd;
  posting::decode_posting_w(enc, &wd);
  size_t idx = size_t(std::lower_bound(uids.begin(), uids.end(), uid) - uids.begin());
  st.count_used_write(key.size(), idx < wd.size() ? wd[idx] : 0);      // the unit's key: a part suffix is layout amp
  puts.emplace_back(std::move(target), std::move(enc));
}

bool posting_remove(CountingStore& st, const std::string& key, uint64_t uid, Puts& puts) {
  std::optional<Located> at = locate_unit(st, key, uid);      // the one part whose range holds uid
  if (!at) return false;
  std::vector<uint64_t> uids = posting::decode_posting(at->raw);
  auto it = std::find(uids.begin(), uids.end(), uid);
  if (it == uids.end()) return false;
  uids.erase(it);
  st.count_used_write(key.size(), 0);
  puts.emplace_back(std::move(at->key), posting::encode_posting(uids));    // an emptied part stays, as 'U'
  return true;
}

void adjacency_add(CountingStore& st, const std::string& key, uint64_t other, const Value& facets, Puts& puts,
                   const posting::AdjCodec& codec) {
  std::optional<Located> at = locate_unit(st, key, other);
  std::string target = at ? at->key : key;
  Postings postings;
  if (at) postings = posting::decode_adjacency(at->raw, codec);
  auto it = std::lower_bound(postings.begin(), postings.end(), other,
                             [](const std::pair<uint64_t, Value>& p, uint64_t x) { return p.first < x; });
  if (it != postings.end() && it->first == other)
    throw std::runtime_error("adjacency write: edge already present (the write params pick new pairs)");
  size_t idx = size_t(it - postings.begin());
  Value entry = facets.is_row() ? facets : Value::row({});
  entry.label = facets.label;                    // the entry's far-end code, when the posting stores one (posting.h)
  postings.insert(it, {other, std::move(entry)});
  std::string enc = posting::encode_adjacency(postings, true, codec);
  std::vector<uint8_t> uw;
  std::vector<uint32_t> fw;
  posting::decode_adjacency_w(enc, &uw, &fw, codec);
  st.count_used_write(key.size(), (idx < uw.size() ? uw[idx] : 0) + (idx < fw.size() ? fw[idx] : 0));
  puts.emplace_back(std::move(target), std::move(enc));
}

bool adjacency_remove(CountingStore& st, const std::string& key, uint64_t other, Puts& puts,
                      const posting::AdjCodec& codec) {
  std::optional<Located> at = locate_unit(st, key, other);    // the one part whose range holds other
  if (!at) return false;
  Postings postings = posting::decode_adjacency(at->raw, codec);
  auto it = std::find_if(postings.begin(), postings.end(), [&](auto& p) { return p.first == other; });
  if (it == postings.end()) return false;
  postings.erase(it);
  st.count_used_write(key.size(), 0);
  puts.emplace_back(std::move(at->key),
                    postings.empty() ? posting::empty_adjacency(codec) : posting::encode_adjacency(postings, true, codec));
  return true;
}

bool adjacency_set(CountingStore& st, const std::string& key, uint64_t other, const Value& props, Puts& puts,
                   const posting::AdjCodec& codec) {
  std::optional<Located> at = locate_unit(st, key, other);    // the one part whose range holds other
  if (!at) return false;
  {
    Postings postings = posting::decode_adjacency(at->raw, codec);
    auto it = std::find_if(postings.begin(), postings.end(), [&](auto& p) { return p.first == other; });
    if (it == postings.end()) return false;
    size_t idx = size_t(it - postings.begin());
    int16_t far = it->second.label;               // the entry keeps its far-end code across a facet update
    it->second = row_merge(it->second.is_row() ? it->second : Value::row({}), props);
    it->second.label = far;
    std::string enc = posting::encode_adjacency(postings, true, codec);
    uint64_t named_width = 0;
    if (!codec.is_schema()) {   // named: the entry's whole facet dict
      std::vector<uint8_t> uw;
      std::vector<uint32_t> fw;
      posting::decode_adjacency_w(enc, &uw, &fw, codec);
      named_width = idx < fw.size() ? fw[idx] : 0;
    }
    st.count_used_write(key.size(), posting::update_used_bytes(it->second, props, codec, named_width));
    puts.emplace_back(std::move(at->key), std::move(enc));
    return true;
  }
}

void adjacency_tomb(CountingStore& st, const std::string& key, Puts& puts, const posting::AdjCodec& codec) {
  Parts parts = rmw_read(st, key);
  st.count_used_write(key.size(), 0);                         // the unit's key, once: the tomb names one unit
  if (!parts) {
    puts.emplace_back(key, posting::empty_adjacency(codec));
    return;
  }
  for (auto& [pk, raw] : *parts) puts.emplace_back(pk, posting::empty_adjacency(codec));
}

std::optional<std::vector<uint64_t>> adjacency_tomb_read(CountingStore& st, const std::string& key, Puts& puts,
                                                         const posting::AdjCodec& codec) {
  Parts parts = rmw_read(st, key);                           // the ONE read: every part, the neighbours from it
  if (!parts) return std::nullopt;
  std::vector<uint64_t> others;
  for (auto& [pk, raw] : *parts)
    for (auto& [u, facets] : posting::decode_adjacency(raw, codec)) others.push_back(u);
  if (others.empty()) return others;                         // an emptied unit: nothing to tomb (as before)
  st.count_used_write(key.size(), 0);
  for (auto& [pk, raw] : *parts) puts.emplace_back(pk, posting::empty_adjacency(codec));
  return others;
}

}  // namespace evalcpp::unit
