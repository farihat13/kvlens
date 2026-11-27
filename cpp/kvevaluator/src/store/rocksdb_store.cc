#include "store/rocksdb_store.h"

#include <rocksdb/version.h>

#include "store/perf_snapshot.h"

#include <dirent.h>
#include <sys/stat.h>
#include <unistd.h>

#include <fcntl.h>
#include <rocksdb/convenience.h>
#include <rocksdb/filter_policy.h>
#include <rocksdb/memory_allocator.h>
#include <rocksdb/iostats_context.h>
#include <rocksdb/perf_context.h>
#include <rocksdb/table.h>
#include <unistd.h>

#include <algorithm>
#include <map>
#include <stdexcept>

namespace evalcpp {

// name -> spec. The zstd-dict variants train a per-SST dictionary from samples of the file
// (training buffer 100x the dictionary, RocksDB's recommended ratio) -- the Python
// The Python store's names, kept so a store dir means the same thing on both sides.
static const std::map<std::string, CompressionSpec>& registry() {
  static const std::map<std::string, CompressionSpec> r = {
      {"none", {rocksdb::kNoCompression, rocksdb::kDisableCompressionOption, 0}},
      {"snappy", {rocksdb::kSnappyCompression, rocksdb::kDisableCompressionOption, 0}},
      {"lz4", {rocksdb::kLZ4Compression, rocksdb::kDisableCompressionOption, 0}},
      {"zstd", {rocksdb::kZSTD, rocksdb::kDisableCompressionOption, 0}},
      {"zstd-dict16k", {rocksdb::kZSTD, rocksdb::kDisableCompressionOption, 16 << 10}},
      {"zstd-dict64k", {rocksdb::kZSTD, rocksdb::kDisableCompressionOption, 64 << 10}},
  };
  return r;
}

CompressionSpec compression_by_name(const std::string& name) {
  const std::string prefix = "bottommost:";
  if (name.rfind(prefix, 0) == 0) {
    CompressionSpec inner = compression_by_name(name.substr(prefix.size()));
    CompressionSpec s;
    s.type = rocksdb::kLZ4Compression;   // fast codec on the upper levels
    s.bottommost = inner.type;
    s.max_dict_bytes = inner.max_dict_bytes;
    return s;
  }
  auto it = registry().find(name);
  if (it == registry().end()) {
    std::string known;
    for (auto& kv : registry()) known += (known.empty() ? "" : ", ") + kv.first;
    throw std::runtime_error("unknown compression '" + name + "' (known: " + known +
                             ", bottommost:<name>)");
  }
  return it->second;
}

std::vector<std::string> compression_names() {
  std::vector<std::string> out;
  for (auto& kv : registry()) out.push_back(kv.first);
  return out;
}

// every setting the store opened with: store_config.json's rocksdb block, its run flags at this run's values and its
// build choices at this store's (from its manifest)
nlohmann::json StoreOptions::rocksdb_record() const {
  nlohmann::json r = contract::rocksdb();
  r["compression"] = compression;
  r["index_filter_blocks"] = index_filter_blocks;
  r["bloom_bits_per_key"] = build_settings_known ? nlohmann::json(bloom_bits_per_key) : nlohmann::json(nullptr);
  r["partition_bytes"] = build_settings_known ? nlohmann::json(partition_bytes) : nlohmann::json(nullptr);
  r["block_cache_bytes"] = block_cache_bytes;
  r["cache_index_and_filter_blocks"] = cache_index_and_filter_blocks;
  r["write_buffer_bytes"] = write_buffer_bytes;
  r["max_write_buffer_number"] = max_write_buffer_number;
  r["data_block_bytes"] = table_options(*this).block_size;
  return r;
}

nlohmann::json StoreOptions::to_json() const {
  return {{"compression", compression},
          {"block_cache_bytes", block_cache_bytes},
          {"direct_io", direct_io},
          {"cache_index_and_filter_blocks", cache_index_and_filter_blocks},
          {"index_filter_blocks", index_filter_blocks},
          {"block_size", table_options(*this).block_size},   // the store's own, from its manifest
          {"write_buffer_bytes", write_buffer_bytes},
          {"max_write_buffer_number", max_write_buffer_number},
          // the apply path's fixed write settings (store/counting.cc put / del_key): default
          // WriteOptions, one engine write per logical write -- recorded, not knobs
          {"wal", true},
          {"wal_sync", false},
          {"write_batch", 1},
          // read back from the options the store opens with (table_options) and the ReadOptions every read uses (a
          // read_options(), store/counting.cc): what ran, never a literal
          {"max_auto_readahead_size", table_options(*this).max_auto_readahead_size},
          {"initial_auto_readahead_size", table_options(*this).initial_auto_readahead_size},
          {"readahead_size", read_options().readahead_size},
          {"adaptive_readahead", read_options().adaptive_readahead},
          {"async_io", read_options().async_io},
          {"fill_cache", read_options().fill_cache},
          {"disable_auto_compactions", !auto_compactions},
          {"rocksdb", rocksdb_record()}};
}

rocksdb::ReadOptions read_options() { return rocksdb::ReadOptions(); }

rocksdb::BlockBasedTableOptions table_options(const StoreOptions& so) {
  rocksdb::BlockBasedTableOptions t;
  t.block_size = so.block_size;
  t.cache_index_and_filter_blocks = so.cache_index_and_filter_blocks;
  t.cache_index_and_filter_blocks_with_high_priority = rocksdb_setting<bool>("index_filter_high_priority");
  t.pin_l0_filter_and_index_blocks_in_cache = rocksdb_setting<bool>("pin_l0_filter_and_index_blocks");
  // the iterator readahead stays RocksDB's default (7.5.3: from 8 KB after 2 sequential reads of a file, doubling to
  // 256 KB); inputs/contract/store_config.json.
  // bloom_bits_per_key 0: no filter_policy at all (RocksDB's default; 7.5.3's builder makes no filter block for a null
  // policy), never NewBloomFilterPolicy(0)
  if (so.bloom_bits_per_key > 0) t.filter_policy.reset(rocksdb::NewBloomFilterPolicy(so.bloom_bits_per_key));
  if (so.index_filter_blocks == "partitioned") {
    t.index_type = rocksdb::BlockBasedTableOptions::IndexType::kTwoLevelIndexSearch;
    t.partition_filters = t.filter_policy != nullptr;   // the index is partitioned either way; a filter only if there is one
    t.metadata_block_size = so.partition_bytes;   // the partition size
    t.pin_top_level_index_and_filter = rocksdb_setting<bool>("pin_top_level_index_and_filter");   // read at open, kept
  } else if (so.index_filter_blocks != "whole") {
    throw std::runtime_error("index_filter_blocks '" + so.index_filter_blocks + "': want whole | partitioned");
  }
  return t;
}

rocksdb::Options make_options(const StoreOptions& so, std::shared_ptr<rocksdb::Cache> cache) {
  rocksdb::Options o;
  o.create_if_missing = false;
  o.disable_auto_compactions = !so.auto_compactions;
  o.max_open_files = rocksdb_setting<int>("max_open_files");
  // RocksDB rotates its INFO log on every open and keeps 1000 of them by default: a store
  // opened by every capture accumulated hundreds of MB of LOG.old.* (more than its SSTs at
  // sf0.1) that `size` then counted as store bytes. Two are enough to read a failure.
  o.keep_log_file_num = rocksdb_setting<size_t>("keep_log_file_num");
  o.write_buffer_size = so.write_buffer_bytes;
  o.max_write_buffer_number = so.max_write_buffer_number;
  o.use_direct_reads = so.direct_io;
  o.use_direct_io_for_flush_and_compaction = so.direct_io;
  CompressionSpec cs = compression_by_name(so.compression);
  // a codec the linked librocksdb lacks would be refused at open, or an SstFileWriter would write the blocks raw: refuse
  // here, naming it (the build checks the five codecs too: cmake/check_rocksdb_codecs.cmake)
  static const std::vector<rocksdb::CompressionType> linked = rocksdb::GetSupportedCompressions();
  for (rocksdb::CompressionType c : {cs.type, cs.bottommost})
    if (c != rocksdb::kDisableCompressionOption && std::find(linked.begin(), linked.end(), c) == linked.end())
      throw std::runtime_error("compression '" + so.compression + "': the linked librocksdb does not support it");
  o.compression = cs.type;
  o.bottommost_compression = cs.bottommost;
  if (cs.max_dict_bytes) {
    o.compression_opts.max_dict_bytes = cs.max_dict_bytes;
    o.compression_opts.zstd_max_train_bytes = 100 * cs.max_dict_bytes;
    o.bottommost_compression_opts = o.compression_opts;
    o.bottommost_compression_opts.enabled = true;
  }
  rocksdb::BlockBasedTableOptions t = table_options(so);
  t.block_cache = cache;
  o.table_factory.reset(rocksdb::NewBlockBasedTableFactory(t));
  return o;
}

std::unique_ptr<Store> Store::open_read_only(const std::string& dir, const StoreOptions& so) {
  std::unique_ptr<Store> s(new Store());
  s->dir_ = dir;
  s->so_ = so;
  s->open(true, false);
  return s;
}

std::unique_ptr<Store> Store::open_read_write(const std::string& dir, const StoreOptions& so,
                                              bool create) {
  std::unique_ptr<Store> s(new Store());
  s->dir_ = dir;
  s->so_ = so;
  s->open(false, create);
  return s;
}

// the WAL files (*.log) in a db dir
static std::set<std::string> wal_files(const std::string& dir) {
  std::set<std::string> out;
  if (DIR* d = opendir(dir.c_str())) {
    while (dirent* e = readdir(d)) {
      std::string n = e->d_name;
      if (n.size() > 4 && n.substr(n.size() - 4) == ".log") out.insert(n);
    }
    closedir(d);
  }
  return out;
}

// malloc / free, with MemoryAllocator's default UsableSize (= the requested size): the cache's charge per block
class DataBytesAllocator : public rocksdb::MemoryAllocator {
 public:
  const char* Name() const override { return "DataBytesAllocator"; }
  void* Allocate(size_t size) override { return malloc(size); }
  void Deallocate(void* p) override { free(p); }
};

std::shared_ptr<rocksdb::Cache> make_block_cache(size_t bytes) {
  rocksdb::LRUCacheOptions o;
  o.capacity = bytes;
  o.high_pri_pool_ratio = rocksdb_setting<double>("high_pri_pool_ratio");
  o.memory_allocator = std::make_shared<DataBytesAllocator>();
  return rocksdb::NewLRUCache(o);
}

void Store::open(bool read_only, bool create) {
  if (!read_only) wals_at_open_ = wal_files(dir_);
  cache_ = make_block_cache(so_.block_cache_bytes);
  if (stats_)                                    // a reopen: the closed DB's counts carry over (ticker())
    for (auto& [t, name] : rocksdb::TickersNameMap) carry_[t] += stats_->getTickerCount(t);
  stats_ = rocksdb::CreateDBStatistics();
  stats_->set_stats_level(rocksdb::kExceptTimeForMutex);
  rocksdb::Options o = make_options(so_, cache_);
  o.statistics = stats_;
  o.create_if_missing = create;
#if ROCKSDB_MAJOR >= 11   // RocksDB 11 opens into a unique_ptr (checked on 7.5.3 and 11.8.1 only)
  std::unique_ptr<rocksdb::DB> db;
  rocksdb::Status st = read_only ? rocksdb::DB::OpenForReadOnly(o, dir_, &db)
                                 : rocksdb::DB::Open(o, dir_, &db);
  db_ = db.release();
#else
  rocksdb::Status st = read_only ? rocksdb::DB::OpenForReadOnly(o, dir_, &db_)
                                 : rocksdb::DB::Open(o, dir_, &db_);
#endif
  if (!st.ok()) throw std::runtime_error("open " + dir_ + ": " + st.ToString());
  read_only_ = read_only;
}

Store::~Store() { close(); }

void Store::close() {
  if (!db_) return;
  // A read-write open creates a fresh WAL, and RocksDB deletes a WAL only once a memtable FLUSH moves the MANIFEST's
  // log number past it. A session that writes nothing never flushes, so every such open left one empty `*.log`
  // behind (63,000 on a LinkBench meta store; each open scans them: 235 -> 500 ms per spawned run). A read-only open
  // would leave none, but this fork's read-only Get records no tickers, so sessions open read-write. So: the WAL(s)
  // THIS open created and never wrote to are removed after Close(). Listed before Close() -- the LOCK is still held,
  // so no other process can have added one -- and only zero-byte files go: a session that wrote has bytes in its
  // WAL and RocksDB's own Close() flushes and deletes it. Safe to delete: track_and_verify_wals_in_manifest is off
  // (recovery reads the logs it finds) and the removed number is the highest, so the next open's continues past it.
  std::set<std::string> now = read_only_ ? std::set<std::string>{} : wal_files(dir_);
  db_->Close();
  delete db_;
  db_ = nullptr;
  for (auto& n : now) {
    if (wals_at_open_.count(n)) continue;
    std::string p = dir_ + "/" + n;
    struct stat sb;
    if (::stat(p.c_str(), &sb) == 0 && sb.st_size == 0) ::unlink(p.c_str());
  }
}

void Store::reopen_cold() {
  bool ro = read_only_;
  close();
  cache_.reset();   // the old cache dies with the DB: a new one is empty
  open(ro, false);
}

void Store::evict_page_cache() const {
  for (auto& f : live_files()) {
    std::string path = f.db_path + f.name;
    int fd = ::open(path.c_str(), O_RDONLY);
    if (fd < 0) continue;
    ::fdatasync(fd);
    ::posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED);
    ::close(fd);
  }
}

nlohmann::json Store::tickers() const {
  nlohmann::json j = nlohmann::json::object();
  for (auto& [t, name] : rocksdb::TickersNameMap) j[name] = ticker(t);
  return j;
}

std::vector<uint64_t> Store::ticker_counts(const std::vector<size_t>* only) const {
  std::vector<uint64_t> v;
  if (only) {
    v.reserve(only->size());
    for (size_t i : *only) v.push_back(ticker(rocksdb::TickersNameMap[i].first));
    return v;
  }
  v.reserve(rocksdb::TickersNameMap.size());
  for (auto& [t, name] : rocksdb::TickersNameMap) v.push_back(ticker(t));
  return v;
}

std::vector<size_t> ticker_indices(const std::vector<std::string>& names) {
  std::vector<size_t> idx;
  for (auto& n : names) {
    size_t i = 0;
    while (i < rocksdb::TickersNameMap.size() && rocksdb::TickersNameMap[i].second != n) i++;
    if (i == rocksdb::TickersNameMap.size()) throw std::runtime_error("unknown RocksDB ticker '" + n + "'");
    idx.push_back(i);
  }
  std::sort(idx.begin(), idx.end());                  // the delta's keys in TickersNameMap order, as without the list
  idx.erase(std::unique(idx.begin(), idx.end()), idx.end());
  return idx;
}

nlohmann::json Store::histograms() const {
  nlohmann::json j = nlohmann::json::object();
  for (auto& [h, name] : rocksdb::HistogramsNameMap) {
    rocksdb::HistogramData d;
    stats_->histogramData(h, &d);
    if (d.count == 0) continue;
    j[name] = {{"count", d.count}, {"sum", d.sum}, {"p50", d.median},
               {"p99", d.percentile99}, {"max", d.max}};
  }
  return j;
}

std::vector<rocksdb::LiveFileMetaData> Store::live_files() const {
  std::vector<rocksdb::LiveFileMetaData> v;
  db_->GetLiveFilesMetaData(&v);
  return v;
}

uint64_t Store::sst_bytes() const {
  uint64_t n = 0;
  for (auto& f : live_files()) n += f.size;
  return n;
}

static std::string file_signature(const std::vector<rocksdb::LiveFileMetaData>& v) {
  std::string s;
  for (auto& f : v) s += f.name + ":" + std::to_string(f.size) + ";";
  return s;
}

void Store::settle() {
  if (read_only_) throw std::runtime_error("settle: store is read-only");
  rocksdb::Status st = db_->Flush(rocksdb::FlushOptions());
  if (!st.ok()) throw std::runtime_error("flush: " + st.ToString());
  rocksdb::CompactRangeOptions cro;
  cro.bottommost_level_compaction = rocksdb::BottommostLevelCompaction::kForce;
  st = db_->CompactRange(cro, nullptr, nullptr);
  if (!st.ok()) throw std::runtime_error("compact: " + st.ToString());
  // Every live file must be fully rewritten: seqno 0 (a moved-not-rewritten file keeps
  // live seqnos, which inflate a compressed store -- the physize.settle() trap).
  auto files = live_files();
  for (auto& f : files) {
    if (f.smallest_seqno != 0 || f.largest_seqno != 0)
      throw std::runtime_error("settle: " + f.name + " keeps seqnos " +
                               std::to_string(f.smallest_seqno) + ".." +
                               std::to_string(f.largest_seqno));
  }
  // A second, non-forced pass must find nothing left to do (kForce would rewrite again).
  std::string before = file_signature(files);
  cro.bottommost_level_compaction = rocksdb::BottommostLevelCompaction::kSkip;
  st = db_->CompactRange(cro, nullptr, nullptr);
  if (!st.ok()) throw std::runtime_error("compact (2nd): " + st.ToString());
  std::string after = file_signature(live_files());
  if (before != after) throw std::runtime_error("settle: a second compaction changed the files");
}

// --- per-thread timers ----------------------------------------------------------------

void set_perf_level(int level) {
  rocksdb::SetPerfLevel(static_cast<rocksdb::PerfLevel>(level));
}

void perf_reset() {
  rocksdb::get_perf_context()->Reset();
  rocksdb::get_iostats_context()->Reset();
  kv_call_times() = KvCallTimes();     // the boundary timer's window starts with the engine's
}

// ToString(exclude_zero) prints "name = value, name = value, ..."; per-level entries print
// as "name = level N: value, ..." -- those are folded as name@levelN.
static nlohmann::json parse_kv_dump(const std::string& s) {
  nlohmann::json j = nlohmann::json::object();
  size_t i = 0;
  while (i < s.size()) {
    size_t eq = s.find(" = ", i);
    if (eq == std::string::npos) break;
    std::string name = s.substr(i, eq - i);
    while (!name.empty() && name[0] == ' ') name.erase(0, 1);
    size_t end = s.find(", ", eq + 3);
    std::string val = s.substr(eq + 3, end == std::string::npos ? std::string::npos : end - eq - 3);
    i = (end == std::string::npos) ? s.size() : end + 2;
    // "level 0: 12, level 1: 3" style
    if (val.rfind("level ", 0) == 0) continue;
    try {
      j[name] = std::stoull(val);
    } catch (...) {
    }
  }
  return j;
}

nlohmann::json perf_context_json() {
  return parse_kv_dump(rocksdb::get_perf_context()->ToString(true));
}

nlohmann::json iostats_json() {
  return parse_kv_dump(rocksdb::get_iostats_context()->ToString(true));
}

nlohmann::json tickers_delta(const nlohmann::json& before, const nlohmann::json& after) {
  nlohmann::json d = nlohmann::json::object();
  for (auto it = after.begin(); it != after.end(); ++it) {
    uint64_t b = before.contains(it.key()) ? before[it.key()].get<uint64_t>() : 0;
    uint64_t a = it.value().get<uint64_t>();
    if (a < b) throw std::runtime_error("tickers_delta: " + it.key() + " went backwards (a different Statistics object?)");
    if (a != b) d[it.key()] = a - b;
  }
  return d;
}

nlohmann::json ticker_counts_delta(const std::vector<uint64_t>& before, const std::vector<uint64_t>& after,
                                   const std::vector<size_t>* only) {
  if (before.size() != after.size() || after.size() != (only ? only->size() : rocksdb::TickersNameMap.size()))
    throw std::runtime_error("ticker_counts_delta: snapshots of different sizes");
  nlohmann::json d = nlohmann::json::object();
  for (size_t i = 0; i < after.size(); i++) {
    const std::string& name = rocksdb::TickersNameMap[only ? (*only)[i] : i].second;
    if (after[i] < before[i]) throw std::runtime_error("tickers_delta: " + name + " went backwards (a different Statistics object?)");
    if (after[i] != before[i]) d[name] = after[i] - before[i];
  }
  return d;
}

nlohmann::json histograms_delta(const nlohmann::json& before, const nlohmann::json& after) {
  nlohmann::json d = nlohmann::json::object();
  for (auto it = after.begin(); it != after.end(); ++it) {
    double c0 = 0, s0 = 0;
    if (before.contains(it.key())) {
      c0 = before[it.key()].value("count", 0.0);
      s0 = before[it.key()].value("sum", 0.0);
    }
    double dc = it.value().value("count", 0.0) - c0;
    double ds = it.value().value("sum", 0.0) - s0;
    if (dc != 0 || ds != 0) d[it.key()] = {{"count", dc}, {"sum", ds}};
  }
  return d;
}

}  // namespace evalcpp
