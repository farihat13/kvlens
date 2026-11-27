// A store manifest's schema path: one rule on both sides (the Python resolver is the reference; its check runs the
// cases through both). The manifest records the path relative to the repo root when the file lies under it, so a store
// copied across clones or machines still names its schema; a reader resolves it against THIS binary's repo root.
#pragma once
#include <optional>
#include <string>

#include "schema/schema.h"

namespace evalcpp {

// the repo root of this binary's clone, found at run time from the executable's location (a moved clone follows)
std::string repo_root();

// the form a manifest records: the real path, relative to the root when it lies under it, else absolute
std::string record_schema_path(const std::string& path, const std::string& root = repo_root());

// the recorded path: a relative one joins the root (or, when that file is gone, its new place in
// inputs/moved_paths.json); an absolute one is used as recorded; none when the file is not there. Never against the
// working dir.
std::optional<std::string> resolve_schema_path(const std::string& recorded, const std::string& root = repo_root());

// a store's schema from its manifest: resolved (a loud stop when it is in neither place), loaded, and checked against
// the manifest's hash and content_sha (a rebased or edited file that differs is refused). `given` (a --schema flag)
// replaces the manifest's path but not the checks. `who` prefixes the errors.
Schema load_manifest_schema(const json& manifest, const std::string& store_dir, const std::string& who,
                            const std::string& given = "");

}  // namespace evalcpp
