// A shared chunk's parse goes to the part of the first landing on it in task order (exec/replay.h,
// credit_chunk_parses), whichever thread parsed it: the probe-miss case (a probe of one part lands on another
// part's chunk) included, and then the part's parsed bytes equal the chunk value bytes the replay charges it.
#include <stdexcept>
#include <string>
#include <vector>

#include "exec/replay.h"
#include "harness.h"

namespace {

using namespace evalcpp;

// part 1 "message" for a probe id under 1000, part 2 "forum" from 1000 (id 0: the catch-all)
struct TwoParts : PartScheme {
  size_t size() const override { return 3; }
  size_t part_of(std::string_view key) const override {
    if (key.size() < 8) return 0;
    uint64_t id = 0;
    for (size_t i = key.size() - 8; i < key.size(); i++) id = (id << 8) | uint8_t(key[i]);
    return id < 1000 ? 1 : 2;
  }
  std::string name(size_t id) const override { return id == 1 ? "message" : id == 2 ? "forum" : "unknown"; }
};

// true when f throws std::runtime_error
template <class F>
bool refused(F f) {
  try {
    f();
  } catch (const std::runtime_error&) {
    return true;
  }
  return false;
}

constexpr size_t kMessage = 1, kForum = 2;

// a probe of `probe` that lands on the chunk [100, 200] (500 B) of prefix "adj", in cache `cache`
Landing on_chunk(uint64_t probe, uint64_t cache = 7) { return {0, "adj", probe, true, 100, 100, 200, 500, true, 11, cache}; }

DecodeCounts parse(uint64_t bytes) {
  DecodeCounts d;
  d.chunks = 1;
  d.bytes = bytes;
  return d;
}

// the decode credit and the replay's per-part counts of `landings`
struct Credit {
  std::vector<DecodeCounts> decoded;
  PartCounts parts;
};
Credit credit(const std::vector<Landing>& landings, const std::vector<ChunkParse>& parses) {
  TwoParts scheme;
  Credit c;
  replay_landings(landings, &scheme, &c.parts);
  credit_chunk_parses(landings, parses, scheme, c.decoded);
  return c;
}

}  // namespace

TEST_CASE(chunk_parse_credit_task_order) {
  std::vector<ChunkParse> parses = {{7, "adj", 100, parse(500)}};
  // the message probe first: the parse is the message part's, as the chunk's value bytes are
  Credit a = credit({on_chunk(150), on_chunk(1500)}, parses);
  CHECK_EQ(a.decoded[kMessage].bytes, uint64_t(500));
  CHECK_EQ(a.decoded[kForum].bytes, uint64_t(0));
  // the probe miss first (a forum probe past the chunk's last owner): the parse follows it, with the value bytes
  Credit b = credit({on_chunk(1500), on_chunk(150)}, parses);
  CHECK_EQ(b.decoded[kForum].bytes, uint64_t(500));
  CHECK_EQ(b.decoded[kForum].chunks, uint64_t(1));
  CHECK_EQ(b.decoded[kMessage].bytes, uint64_t(0));
  for (const Credit* c : {&a, &b})   // per part: the parsed bytes are the chunk value bytes the replay charged it
    for (size_t p = 0; p < 3; p++) CHECK_EQ(c->decoded[p].bytes, c->parts[p].scan_value);
  return fails ? 1 : 0;
}

TEST_CASE(chunk_parse_credit_cache_and_order) {
  // another cache's landing on the same chunk is not this parse's; the parses' own order does not matter
  Landing other = on_chunk(1500, 8);
  Landing second = {0, "adj", 1600, true, 300, 300, 400, 200, true, 11, 7};   // a second chunk, a forum probe
  Landing missed = {0, "adj", 50, false, 0, 0, 0, 0, false, 0, 7};           // no chunk at or before: no parse
  Credit c = credit({missed, other, on_chunk(150), second}, {{7, "adj", 300, parse(200)}, {7, "adj", 100, parse(500)}});
  CHECK_EQ(c.decoded[kMessage].bytes, uint64_t(500));
  CHECK_EQ(c.decoded[kForum].bytes, uint64_t(200));
  return fails ? 1 : 0;
}

TEST_CASE(chunk_parse_credit_refusals) {
  TwoParts scheme;
  std::vector<DecodeCounts> d;
  // a parse no landing reached (another cache's landing does not count)
  CHECK(refused([&] { credit_chunk_parses({on_chunk(150, 8)}, {{7, "adj", 100, parse(500)}}, scheme, d); }));
  // one chunk parsed twice in one cache
  CHECK(refused([&] {
    credit_chunk_parses({on_chunk(150)}, {{7, "adj", 100, parse(500)}, {7, "adj", 100, parse(500)}}, scheme, d);
  }));
  // no parses: nothing credited, no landing needed
  std::vector<DecodeCounts> none;
  credit_chunk_parses({}, {}, scheme, none);
  CHECK(none.empty());
  return fails ? 1 : 0;
}
