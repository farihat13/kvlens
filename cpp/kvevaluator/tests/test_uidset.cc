// util/uidset.h: the flat uid map/set, the canonical-vid guard that gates every string->uid fast
// path, and VidStrSet's migration (uid mode -> string mode keeps every earlier member).
#include <cstdlib>
#include <iostream>
#include <string>
#include <unordered_set>

#include "util/uidset.h"
#include "util/vid.h"
#include "harness.h"

namespace {

using namespace evalcpp;


TEST_CASE(test_uidset) {
  // canonical_vid: exactly 16 lowercase hex; uppercase (which uid_of accepts) is NOT canonical
  uint64_t u = 0;
  CHECK(canonical_vid("00000000000000ab", &u) && u == 0xab);
  CHECK(!canonical_vid("00000000000000AB", &u));
  CHECK(uid_of("00000000000000AB").has_value());        // the guard is stricter than uid_of, by design
  CHECK(!canonical_vid("0000000000000ab", &u));
  CHECK(!canonical_vid("", &u));
  CHECK(!canonical_vid("zzzzzzzzzzzzzzzz", &u));
  CHECK(canonical_vid(hex16(0x123456789abcdefULL), &u) && u == 0x123456789abcdefULL);

  // UidMap / UidSet: insert-once semantics, growth, the reserved ~0 key
  UidMap<uint32_t> m(4);
  for (uint32_t i = 0; i < 100000; i++) CHECK(m.insert(uint64_t(i) * 7919, i).second);
  for (uint32_t i = 0; i < 100000; i++) {
    const uint32_t* p = m.find(uint64_t(i) * 7919);
    CHECK(p && *p == i);
  }
  CHECK(!m.find(1));
  CHECK(!m.insert(0, 42).second);
  CHECK(m.insert(~uint64_t(0), 7).second && *m.find(~uint64_t(0)) == 7 && !m.insert(~uint64_t(0), 8).second);
  CHECK(m.size() == 100001);
  UidSet s;
  CHECK(s.insert(5) && !s.insert(5) && s.contains(5) && !s.contains(6) && s.size() == 1);
  size_t seen = 0;
  s.for_each([&](uint64_t k) { seen += (k == 5); });
  CHECK(seen == 1);

  // VidStrSet: canonical members dedup in uid space; a non-canonical member migrates the set and
  // every earlier member is still a member, with string equality (case matters)
  VidStrSet v(2);
  CHECK(v.insert(hex16(1)) && !v.insert(hex16(1)) && v.insert(hex16(2)));
  CHECK(v.contains(hex16(2)) && !v.contains(hex16(3)));
  CHECK(!v.contains("00000000000000AB"));               // not canonical, not a member either
  CHECK(v.insert("Alice"));                             // migration
  CHECK(v.contains(hex16(1)) && v.contains(hex16(2)) && v.contains("Alice"));
  CHECK(!v.insert(hex16(2)) && !v.insert("Alice"));
  CHECK(v.insert("00000000000000AB") && v.contains("00000000000000AB") && !v.contains("00000000000000ab"));
  CHECK(v.size() == 4);
  // cross-check against a plain string set over a mixed sequence
  {
    VidStrSet a(8);
    std::unordered_set<std::string> b;
    const char* seq[] = {"0000000000000001", "0000000000000002", "0000000000000001", "x", "0000000000000002",
                         "0000000000000003", "x", "0000000000000003", "0000000000000004"};
    for (const char* x : seq) CHECK(a.insert(x) == b.insert(x).second);
    for (const char* x : seq) CHECK(a.contains(x) == (b.count(x) > 0));
    CHECK(a.size() == b.size());
  }

  // insert_uid / contains_uid: a vid by its uid is the member hex16(u), before and after the migration
  {
    VidStrSet a(8);
    std::unordered_set<std::string> b;
    struct Op { bool uid; uint64_t u; const char* s; };
    const Op seq[] = {{true, 1, nullptr}, {false, 0, "0000000000000001"}, {true, 2, nullptr}, {false, 0, "y"},
                      {true, 2, nullptr}, {true, 3, nullptr}, {false, 0, "0000000000000003"}, {true, 0xffffffffffffffffULL, nullptr}};
    for (auto& op : seq) {
      std::string key = op.uid ? hex16(op.u) : std::string(op.s);
      bool want = b.insert(key).second;
      CHECK((op.uid ? a.insert_uid(op.u) : a.insert(op.s)) == want);
    }
    for (uint64_t u : {1ULL, 2ULL, 3ULL, 4ULL, 0xffffffffffffffffULL}) CHECK(a.contains_uid(u) == (b.count(hex16(u)) > 0));
    CHECK(a.contains("y") && a.size() == b.size());
  }

  if (fails) { std::cerr << fails << " failure(s)\n"; return 1; }
  std::cout << "test_uidset: ok\n";
  return 0;
}

}  // namespace
