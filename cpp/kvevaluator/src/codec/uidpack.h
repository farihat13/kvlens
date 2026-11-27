// Dgraph's UidPack posting-list encoding (codec/codec.go, BlockSize 256), byte-identical to
// Python formats/dgraph.py uidpack_encode: sorted uids cut into blocks (256 uids, or the next
// uid not sharing the top 32 bits); per block Base (8B BE, the first uid) + NumUids (4B BE), then
// the n-1 intra-block deltas group-varint coded four at a time (one tag byte per group, a
// 2-bit width-1 code per slot, values little-endian; the final partial group padded with 1-byte zeros).
// `ordered = false` (a date-sorted hybrid adjacency) codes every uid's low 32 bits at its own
// width instead of deltas: the compression is forfeited, which is that layout's real price.
#pragma once
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace evalcpp {

void uidpack_encode(std::string& out, const std::vector<uint64_t>& uids, bool ordered = true);
// n_total < 0: read blocks to the end of the buffer (a flat index posting); else exactly n_total
// uids (an adjacency, whose facet array follows). Advances i.
std::vector<uint64_t> uidpack_decode(std::string_view b, size_t& i, int64_t n_total, bool ordered = true);
// as uidpack_decode, also reporting each uid's ATTRIBUTABLE stored width (its own code
// bytes; 8 for an ordered block base) into `widths` when non-null -- the KV-amp used
// channel's per-uid decomposition; shared framing (count u32s, group tags) is unattributed
std::vector<uint64_t> uidpack_decode_widths(std::string_view b, size_t& i, int64_t n_total, bool ordered,
                                            std::vector<uint8_t>* widths);

}  // namespace evalcpp
