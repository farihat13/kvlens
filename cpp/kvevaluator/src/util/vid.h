// The vid convention (Python vids.py): a vertex id is a 64-bit uint `(tag << 60) | id`,
// carried around as 16 lowercase hex chars so string order == numeric order.
#pragma once
#include <cstdint>
#include <optional>
#include <string>

namespace evalcpp {

constexpr int kVidShift = 60;
constexpr uint64_t kVidIdMask = (uint64_t(1) << kVidShift) - 1;
constexpr uint64_t kVidMaxId = uint64_t(1) << (kVidShift - 1);   // loud guard, as in Python

std::string hex16(uint64_t u);                       // format(u, "016x")
std::optional<uint64_t> uid_of(const std::string& v); // 16 hex chars -> uid, else nullopt
inline uint64_t tag_of_uid(uint64_t u) { return u >> kVidShift; }
inline uint64_t id_of_uid(uint64_t u) { return u & kVidIdMask; }

// Number of Unicode code points in a UTF-8 string (Python len(str)).
size_t utf8_length(const std::string& s);

}  // namespace evalcpp
