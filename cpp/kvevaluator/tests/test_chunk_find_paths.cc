// lists::ChunkView's two find paths agree: the sampled path (a skip sample + at most 15 gaps) and the owner array (built
// once a view has served k/16 finds) give every owner its index and every absent id -1, on random chunks (k = 1 to
// 5,000, small and huge gaps, one or two vid groups, label bits 0 / 2, plain and ordinals chunks). find() itself gives
// the same answers before and after the switch, in random order, and from eight threads on one fresh view. The
// array build books no decode. tests/run-chunk-view-sanitized.sh builds this test under ASan + UBSan, and with a planted
// off-by-one in the array path (it must fail).
#include <algorithm>
#include <cstdint>
#include <iostream>
#include <random>
#include <string>
#include <thread>
#include <vector>

#include "codec/decode_counts.h"
#include "codec/lists.h"
#include "formats/rows.h"
#include "value/value.h"
#include "harness.h"

namespace {

using namespace evalcpp;
static long checks = 0;

static const std::vector<ColDecl> kNone = {};

struct Case {
  std::vector<lists::ChunkOwner> owners;
  std::vector<uint64_t> ords;
  std::string raw;
  unsigned bits = 0;
  bool with_ords = false;
};

static Case make(std::mt19937_64& rng, size_t k, bool two_groups, unsigned bits, bool with_ords) {
  Case c;
  c.bits = bits;
  c.with_ords = with_ords;
  uint64_t src = rng() % 1000;
  for (size_t o = 0; o < k; o++) {
    uint64_t r = rng() % 100;
    src += r < 60 ? 1 : r < 90 ? 2 + rng() % 30 : 1 + rng() % 5000000;           // dense runs, small and huge gaps
    if (two_groups && o == k / 2) src = (uint64_t(1) << 60) + rng() % 1000;    // the second half in vid group 1
    lists::ChunkOwner ow{src, {}, {}};
    size_t deg = rng() % 4;
    uint64_t d = rng() % 50;
    for (size_t j = 0; j < deg; j++) {
      d += 1 + rng() % 1000;
      ow.dsts.push_back(d);
      if (bits) ow.labels.push_back(uint8_t(rng() % (1u << bits)));
      if (with_ords) c.ords.push_back(rng() % 100000);
    }
    c.owners.push_back(ow);
  }
  c.raw = lists::encode_chunk(c.owners, bits, kNone, nullptr, with_ords ? &c.ords : nullptr);
  return c;
}

// the ids to probe: every owner, each owner +-1, below the first, above the last, 0 and the max id
static std::vector<std::pair<uint64_t, int64_t>> probes(const Case& c) {
  std::vector<uint64_t> ids;
  for (auto& o : c.owners) ids.push_back(o.src);
  std::vector<std::pair<uint64_t, int64_t>> out;
  auto expect = [&](uint64_t x) {
    auto it = std::lower_bound(ids.begin(), ids.end(), x);
    return it != ids.end() && *it == x ? int64_t(it - ids.begin()) : int64_t(-1);
  };
  for (uint64_t x : ids) {
    out.push_back({x, expect(x)});
    out.push_back({x + 1, expect(x + 1)});
    if (x) out.push_back({x - 1, expect(x - 1)});
  }
  out.push_back({0, expect(0)});
  out.push_back({UINT64_MAX, expect(UINT64_MAX)});
  return out;
}

static size_t check_case(const Case& c, std::mt19937_64& rng, const std::string& tag) {
  std::vector<std::pair<uint64_t, int64_t>> ps = probes(c);
  {   // the two paths on one view: the sampled path first (the array not built), then the array
    lists::ChunkView cv(&kNone, c.raw, c.bits, c.with_ords);
    for (auto& [x, want] : ps) CHECK(cv.find_sampled(x) == want, tag << " sampled " << x << ": want " << want);
    uint64_t before = decode_counts().chunks + decode_counts().bytes + decode_counts().fields;
    for (auto& [x, want] : ps) {
      CHECK(cv.find_array(x) == want, tag << " array " << x << ": want " << want);
    }
    CHECK(decode_counts().chunks + decode_counts().bytes + decode_counts().fields == before, tag << ": the array build booked a decode");
  }
  {   // find() on a fresh view in random order: the switch happens partway through, the answers do not change
    lists::ChunkView cv(&kNone, c.raw, c.bits, c.with_ords);
    std::vector<std::pair<uint64_t, int64_t>> sh = ps;
    std::shuffle(sh.begin(), sh.end(), rng);
    for (auto& [x, want] : sh) CHECK(cv.find(x) == want, tag << " find " << x << ": want " << want);
  }
  if (c.owners.size() >= 64) {   // eight threads' first finds on one fresh view race the array build
    lists::ChunkView cv(&kNone, c.raw, c.bits, c.with_ords);
    std::vector<int> bad(8, 0);
    std::vector<std::thread> ts;
    for (int t = 0; t < 8; t++)
      ts.emplace_back([&, t] {
        for (size_t p = size_t(t); p < ps.size(); p += 3) bad[t] += cv.find(ps[p].first) != ps[p].second;
      });
    for (auto& t : ts) t.join();
    for (int t = 0; t < 8; t++) CHECK(bad[t] == 0, tag << " thread " << t << ": " << bad[t] << " wrong finds");
  }
  return ps.size();
}

TEST_CASE(test_chunk_find_paths) {
  std::mt19937_64 rng(20261023);
  size_t cases = 0, probed = 0;
  const size_t ks[] = {1, 2, 15, 16, 17, 31, 32, 33, 47, 48, 49, 100, 255, 256, 257, 1000, 4682, 5000};
  for (size_t k : ks)
    for (bool two : {false, true})
      for (unsigned bits : {0u, 2u})
        for (bool ords : {false, true})
          for (int rep = 0; rep < 3; rep++) {
            if (two && k < 2) continue;
            Case c = make(rng, k, two, bits, ords);
            std::string tag = "k=" + std::to_string(k) + (two ? " two-groups" : "") + " bits=" + std::to_string(bits) +
                              (ords ? " ords" : "") + " rep=" + std::to_string(rep);
            probed += check_case(c, rng, tag);
            cases++;
          }
  for (int r = 0; r < 300; r++) {   // random sizes
    size_t k = 1 + rng() % 3000;
    Case c = make(rng, k, rng() % 4 == 0 && k >= 2, (rng() % 2) * 2, rng() % 2);
    probed += check_case(c, rng, "random k=" + std::to_string(k));
    cases++;
  }
  std::cout << "test_chunk_find_paths: " << cases << " chunks, " << probed << " ids probed per path, " << checks
            << " checks, " << fails << " failures\n";
  return fails ? 1 : 0;
}

}  // namespace
