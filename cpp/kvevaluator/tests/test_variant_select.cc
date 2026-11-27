// The bundle selector (ir/plan.cc select_variant via flatten_ir): first variant whose
// `requires` the caps hold and whose `lacks` they do not; `on`-annotated caps are checked
// against the label's owner. Mirrors tests/unit/check_planir.py test_bundle's negative-selector cases.
#include <iostream>
#include <set>
#include <string>

#include "ir/plan.h"
#include "harness.h"

namespace {

using namespace evalcpp;


static std::string chosen(const json& flat) { return flat.at("steps").at(0).at("name").get<std::string>(); }

TEST_CASE(test_variant_select) {
  json bundle = json::parse(R"({
    "plan": "t",
    "variants": [
      {"requires": ["whole_row_read"], "lacks": ["index_entries_by_docid"], "plan": "plain"},
      {"requires": ["whole_row_read"], "plan": "one_phase"},
      {"requires": [], "plan": "scan"}],
    "plans": {"plain": {"steps": [{"name": "plain", "op": "count", "items": {"ref": "x"}}], "output": "plain"},
              "one_phase": {"steps": [{"name": "one_phase", "op": "count", "items": {"ref": "x"}}], "output": "one_phase"},
              "scan": {"steps": [{"name": "scan", "op": "count", "items": {"ref": "x"}}], "output": "scan"}}})");
  std::set<std::string> nebula = {"per_type_index", "sorted_range_index", "type_index", "whole_row_read"};
  std::set<std::string> arango = nebula;
  arango.insert("type_index_single_posting");
  arango.insert("index_entries_by_docid");
  std::set<std::string> dgraph = {"type_index", "type_index_single_posting", "unsorted_range_index"};
  CHECK(chosen(flatten_ir(bundle, nebula)) == "plain", "nebula: holds whole rows, lacks the docid index -> plain");
  CHECK(chosen(flatten_ir(bundle, arango)) == "one_phase", "arango: holds the lacked cap -> skips plain");
  CHECK(chosen(flatten_ir(bundle, dgraph)) == "scan", "dgraph: no whole rows -> fallback");
  // `on`-annotated lacks: the label's OWNER must lack it
  json on = json::parse(R"({
    "plan": "t",
    "variants": [{"requires": [], "lacks": ["whole_row_read"], "on": {"whole_row_read": ["Post"]}, "plan": "a"},
                 {"requires": [], "plan": "b"}],
    "plans": {"a": {"steps": [{"name": "a", "op": "count", "items": {"ref": "x"}}], "output": "a"},
              "b": {"steps": [{"name": "b", "op": "count", "items": {"ref": "x"}}], "output": "b"}}})");
  CapsOfLabel post_rows = [](const std::string& l) { return l == "Post" ? std::set<std::string>{"whole_row_read"} : std::set<std::string>{}; };
  CapsOfLabel none = [](const std::string&) { return std::set<std::string>{}; };
  CHECK(chosen(flatten_ir(on, {}, post_rows)) == "b", "owner of Post holds the lacked cap -> b");
  CHECK(chosen(flatten_ir(on, {}, none)) == "a", "no owner holds it -> a");
  // an unknown capability in `lacks` is refused like one in `requires`
  json bad = json::parse(R"({"plan": "t", "variants": [{"requires": [], "lacks": ["no_such_cap"], "plan": "x"}],
                             "plans": {"x": {"steps": [], "output": "x"}}})");
  bool threw = false;
  try { flatten_ir(bad, {}); } catch (const std::exception&) { threw = true; }
  CHECK(threw, "unknown cap in lacks throws");
  // per-scale pins: a `scales`-tagged variant runs only at a listed scale, the untagged one is the
  // default elsewhere, a tagged variant with no scale known throws (mirrors check_planir.py)
  json sc = json::parse(R"({"plan": "t",
    "variants": [{"requires": [], "scales": ["0.1"], "plan": "small"}, {"requires": [], "plan": "big"}],
    "plans": {"small": {"steps": [{"name": "small", "op": "count", "items": {"ref": "x"}}], "output": "small"},
              "big": {"steps": [{"name": "big", "op": "count", "items": {"ref": "x"}}], "output": "big"}}})");
  CHECK(chosen(flatten_ir(sc, {}, {}, "0.1")) == "small", "scale 0.1 listed -> the tagged variant");
  CHECK(chosen(flatten_ir(sc, {}, {}, "30")) == "big", "scale 30 unlisted -> the default");
  threw = false;
  try { flatten_ir(sc, {}); } catch (const std::exception&) { threw = true; }
  CHECK(threw, "a scale-tagged variant with no scale known throws");
  CHECK(chosen(flatten_ir(bundle, nebula, {}, "30")) == "plain", "an untagged bundle ignores the scale");
  // the scale decides only among variants whose CAPS match: a tagged variant for another format's caps neither
  // selects nor throws on a scale-blind caller; a non-string tag or a non-list is refused
  json sc2 = json::parse(R"({"plan": "t",
    "variants": [{"requires": ["whole_row_read"], "scales": ["0.1"], "plan": "rows_small"},
                 {"requires": ["whole_row_read"], "plan": "rows"}, {"requires": [], "plan": "scan"}],
    "plans": {"rows_small": {"steps": [{"name": "rows_small", "op": "count", "items": {"ref": "x"}}], "output": "rows_small"},
              "rows": {"steps": [{"name": "rows", "op": "count", "items": {"ref": "x"}}], "output": "rows"},
              "scan": {"steps": [{"name": "scan", "op": "count", "items": {"ref": "x"}}], "output": "scan"}}})");
  CHECK(chosen(flatten_ir(sc2, dgraph)) == "scan", "dgraph: the tagged variant's caps do not match -> no scale needed");
  CHECK(chosen(flatten_ir(sc2, nebula, {}, "0.1")) == "rows_small", "nebula at 0.1 -> the tagged variant");
  CHECK(chosen(flatten_ir(sc2, nebula, {}, "3")) == "rows", "nebula at 3 -> the default");
  threw = false;
  try { flatten_ir(sc2, nebula); } catch (const std::exception&) { threw = true; }
  CHECK(threw, "nebula matches the tagged variant: no scale known throws");
  for (const char* bad : {R"({"plan":"t","variants":[{"requires":[],"scales":"30","plan":"x"}],"plans":{"x":{"steps":[],"output":"x"}}})",
                          R"({"plan":"t","variants":[{"requires":[],"scales":[],"plan":"x"}],"plans":{"x":{"steps":[],"output":"x"}}})",
                          R"({"plan":"t","variants":[{"requires":[],"scales":[3],"plan":"x"}],"plans":{"x":{"steps":[],"output":"x"}}})"}) {
    threw = false;
    try { flatten_ir(json::parse(bad), {}, {}, "30"); } catch (const std::exception&) { threw = true; }
    CHECK(threw, "a malformed `scales` is refused");
  }
  // the body sha is the same for the same body whatever the scale (plan.h body_sha_of)
  CHECK(body_sha_of(flatten_ir(sc, {}, {}, "30")) == body_sha_of(flatten_ir(sc, {}, {}, "3")),
        "body sha depends on the body alone (both scales select `big`)");
  CHECK(body_sha_of(flatten_ir(sc, {}, {}, "30")) != body_sha_of(flatten_ir(sc, {}, {}, "0.1")),
        "a different body, a different sha");
  // a pair test (where, where_key, where_in, where_not_in) on any op but join_multi is refused, not ignored; on
  // join_multi it is taken (interp.py ir_plan_to_steps refuses the same)
  const std::string src_x = R"({"name": "x", "op": "read_vertex_by_id", "vid": {"param": "a"}, "label": "Person"}, )";
  const std::string src_y = R"({"name": "y", "op": "read_vertex_by_id", "vid": {"param": "b"}, "label": "Person"}, )";
  for (const char* arg : {R"("where": {"lit": true})", R"("where_key": {"field": "a"})", R"("where_in": {"ref": "x"})",
                          R"("where_not_in": {"ref": "x"})"}) {
    std::string why;
    try {
      prepare_plan(json::parse(R"({"steps": [)" + src_x + R"({"name": "f", "op": "filter", "items": {"ref": "x"}, )" +
                               arg + R"(}], "output": "f"})"),
                   {});
    } catch (const std::exception& e) { why = e.what(); }
    CHECK(why.find("is join_multi's only") != std::string::npos,
          "a pair test on a filter is refused: " << arg << ": " << why);
  }
  std::string why;
  try {
    prepare_plan(json::parse(R"({"steps": [)" + src_x + src_y +
                             R"({"name": "j", "op": "join_multi", "left": {"ref": "x"}, "right": {"ref": "y"},
                                 "left_on": "a", "right_on": "a", "where": {"lit": true}}], "output": "j"})"),
                 {});
  } catch (const std::exception& e) { why = e.what(); }
  CHECK(why.empty(), "a pair test on join_multi is taken: " << why);
  if (fails) { std::cerr << fails << " failures\n"; return 1; }
  std::cout << "test_variant_select ok\n";
  return 0;
}

}  // namespace
