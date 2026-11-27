// The bulk-load sink's small-run pool (ingest/sst_sink.h): a stream that changes run every few keys
// (arango's edge docs on LinkBench) must not make one ingest file per run, the store must hold every
// key once whatever the file split, and a key written by two runs must stay an error on every path
// (pooled: at the pool's write; streamed: finish()'s entries == keys check).
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <map>
#include <string>
#include <unistd.h>

#include <rocksdb/table.h>
#include <rocksdb/table_properties.h>

#include "ingest/sst_sink.h"
#include "store/rocksdb_store.h"
#include "harness.h"

namespace {

using namespace evalcpp;

#define THROWS(stmt) do { bool t = false; try { stmt; } catch (const std::exception&) { t = true; } if (!t) { fails++; std::cerr << "FAIL " << __LINE__ << " expected throw: " #stmt "\n"; } } while (0)

static std::vector<std::string> dirs;
static std::string tmpdir() {
  char tmpl[] = "/tmp/evalcpp-sink-XXXXXX";
  dirs.push_back(kvt::temp_dir(tmpl));
  return dirs.back();
}

static std::string key(char type, int id) {
  char b[32];
  std::snprintf(b, sizeof b, "%c%08d", type, id);
  return b;
}

// a pair's RAM estimate in the sink's run buffer (the cap is compared against this)
static size_t est(const std::string& k, const std::string& v) { return k.size() + v.size() + SstSink::kPairOverhead; }

// two "types" whose ids interleave in segments of `seg` keys: each segment is one run, as arango's
// edge-doc runs on LinkBench; returns what the store must hold
static std::map<std::string, std::string> write_interleaved(SstSink& sink, int n, int seg) {
  std::map<std::string, std::string> want;
  for (int start = 0; start < n; start += seg) {
    char type = (start / seg) % 2 ? 'b' : 'a';
    sink.begin_run(std::string("seg-") + type);
    for (int id = start; id < start + seg && id < n; id++) {
      std::string k = key(type, id), v = "v" + std::to_string(id);
      sink.put(k, v);
      want[k] = v;
    }
    sink.end_run();
  }
  return want;
}

static std::map<std::string, std::string> read_all(const std::string& dir, const std::string& compression = "none") {
  StoreOptions so;
  so.compression = compression;
  auto store = Store::open_read_write(dir + "/db", so, false);
  std::map<std::string, std::string> got;
  std::unique_ptr<rocksdb::Iterator> it(store->db()->NewIterator(rocksdb::ReadOptions()));
  for (it->SeekToFirst(); it->Valid(); it->Next()) got[it->key().ToString()] = it->value().ToString();
  return got;
}

TEST_CASE(test_sst_sink) {
  // 1. 1,000 runs of 9 keys, default caps: all pooled, one file written at finish(), every key once
  {
    std::string d = tmpdir();
    SstSink sink({{d, "none"}});
    auto want = write_interleaved(sink, 9000, 9);
    CHECK_EQ(sink.ingest_files(), size_t(0));
    std::vector<json> m(1);
    sink.finish(m);
    CHECK_EQ(sink.ingest_files(), size_t(1));
    CHECK_EQ(m[0]["ingest_files"].get<size_t>(), size_t(1));
    CHECK(read_all(d) == want);
  }
  // 2. the same stream, every run streamed to its own file (cap 0): the old split, same content
  {
    std::string d = tmpdir();
    SstSink sink({{d, "none"}}, 0);
    auto want = write_interleaved(sink, 9000, 9);
    CHECK_EQ(sink.ingest_files(), size_t(1000));
    std::vector<json> m(1);
    sink.finish(m);
    CHECK(read_all(d) == want);
  }
  // 3. a tiny pool (1 byte): one pooled file per run, written as each run ends; same content
  {
    std::string d = tmpdir();
    SstSink sink({{d, "none"}}, size_t(1) << 20, 1);
    auto want = write_interleaved(sink, 900, 9);
    CHECK_EQ(sink.ingest_files(), size_t(100));
    std::vector<json> m(1);
    sink.finish(m);
    CHECK(read_all(d) == want);
  }
  // 4. a run that outgrows the cap spills mid-run (its big pair goes straight to the file); small and
  //    empty runs around it pool or vanish; index runs written by finish() pool too
  {
    std::string d = tmpdir();
    SstSink sink({{d, "none"}}, 300);
    std::map<std::string, std::string> want;
    sink.begin_run("empty-1"); sink.end_run();
    sink.begin_run("big");
    for (int id = 0; id < 1000; id++) { sink.put(key('c', id), "x"); want[key('c', id)] = "x"; }
    sink.put(key('c', 1000), std::string(10000, 'y')); want[key('c', 1000)] = std::string(10000, 'y');
    sink.end_run();
    CHECK_EQ(sink.ingest_files(), size_t(1));
    sink.begin_run("empty-2"); sink.end_run();
    auto small = write_interleaved(sink, 6, 3);    // two runs of 3 pairs, ~228 B estimated each: pooled
    want.insert(small.begin(), small.end());
    CHECK_EQ(sink.ingest_files(), size_t(1));
    sink.put_index(7, "i-b"); sink.put_index(7, "i-a"); sink.put_index(3, "h-z");
    want["i-a"] = ""; want["i-b"] = ""; want["h-z"] = "";
    std::vector<json> m(1);
    sink.finish(m);
    CHECK_EQ(sink.ingest_files(), size_t(2));      // the big run + one pool
    CHECK(read_all(d) == want);
  }
  // 5. the cap boundary: a run of exactly the cap pools, one byte more spills
  {
    std::string d = tmpdir();
    std::string k1 = "k1", v1 = "abc";
    SstSink sink({{d, "none"}}, est(k1, v1));
    sink.begin_run("exact"); sink.put(k1, v1); sink.end_run();
    CHECK_EQ(sink.ingest_files(), size_t(0));
    sink.begin_run("over"); sink.put("k2", "abcd"); sink.end_run();
    CHECK_EQ(sink.ingest_files(), size_t(1));
    std::vector<json> m(1);
    sink.finish(m);
    CHECK((read_all(d) == std::map<std::string, std::string>{{"k1", "abc"}, {"k2", "abcd"}}));
  }
  // 6. two targets (two compressions): the files and ranges stay in lockstep, both stores hold the keys
  {
    std::string d1 = tmpdir(), d2 = tmpdir();
    SstSink sink({{d1, "none"}, {d2, "zstd"}}, 300, 2000);
    auto want = write_interleaved(sink, 300, 3);   // pooled, a pool file every ~2,000 B
    sink.begin_run("big");
    for (int id = 0; id < 100; id++) { sink.put(key('z', id), "q"); want[key('z', id)] = "q"; }
    sink.end_run();
    std::vector<json> m(2);
    sink.finish(m);
    CHECK(sink.ingest_files() > size_t(2));
    CHECK(read_all(d1) == want);
    CHECK(read_all(d2, "zstd") == want);
  }
  // 7. a key written by two runs: an error when both runs are in one pool ...
  {
    std::string d = tmpdir();
    SstSink sink({{d, "none"}});
    sink.begin_run("r1"); sink.put("k1", "first"); sink.end_run();
    sink.begin_run("r2"); sink.put("k1", "second"); sink.end_run();
    std::vector<json> m(1);
    THROWS(sink.finish(m));
  }
  // ... in two pool files (flushed apart) ...
  {
    std::string d = tmpdir();
    SstSink sink({{d, "none"}}, size_t(1) << 20, 1);
    sink.begin_run("r1"); sink.put("k1", "first"); sink.end_run();
    sink.begin_run("r2"); sink.put("k1", "second"); sink.end_run();
    CHECK_EQ(sink.ingest_files(), size_t(2));
    std::vector<json> m(1);
    THROWS(sink.finish(m));
  }
  // ... in two streamed files (the pre-pool path) ...
  {
    std::string d = tmpdir();
    SstSink sink({{d, "none"}}, 0);
    sink.begin_run("r1"); sink.put("k1", "first"); sink.end_run();
    sink.begin_run("r2"); sink.put("k1", "second"); sink.end_run();
    std::vector<json> m(1);
    THROWS(sink.finish(m));
  }
  // ... and one streamed, one pooled
  {
    std::string d = tmpdir();
    SstSink sink({{d, "none"}}, 200);
    sink.begin_run("big"); sink.put("k0", std::string(300, 'x')); sink.put("k1", "first"); sink.end_run();
    CHECK_EQ(sink.ingest_files(), size_t(1));
    sink.begin_run("small"); sink.put("k1", "s"); sink.end_run();
    CHECK_EQ(sink.ingest_files(), size_t(1));      // pooled
    std::vector<json> m(1);
    THROWS(sink.finish(m));
  }
  // 8. keys out of order inside one run stay an error; finish() inside a run is refused
  {
    std::string d = tmpdir();
    SstSink sink({{d, "none"}});
    sink.begin_run("r");
    sink.put("k2", "");
    THROWS(sink.put("k1", ""));
    std::vector<json> m(1);
    THROWS(sink.finish(m));
  }
  // 9. the file guard: past max_files a new file is refused as it is opened
  {
    std::string d = tmpdir();
    SstSink sink({{d, "none"}}, 0, kSstPoolBytes, 3);
    for (int i = 0; i < 3; i++) { sink.begin_run("r"); sink.put(key('a', i), "v"); sink.end_run(); }
    sink.begin_run("r4");
    THROWS(sink.put(key('a', 3), "v"));
  }
  // 10. index_filter_blocks: one stream into a whole and a partitioned target -- the same content; the
  // partitioned store's SSTs carry a two-level index in partitions and, when store_config.json decides a filter
  // (bloom_bits_per_key > 0), a partitioned filter; a GET finds its key and misses an absent one; an unknown setting is refused
  {
    std::string d1 = tmpdir(), d2 = tmpdir();
    SstSink sink({{d1, "none", "whole"}, {d2, "none", "partitioned"}});
    std::map<std::string, std::string> want;
    sink.begin_run("r");
    for (int id = 0; id < 200000; id++) { std::string k = key('p', id), v(40, char('a' + id % 26)); sink.put(k, v); want[k] = v; }
    sink.end_run();
    std::vector<json> m(2);
    sink.finish(m);
    CHECK(read_all(d1) == want);
    StoreOptions so;
    so.index_filter_blocks = "partitioned";
    {
      auto store = Store::open_read_write(d2 + "/db", so, false);
      std::map<std::string, std::string> got;
      std::unique_ptr<rocksdb::Iterator> it(store->db()->NewIterator(rocksdb::ReadOptions()));
      for (it->SeekToFirst(); it->Valid(); it->Next()) got[it->key().ToString()] = it->value().ToString();
      CHECK(got == want);
      std::string v;
      CHECK(store->db()->Get(rocksdb::ReadOptions(), key('p', 123457), &v).ok());
      CHECK_EQ(v, want[key('p', 123457)]);
      CHECK(store->db()->Get(rocksdb::ReadOptions(), key('q', 1), &v).IsNotFound());
      rocksdb::TablePropertiesCollection props;
      CHECK(store->db()->GetPropertiesOfAllTables(&props).ok());
      CHECK(!props.empty());
      for (auto& [f, tp] : props) {
        CHECK(tp->index_partitions > 1);            // a two-level index: partitions under a top level
        CHECK(tp->top_level_index_size > 0);
        CHECK((tp->filter_size > 0) == (rocksdb_setting<double>("bloom_bits_per_key") > 0));   // a filter only if decided
      }
    }
    {
      StoreOptions whole;
      auto store = Store::open_read_write(d1 + "/db", whole, false);
      rocksdb::TablePropertiesCollection props;
      CHECK(store->db()->GetPropertiesOfAllTables(&props).ok());
      for (auto& [f, tp] : props) {
        CHECK_EQ(tp->index_partitions, uint64_t(0));
        CHECK_EQ(tp->top_level_index_size, uint64_t(0));
      }
    }
    StoreOptions bad;
    bad.index_filter_blocks = "partitionned";
    THROWS(make_options(bad, nullptr));
  }
  for (auto& d : dirs) std::filesystem::remove_all(d);
  if (fails) { std::cerr << fails << " failure(s)\n"; return 1; }
  std::cout << "sst sink OK (pooled / streamed / spilled / two-target splits hold the same keys; cross-run "
               "duplicates refused on 4 paths; the file guard; partitioned index + filters)\n";
  return 0;
}

}  // namespace
