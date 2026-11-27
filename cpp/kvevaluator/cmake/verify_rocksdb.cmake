# Checks the fetched RocksDB source before it is built: a git checkout must be at the pinned sha, and version.h must
# name the release (the only check a tarball gets beyond its sha256). Fails the build otherwise.
if(EXISTS "${DIR}/.git")
  execute_process(COMMAND git rev-parse HEAD WORKING_DIRECTORY "${DIR}" OUTPUT_VARIABLE HEAD
                  OUTPUT_STRIP_TRAILING_WHITESPACE RESULT_VARIABLE rc)
  if(NOT rc EQUAL 0 OR NOT HEAD STREQUAL WANT)
    message(FATAL_ERROR "RocksDB source at ${DIR} is at '${HEAD}', want ${WANT}")
  endif()
endif()
file(STRINGS "${DIR}/include/rocksdb/version.h" V REGEX "^#define ROCKSDB_(MAJOR|MINOR|PATCH) ")
string(REGEX REPLACE ".*MAJOR ([0-9]+).*MINOR ([0-9]+).*PATCH ([0-9]+).*" "\\1.\\2.\\3" GOT "${V}")
if(NOT GOT STREQUAL VERSION)
  message(FATAL_ERROR "RocksDB source at ${DIR} is version ${GOT}, want ${VERSION}")
endif()
message(STATUS "RocksDB source verified: ${VERSION} at ${WANT}")
