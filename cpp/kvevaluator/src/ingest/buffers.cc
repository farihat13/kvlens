#include "ingest/buffers.h"

#include <algorithm>
#include <numeric>
#include <stdexcept>

namespace evalcpp {

RowBuffer::RowBuffer(const std::vector<ColDecl>& cols) : cols_(cols) {
  present_.resize(cols.size());
  ints_.resize(cols.size());
  strs_.resize(cols.size());
  multi_rows_.resize(cols.size());
}

void RowBuffer::append(uint64_t key, const Value& props, uint32_t est) {
  keys_.push_back(key);
  est_.push_back(est);
  for (size_t c = 0; c < cols_.size(); c++) {
    const ColDecl& col = cols_[c];
    const Value* v = props.find(col.name);
    bool here = v && !v->is_null();
    present_[c].push_back(here ? 1 : 0);
    if (col.multi) {
      Span s{items_.size(), 0};
      if (here) {
        for (auto& e : v->as_list()) {
          const std::string& u = e.as_str();
          items_.push_back({arena_.size(), u.size()});
          arena_ += u;
          s.len++;
        }
      }
      multi_rows_[c].push_back(s);
    } else if (stringlike(col)) {
      Span s{arena_.size(), 0};
      if (here) {
        const std::string& u = string_payload(col, *v);
        s.len = u.size();
        arena_ += u;
      }
      strs_[c].push_back(s);
    } else {
      ints_[c].push_back(here ? v->as_int() : 0);
    }
  }
}

std::string_view RowBuffer::str_at(size_t row, size_t col) const {
  const Span& s = strs_[col][row];
  return std::string_view(arena_).substr(s.start, s.len);
}

size_t RowBuffer::multi_count(size_t row, size_t col) const { return multi_rows_[col][row].len; }

std::string_view RowBuffer::multi_at(size_t row, size_t col, size_t k) const {
  const Span& r = multi_rows_[col][row];
  const Span& it = items_[r.start + k];
  return std::string_view(arena_).substr(it.start, it.len);
}

uint64_t RowBuffer::bytes() const {
  uint64_t n = arena_.size() + keys_.size() * 12 + items_.size() * 16;
  for (size_t c = 0; c < cols_.size(); c++)
    n += present_[c].size() + ints_[c].size() * 8 + strs_[c].size() * 16 + multi_rows_[c].size() * 16;
  return n;
}

void RowBuffer::clear() {
  keys_.clear(); keys_.shrink_to_fit();
  est_.clear(); est_.shrink_to_fit();
  for (auto& v : present_) { v.clear(); v.shrink_to_fit(); }
  for (auto& v : ints_) { v.clear(); v.shrink_to_fit(); }
  for (auto& v : strs_) { v.clear(); v.shrink_to_fit(); }
  for (auto& v : multi_rows_) { v.clear(); v.shrink_to_fit(); }
  items_.clear(); items_.shrink_to_fit();
  arena_.clear(); arena_.shrink_to_fit();
}

std::vector<uint32_t> EdgeBuffer::sorted_perm(bool by_dst) const {
  if (src.size() > 0xFFFFFFFFull) throw std::runtime_error("EdgeBuffer: more than 2^32 edges in one key");
  std::vector<uint32_t> p(src.size());
  std::iota(p.begin(), p.end(), 0);
  const std::vector<uint64_t>& a1 = by_dst ? dst : src;
  const std::vector<uint64_t>& a2 = by_dst ? src : dst;
  std::stable_sort(p.begin(), p.end(), [&](uint32_t a, uint32_t b) {
    return a1[a] != a1[b] ? a1[a] < a1[b] : a2[a] < a2[b];
  });
  return p;
}

std::vector<uint32_t> EdgeBuffer::dated_perm(bool by_dst) const {
  if (date.size() != src.size())
    throw std::runtime_error("EdgeBuffer: dated_perm without a full date column");
  std::vector<uint32_t> p(src.size());
  std::iota(p.begin(), p.end(), 0);
  const std::vector<uint64_t>& a1 = by_dst ? dst : src;
  const std::vector<uint64_t>& a2 = by_dst ? src : dst;
  std::stable_sort(p.begin(), p.end(), [&](uint32_t a, uint32_t b) {
    if (a1[a] != a1[b]) return a1[a] < a1[b];
    if (date[a] != date[b]) return date[a] < date[b];
    return a2[a] < a2[b];
  });
  return p;
}

}  // namespace evalcpp
