// Unit tests: the neutral label helpers (exec/label_tags.h) -- what a value's label is, how a list is tagged, how a
// carried label narrows a candidate list, and the first-tagged-occurrence rule for a vid that arrives more than once.
#include <iostream>

#include "exec/label_tags.h"
#include "formats/common.h"
#include "schema/schema.h"
#include "repo_paths.h"
#include "harness.h"

namespace {

using namespace evalcpp;


TEST_CASE(test_label_tags) {
  Schema schema = Schema::load(ldbc_schema_dir() + "/schema.json");
  Value bare("a"), post = tagged(schema, Value("a"), "Post"), comment = tagged(schema, Value("b"), "Comment");

  // a label on a value: by name; none on a bare value; an empty label leaves the value alone
  CHECK(!tag_label(schema, bare));
  CHECK(tag_label(schema, post) && *tag_label(schema, post) == "Post");
  CHECK(tagged(schema, Value("x"), "").label == -1);
  CHECK(equals(post, bare));                                        // the label is not part of the value's identity

  // tag_all: every vid of the list, the strings untouched
  Value all = tag_all(schema, Value::list({Value("a"), Value("b")}), "Forum");
  CHECK(all.as_list().size() == 2 && all.as_list()[1].as_str() == "b");
  CHECK(*tag_label(schema, all.as_list()[0]) == "Forum" && *tag_label(schema, all.as_list()[1]) == "Forum");

  // narrow: a carried label picks itself, rules the list out when it is another label, and no label keeps the list
  std::vector<std::string> pc = {"Post", "Comment"};
  CHECK(narrow(schema, post, pc) == std::vector<std::string>{"Post"});
  CHECK(narrow(schema, comment, pc) == std::vector<std::string>{"Comment"});
  CHECK(narrow(schema, tagged(schema, Value("f"), "Forum"), pc).empty());
  CHECK(narrow(schema, bare, pc) == pc);

  // distinct_values: the first TAGGED occurrence decides, wherever the untagged twin sits
  ValueList in = {Value("a"), post, comment, Value("b"), Value("c")};
  std::vector<Value> d = distinct_values(in, distinct_strs(in), false);
  CHECK(d.size() == 3);
  CHECK(d[0].as_str() == "a" && tag_label(schema, d[0]) && *tag_label(schema, d[0]) == "Post");      // untagged first
  CHECK(d[1].as_str() == "b" && tag_label(schema, d[1]) && *tag_label(schema, d[1]) == "Comment");   // tagged first
  CHECK(d[2].as_str() == "c" && !tag_label(schema, d[2]));
  // no tag anywhere: the bare strings
  ValueList plain = {Value("a"), Value("a"), Value("b")};
  std::vector<Value> p = distinct_values(plain, distinct_strs(plain), true);
  CHECK(p.size() == 2 && p[0].label == -1 && p[1].label == -1);
  // strict (the checkers' trace mode): one vid under two labels is a bug upstream
  ValueList two = {post, tagged(schema, Value("a"), "Comment")};
  bool threw = false;
  try { distinct_values(two, distinct_strs(two), true); } catch (const std::runtime_error&) { threw = true; }
  CHECK(threw);
  CHECK(*tag_label(schema, distinct_values(two, distinct_strs(two), false)[0]) == "Post");   // not strict: the first

  std::cout << (fails ? "test_label_tags FAILED\n" : "test_label_tags OK\n");
  return fails ? 1 : 0;
}

}  // namespace
