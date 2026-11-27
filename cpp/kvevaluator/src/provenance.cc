#include "provenance.h"

#include "allocator.h"
#include "ingest/ingest.h"
#include "store/rocksdb_store.h"

#include <rocksdb/version.h>
#include <sys/utsname.h>

#include <fstream>
#include <string>

namespace evalcpp {

static std::string cpu_model() {
  std::ifstream f("/proc/cpuinfo");
  std::string line;
  while (std::getline(f, line)) {
    if (line.rfind("model name", 0) == 0) {
      auto p = line.find(':');
      if (p != std::string::npos) {
        std::string s = line.substr(p + 1);
        while (!s.empty() && s[0] == ' ') s.erase(0, 1);
        return s;
      }
    }
  }
  return "unknown";
}

std::string dirty_note(const nlohmann::json& prov) {
  if (!prov.contains("binary_dirty")) return " (dirty)";
  const auto& d = prov["binary_dirty"];
  if (d.is_string()) return " (" + d.get<std::string>() + ")";
  return d.is_boolean() && !d.get<bool>() ? "" : " (dirty)";
}

nlohmann::json provenance() {
  nlohmann::json j;
  j["binary_git_sha"] = kBuildGitSha;
  // a bool when git knows; the string "release" for a release archive (its commit from RELEASE_COMMIT), "unknown" when
  // the tree has no git and no release stamp (a reader that wants false refuses it)
  std::string dirty = kBuildGitDirty;
  if (dirty == "unknown" || dirty == "release") j["binary_dirty"] = dirty;
  else j["binary_dirty"] = (dirty == "true");
  j["rocksdb_source"] = kRocksdbSource;
  j["rocksdb"] = std::to_string(ROCKSDB_MAJOR) + "." + std::to_string(ROCKSDB_MINOR) + "." +
                 std::to_string(ROCKSDB_PATCH);
  j["librocksdb_sha256"] = kRocksdbASha256;
  j["compiler"] = kCompiler;
  j["flags"] = kCxxFlags;
  j["ingest_index_filter_blocks"] = rocksdb_setting<std::string>("index_filter_blocks");   // what a new store gets (readers: its manifest)
  j["block_cache_priority"] = {{"index_filter_high_priority", rocksdb_setting<bool>("index_filter_high_priority")},
                                {"high_pri_pool_ratio", rocksdb_setting<double>("high_pri_pool_ratio")}};
  j["allocator"] = allocator_info();   // which malloc measured this run (latency depends on it)
  // read once per process: a serve session builds a provenance per response (uname + /proc/cpuinfo per request otherwise)
  static const std::string cpu = cpu_model();
  static const std::string kernel = [] {
    struct utsname u;
    return uname(&u) == 0 ? std::string(u.sysname) + " " + u.release : std::string("unknown");
  }();
  j["host"] = {{"cpu", cpu}, {"kernel", kernel}};
  return j;
}

}  // namespace evalcpp
