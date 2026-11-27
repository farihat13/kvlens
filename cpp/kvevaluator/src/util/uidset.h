// Flat open-addressing hash set / map keyed by a uint64 vid. The read ops dedup, probe and
// re-assemble in vid space; std::unordered_set allocates a node per element and chases a
// pointer per probe, which is most of what a multi-million-vid step pays (perf sweep).
// Neutral infra: any format or flow op may use it.
//
// canonical_vid: the fast paths that replace a STRING set by a uid set are only taken when
// every item is the canonical vid rendering (16 lowercase hex chars), checked in the same
// scan -- then string equality == uid equality and the dedup/probe result is identical.
// uid_of() also accepts uppercase; such strings, and any non-vid value, must keep the
// Value/string path.
#pragma once
#include <cstdint>
#include <string>
#include <string_view>
#include <unordered_set>
#include <utility>
#include <vector>

#include "util/vid.h"

namespace evalcpp {

// s is exactly 16 lowercase hex chars; on success *out = the uid.
inline bool canonical_vid(std::string_view s, uint64_t* out) {
  if (s.size() != 16) return false;
  uint64_t u = 0;
  for (char c : s) {
    int x;
    if (c >= '0' && c <= '9') x = c - '0';
    else if (c >= 'a' && c <= 'f') x = c - 'a' + 10;
    else return false;
    u = (u << 4) | uint64_t(x);
  }
  *out = u;
  return true;
}

// Map uid -> V (V trivially copyable). Linear probing over a power-of-two table kept at most
// half full; a slot is free while it holds kEmpty, so ~0 itself is tracked separately (a vid
// never is ~0 -- ids are < 2^59 -- but ordinals or other keys may be).
template <class V>
class UidMap {
 public:
  explicit UidMap(size_t expected = 0) { reserve(expected); }

  void reserve(size_t expected) {
    size_t cap = 16;
    while (cap < expected * 2) cap <<= 1;
    if (cap <= keys_.size()) return;
    rehash(cap);
  }
  size_t size() const { return n_ + (has_empty_ ? 1 : 0); }

  // insert if absent; returns (slot value pointer, inserted)
  std::pair<V*, bool> insert(uint64_t k, V v) {
    if (k == kEmpty) {
      bool fresh = !has_empty_;
      if (fresh) { has_empty_ = true; empty_v_ = v; }
      return {&empty_v_, fresh};
    }
    if ((n_ + 1) * 2 > keys_.size()) rehash(keys_.size() * 2);
    size_t i = slot(k);
    while (keys_[i] != kEmpty) {
      if (keys_[i] == k) return {&vals_[i], false};
      i = (i + 1) & mask_;
    }
    keys_[i] = k;
    vals_[i] = v;
    n_++;
    return {&vals_[i], true};
  }
  V* find(uint64_t k) {
    if (k == kEmpty) return has_empty_ ? &empty_v_ : nullptr;
    if (keys_.empty()) return nullptr;
    size_t i = slot(k);
    while (keys_[i] != kEmpty) {
      if (keys_[i] == k) return &vals_[i];
      i = (i + 1) & mask_;
    }
    return nullptr;
  }
  const V* find(uint64_t k) const { return const_cast<UidMap*>(this)->find(k); }
  bool contains(uint64_t k) const { return find(k) != nullptr; }
  template <class F>
  void for_each(F f) const {   // (key, value) in table order -- NOT insertion order
    for (size_t i = 0; i < keys_.size(); i++)
      if (keys_[i] != kEmpty) f(keys_[i], vals_[i]);
    if (has_empty_) f(kEmpty, empty_v_);
  }

 private:
  static constexpr uint64_t kEmpty = ~uint64_t(0);
  size_t slot(uint64_t k) const {   // splitmix64 finalizer: vids are dense in the low bits
    k ^= k >> 30; k *= 0xbf58476d1ce4e5b9ULL;
    k ^= k >> 27; k *= 0x94d049bb133111ebULL;
    k ^= k >> 31;
    return size_t(k) & mask_;
  }
  void rehash(size_t cap) {
    std::vector<uint64_t> ok = std::move(keys_);
    std::vector<V> ov = std::move(vals_);
    keys_.assign(cap, kEmpty);
    vals_.assign(cap, V());
    mask_ = cap - 1;
    n_ = 0;
    for (size_t i = 0; i < ok.size(); i++)
      if (ok[i] != kEmpty) insert(ok[i], ov[i]);
  }
  std::vector<uint64_t> keys_;
  std::vector<V> vals_;
  size_t mask_ = 0, n_ = 0;
  bool has_empty_ = false;
  V empty_v_ = V();
};

class UidSet {
 public:
  explicit UidSet(size_t expected = 0) : m_(expected) {}
  void reserve(size_t expected) { m_.reserve(expected); }
  bool insert(uint64_t k) { return m_.insert(k, 0).second; }   // true = new
  bool contains(uint64_t k) const { return m_.contains(k); }
  size_t size() const { return m_.size(); }
  template <class F>
  void for_each(F f) const { m_.for_each([&](uint64_t k, char) { f(k); }); }
 private:
  UidMap<char> m_;
};

// A set of STRINGS with string-equality semantics, kept in uid space while every member is a
// canonical vid. The first non-canonical member migrates the set to a std::string set (every
// uid re-rendered with hex16, which is exactly the canonical string it came from), so the
// answer never depends on the mode. Insert/contains are what a dedup or membership probe needs.
class VidStrSet {
 public:
  explicit VidStrSet(size_t expected = 0) : uids_(expected) {}
  void reserve(size_t expected) { if (uid_mode_) uids_.reserve(expected); else strs_.reserve(expected); }
  bool insert(const std::string& s) {   // true = new
    uint64_t u;
    if (uid_mode_) {
      if (canonical_vid(s, &u)) return uids_.insert(u);
      migrate();
    }
    return strs_.insert(s).second;
  }
  bool contains(const std::string& s) const {
    uint64_t u;
    if (uid_mode_) return canonical_vid(s, &u) && uids_.contains(u);
    return strs_.count(s) > 0;
  }
  // a vid by its uid: what insert / contains do with hex16(u), without rendering it (no heap string
  // per vid on every dedup and closure level); in string mode it is its canonical text
  bool insert_uid(uint64_t u) { return uid_mode_ ? uids_.insert(u) : strs_.insert(hex16(u)).second; }
  bool contains_uid(uint64_t u) const { return uid_mode_ ? uids_.contains(u) : strs_.count(hex16(u)) > 0; }
  size_t size() const { return uid_mode_ ? uids_.size() : strs_.size(); }
 private:
  void migrate() {
    strs_.reserve(uids_.size() * 2);
    uids_.for_each([&](uint64_t u) { strs_.insert(hex16(u)); });
    uids_ = UidSet();
    uid_mode_ = false;
  }
  bool uid_mode_ = true;
  UidSet uids_;
  std::unordered_set<std::string> strs_;
};

}  // namespace evalcpp
