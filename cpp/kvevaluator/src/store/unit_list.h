// The read-modify-write of a meta-v4 LIST UNIT (codec/lists.h) over its parts (store/unit_value.h): one owner's
// Condensed list, whole or split into consecutive parts. The same counting as unit_posting's adjacency helpers
// (dgraph's, which stays on its own codec): the ONE part whose id range holds the entry is read through
// unit::rmw_locate (1 SEEK whole, 2 split), the rewrite is ONE put of it. Never re-splits. An emptied whole unit, or
// an emptied part after the first, is DELed (its range joins the part below; reads skip a missing part); an emptied
// FIRST part of a split unit stays as an empty list, so every id keeps a part to land on. Policy stays with the
// caller: nothing is written when there is nothing to take out.
#pragma once
#include <cstdint>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "codec/lists.h"
#include "formats/rows.h"
#include "store/counting.h"

namespace evalcpp::unit {

using ListPuts = std::vector<std::pair<std::string, std::string>>;

// Every call names the list's label_bits (codec/lists.h: 0 = no label block); the entries keep their labels through
// any rewrite.
// add (`other`, its far-end `label` code, props) at its id position in the part whose id range holds it. used_write = the entry's own id width + its cells' bytes in the new list
// (the label bits are not a cell: not used bytes). A duplicate THROWS (the write params pick new pairs).
void list_add(CountingStore& st, const std::string& key, uint64_t other, uint8_t label, const Value& props,
              ListPuts& puts, const std::vector<ColDecl>& cols, unsigned label_bits);
// take `other` out of the part holding it (used_write 0): the part is rewritten, or DELed when the removal empties it
// (into `dels`), except a split unit's first part, rewritten as an empty list. False = nothing held it, nothing written.
bool list_remove(CountingStore& st, const std::string& key, uint64_t other, ListPuts& puts,
                 std::vector<std::string>& dels, const std::vector<ColDecl>& cols, unsigned label_bits);
// set props on the entry of `other` ({**old, **props}) in the part holding it -- update_edge_prop. used_write = the
// CHANGED cells' bytes in the new list (what a Split record's update charges). False = nothing held it.
bool list_set(CountingStore& st, const std::string& key, uint64_t other, const Value& props, ListPuts& puts,
              const std::vector<ColDecl>& cols, unsigned label_bits);

}  // namespace evalcpp::unit
