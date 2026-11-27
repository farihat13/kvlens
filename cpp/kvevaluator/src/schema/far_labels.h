// The far-end label of an adjacency entry, shared by every format that stores one (meta, pertype_csr): the labels an
// edge type's far end can have per direction come from the schema's endpoints; a stored code is ceil(log2 k) bits
// (0 for k = 1: the one label is catalog knowledge). One refusal rule for every format at ingest:
//   - an edge whose (source label, target label) pair is not a declared endpoint of its type is refused
//   - more than 256 far labels cannot be coded in a byte: refused when a format stores codes
#pragma once
#include <cstdint>
#include <string>
#include <vector>

#include "schema/schema.h"

namespace evalcpp {

unsigned far_code_bits(size_t k);   // ceil(log2 k); 0 for one label (and for none)
// the code of `label` among `far` (the schema's far labels, sorted); refuses a label that is not one of them
uint8_t far_label_code(const std::vector<std::string>& far, const std::string& label, const std::string& etype, bool out,
                       const char* who);
// refuses more than 256 far labels when codes are stored
void check_far_label_count(const std::vector<std::string>& far, const std::string& etype, const char* who);
// refuses an edge whose label pair is not one of its type's declared endpoints
void check_edge_endpoints(const Schema& schema, const std::string& etype, const std::string& src_label,
                          const std::string& dst_label, const char* who);

}  // namespace evalcpp
