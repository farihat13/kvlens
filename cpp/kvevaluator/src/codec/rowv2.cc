#include "codec/rowv2.h"
#include "codec/decode_counts.h"

#include <algorithm>
#include <stdexcept>

#include "codec/varint.h"

namespace evalcpp {

static void put_i64be(std::string& out, int64_t v) {
  uint64_t x = uint64_t(v);
  for (int i = 7; i >= 0; i--) out.push_back(char((x >> (8 * i)) & 0xff));
}

std::string rowv2_encode(const std::vector<ColDecl>& cols, const RowSource& rows, size_t row, int timestamp_bytes) {
  std::string bitmap((cols.size() + 7) / 8, '\0');
  std::string fixed, var;
  for (size_t c = 0; c < cols.size(); c++) {
    if (!rows.present(row, c)) { bitmap[c / 8] |= char(1 << (c % 8)); continue; }
    const ColDecl& col = cols[c];
    if (col.multi) {
      std::string blob;
      size_t cnt = rows.multi_count(row, c);
      put_uvarint(blob, cnt);
      for (size_t k = 0; k < cnt; k++) {
        std::string_view u = rows.multi_at(row, c, k);
        put_uvarint(blob, u.size());
        blob.append(u.data(), u.size());
      }
      put_u32be(fixed, uint32_t(var.size()));
      put_u32be(fixed, uint32_t(blob.size()));
      var += blob;
    } else if (stringlike(col)) {
      std::string_view u = rows.str_at(row, c);
      put_u32be(fixed, uint32_t(var.size()));
      put_u32be(fixed, uint32_t(u.size()));
      var.append(u.data(), u.size());
    } else {
      put_i64be(fixed, rows.int_at(row, c));
    }
  }
  std::string out = "\0";
  out.resize(1);
  out += bitmap;
  out += fixed;
  out += var;
  out.append(size_t(timestamp_bytes), '\0');
  return out;
}

size_t rowv2_size(const std::vector<ColDecl>& cols, std::string_view raw, size_t at, int timestamp_bytes) {
  size_t nb = (cols.size() + 7) / 8;
  if (raw.size() < at + 1 + nb) throw std::runtime_error("rowv2: truncated header");
  size_t slot = at + 1 + nb, var = 0;
  for (size_t c = 0; c < cols.size(); c++) {
    if ((uint8_t(raw[at + 1 + c / 8]) >> (c % 8)) & 1) continue;   // absent: no slot
    if (raw.size() < slot + 8) throw std::runtime_error("rowv2: truncated slots");
    if (cols[c].multi || stringlike(cols[c])) var = std::max(var, size_t(get_u32be(raw, slot)) + get_u32be(raw, slot + 4));
    slot += 8;
  }
  size_t end = slot + var + size_t(timestamp_bytes);
  if (raw.size() < end) throw std::runtime_error("rowv2: truncated row");
  return end - at;
}

RowV2::RowV2(const std::vector<ColDecl>* cols, std::string_view raw) : cols_(cols), raw_(raw) {
  nb_ = (cols->size() + 7) / 8;
  if (raw.size() < 1 + nb_) throw std::runtime_error("rowv2: truncated header");
  size_t npresent = 0;
  for (size_t c = 0; c < cols->size(); c++)
    if (!((uint8_t(raw[1 + c / 8]) >> (c % 8)) & 1)) npresent++;
  var0_ = 1 + nb_ + 8 * npresent;
  if (raw.size() < var0_) throw std::runtime_error("rowv2: truncated slots");
  decode_counts().bytes += raw.size();   // the row, once per parse
}

bool RowV2::present(size_t col) const { return !((uint8_t(raw_[1 + col / 8]) >> (col % 8)) & 1); }

int RowV2::col_index(const std::string& name) const {
  for (size_t c = 0; c < cols_->size(); c++)
    if ((*cols_)[c].name == name) return int(c);
  return -1;
}

// KV-amp: a field's FRAMED bytes in this row -- its 8-byte slot plus, for str/multi, the
// variable-area span the slot points to. 0 when absent (an absent field costs nothing).
size_t RowV2::cell_bytes(size_t col) const {
  if (!present(col)) return 0;
  const ColDecl& c = (*cols_)[col];
  if (!(c.multi || stringlike(c))) return 8;
  size_t slot = 0;
  for (size_t x = 0; x < col; x++) slot += present(x);
  size_t at = 1 + nb_ + 8 * slot;
  return 8 + get_u32be(raw_, at + 4);
}

Value RowV2::cell(size_t col) const {
  if (!present(col)) return Value();
  DecodeCounts& d = decode_counts();
  d.cells++; d.fields++;
  return cell_at(col);
}

Value RowV2::cell_at(size_t col) const {
  if (!present(col)) return Value();
  size_t slot = 0;                                   // rank among the present columns
  for (size_t c = 0; c < col; c++) slot += present(c);
  size_t at = 1 + nb_ + 8 * slot;
  const ColDecl& c = (*cols_)[col];
  if (c.multi || stringlike(c)) {
    size_t off = get_u32be(raw_, at), len = get_u32be(raw_, at + 4);
    if (var0_ + off + len > raw_.size()) throw std::runtime_error("rowv2: string slot out of range");
    std::string_view s = raw_.substr(var0_ + off, len);
    if (!c.multi) return string_value(c, s);
    size_t p = 0;
    size_t cnt = size_t(get_uvarint(s, p));
    ValueList lst;
    lst.reserve(cnt);
    for (size_t k = 0; k < cnt; k++) {
      size_t el = size_t(get_uvarint(s, p));
      lst.push_back(Value(std::string(s.substr(p, el))));
      p += el;
    }
    return Value::list(std::move(lst));
  }
  uint64_t x = 0;
  for (size_t i = 0; i < 8; i++) x = (x << 8) | uint8_t(raw_[at + i]);
  return Value(int64_t(x));
}

Value RowV2::row() const {
  RowFields f;
  for (size_t c = 0; c < cols_->size(); c++)
    if (present(c)) f.emplace_back((*cols_)[c].name, cell_at(c));
  DecodeCounts& d = decode_counts();
  d.rows++; d.fields += f.size();
  return Value::row(std::move(f));
}

}  // namespace evalcpp
