// The malloc linked into the binary (CMakeLists.txt EVALUATOR_ALLOCATOR: tcmalloc by default, glibc for before /
// after runs). Set up once at process start and reported in every response's provenance.
#pragma once
#include <nlohmann/json.hpp>

namespace evalcpp {

extern const char* kAllocator;            // "tcmalloc" | "glibc" (build time)
extern const char* kAllocatorVersion;     // the package version (dpkg) of the linked library
extern const char* kAllocatorLibSha256;   // sha256 of the linked static library ("" for glibc)

// tcmalloc: never return memory to the OS mid-run (release rate 0, aggressive decommit off: no re-fault cost that
// depends on time), and
// refuse to run if malloc / new do not resolve to tcmalloc (a link that silently kept glibc). glibc: nothing.
void allocator_init();

// {name, version, lib_sha256, release_rate, aggressive_decommit, malloc_is_tcmalloc, env{TCMALLOC_* / GLIBC_TUNABLES /
// MALLOC_* / LD_PRELOAD as set}} -- part of provenance()
nlohmann::json allocator_info();

}  // namespace evalcpp
