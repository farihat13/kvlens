// The census groups: how `census` (a store) and `tally` (an ingest into a counting sink, no store) name the part
// each key belongs to. One definition, so a tally and a census of the same layout give the same rows.
#pragma once

#include <cstdint>
#include <string>
#include <tuple>
#include <vector>

#include "formats/format.h"

namespace evalcpp {
namespace census_groups {

// one group's identity: part (vertex_props | adjacency | edge_props | index | other | unclassified), type (a vertex
// label or an edge type), direction (out | in for adjacency where the key says it), owner (the label or label group
// an adjacency key names), detail (an index's property; "" = the label's type index)
using GroupKey = std::tuple<std::string, std::string, std::string, std::string, std::string>;
struct Tally { uint64_t keys = 0, key_bytes = 0, value_bytes = 0, payload_bytes = 0, empty_values = 0; };

// a key's class -> its census group (the classifier's first five fields)
inline GroupKey group_of(const KeyClass& c) { return {c.part, c.type, c.direction, c.owner, c.detail}; }


}  // namespace census_groups
}  // namespace evalcpp
