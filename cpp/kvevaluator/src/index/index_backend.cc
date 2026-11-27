#include "index/index_backend.h"

#include <stdexcept>

#include "index/packed_posting_index.h"
#include "index/posting_index.h"
#include "index/sorted_index.h"

namespace evalcpp {

// The schema may name a layout per IndexDecl (`"layout"`); this cut supports ONE layout per
// store, so every declared one must agree, and it overrides the format's default. A mixed
// store (some terms sorted, some posting) would need a composite backend -- not built.
static std::string resolve_layout(const Schema& schema, const std::string& fallback) {
  std::string chosen;
  for (auto& ix : schema.indexes()) {
    if (ix.layout.empty()) continue;
    if (!chosen.empty() && chosen != ix.layout)
      throw std::runtime_error("schema declares mixed index layouts ('" + chosen + "' and '" + ix.layout +
                               "'); one layout per store is supported");
    chosen = ix.layout;
  }
  return chosen.empty() ? fallback : chosen;
}

std::unique_ptr<IndexBackend> make_index_backend(const Schema& schema, const std::string& layout, uint64_t chunk_target,
                                                 DateGranularity date_granularity) {
  std::string l = resolve_layout(schema, layout);
  if (l == "sorted_keys") {
    // sorted keys carry the full value per entry: a coarse granularity has no key shape here (yet)
    if (date_granularity != DateGranularity::full)
      throw std::runtime_error("index layout sorted_keys supports only the full date granularity");
    return std::make_unique<SortedIndex>(schema);
  }
  if (l == "posting") return std::make_unique<PostingIndex>(schema, chunk_target, date_granularity);
  // packed_posting: the sorted/date index CHUNKED (many values per KV) -- meta's index.owners:{pack},
  // an interior cell no engine occupies (type + string value indexes stay plain postings).
  if (l == "packed_posting") return std::make_unique<PackedPostingIndex>(schema, chunk_target, date_granularity);
  throw std::runtime_error("unknown index layout '" + l + "' (known: sorted_keys, posting, packed_posting)");
}

}  // namespace evalcpp
