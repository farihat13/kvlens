// `ingest --schema schema.json --format pertype_csr --out <dir-prefix> --compression snappy,zstd
//         [--chunk 65536] [--spec spec.json]
//         (--csv-root <dir> [--multi fmt=prefix[@chunk],...] [--ram-budget-gb N] | < records.jsonl)`
// Builds <dir-prefix>-<compression> for every compression from ONE pass over the records; the decided one
// (store_config.json's rocksdb.compression, the default) builds <dir-prefix> itself.
// A --multi item's own @chunk (else the shared --chunk, else the format's default) rides with
// the format, as its store name does: dgraph@∞ and pertype_csr@64K share one pass.
#include <fstream>
#include <filesystem>
#include <iostream>

#include "cli/commands.h"
#include "ingest/ingest.h"

namespace evalcpp {

int cmd_ingest(const std::vector<std::string>& argv) {
  // --print-config: parse the flags exactly as an ingest would, print the format's canonical
  // codec_config (what the manifest's value_codec would record) and stop -- no records read, no
  // store written. The session's open guard (session.cc: the manifest must round-trip) is the same rule.
  std::vector<std::string> args;
  bool print_config = false;
  for (auto& a : argv) (a == "--print-config" ? print_config = true : (args.push_back(a), false));
  const std::string decided_compression = rocksdb_setting<std::string>("compression");   // a name with no codec suffix
  std::string schema_path, format, out, compressions = decided_compression;   // format: required (no default)
  std::string uids = "delta_varint", strings = "raw_offsets", layout = "plain", spec_path, csv_root, multi, scale;
  std::string index_layout;   // empty = the format's default
  std::string key_order;      // nebula family: vid_first | type_first (empty = the format's default, vid_first)
  std::string far_label;      // pertype_csr: on | off, the far-end label per adjacency entry (empty = the format's default, on)
  std::string props_codec;    // dgraph / arangodb: named | schema (empty = the default)
  std::string index_filter_blocks = rocksdb_setting<std::string>("index_filter_blocks");   // the SSTs' index + filter blocks: whole | partitioned (StoreOptions)
  size_t block_size = data_block_bytes();                          // the SSTs' data block size (StoreOptions)
  double ram_budget_gb = 0;                      // 0 = no budget: every format in one pass
  size_t sst_pool_bytes = kSstPoolBytes, sst_small_run_bytes = kSstSmallRunBytes;   // the sink's pool
  uint64_t chunk = 0;                          // 0 = not given: each format's own default
  for (size_t i = 0; i + 1 < args.size(); i += 2) {
    const std::string& k = args[i];
    const std::string& v = args[i + 1];
    if (k == "--schema") schema_path = v;
    else if (k == "--format") format = v;
    else if (k == "--out") out = v;
    else if (k == "--compression") compressions = v;
    else if (k == "--chunk") chunk = std::stoull(v);   // chunked formats: the part/chunk target (their default 64K); dgraph: opt-in (default ∞)
    else if (k == "--uids") uids = v;
    else if (k == "--strings") strings = v;
    else if (k == "--layout") layout = v;
    else if (k == "--index-layout") index_layout = v;   // per-type formats: posting (default) | sorted_keys
    else if (k == "--key-order") key_order = v;         // nebula / nebula_engine: vid_first | type_first
    else if (k == "--far-label") far_label = v;         // pertype_csr: on | off (the stored far-end label)
    else if (k == "--index-filter-blocks") index_filter_blocks = v;   // partitioned (default) | whole
    else if (k == "--block-size") block_size = std::stoull(v);   // data block bytes (default 4096; a control or an arm)
    else if (k == "--props-codec") props_codec = v;     // dgraph / arangodb: named | schema (codec/props_codec.h)
    else if (k == "--spec") spec_path = v;       // meta: the format spec JSON
    else if (k == "--csv-root") csv_root = v;    // native producer: LDBC CSVs, no records pipe
    else if (k == "--scale") scale = v;          // the dataset tag ("0.1", "3", "30", "n1m") -> the manifest's `scale`
    else if (k == "--multi") multi = v;          // fmt=prefix[,fmt=prefix...]: one pass, many formats
    else if (k == "--ram-budget-gb") ram_budget_gb = std::stod(v);   // bound the multi-format SUM
    else if (k == "--sst-pool-bytes") sst_pool_bytes = std::stoull(v);         // the sink's small-run pool, per file
    else if (k == "--sst-small-run-bytes") sst_small_run_bytes = std::stoull(v);   // a run under this is pooled
    else { std::cerr << "ingest: unknown option " << k << "\n"; return 2; }
  }
  if (schema_path.empty() || (out.empty() && !print_config)) {
    std::cerr << "usage: kvevaluator ingest --schema schema.json --out <dir-prefix> (--format <name> | --multi ...)"
                 " [--compression snappy,zstd] [--chunk N] [--uids delta_varint|for_bitpack]"
                 " [--strings raw_offsets|zstd_column] [--layout plain] [--index-layout posting|sorted_keys]"
                 " [--props-codec named|schema] [--index-filter-blocks partitioned|whole] [--block-size N]"
                 " [--key-order vid_first|type_first] [--far-label on|off]"
                 " [--spec spec.json] [--scale <tag>] [--sst-pool-bytes N] [--sst-small-run-bytes N] [--print-config]"
                 " (--csv-root <dir> [--multi fmt=prefix[@chunk],...] [--ram-budget-gb N] | < records.jsonl)\n";
    return 2;
  }
  if (format.empty() && multi.empty()) {
    std::cerr << "ingest: --format is required (there is no default format)\n";
    return 2;
  }
  Schema schema = Schema::load(schema_path);
  IngestOptions o;
  o.format = format;
  o.scale = scale;
  o.pool_bytes = sst_pool_bytes;
  o.small_run_bytes = sst_small_run_bytes;
  // chunk_target only when asked: a format's Config supplies its own default otherwise (the
  // per-type formats 64K, dgraph ∞ -- a blanket 64K here would chunk dgraph silently)
  o.codec_config = {{"uids", uids}, {"strings", strings}, {"layout", layout}};
  if (chunk) o.codec_config["chunk_target"] = chunk;
  if (!index_layout.empty()) o.codec_config["index_layout"] = index_layout;
  if (!key_order.empty()) o.codec_config["key_order"] = key_order;
  if (!far_label.empty()) {
    if (far_label != "on" && far_label != "off") throw std::runtime_error("--far-label must be on or off, got '" + far_label + "'");
    o.codec_config["far_label"] = far_label == "on";
  }
  // the edge-prop codec: omitted = the format's default (schema, the fair setting). --multi shares ONE
  // codec_config across the pass, so one pass builds every format under the same props_codec.
  if (!props_codec.empty()) o.codec_config["props_codec"] = props_codec;
  if (format == "meta") {                         // meta's config IS its spec JSON
    if (!spec_path.empty()) {                     // meta: --spec is the format spec (there are no presets);
      std::ifstream sf(spec_path);                //       omit it for meta's default dials
      if (!sf) {
        std::cerr << "ingest: cannot read --spec " << spec_path << "\n";
        return 2;
      }
      o.codec_config = json::parse(sf);
    }
    if (!props_codec.empty()) {                    // meta has one property encoding (columns)
      std::cerr << "ingest: --props-codec does not apply to meta: every property is the columns encoding\n";
      return 2;
    }
  } else if (!spec_path.empty()) {
    std::cerr << "ingest: --spec is only for --format meta\n";
    return 2;
  }
  if (print_config) {
    std::cout << make_format(o.format, schema, o.codec_config)->codec_config().dump() << "\n";
    return 0;
  }
  size_t p = 0;
  while (p <= compressions.size()) {
    size_t q = compressions.find(',', p);
    std::string c = compressions.substr(p, q == std::string::npos ? std::string::npos : q - p);
    if (!c.empty()) o.targets.push_back({out + (c == decided_compression ? "" : "-" + c), c, index_filter_blocks, block_size});
    if (q == std::string::npos) break;
    p = q + 1;
  }
  std::vector<json> ms;
  if (!multi.empty()) {                          // --multi fmt=prefix,...: section-4 one-pass matrix
    if (csv_root.empty()) { std::cerr << "ingest: --multi needs --csv-root\n"; return 2; }
    std::vector<IngestFormat> fmts;
    size_t p2 = 0;
    while (p2 <= multi.size()) {
      size_t q = multi.find(',', p2);
      std::string item = multi.substr(p2, q == std::string::npos ? std::string::npos : q - p2);
      if (!item.empty()) {
        size_t eq = item.find('=');
        if (eq == std::string::npos) { std::cerr << "ingest: --multi item '" << item << "' is not fmt=prefix\n"; return 2; }
        IngestFormat f;
        f.format = item.substr(0, eq);
        f.codec_config = o.codec_config;          // the shared knobs; per-format specs not supported here
        std::string prefix = item.substr(eq + 1);
        size_t at = prefix.rfind('@');            // fmt=prefix@chunk: this format's own chunk target
        if (at != std::string::npos) {
          f.codec_config["chunk_target"] = std::stoull(prefix.substr(at + 1));
          prefix = prefix.substr(0, at);
        }
        for (auto& t : o.targets) {               // same compression list as the single path
          std::string suffix = t.dir.substr(out.size());
          f.targets.push_back({prefix + suffix, t.compression, t.index_filter_blocks, t.block_size});
        }
        fmts.push_back(std::move(f));
      }
      if (q == std::string::npos) break;
      p2 = q + 1;
    }
    // --far-label rides in the pass's SHARED knobs: say which formats of this pass have the setting, so a pass
    // meant to switch one format does not switch another unseen
    if (!far_label.empty()) {
      std::string has, hasnt;
      for (auto& f : fmts)
        (make_format(f.format, schema, f.codec_config)->codec_config().contains("far_label") ? has : hasnt) += " " + f.format;
      std::cerr << "ingest: --far-label " << far_label << " applies to:" << (has.empty() ? " (none)" : has)
                << "; without the setting:" << (hasnt.empty() ? " (none)" : hasnt) << "\n";
    }
    // RAM budget (runs within a configurable budget on any machine).
    // Lever (b): bound the multi-format SUM by packing formats into batches whose estimated
    // peaks fit the budget, re-parsing the CSVs per batch (the C++ parse is cheap). Batching
    // groups FORMATS only -- each format still sees every record in one pass -- so the output
    // is byte-identical to the unbudgeted run (gated by store-sha equality in the tests).
    // Estimates: peak buffer RSS per GB of input CSV, calibrated per format at sf3
    // (a calibration run; scales ~linearly -- buffers hold the flat dataset).
    // Lever (a), a SINGLE format's peak, is NOT bounded here: the edge CSVs are not
    // src-sorted, so sub-format bounding means external-sort spills (recorded, not built);
    // the floor is the largest single format's peak.
    if (ram_budget_gb > 0) {
      static const std::map<std::string, double> kPeakPerCsvGb = {
          // peak_rss_gb / input_csv_gb, where input = the dynamic/ + static/ CSV bytes THIS
          // walk sums (2.31 GB at sf3 -- NOT the 3.2 GB tree; updateStream files sit in the
          // root and are excluded). Measured per format at sf3 (single-format ingest under
          // /usr/bin/time -v): pertype 3.55 GB, nebula 2.01,
          // dgraph 2.33, arango 5.02. Rounded UP (the flag exists to avoid OOM).
          {"pertype_csr", 1.6}, {"nebula", 0.9}, {"dgraph", 1.1}, {"arangodb", 2.2}};
      // The table keys by FORMAT NAME; a physical DESIGN can add to a format's peak. dgraph
      // under a key-sorted date index (the full_index design) puts one key-only entry per dated
      // vertex through SstSink::put_index, which BUFFERS every index key until finish() (nebula's
      // SortedIndex rides the same lane; its coefficient above already includes it). Measured at
      // sf3 (single-format ingest under /usr/bin/time -v): canonical dgraph peak
      // 2.33 GB (== the table's), dgraph-full_index 2.95 GB -> +0.62 GB over 2.31 CSV GB = 0.27,
      // rounded UP; charged once per format whose store keys its dates that way.
      const double kKeySortedPerCsvGb = 0.3;
      size_t n_key_sorted = 0;
      for (auto& ix : schema.indexes()) n_key_sorted += ix.key_sorted ? 1 : 0;
      auto key_sorts = [&](const std::string& fmt) {   // the formats whose index honors the flag
        return n_key_sorted > 0 && fmt == "dgraph";
      };
      double csv_gb = 0;
      for (const char* sub : {"/dynamic", "/static"}) {
        std::string d = csv_root + sub;
        // sum the CSV bytes (the estimator's input-size term)
        for (auto& e : std::filesystem::directory_iterator(d))
          if (e.is_regular_file() && e.path().filename().string()[0] != '.')   // skip .crc shadows
            csv_gb += double(e.file_size()) / 1e9;
      }
      std::vector<std::vector<IngestFormat>> batches;
      double cur = 0;
      for (auto& f : fmts) {
        auto it = kPeakPerCsvGb.find(f.format);
        double est = (it == kPeakPerCsvGb.end() ? 1.6 : it->second) * csv_gb;   // unknown: assume the heaviest
        if (key_sorts(f.format)) {
          est += kKeySortedPerCsvGb * csv_gb;
          std::cerr << "[ingest] " << f.format << ": +" << kKeySortedPerCsvGb * csv_gb
                    << " GB for the key-sorted index buffer (" << n_key_sorted << " key-sorted index(es) in the schema)\n";
        }
        if (est > ram_budget_gb)
          std::cerr << "[ingest] WARNING: " << f.format << " alone is estimated at " << est
                    << " GB, over the " << ram_budget_gb << " GB budget -- the single-format "
                    << "floor (sub-format bounding needs external-sort spills, not built)\n";
        if (batches.empty() || cur + est > ram_budget_gb) { batches.push_back({}); cur = 0; }
        batches.back().push_back(f);
        cur += est;
      }
      if (batches.size() > 1)
        std::cerr << "[ingest] ram budget " << ram_budget_gb << " GB (csv " << csv_gb
                  << " GB): " << fmts.size() << " formats in " << batches.size()
                  << " batches (re-parsing per batch)\n";
      for (auto& b : batches) {
        std::vector<json> part = ingest_from_csv_multi(csv_root, schema, schema_path, b, o.pool_bytes, o.scale,
                                                                o.small_run_bytes);
        for (auto& m : part) ms.push_back(std::move(m));
      }
    } else {
      ms = ingest_from_csv_multi(csv_root, schema, schema_path, fmts, o.pool_bytes, o.scale, o.small_run_bytes);
    }
  } else {
    ms = csv_root.empty() ? ingest(std::cin, schema, schema_path, o)
                          : ingest_from_csv(csv_root, schema, schema_path, o);
  }
  for (auto& m : ms) std::cout << m.dump() << "\n";
  return 0;
}

}  // namespace evalcpp
