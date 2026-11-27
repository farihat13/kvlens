// Equivalence checks for the perf-sweep rewrites whose failure mode is a WRONG NUMBER, not a
// crash: the partial VPack decoder vs the whole-doc decoder (member spans, values, field bytes),
// and replay_landings vs a transcription of the pre-2026-09-11 per-landing scan.
#include <cstdlib>
#include <iostream>
#include <map>
#include <random>
#include <string>
#include <vector>

#include "exec/replay.h"
#include "formats/arangodb/vpack.h"
#include "util/vid.h"
#include "harness.h"

namespace {

using namespace evalcpp;
using namespace evalcpp::arangodb;


// the old replay, verbatim in structure: a linear covered/known scan per landing
static Counts replay_reference(const std::vector<Landing>& landings) {
  struct Entry { uint64_t first, lo, hi; };
  std::map<std::pair<uint64_t, std::string>, std::vector<Entry>> cache;
  Counts c;
  for (const Landing& L : landings) {
    std::vector<Entry>& v = cache[{L.group, L.prefix}];
    bool covered = false;
    for (auto& e : v)
      if (e.lo <= L.probe && L.probe <= e.hi) { covered = true; break; }
    if (covered) continue;
    c.S++;
    if (!L.landed) { c.S_miss++; c.seek_miss_key += L.key_valid ? L.key_bytes : 0; continue; }
    c.scan_key += L.key_bytes;
    bool known = false;
    for (auto& e : v)
      if (e.first == L.first) { known = true; break; }
    if (known) continue;
    c.scan_value += L.chunk_bytes;
    v.push_back({L.first, L.lo, L.hi});
  }
  return c;
}

static bool same(const Counts& a, const Counts& b) {
  return a.S == b.S && a.S_miss == b.S_miss && a.seek_miss_key == b.seek_miss_key &&
         a.scan_key == b.scan_key && a.scan_value == b.scan_value && a.G == b.G && a.N == b.N;
}

TEST_CASE(test_perf_equiv) {
  // --- VPack: a doc with every head kind the LDBC data uses ------------------------------
  std::string longstr(200, 'x');
  Value doc = Value::row({
      {"_key", Value(hex16(0x1000000000000123ULL))},                 // packed vid
      {"_from", Value(std::string("Person/") + hex16(42))},          // packed ref
      {"name", Value("Alice")},                                       // short str
      {"bio", Value(longstr)},                                        // long str (> 126)
      {"n", Value(int64_t(7))},                                       // small int
      {"big", Value(int64_t(1234567890123LL))},                       // multi-byte int
      {"neg", Value(int64_t(-300))},
      {"ok", Value(true)},
      {"none", Value()},
      {"tags", Value::list({Value("a"), Value(int64_t(3)), Value::row({{"k", Value("v")}})})},   // array with a nested object
      {"sub", Value::row({{"x", Value(int64_t(1))}, {"s", Value("yy")}})},
  });
  std::string raw;
  vp_encode_obj(raw, doc);
  Value whole = vp_decode_doc(raw);
  std::vector<std::string> all;
  for (auto& kv : doc.as_row()) all.push_back(kv.first);
  // every member through the partial decoder == the whole-doc decoder
  RowFields part = vp_decode_fields(raw, all);
  CHECK(equals(Value::row(part), whole));
  // each single member: value and framed bytes agree with the whole-doc decode's positions
  for (auto& name : all) {
    RowFields one = vp_decode_fields(raw, {name});
    CHECK(one.size() == 1 && one[0].first == name && equals(one[0].second, whole.field(name)));
    CHECK(vp_doc_field_bytes(raw, {name}) > 0);
  }
  uint64_t sum = 0;
  for (auto& name : all) sum += vp_doc_field_bytes(raw, {name});
  CHECK(sum == vp_doc_field_bytes(raw, all));
  CHECK(vp_decode_fields(raw, {"absent"}).empty());
  CHECK(vp_ref_vid(vp_encode_str("Person/" + hex16(42))) == hex16(42));
  CHECK(vp_ref_vid(vp_encode_str("Person/notavid")) == "notavid");
  // a bigger object (w = 2 frame): 300 members
  RowFields many;
  for (int i = 0; i < 300; i++) many.emplace_back("f" + std::to_string(i), Value(int64_t(i)));
  std::string raw2;
  vp_encode_obj(raw2, Value::row(many));
  CHECK(raw2.size() > 256);
  CHECK(equals(Value::row(vp_decode_fields(raw2, {"f0", "f299", "f150"})),
               Value::row({{"f0", Value(int64_t(0))}, {"f150", Value(int64_t(150))}, {"f299", Value(int64_t(299))}})));
  // corruption still throws (truncated tail)
  bool threw = false;
  try { vp_decode_fields(raw.substr(0, raw.size() - 3), {"name"}); } catch (const std::exception&) { threw = true; }
  CHECK(threw);

  // --- replay: random landings over disjoint chunks, old vs new ---------------------------
  std::mt19937_64 rng(7);
  for (int round = 0; round < 200; round++) {
    // per prefix: chunks [first, last] partitioning an id range, with gaps
    std::vector<std::string> prefixes = {"p", "q"};
    std::map<std::string, std::vector<std::pair<uint64_t, uint64_t>>> chunks;
    for (auto& p : prefixes) {
      uint64_t x = rng() % 50;
      for (int k = 0; k < 1 + int(rng() % 8); k++) {
        uint64_t lo = x + rng() % 20, hi = lo + rng() % 40;
        chunks[p].push_back({lo, hi});
        x = hi + 1 + rng() % 5;
      }
    }
    std::vector<Landing> ls;
    for (int i = 0; i < 300; i++) {
      const std::string& p = prefixes[rng() % 2];
      auto& cs = chunks[p];
      uint64_t probe = rng() % (cs.back().second + 30);
      // the store answers with the last chunk whose first <= probe (or none)
      int at = -1;
      for (size_t k = 0; k < cs.size(); k++) if (cs[k].first <= probe) at = int(k);
      Landing L{rng() % 2, p, probe, at >= 0, 0, 0, 0, 0, true, 9};
      if (at >= 0) { L.first = L.lo = cs[size_t(at)].first; L.hi = cs[size_t(at)].second; L.chunk_bytes = 100 + uint32_t(at); }
      ls.push_back(L);
    }
    CHECK(same(replay_landings(ls), replay_reference(ls)));
  }
  // an overlapping-range producer bug is loud, not a miscount
  {
    std::vector<Landing> bad = {{0, "p", 5, true, 0, 0, 10, 50, true, 9}, {0, "p", 30, true, 8, 8, 40, 50, true, 9}};
    bool t = false;
    try { replay_landings(bad); } catch (const std::exception&) { t = true; }
    CHECK(t);
  }

  if (fails) { std::cerr << fails << " failure(s)\n"; return 1; }
  std::cout << "test_perf_equiv: ok\n";
  return 0;
}

}  // namespace
