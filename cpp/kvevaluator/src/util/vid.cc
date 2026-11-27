#include "util/vid.h"

namespace evalcpp {

std::string hex16(uint64_t u) {
  static const char* d = "0123456789abcdef";
  std::string s(16, '0');
  for (int i = 15; i >= 0; i--) {
    s[i] = d[u & 0xf];
    u >>= 4;
  }
  return s;
}

std::optional<uint64_t> uid_of(const std::string& v) {
  if (v.size() != 16) return std::nullopt;
  uint64_t u = 0;
  for (char c : v) {
    int x;
    if (c >= '0' && c <= '9') x = c - '0';
    else if (c >= 'a' && c <= 'f') x = c - 'a' + 10;
    else if (c >= 'A' && c <= 'F') x = c - 'A' + 10;
    else return std::nullopt;
    u = (u << 4) | uint64_t(x);
  }
  return u;
}

size_t utf8_length(const std::string& s) {
  size_t n = 0;
  for (unsigned char c : s)
    if ((c & 0xC0) != 0x80) n++;
  return n;
}

}  // namespace evalcpp
