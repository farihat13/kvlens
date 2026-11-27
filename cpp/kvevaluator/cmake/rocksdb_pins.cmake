# The RocksDB pins and build flags: the one place they are written. Included by rocksdb.cmake; run alone
# (`cmake -DKVEVAL_ROCKSDB=<release> -P rocksdb_pins.cmake`) it prints ROCKSDB_ID, the name of a shared RocksDB build
# (run/build_cpp.py: one build per release and flags on a machine).
if(NOT DEFINED KVEVAL_ROCKSDB OR KVEVAL_ROCKSDB STREQUAL "")
  set(KVEVAL_ROCKSDB "7.5.3")
endif()
if(KVEVAL_ROCKSDB STREQUAL "7.5.3")
  set(ROCKSDB_SHA "540d5aae516265170564ec27b3e67a54a11b7045")   # tag v7.5.3
elseif(KVEVAL_ROCKSDB STREQUAL "11.8.1")
  set(ROCKSDB_SHA "abeebd9630f11bd08c28b7bd43c7bdfc62050654")   # tag v11.8.1
else()
  message(FATAL_ERROR "KVEVAL_ROCKSDB must be 7.5.3 or 11.8.1, not '${KVEVAL_ROCKSDB}'")
endif()
# gcc 13: RocksDB 7.5.3 needs DISABLE_WARNING_AS_ERROR and -include cstdint. EXTRA_CXXFLAGS goes in the ENVIRONMENT:
# on make's command line it would replace the Makefile's own -DROCKSDB_USE_RTTI.
set(ROCKSDB_BUILD_ENV DISABLE_WARNING_AS_ERROR=1 "EXTRA_CXXFLAGS=-include cstdint")
set(ROCKSDB_MAKE_ARGS DEBUG_LEVEL=0 USE_RTTI=1 SNAPPY=1 ZLIB=1 BZ2=1 LZ4=1 ZSTD=1)
string(SHA256 _flags "${ROCKSDB_SHA};${ROCKSDB_BUILD_ENV};${ROCKSDB_MAKE_ARGS}")
string(SUBSTRING "${_flags}" 0 12 _flags)
set(ROCKSDB_ID "${KVEVAL_ROCKSDB}-${_flags}")
if(CMAKE_SCRIPT_MODE_FILE)
  message(NOTICE "${ROCKSDB_ID}")
endif()
