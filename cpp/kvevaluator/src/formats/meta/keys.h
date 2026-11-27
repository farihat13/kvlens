// meta's keys (meta-v4, key family D): ONE family per component, the same in every layout, with no label in an
// adjacency key. A vertex record is named by its exact label (`vtype`) then the vid, so a label's records are
// contiguous (a label's props differ, so props stay per label). An adjacency key is EDGE-TYPE first and names no
// label: a hop needs only the owner vid, in every layout. Discriminators are 1 byte (vtype, attr, etype, dir); vid
// and dst stay u64.
//
//   vertex      'v' · vtype · vid · [attr | group]         (attr: Split; group g >= 1: a grouped-props row)
//   chunk       'v' · vtype · first_vid                    (Chunked props)
//   edge        'e' · etype · dir · owner · [dst]          (dst: Split; none: a Condensed list, + a part suffix)
//   chunk       'e' · etype · dir · first_owner            (Chunked adjacency)
//   own props   'e' · etype · 0x02 · tag · first_ordinal   (Chunked with own placement: ordinals per (etype, the
//                                                          out-src's vid group))
// dir: 0 out, 1 in (2 = the own-props family). Owners sort by vid, whose top 4 bits are the vid-group tag
// (util/vid.h kVidShift), so a group's owners are contiguous in every (etype, dir) and the chunker closes a chunk at
// a group boundary: a chunk is one group's. One (etype, dir) has one layout, and the dir byte comes before the owner,
// so a Split record, a Condensed list and a chunk never share a key range: the families cannot collide, also when an
// etype's two directions have different layouts (config.h adjacency_by_etype_dir).
// Room for parallel edges (decision 7, FinBench): a `multi` edge type would append a discriminator after dst on
// Split ('e'·etype·dir·owner·dst·disc: +8 B for a timestamp, +1-3 B for a uvarint sequence) and carry one more
// column in a list entry; a `unique` edge type (every LDBC / LinkBench one) stores none, so its keys stay as above.
//
// Parts (formats/meta/parts.h; the executor's run.parts): every key belongs to one store PART, named
// `<kind>:<fields>` (fields ':'-separated; parts.cc refuses a schema name holding ':' or '.'):
//   props:<label>                     'v'·vtype·...        a label's vertex props (record, row or chunk)
//   adj:<group>:<etype>:<out|in>      one list family: the owner vid's group (from its tag), in every layout.
//                                     A key cannot tell Post from Comment (both Message): no layout keys a label
//   eprops:<group>:<etype>            own-placement edge-prop chunks, under the out-src's group
//   index:<label>.<prop> | index:<label>   one secondary index ('i'·index_id·...); no prop = the type index
//   unknown                           a key none of the above decodes (never expected; a sum still closes)
#pragma once
#include <cstdint>
#include <string>
#include <string_view>

namespace evalcpp::meta {

constexpr char kVertex = 'v', kEdge = 'e';
constexpr uint8_t kDirOut = 0, kDirIn = 1;

inline void put_u8(std::string& k, uint8_t b) { k.push_back(char(b)); }
inline void put_u64be(std::string& k, uint64_t u) {
  for (int i = 7; i >= 0; i--) k.push_back(char((u >> (8 * i)) & 0xff));
}
inline bool has_prefix(std::string_view key, std::string_view prefix) {
  return key.size() >= prefix.size() && key.substr(0, prefix.size()) == prefix;
}

// --- vertex: 'v' · vtype · vid · [attr] (vtype first -> a label's rows are one contiguous run) ---
inline std::string vertex_key(uint8_t vtype, uint64_t vid) {   // grouped: full key (value = the row)
  std::string k(1, kVertex);
  put_u8(k, vtype);
  put_u64be(k, vid);
  return k;
}
// a grouped-props row: group 0 (the existence row) is the plain vertex key -- a one-group label is the
// Condensed row, byte for byte -- and group g >= 1 appends one byte g (sorts right after group 0)
inline std::string vertex_group_key(uint8_t vtype, uint64_t vid, size_t group) {
  std::string k = vertex_key(vtype, vid);
  if (group) put_u8(k, uint8_t(group));
  return k;
}
// a singleton record's attr byte: the column's position + 1 (attr 0 once named the stored id column, now gone;
// kept free so Split keys did not change when id stopped being stored)
inline uint8_t attr_of_col(size_t col) { return uint8_t(col + 1); }
inline std::string vertex_attr_key(uint8_t vtype, uint64_t vid, uint8_t attr) {   // singleton: one prim cell
  std::string k = vertex_key(vtype, vid);
  put_u8(k, attr);
  return k;
}

// --- edge: 'e' · etype · dir · owner · [dst] (etype-first, no label) ------------------------------------
constexpr uint8_t kEpDir = 2;   // the dir byte of the own-placement edge-prop family
inline std::string adj_prefix(uint8_t etype, bool out, uint64_t owner) {   // a Split owner's records / a Condensed list
  std::string k(1, kEdge);
  put_u8(k, etype);
  put_u8(k, out ? kDirOut : kDirIn);
  put_u64be(k, owner);
  return k;
}
inline std::string edge_key(uint8_t etype, bool out, uint64_t owner, uint64_t dst) {
  std::string k = adj_prefix(etype, out, owner);
  put_u64be(k, dst);
  return k;
}
inline uint64_t trailing_u64(std::string_view key) {   // the last 8 bytes as u64
  uint64_t u = 0;
  for (size_t i = key.size() - 8; i < key.size(); i++) u = (u << 8) | uint8_t(key[i]);
  return u;
}
inline uint64_t edge_dst(std::string_view key) { return trailing_u64(key); }   // the trailing neighbour vid

// --- chunked helpers: a chunk is keyed by its FIRST owner (or ordinal), so seek_for_prev(full key) lands on the
// covering chunk; the family prefix scopes the chunk run ------------------------------------------------------
inline std::string vertex_family(uint8_t vtype) {   // 'v'·vtype -- the chunk run for one label
  std::string k(1, kVertex);
  put_u8(k, vtype);
  return k;
}
inline uint64_t chunk_first(std::string_view chunk_key) { return trailing_u64(chunk_key); }   // its first owner
inline std::string padj_prefix(uint8_t etype, bool out) {   // 'e'·etype·dir -- one (etype, dir)'s chunk run
  std::string k(1, kEdge);
  put_u8(k, etype);
  put_u8(k, out ? kDirOut : kDirIn);
  return k;
}
inline std::string pep_prefix(uint8_t etype, uint8_t tag) {   // 'e'·etype·0x02·tag -- one group's own-props chunks
  std::string k(1, kEdge);
  put_u8(k, etype);
  put_u8(k, kEpDir);
  put_u8(k, tag);
  return k;
}
inline std::string with_owner(const std::string& prefix, uint64_t owner) {   // a chunk key = prefix + first id
  std::string k = prefix;
  put_u64be(k, owner);
  return k;
}

}  // namespace evalcpp::meta
