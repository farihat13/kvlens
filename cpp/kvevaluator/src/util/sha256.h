// A small SHA-256 (for the record-stream hash in the store manifest).
#pragma once
#include <cstdint>
#include <string>

namespace evalcpp {
class Sha256 {
 public:
  Sha256();
  void update(const char* data, size_t len);
  void update(const std::string& s) { update(s.data(), s.size()); }
  std::string hexdigest();   // finalizes
 private:
  void transform(const uint8_t* chunk);
  uint32_t h_[8];
  uint8_t buf_[64];
  size_t buflen_ = 0;
  uint64_t total_ = 0;
};
std::string sha256_hex(const std::string& s);
}  // namespace evalcpp
