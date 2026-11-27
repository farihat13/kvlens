#include "ingest/ingest.h"

#include <chrono>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <thread>
#include <fstream>
#include <iostream>
#include <stdexcept>

#include "ingest/ldbc_csv.h"
#include "ingest/sst_sink.h"
#include "provenance.h"
#include "util/sha256.h"
#include "util/uidset.h"
#include "schema/schema_path.h"
#include "util/vid.h"

namespace evalcpp {

uint64_t parse_vid(const json& j) {   // the canonical form only: another spelling would read back as a different string
  uint64_t u;
  if (!canonical_vid(j.get<std::string>(), &u))
    throw std::runtime_error("record: '" + j.get<std::string>() + "' is not an encoded vid (16 lowercase hex)");
  return u;
}

// A record's props against the schema's declared kinds: a `bytes` prop holds a bytes value, no other prop does. Every
// format reads its props through this one gate, so a record written before the bytes type (a blob as a latin1 str)
// fails here for every format, not only for those whose column codec checks the kind.
const Value& check_kinds(const Value& props, const std::vector<PropDecl>& decls, const std::string& owner) {
  if (!props.is_row()) return props;
  for (const PropDecl& d : decls) {
    Value v = props.get(d.name);
    if (v.is_null()) continue;
    if ((d.type == "bytes") != v.is_bytes())
      throw std::runtime_error("record: " + owner + "." + d.name + " is declared " + d.type + " but holds a " +
                               v.type_name() + " (an input written before the bytes type?)");
  }
  return props;
}

json read_manifest(const std::string& dir) {
  std::ifstream f(dir + "/manifest.json");
  if (!f) throw std::runtime_error("no manifest.json in " + dir + " (not a kvevaluator store, or its ingest never finished)");
  return json::parse(f);
}

StoreOptions store_options_of(const json& m, const std::string& dir) {
  for (const char* k : {"compression", "index_filter_blocks", "block_size"})
    if (!m.contains(k)) throw std::runtime_error("store " + dir + ": its manifest has no " + k + " -- re-ingest the store");
  StoreOptions so;
  so.compression = m.at("compression").get<std::string>();
  so.index_filter_blocks = m.at("index_filter_blocks").get<std::string>();
  so.block_size = m.at("block_size").get<size_t>();
  so.build_settings_known = m.contains("bloom_bits_per_key") && m.contains("partition_bytes");
  if (so.build_settings_known) {
    so.bloom_bits_per_key = m.at("bloom_bits_per_key").get<double>();
    so.partition_bytes = m.at("partition_bytes").get<size_t>();
  }
  return so;
}

void check_store_codec(const json& m, const Format& fmt, const std::string& dir, const std::string& who) {
  if (m.value("key_codec", "") != fmt.key_codec())
    throw std::runtime_error(who + ": store " + dir + " was ingested with key layout '" + m.value("key_codec", "") +
                             "', this binary reads '" + fmt.key_codec() + "' -- re-ingest the store");
  // config round trip: a store built before a knob existed, or under a default since flipped (props_codec named ->
  // schema; meta's chunk_header), would otherwise be read under the new meaning with correct-looking counts.
  // Compared key-order-free.
  const json have = m.value("value_codec", json::object());
  const json want = fmt.codec_config();
  if (json::parse(have.dump()) != json::parse(want.dump()))
    throw std::runtime_error(who + ": store " + dir + " was ingested with codec config " + have.dump() +
                             ", this binary reads it as " + want.dump() +
                             " (built before a knob existed or under another default) -- re-ingest the store");
}

namespace {
// Shared per-producer state: one struct so the jsonl path and the native CSV path finish
// through the same tail (finalize -> SST -> manifests).
// IngestOptions::format has no default: a caller that forgets it is refused by name, never given a format
const std::string& required_format(const IngestOptions& opts) {
  if (opts.format.empty()) throw std::runtime_error("ingest: IngestOptions.format is required (there is no default format)");
  return opts.format;
}

struct IngestRun {
  std::unique_ptr<Format> fmt;
  SstSink sink;
  Sha256 hash;
  uint64_t n_v = 0, n_e = 0, n_lines = 0;
  std::map<std::string, uint64_t> counts;
  IngestRun(const Schema& schema, const IngestOptions& opts)
      : fmt(make_format(required_format(opts), schema, opts.codec_config)),
        sink(opts.targets, opts.small_run_bytes, opts.pool_bytes) {}
};

std::vector<json> finish_run(IngestRun& run, const Schema& schema, const std::string& schema_path,
                             const IngestOptions& opts, std::chrono::steady_clock::time_point t0,
                             std::chrono::steady_clock::time_point t1);
}  // namespace

std::vector<json> ingest(std::istream& records, const Schema& schema, const std::string& schema_path,
                         const IngestOptions& opts) {
  auto t0 = std::chrono::steady_clock::now();
  IngestRun run(schema, opts);
  Format* fmt = run.fmt.get();
  SstSink& sink = run.sink;
  std::string line;
  while (std::getline(records, line)) {
    if (line.empty()) continue;
    run.hash.update(line);
    run.hash.update("\n", 1);
    run.n_lines++;
    json r = json::parse(line);
    const std::string kind = r.at(0).get<std::string>();
    if (kind == "v") {
      // ["v", vid, label, props]
      std::string label = r.at(2).get<std::string>();
      fmt->add_vertex(sink, parse_vid(r.at(1)), label, check_kinds(from_json(r.at(3)), schema.vertex(label).props, label));
      run.counts[label]++;
      run.n_v++;
    } else if (kind == "e") {
      // ["e", etype, src, dst, props, src_label, dst_label]
      std::string etype = r.at(1).get<std::string>();
      fmt->add_edge(sink, etype, parse_vid(r.at(2)), parse_vid(r.at(3)),
                    check_kinds(from_json(r.at(4)), schema.edge_props(etype), etype),
                    r.at(5).get<std::string>(), r.at(6).get<std::string>());
      run.counts[etype]++;
      run.n_e++;
    } else {
      throw std::runtime_error("record line " + std::to_string(run.n_lines) + ": unknown kind '" + kind + "'");
    }
    if (run.n_lines % 5000000 == 0) std::cerr << "[ingest] " << run.n_lines << " records\n";
  }
  return finish_run(run, schema, schema_path, opts, t0, std::chrono::steady_clock::now());
}

std::vector<json> ingest_from_csv(const std::string& csv_root, const Schema& schema,
                                  const std::string& schema_path, const IngestOptions& opts) {
  return ingest_from_csv_multi(csv_root, schema, schema_path,
                               {{opts.format, opts.codec_config, opts.targets}}, opts.pool_bytes, opts.scale,
                               opts.small_run_bytes);
}

namespace {
// per-format encoder threads. The parser publishes shared read-only record
// BATCHES; each format's thread consumes them IN ORDER (formats assume the stream order --
// label-contiguous vertices, vertices before edges -- so parallelism is across FORMATS,
// never across records of one format). Records share their props Value (shared_ptr to
// immutable data: safe for concurrent readers); the label/etype pointers are into the
// static file tables. Bounded queues give backpressure so in-flight batches stay small.
struct RecordBatch {
  std::vector<ldbc_csv::Record> recs;
};
using BatchPtr = std::shared_ptr<const RecordBatch>;

class BatchQueue {
 public:
  void push(BatchPtr b) {                      // nullptr = end of stream
    std::unique_lock<std::mutex> lk(m_);
    cv_full_.wait(lk, [&] { return q_.size() < kCap; });
    q_.push_back(std::move(b));
    cv_empty_.notify_one();
  }
  BatchPtr pop() {
    std::unique_lock<std::mutex> lk(m_);
    cv_empty_.wait(lk, [&] { return !q_.empty(); });
    BatchPtr b = std::move(q_.front());
    q_.pop_front();
    cv_full_.notify_one();
    return b;
  }

 private:
  static constexpr size_t kCap = 8;
  std::mutex m_;
  std::condition_variable cv_full_, cv_empty_;
  std::deque<BatchPtr> q_;
};
}  // namespace

std::vector<json> ingest_from_csv_multi(const std::string& csv_root, const Schema& schema,
                                        const std::string& schema_path, const std::vector<IngestFormat>& formats,
                                        size_t pool_bytes, const std::string& scale, size_t small_run_bytes) {
  auto t0 = std::chrono::steady_clock::now();
  std::vector<std::unique_ptr<IngestRun>> runs;
  std::vector<IngestOptions> per_opts;
  for (auto& f : formats) {
    IngestOptions o;
    o.format = f.format;
    o.codec_config = f.codec_config;
    o.targets = f.targets;
    o.pool_bytes = pool_bytes;
    o.small_run_bytes = small_run_bytes;
    o.scale = scale;
    per_opts.push_back(o);
  }
  for (auto& o : per_opts) runs.push_back(std::make_unique<IngestRun>(schema, o));

  const size_t kBatch = 8192;
  std::vector<BatchQueue> queues(runs.size());
  std::vector<std::string> errors(runs.size());
  std::vector<std::thread> encoders;
  for (size_t i = 0; i < runs.size(); i++) {
    encoders.emplace_back([&, i] {
      Format* fmt = runs[i]->fmt.get();
      SstSink& sink = runs[i]->sink;
      try {
        while (BatchPtr b = queues[i].pop()) {
          for (auto& r : b->recs) {
            if (r.is_vertex) fmt->add_vertex(sink, r.uid, *r.label, r.props);
            else fmt->add_edge(sink, *r.etype, r.src, r.dst, r.props, *r.src_label, *r.dst_label);
          }
        }
      } catch (const std::exception& e) {
        errors[i] = e.what();                  // drain to the sentinel so the parser never blocks
        while (queues[i].pop()) {}
      }
    });
  }

  // the parser thread (this one): parse, hash the canonical line, publish shared batches
  Sha256 hash;
  uint64_t n_v = 0, n_e = 0, n_lines = 0;
  std::map<std::string, uint64_t> counts;
  std::string line;
  auto batch = std::make_shared<RecordBatch>();
  batch->recs.reserve(kBatch);
  auto flush = [&] {
    if (batch->recs.empty()) return;
    BatchPtr ready = std::move(batch);
    for (auto& q : queues) q.push(ready);
    batch = std::make_shared<RecordBatch>();
    batch->recs.reserve(kBatch);
  };
  // a parser error must reach main's catch with its message -- unwinding past joinable
  // threads is std::terminate: trap, shut the pipeline down, rethrow
  std::string parse_error;
  try {
    ldbc_csv::stream(csv_root, schema, [&](const ldbc_csv::Record& r) {
      ldbc_csv::jsonl_line(r, line);           // the canonical line: hashed once, never piped
      hash.update(line);
      hash.update("\n", 1);
      n_lines++;
      if (r.is_vertex) { counts[*r.label]++; n_v++; }
      else { counts[*r.etype]++; n_e++; }
      batch->recs.push_back(r);
      if (batch->recs.size() >= kBatch) flush();
      if (n_lines % 5000000 == 0) std::cerr << "[ingest] " << n_lines << " records\n";
    });
    flush();
  } catch (const std::exception& e) {
    parse_error = e.what();
  }
  for (auto& q : queues) q.push(nullptr);
  for (auto& t : encoders) t.join();
  if (!parse_error.empty()) throw std::runtime_error(parse_error);
  for (size_t i = 0; i < runs.size(); i++)
    if (!errors[i].empty())
      throw std::runtime_error("ingest[" + per_opts[i].format + "]: " + errors[i]);
  auto t1 = std::chrono::steady_clock::now();

  // finalize every format in parallel (independent buffers, independent target dirs)
  std::vector<std::vector<json>> per_format(runs.size());
  std::vector<std::thread> finishers;
  for (size_t i = 0; i < runs.size(); i++) {
    finishers.emplace_back([&, i] {
      try {
        runs[i]->hash = hash;                  // the one shared stream identity
        runs[i]->n_v = n_v;
        runs[i]->n_e = n_e;
        runs[i]->n_lines = n_lines;
        runs[i]->counts = counts;
        per_format[i] = finish_run(*runs[i], schema, schema_path, per_opts[i], t0, t1);
        runs[i].reset();                       // free this format's buffers as soon as it lands
      } catch (const std::exception& e) {
        errors[i] = e.what();
      }
    });
  }
  for (auto& t : finishers) t.join();
  for (size_t i = 0; i < runs.size(); i++)
    if (!errors[i].empty())
      throw std::runtime_error("finalize[" + per_opts[i].format + "]: " + errors[i]);
  std::vector<json> all;
  for (auto& ms : per_format)
    for (auto& m : ms) all.push_back(std::move(m));
  return all;
}

namespace {
std::vector<json> finish_run(IngestRun& run, const Schema& schema, const std::string& schema_path,
                             const IngestOptions& opts, std::chrono::steady_clock::time_point t0,
                             std::chrono::steady_clock::time_point t1) {
  Format* fmt = run.fmt.get();
  SstSink& sink = run.sink;
  uint64_t n_v = run.n_v, n_e = run.n_e, n_lines = run.n_lines;
  std::map<std::string, uint64_t>& counts = run.counts;
  std::string stream_sha = run.hash.hexdigest();
  fmt->finalize(sink);
  auto t2 = std::chrono::steady_clock::now();
  std::cerr << "[ingest] " << n_v << " vertices, " << n_e << " edges -> " << sink.keys() << " chunk keys + "
            << sink.index_keys() << " index keys; stream "
            << std::chrono::duration_cast<std::chrono::seconds>(t1 - t0).count() << "s, finalize "
            << std::chrono::duration_cast<std::chrono::seconds>(t2 - t1).count() << "s\n";
  std::vector<json> manifests;
  for (auto& tgt : opts.targets) {
    json m;
    m["format"] = opts.format;
    m["key_codec"] = fmt->key_codec();
    m["value_codec"] = fmt->codec_config();
    m["value_codec_hash"] = sha256_hex(fmt->codec_config().dump()).substr(0, 16);
    m["compression"] = tgt.compression;
    m["index_filter_blocks"] = tgt.index_filter_blocks;
    {
      StoreOptions so;
      so.block_size = tgt.block_size;
      m["block_size"] = table_options(so).block_size;   // read back from the options the SSTs were written with
      m["bloom_bits_per_key"] = so.bloom_bits_per_key;
      m["partition_bytes"] = so.partition_bytes;
    }
    // content_sha covers the WHOLE schema.json incl. the post-hash adj/prop_groups blocks
    // (the dataset hash deliberately excludes them; an in-place edit of only those lines
    // would otherwise leave a stale store green -- session open checks this stamp)
    m["schema"] = {{"name", schema.name()}, {"hash", schema.hash()},
                   {"content_sha", schema.content_sha()}, {"path", record_schema_path(schema_path)}};
    m["records"] = {{"sha256", stream_sha}, {"lines", n_lines}, {"vertices", n_v}, {"edges", n_e}, {"counts", counts}};
    m["ingest_counts"] = fmt->ingest_counts();
    manifests.push_back(m);
  }
  sink.finish(manifests);
  auto t3 = std::chrono::steady_clock::now();
  for (size_t i = 0; i < opts.targets.size(); i++) {
    json& m = manifests[i];
    m["keys"] = sink.keys();
    m["logical_bytes"] = sink.logical_bytes();
    if (!opts.scale.empty()) m["scale"] = opts.scale;   // the dataset tag (--scale); absent on a store whose manifest has no scale
    m["ingest_wall_s"] = std::chrono::duration<double>(t3 - t0).count();   // --multi: the WHOLE shared pass, not this format alone
    m["provenance"] = provenance();
    std::ofstream out(opts.targets[i].dir + "/manifest.json");
    out << m.dump(2) << "\n";
    std::cerr << "[ingest] " << opts.targets[i].dir << ": " << m["sst_bytes"] << " SST bytes ("
              << opts.targets[i].compression << "), settled\n";
  }
  return manifests;
}
}  // namespace

}  // namespace evalcpp
