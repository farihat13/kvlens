// The swappable column encoders inside the pertype_csr chunk layout.
// The chunk LAYOUT (header, column order, CSR shape) is fixed; these decide the bytes of
//   - a sorted uid list (the id column, CSR srcs, each src's dst list): delta_varint | for_bitpack
//   - a str / multi column body (u32 end offsets + blob):              raw_offsets | zstd_column
// Swapping one touches no op and never moves a chunk boundary (the chunker's estimate is
// logical). delta_varint + raw_offsets are Python's bytes exactly.
#pragma once
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

#include "value/value.h"

namespace evalcpp {

enum class UidCodec { DeltaVarint, ForBitpack };
enum class StrCodec { RawOffsets, ZstdColumn };
// The chunk BODY layout (codec/chunks.h): plain = Python pertype_csr's bytes with
// the two encoders above swappable (the only layout).
enum class Layout { Plain };

struct ChunkCodecs {
  UidCodec uids = UidCodec::DeltaVarint;
  StrCodec strings = StrCodec::RawOffsets;
  Layout layout = Layout::Plain;
  static ChunkCodecs from_names(const std::string& uids, const std::string& strings,
                                const std::string& layout = "plain");   // throws on unknown
  static const std::vector<std::string>& uid_codec_names();
  static const std::vector<std::string>& str_codec_names();
  static const std::vector<std::string>& layout_names();
};

// --- sorted uid lists (n is known from the chunk header) ---------------------------------
void encode_uids(std::string& out, UidCodec c, const std::vector<uint64_t>& sorted);
// decodes n values starting at b[i]; advances i past them
std::vector<uint64_t> decode_uids(UidCodec c, std::string_view b, size_t& i, size_t n);

// --- str / multi column bodies -----------------------------------------------------------
// `raw` = the raw_offsets body (u32 end offsets for k cells + the concatenated blob).
void encode_str_column(std::string& out, StrCodec c, std::string_view raw);
// the encoded size of the column body at b[i] (to skip it without decoding), k = present cells
size_t str_column_size(StrCodec c, std::string_view b, size_t i, size_t k);
// the raw_offsets body at b[i] -- a view for raw_offsets, an owned decompressed copy for zstd
// (the decompress time is added to codec_stats().decompress_ns)
std::string_view str_column_raw(StrCodec c, std::string_view b, size_t i, size_t k, std::string& owned);

}  // namespace evalcpp
