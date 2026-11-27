// The posting-value codecs shared by every posting-based layout (dgraph, the posting index,
// meta): a value is a head byte + UidPack (codec/uidpack.h). Byte-identical to dgraph's
// former static codec (formats/dgraph), lifted here so no format includes a sibling.
//   U  flat uid posting (an index term): UidPack blocks to the end
//   A  adjacency, uid-ascending: uvarint n + UidPack(ordered) + n facet dicts (prim)
//   S  adjacency, CALLER order (a date-sorted form): uvarint n + UidPack(unordered) + n facet
//      dicts. Decode reads the head byte, so the mode never has to be known at a read site.
//   R / Q  the SCHEMA facet codec (AdjCodec), uid-ascending / caller order: n + UidPack + per entry
//      the RowV2 row of the etype's declared edge columns (a Split edge record's exact value bytes),
//      unframed: a row is self-delimiting (rowv2_size). No columns = no per-entry bytes at all (a Split
//      record there still stores a 1-B empty row + timestamp; the list needs nothing to delimit entries).
#pragma once
#include <cstdint>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "codec/props_codec.h"
#include "formats/rows.h"
#include "value/value.h"

namespace evalcpp::posting {

// how an adjacency posting stores each entry's facets. named = a prim dict per entry, every field NAME
// repeated per edge (dgraph's facets); schema = a RowV2 row over `cols`. A posting written with one
// codec throws when decoded with the other (distinct head bytes). No function below defaults its
// codec: every call site names the one its format's Config chose (codec/props_codec.h).
struct AdjCodec {
  using Mode = PropsCodec;
  Mode mode = Mode::named;
  const std::vector<ColDecl>* cols = nullptr;   // schema: the etype's declared edge columns
  int row_timestamp = 0;                        // schema: trailing zero bytes per row (the Split record's)
  // a far-end LABEL block after the entries: one code of `label_bits` bits per entry, packed low bit first
  // (codec/lists.h put_labels: the configurable format's rule). 0 = none: today's bytes, and what Dgraph always
  // writes. In this API an entry's code rides on its facets Value (Value::label, -1 = none): decode sets it,
  // encode reads it and throws on an entry without one.
  unsigned label_bits = 0;
  static AdjCodec schema(const std::vector<ColDecl>* cols, int row_timestamp) {
    return AdjCodec{Mode::schema, cols, row_timestamp, 0};
  }
  AdjCodec with_label_bits(unsigned bits) const { AdjCodec c = *this; c.label_bits = bits; return c; }
  static AdjCodec named() { return AdjCodec{}; }
  bool is_schema() const { return mode == Mode::schema; }
};

// one entry's facet bytes under the codec (named: the prim dict; schema: the RowV2 row, or "" when
// the etype declares no edge columns). Schema throws on a field the etype does not declare (named would
// keep it; a row cannot).
std::string encode_facets(const Value& facets, const AdjCodec& codec);
// the used_write bytes of an edge-prop update of one entry, whose facets are now `new_facets`: schema =
// the CHANGED cells of its new row (what a Split record's update charges); named = the entry's whole
// facet dict, `named_width` (the caller's decode_adjacency_w width). One rule for every posting format.
uint64_t update_used_bytes(const Value& new_facets, const Value& props, const AdjCodec& codec, uint64_t named_width);
// one adjacency posting from raw parts (dsts + per-entry facet bytes, encode_facets); ordered=false
// is the caller-order form (the delta compression is forfeited, that layout's real price). `labels`: one far-end
// code per dst when codec.label_bits > 0 (required then), ignored otherwise
std::string encode_adjacency_bytes(const std::vector<uint64_t>& dsts, const std::vector<std::string_view>& facets,
                                   bool ordered, const AdjCodec& codec, const std::vector<uint8_t>* labels = nullptr);
std::string encode_adjacency(const std::vector<std::pair<uint64_t, Value>>& postings, bool ordered,
                             const AdjCodec& codec);
std::string empty_adjacency(const AdjCodec& codec);   // the ascending head + count 0
std::string encode_posting(const std::vector<uint64_t>& uids);   // sorted, distinct uids
std::vector<std::pair<uint64_t, Value>> decode_adjacency(std::string_view raw, const AdjCodec& codec);
std::vector<uint64_t> decode_posting(std::string_view raw);
// the KV-amp variants: also report each entry's attributable stored bytes -- the uid's own
// code width (uidpack_decode_widths), and for an adjacency each entry's facet bytes: named = the
// whole prim dict; schema = the row's present cells (RowV2::cell_bytes, what a Split edge record
// counts). Framing (tags, counts, group headers, a row's header + bitmap) is unattributed = amplification.
std::vector<uint64_t> decode_posting_w(std::string_view raw, std::vector<uint8_t>* widths);
std::vector<std::pair<uint64_t, Value>> decode_adjacency_w(std::string_view raw, std::vector<uint8_t>* uid_widths,
                                                           std::vector<uint32_t>* facet_widths,
                                                           const AdjCodec& codec);

}  // namespace evalcpp::posting
