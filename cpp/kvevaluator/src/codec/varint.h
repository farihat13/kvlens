// uvarint / delta-uvarint helpers (Python store/valcodec.py uvarint + pertype_csr deltas).
#pragma once
#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace evalcpp {

inline void put_uvarint(std::string& out, uint64_t n) {
  while (true) {
    uint8_t b = n & 0x7F;
    n >>= 7;
    out.push_back(char(b | (n ? 0x80 : 0)));
    if (!n) return;
  }
}

inline uint64_t get_uvarint(std::string_view b, size_t& i) {
  uint64_t n = 0;
  int shift = 0;
  while (true) {
    if (i >= b.size()) throw std::runtime_error("uvarint: truncated");
    uint8_t c = uint8_t(b[i++]);
    n |= uint64_t(c & 0x7F) << shift;
    if (!(c & 0x80)) return n;
    shift += 7;
    if (shift > 63) throw std::runtime_error("uvarint: too long");
  }
}

inline void skip_uvarint(std::string_view b, size_t& i) {
  while (i < b.size() && (uint8_t(b[i++]) & 0x80)) {}
}

inline void put_deltas(std::string& out, const std::vector<uint64_t>& sorted) {
  uint64_t prev = 0;
  for (uint64_t u : sorted) {
    put_uvarint(out, u - prev);
    prev = u;
  }
}

inline std::vector<uint64_t> get_deltas(std::string_view b, size_t& i, size_t n) {
  std::vector<uint64_t> v;
  v.reserve(n);
  uint64_t cur = 0;
  for (size_t k = 0; k < n; k++) {
    cur += get_uvarint(b, i);
    v.push_back(cur);
  }
  return v;
}

inline void put_u32be(std::string& out, uint32_t x) {
  out.push_back(char(x >> 24)); out.push_back(char(x >> 16)); out.push_back(char(x >> 8)); out.push_back(char(x));
}
inline uint32_t get_u32be(std::string_view b, size_t i) {
  if (i + 4 > b.size()) throw std::runtime_error("u32: truncated");
  return (uint32_t(uint8_t(b[i])) << 24) | (uint32_t(uint8_t(b[i + 1])) << 16) |
         (uint32_t(uint8_t(b[i + 2])) << 8) | uint32_t(uint8_t(b[i + 3]));
}
inline void put_u64be(std::string& out, uint64_t x) {
  for (int s = 56; s >= 0; s -= 8) out.push_back(char(x >> s));
}
inline uint64_t get_u64be(std::string_view b, size_t i) {
  if (i + 8 > b.size()) throw std::runtime_error("u64: truncated");
  uint64_t x = 0;
  for (int k = 0; k < 8; k++) x = (x << 8) | uint8_t(b[i + k]);
  return x;
}
inline void put_u16be(std::string& out, uint16_t x) { out.push_back(char(x >> 8)); out.push_back(char(x)); }

}  // namespace evalcpp
