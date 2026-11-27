#include "ingest/sst_sink.h"

#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <stdexcept>

namespace evalcpp {

SstSink::SstSink(const std::vector<IngestTarget>& targets, size_t small_run_bytes, size_t pool_bytes, size_t max_files)
    : small_run_bytes_(small_run_bytes), pool_bytes_(pool_bytes), max_files_(max_files) {
  for (auto& t : targets) {
    Target tg;
    tg.spec = t;
    StoreOptions so;
    so.compression = t.compression;
    so.index_filter_blocks = t.index_filter_blocks;
    so.block_size = t.block_size;
    so.block_cache_bytes = size_t(256) << 20;
    tg.cache = make_block_cache(so.block_cache_bytes);
    tg.options = make_options(so, tg.cache);
    ::mkdir(t.dir.c_str(), 0755);
    targets_.push_back(std::move(tg));
  }
}

void SstSink::open_files() {
  if (ingest_files() >= max_files_)
    throw std::runtime_error("SstSink: more than " + std::to_string(max_files_) +
                             " ingest files (ingest opens them all; the open-file limit is per process) -- in run " +
                             run_name_ + ": too many runs over the small-run cap");
  for (auto& t : targets_) {
    std::string path = t.spec.dir + "/ingest-" + std::to_string(t.files.size()) + ".sst";
    t.writer = std::make_unique<rocksdb::SstFileWriter>(rocksdb::EnvOptions(), t.options);
    rocksdb::Status st = t.writer->Open(path);
    if (!st.ok()) throw std::runtime_error("sst open " + path + ": " + st.ToString());
    t.files.push_back(path);
  }
}

void SstSink::put_files(std::string_view key, std::string_view value) {
  for (auto& t : targets_) {
    rocksdb::Status st = t.writer->Put(rocksdb::Slice(key.data(), key.size()), rocksdb::Slice(value.data(), value.size()));
    if (!st.ok()) throw std::runtime_error("sst put: " + st.ToString());
  }
}

void SstSink::finish_files(const std::string& first, const std::string& last) {
  ranges_.emplace_back(first, last);
  for (auto& t : targets_) {
    rocksdb::Status st = t.writer->Finish();
    if (!st.ok()) throw std::runtime_error("sst finish: " + st.ToString());
    t.writer.reset();
  }
}

void SstSink::begin_run(const std::string& name) {
  if (in_run_) throw std::runtime_error("SstSink: begin_run inside a run (" + run_name_ + ")");
  in_run_ = true;
  run_name_ = name;
  last_key_.clear();
  first_key_.clear();
  run_buf_.clear();
  run_buf_bytes_ = 0;
  streaming_ = false;
}

void SstSink::put(std::string_view key, std::string_view value) {
  if (!in_run_) throw std::runtime_error("SstSink: put outside a run");
  if (!last_key_.empty() && key <= std::string_view(last_key_))
    throw std::runtime_error("SstSink: keys out of order in run " + run_name_);
  if (first_key_.empty()) first_key_.assign(key.data(), key.size());
  last_key_.assign(key.data(), key.size());
  keys_++;
  bytes_ += key.size() + value.size();
  if (streaming_) {
    put_files(key, value);
    return;
  }
  size_t need = key.size() + value.size() + kPairOverhead;
  if (run_buf_bytes_ + need > small_run_bytes_) {   // a big run: its own file, streamed from here on (this pair
    spill();                                         // too -- a large value is never copied into the buffer)
    put_files(key, value);
    return;
  }
  run_buf_.emplace_back(std::string(key), std::string(value));
  run_buf_bytes_ += need;
}

void SstSink::spill() {
  open_files();
  for (auto& [k, v] : run_buf_) put_files(k, v);
  run_buf_.clear();
  run_buf_.shrink_to_fit();
  streaming_ = true;
}

void SstSink::end_run() {
  if (!in_run_) throw std::runtime_error("SstSink: end_run outside a run");
  in_run_ = false;
  if (streaming_) {
    finish_files(first_key_, last_key_);
    streaming_ = false;
    return;
  }
  if (run_buf_.empty()) return;   // an empty run: no file
  pool_now_ += run_buf_bytes_;
  for (auto& kv : run_buf_) pool_.push_back(std::move(kv));
  run_buf_.clear();
  if (pool_now_ >= pool_bytes_) flush_pool();
}

void SstSink::flush_pool() {
  if (pool_.empty()) return;
  std::sort(pool_.begin(), pool_.end(), [](const auto& a, const auto& b) { return a.first < b.first; });
  // two runs wrote one key: an error, as for streamed files (finish()'s entries == keys check)
  for (size_t i = 1; i < pool_.size(); i++)
    if (pool_[i].first == pool_[i - 1].first)
      throw std::runtime_error("SstSink: duplicate key across runs (pooled), key " +
                               rocksdb::Slice(pool_[i].first).ToString(true));
  open_files();
  for (auto& [k, v] : pool_) put_files(k, v);
  finish_files(pool_.front().first, pool_.back().first);
  pool_.clear();
  pool_.shrink_to_fit();
  pool_now_ = 0;
}

void SstSink::put_index(int index_id, std::string key) { index_keys_[index_id].push_back(std::move(key)); }

uint64_t SstSink::index_keys() const {
  uint64_t n = 0;
  for (auto& kv : index_keys_) n += kv.second.size();
  return n;
}

void SstSink::finish(std::vector<json>& manifests) {
  if (in_run_) throw std::runtime_error("SstSink: finish() inside a run (" + run_name_ + ")");
  // the index runs: one per index id (disjoint prefixes), sorted
  for (auto& [id, keys] : index_keys_) {
    std::sort(keys.begin(), keys.end());
    begin_run("index-" + std::to_string(id));
    for (auto& k : keys) put(k, "");
    end_run();
    keys.clear();
    keys.shrink_to_fit();
  }
  flush_pool();
  for (size_t i = 0; i < targets_.size(); i++) {
    Target& t = targets_[i];
    StoreOptions so;
    so.compression = t.spec.compression;
    so.index_filter_blocks = t.spec.index_filter_blocks;
    so.block_size = t.spec.block_size;
    so.block_cache_bytes = size_t(1) << 30;
    auto store = Store::open_read_write(t.spec.dir + "/db", so, true);
    // A NON-EMPTY target would silently MERGE with the new batches (RocksDB resolves
    // duplicate keys by seqno) -- a re-ingested dir becomes a
    // hybrid of both layouts with the manifest claiming only the new one. Refuse loudly;
    // the caller wipes deliberately.
    if (!store->live_files().empty())
      throw std::runtime_error("ingest target " + t.spec.dir + "/db is not empty (" +
                               std::to_string(store->live_files().size()) +
                               " live files) -- ingest never merges; delete the dir first");
    rocksdb::IngestExternalFileOptions io;
    io.move_files = true;
    // batches of files with pairwise-disjoint key ranges: sort files by first key, greedily
    // assign each to the first batch whose last key is below its first key
    std::vector<size_t> order(t.files.size());
    for (size_t k = 0; k < order.size(); k++) order[k] = k;
    std::sort(order.begin(), order.end(), [&](size_t a, size_t b) { return ranges_[a].first < ranges_[b].first; });
    std::vector<std::vector<std::string>> batches;
    std::vector<std::string> batch_last;
    for (size_t k : order) {
      size_t b = 0;
      while (b < batches.size() && !(batch_last[b] < ranges_[k].first)) b++;
      if (b == batches.size()) { batches.emplace_back(); batch_last.emplace_back(); }
      batches[b].push_back(t.files[k]);
      batch_last[b] = ranges_[k].second;
    }
    for (auto& files : batches) {
      rocksdb::Status st = store->db()->IngestExternalFile(files, io);
      if (!st.ok()) throw std::runtime_error("ingest " + t.spec.dir + ": " + st.ToString());
    }
    store->settle();
    // The free integrity invariant: every put reached the store, nothing was shadowed by a
    // duplicate key (the arango edge-doc collision class) and nothing pre-existing merged in.
    // settle() force-rewrites every file, so num_entries is populated; a zero falls back to
    // a full iterator count.
    uint64_t entries = 0;
    bool meta_ok = true;
    for (auto& f : store->live_files()) {
      if (f.num_entries == 0) { meta_ok = false; break; }
      entries += f.num_entries;
    }
    if (!meta_ok) {
      entries = 0;
      auto it = store->db()->NewIterator(rocksdb::ReadOptions());
      for (it->SeekToFirst(); it->Valid(); it->Next()) entries++;
      delete it;
    }
    if (entries != keys_)
      throw std::runtime_error("ingest " + t.spec.dir + ": store holds " + std::to_string(entries) +
                               " keys but the sink wrote " + std::to_string(keys_) +
                               " -- duplicate keys shadowed each other (or the dir was not empty)");
    json& m = manifests[i];
    m["sst_bytes"] = store->sst_bytes();
    m["sst_files"] = store->live_files().size();
    m["ingest_files"] = t.files.size();   // the files ingest wrote before settle (the sink's pool bounds it)
    m["settled"] = true;
    store->close();
  }
}

}  // namespace evalcpp
