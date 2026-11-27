// Build provenance stamped into every response and store manifest.
#pragma once
#include <nlohmann/json.hpp>

namespace evalcpp {

extern const char* kBuildGitSha;      // git rev-parse HEAD at build time
extern const char* kBuildKey;        // the build cache key (run/build_cpp.py); "" for a build in place
extern const char* kBuildGitDirty;   // "true" | "false" | "unknown" (no git tree): uncommitted changes under cpp/ at build time
extern const char* kRocksdbSource;   // the release and pinned commit it was built from
extern const char* kRocksdbASha256;   // sha256 of the linked librocksdb.a
extern const char* kCompiler;
extern const char* kCxxFlags;

// {binary_git_sha, binary_dirty, rocksdb, librocksdb_sha256, compiler, flags, allocator{...}, host{cpu, kernel}}
nlohmann::json provenance();
// The note a log line puts after the sha: " (dirty)" for a dirty build, " (release)" / " (unknown)" for the two no-git
// states (binary_dirty is then a string), "" for a clean one.
std::string dirty_note(const nlohmann::json& prov);

}  // namespace evalcpp
