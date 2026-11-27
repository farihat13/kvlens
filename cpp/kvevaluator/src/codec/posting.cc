#include "codec/posting.h"
#include "codec/decode_counts.h"

#include <algorithm>
#include <stdexcept>

#include "codec/lists.h"
#include "codec/prim.h"
#include "codec/rowv2.h"
#include "codec/uidpack.h"
#include "codec/varint.h"

namespace evalcpp::posting {

static char head_of(bool ordered, const AdjCodec& codec) {
  if (codec.is_schema()) return ordered ? 'R' : 'Q';
  return ordered ? 'A' : 'S';
}

static void check_codec(const AdjCodec& codec) {
  if (codec.is_schema() && !codec.cols) throw std::runtime_error("posting codec: schema mode needs the column decls");
}

std::string encode_facets(const Value& facets, const AdjCodec& codec) {
  check_codec(codec);
  std::string out;
  if (!codec.is_schema()) { prim::encode(out, facets); return out; }   // named: the value as given
  Value f = facets.is_row() ? facets : Value::row({});
  for (auto& [name, v] : f.as_row()) {   // a row holds declared columns only: an undeclared prop would vanish
    bool declared = false;
    for (auto& c : *codec.cols) declared = declared || c.name == name;
    if (!declared) throw std::runtime_error("posting codec: edge prop '" + name + "' is not a declared column of its etype");
  }
  if (codec.cols->empty()) return out;   // no declared columns: no per-entry bytes
  std::vector<PropRow> rows = {{0, &f}};
  ValueRows rs(*codec.cols, rows);
  return rowv2_encode(*codec.cols, rs, 0, codec.row_timestamp);
}

uint64_t update_used_bytes(const Value& new_facets, const Value& props, const AdjCodec& codec, uint64_t named_width) {
  if (!codec.is_schema()) return named_width;
  std::string row = encode_facets(new_facets, codec);
  uint64_t used = 0;
  if (!row.empty() && props.is_row()) {
    RowV2 rv(codec.cols, row);
    for (auto& [name, v] : props.as_row()) { int c = rv.col_index(name); if (c >= 0) used += rv.cell_bytes(size_t(c)); }
  }
  return used;
}

std::string encode_adjacency_bytes(const std::vector<uint64_t>& dsts, const std::vector<std::string_view>& facets,
                                   bool ordered, const AdjCodec& codec, const std::vector<uint8_t>* labels) {
  check_codec(codec);
  if (codec.label_bits && (!labels || labels->size() != dsts.size()))
    throw std::runtime_error("posting codec: a posting with a label block needs one far-end code per entry");
  if (facets.size() != dsts.size())   // one facet dict per entry, aligned: the caller's contract
    throw std::runtime_error("dgraph codec: " + std::to_string(dsts.size()) + " dsts but " + std::to_string(facets.size()) + " facets");
  if (codec.is_schema())
    for (auto f : facets)
      if (f.empty() != codec.cols->empty())   // a row per entry iff the etype declares columns
        throw std::runtime_error("posting codec: a schema entry's row does not match the declared columns");
  if (ordered) {
    for (size_t j = 1; j < dsts.size(); j++)
      if (dsts[j] <= dsts[j - 1]) throw std::runtime_error("dgraph codec: unsorted/duplicate adjacency");
  } else {
    std::vector<uint64_t> dedup(dsts);
    std::sort(dedup.begin(), dedup.end());
    for (size_t j = 1; j < dedup.size(); j++)
      if (dedup[j] == dedup[j - 1]) throw std::runtime_error("dgraph codec: duplicate adjacency entry");
  }
  std::string out(1, head_of(ordered, codec));
  put_uvarint(out, dsts.size());
  uidpack_encode(out, dsts, ordered);
  for (auto f : facets) out.append(f.data(), f.size());
  if (codec.label_bits) lists::put_labels(out, *labels, codec.label_bits);   // after the entries; nothing at 0 bits
  return out;
}

std::string encode_adjacency(const std::vector<std::pair<uint64_t, Value>>& postings, bool ordered,
                             const AdjCodec& codec) {
  std::vector<uint64_t> dsts;
  std::vector<std::string> fb;
  std::vector<uint8_t> labels;
  for (auto& [d, f] : postings) {
    dsts.push_back(d);
    fb.push_back(encode_facets(f, codec));
    if (codec.label_bits) {
      if (f.label < 0 || f.label >= (1 << codec.label_bits))
        throw std::runtime_error("posting codec: an entry carries no far-end label code for a posting that stores one");
      labels.push_back(uint8_t(f.label));
    }
  }
  std::vector<std::string_view> views(fb.begin(), fb.end());
  return encode_adjacency_bytes(dsts, views, ordered, codec, &labels);
}

std::string empty_adjacency(const AdjCodec& codec) {
  std::string v(1, head_of(true, codec));
  put_uvarint(v, 0);
  return v;
}

std::string encode_posting(const std::vector<uint64_t>& uids) {
  for (size_t j = 1; j < uids.size(); j++)
    if (uids[j] <= uids[j - 1]) throw std::runtime_error("dgraph codec: unsorted/duplicate index posting");
  std::string out = "U";
  uidpack_encode(out, uids, true);
  return out;
}

std::vector<std::pair<uint64_t, Value>> decode_adjacency(std::string_view raw, const AdjCodec& codec) {
  return decode_adjacency_w(raw, nullptr, nullptr, codec);
}

std::vector<std::pair<uint64_t, Value>> decode_adjacency_w(std::string_view raw,
                                                           std::vector<uint8_t>* uid_widths,
                                                           std::vector<uint32_t>* facet_widths,
                                                           const AdjCodec& codec) {
  check_codec(codec);
  uint64_t bytes0 = decode_counts().bytes;
  bool schema = codec.is_schema();
  if (raw.empty() || (raw[0] != head_of(true, codec) && raw[0] != head_of(false, codec)))
    throw std::runtime_error(std::string("posting codec: not a ") + (schema ? "schema" : "named") +
                             " adjacency posting (head byte " + std::to_string(raw.empty() ? -1 : int(uint8_t(raw[0]))) + ")");
  size_t i = 1;
  size_t n = size_t(get_uvarint(raw, i));
  std::vector<uint64_t> uids = uidpack_decode_widths(raw, i, int64_t(n), raw[0] == head_of(true, codec), uid_widths);
  std::vector<std::pair<uint64_t, Value>> out;
  out.reserve(n);
  for (uint64_t u : uids) {
    size_t before = i;
    if (!schema) {
      out.emplace_back(u, prim::decode(raw, i));
      if (facet_widths) facet_widths->push_back(uint32_t(i - before));
    } else if (codec.cols->empty()) {   // no declared columns: nothing stored per entry
      out.emplace_back(u, Value::row({}));
      if (facet_widths) facet_widths->push_back(0);
    } else {
      i += rowv2_size(*codec.cols, raw, i, codec.row_timestamp);
      RowV2 row(codec.cols, raw.substr(before, i - before));
      out.emplace_back(u, row.row());
      if (facet_widths) {
        uint32_t w = 0;
        for (size_t c = 0; c < codec.cols->size(); c++) w += uint32_t(row.cell_bytes(c));
        facet_widths->push_back(w);
      }
    }
  }
  if (codec.label_bits) {   // the far-end codes, one per entry, onto the entries' facets (unattributed: framing)
    std::vector<uint8_t> codes = lists::get_labels(raw, i, n, codec.label_bits);
    for (size_t j = 0; j < n; j++) out[j].second.label = int16_t(codes[j]);
  }
  if (i != raw.size()) throw std::runtime_error("dgraph codec: trailing bytes in an adjacency posting");
  DecodeCounts& d = decode_counts();
  d.postings++; d.fields += n;          // one per entry; an entry's facets counted themselves (a row, or a prim value)
  d.bytes = bytes0 + raw.size();        // the whole posting once (the facets counted their own bytes: replaced)
  return out;
}

std::vector<uint64_t> decode_posting(std::string_view raw) {
  return decode_posting_w(raw, nullptr);
}

std::vector<uint64_t> decode_posting_w(std::string_view raw, std::vector<uint8_t>* widths) {
  if (raw.empty() || raw[0] != 'U') throw std::runtime_error("dgraph codec: not a uid posting");
  size_t i = 1;
  std::vector<uint64_t> uids = uidpack_decode_widths(raw, i, -1, true, widths);
  DecodeCounts& d = decode_counts();
  d.postings++; d.fields += uids.size(); d.bytes += raw.size();
  return uids;
}

}  // namespace evalcpp::posting
