// load_manifest_schema (schema/schema_path.h), the census / size --roles reader: a path under the root is recorded
// relative and loads from this clone, a path outside it is recorded absolute and loads as recorded, a gone file stops loudly, and a schema whose hash or content_sha
// differs from the manifest's is refused (also through a --schema override). The path rule's case table is
// tests/unit/check_manifest_schema_path.py, against Python's resolver.
#include <filesystem>
#include <iostream>
#include <stdexcept>
#include <string>

#include "schema/schema_path.h"
#include "harness.h"

namespace {

using namespace evalcpp;


static const std::string kRel = std::string(KVEVAL_LDBC_SCHEMA_DIR) + "/schema_label_only.json";   // repo-relative (CMakeLists.txt)

static std::string error_of(const json& manifest, const std::string& given = "") {
  try {
    load_manifest_schema(manifest, "/store", "test", given);
    return "";
  } catch (const std::exception& e) {
    return e.what();
  }
}

TEST_CASE(test_schema_path) {
  Schema s = Schema::load(repo_root() + "/" + kRel);
  json ok = {{"schema", {{"name", s.name()}, {"hash", s.hash()}, {"content_sha", s.content_sha()}, {"path", kRel}}}};
  CHECK(error_of(ok).empty(), "a relative path loads from this tree: " << error_of(ok));
  CHECK(record_schema_path(repo_root() + "/" + kRel) == kRel, "ingest records the relative form");

  // a file outside the root: recorded absolute, loaded as recorded
  std::string outside = (std::filesystem::temp_directory_path() / "kveval_test_schema_outside.json").string();
  std::filesystem::copy_file(repo_root() + "/" + kRel, outside, std::filesystem::copy_options::overwrite_existing);
  CHECK(record_schema_path(outside) == outside, "a path outside the root is recorded absolute");
  json other = ok;
  other["schema"]["path"] = outside;
  CHECK(error_of(other).empty(), "an absolute path loads as recorded: " << error_of(other));
  std::filesystem::remove(outside);

  json gone = ok;
  gone["schema"]["path"] = "inputs/no_such_schema.json";
  CHECK(error_of(gone).find("in neither the recorded place nor this tree") != std::string::npos,
        "a gone file stops loudly: " << error_of(gone));
  CHECK(!resolve_schema_path(kRel, "/nonexistent-clone"), "a gone build tree resolves nothing relative");

  json sha = ok;
  sha["schema"]["content_sha"] = "0000000000000000";
  CHECK(error_of(sha).find("content_sha") != std::string::npos, "a content_sha mismatch is refused: " << error_of(sha));
  CHECK(error_of(sha, repo_root() + "/" + kRel).find("content_sha") != std::string::npos,
        "a --schema override is checked too");

  json hash = ok;
  hash["schema"]["hash"] = "other";
  CHECK(error_of(hash).find("hash differs") != std::string::npos, "a hash mismatch is refused: " << error_of(hash));

  json old = ok;
  old["schema"].erase("content_sha");                       // a manifest from before content_sha: hash only
  CHECK(error_of(old).empty(), "a manifest without content_sha loads on its hash: " << error_of(old));

  if (fails) { std::cerr << fails << " of " << checks << " checks failed\n"; return 1; }
  std::cout << "test_schema_path ok (" << checks << " checks)\n";
  return 0;
}

}  // namespace
