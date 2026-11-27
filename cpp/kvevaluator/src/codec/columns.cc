#include "codec/columns.h"
#include "codec/decode_counts.h"

#include <stdexcept>

#include "codec/varint.h"

namespace evalcpp::columns {

static bool var_col(const ColDecl& c) { return c.multi || stringlike(c); }

// a multi cell's blob: uvarint count + per element uvarint length + bytes
static void put_multi(std::string& b, const RowSource& rows, size_t r, size_t c) {
  size_t cnt = rows.multi_count(r, c);
  put_uvarint(b, cnt);
  for (size_t k = 0; k < cnt; k++) {
    std::string_view u = rows.multi_at(r, c, k);
    put_uvarint(b, u.size());
    b.append(u.data(), u.size());
  }
}

void encode(std::string& out, const std::vector<ColDecl>& cols, const RowSource& rows) {
  size_t n = rows.size();
  std::string bitmap((cols.size() * n + 7) / 8, '\0');   // one bitmap over cols x n, column-major (bit c*n + i)
  for (size_t c = 0; c < cols.size(); c++)
    for (size_t i = 0; i < n; i++)
      if (!rows.present(i, c)) bitmap[(c * n + i) / 8] |= char(1 << ((c * n + i) % 8));
  out += bitmap;
  for (size_t c = 0; c < cols.size(); c++) {
    std::vector<size_t> cells;
    for (size_t i = 0; i < n; i++)
      if (rows.present(i, c)) cells.push_back(i);
    if (var_col(cols[c])) {
      std::string offs, blob;
      for (size_t i : cells) {
        if (cols[c].multi) put_multi(blob, rows, i, c);
        else { std::string_view u = rows.str_at(i, c); blob.append(u.data(), u.size()); }
        put_u32be(offs, uint32_t(blob.size()));
      }
      out += offs;
      out += blob;
    } else {
      for (size_t i : cells) put_u64be(out, uint64_t(rows.int_at(i, c)));
    }
  }
}

namespace {
class OneRow : public RowSource {   // row `r` of another source, as a source of one row
 public:
  OneRow(const RowSource& rows, size_t r) : rows_(rows), r_(r) {}
  size_t size() const override { return 1; }
  uint64_t key(size_t) const override { return rows_.key(r_); }
  bool present(size_t, size_t col) const override { return rows_.present(r_, col); }
  int64_t int_at(size_t, size_t col) const override { return rows_.int_at(r_, col); }
  std::string_view str_at(size_t, size_t col) const override { return rows_.str_at(r_, col); }
  size_t multi_count(size_t, size_t col) const override { return rows_.multi_count(r_, col); }
  std::string_view multi_at(size_t, size_t col, size_t k) const override { return rows_.multi_at(r_, col, k); }
 private:
  const RowSource& rows_;
  size_t r_;
};
}  // namespace

void encode_row(std::string& out, const std::vector<ColDecl>& cols, const RowSource& rows, size_t row) {
  encode(out, cols, OneRow(rows, row));
}

std::string encode_cell(const ColDecl& col, const Value& v) {
  std::string out;
  if (v.is_null()) return out;
  if (col.multi) {
    const ValueList& items = v.as_list();
    put_uvarint(out, items.size());
    for (auto& e : items) {
      const std::string& s = e.as_str();
      put_uvarint(out, s.size());
      out += s;
    }
  } else if (stringlike(col)) {
    out = string_payload(col, v);
  } else {
    put_u64be(out, uint64_t(v.as_int()));
  }
  return out;
}

static Value decode_multi(std::string_view b) {
  size_t i = 0;
  size_t cnt = size_t(get_uvarint(b, i));
  ValueList items;
  for (size_t k = 0; k < cnt; k++) {
    size_t len = size_t(get_uvarint(b, i));
    if (i + len > b.size()) throw std::runtime_error("columns: truncated multi cell");
    items.push_back(Value(std::string(b.substr(i, len))));
    i += len;
  }
  return Value::list(std::move(items));
}

static Value cell_of(const ColDecl& col, std::string_view raw) {
  if (col.multi) return decode_multi(raw);
  if (stringlike(col)) return string_value(col, raw);
  if (raw.size() != 8) throw std::runtime_error("columns: an int cell is not 8 bytes");
  return Value(int64_t(get_u64be(raw, 0)));
}

Value decode_cell(const ColDecl& col, std::string_view raw) {
  DecodeCounts& d = decode_counts();
  d.cells++; d.fields++; d.bytes += raw.size();
  return cell_of(col, raw);
}

size_t payload_of(const ColDecl& col, const Value& v) { return encode_cell(col, v).size(); }

View::View(const std::vector<ColDecl>* cols, std::string_view raw, size_t at, size_t n) { reset(cols, raw, at, n); }

void View::reset(const std::vector<ColDecl>* cols, std::string_view raw, size_t at, size_t n) {
  cols_ = cols;
  raw_ = raw;
  at_ = at;
  n_ = n;
  data_.clear();
  present_.clear();
  size_t nb = (cols->size() * n + 7) / 8, bm = at, i = at + nb;   // the one bitmap, then the columns' cells
  if (i > raw.size()) throw std::runtime_error("columns: truncated bitmap");
  rank_.resize(cols->size());
  for (size_t c = 0; c < cols->size(); c++) {
    std::vector<int32_t>& rk = rank_[c];
    rk.resize(n);
    int32_t k = 0;
    for (size_t r = 0; r < n; r++) {
      size_t b = c * n + r;
      rk[r] = (uint8_t(raw[bm + b / 8]) >> (b % 8)) & 1 ? -1 : k++;
    }
    data_.push_back(i);
    present_.push_back(size_t(k));
    if (var_col((*cols)[c])) {
      size_t last = k ? get_u32be(raw, i + 4 * (size_t(k) - 1)) : 0;
      i += 4 * size_t(k) + last;
    } else {
      i += 8 * size_t(k);
    }
    if (i > raw.size()) throw std::runtime_error("columns: truncated column");
  }
  end_ = i;
  decode_counts().bytes += end_ - at_;   // the block, once per parse (a list or chunk that holds it counts itself instead)
}

int View::col_index(const std::string& name) const {
  for (size_t c = 0; c < cols_->size(); c++)
    if ((*cols_)[c].name == name) return int(c);
  return -1;
}

std::string_view View::span(size_t col, size_t row) const {
  size_t r = size_t(rank_[col][row]), d = data_[col], blob = d + 4 * present_[col];
  size_t start = r == 0 ? 0 : get_u32be(raw_, d + 4 * (r - 1));
  size_t end = get_u32be(raw_, d + 4 * r);
  return raw_.substr(blob + start, end - start);
}

Value View::cell(size_t col, size_t row) const {
  if (rank_[col][row] < 0) return Value();
  DecodeCounts& d = decode_counts();
  d.cells++; d.fields++;
  return cell_at(col, row);
}

Value View::cell_at(size_t col, size_t row) const {
  if (rank_[col][row] < 0) return Value();
  const ColDecl& c = (*cols_)[col];
  if (var_col(c)) return cell_of(c, span(col, row));
  return Value(int64_t(get_u64be(raw_, data_[col] + 8 * size_t(rank_[col][row]))));
}

Value View::row(size_t r) const {
  RowFields f;
  for (size_t c = 0; c < cols_->size(); c++)
    if (present(c, r)) f.emplace_back((*cols_)[c].name, cell_at(c, r));
  DecodeCounts& d = decode_counts();
  d.rows++; d.fields += f.size();
  return Value::row(std::move(f));
}

size_t View::payload_bytes(size_t col, size_t row) const {
  if (rank_[col][row] < 0) return 0;
  return var_col((*cols_)[col]) ? span(col, row).size() : 8;
}

size_t View::cell_bytes(size_t col, size_t row) const {
  if (rank_[col][row] < 0) return 0;
  return var_col((*cols_)[col]) ? 4 + span(col, row).size() : 8;
}

size_t View::payload_total() const {
  size_t t = 0;
  for (size_t c = 0; c < cols_->size(); c++)
    for (size_t r = 0; r < n_; r++) t += payload_bytes(c, r);
  return t;
}

}  // namespace evalcpp::columns
