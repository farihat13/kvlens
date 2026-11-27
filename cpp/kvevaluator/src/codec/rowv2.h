// nebula's RowV2 property row, positional, byte-identical to Python nebula._rowv2_encode:
//   0x00 header | absent bitmap over the DECLARED columns (bit set = absent) | one 8-byte slot
//   per PRESENT column in declared order (int/date: i64 BE; str/multi: u32 offset, u32 length
//   BE into the variable area) | the variable area (str bytes; a multi = uvarint count + per
//   element uvarint length + bytes) | `timestamp` zero bytes (NebulaSizing.row_timestamp).
// Decoding is by slot: a column's slot index is its rank among the present columns, counted per read
// over the columns before it, so one cell costs O(cols) after the header (cols = the label's declared
// props, <= 9 here) -- nothing else is parsed.
#pragma once
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

#include "formats/rows.h"
#include "value/value.h"

namespace evalcpp {

// one row of `rows` (a RowSource) under the declared columns
std::string rowv2_encode(const std::vector<ColDecl>& cols, const RowSource& rows, size_t row, int timestamp_bytes);
// the byte length of the row starting at raw[at]: a row is self-delimiting given its columns and
// timestamp width (header + bitmap + slots + the var area the slots span + timestamp)
size_t rowv2_size(const std::vector<ColDecl>& cols, std::string_view raw, size_t at, int timestamp_bytes);

class RowV2 {
 public:
  RowV2(const std::vector<ColDecl>* cols, std::string_view raw);   // parses the header only
  bool present(size_t col) const;
  Value cell(size_t col) const;                                     // null if absent
  size_t cell_bytes(size_t col) const;      // the field's framed bytes (slot + var span); 0 absent
  Value row() const;                                                // present cells only (counted as one row)
  int col_index(const std::string& name) const;                     // -1 if not a column
 private:
  Value cell_at(size_t col) const;                                  // cell() without the count
  const std::vector<ColDecl>* cols_;
  std::string_view raw_;
  size_t nb_ = 0;        // bitmap bytes
  size_t var0_ = 0;      // where the variable area starts
};

}  // namespace evalcpp
