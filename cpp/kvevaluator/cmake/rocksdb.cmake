# Included by CMakeLists.txt. --- RocksDB, fetched at an exact commit and built here --------------------------------
# KVEVAL_ROCKSDB picks the release; each one is pinned by its commit sha, and configure refuses any other source.
# The library is built with RocksDB's own Makefile and the flags the measurements used (no RocksDB CMake build).
# KVEVAL_ROCKSDB_SOURCE (optional) builds offline: a local git clone (checked out at the pinned sha) or a tarball
# whose sha256 is KVEVAL_ROCKSDB_TARBALL_SHA256.
include(ExternalProject)
set(KVEVAL_ROCKSDB "7.5.3" CACHE STRING "RocksDB release: 7.5.3 (the paper's measurements) | 11.8.1")
set_property(CACHE KVEVAL_ROCKSDB PROPERTY STRINGS 7.5.3 11.8.1)
include(${CMAKE_CURRENT_LIST_DIR}/rocksdb_pins.cmake)   # ROCKSDB_SHA, the make flags, ROCKSDB_ID
if(KVEVAL_ROCKSDB STREQUAL "11.8.1")
  set(CMAKE_CXX_STANDARD 20)                                     # RocksDB 11 headers require C++20
else()
  set(CMAKE_CXX_STANDARD 17)
endif()
set(KVEVAL_ROCKSDB_SOURCE "" CACHE STRING "offline RocksDB source: a local git clone or a .tar.gz (empty: GitHub)")
set(KVEVAL_ROCKSDB_TARBALL_SHA256 "" CACHE STRING "the tarball's sha256 (required with a tarball source)")
cmake_host_system_information(RESULT _ncpu QUERY NUMBER_OF_LOGICAL_CORES)
set(KVEVAL_ROCKSDB_JOBS "${_ncpu}" CACHE STRING "make -j for the RocksDB build")
# Where RocksDB is built: in this build dir (a build in place), or a shared dir that run/build_cpp.py names, one per
# ROCKSDB_ID on the machine. A shared dir that holds its done-marker is linked as it is: no fetch, no build here.
set(KVEVAL_ROCKSDB_DIR "" CACHE PATH "a shared RocksDB build (run/build_cpp.py); empty: build it in this build dir")
if(KVEVAL_ROCKSDB_DIR STREQUAL "")
  set(ROCKSDB_DIR "${CMAKE_BINARY_DIR}/rocksdb-${KVEVAL_ROCKSDB}")
else()
  set(ROCKSDB_DIR "${KVEVAL_ROCKSDB_DIR}")
endif()
set(ROCKSDB_DONE "${ROCKSDB_DIR}/kvevaluator-rocksdb.done")   # holds ROCKSDB_ID; written by run/build_cpp.py
set(ROCKSDB_PREBUILT OFF)
if(NOT KVEVAL_ROCKSDB_DIR STREQUAL "" AND EXISTS "${ROCKSDB_DONE}")
  file(READ "${ROCKSDB_DONE}" _done)
  string(STRIP "${_done}" _done)
  if(NOT _done STREQUAL ROCKSDB_ID)
    message(FATAL_ERROR "${ROCKSDB_DIR} holds RocksDB ${_done}, not ${ROCKSDB_ID}")
  endif()
  set(ROCKSDB_PREBUILT ON)
endif()
set(ROCKSDB_A "${ROCKSDB_DIR}/librocksdb.a")
set(ROCKSDB_SOURCE_LABEL "${KVEVAL_ROCKSDB}@${ROCKSDB_SHA}")
if(KVEVAL_ROCKSDB_SOURCE MATCHES "\\.(tar\\.gz|tgz)$")
  set(ROCKSDB_SOURCE_LABEL "${ROCKSDB_SOURCE_LABEL} (tarball sha256 ${KVEVAL_ROCKSDB_TARBALL_SHA256})")
endif()
if(NOT ROCKSDB_PREBUILT)   # fetch and build it here
if(KVEVAL_ROCKSDB_SOURCE MATCHES "\\.(tar\\.gz|tgz)$")
  if(KVEVAL_ROCKSDB_TARBALL_SHA256 STREQUAL "")
    message(FATAL_ERROR "a tarball KVEVAL_ROCKSDB_SOURCE needs KVEVAL_ROCKSDB_TARBALL_SHA256")
  endif()
  set(_fetch URL "${KVEVAL_ROCKSDB_SOURCE}" URL_HASH SHA256=${KVEVAL_ROCKSDB_TARBALL_SHA256})
else()
  if(KVEVAL_ROCKSDB_SOURCE STREQUAL "")
    set(_repo "https://github.com/facebook/rocksdb.git")
  else()
    get_filename_component(_repo "${KVEVAL_ROCKSDB_SOURCE}" ABSOLUTE)
  endif()
  set(_fetch GIT_REPOSITORY "${_repo}" GIT_TAG ${ROCKSDB_SHA} GIT_PROGRESS OFF UPDATE_DISCONNECTED ON)
endif()
ExternalProject_Add(rocksdb_build
  ${_fetch}
  SOURCE_DIR "${ROCKSDB_DIR}"
  CONFIGURE_COMMAND ""
  BUILD_IN_SOURCE 1
  BUILD_COMMAND ${CMAKE_COMMAND} -E env ${ROCKSDB_BUILD_ENV} make static_lib -j${KVEVAL_ROCKSDB_JOBS} ${ROCKSDB_MAKE_ARGS}
  INSTALL_COMMAND ""
  BUILD_BYPRODUCTS "${ROCKSDB_DIR}/librocksdb.a"
  LOG_DOWNLOAD ON LOG_BUILD ON LOG_OUTPUT_ON_FAILURE ON)
# a git source must sit at the pinned sha (checked after the fetch, before the build: a wrong tree fails here)
ExternalProject_Add_Step(rocksdb_build verify_sha
  COMMAND ${CMAKE_COMMAND} -DDIR=${ROCKSDB_DIR} -DWANT=${ROCKSDB_SHA} -DVERSION=${KVEVAL_ROCKSDB}
          -P ${CMAKE_CURRENT_LIST_DIR}/verify_rocksdb.cmake
  DEPENDEES download DEPENDERS build)
# the library must hold all five compression libraries: RocksDB's Makefile drops one it does not find, without a word
ExternalProject_Add_Step(rocksdb_build verify_codecs
  COMMAND ${CMAKE_COMMAND} -DLIB=${ROCKSDB_A} -P ${CMAKE_CURRENT_LIST_DIR}/check_rocksdb_codecs.cmake
  DEPENDEES build)
# Once the library is checked, its object files (871 MB) and the clone's .git (269 MB) are not read again: the
# binaries link librocksdb.a. The sources stay, so the rebuild below still works (it recompiles them all).
ExternalProject_Add_Step(rocksdb_build prune
  COMMAND ${CMAKE_COMMAND} -DDIR=${ROCKSDB_DIR} -P ${CMAKE_CURRENT_LIST_DIR}/prune_rocksdb.cmake
  DEPENDEES verify_codecs)
# A library built before a compression library was installed lacks it, and ExternalProject would keep it (its build
# stamp says done). Such a library is cleaned here, at configure, so this build makes it again.
ExternalProject_Get_Property(rocksdb_build STAMP_DIR)
if(EXISTS "${ROCKSDB_A}")
  execute_process(COMMAND ${CMAKE_COMMAND} -DLIB=${ROCKSDB_A} -P ${CMAKE_CURRENT_LIST_DIR}/check_rocksdb_codecs.cmake
                  RESULT_VARIABLE _codecs_rc OUTPUT_VARIABLE _codecs_out ERROR_VARIABLE _codecs_out)
  if(NOT _codecs_rc EQUAL 0)
    string(STRIP "${_codecs_out}" _codecs_out)
    message(WARNING "${_codecs_out}\nRebuilding RocksDB: `make clean` in ${ROCKSDB_DIR}, and its build stamp removed.")
    execute_process(COMMAND make clean WORKING_DIRECTORY "${ROCKSDB_DIR}" OUTPUT_QUIET ERROR_QUIET)
    file(REMOVE "${STAMP_DIR}/rocksdb_build-build" "${STAMP_DIR}/rocksdb_build-verify_codecs"
         "${STAMP_DIR}/rocksdb_build-done")
  endif()
endif()
else()   # a shared build that run/build_cpp.py made and marked done: link it; a library missing a codec is refused
  execute_process(COMMAND ${CMAKE_COMMAND} -DLIB=${ROCKSDB_A} -P ${CMAKE_CURRENT_LIST_DIR}/check_rocksdb_codecs.cmake
                  RESULT_VARIABLE _codecs_rc OUTPUT_VARIABLE _codecs_out ERROR_VARIABLE _codecs_out)
  if(NOT _codecs_rc EQUAL 0)
    message(FATAL_ERROR "the shared RocksDB in ${ROCKSDB_DIR} fails the codec check: ${_codecs_out}")
  endif()
  add_custom_target(rocksdb_build)   # the targets below depend on it; nothing to do
  message(STATUS "RocksDB ${ROCKSDB_ID}: the shared build in ${ROCKSDB_DIR}")
endif()
