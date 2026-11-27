// One LOGICAL UNIT (a posting, an adjacency list) stored as one point-seekable KV, or -- only
// when its encoding overflows chunk_target -- as consecutive PARTS under `K | 0x00 | start:u64BE`
// (no bare-K key then). Every split unit is id-ascending; a part holds a contiguous slice of its
// entries and is keyed by the START of its id range, fixed when the part is made: part 0 starts at
// 0, part i > 0 at its first entry's id (Dgraph's SplitKey: the base key + the 8-byte start uid,
// x/keys.go:511; its first part starts at uid 1). The ranges [start_i, start_i+1) hold every id,
// so a write finds the one part for an id with one reverse seek (rmw_locate, after its look at K). A bounded prefix read
// stops after the first part(s). Valid wherever no sibling unit key is a
// proper prefix of another (fixed-width keys, or length-tagged terms): then `K` and `K|0x00|i`
// belong to the same unit and nothing else sorts between them.
// Read cost: a whole unit is 1 SEEK; a split unit is 1 SEEK + 1 NEXT per further part + the NEXT
// that lands past the last part, an absence miss (the shared end_scan rule) -- N = parts. A write's
// landing (rmw_locate) is 1 SEEK on a whole unit and 2 on a split one: a forward seek to K, then on a
// split unit seek_for_prev to the part whose range holds the id. Shared infra: the posting index
// (index/posting_index.h), dgraph's adjacency and meta's lists use it; no format owns it.
#pragma once
#include <cstdint>
#include <functional>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "formats/format.h"
#include "store/counting.h"

namespace evalcpp::unit {

constexpr size_t kPartSuffix = 9;   // 0x00 + start:u64BE (the start of the part's id range; part 0: 0)

std::string part_key(std::string_view unit_key, uint64_t start);
// is `key` a part of the unit whose key is its first `unit_len` bytes?
inline bool is_part_key(std::string_view key, size_t unit_len) {
  return key.size() == unit_len + kPartSuffix && key[unit_len] == '\0';
}
// the start of a part key's id range (its u64BE suffix; callers check is_part_key first); 0 = the unit's first part
inline uint64_t part_start(std::string_view key, size_t unit_len) {
  uint64_t p = 0;
  for (size_t i = 0; i < 8; i++) p = (p << 8) | uint8_t(key[unit_len + 1 + i]);
  return p;
}
// does `key` belong to `unit_key` -- the whole, or one of its parts?
inline bool owns(std::string_view key, std::string_view unit_key) {
  return key.size() >= unit_key.size() && key.substr(0, unit_key.size()) == unit_key &&
         (key.size() == unit_key.size() || is_part_key(key, unit_key.size()));
}

// encodes entries [lo, hi) of the unit as one value
using Encoder = std::function<std::string(size_t lo, size_t hi)>;
// the id of entry i (ascending in i): what a part is keyed by
using IdOf = std::function<uint64_t(size_t i)>;
// the key of the part whose first entry is entry `first` of the unit: start 0 for the first part, else
// that entry's id. Throws when entry `first` and the one before it share an id (that id would then
// belong to two ranges). Every ingest splitter keys its parts here (unit::write, meta's own).
std::string part_key_at(std::string_view unit_key, size_t first, const IdOf& id_of);
// ingest: the unit `key` of n entries -- whole when its encoding fits chunk_target, else parts
// each within the target (a single entry larger than the target gets a part of its own), keyed by
// part_key_at
void write(KvSink& sink, const std::string& key, size_t n, const Encoder& encode, uint64_t chunk_target,
           const IdOf& id_of);
// counted read: the unit's value, or its parts' values in order (1 SEEK; split: + N = parts, the
// last a miss). nullopt = absent (the SEEK landed on a foreign key: the miss lane, 0 used).
std::optional<std::vector<std::string>> read(CountingStore& st, const std::string& key);
// a write's read of the WHOLE unit, every part (a delete of the owner: it rewrites every part): counted as read()
// (1 SEEK; split: + N = parts, the last a miss) -- (key, value) of the whole, or of every part in order. nullopt = absent.
std::optional<std::vector<std::pair<std::string, std::string>>> rmw_read(CountingStore& st, const std::string& key);
// a write's read of the whole unit, or of the ONE part whose id range holds `id`: a forward SEEK(K) first, which lands
// on bare K (whole: done, 1 SEEK), on part 0 (split: + seek_for_prev(K|0x00|id), the part with the greatest start
// <= id, 2 SEEKs) or on a foreign key or nothing (absent: a miss). (key, value) of the unit or part; nullopt = absent.
// Part 0 (start 0) exists while a unit is split -- a writer never deletes it (an emptied part 0 stays as an empty
// value) -- so a split unit is never read as absent.
std::optional<std::pair<std::string, std::string>> rmw_locate(CountingStore& st, const std::string& key, uint64_t id);

}  // namespace evalcpp::unit
