// The COLUMNS block, meta-v4's one property encoding: n rows x the declared columns, column-major. Byte layout =
// a packed chunk's column body (codec/chunks.h "plain", raw_offsets) but for its one bitmap; shares no code:
//   ONE presence bitmap over cols x n bits, column-major (bit c*n + i, set = ABSENT): ceil(cols*n / 8) bytes, so an
//   n = 1 row pays ceil(cols / 8), not a byte per column; then per column its present cells
//   in row order --
//     int / date: 8 B big-endian; str: u32 END offsets (one per present cell) + the UTF-8 bytes;
//     multi: u32 END offsets + per cell (uvarint count + per element uvarint length + bytes).
// Used for every property in meta-v4: a Condensed row (n = 1, one block per row group), a Split edge record
// (n = 1, the edge's columns; 0 columns = an empty value), a Condensed list's entries (n = the list), a chunk's rows.
// A Split VERTEX record holds one present cell: its value is the cell's payload alone (encode_cell), since presence
// is the key's and the length is the value's.
// PAYLOAD vs FRAMING (the symmetry test reads both): a cell's payload is the same in every layout (int / date 8 B;
// str / multi: its bytes after the offset); everything else in a block is framing (bitmaps, offsets).
#pragma once
#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

#include "formats/rows.h"
#include "value/value.h"

namespace evalcpp::columns {

// every row of `rows` under `cols`, appended to `out`
void encode(std::string& out, const std::vector<ColDecl>& cols, const RowSource& rows);
// row `row` of `rows` alone (n = 1: a Condensed row, one group's row)
void encode_row(std::string& out, const std::vector<ColDecl>& cols, const RowSource& rows, size_t row);
// one present cell's payload (a Split vertex record): int / date 8 B BE, str its bytes, multi its blob
std::string encode_cell(const ColDecl& col, const Value& v);
Value decode_cell(const ColDecl& col, std::string_view raw);
// the payload bytes of one cell value as a block stores it (0 for null): what encode_cell writes
size_t payload_of(const ColDecl& col, const Value& v);

// A parsed block: the column layout up front (one pass over the bitmaps), each cell O(1) after that.
class View {
 public:
  // the block of `n` rows starting at raw[at]
  View(const std::vector<ColDecl>* cols, std::string_view raw, size_t at, size_t n);
  View() = default;                                        // empty until reset()
  // parse another block into this view, reusing its vectors (a reader that parses one row per vertex keeps one View)
  void reset(const std::vector<ColDecl>* cols, std::string_view raw, size_t at, size_t n);
  size_t end() const { return end_; }                      // the offset just past the block
  size_t rows() const { return n_; }
  size_t cols() const { return cols_->size(); }
  int col_index(const std::string& name) const;            // -1 if not a column
  bool present(size_t col, size_t row) const { return rank_[col][row] >= 0; }
  Value cell(size_t col, size_t row) const;                // null if absent (counted: codec/decode_counts.h)
  Value row(size_t r) const;                               // present cells only (counted as one row)
  // the cell's FRAMED bytes (its offset + payload; an int's 8): the used-bytes width a read charges; 0 absent
  size_t cell_bytes(size_t col, size_t row) const;
  size_t payload_bytes(size_t col, size_t row) const;      // the cell's payload; 0 absent
  size_t payload_total() const;                            // every present cell's payload
  size_t framing_total() const { return end_ - at_ - payload_total(); }
 private:
  Value cell_at(size_t col, size_t row) const;             // cell() without the count
  std::string_view span(size_t col, size_t row) const;     // a present str / multi cell's bytes
  const std::vector<ColDecl>* cols_ = nullptr;
  std::string_view raw_;
  size_t at_ = 0, end_ = 0, n_ = 0;
  std::vector<size_t> data_;                     // per column: where its cells (ints, or the offsets) start
  std::vector<size_t> present_;                  // per column: present cells
  std::vector<std::vector<int32_t>> rank_;       // per column, per row: its index among the present cells, -1 absent
};

}  // namespace evalcpp::columns
