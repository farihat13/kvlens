// The RocksDB backend: open/close a store with the fixed measurement options, the
// block-compression registry, and the engine's own counters (Statistics tickers,
// per-thread PerfContext / IOStatsContext) exported as JSON.
#pragma once
#include <nlohmann/json.hpp>

#include "contract/contract.h"
#if __has_include(<rocksdb/advanced_cache.h>)   // RocksDB 8+: Cache is only forward-declared in cache.h
#include <rocksdb/advanced_cache.h>
#endif
#include <rocksdb/cache.h>
#include <rocksdb/db.h>
#include <rocksdb/options.h>
#include <rocksdb/statistics.h>
#include <rocksdb/table.h>

#include <memory>
#include <set>
#include <string>
#include <vector>

namespace evalcpp {

// Block-level compression, by the names the Python drivers use ("none", "zstd",
// "zstd-dict16k", ...). "bottommost:<name>" = lz4 on the upper levels, <name> at the bottom.
struct CompressionSpec {
  rocksdb::CompressionType type = rocksdb::kNoCompression;
  rocksdb::CompressionType bottommost = rocksdb::kDisableCompressionOption;
  int max_dict_bytes = 0;   // zstd dictionary size; 0 = no dictionary
};
CompressionSpec compression_by_name(const std::string& name);   // throws on an unknown name
std::vector<std::string> compression_names();

// Every RocksDB setting comes from inputs/contract/store_config.json's `rocksdb` block, compiled in (contract::rocksdb);
// `rocksdb_setting<T>(name)` reads one.
template <class T>
T rocksdb_setting(const char* name) { return contract::rocksdb().at(name).get<T>(); }

// the data block size a new store gets (ingest --block-size changes it, for a control or a sensitivity arm only)
inline size_t data_block_bytes() { return rocksdb_setting<size_t>("data_block_bytes"); }

struct StoreOptions {
  std::string compression = rocksdb_setting<std::string>("compression");
  size_t block_cache_bytes = rocksdb_setting<size_t>("block_cache_bytes");   // a run knob, reported
  bool direct_io = false;
  // index + filter blocks in the block cache (the default; the whole-file bloom filter of a
  // multi-million-key SST is 1-4 MB, so under a SMALL bounded cache they thrash and every Get
  // re-reads a filter block -- the steady regime keeps them OUT of the budget: the bounded
  // cache then holds data blocks only, like a deployment that pins its index and filters)
  bool cache_index_and_filter_blocks = rocksdb_setting<bool>("cache_index_and_filter_blocks");
  // how each SST stores its index and bloom filter: "whole" (default: one index block and one
  // filter block per file, read whole on a file's first use) or "partitioned" (4 KB partitions
  // under a top-level index read at open, so a first use reads one partition of each). A store
  // layout fixed at ingest (the manifest's `index_filter_blocks`); readers take it from there (store_options_of).
  std::string index_filter_blocks = rocksdb_setting<std::string>("index_filter_blocks");
  // the data block target size, the bloom filter's bits per key and the index / filter partition size: fixed at ingest
  // too, in the manifest. build_settings_known is false for a store whose manifest predates the bloom and partition
  // fields: its record says null for them, never the reading binary's values.
  size_t block_size = data_block_bytes();
  double bloom_bits_per_key = rocksdb_setting<double>("bloom_bits_per_key");
  size_t partition_bytes = rocksdb_setting<size_t>("partition_bytes");
  bool build_settings_known = true;
  // the memtable: a write lands in a memtable of this size; when it fills, RocksDB switches to a
  // new one and flushes the full one in the background, and a write waits (a stall) if every one
  // of max_write_buffer_number is full. RocksDB 7.5's defaults, set explicitly so a write-latency
  // run can name and change them (an open option, not a store layout).
  size_t write_buffer_bytes = rocksdb_setting<size_t>("write_buffer_bytes");
  int max_write_buffer_number = rocksdb_setting<int>("max_write_buffer_number");
  // RocksDB's background compactions (an open option): off for every measured store (its files never change
  // under a capture); on only for a mixed read + write replay on an arena, where flush AND compaction are the
  // protocol (the session refuses it on any other dir). On, RocksDB's own defaults decide when and what.
  bool auto_compactions = false;
  nlohmann::json to_json() const;
  nlohmann::json rocksdb_record() const;
};

// Options shared by ingest and measurement: no auto compaction, RocksDB's iterator readahead, index and
// filter blocks in the block cache, statistics on (inputs/contract/store_config.json, the one home of these settings).
rocksdb::Options make_options(const StoreOptions& so, std::shared_ptr<rocksdb::Cache> cache);

// The ReadOptions every measured read uses (store/counting.cc): RocksDB's defaults (fill_cache true, no readahead
// override, no async I/O). One place, so StoreOptions::to_json reports what ran.
rocksdb::ReadOptions read_options();
// The table options make_options uses, without the cache: StoreOptions::to_json reports from these, so the record says
// what the store really runs with.
rocksdb::BlockBasedTableOptions table_options(const StoreOptions& so);

// The block cache every store opens. A cached block is charged at its DATA bytes, not at what the allocator rounded
// the allocation up to: RocksDB (built with ROCKSDB_MALLOC_USABLE_SIZE) otherwise charges malloc_usable_size, which
// tcmalloc rounds to its size classes (a 4 KB block +12%, 16 KB +25%), so a bounded cache would hold fewer blocks by
// an amount that depends on each format's block sizes. The allocator is engine cost, so it is normalized away.
std::shared_ptr<rocksdb::Cache> make_block_cache(size_t bytes);

class Store {
 public:
  static std::unique_ptr<Store> open_read_only(const std::string& dir, const StoreOptions& so);
  static std::unique_ptr<Store> open_read_write(const std::string& dir, const StoreOptions& so,
                                                bool create);
  ~Store();

  rocksdb::DB* db() const { return db_; }
  const std::string& dir() const { return dir_; }
  const StoreOptions& options() const { return so_; }
  bool read_only() const { return read_only_; }

  nlohmann::json tickers() const;      // every Statistics ticker: name -> count
  // the same snapshot as counts in rocksdb::TickersNameMap order: what a per-request delta reads (building the name ->
  // count JSON twice per request cost ~90 us of the request's wall outside its clock)
  // `only`: those tickers alone (TickersNameMap indices, a batch's spec.tickers): each ticker read sums every core's shard
  std::vector<uint64_t> ticker_counts(const std::vector<size_t>* only = nullptr) const;
  // every ticker read counts since this Store was first opened: a reopen (reopen_cold) makes a new Statistics object,
  // and the old one's counts are carried, so a delta across a cold reopen (a batch's) never goes backwards
  uint64_t ticker(rocksdb::Tickers t) const { return stats_->getTickerCount(t) + carry_[t]; }
  nlohmann::json histograms() const;   // every histogram: name -> {count, sum, p50, p99, max}
  // the block cache now: its pinned bytes (blocks a live iterator or handle holds) and its whole usage, against its capacity
  size_t cache_pinned_bytes() const { return cache_ ? cache_->GetPinnedUsage() : 0; }
  size_t cache_usage_bytes() const { return cache_ ? cache_->GetUsage() : 0; }

  // Flush + force a bottommost compaction, then check the store is fully settled: every
  // live SST has seqno 0 and a second compaction changes nothing. Throws otherwise.
  void settle();
  std::vector<rocksdb::LiveFileMetaData> live_files() const;
  uint64_t sst_bytes() const;

  // Close and reopen (same mode) with a brand-new block cache (a COLD RocksDB cache).
  void reopen_cold();
  // Drop the OS page cache pages of this store's SST files (posix_fadvise DONTNEED).
  void evict_page_cache() const;
  void close();

 private:
  Store() = default;
  void open(bool read_only, bool create);
  std::string dir_;
  std::set<std::string> wals_at_open_;   // the db dir's *.log files before this open (close() drops the new empty ones)
  StoreOptions so_;
  bool read_only_ = true;
  std::shared_ptr<rocksdb::Cache> cache_;
  std::shared_ptr<rocksdb::Statistics> stats_;
  std::vector<uint64_t> carry_ = std::vector<uint64_t>(rocksdb::TICKER_ENUM_MAX, 0);   // closed Statistics' counts
  rocksdb::DB* db_ = nullptr;
};

// --- per-thread engine timers ------------------------------------------------------
void set_perf_level(int level);          // 0..5, RocksDB PerfLevel; 3 = time except mutex
void perf_reset();                       // zero this thread's PerfContext + IOStatsContext
nlohmann::json perf_context_json();      // every non-zero PerfContext field of this thread
nlohmann::json iostats_json();           // every non-zero IOStatsContext field of this thread

// A snapshot of every ticker, and the delta of two snapshots (only tickers that moved).
nlohmann::json tickers_delta(const nlohmann::json& before, const nlohmann::json& after);
// The same delta from two ticker_counts() snapshots: the same JSON as tickers_delta of the two tickers() snapshots.
// `only`: the snapshots were taken over those tickers (ticker_counts(only)).
nlohmann::json ticker_counts_delta(const std::vector<uint64_t>& before, const std::vector<uint64_t>& after,
                                   const std::vector<size_t>* only = nullptr);
// The TickersNameMap indices of the named tickers (a batch's spec.tickers); an unknown name throws.
std::vector<size_t> ticker_indices(const std::vector<std::string>& names);
// Histogram deltas between two histograms() snapshots: name -> {count, sum} moved (only
// nonzero; the percentile fields don't delta, so they're dropped).
nlohmann::json histograms_delta(const nlohmann::json& before, const nlohmann::json& after);

}  // namespace evalcpp
