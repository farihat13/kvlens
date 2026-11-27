// nebula's realistic key codec (in and out). The logical keys are Python's tuples
// (formats/nebula.py); only the bytes change. Two key ORDERS, one per store (Config::key_order):
//   vid_first (the default: the engine's order -- partId + vid + tagId; partId + srcVid + edgeType + rank + dstVid)
//     vertex  (vid, tag)                         -> 'v' vid:u64 tag:u32
//     edge    (src, "+etype"|"-etype", "0", dst) -> 'e' src:u64 etype:u32 (top bit = reverse) dst:u64
//   type_first (the arm `nebula-typefirst`, never a baseline)
//     vertex  -> 'v' tag:u32 vid:u64          edge -> 'e' etype:u32 (top bit = reverse) src:u64 dst:u64
//   index   ("i", label, prop, value, vid)  -> the shared SortedIndex codec (index/sorted_index.h), either order
// Out (constant-width engine fields, left out as for every format: the fairness rule): partId (3 B),
// rank (8 B), the edge placeholder byte. Same fields, widths and values in both orders (13 B and 21 B keys). Every read is
// prefix-scoped -- a vertex GET, an edge prefix scan on (src, +-etype) -- so which keys an op visits, and its
// S/G/N, are the same either way; what differs is which keys sit next to each other in a block (type_first: each
// label and each (etype, direction) is one key run; vid_first: a vertex's rows and edges sit together).
#pragma once
#include <cstdint>
#include <string>
#include <string_view>

namespace evalcpp::nebula {

enum class KeyOrder { VidFirst, TypeFirst };
KeyOrder parse_key_order(const std::string& name);   // "vid_first" | "type_first"; throws on anything else
const char* key_order_name(KeyOrder o);

// every vertex / edge key of one store, built and decoded in that store's order. No file assembles a key by hand.
struct Keys {
  KeyOrder order = KeyOrder::VidFirst;

  std::string vertex_key(int tag_id, uint64_t vid) const;
  std::string edge_prefix(int etype_id, bool out, uint64_t src) const;       // (src, +-etype)
  std::string edge_key(int etype_id, bool out, uint64_t src, uint64_t dst) const;
  // a date-sorted subclass's DATED etypes (retired): an 8-byte BE DATE between the (src, +-etype) prefix and dst -- the sort key (an
  // edge property in the key; NOT nebula's native rank, which the model byte-accounts
  // out-of-store and never materializes). THE RULE IS SCHEMA-DRIVEN: the
  // date field exists on a direction IFF the schema's adj_sort declaration dates that
  // direction. The shipped declarations are all `in`, so the REVERSE key carries it and the
  // forward key holds no field; a declared-`out` dated etype is REFUSED at construction
  // (both languages), never served with zeros. Non-dated etypes keep the dateless layout
  // everywhere, so canonical stores are byte-identical; edge_dst works on every shape (dst
  // is last either way).
  std::string edge_key_dated(int etype_id, bool out, uint64_t src, int64_t date, uint64_t dst) const;
  // a vertex prop-group SUB-ROW (vid, tag, gname): the split feature's extra key per group
  std::string vertex_subrow_key(int tag_id, uint64_t vid, const std::string& gname) const;
  // the u32 of a 'v' / 'e' key: the tag id, or the etype id with the reverse bit on top (key.size() >= 13)
  uint32_t type_field(std::string_view key) const;
};

uint64_t edge_dst(std::string_view key);                            // the trailing u64
inline bool has_prefix(std::string_view key, std::string_view prefix) {
  return key.size() >= prefix.size() && key.substr(0, prefix.size()) == prefix;
}

}  // namespace evalcpp::nebula
