// Flat ingest buffers: rows of one label as columns in arenas, edges as (src, dst)
// pairs -- no per-row objects, so an sf30 load fits in RAM. Byte-identical output to the
// Value-based path: the chunk encoders read every row through RowSource.
#pragma once
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

#include "formats/rows.h"
#include "value/value.h"

namespace evalcpp {

// Rows of one label (or the prop rows of one edge type), appended in arrival order, read in
// sorted-key order through a permutation.
class RowBuffer {
 public:
  explicit RowBuffer(const std::vector<ColDecl>& cols);
  // key = uid (vertex rows) or the ordinal (edge-prop rows); est = the chunker's logical size
  void append(uint64_t key, const Value& props, uint32_t est);
  size_t size() const { return keys_.size(); }
  uint64_t key(size_t i) const { return keys_[i]; }
  uint32_t est(size_t i) const { return est_[i]; }
  bool present(size_t row, size_t col) const { return present_[col][row]; }
  int64_t int_at(size_t row, size_t col) const { return ints_[col][row]; }
  std::string_view str_at(size_t row, size_t col) const;
  size_t multi_count(size_t row, size_t col) const;
  std::string_view multi_at(size_t row, size_t col, size_t k) const;
  uint64_t bytes() const;                     // buffer memory, for the ingest log
  void clear();

 private:
  struct Span { uint64_t start = 0, len = 0; };
  const std::vector<ColDecl>& cols_;
  std::vector<uint64_t> keys_;
  std::vector<uint32_t> est_;
  std::vector<std::vector<uint8_t>> present_;   // per column, per row
  std::vector<std::vector<int64_t>> ints_;      // int/date columns (0 where absent)
  std::vector<std::vector<Span>> strs_;         // str columns: spans into arena_
  std::vector<std::vector<Span>> multi_rows_;   // multi columns: span into items_ (start, count)
  std::vector<Span> items_;                     // multi items: spans into arena_
  std::string arena_;
};

// A window [begin, end) of a RowBuffer in permuted order, as the chunk encoder reads it.
class BufferRows : public RowSource {
 public:
  BufferRows(const RowBuffer& b, const std::vector<uint32_t>& perm, size_t begin, size_t end)
      : b_(b), perm_(perm), begin_(begin), end_(end) {}
  size_t size() const override { return end_ - begin_; }
  uint64_t key(size_t row) const override { return b_.key(perm_[begin_ + row]); }
  bool present(size_t row, size_t col) const override { return b_.present(perm_[begin_ + row], col); }
  int64_t int_at(size_t row, size_t col) const override { return b_.int_at(perm_[begin_ + row], col); }
  std::string_view str_at(size_t row, size_t col) const override { return b_.str_at(perm_[begin_ + row], col); }
  size_t multi_count(size_t row, size_t col) const override { return b_.multi_count(perm_[begin_ + row], col); }
  std::string_view multi_at(size_t row, size_t col, size_t k) const override { return b_.multi_at(perm_[begin_ + row], col, k); }
 private:
  const RowBuffer& b_;
  const std::vector<uint32_t>& perm_;
  size_t begin_, end_;
};

// Edges of one (label, etype, direction): parallel src/dst vectors, sorted through a permutation.
// `date` is an OPTIONAL third column (a date-keyed reverse key would ride it; every
// other format leaves it empty): when present, dated_perm orders by (owner, date, other) --
// the byte order of a key that carries the date between src and dst. The field is the SORT
// KEY (an edge property in the key), NOT nebula's native rank (the multi-edge disambiguator,
// which the model byte-accounts out-of-store and never materializes).
struct EdgeBuffer {
  std::vector<uint64_t> src, dst;
  std::vector<int64_t> date;
  void append(uint64_t s, uint64_t d) { src.push_back(s); dst.push_back(d); }
  size_t size() const { return src.size(); }
  std::vector<uint32_t> sorted_perm(bool by_dst = false) const;    // by (src, dst), or (dst, src)
  std::vector<uint32_t> dated_perm(bool by_dst) const;             // by (owner, date, other)
};

}  // namespace evalcpp
