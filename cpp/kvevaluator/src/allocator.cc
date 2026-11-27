#include "allocator.h"

#include <gnu/libc-version.h>

#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <string>

extern char** environ;

#ifdef EVALCPP_ALLOCATOR_TCMALLOC
#include <gperftools/malloc_extension.h>
#endif

namespace evalcpp {

#ifdef EVALCPP_ALLOCATOR_TCMALLOC
// does tcmalloc own what malloc and new return? (2,416 B: the size of the iterator a SEEK creates)
static bool tcmalloc_owns_malloc() {
  void* p = malloc(2416);
  char* q = new char[2416];
  bool owned = MallocExtension::instance()->GetOwnership(p) == MallocExtension::kOwned &&
               MallocExtension::instance()->GetOwnership(q) == MallocExtension::kOwned;
  free(p);
  delete[] q;
  return owned;
}
#endif

void allocator_init() {
#ifdef EVALCPP_ALLOCATOR_TCMALLOC
  if (!tcmalloc_owns_malloc())
    throw std::runtime_error("built with EVALUATOR_ALLOCATOR=tcmalloc but malloc / new are not tcmalloc's "
                             "(the link kept another malloc): refusing to run");
  MallocExtension::instance()->SetMemoryReleaseRate(0.0);
  MallocExtension::instance()->SetNumericProperty("tcmalloc.aggressive_memory_decommit", 0);   // env can set it
#endif
}

// the environment variables that change an allocator's behavior, as set for this process (a run must say them)
static nlohmann::json allocator_env() {
  nlohmann::json j = nlohmann::json::object();
  for (char** e = environ; *e; e++) {
    std::string kv = *e;
    for (const char* p : {"TCMALLOC_", "GLIBC_TUNABLES=", "MALLOC_", "LD_PRELOAD="})
      if (kv.rfind(p, 0) == 0) {
        auto eq = kv.find('=');
        j[kv.substr(0, eq)] = kv.substr(eq + 1);
      }
  }
  return j;
}

nlohmann::json allocator_info() {
  nlohmann::json j;
  j["name"] = kAllocator;
#ifdef EVALCPP_ALLOCATOR_TCMALLOC
  j["version"] = kAllocatorVersion;
  j["lib_sha256"] = kAllocatorLibSha256;
  j["release_rate"] = MallocExtension::instance()->GetMemoryReleaseRate();
  j["malloc_is_tcmalloc"] = tcmalloc_owns_malloc();
  size_t decommit = 0;
  MallocExtension::instance()->GetNumericProperty("tcmalloc.aggressive_memory_decommit", &decommit);
  j["aggressive_decommit"] = decommit;
#else
  j["version"] = gnu_get_libc_version();
#endif
  j["env"] = allocator_env();
  return j;
}

}  // namespace evalcpp
