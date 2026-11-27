// The LIST block, meta-v4's one adjacency value: one owner's neighbours (a Condensed list, or one part of a split
// one) = uvarint n + the n sorted neighbour ids (the first from its vid group's base, the rest as deltas) +
// [label_bits > 0] the far-end LABEL block (n codes of label_bits bits, packed low bit first: ceil(n x bits / 8) B) +
// [the edge type declares props] a columns block over the n entries (codec/columns.h). label_bits = 0 (the family's
// far end has one label, or far_label is off) writes no label byte. It is a one-owner chunk
// without the owner column (the owner is in the key), so a Condensed list and a Chunked owner's list hold the same
// ids and the same property columns.
#pragma once
#include <cstddef>
#include <cstdint>
#include <memory>
#include <atomic>
#include <mutex>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "codec/columns.h"
#include "formats/rows.h"
#include "value/value.h"

namespace evalcpp::lists {

struct Entry {
  uint64_t id;      // the neighbour
  Value props;      // its props row
  uint8_t label;    // its far-end label code (0 when the list stores none)
};
using Entries = std::vector<Entry>;   // ascending by id

// the label block: `codes` packed `bits` each, low bit first (bits = 0: nothing); read back n of them
void put_labels(std::string& out, const std::vector<uint8_t>& codes, unsigned bits);
std::vector<uint8_t> get_labels(std::string_view b, size_t& i, size_t n, unsigned bits);
// codes [first, first + n) of the block that starts at byte `block`: the codes are fixed-width, so one owner's are
// read without unpacking the rest
std::vector<uint8_t> get_labels_at(std::string_view b, size_t block, size_t first, size_t n, unsigned bits);

// `dsts` sorted ascending; `labels` (when label_bits > 0) one code per dst; `props` (when `cols` is non-empty) one row
// per dst, in the same order
std::string encode(const std::vector<uint64_t>& dsts, const std::vector<uint8_t>* labels, unsigned label_bits,
                   const std::vector<ColDecl>& cols, const RowSource* props);
std::string encode(const Entries& entries, unsigned label_bits, const std::vector<ColDecl>& cols);
std::string empty(const std::vector<ColDecl>& cols);         // n = 0 (no label byte at any width)

class View {
 public:
  View(const std::vector<ColDecl>* cols, std::string_view raw, unsigned label_bits);
  size_t size() const { return dsts_.size(); }
  const std::vector<uint64_t>& dsts() const { return dsts_; }
  uint8_t label(size_t i) const { return labels_.empty() ? 0 : labels_[i]; }   // entry i's far-end label code
  size_t id_bytes(size_t i) const { return id_width_[i]; }   // entry i's own delta-uvarint width (used bytes)
  bool has_props() const { return props_ != nullptr; }
  const columns::View* props() const { return props_.get(); }
  Value props_row(size_t i) const;                            // entry i's present props (empty row without props)
  size_t props_bytes(size_t i) const;                         // entry i's framed cell bytes (used bytes)
  Entries entries() const;
 private:
  std::vector<uint64_t> dsts_;
  std::vector<uint8_t> id_width_;
  std::vector<uint8_t> labels_;   // empty at label_bits 0
  std::unique_ptr<columns::View> props_;
};

// --- the k-owner CHUNK (a Chunked adjacency chunk with the edge props inline, meta-v4's `adjacency` placement) ---
// The skip16 header (config.h kChunkHeader "skip16"): an owner is found by a binary search over a skip sample
// every 16 owners plus at most 15 gaps, and nothing in the header is walked to read one list. Fields in order:
//   uvarint k                       k = 0: nothing else in the header (a stored chunk is never empty)
//   uvarint first                   the first owner, coded from its vid group's base (lists' first-id code)
//   uvarint span                    last - first (the chunk cache's bound at O(1))
//   u8 w_b + k fields of w_b bits   END[i]: the byte offset, from the dst area's start, just past owner i's list;
//                                   w_b = bits(the dst area's length)
//   [per-edge block]                only when the family stores something per edge: a label block (label_bits > 0),
//                                   ordinals or a columns block -- known from the config, no flag. u8 w_e: 0 = every
//                                   owner has the same degree, then uvarint d (owner i's first edge = i x d); else
//                                   w_e = bits(edges) and k fields of w_e bits, CUM[i] = the edges of owners 0..i
//   uvarint G                       the gap stream's length in bytes
//   floor((k-1)/16) samples         sample s = 1, 2, ... for owner 16s: (id - first) in bits(span) bits, then where
//                                   owner 16s+1's gap starts in the gap stream, in bits(G) bits (two fields: together
//                                   they can pass 64 bits; both widths derived, none stored)
//   G bytes                         the k-1 owner gaps as uvarints
//   the dst area                    per owner its sorted dsts as delta-uvarints (the first from its vid group's base)
//   [label block] [ordinals | columns]   over every edge of the chunk in owner order
// A bit field of width w (0-64) sits at bits [i x w, i x w + w) of its block, low bit first (put_labels' order).
// A degree is the number of uvarints between two ENDs. With props the columns block is the edge block; OWN placement
// (the edge props in their own chunks) puts one uvarint per edge there instead, the row of that edge in its etype's own
// chunks (`ords`; never with cols). The chunker closes on est_chunk_owner, a logical size, so the header form moves no
// chunk boundary. The old header (owner gaps + degree varints + u32 ENDs) is in git history before this stamp.
struct ChunkOwner {
  uint64_t src;
  std::vector<uint64_t> dsts;     // sorted
  std::vector<uint8_t> labels;    // label_bits > 0: one far-end label code per dst
};
// `owners` strictly ascending by src. `props`: one row per edge, in owner order then dst order (nullptr when the etype
// declares no props). `ords`: own placement only, one ordinal per edge in the same order (then `cols` is empty and
// `props` nullptr)
std::string encode_chunk(const std::vector<ChunkOwner>& owners, unsigned label_bits, const std::vector<ColDecl>& cols,
                         const RowSource* props, const std::vector<uint64_t>* ords = nullptr);
// the estimate the chunker closes on (a LOGICAL size, never bytes): the plain chunk's 9 + 5 x degree per owner, plus
// each edge's props estimate (codec/chunks.h est_props) -- so the target counts the inline bytes
uint64_t est_chunk_owner(size_t degree, uint64_t props_est);

// A chunk read. The constructor reads the header's fixed fields and checks every block's bounds (O(1): no owner is
// decoded), so a view is immutable once built and holds nothing per owner. An owner is found from the skip samples
// plus at most 15 gaps; a list is read between its two ENDs; an edge's row (label, ordinal, props) from the edge
// column. The ordinals and the props view are built on their first use, once (a view in the shared chunk cache is read
// by several workers: a mutex and a done flag; a build that throws leaves the flag unset, so the next call throws too).
// decode_counts books the whole chunk once, at construction, and postings / fields per list read. The trailing-bytes
// check of a chunk without ordinals or props runs at construction; with them, at their first use. `validate()` walks
// everything (every gap and sample, every list against its END and the edge column) and builds the lazy parts.
class ChunkView {
 public:
  // with_ords: the chunk ends in an ordinals block (own placement; `cols` is then empty)
  ChunkView(const std::vector<ColDecl>* cols, std::string raw, unsigned label_bits, bool with_ords = false);
  ChunkView(const ChunkView&) = delete;              // the props view points into raw_: never copied or moved
  ChunkView& operator=(const ChunkView&) = delete;
  size_t size() const { return k_; }
  uint64_t first() const { return first_; }
  uint64_t last() const { return last_; }
  uint64_t src(size_t i) const;                          // a sample + at most 15 gaps
  std::vector<uint64_t> owners() const;                  // every owner id, one pass (the bulk readers)
  // the owner's index, -1 if not in the chunk: the sampled path while the view has served at most k/16 finds, then
  // the owner array, built once (a chunk read for many owners pays the old header's one decode, not 16 per owner)
  int64_t find(uint64_t src) const;
  int64_t find_sampled(uint64_t src) const;              // a sample + at most 15 gaps (the tests compare the two paths)
  int64_t find_array(uint64_t src) const;                // lower_bound on the owner array (built on first use)
  std::vector<uint64_t> dsts(size_t i) const;
  size_t edge_base(size_t i) const;                      // owner i's first edge row (a family with a per-edge block)
  const columns::View* props() const;
  Value props_row(size_t i, size_t j) const;            // owner i's j-th edge's present props
  // the used-bytes units (lists::View's id_bytes / props_bytes): owner i's entries' own delta-uvarint widths, and its
  // j-th edge's framed cell bytes. Not counted as decodes (they are the meter's)
  std::vector<uint8_t> id_widths(size_t i) const;
  size_t props_bytes(size_t i, size_t j) const;
  uint8_t label(size_t i, size_t j) const;              // its far label code (0 at label_bits 0)
  uint64_t ordinal(size_t i, size_t j) const;           // own placement: its row in the own chunks
  void validate() const;                                // walks every field and builds every lazy part
 private:
  uint64_t end_at(size_t i) const;                      // END[i]
  uint64_t sample_id(size_t s) const;                   // owner 16s's id (s = 0: the first owner)
  size_t sample_pos(size_t s) const;                    // where owner 16s+1's gap starts (absolute in raw_)
  // owner i's list [from, to) in raw_; decodes it into `ids` / `widths` when given; returns its entry count
  size_t walk_list(size_t i, std::vector<uint64_t>* ids, std::vector<uint8_t>* widths) const;
  const std::vector<uint64_t>& ords() const;
  const std::vector<uint64_t>& owner_array() const;
  std::string raw_;
  const std::vector<ColDecl>* cols_;
  unsigned label_bits_;
  bool with_ords_;
  bool per_edge_;                                         // the family has a per-edge block (labels, ords or cols)
  size_t k_ = 0;                                          // owners in the chunk
  uint64_t first_ = 0, last_ = 0;                         // the first and last owner ids (0 when empty)
  size_t edges_ = 0;                                      // with a per-edge block: the chunk's edges
  unsigned w_b_ = 0, w_e_ = 0, w_id_ = 0, w_pos_ = 0;     // field widths: ENDs, CUM (0 = constant degree), samples
  uint64_t degree_ = 0;                                   // w_e_ = 0: every owner's degree
  size_t ends_at_ = 0, cum_at_ = 0, samples_at_ = 0, gaps_at_ = 0, gaps_len_ = 0;   // block starts in raw_
  size_t area_ = 0, labels_at_ = 0, tail_at_ = 0;         // the dst area, the label block, ordinals-or-props
  mutable std::mutex lazy_mutex_;                                 // guards the three first builds below
  mutable std::atomic<bool> ords_built_{false}, props_built_{false}, owners_built_{false};
  mutable std::atomic<size_t> finds_{0};                          // finds served on the sampled path
  mutable std::vector<uint64_t> owner_ids_;                       // every owner id, once built (lives as the view does)
  mutable std::vector<uint64_t> ords_;    // every edge's, owner order; empty without an ordinals block
  mutable std::unique_ptr<columns::View> props_;
};

}  // namespace evalcpp::lists
