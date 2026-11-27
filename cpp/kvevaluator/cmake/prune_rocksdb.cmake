# prune_rocksdb.cmake -- after the library is built and checked, drop what no later step reads: the object files
# (librocksdb.a holds them) and the clone's .git (verify_sha read it, before the build).
#   cmake -DDIR=<rocksdb dir> -P prune_rocksdb.cmake
file(GLOB_RECURSE _objects "${DIR}/*.o")
list(LENGTH _objects _n)
file(REMOVE ${_objects})
file(REMOVE_RECURSE "${DIR}/.git")
message(STATUS "RocksDB pruned: ${_n} object files and .git removed from ${DIR} (librocksdb.a and the sources kept)")
