// `checkpoint <store-dir> <arena-dir>`: a disposable hard-link copy (RocksDB Checkpoint) of a
// settled store, marked as a write ARENA. The apply-writes run mode (write latency) refuses any
// dir without the arena.json marker, so real writes can only ever land on one of these copies --
// never on a measured pristine store. Restore-per-run = delete the arena, checkpoint again (the
// driver owns the lifecycle; SSTs are hard-linked, so creation is O(files), seconds).
// The source is opened READ-WRITE (rocksdb Checkpoint cannot run on a read-only DB), so a
// checkpoint bumps the source's MANIFEST/WAL bookkeeping exactly as every session open
// already does -- logical content untouched (dump-sha validated) -- and it fails on the
// RocksDB LOCK if anything else holds the store open.
#include <ctime>
#include <filesystem>
#include <fstream>
#include <iostream>

#include <rocksdb/utilities/checkpoint.h>

#include "cli/commands.h"
#include "ingest/ingest.h"
#include "provenance.h"
#include "store/rocksdb_store.h"

namespace evalcpp {

int cmd_checkpoint(const std::vector<std::string>& args) {
  if (args.size() != 2) {
    std::cerr << "usage: kvevaluator checkpoint <store-dir> <arena-dir>\n";
    return 2;
  }
  const std::string& src = args[0];
  const std::string& arena = args[1];
  // non-empty-target doctrine (the ingest lesson): never checkpoint INTO something
  if (std::filesystem::exists(arena))
    throw std::runtime_error("arena dir " + arena + " already exists -- delete it first "
                             "(an arena is disposable by design)");
  // an arena is never a SOURCE: it may carry applied writes its (copied) manifest knows
  // nothing about -- "settled: true" in there describes the original ingest, not this dir
  if (std::filesystem::exists(src + "/arena.json"))
    throw std::runtime_error("source " + src + " is itself an arena (possibly dirty) -- "
                             "checkpoint from the pristine store instead");
  json m = read_manifest(src);
  if (!m.value("settled", false))
    throw std::runtime_error("store " + src + " is not settled -- refuse to checkpoint");
  StoreOptions so = store_options_of(m, src);
  auto store = Store::open_read_write(src + "/db", so, false);
  std::filesystem::create_directories(arena);
  rocksdb::Checkpoint* cp = nullptr;
  rocksdb::Status st = rocksdb::Checkpoint::Create(store->db(), &cp);
  if (st.ok()) {
    st = cp->CreateCheckpoint(arena + "/db");
    delete cp;
  }
  if (!st.ok()) throw std::runtime_error("checkpoint: " + st.ToString());
  std::filesystem::copy_file(src + "/manifest.json", arena + "/manifest.json");
  char now[32];
  std::time_t t = std::time(nullptr);
  std::strftime(now, sizeof(now), "%Y-%m-%dT%H:%M:%S", std::localtime(&t));
  json marker = {{"source", std::filesystem::absolute(src).string()},
                 {"created", now},
                 {"provenance", provenance()}};
  std::ofstream(arena + "/arena.json") << marker.dump(1) << "\n";
  std::cout << json{{"arena", arena}, {"source", src}, {"created", now}}.dump() << "\n";
  return 0;
}

}  // namespace evalcpp
