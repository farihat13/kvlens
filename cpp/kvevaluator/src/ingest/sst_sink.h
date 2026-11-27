// The streaming bulk-load sink: a run of keys goes into one SstFileWriter file per target
// (compression), index entries are buffered per index and sorted at the end, then every file is
// ingested, the store settled, and the manifest written. Runs may overlap in key range (dgraph
// flushes one predicate's postings per label, and two labels of one vid group interleave): files
// are ingested in batches whose ranges do not overlap (RocksDB refuses overlapping files in one
// call, not across calls) and the settle compaction merges them into one sorted level.
// SMALL runs are pooled: a run is buffered until it passes small_run_bytes (then it spills to its
// own file and streams); one that ends under it joins a pool, written as one sorted file per
// pool_bytes. A stream that changes run every few keys (arango's edge docs on LinkBench, whose two
// link types interleave) would otherwise make one file per run and pass the open-file limit at
// ingest. The settled store does not depend on the file split (settle rewrites every file).
#pragma once
#include <rocksdb/sst_file_writer.h>

#include <map>
#include <memory>
#include <string>
#include <vector>

#include "formats/format.h"
#include "ingest/ingest.h"
#include "store/rocksdb_store.h"

namespace evalcpp {

class SstSink : public KvSink {
 public:
  // ingest opens every file (max_open_files = -1) and the open-file limit is per PROCESS (a multi-format
  // pass holds several sinks): refuse a sink's file past this, as it is opened -- LDBC sf30 writes hundreds
  static constexpr size_t kMaxFiles = 10000;
  static constexpr size_t kPairOverhead = 64;   // RAM per buffered pair beyond its bytes (two strings + vector slot)
  SstSink(const std::vector<IngestTarget>& targets, size_t small_run_bytes = kSstSmallRunBytes,
          size_t pool_bytes = kSstPoolBytes, size_t max_files = kMaxFiles);
  void begin_run(const std::string& name) override;
  void put(std::string_view key, std::string_view value) override;
  void end_run() override;
  void put_index(int index_id, std::string key) override;
  // writes the index runs, ingests every file into every target, settles; fills per-target facts
  void finish(std::vector<json>& manifests);
  uint64_t keys() const { return keys_; }
  uint64_t logical_bytes() const { return bytes_; }
  uint64_t index_keys() const;
  size_t ingest_files() const { return targets_.empty() ? 0 : targets_[0].files.size(); }   // written so far

 private:
  struct Target {
    IngestTarget spec;
    rocksdb::Options options;
    std::shared_ptr<rocksdb::Cache> cache;
    std::unique_ptr<rocksdb::SstFileWriter> writer;
    std::vector<std::string> files;
  };
  void open_files();                                            // one new file per target
  void finish_files(const std::string& first, const std::string& last);
  void put_files(std::string_view key, std::string_view value);
  void flush_pool();
  size_t small_run_bytes_, pool_bytes_, max_files_;
  void spill();                                                 // the open run outgrew the cap: its own file
  std::vector<std::pair<std::string, std::string>> run_buf_;   // the open run, until it spills or ends
  size_t run_buf_bytes_ = 0;                                    // its RAM estimate (bytes + kPairOverhead per pair)
  bool streaming_ = false;                                      // the open run spilled to its own file
  std::vector<std::pair<std::string, std::string>> pool_;      // ended small runs, not yet written
  size_t pool_now_ = 0;
  std::vector<std::pair<std::string, std::string>> ranges_;   // per file: (first key, last key)
  std::string first_key_;
  std::vector<Target> targets_;
  std::map<int, std::vector<std::string>> index_keys_;
  std::string last_key_;
  bool in_run_ = false;
  std::string run_name_;
  uint64_t keys_ = 0, bytes_ = 0;
};

}  // namespace evalcpp
