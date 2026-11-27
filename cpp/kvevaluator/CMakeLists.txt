# kvevaluator: the C++ measurement engine of the KV-format evaluator (built as the binary `kvevaluator`).
# Fetches and builds its RocksDB at a pinned commit (KVEVAL_ROCKSDB, cmake/rocksdb.cmake); reads repo paths only
# from the block below.
cmake_minimum_required(VERSION 3.20)
project(kvevaluator CXX)

set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE RelWithDebInfo CACHE STRING "" FORCE)
endif()
set(CMAKE_CXX_FLAGS_RELWITHDEBINFO "-O2 -g")
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -Wall -Wextra -Wno-unused-parameter -Wno-dangling-reference")
# RocksDB's `make static_lib` builds non-PIC objects; Ubuntu's gcc links PIE by default -> -no-pie.
set(CMAKE_POSITION_INDEPENDENT_CODE OFF)
add_link_options(-no-pie)

# --- repo paths: the ONE place they are named (repo-relative; a rename is one line here) --------------------
set(KVEVAL_ROOT_REL "../..")                                          # the repo root, from this dir (cpp/kvevaluator)
set(KVEVAL_LDBC_SCHEMA_DIR "inputs/schemas/ldbc")                  # the LDBC schema JSONs (tests, ingest examples)
set(KVEVAL_SPECS_DIR "inputs/specs")                                  # the layout specs (test_meta's corners/)
set(KVEVAL_MOVED_PATHS "inputs/moved_paths.json")                     # read at run time (schema/schema_path.cc)
set(KVEVAL_CONTRACT_DIR "inputs/contract")                            # compiled in at build time (cmake/embed_contract.cmake)
# run/build_cpp.py archives cpp/kvevaluator and every path named in this block, and keys its cache on them
get_filename_component(KVEVAL_REPO_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/${KVEVAL_ROOT_REL}" ABSOLUTE)

# --- the system libraries, checked at configure, before RocksDB builds (minutes) --------------------------------
# RocksDB is built with five compression libraries, and the binary links them and tcmalloc. A missing one fails here,
# with the one line that installs them all; a RocksDB built without one is caught in cmake/rocksdb.cmake.
set(KVEVAL_APT_LINE "sudo apt-get install -y libgoogle-perftools-dev libsnappy-dev zlib1g-dev libbz2-dev liblz4-dev libzstd-dev")
set(_missing "")
foreach(_pair "snappy:snappy.h:libsnappy-dev" "z:zlib.h:zlib1g-dev" "bz2:bzlib.h:libbz2-dev" "lz4:lz4.h:liblz4-dev"
              "zstd:zstd.h:libzstd-dev")
  string(REPLACE ":" ";" _pair "${_pair}")
  list(GET _pair 0 _lib)
  list(GET _pair 1 _hdr)
  list(GET _pair 2 _pkg)
  find_library(KVEVAL_LIB_${_lib} ${_lib})
  find_path(KVEVAL_INC_${_lib} ${_hdr})
  if(NOT KVEVAL_LIB_${_lib} OR NOT KVEVAL_INC_${_lib})
    list(APPEND _missing "${_pkg}")
  endif()
endforeach()
if(_missing)
  list(JOIN _missing " " _missing)
  message(FATAL_ERROR "missing system packages: ${_missing}. Install every package the build needs with:\n  "
          "${KVEVAL_APT_LINE}\n(then run the build again; nothing has been built yet)")
endif()

include(${CMAKE_CURRENT_SOURCE_DIR}/cmake/rocksdb.cmake)   # KVEVAL_ROCKSDB: fetch + build at a pinned commit

# --- the allocator: tcmalloc by default (glibc's malloc slows in a used heap, so a step's warm latency grew with
# what the process ran before). Linked STATICALLY by absolute path: never a
# -l search, which can find a /usr/local/lib/libjemalloc built --with-jemalloc-prefix=je_ (it exports je_malloc,
# not malloc) first and silently keep glibc. -DEVALUATOR_ALLOCATOR=glibc builds with glibc's malloc (for before / after runs only).
set(EVALUATOR_ALLOCATOR "tcmalloc" CACHE STRING "malloc implementation linked into the binary: tcmalloc | glibc")
set_property(CACHE EVALUATOR_ALLOCATOR PROPERTY STRINGS tcmalloc glibc)
if(EVALUATOR_ALLOCATOR STREQUAL "tcmalloc")
  set(TCMALLOC_A "/usr/lib/${CMAKE_LIBRARY_ARCHITECTURE}/libtcmalloc_minimal.a" CACHE FILEPATH
      "gperftools' static tcmalloc (Ubuntu: libgoogle-perftools-dev)")
  if(NOT EXISTS "${TCMALLOC_A}")
    message(FATAL_ERROR "EVALUATOR_ALLOCATOR=tcmalloc but ${TCMALLOC_A} is missing: install libgoogle-perftools-dev "
            "(Ubuntu; every package the build needs: ${KVEVAL_APT_LINE}), or set -DTCMALLOC_A=<path>. "
            "Nothing has been built yet. Do not point it at a jemalloc or any "
            "library found by -l search: a je_-prefixed libjemalloc would keep glibc's malloc. "
            "-DEVALUATOR_ALLOCATOR=glibc builds the old, history-dependent allocator on purpose (before / after runs).")
  endif()
  # the package that owns the .a (its sha256 is taken at BUILD time, gen_build_info.cmake, so an upgrade shows)
  execute_process(COMMAND dpkg-query -W -f=\${Version} libgoogle-perftools-dev
                  OUTPUT_VARIABLE ALLOCATOR_VERSION OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
  if(ALLOCATOR_VERSION STREQUAL "")
    set(ALLOCATOR_VERSION "unknown")   # not a dpkg install: lib_sha256 identifies it
  endif()
  set(ALLOCATOR_LIB "${TCMALLOC_A}")
  set(ALLOCATOR_LIBS "${TCMALLOC_A}")
  set(ALLOCATOR_DEFS EVALCPP_ALLOCATOR_TCMALLOC=1)
elseif(EVALUATOR_ALLOCATOR STREQUAL "glibc")
  set(ALLOCATOR_LIB "")
  set(ALLOCATOR_VERSION "")      # glibc's own version is read at run time (gnu_get_libc_version)
  set(ALLOCATOR_LIBS "")
  set(ALLOCATOR_DEFS "")
else()
  message(FATAL_ERROR "EVALUATOR_ALLOCATOR must be tcmalloc or glibc, not '${EVALUATOR_ALLOCATOR}'")
endif()
message(STATUS "kvevaluator allocator: ${EVALUATOR_ALLOCATOR} ${ALLOCATOR_VERSION}")

# --- build provenance, regenerated on every build (git SHA + dirty flag) -----------
# A cache build (run/build_cpp.py) builds a git archive, which has no .git: it names the commit, the dirty flag and its
# cache key here. Empty (a build in place): gen_build_info.cmake asks git.
set(KVEVAL_BUILD_COMMIT "" CACHE STRING "the commit a cache build archived (run/build_cpp.py); empty: ask git")
set(KVEVAL_BUILD_DIRTY "" CACHE STRING "true | false, with KVEVAL_BUILD_COMMIT")
set(KVEVAL_BUILD_KEY "" CACHE STRING "the build cache key, with KVEVAL_BUILD_COMMIT")
set(BUILD_INFO_CC "${CMAKE_BINARY_DIR}/build_info.cc")
add_custom_target(build_info ALL
  COMMAND ${CMAKE_COMMAND}
    -DOUT=${BUILD_INFO_CC}
    -DSRC_DIR=${CMAKE_CURRENT_SOURCE_DIR}
    -DROCKSDB_A=${ROCKSDB_A}
    "-DROCKSDB_SOURCE=${ROCKSDB_SOURCE_LABEL}"
    -DCOMPILER_ID=${CMAKE_CXX_COMPILER_ID}
    -DCOMPILER_VERSION=${CMAKE_CXX_COMPILER_VERSION}
    "-DCXX_FLAGS=${CMAKE_CXX_FLAGS} ${CMAKE_CXX_FLAGS_RELWITHDEBINFO}"
    -DALLOCATOR=${EVALUATOR_ALLOCATOR}
    "-DALLOCATOR_VERSION=${ALLOCATOR_VERSION}"
    "-DALLOCATOR_LIB=${ALLOCATOR_LIB}"
    "-DBUILD_COMMIT=${KVEVAL_BUILD_COMMIT}"
    "-DBUILD_DIRTY=${KVEVAL_BUILD_DIRTY}"
    "-DBUILD_KEY=${KVEVAL_BUILD_KEY}"
    # a release archive (git archive, a GitHub or Zenodo zip) has no .git: RELEASE_COMMIT names its commit (export-subst)
    "-DRELEASE_FILE=${KVEVAL_REPO_ROOT}/RELEASE_COMMIT"
    # the dirty flag of a build in place covers every path the build reads from the repo, not only this dir
    "-DSTATUS_PATHS=${KVEVAL_REPO_ROOT}/${KVEVAL_LDBC_SCHEMA_DIR}|${KVEVAL_REPO_ROOT}/${KVEVAL_SPECS_DIR}|${KVEVAL_REPO_ROOT}/${KVEVAL_MOVED_PATHS}|${KVEVAL_REPO_ROOT}/${KVEVAL_CONTRACT_DIR}"
    -P ${CMAKE_CURRENT_SOURCE_DIR}/cmake/gen_build_info.cmake
  BYPRODUCTS ${BUILD_INFO_CC}
  COMMENT "stamping build provenance"
  VERBATIM)
add_dependencies(build_info rocksdb_build)   # it hashes the built library

file(GLOB_RECURSE EVAL_SOURCES CONFIGURE_DEPENDS src/*.cc)
# Every source but main.cc is compiled ONCE, into an object library the server and every test link: each binary used
# to compile all of them again (29 compiles of every file; ~6-8 min after a header change). build_info.cc stays per
# binary, so each one names its own commit and dirty flag.
set(LIB_SOURCES ${EVAL_SOURCES})
list(FILTER LIB_SOURCES EXCLUDE REGEX ".*/main\\.cc$")
# the shared data files (inputs/contract), compiled in: a binary carries its own commit's KV-op table, capability
# vocabulary and capture columns (src/contract/contract.h); regenerated when a file changes
set(KVEVAL_CONTRACT_NAMES kv_ops capabilities capture_columns store_config)
set(CONTRACT_DATA_CC "${CMAKE_BINARY_DIR}/contract_data.cc")
set(_contract_files "")
foreach(_n ${KVEVAL_CONTRACT_NAMES})
  list(APPEND _contract_files "${KVEVAL_REPO_ROOT}/${KVEVAL_CONTRACT_DIR}/${_n}.json")
endforeach()
string(REPLACE ";" "|" _contract_names "${KVEVAL_CONTRACT_NAMES}")
add_custom_command(OUTPUT ${CONTRACT_DATA_CC}
  COMMAND ${CMAKE_COMMAND} -DOUT=${CONTRACT_DATA_CC} -DCONTRACT_DIR=${KVEVAL_REPO_ROOT}/${KVEVAL_CONTRACT_DIR}
          "-DNAMES=${_contract_names}" -P ${CMAKE_CURRENT_SOURCE_DIR}/cmake/embed_contract.cmake
  DEPENDS ${_contract_files} ${CMAKE_CURRENT_SOURCE_DIR}/cmake/embed_contract.cmake
  COMMENT "compiling in inputs/contract"
  VERBATIM)
add_library(evalcpp_objects OBJECT ${LIB_SOURCES} ${CONTRACT_DATA_CC})
# the root a manifest's relative schema path is resolved against (schema/schema_path.cc), found at RUN time from the
# executable's own location plus this RELATIVE path (no absolute path is baked into the binary)
file(RELATIVE_PATH KVEVAL_ROOT_FROM_BIN "${CMAKE_BINARY_DIR}" "${KVEVAL_REPO_ROOT}")
if(KVEVAL_ROOT_FROM_BIN STREQUAL "")
  set(KVEVAL_ROOT_FROM_BIN ".")
endif()
set_source_files_properties(${CMAKE_CURRENT_SOURCE_DIR}/src/schema/schema_path.cc PROPERTIES
  COMPILE_DEFINITIONS "KVEVAL_ROOT_FROM_BIN=\"${KVEVAL_ROOT_FROM_BIN}\";KVEVAL_MOVED_PATHS=\"${KVEVAL_MOVED_PATHS}\"")
target_include_directories(evalcpp_objects PUBLIC
  ${CMAKE_CURRENT_SOURCE_DIR}/src ${CMAKE_CURRENT_SOURCE_DIR}/third_party ${ROCKSDB_DIR}/include)
add_dependencies(evalcpp_objects rocksdb_build)
target_compile_definitions(evalcpp_objects PUBLIC ${ALLOCATOR_DEFS})

add_executable(kvevaluator ${CMAKE_CURRENT_SOURCE_DIR}/src/main.cc ${BUILD_INFO_CC})
add_dependencies(kvevaluator build_info)
# The .a's undefined symbols: the five compression libs + pthread/dl (no gflags, no numa).
target_link_libraries(kvevaluator PRIVATE evalcpp_objects ${ROCKSDB_A} snappy z bz2 lz4 zstd ${ALLOCATOR_LIBS} pthread dl)

# --- unit tests (no RocksDB needed for most) ---------------------------------------
enable_testing()
file(GLOB TEST_SOURCES CONFIGURE_DEPENDS tests/*.cc)
list(REMOVE_ITEM TEST_SOURCES ${CMAKE_CURRENT_SOURCE_DIR}/tests/harness_main.cc)
# Two binaries on one harness (tests/harness.h): kvevaluator_tests holds every case but test_value, which replaces the
# global operator new / delete to count a row's allocations and so must not share a binary with the other cases.
# A test binary links every object and RocksDB: ~15 MB without debug info, ~360 MB with it.
# The tests are linked without debug info unless this is ON (for someone debugging a test); the kvevaluator binary
# is linked as before either way
option(KVEVAL_TEST_DEBUG_INFO "link the unit tests with debug info" OFF)
# The tests are not in the default build: `ctest` builds them first (the build_unit_tests fixture below), or
# `cmake --build <dir> --target unit_tests`. The default build makes kvevaluator only.
add_custom_target(unit_tests)
cmake_host_system_information(RESULT _test_jobs QUERY NUMBER_OF_LOGICAL_CORES)
add_test(NAME build_unit_tests COMMAND ${CMAKE_COMMAND} --build ${CMAKE_BINARY_DIR} --target unit_tests -j ${_test_jobs})
set_tests_properties(build_unit_tests PROPERTIES FIXTURES_SETUP unit_tests_built)
set(_own_binary ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_value.cc)
set(_shared_sources ${TEST_SOURCES})
list(REMOVE_ITEM _shared_sources ${_own_binary})
foreach(bin kvevaluator_tests test_value)
  if(bin STREQUAL "test_value")
    set(_srcs ${_own_binary})
  else()
    set(_srcs ${_shared_sources})
  endif()
  add_executable(${bin} EXCLUDE_FROM_ALL ${CMAKE_CURRENT_SOURCE_DIR}/tests/harness_main.cc ${_srcs} ${BUILD_INFO_CC})
  add_dependencies(unit_tests ${bin})
  add_dependencies(${bin} build_info)
  target_link_libraries(${bin} PRIVATE evalcpp_objects ${ROCKSDB_A} snappy z bz2 lz4 zstd ${ALLOCATOR_LIBS} pthread dl)
  if(NOT KVEVAL_TEST_DEBUG_INFO)
    target_link_options(${bin} PRIVATE -Wl,--strip-debug)
  endif()
  target_compile_definitions(${bin} PRIVATE FIXTURE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/tests/fixtures"
    KVEVAL_LDBC_SCHEMA_DIR="${KVEVAL_LDBC_SCHEMA_DIR}" KVEVAL_SPECS_DIR="${KVEVAL_SPECS_DIR}")
  # one ctest test per case, named by its `TEST_CASE(<name>)` line (read here, at configure: the fixture above builds
  # the binary, so its own --list would come too late); rc 77 = skipped (kvt::skip)
  set(_names "")
  foreach(src ${_srcs})
    file(STRINGS ${src} _lines REGEX "^TEST_CASE\\([A-Za-z0-9_]+\\)")
    foreach(line ${_lines})
      string(REGEX REPLACE "^TEST_CASE\\(([A-Za-z0-9_]+)\\).*" "\\1" _case "${line}")
      list(APPEND _names ${_case})
      add_test(NAME ${_case} COMMAND ${bin} ${_case})
      set_tests_properties(${_case} PROPERTIES FIXTURES_REQUIRED unit_tests_built SKIP_RETURN_CODE 77)
    endforeach()
  endforeach()
  # the guard: the cases the binary registers equal the ones listed here (a case CMake did not read never runs)
  add_test(NAME harness_cases_listed_${bin} COMMAND ${bin} --check-list ${_names})
  set_tests_properties(harness_cases_listed_${bin} PROPERTIES FIXTURES_REQUIRED unit_tests_built)
endforeach()
# the opaque-vid rule (meta-v4): no query or write in formats/meta takes a decision from a vid's bits
add_test(NAME check_vid_opaque COMMAND python3 ${CMAKE_CURRENT_SOURCE_DIR}/tests/check_vid_opaque.py ${CMAKE_CURRENT_SOURCE_DIR}/src)
