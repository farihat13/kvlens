#include "codec/lists.h"
#include "codec/decode_counts.h"

#include <algorithm>
#include <cstring>
#include <stdexcept>

#include "codec/varint.h"

namespace evalcpp::lists {

// sorted ids: the FIRST coded from its vid group's base (the tag nibble moved to the low end, so a small id under a
// tag is 2-3 B, not a 9-B delta from 0), the rest as deltas; the shared varint.h put_deltas is not used here
static constexpr uint64_t kLow60 = (uint64_t(1) << 60) - 1;
static void put_ids(std::string& out, const std::vector<uint64_t>& sorted) {
  for (size_t k = 0; k < sorted.size(); k++)
    put_uvarint(out, k == 0 ? ((sorted[0] & kLow60) << 4) | (sorted[0] >> 60) : sorted[k] - sorted[k - 1]);
}
static uint64_t first_id(uint64_t v) { return (v >> 4) | ((v & 0xF) << 60); }

void put_labels(std::string& out, const std::vector<uint8_t>& codes, unsigned bits) {
  if (bits == 0) return;
  std::string block((codes.size() * bits + 7) / 8, '\0');
  for (size_t e = 0; e < codes.size(); e++) {
    if (codes[e] >> bits) throw std::logic_error("lists: a label code does not fit its bits");
    for (unsigned b = 0; b < bits; b++)
      if ((codes[e] >> b) & 1) block[(e * bits + b) / 8] |= char(1 << ((e * bits + b) % 8));
  }
  out += block;
}

std::vector<uint8_t> get_labels(std::string_view b, size_t& i, size_t n, unsigned bits) {
  std::vector<uint8_t> codes;
  if (bits == 0) return codes;
  size_t len = (n * bits + 7) / 8;
  if (i + len > b.size()) throw std::runtime_error("lists: truncated label block");
  codes.resize(n);
  for (size_t e = 0; e < n; e++)
    for (unsigned k = 0; k < bits; k++)
      if ((uint8_t(b[i + (e * bits + k) / 8]) >> ((e * bits + k) % 8)) & 1) codes[e] |= uint8_t(1 << k);
  i += len;
  return codes;
}

std::vector<uint8_t> get_labels_at(std::string_view b, size_t block, size_t first, size_t n, unsigned bits) {
  std::vector<uint8_t> codes;
  if (bits == 0 || n == 0) return codes;
  if (block + ((first + n) * bits + 7) / 8 > b.size()) throw std::runtime_error("lists: truncated label block");
  codes.resize(n);
  for (size_t e = 0; e < n; e++)
    for (unsigned k = 0; k < bits; k++) {
      size_t bit = (first + e) * bits + k;
      if ((uint8_t(b[block + bit / 8]) >> (bit % 8)) & 1) codes[e] |= uint8_t(1 << k);
    }
  return codes;
}

std::string encode(const std::vector<uint64_t>& dsts, const std::vector<uint8_t>* labels, unsigned label_bits,
                   const std::vector<ColDecl>& cols, const RowSource* props) {
  std::string out;
  put_uvarint(out, dsts.size());
  put_ids(out, dsts);
  if (label_bits) {
    if (!labels || labels->size() != dsts.size()) throw std::logic_error("lists::encode: one label per dst");
    put_labels(out, *labels, label_bits);
  }
  if (!cols.empty()) {
    if (!props || props->size() != dsts.size()) throw std::logic_error("lists::encode: one props row per dst");
    columns::encode(out, cols, *props);
  }
  return out;
}

std::string encode(const Entries& entries, unsigned label_bits, const std::vector<ColDecl>& cols) {
  std::vector<uint64_t> dsts;
  std::vector<uint8_t> labels;
  std::vector<PropRow> rows;
  dsts.reserve(entries.size());
  for (auto& e : entries) {
    if (!dsts.empty() && e.id <= dsts.back()) throw std::logic_error("lists::encode: ids not strictly ascending");
    dsts.push_back(e.id);
    labels.push_back(e.label);
    rows.push_back({e.id, &e.props});
  }
  ValueRows vr(cols, rows);
  return encode(dsts, &labels, label_bits, cols, cols.empty() ? nullptr : &vr);
}

std::string empty(const std::vector<ColDecl>& cols) { return encode(Entries{}, 0, cols); }

View::View(const std::vector<ColDecl>* cols, std::string_view raw, unsigned label_bits) {
  uint64_t bytes0 = decode_counts().bytes;
  size_t i = 0;
  size_t n = size_t(get_uvarint(raw, i));
  dsts_.reserve(n);
  id_width_.reserve(n);
  uint64_t cur = 0;
  for (size_t k = 0; k < n; k++) {
    size_t at = i;
    uint64_t x = get_uvarint(raw, i);
    cur = k == 0 ? first_id(x) : cur + x;
    dsts_.push_back(cur);
    id_width_.push_back(uint8_t(i - at));
  }
  labels_ = get_labels(raw, i, n, label_bits);
  if (!cols->empty()) props_ = std::make_unique<columns::View>(cols, raw, i, n);
  size_t end = props_ ? props_->end() : i;
  if (end != raw.size()) throw std::runtime_error("lists: trailing bytes after the list block");
  DecodeCounts& d = decode_counts();
  d.postings++; d.fields += n;
  d.bytes = bytes0 + raw.size();        // the whole list value once (its props block counted itself: replaced)
}

Value View::props_row(size_t i) const { return props_ ? props_->row(i) : Value::row({}); }

size_t View::props_bytes(size_t i) const {
  if (!props_) return 0;
  size_t b = 0;
  for (size_t c = 0; c < props_->cols(); c++) b += props_->cell_bytes(c, i);
  return b;
}

Entries View::entries() const {
  Entries e;
  e.reserve(dsts_.size());
  for (size_t i = 0; i < dsts_.size(); i++) e.push_back({dsts_[i], props_row(i), label(i)});
  return e;
}

// --- the chunk's skip16 header (lists.h): bit fields and skip samples ---
static constexpr size_t kSkip = 16;   // owners per skip sample

static unsigned bit_width(uint64_t x) { return x ? unsigned(64 - __builtin_clzll(x)) : 0; }
static size_t field_bytes(uint64_t n, unsigned w) { return size_t((n * w + 7) / 8); }   // n <= a chunk's bytes

// appends fields of the given widths (0-64 bits each), low bit first, as one block
class BitBlock {
 public:
  void put(uint64_t v, unsigned w) {
    if (w < 64 && (v >> w)) throw std::logic_error("lists: a field does not fit its bits");
    for (unsigned done = 0; done < w;) {
      size_t byte = size_t(pos_ / 8);
      unsigned at = unsigned(pos_ % 8), n = std::min(8 - at, w - done);
      if (byte == buf_.size()) buf_.push_back('\0');
      buf_[byte] = char(uint8_t(buf_[byte]) | uint8_t(((v >> done) & ((1u << n) - 1)) << at));
      done += n;
      pos_ += n;
    }
  }
  const std::string& bytes() const { return buf_; }
 private:
  std::string buf_;
  uint64_t pos_ = 0;
};

// the w-bit field (w <= 64) at bit `bit` of the block that starts at byte `block`; the caller checked the block's bounds
static_assert(__BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__, "get_field's fast path loads a little-endian word");
static inline uint64_t get_field(const std::string& b, size_t block, uint64_t bit, unsigned w) {
  if (w == 0) return 0;
  size_t at = block + size_t(bit / 8);
  unsigned sh = unsigned(bit % 8);
  if (sh + w <= 64 && at + 8 <= b.size()) {         // the common case: one unaligned 8-byte load
    uint64_t x;
    std::memcpy(&x, b.data() + at, 8);
    x >>= sh;
    return w == 64 ? x : x & ((uint64_t(1) << w) - 1);
  }
  size_t n = (sh + w + 7) / 8;                      // 1-9 bytes, near the value's end or a field across 9 bytes
  unsigned __int128 acc = 0;
  for (size_t i = 0; i < n; i++) acc |= (unsigned __int128)uint8_t(b[at + i]) << (8 * i);
  acc >>= sh;
  return w == 64 ? uint64_t(acc) : uint64_t(acc) & ((uint64_t(1) << w) - 1);
}

std::string encode_chunk(const std::vector<ChunkOwner>& owners, unsigned label_bits, const std::vector<ColDecl>& cols,
                         const RowSource* props, const std::vector<uint64_t>* ords) {
  const size_t k = owners.size();
  const bool per_edge = label_bits > 0 || ords != nullptr || !cols.empty();
  std::string out;
  put_uvarint(out, k);
  size_t edges = 0;
  for (auto& o : owners) edges += o.dsts.size();
  std::string nbr;                                   // the dst area
  if (k) {
    for (size_t o = 1; o < k; o++)
      if (owners[o].src <= owners[o - 1].src) throw std::logic_error("lists::encode_chunk: owners not strictly ascending");
    const uint64_t first = owners[0].src, span = owners[k - 1].src - first;
    put_uvarint(out, ((first & kLow60) << 4) | (first >> 60));
    put_uvarint(out, span);
    BitBlock ends;
    std::vector<uint64_t> end(k);
    for (size_t o = 0; o < k; o++) {
      put_ids(nbr, owners[o].dsts);
      end[o] = nbr.size();
    }
    const unsigned w_b = bit_width(nbr.size());
    for (uint64_t e : end) ends.put(e, w_b);
    out.push_back(char(w_b));
    out += ends.bytes();
    if (per_edge) {
      bool constant = true;
      for (auto& o : owners) constant = constant && o.dsts.size() == owners[0].dsts.size();
      if (constant) {
        out.push_back('\0');
        put_uvarint(out, owners[0].dsts.size());
      } else {
        const unsigned w_e = bit_width(edges);
        BitBlock cum;
        uint64_t c = 0;
        for (auto& o : owners) cum.put(c += o.dsts.size(), w_e);
        out.push_back(char(w_e));
        out += cum.bytes();
      }
    }
    std::string gaps;
    std::vector<size_t> start(k + 1, 0);            // start[o] = where owner o's gap starts (o >= 1); start[k] = G
    for (size_t o = 1; o < k; o++) {
      start[o] = gaps.size();
      put_uvarint(gaps, owners[o].src - owners[o - 1].src);
    }
    start[k] = gaps.size();
    put_uvarint(out, gaps.size());
    const unsigned w_id = bit_width(span), w_pos = bit_width(gaps.size());
    BitBlock samples;
    for (size_t o = kSkip; o < k; o += kSkip) {
      samples.put(owners[o].src - first, w_id);
      samples.put(start[o + 1], w_pos);
    }
    out += samples.bytes();
    out += gaps;
  }
  out += nbr;
  if (label_bits) {
    std::vector<uint8_t> codes;
    for (auto& o : owners) {
      if (o.labels.size() != o.dsts.size()) throw std::logic_error("lists::encode_chunk: one label per dst");
      codes.insert(codes.end(), o.labels.begin(), o.labels.end());
    }
    put_labels(out, codes, label_bits);
  }
  if (ords) {
    if (!cols.empty() || ords->size() != edges) throw std::logic_error("lists::encode_chunk: one ordinal per edge, and no columns beside them");
    for (uint64_t o : *ords) put_uvarint(out, o);
  }
  if (!cols.empty()) {
    if (!props || props->size() != edges) throw std::logic_error("lists::encode_chunk: one props row per edge");
    columns::encode(out, cols, *props);
  }
  return out;
}

uint64_t est_chunk_owner(size_t degree, uint64_t props_est) { return 9 + 5 * uint64_t(degree) + props_est; }

ChunkView::ChunkView(const std::vector<ColDecl>* cols, std::string raw, unsigned label_bits, bool with_ords)
    : raw_(std::move(raw)), cols_(cols), label_bits_(label_bits), with_ords_(with_ords),
      per_edge_(label_bits > 0 || with_ords || !cols->empty()) {
  if (with_ords && !cols->empty()) throw std::logic_error("lists: a chunk holds ordinals or columns, not both");
  auto need = [&](size_t at, size_t len, const char* what) {   // a block must lie inside raw_
    if (at > raw_.size() || len > raw_.size() - at) throw std::runtime_error(std::string("lists: truncated chunk (") + what + ")");
  };
  auto width = [&](size_t& i, const char* what) {
    need(i, 1, what);
    unsigned w = uint8_t(raw_[i++]);
    if (w > 64) throw std::runtime_error(std::string("lists: a chunk's ") + what + " width over 64 bits");
    return w;
  };
  size_t i = 0;
  k_ = size_t(get_uvarint(raw_, i));
  if (k_ > raw_.size()) throw std::runtime_error("lists: a chunk's owner count is past its bytes");
  area_ = i;
  uint64_t a = 0;                                   // the dst area's length
  if (k_) {
    first_ = first_id(get_uvarint(raw_, i));
    uint64_t span = get_uvarint(raw_, i);
    last_ = first_ + span;
    if (last_ < first_) throw std::runtime_error("lists: a chunk's owner span overflows");
    w_b_ = width(i, "END");
    ends_at_ = i;
    need(i, field_bytes(k_, w_b_), "ENDs");
    i += field_bytes(k_, w_b_);
    if (per_edge_) {
      w_e_ = width(i, "edge column");
      if (w_e_ == 0) {
        degree_ = get_uvarint(raw_, i);
        edges_ = size_t(degree_ * k_);
      } else {
        cum_at_ = i;
        need(i, field_bytes(k_, w_e_), "edge column");
        i += field_bytes(k_, w_e_);
      }
    }
    gaps_len_ = size_t(get_uvarint(raw_, i));
    w_id_ = bit_width(span);
    w_pos_ = bit_width(gaps_len_);
    samples_at_ = i;
    size_t sb = field_bytes((k_ - 1) / kSkip, w_id_ + w_pos_);
    need(i, sb, "skip samples");
    i += sb;
    gaps_at_ = i;
    need(i, gaps_len_, "owner gaps");
    area_ = i + gaps_len_;
    a = end_at(k_ - 1);
    if (per_edge_ && w_e_) edges_ = size_t(get_field(raw_, cum_at_, uint64_t(k_ - 1) * w_e_, w_e_));
  }
  need(area_, a, "dst area");
  labels_at_ = area_ + a;
  need(labels_at_, (uint64_t(edges_) * label_bits + 7) / 8, "label block");
  tail_at_ = labels_at_ + (edges_ * label_bits + 7) / 8;
  if (!with_ords && cols->empty() && tail_at_ != raw_.size()) throw std::runtime_error("lists: trailing bytes after the chunk");
  DecodeCounts& d = decode_counts();
  d.chunks++;
  d.bytes += raw_.size();               // the whole chunk once (a props view built later books nothing: props())
}

uint64_t ChunkView::end_at(size_t i) const { return get_field(raw_, ends_at_, uint64_t(i) * w_b_, w_b_); }

uint64_t ChunkView::sample_id(size_t s) const {
  return s == 0 ? first_ : first_ + get_field(raw_, samples_at_, uint64_t(s - 1) * (w_id_ + w_pos_), w_id_);
}

size_t ChunkView::sample_pos(size_t s) const {
  return gaps_at_ + (s == 0 ? 0 : size_t(get_field(raw_, samples_at_, uint64_t(s - 1) * (w_id_ + w_pos_) + w_id_, w_pos_)));
}

uint64_t ChunkView::src(size_t i) const {
  if (i >= k_) throw std::out_of_range("lists: an owner past the chunk");
  size_t s = i / kSkip, p = sample_pos(s);
  uint64_t cur = sample_id(s);
  for (size_t o = s * kSkip; o < i; o++) cur += get_uvarint(raw_, p);
  return cur;
}

std::vector<uint64_t> ChunkView::owners() const {
  std::vector<uint64_t> v;
  v.reserve(k_);
  size_t p = gaps_at_;
  uint64_t cur = first_;
  for (size_t o = 0; o < k_; o++) {
    if (o) cur += get_uvarint(raw_, p);
    v.push_back(cur);
  }
  return v;
}

int64_t ChunkView::find(uint64_t src) const {
  if (k_ == 0 || src < first_ || src > last_) return -1;
  // ski rental: k/16 sampled finds cost about one owner-array build (k gap decodes); after that the array is cheaper
  if (owners_built_.load(std::memory_order_acquire) || finds_.fetch_add(1, std::memory_order_relaxed) >= k_ / kSkip)
    return find_array(src);
  return find_sampled(src);
}

const std::vector<uint64_t>& ChunkView::owner_array() const {
  if (owners_built_.load(std::memory_order_acquire)) return owner_ids_;
  std::lock_guard<std::mutex> lk(lazy_mutex_);
  if (!owners_built_.load(std::memory_order_relaxed)) {
    owner_ids_ = owners();                  // reader CPU, not a decode of new data: books nothing (the ctor booked the chunk)
    owners_built_.store(true, std::memory_order_release);
  }
  return owner_ids_;
}

int64_t ChunkView::find_array(uint64_t src) const {
  if (k_ == 0 || src < first_ || src > last_) return -1;
  const std::vector<uint64_t>& a = owner_array();
  auto it = std::lower_bound(a.begin(), a.end(), src);
  return it != a.end() && *it == src ? int64_t(it - a.begin()) : -1;
}

int64_t ChunkView::find_sampled(uint64_t src) const {
  if (k_ == 0 || src < first_ || src > last_) return -1;
  size_t lo = 0, hi = (k_ - 1) / kSkip;             // the last sample whose owner is <= src (sample 0 = the first)
  while (lo < hi) {
    size_t mid = (lo + hi + 1) / 2;
    if (sample_id(mid) <= src) lo = mid;
    else hi = mid - 1;
  }
  size_t o = lo * kSkip, p = sample_pos(lo);
  uint64_t cur = sample_id(lo);
  for (size_t stop = std::min(o + kSkip, k_); cur < src && o + 1 < stop;) {   // at most 15 gaps
    cur += get_uvarint(raw_, p);
    o++;
  }
  return cur == src ? int64_t(o) : -1;
}

size_t ChunkView::walk_list(size_t i, std::vector<uint64_t>* ids, std::vector<uint8_t>* widths) const {
  if (i >= k_) throw std::out_of_range("lists: an owner past the chunk");
  size_t at = area_ + size_t(i ? end_at(i - 1) : 0), to = area_ + size_t(end_at(i));
  if (to < at || to > labels_at_) throw std::runtime_error("lists: a chunk's END is out of order");
  if (ids) ids->reserve(to - at);                   // an upper bound: one byte per entry at least
  size_t n = 0;
  uint64_t cur = 0;
  while (at < to) {
    size_t from = at;
    uint64_t x = get_uvarint(raw_, at);
    cur = n == 0 ? first_id(x) : cur + x;
    if (ids) ids->push_back(cur);
    if (widths) widths->push_back(uint8_t(at - from));
    n++;
  }
  if (at != to) throw std::runtime_error("lists: a chunk's list runs past its END");
  return n;
}

std::vector<uint64_t> ChunkView::dsts(size_t i) const {
  std::vector<uint64_t> v;
  size_t n = walk_list(i, &v, nullptr);
  DecodeCounts& d = decode_counts();
  d.postings++;
  d.fields += n;
  return v;
}

std::vector<uint8_t> ChunkView::id_widths(size_t i) const {
  std::vector<uint8_t> w;
  walk_list(i, nullptr, &w);
  return w;
}

size_t ChunkView::edge_base(size_t i) const {
  if (!per_edge_) throw std::logic_error("lists: edge_base on a chunk with no per-edge block");
  if (i >= k_) throw std::out_of_range("lists: an owner past the chunk");
  if (w_e_ == 0) return size_t(degree_ * i);
  return i == 0 ? 0 : size_t(get_field(raw_, cum_at_, uint64_t(i - 1) * w_e_, w_e_));
}

uint8_t ChunkView::label(size_t i, size_t j) const {
  if (label_bits_ == 0) return 0;
  size_t e = edge_base(i) + j;
  if (e >= edges_) throw std::out_of_range("lists: a label past the chunk's edges");
  return uint8_t(get_field(raw_, labels_at_, uint64_t(e) * label_bits_, label_bits_));   // the ctor checked the block
}

const std::vector<uint64_t>& ChunkView::ords() const {
  if (ords_built_.load(std::memory_order_acquire)) return ords_;
  std::lock_guard<std::mutex> lk(lazy_mutex_);
  if (!ords_built_.load(std::memory_order_relaxed)) {
    if (with_ords_) {
      std::vector<uint64_t> v;
      v.reserve(edges_);
      size_t i = tail_at_;
      for (size_t e = 0; e < edges_; e++) v.push_back(get_uvarint(raw_, i));
      if (i != raw_.size()) throw std::runtime_error("lists: trailing bytes after the chunk");
      ords_ = std::move(v);
    }
    ords_built_.store(true, std::memory_order_release);
  }
  return ords_;
}

uint64_t ChunkView::ordinal(size_t i, size_t j) const { return ords().at(edge_base(i) + j); }

const columns::View* ChunkView::props() const {
  if (cols_->empty()) return nullptr;
  if (props_built_.load(std::memory_order_acquire)) return props_.get();
  std::lock_guard<std::mutex> lk(lazy_mutex_);
  if (!props_built_.load(std::memory_order_relaxed)) {
    uint64_t bytes0 = decode_counts().bytes;
    auto v = std::make_unique<columns::View>(cols_, raw_, tail_at_, edges_);
    decode_counts().bytes = bytes0;     // the constructor booked the whole chunk: the block's own booking is undone
    if (v->end() != raw_.size()) throw std::runtime_error("lists: trailing bytes after the chunk");
    props_ = std::move(v);
    props_built_.store(true, std::memory_order_release);
  }
  return props_.get();
}

size_t ChunkView::props_bytes(size_t i, size_t j) const {
  const columns::View* p = props();
  if (!p) return 0;
  size_t b = 0;
  for (size_t c = 0; c < p->cols(); c++) b += p->cell_bytes(c, edge_base(i) + j);
  return b;
}

Value ChunkView::props_row(size_t i, size_t j) const {
  const columns::View* p = props();
  return p ? p->row(edge_base(i) + j) : Value::row({});
}

void ChunkView::validate() const {
  // the owner gaps: exactly k - 1 of them in G bytes, strictly ascending, summing to the span, every sample on its owner
  size_t p = gaps_at_;
  uint64_t cur = first_;
  for (size_t o = 1; o < k_; o++) {
    uint64_t x = get_uvarint(raw_, p);
    if (x == 0 || p > gaps_at_ + gaps_len_) throw std::runtime_error("lists: a chunk's owner gaps are corrupt");
    cur += x;
    if (o % kSkip == 0 && (sample_id(o / kSkip) != cur || sample_pos(o / kSkip) != p))
      throw std::runtime_error("lists: a chunk's skip sample does not match its owner");
  }
  if (k_ && (p != gaps_at_ + gaps_len_ || cur != last_)) throw std::runtime_error("lists: a chunk's owner gaps do not end at G or the last owner");
  // every list ends at its END; with an edge column, its length is its degree
  size_t edges = 0;
  for (size_t o = 0; o < k_; o++) {
    size_t n = walk_list(o, nullptr, nullptr);
    if (per_edge_ && edge_base(o) != edges) throw std::runtime_error("lists: a chunk's edge column disagrees with its lists");
    edges += n;
  }
  if (per_edge_ && edges != edges_) throw std::runtime_error("lists: a chunk's edge count disagrees with its lists");
  ords();
  props();
}

}  // namespace evalcpp::lists
