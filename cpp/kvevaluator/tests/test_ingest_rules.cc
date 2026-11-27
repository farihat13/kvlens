// The ingest rules every format shares: the chunk cut (ingest/chunker.h), the one-label-at-a-time vertex stream
// (ingest/label_stream.h) and the far-label code (schema/far_labels.h).
#include <functional>
#include <stdexcept>
#include <string>
#include <vector>

#include "harness.h"
#include "ingest/chunker.h"
#include "ingest/label_stream.h"
#include "schema/far_labels.h"

namespace {

using namespace evalcpp;

// the indexes after which a chunk closes, for units of these sizes (the caller closes the last one itself)
std::vector<size_t> cuts(uint64_t target, const std::vector<uint64_t>& ests) {
  GreedyChunker c(target);
  std::vector<size_t> out;
  for (size_t i = 0; i < ests.size(); i++)
    if (c.add(ests[i])) out.push_back(i);
  return out;
}

bool throws(const std::function<void()>& f) {
  try { f(); } catch (const std::runtime_error&) { return true; }
  return false;
}

TEST_CASE(test_ingest_rules) {
  // close after the unit that reaches the target, that unit included
  CHECK(cuts(10, {4, 4, 4, 4, 4, 4}) == std::vector<size_t>({2, 5}));
  CHECK(cuts(10, {10, 1, 9}) == std::vector<size_t>({0, 2}));     // exactly the target closes
  CHECK(cuts(10, {25, 1}) == std::vector<size_t>({0}));           // one unit over the target is its own chunk
  CHECK(cuts(10, {3, 3, 3}).empty());                             // under the target: no cut
  GreedyChunker c(10);
  c.add(9);
  c.reset();                                                      // a forced close (meta: a vid group) starts over
  CHECK(!c.add(9));

  LabelStream s("test");
  CHECK(!s.open());
  CHECK(s.changes("A"));
  s.start("A");
  CHECK(s.open() && s.label() == "A" && !s.changes("A"));
  s.finish();
  s.start("B");
  s.finish();
  s.finish();                                                     // a second finish is a no-op
  CHECK(throws([&] { s.start("A"); }), "a label that reappears is refused");

  CHECK_EQ(far_code_bits(1), 0u);
  CHECK_EQ(far_code_bits(2), 1u);
  CHECK_EQ(far_code_bits(3), 2u);
  CHECK_EQ(far_code_bits(256), 8u);
  std::vector<std::string> far = {"Comment", "Post"};
  CHECK_EQ(far_label_code(far, "Post", "replyOf", true, "test"), 1);
  CHECK(throws([&] { far_label_code(far, "Forum", "replyOf", true, "test"); }), "an undeclared far label is refused");
  std::vector<std::string> many(257);
  for (size_t i = 0; i < many.size(); i++) many[i] = "L" + std::to_string(1000 + i);
  CHECK(throws([&] { check_far_label_count(many, "e", "test"); }), "257 far labels are refused");
  many.pop_back();
  CHECK(!throws([&] { check_far_label_count(many, "e", "test"); }));
  return fails ? 1 : 0;
}

}  // namespace
