# Checks that a librocksdb.a was built with all five compression libraries, from the symbols it leaves undefined for
# them (RocksDB's own build drops a library it does not find, without a word). -DLIB=<librocksdb.a>.
# Fails naming the missing ones; prints "rocksdb codecs: all five" otherwise.
execute_process(COMMAND nm -u "${LIB}"
                COMMAND grep -oE "6snappy11RawCompress|deflateInit2_|BZ2_bzCompressInit|LZ4_compress_fast_continue|ZSTD_compress"
                OUTPUT_VARIABLE SYMS ERROR_QUIET RESULTS_VARIABLE RCS)
list(GET RCS 0 NM_RC)
if(NOT NM_RC EQUAL 0)
  message(FATAL_ERROR "nm -u ${LIB} failed (rc ${NM_RC}): is binutils installed?")
endif()
set(MISSING "")
foreach(PAIR "snappy:6snappy11RawCompress" "zlib:deflateInit2_" "bzip2:BZ2_bzCompressInit" "lz4:LZ4_compress_fast_continue"
             "zstd:ZSTD_compress")
  string(REPLACE ":" ";" PAIR "${PAIR}")
  list(GET PAIR 0 NAME)
  list(GET PAIR 1 SYM)
  string(FIND "${SYMS}" "${SYM}" POS)
  if(POS EQUAL -1)
    list(APPEND MISSING ${NAME})
  endif()
endforeach()
if(MISSING)
  list(JOIN MISSING ", " MISSING)
  message(FATAL_ERROR "${LIB} was built without: ${MISSING}")
endif()
message(STATUS "rocksdb codecs: all five (snappy, zlib, bzip2, lz4, zstd) in ${LIB}")
