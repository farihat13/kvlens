// A plain-struct copy of this thread's RocksDB PerfContext + IOStatsContext: cheap to take
// per task and to sum per step (ToString + parsing is what costs microseconds). Every field
// of both structs is listed, so nothing the engine measures is dropped (capture-all).
#pragma once
#include <atomic>
#include <cstdint>

#include "value/value.h"

namespace evalcpp {

#define EVALCPP_PERF_FIELDS(X) \
  X(user_key_comparison_count) X(block_cache_hit_count) X(block_read_count) X(block_read_byte) \
  X(block_read_time) X(block_cache_index_hit_count) X(index_block_read_count) \
  X(block_cache_filter_hit_count) X(filter_block_read_count) X(compression_dict_block_read_count) \
  X(secondary_cache_hit_count) X(block_checksum_time) X(block_decompress_time) X(get_read_bytes) \
  X(multiget_read_bytes) X(iter_read_bytes) X(blob_cache_hit_count) X(blob_read_count) \
  X(blob_read_byte) X(blob_read_time) X(blob_checksum_time) X(blob_decompress_time) \
  X(internal_key_skipped_count) X(internal_delete_skipped_count) X(internal_recent_skipped_count) \
  X(internal_merge_count) X(get_snapshot_time) X(get_from_memtable_time) X(get_from_memtable_count) \
  X(get_post_process_time) X(get_from_output_files_time) X(seek_on_memtable_time) \
  X(seek_on_memtable_count) X(next_on_memtable_count) X(prev_on_memtable_count) \
  X(seek_child_seek_time) X(seek_child_seek_count) X(seek_min_heap_time) X(seek_max_heap_time) \
  X(seek_internal_seek_time) X(find_next_user_entry_time) X(write_wal_time) X(write_memtable_time) \
  X(write_delay_time) X(write_scheduling_flushes_compactions_time) X(write_pre_and_post_process_time) \
  X(write_thread_wait_nanos) X(db_mutex_lock_nanos) X(db_condition_wait_nanos) \
  X(merge_operator_time_nanos) X(read_index_block_nanos) X(read_filter_block_nanos) \
  X(new_table_block_iter_nanos) X(new_table_iterator_nanos) X(block_seek_nanos) X(find_table_nanos) \
  X(bloom_memtable_hit_count) X(bloom_memtable_miss_count) X(bloom_sst_hit_count) \
  X(bloom_sst_miss_count) X(key_lock_wait_time) X(key_lock_wait_count) \
  X(env_new_sequential_file_nanos) X(env_new_random_access_file_nanos) X(env_new_writable_file_nanos) \
  X(env_reuse_writable_file_nanos) X(env_new_random_rw_file_nanos) X(env_new_directory_nanos) \
  X(env_file_exists_nanos) X(env_get_children_nanos) X(env_get_children_file_attributes_nanos) \
  X(env_delete_file_nanos) X(env_create_dir_nanos) X(env_create_dir_if_missing_nanos) \
  X(env_delete_dir_nanos) X(env_get_file_size_nanos) X(env_get_file_modification_time_nanos) \
  X(env_rename_file_nanos) X(env_link_file_nanos) X(env_lock_file_nanos) X(env_unlock_file_nanos) \
  X(env_new_logger_nanos) X(get_cpu_nanos) X(iter_next_cpu_nanos) X(iter_prev_cpu_nanos) \
  X(iter_seek_cpu_nanos) X(encrypt_data_nanos) X(decrypt_data_nanos) X(number_async_seek)

#define EVALCPP_IOSTATS_FIELDS(X) \
  X(bytes_written) X(bytes_read) X(open_nanos) X(allocate_nanos) X(write_nanos) X(read_nanos) \
  X(range_sync_nanos) X(fsync_nanos) X(prepare_write_nanos) X(logger_nanos) X(cpu_write_nanos) X(cpu_read_nanos)

// The API-boundary timer (run.kv_timing, default off): every RocksDB call a measured read or write makes, timed on the
// steady clock around the call -- Get, NewIterator + Seek / SeekForPrev, Next / Prev, the iterator's delete, Put,
// Delete (store/counting.cc). Per kind: ns and calls; kv_call_ns = their sum. Off, every field is 0 and so absent
// from the step's kv block.
#define EVALCPP_KVCALL_FIELDS(X) \
  X(kv_call_ns) X(kv_get_ns) X(kv_get_calls) X(kv_seek_ns) X(kv_seek_calls) X(kv_next_ns) X(kv_next_calls) \
  X(kv_iter_close_ns) X(kv_iter_close_calls) X(kv_put_ns) X(kv_put_calls) X(kv_delete_ns) X(kv_delete_calls)

struct KvCallTimes {
#define EVALCPP_DECL_KV(f) uint64_t f = 0;
  EVALCPP_KVCALL_FIELDS(EVALCPP_DECL_KV)
#undef EVALCPP_DECL_KV
};
KvCallTimes& kv_call_times();            // this thread's running totals (zeroed by perf_reset, read by current())
std::atomic<bool>& kv_timing_on();        // process-wide: execute sets it for its run, every worker reads it
uint64_t kv_timer_pair_ns();              // the cost of one timer pair on this machine (measured once per process)

struct PerfSnapshot {
#define EVALCPP_DECL(f) uint64_t f = 0;
  EVALCPP_PERF_FIELDS(EVALCPP_DECL)
#define EVALCPP_DECL_IO(f) uint64_t io_##f = 0;
  EVALCPP_IOSTATS_FIELDS(EVALCPP_DECL_IO)
  EVALCPP_KVCALL_FIELDS(EVALCPP_DECL)
#undef EVALCPP_DECL
#undef EVALCPP_DECL_IO
  static PerfSnapshot current();          // this thread's contexts, as they stand
  void add(const PerfSnapshot& o);
  json to_json() const;                   // non-zero fields only; IOStats fields as io_<name>
};

}  // namespace evalcpp
