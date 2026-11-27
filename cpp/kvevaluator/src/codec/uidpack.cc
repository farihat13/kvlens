#include "codec/uidpack.h"

#include <stdexcept>

namespace evalcpp {

static constexpr uint64_t kMsb32 = 0xffffffff00000000ull;   // codec.go bitMask
static constexpr size_t kBlock = 256;

static unsigned width_of(uint64_t x) {   // group-varint width code -> 1..4 bytes
  if (x < 0x100) return 1;
  if (x < 0x10000) return 2;
  if (x < 0x1000000) return 3;
  return 4;
}

static void put_u64be(std::string& out, uint64_t x) {
  for (int i = 7; i >= 0; i--) out.push_back(char((x >> (8 * i)) & 0xff));
}
static void put_u32be(std::string& out, uint32_t x) {
  for (int i = 3; i >= 0; i--) out.push_back(char((x >> (8 * i)) & 0xff));
}

// codes four at a time: tag byte (width-1 per slot, slot 0 in the low bits), then each code
// little-endian at its width; the final partial group is padded with width-1 zeros
static void groups_encode(std::string& out, const std::vector<uint64_t>& codes) {
  for (size_t g = 0; g < codes.size(); g += 4) {
    uint64_t grp[4] = {0, 0, 0, 0};
    unsigned w[4] = {1, 1, 1, 1};
    for (size_t s = 0; s < 4 && g + s < codes.size(); s++) { grp[s] = codes[g + s]; w[s] = width_of(grp[s]); }
    unsigned tag = 0;
    for (size_t s = 0; s < 4; s++) tag |= (w[s] - 1) << (2 * s);
    out.push_back(char(tag));
    for (size_t s = 0; s < 4; s++)
      for (unsigned k = 0; k < w[s]; k++) out.push_back(char((grp[s] >> (8 * k)) & 0xff));
  }
}

static std::vector<uint64_t> groups_decode(std::string_view b, size_t& i, size_t n) {
  std::vector<uint64_t> codes;
  while (codes.size() < n) {
    if (i >= b.size()) throw std::runtime_error("uidpack: truncated group");
    unsigned tag = uint8_t(b[i++]);
    for (size_t s = 0; s < 4; s++) {
      unsigned w = ((tag >> (2 * s)) & 3) + 1;
      if (i + w > b.size()) throw std::runtime_error("uidpack: truncated code");
      uint64_t c = 0;
      for (unsigned k = 0; k < w; k++) c |= uint64_t(uint8_t(b[i + k])) << (8 * k);
      i += w;
      codes.push_back(c);
    }
  }
  codes.resize(n);   // the padding of the last group is dropped
  return codes;
}

void uidpack_encode(std::string& out, const std::vector<uint64_t>& uids, bool ordered) {
  if (uids.empty()) return;
  // blocks exactly as Encoder.Add cuts them: 256 uids, or a change of the top 32 bits
  size_t start = 0;
  while (start < uids.size()) {
    size_t end = start + 1;
    while (end < uids.size() && end - start < kBlock && (uids[end] & kMsb32) == (uids[end - 1] & kMsb32)) end++;
    put_u64be(out, uids[start]);
    put_u32be(out, uint32_t(end - start));
    std::vector<uint64_t> codes;
    if (ordered) {
      for (size_t j = start + 1; j < end; j++) codes.push_back(uids[j] - uids[j - 1]);
    } else {
      for (size_t j = start; j < end; j++) codes.push_back(uids[j] & 0xFFFFFFFFull);
    }
    groups_encode(out, codes);
    start = end;
  }
}

std::vector<uint64_t> uidpack_decode(std::string_view b, size_t& i, int64_t n_total, bool ordered) {
  return uidpack_decode_widths(b, i, n_total, ordered, nullptr);
}

// per-uid ATTRIBUTABLE byte widths for the KV-amp used channel: a uid's own code bytes
// (1-4, group varint) -- or 8 for an ordered block's base (it rides the header). Block
// count u32s and group tag bytes are shared FRAMING, deliberately unattributed (they are
// the codec's overhead = amplification for a subset read).
std::vector<uint64_t> uidpack_decode_widths(std::string_view b, size_t& i, int64_t n_total, bool ordered,
                                            std::vector<uint8_t>* widths) {
  std::vector<uint64_t> uids;
  while (n_total < 0 ? i < b.size() : int64_t(uids.size()) < n_total) {
    if (i + 12 > b.size()) throw std::runtime_error("uidpack: truncated block header");
    uint64_t base = 0;
    for (size_t k = 0; k < 8; k++) base = (base << 8) | uint8_t(b[i + k]);
    uint32_t n = 0;
    for (size_t k = 8; k < 12; k++) n = (n << 8) | uint8_t(b[i + k]);
    i += 12;
    if (ordered) {
      uids.push_back(base);
      if (widths) widths->push_back(8);
      uint64_t cur = base;
      for (uint64_t c : groups_decode(b, i, n - 1)) {
        cur += c;
        uids.push_back(cur);
        if (widths) widths->push_back(uint8_t(width_of(c)));
      }
    } else {
      uint64_t top = base & kMsb32;
      for (uint64_t c : groups_decode(b, i, n)) {
        uids.push_back(top | c);
        if (widths) widths->push_back(uint8_t(width_of(c)));
      }
    }
  }
  return uids;
}

}  // namespace evalcpp
