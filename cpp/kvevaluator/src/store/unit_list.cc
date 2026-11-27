#include "store/unit_list.h"

#include <algorithm>
#include <stdexcept>

#include "formats/common.h"
#include "store/unit_value.h"

namespace evalcpp::unit {

void list_add(CountingStore& st, const std::string& key, uint64_t other, uint8_t label, const Value& props,
              ListPuts& puts, const std::vector<ColDecl>& cols, unsigned label_bits) {
  auto at = rmw_locate(st, key, other);                    // the unit, or its part holding other (1 SEEK whole, 2 split)
  std::string target = at ? at->first : key;               // the key rewritten: the whole, or ONE part
  lists::Entries entries;
  if (at) entries = lists::View(&cols, at->second, label_bits).entries();
  auto it = std::lower_bound(entries.begin(), entries.end(), other,
                             [](const lists::Entry& e, uint64_t x) { return e.id < x; });
  if (it != entries.end() && it->id == other)
    throw std::runtime_error("list write: edge already present (the write params pick new pairs)");
  size_t idx = size_t(it - entries.begin());
  entries.insert(it, {other, props.is_row() ? props : Value::row({}), label});
  std::string enc = lists::encode(entries, label_bits, cols);
  lists::View nv(&cols, enc, label_bits);
  st.count_used_write(key.size(), nv.id_bytes(idx) + nv.props_bytes(idx));   // the unit's key: a part suffix is layout amp
  puts.emplace_back(std::move(target), std::move(enc));
}

bool list_remove(CountingStore& st, const std::string& key, uint64_t other, ListPuts& puts,
                 std::vector<std::string>& dels, const std::vector<ColDecl>& cols, unsigned label_bits) {
  auto at = rmw_locate(st, key, other);                    // the unit, or its part holding other (1 SEEK whole, 2 split)
  if (!at) return false;
  auto& [pk, raw] = *at;
  lists::Entries entries = lists::View(&cols, raw, label_bits).entries();
  auto it = std::find_if(entries.begin(), entries.end(), [&](auto& e) { return e.id == other; });
  if (it == entries.end()) return false;
  entries.erase(it);
  st.count_used_write(key.size(), 0);
  // a split unit's first part (start 0) stays, empty: every id of the unit lands on a part at or below it
  bool first_part = pk.size() != key.size() && part_start(pk, key.size()) == 0;
  if (entries.empty() && !first_part) dels.push_back(pk);   // an emptied whole unit or later part is DELed
  else puts.emplace_back(pk, lists::encode(entries, label_bits, cols));
  return true;
}

bool list_set(CountingStore& st, const std::string& key, uint64_t other, const Value& props, ListPuts& puts,
              const std::vector<ColDecl>& cols, unsigned label_bits) {
  auto at = rmw_locate(st, key, other);                    // the unit, or its part holding other (1 SEEK whole, 2 split)
  if (!at) return false;
  auto& [pk, raw] = *at;
  {
    lists::Entries entries = lists::View(&cols, raw, label_bits).entries();
    auto it = std::find_if(entries.begin(), entries.end(), [&](auto& e) { return e.id == other; });
    if (it == entries.end()) return false;
    size_t idx = size_t(it - entries.begin());
    it->props = row_merge(it->props.is_row() ? it->props : Value::row({}), props);
    std::string enc = lists::encode(entries, label_bits, cols);
    lists::View nv(&cols, enc, label_bits);
    uint64_t used = 0;                                      // the changed cells, as a Split record's update charges
    for (auto& [name, v] : props.as_row()) {
      int c = nv.has_props() ? nv.props()->col_index(name) : -1;
      if (c >= 0) used += nv.props()->cell_bytes(size_t(c), idx);
    }
    st.count_used_write(key.size(), used);
    puts.emplace_back(pk, std::move(enc));
    return true;
  }
}

}  // namespace evalcpp::unit
