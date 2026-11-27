// pertype_csr's realistic key codec. The logical keys are Python's tuples;
// only the bytes change:
//   props chunk  ("t", label, "p", first_uid)              -> 't' label:u16 'p' first:u64
//   adjacency    ("t", label, "a", etype, dir, first_src)  -> 't' label:u16 'a' etype:u16 dir:u8 first:u64
//   edge props   ("t", label, "ep", etype, first_ordinal)  -> 't' label:u16 'e' etype:u16 first:u64
// Every read is prefix-scoped, so the order BETWEEN prefixes never changes an op count.
#pragma once
#include <cstdint>
#include <string>
#include <string_view>

namespace evalcpp::pertype_csr {

std::string props_prefix(int label_id);
std::string adj_prefix(int label_id, int etype_id, bool out);
std::string ep_prefix(int label_id, int etype_id);
// prefix + the trailing u64 (first uid / first src / first ordinal, or a probe id)
std::string with_id(const std::string& prefix, uint64_t id);
inline bool has_prefix(std::string_view key, std::string_view prefix) {
  return key.size() >= prefix.size() && key.substr(0, prefix.size()) == prefix;
}
uint64_t trailing_id(std::string_view key);   // the last 8 bytes, big-endian

}  // namespace evalcpp::pertype_csr
