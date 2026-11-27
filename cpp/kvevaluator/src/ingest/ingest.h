// Ingest: a neutral record stream (JSON lines from Python loader.stream()) -> a format's
// buffers -> sorted (key, value) pairs -> SstFileWriter files -> IngestExternalFile ->
// settle -> manifest.json. One producer pass can feed several compression targets.
#pragma once
#include <istream>
#include <string>
#include <vector>

#include "formats/format.h"
#include "schema/schema.h"
#include "store/rocksdb_store.h"
#include "value/value.h"

namespace evalcpp {

constexpr size_t kSstSmallRunBytes = size_t(4) << 20;   // the sink pools a run up to this (ingest/sst_sink.h)
constexpr size_t kSstPoolBytes = size_t(256) << 20;      // ... and writes the pool as one file per this (RAM estimate)


struct IngestTarget {
  std::string dir;
  std::string compression;   // store/rocksdb_store.h registry name
  std::string index_filter_blocks = rocksdb_setting<std::string>("index_filter_blocks");   // whole | partitioned
  size_t block_size = data_block_bytes();                            // StoreOptions::block_size
};

struct IngestOptions {
  std::string format;                     // required, no default: an empty format is refused by name (ingest.cc)
  json codec_config = json::object();
  std::vector<IngestTarget> targets;
  size_t pool_bytes = kSstPoolBytes;            // the sink's small-run pool, one file per this (ingest/sst_sink.h)
  size_t small_run_bytes = kSstSmallRunBytes;   // a run up to this is pooled
  std::string scale;                      // the dataset tag the store is built from ("0.1", "3", "30", "n1m"): the
                                          // manifest's `scale`, which selects a scale-tagged bundle variant (ir/plan.cc)
};

// One format of a multi-format pass: its own codec config and its own targets (dirs ×
// compressions); every format shares the single producer pass.
struct IngestFormat {
  std::string format;
  json codec_config = json::object();
  std::vector<IngestTarget> targets;
};

// Reads the whole stream (vertices first, then edges), builds every target, returns the
// manifests (one per target). The manifest is also written to <dir>/manifest.json.
// a record's vid: the canonical encoded form only (16 lowercase hex)
uint64_t parse_vid(const json& j);
// a record's props against the schema's declared kinds (the one gate every format's ingest and the tally read through)
const Value& check_kinds(const Value& props, const std::vector<PropDecl>& decls, const std::string& owner);

std::vector<json> ingest(std::istream& records, const Schema& schema, const std::string& schema_path,
                         const IngestOptions& opts);

// The native path: parse the LDBC CSVs in-process
// (ingest/ldbc_csv.h) -- no Python, no JSON, no pipe. The stream sha in the manifest is
// computed over the SAME canonical jsonl lines Python emits, so a store built either way
// records the same records.sha256 (the record-hash gate's currency).
std::vector<json> ingest_from_csv(const std::string& csv_root, const Schema& schema,
                                  const std::string& schema_path, const IngestOptions& opts);

// Multi-format single pass: ONE CSV parse drives every format's buffers.
// Returns all manifests, grouped per format in `formats` order (each format's targets in
// order). ~6x producer cost -> 1x for the multi-format store matrix.
std::vector<json> ingest_from_csv_multi(const std::string& csv_root, const Schema& schema,
                                        const std::string& schema_path, const std::vector<IngestFormat>& formats,
                                        size_t pool_bytes, const std::string& scale = "",
                                        size_t small_run_bytes = kSstSmallRunBytes);

// The manifest a run checks its request against (throws on a mismatch).
json read_manifest(const std::string& dir);
// the store options a store was built with, from its manifest: refuses a manifest without compression,
// index_filter_blocks or block_size (re-ingest the store)
StoreOptions store_options_of(const json& manifest, const std::string& dir);

// Every reader that decodes a store's values runs this before it reads (the session, census): the manifest's
// key_codec must be the binary's, and its value_codec must be what THIS binary's config writes for it (a store
// built before a knob or a codec stamp existed is refused). `who` names the caller in the message.
void check_store_codec(const json& manifest, const Format& fmt, const std::string& dir, const std::string& who);

}  // namespace evalcpp
