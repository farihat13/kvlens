// The neutral packed-chunk codec: one owner-packed KV block, encode + lazy decode. Shared infra (no
// format owns it) -- pertype_csr uses it, and the config-driven `meta` format's packed mode reuses it
// byte-for-byte. The layout (ChunkCodecs::layout; one today):
//   plain  (Python formats/pertype_csr.py, byte-identical):
//     props = uvarint n + delta-uvarint uids + per column: absent bitmap, then int/date as
//             8B BE, str as u32 END offsets + UTF-8, multi as u32 END offsets + per-list blobs
//     adj   = uvarint nsrc + delta-uvarint srcs + uvarint degrees + u32 END offsets + per src
//             the sorted dsts as delta-uvarints + [label_bits > 0] the far-end LABEL block +
//             [prop-carrying etype] one uvarint ordinal/edge
//     ep    = uvarint n + delta-uvarint ordinals + prop columns as in a props chunk
// The LABEL block (C++ only: the Python twins store none): one code of `label_bits` bits per edge,
// in owner order, packed low bit first (codec/lists.h put_labels: ceil(edges x bits / 8) bytes). label_bits = 0
// writes nothing: today's bytes, and what every caller but Per-type CSR with far_label passes.
//     ep    = uvarint n + FoR+bitpack ordinals + prop columns as in a props chunk
// Decoding is lazy: a lookup parses the header and only the cells it is asked for.
#pragma once
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <vector>

#include "codec/encoders.h"
#include "formats/rows.h"
#include "value/value.h"

namespace evalcpp {

// the neutral row surface (formats/rows.h), under this namespace for the chunk code
using evalcpp::ColDecl;
using evalcpp::RowSource;
using evalcpp::PropRow;
using evalcpp::ValueRows;

// --- chunk-size estimates (the chunker's LOGICAL size, a contract: never bytes) ------------
uint64_t est_props(const Value& props);                    // pertype_csr._est_props
// a vertex row's estimate over its STORED columns only (the loader's `id` is not one: formats/common.h vertex_cols)
uint64_t est_vertex(const std::vector<ColDecl>& cols, const Value& props);
inline uint64_t est_adj(size_t degree, bool with_ords) { return 9 + 5 * degree + (with_ords ? 3 * degree : 0); }
inline uint64_t est_ep(const Value& props) { return 2 + est_props(props); }

// --- encoders (ingest) ----------------------------------------------------------------------
struct AdjGroup {
  uint64_t src;
  std::vector<uint64_t> dsts;   // sorted
  std::vector<uint64_t> ords;   // parallel to dsts when the etype carries props, else empty
  std::vector<uint8_t> labels;  // parallel to dsts when the chunk stores far-end label codes (label_bits > 0), else empty
};
std::string encode_props(const ChunkCodecs& cc, const std::vector<ColDecl>& cols, const RowSource& rows);
std::string encode_adj(const ChunkCodecs& cc, const std::vector<AdjGroup>& groups, bool with_ordinals,
                       unsigned label_bits = 0);
std::string encode_ep(const ChunkCodecs& cc, const std::vector<ColDecl>& cols, const RowSource& rows);
inline std::string encode_props(const ChunkCodecs& cc, const std::vector<ColDecl>& cols, const std::vector<PropRow>& rows) {
  return encode_props(cc, cols, ValueRows(cols, rows));
}
inline std::string encode_ep(const ChunkCodecs& cc, const std::vector<ColDecl>& cols, const std::vector<PropRow>& rows) {
  return encode_ep(cc, cols, ValueRows(cols, rows));
}

std::vector<uint8_t> adj_label_codes(const std::vector<AdjGroup>& groups);   // every edge's code, owner order

// --- decoders (lazy) ------------------------------------------------------------------------
// A props or ep chunk. `decode` picks the layout's implementation.
class PropsChunk {
 public:
  static std::shared_ptr<PropsChunk> decode(const ChunkCodecs* cc, const std::vector<ColDecl>* cols, std::string raw);
  virtual ~PropsChunk() = default;
  size_t size() const { return n_; }
  virtual const std::vector<uint64_t>& ids() const = 0;   // uids or ordinals (bulk; the scan path)
  virtual uint64_t first() const = 0;
  virtual uint64_t last() const = 0;
  virtual int64_t find(uint64_t id) const = 0;             // row index or -1
  int col_index(const std::string& name) const;            // -1 if not a column
  Value cell(size_t col, size_t row) const;                // null if absent (counted: codec/decode_counts.h)
  Value row(size_t r) const;                               // present cells only (counted as one row)
  // a cell's framed bytes, the used-bytes unit (the Condensed rule, columns::View::cell_bytes): a variable cell's 4 B
  // end offset + its bytes, a fixed one 8, an absent one 0. Not counted as a decode (it is the meter's, not a read)
  virtual size_t cell_bytes(size_t col, size_t row) const = 0;
  // Whole-chunk decode ahead of a scan over every row (a no-op for the plain layout, whose
  // cells are already O(1)).
  virtual void bulk() const {}
 protected:
  PropsChunk(const std::vector<ColDecl>* cols) : cols_(cols) {}
  virtual Value cell_impl(size_t col, size_t row) const = 0;   // the layout's decoder, uncounted
  const std::vector<ColDecl>* cols_;
  size_t n_ = 0;
};

class AdjChunk {
 public:
  static std::shared_ptr<AdjChunk> decode(const ChunkCodecs* cc, std::string raw, bool with_ordinals,
                                          unsigned label_bits = 0);
  virtual ~AdjChunk() = default;
  virtual size_t size() const = 0;
  virtual uint64_t first() const = 0;
  virtual uint64_t last() const = 0;
  virtual int64_t find(uint64_t src) const = 0;
  virtual uint64_t src(size_t i) const = 0;
  virtual std::vector<uint64_t> dsts(size_t i) const = 0;
  virtual const std::vector<uint64_t>& ordinals(size_t i) = 0;   // decoded once per chunk
  // owner i's far-end label codes, parallel to dsts(i); empty when the chunk stores none. Only that owner's codes
  // are unpacked (fixed width), as dsts(i) decodes only its own list
  virtual std::vector<uint8_t> labels(size_t i) const = 0;
  // the used-bytes unit (lists::View's id_bytes): owner i's entries' widths as a Condensed list codes them, parallel to
  // dsts(i) (the stored deltas', but the first id's: Condensed codes it from its vid group's base). Not counted as a
  // decode (it is the meter's). An entry with no byte width of its own (FoR+bitpack) books 0
  virtual std::vector<uint8_t> id_widths(size_t i) const = 0;
};

// Only the id column of a props chunk (the type-scan path).
std::vector<uint64_t> chunk_ids(const ChunkCodecs& cc, std::string_view raw);

}  // namespace evalcpp
