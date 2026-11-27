#include "formats/nebula/keys.h"

#include <stdexcept>

#include "codec/varint.h"

namespace evalcpp::nebula {

static void put_u64be(std::string& out, uint64_t x) {
  for (int i = 7; i >= 0; i--) out.push_back(char((x >> (8 * i)) & 0xff));
}

KeyOrder parse_key_order(const std::string& name) {
  if (name == "vid_first") return KeyOrder::VidFirst;
  if (name == "type_first") return KeyOrder::TypeFirst;
  throw std::runtime_error("nebula: key_order must be vid_first or type_first, got '" + name + "'");
}

const char* key_order_name(KeyOrder o) { return o == KeyOrder::VidFirst ? "vid_first" : "type_first"; }

std::string Keys::vertex_key(int tag_id, uint64_t vid) const {
  std::string k = "v";
  if (order == KeyOrder::VidFirst) {
    put_u64be(k, vid);
    put_u32be(k, uint32_t(tag_id));
  } else {
    put_u32be(k, uint32_t(tag_id));
    put_u64be(k, vid);
  }
  return k;
}

std::string Keys::edge_prefix(int etype_id, bool out, uint64_t src) const {
  std::string k = "e";
  uint32_t et = uint32_t(etype_id) | (out ? 0u : 0x80000000u);
  if (order == KeyOrder::VidFirst) {
    put_u64be(k, src);
    put_u32be(k, et);
  } else {
    put_u32be(k, et);
    put_u64be(k, src);
  }
  return k;
}

std::string Keys::edge_key(int etype_id, bool out, uint64_t src, uint64_t dst) const {
  std::string k = edge_prefix(etype_id, out, src);
  put_u64be(k, dst);
  return k;
}

std::string Keys::edge_key_dated(int etype_id, bool out, uint64_t src, int64_t date, uint64_t dst) const {
  std::string k = edge_prefix(etype_id, out, src);
  put_u64be(k, uint64_t(date));                  // non-negative ms: BE bytes sort numerically
  put_u64be(k, dst);
  return k;
}

std::string Keys::vertex_subrow_key(int tag_id, uint64_t vid, const std::string& gname) const {
  std::string k = vertex_key(tag_id, vid);
  k += gname;
  return k;
}

uint32_t Keys::type_field(std::string_view key) const {
  size_t at = order == KeyOrder::VidFirst ? 9 : 1;   // after the tag byte, and the 8-byte vid when it leads
  return (uint32_t(uint8_t(key[at])) << 24) | (uint32_t(uint8_t(key[at + 1])) << 16) |
         (uint32_t(uint8_t(key[at + 2])) << 8) | uint32_t(uint8_t(key[at + 3]));
}

uint64_t edge_dst(std::string_view key) {
  uint64_t x = 0;
  for (size_t i = key.size() - 8; i < key.size(); i++) x = (x << 8) | uint8_t(key[i]);
  return x;
}


}  // namespace evalcpp::nebula
