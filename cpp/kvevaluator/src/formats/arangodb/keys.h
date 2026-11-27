// arangodb's realistic key codec. The logical keys are Python's tuples
// (formats/arangodb.py); only the bytes change:
//   document   (collection, docid)        -> 'D' coll:u32 docid:u64
//   primary    ("pk", label, vid)         -> 'P' coll:u32 vid:u64
//   edge index ("f"|"t", etype, vid, eid) -> 'f'|'t' etype:u32 vid:u64 eid:u64
//   value index ("i", label, prop, val, docid) -> the shared IndexKeys codec with the DOCID in
//   the trailing slot (arango's index key holds the docid, not the vid; a string term is the
//   shared variable-width length-tagged encoding, index/sorted_index.h -- Python's tuple components
//   are self-delimiting, a byte codec needs the length tag; scans visit the same keys either way)
// Docids are u64 counters (Python's "d1"/"e1" strings); a write-inserted document sets the top
// bit -- Python's separate "w" id space, so an incremental insert can never collide with a
// loaded docid. Two small VALUE codecs live here too: the pk's loc (coll:u32 docid:u64) and the
// covering edge-index ref (the far vertex's label as bytes + vid:u64, vpack.cc; decoded back to "Label/<hexvid>").
#pragma once
#include <cstdint>
#include <string>
#include <string_view>

namespace evalcpp::arangodb {

constexpr uint64_t kWriteDoc = uint64_t(1) << 63;   // the incremental-insert docid space

// The 'D' document namespace holds BOTH vertex and edge documents, but vertex-label ids and
// edge-type ids are independent 0-based spaces -- packed raw into the same coll:u32 they
// COLLIDE (Comment vertex id 2 == hasInterest edge id 2; the two ingest runs then write
// DUPLICATE doc keys and the later SST batch silently shadows the earlier one's docs: 81,066
// Comment docs lost at sf0.3). Edge collections therefore live at a disjoint u32 offset --
// one effective collection-id space, like real arango's unique per-collection objectids.
// Same key width; op counts unchanged -- but edge docs no longer interleave with vertex docs
// in the 'D' key range, so BLOCK adjacency (cold block-fetch counts) does change; arango cold
// numbers are not comparable across the fix (its data was wrong anyway). Every edge-DOC site
// must go through edge_doc_coll; the pk/'f'/'t' families are kind-prefixed and never collided.
constexpr uint32_t kEdgeDocColl = uint32_t(1) << 16;
inline int edge_doc_coll(int etype_id) { return int(kEdgeDocColl + uint32_t(etype_id)); }

inline void put_u32be(std::string& k, uint32_t x) {
  for (int i = 3; i >= 0; i--) k.push_back(char((x >> (8 * i)) & 0xff));
}
inline void put_u64be(std::string& k, uint64_t x) {
  for (int i = 7; i >= 0; i--) k.push_back(char((x >> (8 * i)) & 0xff));
}
inline uint64_t get_u64be(std::string_view b, size_t i) {
  uint64_t x = 0;
  for (size_t k = 0; k < 8; k++) x = (x << 8) | uint8_t(b[i + k]);
  return x;
}

inline std::string doc_key(int coll_id, uint64_t docid) {
  std::string k = "D";
  put_u32be(k, uint32_t(coll_id));
  put_u64be(k, docid);
  return k;
}
inline std::string doc_prefix(int coll_id) {   // one collection's documents: one key run, in docid order
  std::string k = "D";
  put_u32be(k, uint32_t(coll_id));
  return k;
}
inline std::string pk_key(int coll_id, uint64_t vid) {
  std::string k = "P";
  put_u32be(k, uint32_t(coll_id));
  put_u64be(k, vid);
  return k;
}
inline std::string pk_prefix(int coll_id) {
  std::string k = "P";
  put_u32be(k, uint32_t(coll_id));
  return k;
}
inline std::string edge_prefix(bool from, int etype_id, uint64_t vid) {
  std::string k = from ? "f" : "t";
  put_u32be(k, uint32_t(etype_id));
  put_u64be(k, vid);
  return k;
}
inline std::string edge_key(bool from, int etype_id, uint64_t vid, uint64_t eid) {
  std::string k = edge_prefix(from, etype_id, vid);
  put_u64be(k, eid);
  return k;
}
inline uint64_t trailing_u64(std::string_view key) { return get_u64be(key, key.size() - 8); }
inline bool has_prefix(std::string_view key, std::string_view prefix) {
  return key.size() >= prefix.size() && key.substr(0, prefix.size()) == prefix;
}

// the pk's value: where the document lives
inline std::string loc_value(int coll_id, uint64_t docid) {
  std::string v;
  put_u32be(v, uint32_t(coll_id));
  put_u64be(v, docid);
  return v;
}
struct Loc { int coll_id; uint64_t docid; };
inline Loc loc_of(std::string_view raw) {
  uint32_t c = 0;
  for (size_t k = 0; k < 4; k++) c = (c << 8) | uint8_t(raw[k]);
  return {int(c), get_u64be(raw, 4)};
}

}  // namespace evalcpp::arangodb
