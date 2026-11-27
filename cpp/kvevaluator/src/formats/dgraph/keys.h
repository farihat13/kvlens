// dgraph's realistic key codec (in and out): the Badger key
// [prefix][namespace][attr][typeByte][uid | term] minus the constant prefix + namespace (deployment
// fields, left out as for every format: the fairness rule). The logical keys are Python's (attr, kind, last) tuples:
//   data / reverse  (attr, "D"|"R", vid)   -> attr bytes | typeByte 0x00 | 0x04 | vid:u64 BE
//   index           (attr, "I", term)      -> attr bytes | typeByte 0x02 | term bytes
// A term is the indexed value's string form (str(value)), a 7-digit zero-padded day number for
// the @index(day) date index, or the label for the dgraph.type enumeration -- so within
// (attr, I) byte order == Python's string order, which the day-token walks depend on.
// Predicate names are ASCII identifiers, so `attr + typeByte` is a prefix no other predicate
// shares (no name contains 0x00 / 0x02 / 0x04).
#pragma once
#include <cstdint>
#include <string>
#include <string_view>

namespace evalcpp::dgraph {

constexpr char kData = '\x00', kIndex = '\x02', kReverse = '\x04';

inline std::string prefix(const std::string& attr, char kind) { return attr + kind; }
inline std::string uid_key(const std::string& attr, char kind, uint64_t uid) {
  std::string k = prefix(attr, kind);
  for (int i = 7; i >= 0; i--) k.push_back(char((uid >> (8 * i)) & 0xff));
  return k;
}
inline std::string term_key(const std::string& attr, std::string_view term) {
  std::string k = prefix(attr, kIndex);
  k.append(term.data(), term.size());
  return k;
}
inline bool has_prefix(std::string_view key, std::string_view prefix) {
  return key.size() >= prefix.size() && key.substr(0, prefix.size()) == prefix;
}
// the term of an index key under `prefix` (the bytes after it)
inline std::string_view term_of(std::string_view key, size_t prefix_len) { return key.substr(prefix_len); }
// the 7-digit day token of a millisecond timestamp (Python f"{ms // 86400000:07d}")
std::string day_token(int64_t ms);

}  // namespace evalcpp::dgraph
