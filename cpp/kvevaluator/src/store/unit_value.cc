#include "store/unit_value.h"

#include <algorithm>
#include <stdexcept>

#include "codec/varint.h"
#include "formats/common.h"

namespace evalcpp::unit {

std::string part_key(std::string_view unit_key, uint64_t start) {
  std::string k(unit_key);
  k.push_back('\0');
  put_u64be(k, start);
  return k;
}

std::string part_key_at(std::string_view unit_key, size_t first, const IdOf& id_of) {
  if (first == 0) return part_key(unit_key, 0);
  uint64_t id = id_of(first);
  if (id <= id_of(first - 1))                                // the ranges would overlap: an id in two parts
    throw std::runtime_error("unit::write: a part boundary between equal (or unsorted) ids (" + std::to_string(id) + ")");
  return part_key(unit_key, id);
}

void write(KvSink& sink, const std::string& key, size_t n, const Encoder& encode, uint64_t chunk_target,
           const IdOf& id_of) {
  std::string whole = encode(0, n);
  if (whole.size() <= chunk_target || n <= 1) {
    sink.put(key, whole);
    return;
  }
  // Greedy MAXIMAL parts: each part takes the most entries that still encode within the target
  // (the cheapest read the layout allows -- fewest parts). Encodings are not linear in n, so a
  // proportional guess seeds a grow-then-bisect search: O(log n) encodes per part.
  size_t per = std::max<size_t>(1, size_t(uint64_t(n) * chunk_target / whole.size()));
  for (size_t i = 0; i < n;) {
    size_t left = n - i;
    auto fits = [&](size_t m) { return encode(i, i + m).size() <= chunk_target; };
    size_t lo, hi;                                   // lo fits (or is 1), hi does not (or is left+1)
    size_t m = std::min(per, left);
    if (fits(m)) {
      lo = m;
      hi = left + 1;
      for (size_t g = m * 2; g < hi; g *= 2) {       // grow until a size that does not fit
        if (fits(g)) lo = g;
        else { hi = g; break; }
      }
    } else {
      lo = 1;
      hi = m;
    }
    while (hi - lo > 1) {                            // bisect to the largest fitting size
      size_t mid = lo + (hi - lo) / 2;
      if (fits(mid)) lo = mid;
      else hi = mid;
    }
    sink.put(part_key_at(key, i, id_of), encode(i, i + lo));   // lo = 1 may exceed the target: one entry
    i += lo;
  }
}

std::optional<std::vector<std::string>> read(CountingStore& st, const std::string& key) {
  Cursor cur = st.seek(key);                                  // 1 SEEK
  if (!cur.valid()) { cur.mark_miss(); return std::nullopt; }
  if (cur.key() == key) {                                     // the whole unit: one landing, a hit
    std::vector<std::string> out;
    out.emplace_back(cur.value());
    return out;
  }
  if (!owns(cur.key(), key)) { cur.mark_miss(); return std::nullopt; }   // a foreign key: absent
  std::vector<std::string> out;
  while (cur.valid() && owns(cur.key(), key)) {               // the parts, in order
    out.emplace_back(cur.value());
    cur.next();                                               // 1 NEXT per further part
  }
  end_scan(cur);                                              // the landing past the last part
  return out;
}

std::optional<std::pair<std::string, std::string>> rmw_locate(CountingStore& st, const std::string& key, uint64_t id) {
  // A writer does not know whether the unit is split, so it looks up the unit's own key first (as Dgraph reads the main
  // key before its parts): a whole unit is that one forward landing; a split one adds the reverse seek to its part.
  Cursor cur = st.seek(key);                                  // 1 SEEK: lands on K (whole), part 0 (split) or a foreign key
  if (!cur.valid() || !owns(cur.key(), key)) { cur.mark_miss(); return std::nullopt; }   // absent: a foreign key
  st.count_used(key.size(), 0);                               // the unit's key used, once per locate; the value = RMW amp
  if (cur.key() == key) return std::make_pair(key, std::string(cur.value()));   // whole: 1 SEEK
  Cursor part = st.seek_for_prev(part_key(key, id));          // split: + 1 SEEK back to the greatest start <= id
  if (!part.valid() || !owns(part.key(), key)) { part.mark_miss(); return std::nullopt; }   // unreachable while part 0 exists
  return std::make_pair(std::string(part.key()), std::string(part.value()));
}

std::optional<std::vector<std::pair<std::string, std::string>>> rmw_read(CountingStore& st, const std::string& key) {
  // the same cheapest read as read() -- 1 SEEK, + 1 NEXT per further part and the landing past the
  // last -- keeping each part's KEY (a delete of the owner rewrites every part)
  Cursor cur = st.seek(key);                                  // 1 SEEK
  if (!cur.valid()) { cur.mark_miss(); return std::nullopt; }
  std::vector<std::pair<std::string, std::string>> out;
  if (cur.key() == key) {                                     // the whole unit: one landing, a hit
    st.count_used(key.size(), 0);                             // key used; the value = RMW amp
    out.emplace_back(key, std::string(cur.value()));
    return out;
  }
  if (!owns(cur.key(), key)) { cur.mark_miss(); return std::nullopt; }   // a foreign key: absent
  while (cur.valid() && owns(cur.key(), key)) {               // the parts, in order
    if (out.empty()) st.count_used(key.size(), 0);            // the unit's key used once, as a read charges it
    out.emplace_back(std::string(cur.key()), std::string(cur.value()));
    cur.next();                                               // 1 NEXT per further part
  }
  end_scan(cur);                                              // the landing past the last part
  return out;
}

}  // namespace evalcpp::unit
