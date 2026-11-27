#include "util/base64.h"

#include <cstdint>
#include <stdexcept>

namespace evalcpp {

static const char kAlphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

std::string base64_encode(std::string_view raw) {
  std::string out;
  out.reserve((raw.size() + 2) / 3 * 4);
  size_t i = 0;
  for (; i + 3 <= raw.size(); i += 3) {
    uint32_t x = uint32_t(uint8_t(raw[i])) << 16 | uint32_t(uint8_t(raw[i + 1])) << 8 | uint8_t(raw[i + 2]);
    out += kAlphabet[x >> 18 & 63]; out += kAlphabet[x >> 12 & 63]; out += kAlphabet[x >> 6 & 63]; out += kAlphabet[x & 63];
  }
  size_t rest = raw.size() - i;
  if (rest) {
    uint32_t x = uint32_t(uint8_t(raw[i])) << 16 | (rest == 2 ? uint32_t(uint8_t(raw[i + 1])) << 8 : 0);
    out += kAlphabet[x >> 18 & 63]; out += kAlphabet[x >> 12 & 63];
    out += rest == 2 ? kAlphabet[x >> 6 & 63] : '=';
    out += '=';
  }
  return out;
}

static int sextet(char c) {
  if (c >= 'A' && c <= 'Z') return c - 'A';
  if (c >= 'a' && c <= 'z') return c - 'a' + 26;
  if (c >= '0' && c <= '9') return c - '0' + 52;
  if (c == '+') return 62;
  if (c == '/') return 63;
  return -1;
}

std::string base64_decode(std::string_view text) {
  if (text.size() % 4) throw std::runtime_error("base64: length " + std::to_string(text.size()) + " is not a multiple of 4");
  std::string out;
  out.reserve(text.size() / 4 * 3);
  for (size_t i = 0; i < text.size(); i += 4) {
    bool last = i + 4 == text.size();
    int pad = last ? (text[i + 3] == '=') + (text[i + 2] == '=') : 0;
    if (pad == 1 && text[i + 2] == '=') throw std::runtime_error("base64: misplaced padding");
    uint32_t x = 0;
    for (int k = 0; k < 4 - pad; k++) {
      int s = sextet(text[i + k]);
      if (s < 0) throw std::runtime_error("base64: a character outside the alphabet");
      x |= uint32_t(s) << (18 - 6 * k);
    }
    out += char(x >> 16 & 0xff);
    if (pad < 2) out += char(x >> 8 & 0xff);
    if (pad < 1) out += char(x & 0xff);
  }
  return out;
}

}  // namespace evalcpp
