// `kvevaluator schema-path record|resolve <path> [--root <experiment dir>]`: the manifest schema path rule
// (schema/schema_path.h) on one path, as JSON {"path": ... | null}, so tests/unit/check_manifest_schema_path.py
// can hold it equal to Python's. --root replaces this binary's experiment dir (a build tree that is gone).
#include <iostream>

#include "cli/commands.h"
#include "schema/schema_path.h"

namespace evalcpp {

int cmd_schema_path(const std::vector<std::string>& args) {
  std::string mode, path, root = repo_root();
  for (size_t i = 0; i < args.size(); i++) {
    if (args[i] == "--root" && i + 1 < args.size()) root = args[++i];
    else if (mode.empty()) mode = args[i];
    else if (path.empty()) path = args[i];
    else mode = "";   // one path only
  }
  if ((mode != "record" && mode != "resolve") || path.empty()) {
    std::cerr << "usage: kvevaluator schema-path record|resolve <path> [--root <experiment dir>]\n";
    return 2;
  }
  json out = {{"path", nullptr}, {"root", root}};
  if (mode == "record") {
    out["path"] = record_schema_path(path, root);
  } else {
    std::optional<std::string> r = resolve_schema_path(path, root);
    if (r) out["path"] = *r;
  }
  std::cout << out.dump() << "\n";
  return 0;
}

}  // namespace evalcpp
