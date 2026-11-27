// What our code DECODED, counted where the decoders are (the codec layer's entry points and the two decoders
// outside it: ArangoDB's documents, Dgraph's value-sorted index posting), per thread; the executor folds a
// step's tasks into the step row. Deterministic: a count depends on which stored values a step parses and which
// fields it asks for, never on timing.
//   records by kind -- one per decode REQUEST:
//     rows      a whole row materialized (RowV2::row, columns::View::row, PropsChunk::row)
//     cells     one cell asked for alone and present (the cell() of the same three; columns::decode_cell, a Split
//               vertex record; prim::decode, a Dgraph scalar or a named facet dict)
//     postings  a list of ids decoded as a unit (posting::decode_posting / decode_adjacency, a lists::View, one
//               owner's list out of a chunk: AdjChunk::dsts, lists::ChunkView::dsts, Dgraph's value-sorted posting)
//     chunks    a packed chunk parsed (PropsChunk / AdjChunk decode, chunk_ids, a lists::ChunkView)
//     docs      an ArangoDB document parsed (vp_decode_doc, vp_decode_fields)
//   fields      the present cells materialized (by a row or a cell request) + the list entries produced (one per id)
//   bytes       decoded_kvbytes: the STORED bytes of the values parsed (a lazily parsed row, list, chunk or document
//               counts whole, once per parse; a cell decoded alone counts its own bytes). Never the in-memory size.
#pragma once
#include <cstdint>

namespace evalcpp {

struct DecodeCounts {
  uint64_t rows = 0, cells = 0, postings = 0, chunks = 0, docs = 0;
  uint64_t fields = 0;
  uint64_t bytes = 0;
  void add(const DecodeCounts& o) {
    rows += o.rows; cells += o.cells; postings += o.postings; chunks += o.chunks; docs += o.docs;
    fields += o.fields; bytes += o.bytes;
  }
  bool operator==(const DecodeCounts& o) const {
    return rows == o.rows && cells == o.cells && postings == o.postings && chunks == o.chunks && docs == o.docs &&
           fields == o.fields && bytes == o.bytes;
  }
  void reset() { *this = DecodeCounts(); }
  bool any() const { return rows || cells || postings || chunks || docs || fields || bytes; }
  // *this - o, lane by lane (the counters only grow inside one window)
  DecodeCounts since(const DecodeCounts& o) const {
    DecodeCounts d;
    d.rows = rows - o.rows; d.cells = cells - o.cells; d.postings = postings - o.postings; d.chunks = chunks - o.chunks;
    d.docs = docs - o.docs; d.fields = fields - o.fields; d.bytes = bytes - o.bytes;
    return d;
  }
};

inline thread_local DecodeCounts t_decode_counts;
inline DecodeCounts& decode_counts() { return t_decode_counts; }   // this thread's counters

}  // namespace evalcpp
