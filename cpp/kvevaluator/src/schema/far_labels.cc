#include "schema/far_labels.h"

#include <stdexcept>

namespace evalcpp {

unsigned far_code_bits(size_t k) {
  unsigned bits = 0;
  while ((size_t(1) << bits) < k) bits++;
  return bits;
}

uint8_t far_label_code(const std::vector<std::string>& far, const std::string& label, const std::string& etype, bool out,
                       const char* who) {
  for (size_t c = 0; c < far.size(); c++)
    if (far[c] == label) return uint8_t(c);
  throw std::runtime_error(std::string(who) + ": a " + etype + (out ? " out" : " in") + " entry's far end is a " +
                           (label.empty() ? std::string("vertex of no given label") : label) +
                           ", not one of the schema's endpoints");
}

void check_far_label_count(const std::vector<std::string>& far, const std::string& etype, const char* who) {
  if (far.size() > 256)
    throw std::runtime_error(std::string(who) + ": edge type " + etype + " has " + std::to_string(far.size()) +
                             " far-end labels; far_label codes hold at most 256");
}

void check_edge_endpoints(const Schema& schema, const std::string& etype, const std::string& src_label,
                          const std::string& dst_label, const char* who) {
  for (auto& [s, d] : schema.edge(etype).endpoints)
    if (s == src_label && d == dst_label) return;
  throw std::runtime_error(std::string(who) + " ingest: edge '" + etype + "' from a " + src_label + " to a " +
                           dst_label + " is not one of the schema's endpoints of that edge type");
}

}  // namespace evalcpp
