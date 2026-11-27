// What meta's .cc files share beyond the class (format.h): the fan-out prefix registration that the reads and the index
// ops both use (defined in reads.cc), and the empty column list of an ordinals-only chunk.
#pragma once

#include <string>
#include <vector>

#include "exec/chunk_cache.h"
#include "formats/meta/format.h"

namespace evalcpp::meta {

void register_props(FanOut& f, const Schema& schema);
void register_adj(FanOut& f, const Schema& schema, const std::string& etype);

inline const std::vector<ColDecl> kNoCols;   // own placement's adjacency chunk holds ordinals, no columns

}  // namespace evalcpp::meta
