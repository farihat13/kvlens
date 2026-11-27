// The neutral row surface every format's value codec reads: a column declaration and a
// RowSource (rows however they are stored -- flat ingest buffers, or Value rows in tests).
// Lives outside any format directory so ingest/ and every format share one definition.
#pragma once
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

#include "value/value.h"

namespace evalcpp {

struct ColDecl {
  std::string name;
  std::string type;   // "int" | "date" | "str" | "bytes"
  bool multi = false;
};

// A str or a bytes column: both are stored as their raw bytes behind a length; only the value kind a read returns
// differs. The one decision every codec asks, so a str site and a bytes site cannot drift apart.
inline bool stringlike(const ColDecl& c) { return c.type == "str" || c.type == "bytes"; }
// the value a stringlike cell decodes to: a bytes value for a bytes column, else a str
inline Value string_value(const ColDecl& c, std::string_view raw) {
  return c.type == "bytes" ? Value::bytes(std::string(raw)) : Value(std::string(raw));
}
// the raw bytes a stringlike cell stores; a str in a bytes column (or the reverse) is refused: it means an input that
// predates the bytes type (a records cache or a params file written before the loader yielded bytes)
const std::string& string_payload(const ColDecl& c, const Value& v);

struct RowSource {
  virtual ~RowSource() = default;
  virtual size_t size() const = 0;
  virtual uint64_t key(size_t row) const = 0;                                 // uid or ordinal
  virtual bool present(size_t row, size_t col) const = 0;
  virtual int64_t int_at(size_t row, size_t col) const = 0;
  virtual std::string_view str_at(size_t row, size_t col) const = 0;
  virtual size_t multi_count(size_t row, size_t col) const = 0;
  virtual std::string_view multi_at(size_t row, size_t col, size_t k) const = 0;
};

// a subset of another RowSource's columns (column c here = column at[c] there): one group's row out of a
// label's ingest buffer (meta's grouped props)
class ColumnSubset : public RowSource {
 public:
  ColumnSubset(const RowSource& base, const std::vector<size_t>& at) : b_(base), at_(at) {}
  size_t size() const override { return b_.size(); }
  uint64_t key(size_t row) const override { return b_.key(row); }
  bool present(size_t row, size_t col) const override { return b_.present(row, at_[col]); }
  int64_t int_at(size_t row, size_t col) const override { return b_.int_at(row, at_[col]); }
  std::string_view str_at(size_t row, size_t col) const override { return b_.str_at(row, at_[col]); }
  size_t multi_count(size_t row, size_t col) const override { return b_.multi_count(row, at_[col]); }
  std::string_view multi_at(size_t row, size_t col, size_t k) const override { return b_.multi_at(row, at_[col], k); }
 private:
  const RowSource& b_;
  const std::vector<size_t>& at_;
};

struct PropRow {
  uint64_t key;         // uid (props chunk) or ordinal (edge-prop row)
  const Value* props;   // a row Value: name -> value; absent/null cells are not stored
};

// Value rows as a RowSource (tests, fixtures, single-row encodes).
class ValueRows : public RowSource {
 public:
  ValueRows(const std::vector<ColDecl>& cols, const std::vector<PropRow>& rows) : cols_(cols), rows_(rows) {}
  size_t size() const override { return rows_.size(); }
  uint64_t key(size_t row) const override { return rows_[row].key; }
  bool present(size_t row, size_t col) const override;
  int64_t int_at(size_t row, size_t col) const override;
  std::string_view str_at(size_t row, size_t col) const override;
  size_t multi_count(size_t row, size_t col) const override;
  std::string_view multi_at(size_t row, size_t col, size_t k) const override;
 private:
  const Value* cell(size_t row, size_t col) const;
  const std::vector<ColDecl>& cols_;
  const std::vector<PropRow>& rows_;
};

}  // namespace evalcpp
