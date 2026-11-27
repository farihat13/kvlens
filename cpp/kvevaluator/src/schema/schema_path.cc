#include "schema/schema_path.h"

#include <filesystem>
#include <fstream>
#include <stdexcept>

namespace fs = std::filesystem;

namespace evalcpp {

// the repo root: the executable's own dir plus the relative path CMake wrote (CMakeLists.txt, KVEVAL_ROOT_FROM_BIN),
// found at run time, so a renamed or moved clone still resolves
std::string repo_root() {
  fs::path exe = fs::canonical("/proc/self/exe");
  return fs::weakly_canonical(exe.parent_path() / KVEVAL_ROOT_FROM_BIN).string();
}

static bool is_file(const std::string& p) {
  std::error_code ec;
  return fs::is_regular_file(p, ec);
}

std::string record_schema_path(const std::string& path, const std::string& root) {
  fs::path real = fs::weakly_canonical(fs::absolute(path));
  fs::path rel = real.lexically_relative(fs::weakly_canonical(root));
  bool under = !rel.empty() && *rel.begin() != "..";
  return under ? rel.string() : real.string();
}

// a schema file moved in the tree since a store recorded it: inputs/moved_paths.json ("moved": {old: new}, relative to
// the root); the caller still checks the schema's hash, so the table only says where to look
static std::optional<std::string> moved_path(const std::string& recorded, const std::string& root) {
  std::ifstream f(root + "/" + KVEVAL_MOVED_PATHS);   // CMakeLists.txt's repo paths block
  if (!f) return std::nullopt;
  json table = json::parse(f);
  const json& moved = table.at("moved");
  auto it = moved.find(recorded);
  if (it == moved.end()) return std::nullopt;
  return root + "/" + it->get<std::string>();
}

std::optional<std::string> resolve_schema_path(const std::string& recorded, const std::string& root) {
  if (recorded.empty()) return std::nullopt;
  std::string path = recorded[0] == '/' ? recorded : root + "/" + recorded;
  if (is_file(path)) return path;
  if (recorded[0] != '/') {
    std::optional<std::string> m = moved_path(recorded, root);
    if (m && is_file(*m)) return m;
  }
  return std::nullopt;
}

Schema load_manifest_schema(const json& manifest, const std::string& store_dir, const std::string& who,
                            const std::string& given) {
  const json& ms = manifest.at("schema");
  std::string file = given;
  if (file.empty()) {
    std::string recorded = ms.value("path", "");
    std::optional<std::string> r = resolve_schema_path(recorded);
    if (!r)
      throw std::runtime_error(who + ": " + store_dir + "'s manifest names the schema '" + recorded +
                               "', in neither the recorded place nor this tree (" + repo_root() + ")");
    file = *r;
  }
  Schema schema = Schema::load(file);
  if (schema.hash() != ms.value("hash", ""))
    throw std::runtime_error(who + ": the schema's hash differs from the one " + store_dir + " was built with (" + file + ")");
  // content_sha covers the whole file (the hash leaves out the adj / prop_groups blocks): the session refuses a
  // mismatch at store open, and a reader that decodes values must too
  std::string want = ms.value("content_sha", "");
  if (!want.empty() && want != schema.content_sha())
    throw std::runtime_error(who + ": " + file + " has content_sha " + schema.content_sha().substr(0, 12) + ", " +
                             store_dir + " was built with " + want.substr(0, 12));
  return schema;
}

}  // namespace evalcpp
