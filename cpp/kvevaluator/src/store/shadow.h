// ShadowOverlay -- the copy-on-write overlay a WRITE run buffers its puts and deletes in, so a
// measured store is never mutated (Python store/shadow.py, same rules). Reads merge the base
// store with the overlay; the overlay wins a tie (it is the newer value); a tombstone marks a
// key deleted (distinct from "absent: ask the base"). The executor owns one overlay per plan
// run and RESETS it before every iteration, so a re-run insert stays an insert.
//
// Thread rules: a write op runs as ONE task and is the only mutator; read steps after it may
// consult the overlay from many tasks (read-only). Counting sits ABOVE the shadow
// (store/counting.h): one logical op is one count whether the overlay or the base serves it.
#pragma once
#include <map>
#include <string>
#include <string_view>

namespace evalcpp {

class ShadowOverlay {
 public:
  struct Entry {
    std::string value;
    bool tomb = false;   // deleted (a plain empty value would conflate "deleted" with "empty")
  };
  using Map = std::map<std::string, Entry, std::less<>>;

  void reset() { over_.clear(); }
  bool empty() const { return over_.empty(); }
  size_t pending() const { return over_.size(); }

  void put(std::string_view key, std::string_view value) {
    over_[std::string(key)] = Entry{std::string(value), false};
  }
  void del(std::string_view key) { over_[std::string(key)] = Entry{"", true}; }

  // nullptr = the key is not in the overlay (ask the base)
  const Entry* find(std::string_view key) const {
    auto it = over_.find(key);
    return it == over_.end() ? nullptr : &it->second;
  }
  const Map& map() const { return over_; }

 private:
  Map over_;
};

}  // namespace evalcpp
