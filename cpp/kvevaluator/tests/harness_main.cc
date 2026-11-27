// The unit tests' main (harness.h): `<binary> --list`, `<binary> --check-list <names...>` (the ctest guard: the
// registered cases equal the names CMake configured), `<binary> <case> [--keep-tmp] [args...]` (one case).
#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <map>
#include <set>
#include <string>
#include <system_error>
#include <vector>
#include <stdlib.h>

#include "harness.h"

namespace kvt {

static std::map<std::string, CaseFn>& cases() {
  static std::map<std::string, CaseFn> m;   // a function-local static: registrars run before main in any order
  return m;
}

void register_case(const char* name, CaseFn fn) {
  if (!cases().emplace(name, fn).second) {
    std::fprintf(stderr, "harness: the case %s is registered twice\n", name);
    std::exit(2);
  }
}

static std::vector<std::string>& temp_dirs() {
  static std::vector<std::string> v;
  return v;
}

char* temp_dir(char* tmpl) {
  char* p = mkdtemp(tmpl);
  if (p) temp_dirs().push_back(p);
  return p;
}

}  // namespace kvt

int main(int argc, char** argv) {
  using namespace kvt;
  if (argc < 2) {
    std::fprintf(stderr, "usage: %s --list | --check-list <names...> | <case> [--keep-tmp] [args...]\n", argv[0]);
    return 2;
  }
  std::string first = argv[1];
  if (first == "--list") {
    for (const auto& [name, fn] : cases()) std::printf("%s\n", name.c_str());
    return 0;
  }
  if (first == "--check-list") {   // CMake's list of this binary's cases (from `^TEST_CASE(<name>)` lines)
    std::set<std::string> want(argv + 2, argv + argc), have;
    for (const auto& [name, fn] : cases()) have.insert(name);
    int bad = 0;
    for (const auto& n : have)
      if (!want.count(n)) { std::printf("REGISTERED BUT NOT IN CTEST: %s (re-run cmake; its TEST_CASE line must start the line)\n", n.c_str()); bad++; }
    for (const auto& n : want)
      if (!have.count(n)) { std::printf("IN CTEST BUT NOT REGISTERED: %s\n", n.c_str()); bad++; }
    std::printf("[cases] %zu registered, %zu configured, %d differ\n", have.size(), want.size(), bad);
    return bad ? 1 : 0;
  }
  auto it = cases().find(first);
  if (it == cases().end()) {
    std::fprintf(stderr, "no case %s (--list lists them)\n", first.c_str());
    return 2;
  }
  // the case's argv: the binary, then its own arguments (--keep-tmp is the harness's)
  std::vector<char*> args = {argv[0]};
  for (int i = 2; i < argc; i++) {
    if (std::strcmp(argv[i], "--keep-tmp") == 0) keep_tmp = true;
    else args.push_back(argv[i]);
  }
  args.push_back(nullptr);
  int rc = it->second(int(args.size()) - 1, args.data());
  for (const auto& d : temp_dirs()) {
    if (keep_tmp) { std::printf("[%s] kept %s\n", first.c_str(), d.c_str()); continue; }
    std::error_code ec;
    std::filesystem::remove_all(d, ec);   // the case's own temp dir; a failure to remove is not the case's failure
  }
  if (rc == 77) return 77;   // skipped (kvt::skip)
  std::printf("[%s] checks=%d fails=%d\n", first.c_str(), checks, fails);
  if (rc != 0) return rc;
  if (fails) return 1;
  if (checks == 0) {
    std::printf("[%s] FAIL: no check ran\n", first.c_str());
    return 1;
  }
  return 0;
}
