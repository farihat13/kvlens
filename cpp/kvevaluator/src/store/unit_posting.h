// The read-modify-write of a POSTING UNIT over its parts (store/unit_value.h): a 'U' uid
// posting (an index term) or an 'A' uid-ascending adjacency with facets, stored whole or as
// consecutive parts. Shared infra: the posting index and dgraph-c64k rewrite units
// this way; no format owns it. Policy stays with the caller: these helpers do nothing when
// there is nothing to take out (an absent unit, an entry in no part) -- a format that re-puts
// blindly there (dgraph's rule) decides that itself before calling.
// An add / remove / set reads the ONE part whose id range holds its uid through unit::rmw_locate (counted:
// 1 SEEK whole, 2 split) and rewrites it with ONE put. Never re-splits: a unit grows in place
// (dgraph-faithful), so an incrementally written unit can carry a part above chunk_target. An
// emptied part is kept as the empty posting of its kind, so its range stays its own. The tomb
// reads every part (unit::rmw_read) and empties each.
#pragma once
#include <cstdint>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "codec/posting.h"
#include "formats/format.h"
#include "store/counting.h"

namespace evalcpp::unit {

using Puts = std::vector<std::pair<std::string, std::string>>;

// 'U' posting: add `uid` to the part whose uid range holds it. used_write = the entry's packed width in the new posting; the
// co-tenant rewrite + framing = write amplification. A uid already present: no duplicate.
void posting_add(CountingStore& st, const std::string& key, uint64_t uid, Puts& puts);
// 'U' posting: take `uid` out of the part holding it (used_write 0: a removal has no logical
// value bytes). Returns false, writing nothing, when no part holds it (or the unit is absent).
bool posting_remove(CountingStore& st, const std::string& key, uint64_t uid, Puts& puts);

// The adjacency helpers take the posting's facet codec (codec/posting.h; default named = dgraph's
// prim dicts, which dgraph always uses); a unit is decoded and re-encoded with the same codec.
// A codec with label_bits > 0 (posting.h): `facets.label` is the new entry's far-end code on add; set keeps the
// entry's stored code; remove drops it with its entry.
// 'A' adjacency: add (`other`, facets) at its uid position in the part whose range holds it.
// used_write = the entry's packed uid width + its own facet bytes. A duplicate entry THROWS
// (the write params pick new pairs).
void adjacency_add(CountingStore& st, const std::string& key, uint64_t other, const Value& facets, Puts& puts,
                   const posting::AdjCodec& codec);
// 'A' adjacency: take `other` out of the part holding it (used_write 0). False = nothing held it.
bool adjacency_remove(CountingStore& st, const std::string& key, uint64_t other, Puts& puts,
                      const posting::AdjCodec& codec);
// 'A' adjacency: set facets on the entry of `other` ({**old, **props}: its untouched facets stay)
// in the part holding it -- update_edge_prop. used_write = the entry's new facet bytes (schema: the
// changed cells only, as a Split record's update; the rest of the rewritten part is amplification). False = nothing held it, nothing written. A posting holds
// each uid once (adjacency_add refuses a duplicate), so the first entry found is the only one.
bool adjacency_set(CountingStore& st, const std::string& key, uint64_t other, const Value& props, Puts& puts,
                   const posting::AdjCodec& codec);
// 'A' adjacency: empty the unit -- every part it has becomes the empty posting (an absent unit
// gets one empty whole: the tomb names the change even where nothing was stored). used_write = the
// unit's key once, whatever the part count.
void adjacency_tomb(CountingStore& st, const std::string& key, Puts& puts, const posting::AdjCodec& codec);
// 'A' adjacency, a delete of the unit's OWNER: ONE read of every part (rmw_read) gives the neighbours to fix up,
// and the same part keys get the empty posting (no second read). Returns the neighbours in stored order;
// nullopt = absent; an empty list (an emptied unit) writes nothing.
std::optional<std::vector<uint64_t>> adjacency_tomb_read(CountingStore& st, const std::string& key, Puts& puts,
                                                         const posting::AdjCodec& codec);

// The locate seam: the whole unit, or the ONE part whose uid range holds `uid` (unit::rmw_locate,
// 1 SEEK whole, 2 split) -- its key and raw value; nullopt = absent. What the helpers rewrite, and what a caller
// with its own rewrite policy reads through (dgraph-c64k re-puts blindly where nothing was taken
// out: dgraph's rule, decided by dgraph). The same landing serves a 'U' posting and an 'A' adjacency.
struct Located { std::string key; std::string raw; };
std::optional<Located> locate_unit(CountingStore& st, const std::string& key, uint64_t uid);

}  // namespace evalcpp::unit
