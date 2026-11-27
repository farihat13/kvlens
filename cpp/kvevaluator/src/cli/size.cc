// `size <store-dir>`: the physical-size/compression measurement of one settled store, as
// JSON -- the C++ half of the size studies (the modeled half runs on the python abstract
// oracle). Reports:
//   sst_bytes / n_sst / wal_bytes / log_bytes / n_log / other_bytes   the on-disk split (physize's
//                                                  phys_* rows; log = RocksDB's INFO logs, apart)
//   keys / key_bytes / value_bytes                the full logical scan's totals (absent under --repeats 0)
//   roles (--roles): keys / key_bytes / value_bytes per key ROLE {headline: {sub: [...]}}, from one extra
//         UNTIMED walk classifying every key with the format's key_role (formats/format.h) -- the logical
//         per-role size the size studies report (was the python modeled half); "other" = a classifier miss;
//         and "labels": the same per {vertex|edge|index, label} where the format's keys name one (key_label);
//   scan: statistics tickers moved by a COLD full-value scan (block cache emptied first;
//         page cache dropped for this store's files) -- read.block.get.micros,
//         number.block.decompressed, bytes.decompressed.* etc: gen_compress_cost's
//         decompression-tax measure, timed inside RocksDB's own C++.
// The store must be SETTLED (ingest guarantees it; refused otherwise) so the SST set is
// converged and comparable across codecs.
#include <rocksdb/table_properties.h>

#include <array>
#include <chrono>
#include <filesystem>
#include <iostream>
#include <map>

#include "cli/commands.h"
#include "formats/format.h"
#include "ingest/ingest.h"
#include "schema/schema_path.h"
#include "store/rocksdb_store.h"

namespace evalcpp {

int cmd_size(const std::vector<std::string>& args) {
  if (args.empty()) {
    std::cerr << "usage: kvevaluator size <store-dir> [--repeats N] [--evict] [--roles] [--schema PATH]\n"
                 "  --repeats N  cold-cache scans to run (default 1; the study medians them; 0 = none)\n"
                 "  --roles      add the per-key-role table (one extra untimed walk)\n"
                 "  --schema P   the schema artifact for --roles (default: the path the manifest records)\n"
                 "  --evict      also drop the OS page cache per scan (cold DISK; default off =\n"
                 "               warm file, the CPU-bound decompression-tax scenario)\n";
    return 2;
  }
  const std::string& dir = args[0];
  int repeats = 1;
  bool evict = false, roles_wanted = false;
  std::string schema_path;
  for (size_t i = 1; i < args.size(); i++) {
    if (args[i] == "--evict") evict = true;
    else if (args[i] == "--roles") roles_wanted = true;
    else if (args[i] == "--schema" && i + 1 < args.size()) schema_path = args[++i];
    else if (args[i] == "--repeats" && i + 1 < args.size()) repeats = std::stoi(args[++i]);
    else throw std::runtime_error("size: unknown arg " + args[i]);
  }
  json m = read_manifest(dir);
  if (!m.value("settled", false))
    throw std::runtime_error("store " + dir + " is not settled -- sizes would not be comparable");
  StoreOptions so = store_options_of(m, dir);
  auto store = Store::open_read_write(dir + "/db", so, false);

  // the on-disk split: SSTs, WALs, RocksDB's INFO logs (LOG, LOG.old.*: rotated on every open,
  // so their bytes count opens, not content -- reported apart, never inside other_bytes), and
  // the rest (MANIFEST, OPTIONS, CURRENT, IDENTITY: the store's own bookkeeping)
  uint64_t sst = 0, wal = 0, log = 0, other = 0;
  size_t n_sst = 0, n_log = 0;
  for (auto& e : std::filesystem::directory_iterator(dir + "/db")) {
    if (!e.is_regular_file()) continue;
    uint64_t sz = uint64_t(e.file_size());
    std::string name = e.path().filename().string();
    if (name.size() > 4 && name.substr(name.size() - 4) == ".sst") { sst += sz; n_sst++; }
    else if (name.size() > 4 && name.substr(name.size() - 4) == ".log") wal += sz;
    else if (name == "LOG" || name.rfind("LOG.old.", 0) == 0) { log += sz; n_log++; }
    else other += sz;
  }

  // COLD-cache full scans (histograms + tickers deltas per scan): reopen_cold empties the
  // block cache each repeat; --evict also drops the page cache (cold DISK) -- default off
  // is the warm-file CPU-bound case, the decompression-tax study's honest upper bound
  json scans = json::array();
  uint64_t keys = 0, kb = 0, vb = 0;
  for (int rep = 0; rep < repeats; rep++) {
    store->reopen_cold();
    if (evict) store->evict_page_cache();
    json t_before = store->tickers();
    json h_before = store->histograms();
    keys = kb = vb = 0;
    auto t0 = std::chrono::steady_clock::now();
    {
      std::unique_ptr<rocksdb::Iterator> it(store->db()->NewIterator(rocksdb::ReadOptions()));
      for (it->SeekToFirst(); it->Valid(); it->Next()) {
        keys++;
        kb += it->key().size();
        vb += it->value().size();
      }
    }
    uint64_t wall = uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now() - t0).count());
    scans.push_back({{"wall_ns", wall},
                     {"tickers", tickers_delta(t_before, store->tickers())},
                     {"histograms", histograms_delta(h_before, store->histograms())}});
  }
  // the per-role table: one warm, UNTIMED walk (never inside a timed scan above)
  json roles = json::object(), labels = json::object();
  if (roles_wanted) {
    Schema schema = load_manifest_schema(m, dir, "size --roles", schema_path);
    auto fmt = make_format(m.value("format", ""), schema, m.value("value_codec", json::object()));
    // key_role decodes keys: a store of another key layout or codec config would be tallied under the wrong roles
    check_store_codec(m, *fmt, dir, "size --roles");
    std::map<std::pair<std::string, std::string>, std::array<uint64_t, 3>> tally, by_label;
    std::unique_ptr<rocksdb::Iterator> it(store->db()->NewIterator(rocksdb::ReadOptions()));
    for (it->SeekToFirst(); it->Valid(); it->Next()) {
      std::string_view k(it->key().data(), it->key().size());
      auto role = fmt->key_role(k);
      for (auto* t : {&tally[role], &by_label[fmt->key_label(k)]}) {
        (*t)[0]++;
        (*t)[1] += it->key().size();
        (*t)[2] += it->value().size();
      }
    }
    for (auto& [role, t] : tally) roles[role.first][role.second] = {t[0], t[1], t[2]};
    for (auto& [bl, t] : by_label)
      if (!bl.first.empty()) labels[bl.first][bl.second] = {t[0], t[1], t[2]};
  }
  json out = {{"dir", dir},
              {"format", m.value("format", "")},
              {"compression", so.compression},
              {"sst_bytes", sst}, {"n_sst", n_sst},
              {"wal_bytes", wal}, {"log_bytes", log}, {"n_log", n_log}, {"other_bytes", other},
              {"evict", evict}, {"scans", scans}};
  if (repeats > 0) {   // the scan's totals; --repeats 0 runs no scan, so it reports none (never a fake 0)
    out["keys"] = keys;
    out["key_bytes"] = kb;
    out["value_bytes"] = vb;
  }
  if (roles_wanted) {
    out["roles"] = roles;     // {headline: {sub: [keys, key_bytes, value_bytes]}}
    out["labels"] = labels;   // {bucket: {label: [keys, key_bytes, value_bytes]}}; empty for a format without key_label
    out["key_roles_version"] = kKeyRolesVersion;
  }
  std::cout << out.dump() << "\n";
  return 0;
}

// `sst-props <store-dir>`: one JSON line per SST file with RocksDB's table properties -- data / index / filter
// block bytes, entries, data blocks, raw key / value bytes, the index partitions (a partitioned store) -- the
// per-file sizes a cold first touch reads. Read-only open; nothing is scanned.
int cmd_sst_props(const std::vector<std::string>& args) {
  if (args.size() != 1) {
    std::cerr << "usage: kvevaluator sst-props <store-dir>\n";
    return 2;
  }
  json m = read_manifest(args[0]);
  StoreOptions so = store_options_of(m, args[0]);
  so.block_cache_bytes = size_t(64) << 20;
  auto store = Store::open_read_only(args[0] + "/db", so);
  rocksdb::TablePropertiesCollection props;
  rocksdb::Status st = store->db()->GetPropertiesOfAllTables(&props);
  if (!st.ok()) {
    std::cerr << "sst-props: " << st.ToString() << "\n";
    return 1;
  }
  std::map<std::string, std::shared_ptr<const rocksdb::TableProperties>> sorted(props.begin(), props.end());
  for (auto& [file, tp] : sorted) {
    std::cout << json{{"file", std::filesystem::path(file).filename().string()},
                      {"file_bytes", std::filesystem::file_size(file)},
                      {"data_bytes", tp->data_size}, {"index_bytes", tp->index_size},
                      {"filter_bytes", tp->filter_size}, {"entries", tp->num_entries},
                      {"data_blocks", tp->num_data_blocks}, {"raw_key_bytes", tp->raw_key_size},
                      {"raw_value_bytes", tp->raw_value_size}, {"index_partitions", tp->index_partitions},
                      {"top_level_index_bytes", tp->top_level_index_size},
                      {"filter_policy", tp->filter_policy_name}}.dump() << "\n";
  }
  return 0;
}

}  // namespace evalcpp
