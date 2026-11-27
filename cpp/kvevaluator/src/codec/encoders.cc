#include "codec/encoders.h"

#include <zstd.h>

#include <chrono>
#include <stdexcept>

#include "codec/timers.h"
#include "codec/varint.h"

namespace evalcpp {

const std::vector<std::string>& ChunkCodecs::uid_codec_names() {
  static const std::vector<std::string> n = {"delta_varint", "for_bitpack"};
  return n;
}
const std::vector<std::string>& ChunkCodecs::str_codec_names() {
  static const std::vector<std::string> n = {"raw_offsets", "zstd_column"};
  return n;
}

const std::vector<std::string>& ChunkCodecs::layout_names() {
  static const std::vector<std::string> n = {"plain"};
  return n;
}

ChunkCodecs ChunkCodecs::from_names(const std::string& uids, const std::string& strings, const std::string& layout) {
  ChunkCodecs c;
  if (layout == "plain") c.layout = Layout::Plain;
  else throw std::runtime_error("unknown chunk layout '" + layout + "' (known: plain)");
  if (uids == "delta_varint") c.uids = UidCodec::DeltaVarint;
  else if (uids == "for_bitpack") c.uids = UidCodec::ForBitpack;
  else throw std::runtime_error("unknown uid encoder '" + uids + "' (known: delta_varint, for_bitpack)");
  if (strings == "raw_offsets") c.strings = StrCodec::RawOffsets;
  else if (strings == "zstd_column") c.strings = StrCodec::ZstdColumn;
  else throw std::runtime_error("unknown string encoder '" + strings + "' (known: raw_offsets, zstd_column)");
  return c;
}

// --- for_bitpack: uvarint(first), then u8 bit width, then the n-1 deltas packed LSB-first ---

static int bit_width(uint64_t x) {
  int b = 0;
  while (x) { b++; x >>= 1; }
  return b;
}

void encode_uids(std::string& out, UidCodec c, const std::vector<uint64_t>& sorted) {
  if (c == UidCodec::DeltaVarint) { put_deltas(out, sorted); return; }
  if (sorted.empty()) return;
  put_uvarint(out, sorted[0]);
  if (sorted.size() == 1) return;
  uint64_t maxd = 0;
  for (size_t k = 1; k < sorted.size(); k++) maxd = std::max(maxd, sorted[k] - sorted[k - 1]);
  int bits = bit_width(maxd);
  out.push_back(char(bits));
  if (bits == 0) return;
  size_t nbits = (sorted.size() - 1) * size_t(bits);
  size_t start = out.size();
  out.resize(start + (nbits + 7) / 8, '\0');
  size_t pos = 0;
  for (size_t k = 1; k < sorted.size(); k++) {
    uint64_t d = sorted[k] - sorted[k - 1];
    for (int b = 0; b < bits; b++, pos++)
      if ((d >> b) & 1) out[start + pos / 8] |= char(1 << (pos % 8));
  }
}

std::vector<uint64_t> decode_uids(UidCodec c, std::string_view b, size_t& i, size_t n) {
  if (c == UidCodec::DeltaVarint) return get_deltas(b, i, n);
  std::vector<uint64_t> v;
  if (n == 0) return v;
  v.reserve(n);
  uint64_t cur = get_uvarint(b, i);
  v.push_back(cur);
  if (n == 1) return v;
  if (i >= b.size()) throw std::runtime_error("for_bitpack: truncated");
  int bits = uint8_t(b[i++]);
  if (bits == 0) {
    for (size_t k = 1; k < n; k++) v.push_back(cur);
    return v;
  }
  size_t nbytes = ((n - 1) * size_t(bits) + 7) / 8;
  if (i + nbytes > b.size()) throw std::runtime_error("for_bitpack: truncated deltas");
  // a 64-bit window over the packed bytes: refill a byte at a time, take `bits` per value
  const uint8_t* p = reinterpret_cast<const uint8_t*>(b.data()) + i;
  const uint8_t* end = p + nbytes;
  uint64_t window = 0;
  int have = 0;
  const uint64_t mask = bits == 64 ? ~uint64_t(0) : ((uint64_t(1) << bits) - 1);
  for (size_t k = 1; k < n; k++) {
    uint64_t d;
    if (bits <= 56) {
      while (have < bits && p < end) { window |= uint64_t(*p++) << have; have += 8; }
      d = window & mask;
      window >>= bits;
      have -= bits;
    } else {                       // wide deltas (rare): assemble bit by bit
      d = 0;
      for (int bb = 0; bb < bits; bb++) {
        if (have == 0 && p < end) { window = *p++; have = 8; }
        d |= (window & 1) << bb;
        window >>= 1;
        have--;
      }
    }
    cur += d;
    v.push_back(cur);
  }
  i += nbytes;
  return v;
}

// --- zstd_column: uvarint(raw_len) + uvarint(comp_len) + zstd bytes ------------------------

void encode_str_column(std::string& out, StrCodec c, std::string_view raw) {
  if (c == StrCodec::RawOffsets) { out.append(raw.data(), raw.size()); return; }
  put_uvarint(out, raw.size());
  if (raw.empty()) { put_uvarint(out, 0); return; }
  std::string comp(ZSTD_compressBound(raw.size()), '\0');
  size_t n = ZSTD_compress(comp.data(), comp.size(), raw.data(), raw.size(), 3);
  if (ZSTD_isError(n)) throw std::runtime_error(std::string("zstd: ") + ZSTD_getErrorName(n));
  put_uvarint(out, n);
  out.append(comp.data(), n);
}

size_t str_column_size(StrCodec c, std::string_view b, size_t i, size_t k) {
  if (c == StrCodec::RawOffsets) {
    size_t last = k ? get_u32be(b, i + 4 * (k - 1)) : 0;
    return 4 * k + last;
  }
  size_t j = i;
  get_uvarint(b, j);
  size_t comp = size_t(get_uvarint(b, j));
  return (j - i) + comp;
}

std::string_view str_column_raw(StrCodec c, std::string_view b, size_t i, size_t k, std::string& owned) {
  if (c == StrCodec::RawOffsets) return b.substr(i, str_column_size(c, b, i, k));
  bool timed = decode_timing_on().load(std::memory_order_relaxed);   // the codec clocks: only when the run asks
  auto t0 = timed ? std::chrono::steady_clock::now() : std::chrono::steady_clock::time_point();
  size_t j = i;
  size_t raw_len = size_t(get_uvarint(b, j));
  size_t comp = size_t(get_uvarint(b, j));
  owned.assign(raw_len, '\0');
  if (raw_len) {
    size_t n = ZSTD_decompress(owned.data(), raw_len, b.data() + j, comp);
    if (ZSTD_isError(n) || n != raw_len) throw std::runtime_error("zstd_column: decompress failed");
  }
  if (timed) codec_stats().decompress_ns += std::chrono::duration_cast<std::chrono::nanoseconds>(
                                                std::chrono::steady_clock::now() - t0).count();
  return owned;
}

}  // namespace evalcpp
