#include "codec/chunks.h"
#include "codec/decode_counts.h"

#include <algorithm>
#include <stdexcept>

#include "codec/lists.h"    // put_labels / get_labels: the label block's packing
#include "codec/timers.h"
#include "codec/varint.h"

namespace evalcpp {

uint64_t est_props(const Value& props) {
  uint64_t n = 8;
  for (auto& kv : props.as_row()) {
    const Value& v = kv.second;
    if (v.is_str()) n += 4 + v.as_str().size();           // a string's UTF-8 bytes, as stored (not its characters)
    else if (v.is_bytes()) n += 4 + v.as_bytes().size();   // a blob's raw length
    else if (v.is_list() || v.is_tuple()) {
      n += 2;
      for (auto& e : v.as_list()) n += 1 + e.as_str().size();
    } else n += 8;
  }
  return n;
}

uint64_t est_vertex(const std::vector<ColDecl>& cols, const Value& props) {
  RowFields stored;
  for (auto& kv : props.as_row())
    for (auto& c : cols)
      if (c.name == kv.first) { stored.push_back(kv); break; }
  return 8 + est_props(Value::row(std::move(stored)));
}

// --- encoders -------------------------------------------------------------------------------

static void put_columns(const ChunkCodecs& cc, std::string& out, const std::vector<ColDecl>& cols, const RowSource& rows) {
  size_t n = rows.size();
  for (size_t c = 0; c < cols.size(); c++) {
    const ColDecl& col = cols[c];
    std::string bitmap((n + 7) / 8, '\0');
    std::vector<size_t> cells;
    for (size_t i = 0; i < n; i++) {
      if (!rows.present(i, c)) bitmap[i / 8] |= char(1 << (i % 8));
      else cells.push_back(i);
    }
    out += bitmap;
    if (col.multi) {
      std::string offs, blob;
      for (size_t i : cells) {
        std::string b;
        size_t cnt = rows.multi_count(i, c);
        put_uvarint(b, cnt);
        for (size_t k = 0; k < cnt; k++) {
          std::string_view u = rows.multi_at(i, c, k);
          put_uvarint(b, u.size());
          b.append(u.data(), u.size());
        }
        blob += b;
        put_u32be(offs, uint32_t(blob.size()));
      }
      encode_str_column(out, cc.strings, offs + blob);
    } else if (stringlike(col)) {
      std::string offs, blob;
      for (size_t i : cells) {
        std::string_view u = rows.str_at(i, c);
        blob.append(u.data(), u.size());
        put_u32be(offs, uint32_t(blob.size()));
      }
      encode_str_column(out, cc.strings, offs + blob);
    } else {
      for (size_t i : cells) put_u64be(out, uint64_t(rows.int_at(i, c)));
    }
  }
}

std::string encode_props(const ChunkCodecs& cc, const std::vector<ColDecl>& cols, const RowSource& rows) {
  std::string out;
  put_uvarint(out, rows.size());
  std::vector<uint64_t> ids;
  ids.reserve(rows.size());
  for (size_t i = 0; i < rows.size(); i++) ids.push_back(rows.key(i));
  encode_uids(out, cc.uids, ids);
  put_columns(cc, out, cols, rows);
  return out;
}

std::string encode_ep(const ChunkCodecs& cc, const std::vector<ColDecl>& cols, const RowSource& rows) {
  return encode_props(cc, cols, rows);   // same body, keyed by ordinal
}

// every edge's label code in owner order; a group without one code per dst is a caller's bug
std::vector<uint8_t> adj_label_codes(const std::vector<AdjGroup>& groups) {
  std::vector<uint8_t> codes;
  for (auto& g : groups) {
    if (g.labels.size() != g.dsts.size()) throw std::logic_error("adj chunk: a group without one label code per neighbour");
    codes.insert(codes.end(), g.labels.begin(), g.labels.end());
  }
  return codes;
}

std::string encode_adj(const ChunkCodecs& cc, const std::vector<AdjGroup>& groups, bool with_ordinals, unsigned label_bits) {
  std::string out;
  put_uvarint(out, groups.size());
  std::vector<uint64_t> srcs;
  for (auto& g : groups) srcs.push_back(g.src);
  encode_uids(out, cc.uids, srcs);
  for (auto& g : groups) put_uvarint(out, g.dsts.size());
  std::string offs, nbr;
  for (auto& g : groups) {
    encode_uids(nbr, cc.uids, g.dsts);
    put_u32be(offs, uint32_t(nbr.size()));
  }
  out += offs;
  out += nbr;
  if (label_bits) lists::put_labels(out, adj_label_codes(groups), label_bits);
  if (with_ordinals)
    for (auto& g : groups)
      for (uint64_t o : g.ords) put_uvarint(out, o);
  return out;
}

// --- decoders -------------------------------------------------------------------------------

// --- the plain layout ---------------------------------------------------------------------
namespace {

class PlainPropsChunk : public PropsChunk {
 public:
  PlainPropsChunk(const ChunkCodecs* cc, const std::vector<ColDecl>* cols, std::string raw);   // parses ids + column layout
  const std::vector<uint64_t>& ids() const override { return ids_; }
  uint64_t first() const override { return ids_.front(); }
  uint64_t last() const override { return ids_.back(); }
  int64_t find(uint64_t id) const override;
  Value cell_impl(size_t col, size_t row) const override;
  size_t cell_bytes(size_t col, size_t row) const override;
 private:
  struct Col {
    size_t bitmap = 0, data = 0, present = 0;
    size_t dir = 0;                        // this column's slice of absent_dir_
  };
  int64_t rank(size_t col, size_t row) const;                      // index among present, -1 if absent
  std::string_view str_body(size_t col) const;                     // the raw_offsets body (decoded once)
  const ChunkCodecs* cc_;
  std::string raw_;
  std::vector<uint64_t> ids_;
  std::vector<Col> layout_;
  mutable std::vector<std::string> str_bodies_;                    // per column, filled on first use
  mutable std::mutex str_mutex_;                                    // chunks are shared across tasks
  std::vector<uint32_t> absent_dir_;   // per column, [j] = absent rows in [0, 64 j): one allocation per chunk
};

class PlainAdjChunk : public AdjChunk {
 public:
  PlainAdjChunk(const ChunkCodecs* cc, std::string raw, bool with_ordinals, unsigned label_bits);
  size_t size() const override { return srcs_.size(); }
  uint64_t first() const override { return srcs_.front(); }
  uint64_t last() const override { return srcs_.back(); }
  int64_t find(uint64_t src) const override;
  uint64_t src(size_t i) const override { return srcs_[i]; }
  std::vector<uint64_t> dsts(size_t i) const override;
  const std::vector<uint64_t>& ordinals(size_t i) override;     // decodes the ordinal tail once
  std::vector<uint8_t> labels(size_t i) const override;         // owner i's codes only
  std::vector<uint8_t> id_widths(size_t i) const override;
 private:
  const ChunkCodecs* cc_;
  std::string raw_;
  bool with_ords_;
  unsigned label_bits_;
  size_t ord_tail_ = 0;                                 // where the ordinals start: after the label block
  std::vector<uint32_t> edge_at_;                       // label_bits > 0: the edges before owner i (its codes' offset)
  std::vector<uint64_t> srcs_, degs_;
  std::vector<uint32_t> offs_;
  size_t area_ = 0, tail_ = 0;
  std::vector<std::vector<uint64_t>> ords_;             // filled lazily, under ords_mutex_
  bool ords_decoded_ = false;
  std::mutex ords_mutex_;
};

}  // namespace


static uint64_t bitmap_word(std::string_view bm, size_t j) {
  // bitmap bits for rows [64 j, 64 j + 64) as one word, row 64 j + k = bit k; bytes past the bitmap read as 0
  uint64_t w = 0;
  size_t end = std::min(bm.size(), 8 * j + 8);
  for (size_t i = 8 * j; i < end; i++) w |= uint64_t(uint8_t(bm[i])) << (8 * (i - 8 * j));
  return w;
}

static uint64_t low_bits(size_t k) { return k >= 64 ? ~uint64_t(0) : (uint64_t(1) << k) - 1; }

static size_t append_absent_directory(std::string_view bm, size_t n_rows, std::vector<uint32_t>& dir) {
  // absent rows before every 64th row, appended to the chunk's directory in the one pass that counts the column's
  // absent rows (one popcount per 64 rows); returns that count. A cell read is then O(1), not O(row) -- the bitmap
  // format does not force a scan from row 0; without this a full chunk scan cost O(rows^2). Only rows < n_rows are
  // counted (never the padding bits).
  size_t acc = 0;
  for (size_t j = 0; j < (n_rows + 63) / 64; j++) {
    dir.push_back(uint32_t(acc));
    acc += size_t(__builtin_popcountll(bitmap_word(bm, j) & low_bits(n_rows - 64 * j)));
  }
  return acc;
}

PlainPropsChunk::PlainPropsChunk(const ChunkCodecs* cc, const std::vector<ColDecl>* cols, std::string raw)
    : PropsChunk(cols), cc_(cc), raw_(std::move(raw)) {
  DecodeTimer t;
  codec_stats().chunks_decoded++;
  { DecodeCounts& d = decode_counts(); d.chunks++; d.bytes += raw_.size(); }
  size_t i = 0;
  n_ = size_t(get_uvarint(raw_, i));
  ids_ = decode_uids(cc_->uids, raw_, i, n_);
  str_bodies_.resize(cols_->size());
  size_t nb = (n_ + 7) / 8;
  absent_dir_.reserve(cols_->size() * ((n_ + 63) / 64));
  for (auto& c : *cols_) {
    Col L;
    L.bitmap = i;
    if (i + nb > raw_.size()) throw std::runtime_error("props chunk: truncated bitmap");
    L.dir = absent_dir_.size();
    L.present = n_ - append_absent_directory(std::string_view(raw_).substr(i, nb), n_, absent_dir_);
    i += nb;
    L.data = i;
    if (c.multi || stringlike(c)) {
      i += str_column_size(cc_->strings, raw_, i, L.present);
    } else {
      i += 8 * L.present;
    }
    layout_.push_back(L);
  }
  if (i > raw_.size()) throw std::runtime_error("props chunk: truncated columns");
}

int64_t PlainPropsChunk::find(uint64_t id) const {
  // the LAST row with this id: Python's dict(zip(uids, rows)) keeps the last duplicate
  auto it = std::upper_bound(ids_.begin(), ids_.end(), id);
  if (it == ids_.begin() || *(it - 1) != id) return -1;
  return (it - 1) - ids_.begin();
}

int PropsChunk::col_index(const std::string& name) const {
  for (size_t c = 0; c < cols_->size(); c++)
    if ((*cols_)[c].name == name) return int(c);
  return -1;
}

int64_t PlainPropsChunk::rank(size_t col, size_t row) const {
  const Col& L = layout_[col];
  std::string_view bm = std::string_view(raw_).substr(L.bitmap, (n_ + 7) / 8);
  if ((uint8_t(bm[row / 8]) >> (row % 8)) & 1) return -1;
  size_t j = row / 64;
  return int64_t(row) - int64_t(absent_dir_[L.dir + j] + size_t(__builtin_popcountll(bitmap_word(bm, j) & low_bits(row % 64))));
}

std::string_view PlainPropsChunk::str_body(size_t col) const {
  const Col& L = layout_[col];
  if (cc_->strings == StrCodec::RawOffsets)
    return std::string_view(raw_).substr(L.data, str_column_size(cc_->strings, raw_, L.data, L.present));
  std::lock_guard<std::mutex> lk(str_mutex_);
  if (str_bodies_[col].empty() && L.present)
    str_column_raw(cc_->strings, raw_, L.data, L.present, str_bodies_[col]);
  return str_bodies_[col];
}

Value PlainPropsChunk::cell_impl(size_t col, size_t row) const {
  DecodeTimer t;
  int64_t r = rank(col, row);
  if (r < 0) return Value();
  const Col& L = layout_[col];
  const ColDecl& c = (*cols_)[col];
  std::string_view raw(raw_);
  if (c.multi || stringlike(c)) {
    std::string_view body = str_body(col);          // u32 end offsets + blob
    size_t blob = 4 * L.present;
    size_t start = r == 0 ? 0 : get_u32be(body, 4 * (r - 1));
    size_t end = get_u32be(body, 4 * r);
    std::string_view sub = body.substr(blob + start, end - start);
    if (!c.multi) return string_value(c, sub);
    size_t p = 0;
    size_t cnt = size_t(get_uvarint(sub, p));
    ValueList lst;
    lst.reserve(cnt);
    for (size_t k = 0; k < cnt; k++) {
      size_t len = size_t(get_uvarint(sub, p));
      lst.push_back(Value(std::string(sub.substr(p, len))));
      p += len;
    }
    return Value::list(std::move(lst));
  }
  return Value(int64_t(get_u64be(raw, L.data + 8 * r)));
}

size_t PlainPropsChunk::cell_bytes(size_t col, size_t row) const {
  int64_t r = rank(col, row);
  if (r < 0) return 0;
  const ColDecl& c = (*cols_)[col];
  if (!(c.multi || stringlike(c))) return 8;
  // a compressed string column (zstd_column) has no stored width per cell: 0, (and no decompress)
  if (cc_->strings != StrCodec::RawOffsets) return 0;
  std::string_view body = str_body(col);          // u32 end offsets + blob
  size_t start = r == 0 ? 0 : get_u32be(body, 4 * (r - 1));
  return 4 + (get_u32be(body, 4 * r) - start);
}

Value PropsChunk::cell(size_t col, size_t row) const {
  Value v = cell_impl(col, row);
  if (!v.is_null()) { DecodeCounts& d = decode_counts(); d.cells++; d.fields++; }
  return v;
}

Value PropsChunk::row(size_t r) const {
  RowFields f;
  for (size_t c = 0; c < cols_->size(); c++) {
    Value v = cell_impl(c, r);
    if (!v.is_null()) f.emplace_back((*cols_)[c].name, std::move(v));
  }
  DecodeCounts& d = decode_counts();
  d.rows++; d.fields += f.size();
  return Value::row(std::move(f));
}

PlainAdjChunk::PlainAdjChunk(const ChunkCodecs* cc, std::string raw, bool with_ordinals, unsigned label_bits)
    : cc_(cc), raw_(std::move(raw)), with_ords_(with_ordinals), label_bits_(label_bits) {
  DecodeTimer t;
  codec_stats().chunks_decoded++;
  { DecodeCounts& d = decode_counts(); d.chunks++; d.bytes += raw_.size(); }
  size_t i = 0;
  size_t n = size_t(get_uvarint(raw_, i));
  srcs_ = decode_uids(cc_->uids, raw_, i, n);
  degs_.reserve(n);
  for (size_t k = 0; k < n; k++) degs_.push_back(get_uvarint(raw_, i));
  offs_.reserve(n);
  for (size_t k = 0; k < n; k++) offs_.push_back(get_u32be(raw_, i + 4 * k));
  i += 4 * n;
  area_ = i;
  tail_ = area_ + (n ? offs_.back() : 0);
  ord_tail_ = tail_;
  if (label_bits_) {                       // no label block: nothing below runs (the decode before the block existed)
    size_t edges = 0;
    edge_at_.reserve(n);
    for (uint64_t d : degs_) {
      edge_at_.push_back(uint32_t(edges));
      edges += size_t(d);
    }
    ord_tail_ = tail_ + (edges * label_bits_ + 7) / 8;
  }
  if (ord_tail_ > raw_.size()) throw std::runtime_error("adj chunk: truncated");
}

int64_t PlainAdjChunk::find(uint64_t src) const {
  auto it = std::lower_bound(srcs_.begin(), srcs_.end(), src);
  if (it == srcs_.end() || *it != src) return -1;
  return it - srcs_.begin();
}

std::vector<uint64_t> PlainAdjChunk::dsts(size_t i) const {
  DecodeTimer t;
  { DecodeCounts& d = decode_counts(); d.postings++; d.fields += degs_[i]; }
  size_t start = area_ + (i == 0 ? 0 : offs_[i - 1]);
  return decode_uids(cc_->uids, raw_, start, size_t(degs_[i]));
}

static uint8_t uvarint_width(uint64_t v) {
  uint8_t n = 1;
  while (v >= 0x80) { v >>= 7; n++; }
  return n;
}

std::vector<uint8_t> PlainAdjChunk::id_widths(size_t i) const {
  std::vector<uint8_t> w(size_t(degs_[i]), 0);
  if (cc_->uids != UidCodec::DeltaVarint) return w;     // bit-packed deltas: no byte width per entry
  size_t at = area_ + (i == 0 ? 0 : offs_[i - 1]);
  for (size_t k = 0; k < w.size(); k++) {
    size_t from = at;
    uint64_t d = get_uvarint(raw_, at);
    // the first id is stored as a delta from 0 (9-10 B under a tag); used books it as a Condensed list codes it,
    // from its vid group's base (codec/lists.cc put_ids: the tag nibble at the low end, 2-3 B)
    w[k] = k == 0 ? uvarint_width(((d & ((uint64_t(1) << 60) - 1)) << 4) | (d >> 60)) : uint8_t(at - from);
  }
  return w;
}

const std::vector<uint64_t>& PlainAdjChunk::ordinals(size_t i) {
  std::lock_guard<std::mutex> lk(ords_mutex_);
  if (!ords_decoded_) {
    DecodeTimer t;
    if (!with_ords_) throw std::runtime_error("adj chunk: no ordinals in this chunk");
    size_t p = ord_tail_;
    ords_.resize(srcs_.size());
    for (size_t g = 0; g < srcs_.size(); g++) {
      ords_[g].reserve(size_t(degs_[g]));
      for (uint64_t k = 0; k < degs_[g]; k++) ords_[g].push_back(get_uvarint(raw_, p));
    }
    ords_decoded_ = true;
  }
  return ords_[i];
}

std::vector<uint8_t> PlainAdjChunk::labels(size_t i) const {
  if (!label_bits_) return {};
  DecodeTimer t;
  return lists::get_labels_at(raw_, tail_, edge_at_[i], size_t(degs_[i]), label_bits_);
}

std::shared_ptr<PropsChunk> PropsChunk::decode(const ChunkCodecs* cc, const std::vector<ColDecl>* cols, std::string raw) {
  return std::make_shared<PlainPropsChunk>(cc, cols, std::move(raw));
}

std::shared_ptr<AdjChunk> AdjChunk::decode(const ChunkCodecs* cc, std::string raw, bool with_ordinals, unsigned label_bits) {
  return std::make_shared<PlainAdjChunk>(cc, std::move(raw), with_ordinals, label_bits);
}

std::vector<uint64_t> chunk_ids(const ChunkCodecs& cc, std::string_view raw) {
  DecodeTimer t;
  codec_stats().chunks_decoded++;
  { DecodeCounts& d = decode_counts(); d.chunks++; d.bytes += raw.size(); }
  size_t i = 0;
  size_t n = size_t(get_uvarint(raw, i));
  return decode_uids(cc.uids, raw, i, n);
}

}  // namespace evalcpp
