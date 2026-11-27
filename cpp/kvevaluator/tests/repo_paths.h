// The repo paths the tests read outside cpp/: the repo root found at run time (schema/schema_path.h) plus the
// repo-relative dirs CMakeLists.txt names once (KVEVAL_LDBC_SCHEMA_DIR, KVEVAL_SPECS_DIR).
#pragma once
#include <string>

#include "schema/schema_path.h"

inline std::string ldbc_schema_dir() { return evalcpp::repo_root() + "/" + KVEVAL_LDBC_SCHEMA_DIR; }
inline std::string specs_dir() { return evalcpp::repo_root() + "/" + KVEVAL_SPECS_DIR; }
