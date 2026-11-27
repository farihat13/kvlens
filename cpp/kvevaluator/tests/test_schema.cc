// Schema's adj/prop_groups parsing against the COMMITTED artifacts Python emits
// (workloads/ldbc/schema*.json) -- the step-3 S1 contract: the C++ accessors must read the
// same maps Python compiled, absent blocks must load as empty (older artifacts), and the
// content_sha must move when only a post-hash block changes (the trap the dataset hash
// cannot see).
#include <cassert>
#include <fstream>
#include <iostream>
#include <string>

#include "schema/schema.h"
#include "repo_paths.h"
#include "harness.h"

namespace {

using evalcpp::Schema;
using evalcpp::json;

static std::string ldbc_dir() {
  return ldbc_schema_dir();
}

static json load_json(const std::string& path) {
  std::ifstream f(path);
  if (!f) { std::cerr << "no artifact at " << path << "\n"; exit(1); }
  return json::parse(f);
}

TEST_CASE(test_schema) {
  // canonical: empty blocks, "in" default everywhere
  Schema canon = Schema::load(ldbc_dir() + "/schema.json");
  CHECK(canon.adj_sort().empty() && canon.adj_inline().empty() && canon.adj_sort_facet().empty());
  CHECK(canon.prop_groups().empty());
  CHECK(!canon.is_dated_etype("likes"));
  CHECK(canon.adj_direction("hasCreator") == "in");
  CHECK(canon.content_sha().size() == 64);

  // both dated families + the inline copies, all declared "in" (the blocks a date-sorted adjacency design
  // declared; its schema files were retired, the directives are still parsed)
  json dated = canon.to_json();
  dated["adj"] = json::parse(R"({"sort": {"hasCreator": "creationDate", "replyOf": "creationDate"},
      "inline": {"hasCreator": "creationDate", "replyOf": "creationDate"}, "sort_facet": {"likes": "creationDate"},
      "dir": {"hasCreator": "in", "likes": "in", "replyOf": "in"}})");
  Schema pi = Schema::from_json(dated);
  CHECK(pi.adj_sort().at("hasCreator") == "creationDate");
  CHECK(pi.adj_sort().at("replyOf") == "creationDate");
  CHECK(pi.adj_inline().at("hasCreator") == "creationDate");
  CHECK(pi.adj_sort_facet().at("likes") == "creationDate");
  CHECK(pi.is_dated_etype("likes") && pi.is_dated_etype("hasCreator") && !pi.is_dated_etype("knows"));
  CHECK(pi.adj_direction("likes") == "in");
  CHECK(pi.group_of_pred("content").empty());

  // a property group on the canonical indexes (the grouped-and-indexed design; retired)
  json grouped = canon.to_json();
  grouped["prop_groups"] = json::parse(R"({"g1": ["firstName", "lastName"]})");
  Schema pg = Schema::from_json(grouped);
  CHECK(pg.prop_groups().at("g1") == (std::vector<std::string>{"firstName", "lastName"}));
  CHECK(pg.group_of_pred("firstName") == "g1" && pg.group_of_pred("lastName") == "g1");
  CHECK(pg.group_of_pred("creationDate").empty());
  CHECK(pg.is_indexed("Person", "firstName"));   // the branch this variant exists for
  CHECK(pg.adj_sort().empty());

  // absent blocks (an older artifact): loads, empty maps
  json stripped = canon.to_json();
  stripped.erase("adj");
  stripped.erase("prop_groups");
  Schema old = Schema::from_json(stripped);
  CHECK(old.adj_sort().empty() && old.prop_groups().empty());
  CHECK(old.adj_direction("likes") == "in");

  // THE TRAP: edit only a post-hash adj line -> dataset hash identical, content_sha moves
  json tweaked = pi.to_json();
  tweaked["adj"]["sort"]["hasCreator"] = "length";
  Schema tw = Schema::from_json(tweaked);
  CHECK(tw.hash() == pi.hash());
  CHECK(tw.content_sha() != pi.content_sha());

  std::cout << "schema adj/prop_groups + content_sha: all assertions passed\n";
  return 0;
}

}  // namespace
