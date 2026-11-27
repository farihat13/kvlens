// The C++ LDBC CSV reader -- the ingest producer.
// Ports Python loader.stream() verbatim: same file tables, same record shape, same order
// (vertices in VERTEX_FILES order, then edges in EDGE_FILES order), and a JSON line form
// byte-identical to records_dump.py (the record-hash gate depends on it). Structure comes
// from the loaded Schema (prop names/types, groups, vid tags); this file holds only what
// the schema does not: the CSV file mapping and the streaming mechanics -- exactly the
// split Python keeps.
//
// Two consumers, one contract: the store-builder takes Record structs in-process (no JSON,
// no pipe); `kvevaluator records` serializes the same stream as records.jsonl for (a) the
// hash-equality gate vs Python loader.stream() and (b) the Python oracle.
#pragma once
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

#include "schema/schema.h"
#include "value/value.h"

namespace evalcpp::ldbc_csv {

struct Record {
  bool is_vertex;
  // vertex: uid, label, props. edge: etype, src, dst, props, src_label, dst_label.
  uint64_t uid = 0, src = 0, dst = 0;
  const std::string* label = nullptr;      // interned: points into the file tables
  const std::string* etype = nullptr;
  const std::string* src_label = nullptr;
  const std::string* dst_label = nullptr;
  Value props;                             // row in emission order (Python dict order)
};

// Stream every record of `root` (a .../CsvBasic-LongDateFormatter dir) in loader.stream()
// order. Throws on a missing file, a schema prop absent from a CSV header (loud, like
// Python's header.index), a quoted field (this reader doesn't implement CSV quoting), a
// short row, or an id over the vid budget.
void stream(const std::string& root, const Schema& schema, const std::function<void(const Record&)>& emit);

// The record's JSON line, byte-identical to Python records_dump.py:
//   json.dumps(list(rec), separators=(",", ":"), ensure_ascii=False)
// (ints decimal, strings UTF-8 with only " \ and <0x20 escaped, \b\f\n\r\t shortcuts).
void jsonl_line(const Record& r, std::string& out);

}  // namespace evalcpp::ldbc_csv
