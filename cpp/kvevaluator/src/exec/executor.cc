#include "exec/executor.h"
#include "store/perf_snapshot.h"

#include "formats/common.h"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <map>
#include <numeric>
#include <stdexcept>
#include <thread>
#include <tuple>
#include <utility>

#include "exec/pool.h"
#include "exec/reaper.h"
#include "exec/replay.h"
#include "exec/step_exec.h"
#include "ops/flow.h"

namespace evalcpp {

static std::vector<int> physical_cores() {
  // one cpu per physical core: the first id of every thread_siblings_list
  std::vector<int> out;
  std::set<int> seen;
  for (int c = 0; c < int(std::thread::hardware_concurrency()); c++) {
    std::ifstream f("/sys/devices/system/cpu/cpu" + std::to_string(c) + "/topology/thread_siblings_list");
    std::string s;
    if (!f || !std::getline(f, s)) { out.push_back(c); continue; }
    int first = std::atoi(s.c_str());
    if (seen.insert(first).second) out.push_back(first);
  }
  return out;
}

RunConfig RunConfig::from_json(const json& j) {
  RunConfig c;
  if (!j.is_object()) return c;
  c.P = j.value("P", 1);
  c.task_batch = j.value("task_batch", size_t(64));
  if (j.contains("pin_cpus")) {
    if (j["pin_cpus"].is_string() && j["pin_cpus"] == "physical") c.pin_cpus = physical_cores();
    else if (j["pin_cpus"].is_array()) for (auto& x : j["pin_cpus"]) c.pin_cpus.push_back(x.get<int>());
    else if (!j["pin_cpus"].is_null()) throw std::runtime_error("run.pin_cpus: a core list or \"physical\"");
  }
  if (j.contains("iterations")) {
    c.warmup = j["iterations"].value("warmup", 0);
    c.measured = j["iterations"].value("measured", 1);
  }
  c.cache = j.value("cache", "asis");
  c.perf_level = j.value("perf_level", 2);   // RocksDB counts, no per-call timers: a client that wants them asks for 3
  if (j.contains("emit")) {
    c.emit_iterations = j["emit"].value("iterations", true);
    c.emit_tasks = j["emit"].value("tasks", false);
    c.emit_histograms = j["emit"].value("histograms", false);
    c.emit_input_labels = j["emit"].value("input_labels", true);
    c.emit_kvpair_stats = j["emit"].value("kvpair_stats", true);
    c.emit_out_bytes = j["emit"].value("out_bytes", true);
    c.emit_row_digest = j["emit"].value("row_digest", false);
    c.emit_pinned_usage = j["emit"].value("pinned_usage", false);
  }
  if (j.contains("remote")) c.remote_page_bytes = j["remote"].value("page_bytes", c.remote_page_bytes);
  if (c.remote_page_bytes == 0) throw std::runtime_error("run.remote.page_bytes: must be above 0");
  if (j.contains("remote")) {
    c.remote_delay_us = j["remote"].value("delay_us", uint64_t(0));
    c.remote_mode = j["remote"].value("mode", c.remote_mode);
    c.remote_bandwidth_mbps = j["remote"].value("bandwidth_mbps", 0.0);
    if (c.remote_mode != "operation" && c.remote_mode != "batch" && c.remote_mode != "step")
      throw std::runtime_error("run.remote.mode: operation | batch | step");
    if (c.remote_bandwidth_mbps < 0) throw std::runtime_error("run.remote.bandwidth_mbps: must not be negative");
    if (c.remote_bandwidth_mbps > 0 && !c.remote_delay_us)
      throw std::runtime_error("run.remote.bandwidth_mbps needs run.remote.delay_us above 0 (the regime is off without it)");
    // a sleep under about 100 us is inaccurate: the regime starts there
    if (c.remote_delay_us && c.remote_delay_us < 100) throw std::runtime_error("run.remote.delay_us: 0 (off) or at least 100");
  }
  c.drop_caches = j.value("drop_caches", false);
  c.sample_every = j.value("sample_every", uint64_t(0));   // Stage B: 1-in-K per-op rdtsc sampling
  c.apply_writes = j.value("apply_writes", false);
  c.parts = j.value("parts", false);
  c.trace_args = j.value("trace_args", false);
  c.reuse_distance = j.value("reuse_distance", false);
  c.reuse_reset = j.value("reuse_reset", false);
  c.free_rows = j.value("free_rows", false);
  c.free_outputs = j.value("free_outputs", std::string("inline"));
  c.iterator_pool = j.value("iterator_pool", true);
  if (c.free_outputs != "reaper" && c.free_outputs != "inline")
    throw std::runtime_error("run.free_outputs is \"reaper\" or \"inline\", not \"" + c.free_outputs + "\"");
  c.kv_timing = j.value("kv_timing", false);
  c.decode_timing = j.value("decode_timing", false) || c.kv_timing;   // the breakdown pass (kv_timing) has its decode line
  // a latency run never frees rows between its steps: no timing is ever taken with the option on
  if (c.free_rows && (!c.emit_out_bytes || !c.emit_kvpair_stats || c.cache == "cold" || c.sample_every))
    throw std::runtime_error("run.free_rows is for counts runs only: this run is a latency run (emit.out_bytes or "
                             "emit.kvpair_stats off, cache cold, or sample_every set). Leave run.free_rows out");
  if (j.contains("label_tags")) {
    c.label_tags.hops = j["label_tags"].value("hops", true);
    c.label_tags.probes = j["label_tags"].value("probes", true);
  }
  if (c.apply_writes && (c.P != 1 || c.warmup != 0 || c.measured != 1 || c.cache != "asis"))
    throw std::runtime_error("run.apply_writes needs P=1, no warmup, measured=1, cache=asis "
                             "(an applied write cannot re-run; the sequence is the experiment)");
  if (c.P < 1) throw std::runtime_error("run.P must be >= 1");
  if (c.reuse_distance && (c.P != 1 || c.warmup != 0 || c.measured != 1))
    throw std::runtime_error("run.reuse_distance needs P=1, no warmup, measured=1 (one fetch order, each plan once)");
  if (!c.pin_cpus.empty() && int(c.pin_cpus.size()) < c.P)
    throw std::runtime_error("run.pin_cpus has fewer cores than P");
  if (c.cache != "asis" && c.cache != "warm" && c.cache != "cold")
    throw std::runtime_error("run.cache must be asis | warm | cold");
  if (c.cache == "warm" && c.warmup < 1)
    throw std::runtime_error("run.cache = warm needs iterations.warmup >= 1 (the warmup runs ARE the warming)");
  if (c.measured < 1) throw std::runtime_error("run.iterations.measured must be >= 1");
  return c;
}

json RunConfig::to_json() const {
  json pins = json::array();
  for (int c : pin_cpus) pins.push_back(c);
  return {{"P", P}, {"task_batch", task_batch}, {"pin_cpus", pins},
          {"iterations", {{"warmup", warmup}, {"measured", measured}}}, {"cache", cache},
          {"perf_level", perf_level}, {"drop_caches", drop_caches}, {"sample_every", sample_every},
          {"apply_writes", apply_writes}, {"parts", parts}, {"trace_args", trace_args}, {"free_outputs", free_outputs}, {"iterator_pool", iterator_pool},
          {"label_tags", {{"hops", label_tags.hops}, {"probes", label_tags.probes}}}};
}

namespace {
// every field of `a` is in `b` with the same value (objects: recursively; anything else: equal)
bool json_within(const json& a, const json& b) {
  if (!a.is_object()) return a == b;
  if (!b.is_object()) return false;
  for (auto it = a.begin(); it != a.end(); ++it) {
    auto f = b.find(it.key());
    if (f == b.end() || !json_within(it.value(), *f)) return false;
  }
  return true;
}

// emit.row_digest: (rows in order, rows as a multiset) of one step's output. hash_value ignores the label tags a
// value carries and a row's field order; the mixing constant keeps the two digests apart.
std::pair<uint64_t, uint64_t> row_digests(const Value& out) {
  auto mix = [](uint64_t h) { h ^= h >> 33; h *= 0xff51afd7ed558ccdULL; h ^= h >> 33; return h; };
  if (!(out.is_list() || out.is_tuple())) { uint64_t h = mix(hash_value(out)); return {h, h}; }
  uint64_t ordered = 0xcbf29ce484222325ULL, as_set = 0;
  for (auto& row : out.as_list()) {
    uint64_t h = mix(hash_value(row));
    ordered = (ordered ^ h) * 0x100000001b3ULL;
    as_set += h;
  }
  return {ordered, as_set};
}

using Clock = std::chrono::steady_clock;

struct PhysicalCounts {
  uint64_t seek = 0, get = 0, next = 0;
  uint64_t keys_written = 0, bytes_written = 0;   // the write side (apply mode only moves them)
};

PhysicalCounts physical_snapshot(Store& store) {   // six tickers only: cheap per step
  PhysicalCounts p;
  p.seek = store.ticker(rocksdb::NUMBER_DB_SEEK);
  p.get = store.ticker(rocksdb::NUMBER_KEYS_READ);
  p.next = store.ticker(rocksdb::NUMBER_DB_NEXT) + store.ticker(rocksdb::NUMBER_DB_PREV);
  p.keys_written = store.ticker(rocksdb::NUMBER_KEYS_WRITTEN);
  p.bytes_written = store.ticker(rocksdb::BYTES_WRITTEN);
  return p;
}

// one step, one iteration
struct StepRun {
  Counts logical;             // replay(landings) + direct
  Counts physical;            // what the workers did (sum of their counting stores)
  PhysicalCounts tickers;     // RocksDB's own count for the step
  uint64_t wall_ns = 0;       // the step on the main thread's clock (tasks + merge + dispatch)
  uint64_t work_ns = 0;       // sum of task walls
  uint64_t wait_ns = 0;       // of which: waiting for a sibling's decode
  uint64_t critical_ns = 0;   // sum over the step's fan-outs of the longest task
  uint64_t overhead_ns = 0;   // wall - the tasks' contribution at this P (dispatch + merge)
  uint64_t span_ns = 0;       // critical + overhead
  size_t ntasks = 0;
  uint64_t task_max_ns = 0;
  size_t rows = 0;            // the step's output size (list length; 1 for a scalar)
  json args;                  // trace_args: the KV step's resolved arguments
  json out_vids;              // trace_args: the _vid of each output row (a list of rows only)
  json arg_tags;              // trace_args: the labels the arguments' values carry (Args::tags_json)
  size_t pinned_at_end = 0, usage_at_end = 0;   // run.emit.pinned_usage: the block cache before the step's pool deletes
  size_t out_bytes = 0;       // the output's NEUTRAL size (value::neutral_bytes); sized at the step's end, out of the walls
  uint64_t rows_digest = 0;   // emit.row_digest: the output rows in order (a check, never a cost input)
  uint64_t rows_set_digest = 0;   // the same rows with their order ignored: tells another order from other rows
  uint64_t free_ns = 0;       // run.free_rows: freeing the outputs whose last reader this step was (in no wall)
  PerfSnapshot perf;          // PerfContext + IOStats, summed over tasks
  CodecStats codec;           // summed over tasks
  std::vector<TaskStats> tasks;
  PartCounts parts;           // run.parts + a format PartScheme: `logical` split by store part
  HistParts hists;            // the KV-pair size histograms of `logical`, by part id (id 0 without a scheme)
  std::vector<DecodeCounts> decoded;   // parts on: the decode counts by part id (they sum to codec.decoded)
  ChunkStats chunks;          // from the landing replay (+ a fused scan's held chunk)
  RtTotals round_trips;       // store/round_trips.h: the replayed reads' + the direct ones'
  RtText rt_text;             // ... the same as levels, job by job (the step column rt_levels)
  uint64_t remote_wait_ns = 0;   // the remote regime: what the step waited (measured); inside wall_ns
  RowsIn in;                  // the rows the step took in (ir/plan.h rows_in)
  uint64_t reuse[ReuseTracker::kBuckets] = {};   // run.reuse_distance: fetched kvbytes by reuse-distance bucket
  uint64_t reuse_blocks[ReuseTracker::kBuckets] = {};   // and the KV pairs' own 4 KB blocks, summed, by the same bucket
  uint64_t reuse_request_blocks[ReuseTracker::kBuckets] = {};   // and each request's bytes rounded up once
};

// A step's histograms against its counts: each histogram holds one entry per operation and its sizes sum to the
// summed lanes. A mismatch is a task whose logical counts were taken without committing its histogram entries (or the
// reverse), never data.
void check_hists(const std::string& where, const KvPairHists& h, const Counts& c) {
  auto bad = [&](const char* what, uint64_t have, uint64_t want) {
    throw std::runtime_error(where + ": KV-pair size histogram " + what + " " + std::to_string(have) + " != " +
                             std::to_string(want) + " (a task's counts taken without its histogram entries)");
  };
  if (h.get.n != c.G) bad("GET entries", h.get.n, c.G);
  if (h.get.sum != c.get_key + c.get_value) bad("GET bytes", h.get.sum, c.get_key + c.get_value);
  if (h.range_bytes.n != c.S) bad("range-read entries", h.range_bytes.n, c.S);
  if (h.range_keys.n != c.S) bad("range-read key entries", h.range_keys.n, c.S);
  if (h.range_bytes.sum != c.scan_key + c.scan_value) bad("range-read bytes", h.range_bytes.sum, c.scan_key + c.scan_value);
  if (h.put.n != c.P) bad("PUT entries", h.put.n, c.P);
  if (h.del.n != c.D) bad("DELETE entries", h.del.n, c.D);
  if (h.put.sum > c.write_key + c.write_value) bad("PUT bytes above the write lanes", h.put.sum, c.write_key + c.write_value);
  if (c.D == 0 && h.put.sum != c.write_key + c.write_value) bad("PUT bytes", h.put.sum, c.write_key + c.write_value);
  // exact Fetch: a GET hit is one store block; the 4 KB units of a kind lie between its bytes / 4096 and that plus one
  // per request; a request never fills more store blocks than 4 KB units
  if (h.get_store_blocks != c.G - c.G_miss) bad("GET store blocks (one per hit)", h.get_store_blocks, c.G - c.G_miss);
  if (h.get_fetch_4k < KvPairHists::blocks_4k(h.get.sum) || h.get_fetch_4k > h.get.sum / 4096 + c.G)
    bad("GET 4 KB units outside the bytes' bounds", h.get_fetch_4k, KvPairHists::blocks_4k(h.get.sum));
  if (h.range_fetch_4k < KvPairHists::blocks_4k(h.range_bytes.sum) || h.range_fetch_4k > h.range_bytes.sum / 4096 + c.S)
    bad("range-read 4 KB units outside the bytes' bounds", h.range_fetch_4k, KvPairHists::blocks_4k(h.range_bytes.sum));
  if (h.range_store_blocks > h.range_fetch_4k) bad("range-read store blocks above its 4 KB units", h.range_store_blocks, h.range_fetch_4k);
  if (h.get_store_blocks > h.get_fetch_4k) bad("GET store blocks above its 4 KB units", h.get_store_blocks, h.get_fetch_4k);
  // exact write units (1 KB): at least one per request and the bytes' units, at most bytes / 1024 + one per request; a
  // DELETE of unknown size is exactly 1
  uint64_t put_lo = std::max<uint64_t>(c.P, KvPairHists::units(h.put.sum, 1024));
  if (h.put_units_1k < put_lo || h.put_units_1k > h.put.sum / 1024 + c.P)
    bad("PUT 1 KB units outside the bytes' bounds", h.put_units_1k, put_lo);
  uint64_t del_lo = std::max<uint64_t>(c.D, KvPairHists::units(h.del.sum, 1024) + h.del.unknown);
  if (h.del_units_1k < del_lo || h.del_units_1k > h.del.sum / 1024 + c.D)
    bad("DELETE 1 KB units outside the bytes' bounds", h.del_units_1k, del_lo);
  // a key crossed is one of the keys the range reads returned (range_keys.sum: the hit landings)
  // every key a range read returned is a key crossed or a landing whose value was read: nothing else
  if (h.keys_crossed + h.values_read != h.range_keys.sum)
    throw std::runtime_error(where + ": keys_crossed " + std::to_string(h.keys_crossed) + " + values read " +
                             std::to_string(h.values_read) + " != the " + std::to_string(h.range_keys.sum) +
                             " keys its range reads returned");
}

// one part's bucket as the response carries it: the attributed lanes only (no used_*, no audit)
json part_json(const Counts& c) {
  json j = {{"S", c.S}, {"G", c.G}, {"N", c.N}, {"P", c.P}, {"D", c.D},
          {"S_miss", c.S_miss}, {"G_miss", c.G_miss}, {"N_miss", c.N_miss},
          {"bytes", {{"get_value", c.get_value}, {"scan_value", c.scan_value}, {"scan_key", c.scan_key},
                     {"get_key", c.get_key}, {"seek_miss_key", c.seek_miss_key},
                     {"next_miss_key", c.next_miss_key}, {"get_miss_key", c.get_miss_key},
                     {"write_key", c.write_key}, {"write_value", c.write_value}}}};
  if (c.last_chunk.any()) j["last_chunk"] = c.last_chunk.to_json();   // a write's last-chunk touches (Counts::LastChunk)
  return j;
}

// two buckets equal on every field part_json carries (the comparison part_json(a) == part_json(b), without the JSON)
bool part_same(const Counts& a, const Counts& b) {
  return a.S == b.S && a.G == b.G && a.N == b.N && a.P == b.P && a.D == b.D &&
         a.S_miss == b.S_miss && a.G_miss == b.G_miss && a.N_miss == b.N_miss &&
         a.get_value == b.get_value && a.scan_value == b.scan_value && a.scan_key == b.scan_key && a.get_key == b.get_key &&
         a.seek_miss_key == b.seek_miss_key && a.next_miss_key == b.next_miss_key && a.get_miss_key == b.get_miss_key &&
         a.write_key == b.write_key && a.write_value == b.write_value && a.last_chunk == b.last_chunk;
}

bool part_empty(const Counts& c) {
  return !(c.S || c.G || c.N || c.P || c.D || c.get_value || c.scan_value || c.scan_key || c.get_key ||
           c.seek_miss_key || c.next_miss_key || c.get_miss_key || c.write_key || c.write_value || c.last_chunk.any());
}

void make_cold(Store& store, bool drop_caches) {
  store.reopen_cold();
  store.evict_page_cache();
  if (drop_caches) {
    int rc = std::system("sync; echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null");
    if (rc != 0) throw std::runtime_error("drop_caches failed (sudo?)");
  }
}

}  // namespace

json run_echo(const RunConfig& cfg, const Store& store) {
  json run = cfg.to_json();
  if (cfg.free_rows) run["free_rows"] = true;    // off: absent, the response of before the option
  run["store_options"] = store.options().to_json();
  if (cfg.kv_timing) run["kv_timing"] = true;    // off: absent, the response of before the option
  if (cfg.decode_timing) run["decode_timing"] = true;   // off: absent; the codec clocks read null
  run["cpu"] = {{"governor", "n/a (no cpufreq on this host)"}, {"boost", "n/a"},
                {"smt", "on"}};
  return run;
}

json execute(Format& fmt, Store& store, const Plan& plan, const RunConfig& cfg, const ResponseOpts& opts) {
  set_perf_level(cfg.perf_level);
  rt_page_bytes() = cfg.remote_page_bytes;
  remote() = Remote();
  if (cfg.remote_delay_us) {
    remote().mode = cfg.remote_mode == "operation" ? Remote::kOperation : cfg.remote_mode == "step" ? Remote::kStep : Remote::kBatch;
    remote().delay_ns = cfg.remote_delay_us * 1000;
    remote().ns_per_byte = cfg.remote_bandwidth_mbps > 0 ? 8000.0 / cfg.remote_bandwidth_mbps : 0;   // 1 Mbps = 125 bytes per ms
  }
  struct RemoteOff { ~RemoteOff() { remote() = Remote(); } } remote_off;   // never left on for the next run
  kv_timing_on() = cfg.kv_timing;              // before the pool: its workers start after this store
  struct KvTimingOff { ~KvTimingOff() { kv_timing_on() = false; } } kv_timing_off;
  decode_timing_on() = cfg.decode_timing;      // the same, for the codec clocks
  struct DecodeTimingOff { ~DecodeTimingOff() { decode_timing_on() = false; } } decode_timing_off;
  ShadowOverlay overlay;                        // write ops buffer here; a measured store never mutates
  bool plan_writes = false;                     // any write op: the tickers check goes n/a (kvbench's
  for (auto& st : plan.steps)                   // rocks check does the same for a shadowed cell --
    plan_writes = plan_writes || is_write_op(st.op);   // the overlay serves some reads off RocksDB)
  // apply mode: no overlay -- the workers' puts land in the real store (counting.h)
  ThreadPool pool(store, cfg.P, cfg.pin_cpus, cfg.perf_level, cfg.emit_tasks,
                  cfg.apply_writes ? nullptr : &overlay, cfg.apply_writes);
  // one iterator per worker store, reused across a step's SEEKs and deleted at the step's end (StepExec::end_job)
  for (int i = 0; i < pool.P(); i++) {
    pool.slot(i).store.pool_iterators(cfg.iterator_pool);
    pool.slot(i).store.read_pinned = cfg.emit_pinned_usage;
  }
  const PartScheme* scheme = cfg.parts ? fmt.part_scheme() : nullptr;   // null: no attribution at all
  pool.set_part_scheme(scheme);
  ReadSizes read_sizes;                         // a write plan: the sizes its deletes can know (store/size_hist.h)
  if (plan_writes && cfg.emit_kvpair_stats) pool.set_read_sizes(&read_sizes);   // (a latency run: none of it)
  pool.set_kvpair_stats(cfg.emit_kvpair_stats);
  // the round-trip counts ride the KV pair statistics; the remote regime waits by them, so it turns them on too
  const bool round_trips_on = cfg.emit_kvpair_stats || cfg.remote_delay_us > 0;
  pool.set_round_trips(round_trips_on);
  if (cfg.reuse_distance) {                     // P=1: the one worker's store sees every fetch, in order
    if (cfg.reuse_reset) process_reuse_tracker().clear();
    pool.slot(0).store.set_reuse_tracker(&process_reuse_tracker());
  }
  size_t nsteps = plan.steps.size();
  std::vector<std::vector<StepRun>> runs(nsteps);   // per step: measured iterations
  std::vector<uint64_t> plan_wall;
  // per step name, the index of the last step whose args reference it (Step::refs, the names resolve_args
  // substitutes); the plan's output is read after the plan. Read once here, before the first iteration.
  const bool free_rows = cfg.free_rows;
  std::map<std::string, size_t> last_reader;
  for (size_t i = 0; i < nsteps; i++)
    for (auto& r : plan.steps[i].refs) last_reader[r] = i;
  last_reader[plan.output] = nsteps;
  // a flow step takes an input by move when it is that input's last reader and names it once, as a whole argument
  // ({"ref": name}): the step then holds the only reference, and an op may hand the input's rows on instead of
  // copying them (Args::owned_list). What the op leaves goes back to the outputs under kSpent + name, so it is freed
  // with them, after the plan's clock, as before. run.free_rows keeps its own between-step frees: no move there.
  std::vector<std::vector<std::pair<std::string, std::string>>> owned(nsteps);   // per step: (arg key, input name)
  if (!free_rows)
    for (size_t i = 0; i < nsteps; i++) {
      const Step& st = plan.steps[i];
      if (!is_flow_op(st.op)) continue;
      for (auto it = st.args.begin(); it != st.args.end(); ++it) {
        const json& v = it.value();
        if (!(v.is_object() && v.size() == 1 && v.contains("ref") && v["ref"].is_string())) continue;
        std::string x = v["ref"].get<std::string>();
        if (last_reader[x] == i && std::count(st.refs.begin(), st.refs.end(), x) == 1) owned[i].emplace_back(it.key(), x);
      }
    }
  static const std::string kSpent = "\x01spent:";   // no step name starts with a control character
  std::vector<uint64_t> plan_free;        // per measured iteration: the frees' time (its own channel, not in plan_wall)
  uint64_t freed_steps = 0, freed_bytes = 0;   // the control that the freeing happened: the last measured iteration's
  std::vector<nlohmann::json> plan_deltas;
  std::vector<nlohmann::json> plan_hdeltas;   // per-iteration histogram deltas (count+sum)
  std::vector<std::vector<uint64_t>> busy_per_iter;
  Value final_rows;
  const bool reaper = cfg.free_outputs == "reaper";
  std::vector<uint64_t> inline_free;      // run.free_outputs = inline: per measured iteration, the outputs' free
  int total_iters = cfg.warmup + cfg.measured;
  for (int iter = 0; iter < total_iters; iter++) {
    // no free overlaps a plan: not this request's earlier iterations', not the previous request's (a free running beside
    // a plan slowed it 35-60%, IC5 at SF0.1: allocator and memory contention). The reaper takes the free off the request's
    // path only where nothing else runs: the response's assembly and write, and the gap until the next request
    Reaper::get().drain();                       // every mode: a batch line may follow a reaper line (idle: a lock and a check)
    overlay.reset();   // each iteration starts from the pristine store (an insert stays an insert)
    read_sizes.clear();
    bool measured = iter >= cfg.warmup;
    pool.set_sample_every(measured ? cfg.sample_every : 0);   // never sample warmup ops
    if (cfg.cache == "cold") {
      for (int i = 0; i < pool.P(); i++) pool.slot(i).store.end_step();   // none is left; a reopen would strand it
      make_cold(store, cfg.drop_caches);
    }
    std::map<std::string, Value> outputs;
    bool vertex_written = false;   // a vertex write ran earlier in this iteration (labels on rows: existence probes)
    std::vector<uint64_t> tickers_before;
    nlohmann::json hists_before;
    if (measured) { tickers_before = store.ticker_counts(opts.tickers); if (cfg.emit_histograms) hists_before = store.histograms(); }
    pool.reset_busy();
    uint64_t aside_ns = 0;                       // run.free_rows: the sizing walks and the frees, taken out of the plan wall
    uint64_t free_ns_iter = 0;
    std::map<std::string, uint64_t> out_size;   // run.free_rows: each live output's out_bytes
    if (free_rows && measured) freed_steps = freed_bytes = 0;
    auto plan_t0 = Clock::now();
    for (size_t i = 0; i < nsteps; i++) {
      const Step& step = plan.steps[i];
      Args args = resolve_args(step, plan.bindings, outputs);   // untimed: plumbing, not the op
      RowsIn in = measured && cfg.emit_kvpair_stats ? rows_in(step, outputs, cfg.emit_input_labels) : RowsIn();
      for (auto& [key, x] : owned[i]) outputs.erase(x);   // the step's args hold it alone now
      StepExec ex(pool, cfg.task_batch, cfg.emit_tasks);
      ex.tags = cfg.label_tags;
      ex.after_vertex_write = vertex_written;
      ex.strict_tags = cfg.trace_args;
      if (cfg.reuse_distance) {
        std::fill_n(pool.slot(0).store.reuse_bytes(), size_t(ReuseTracker::kBuckets), uint64_t(0));
        std::fill_n(pool.slot(0).store.reuse_blocks(), size_t(ReuseTracker::kBuckets), uint64_t(0));
        std::fill_n(pool.slot(0).store.reuse_request_blocks(), size_t(ReuseTracker::kBuckets), uint64_t(0));
      }
      PhysicalCounts p0 = physical_snapshot(store);
      auto t0 = Clock::now();
      Value out;
      try {
        if (is_flow_op(step.op)) {
          std::vector<TaskFn> one = {[&](WorkerSlot&, TaskResult& r) { r.out = run_flow_op_owned(step, args); }};
          out = ex.run(one)[0].out;
        } else if (fmt.has_kv_op(step.op)) {
          out = fmt.run_kv_op(step, args, ex);
          vertex_written = vertex_written || step.op == "delete_vertex" || step.op == "insert_vertex";
        } else {
          throw std::runtime_error("unknown op '" + step.op + "'");
        }
      } catch (const std::exception& e) {
        throw std::runtime_error("step " + step.name + " (" + step.op + "): " + e.what());
      }
      auto t1 = Clock::now();
      for (auto& [key, x] : owned[i]) outputs[kSpent + x] = args.take(key);   // freed with the outputs, after the clock
      PhysicalCounts p1 = physical_snapshot(store);
      if (measured) {
        StepRun r;
        r.rows = (out.is_list() || out.is_tuple()) ? out.as_list().size() : 1;
        if (cfg.trace_args && !is_flow_op(step.op)) {
          r.args = args.to_json();
          r.arg_tags = args.tags_json();
          r.out_vids = json::array();
          if (out.is_list())
            for (auto& row : out.as_list())
              if (row.is_row())
                if (const Value* v = row.find("_vid"); v && v->is_text()) r.out_vids.push_back(v->text());
        }
        r.logical = replay_landings(ex.landings(), scheme, scheme ? &r.parts : nullptr,
                                    cfg.emit_kvpair_stats ? &r.hists : nullptr, &r.chunks, &r.round_trips,
                                    round_trips_on ? &r.rt_text : nullptr);
        r.logical.add(ex.direct);
        r.round_trips.add(ex.direct_round_trips);
        r.rt_text.add(ex.direct_rt_text);
        // every request is one of the step's operations, and a level holds at least one request
        if (uint64_t requests = r.logical.S + r.logical.G + r.round_trips.pages_extra + (r.logical.P + r.logical.D ? 1 : 0);
            round_trips_on && (r.round_trips.critical > r.round_trips.round_trips || r.round_trips.round_trips > requests ||
                               (requests > 0) != (r.round_trips.round_trips > 0)))
          throw std::runtime_error("step " + step.name + " (" + step.op + "): round trips " +
                                   std::to_string(r.round_trips.round_trips) + ", critical " +
                                   std::to_string(r.round_trips.critical) + " against " + std::to_string(requests) +
                                   " requests (S + G + pages + a write batch)");
        if (cfg.emit_kvpair_stats) {
          if (r.hists.size() < (scheme ? scheme->size() : 1)) r.hists.resize(scheme ? scheme->size() : 1);
          add_hists(r.hists, ex.direct_hists);
        }
        if (ex.scan_chunk_bytes_max > r.chunks.held_bytes_peak) r.chunks.held_bytes_peak = ex.scan_chunk_bytes_max;
        r.decoded = ex.decode_parts;
        if (scheme) credit_chunk_parses(ex.landings(), ex.chunk_parses, *scheme, r.decoded);
        r.in = in;
        if (cfg.reuse_distance) {
          std::copy_n(pool.slot(0).store.reuse_bytes(), size_t(ReuseTracker::kBuckets), r.reuse);
          std::copy_n(pool.slot(0).store.reuse_blocks(), size_t(ReuseTracker::kBuckets), r.reuse_blocks);
          std::copy_n(pool.slot(0).store.reuse_request_blocks(), size_t(ReuseTracker::kBuckets), r.reuse_request_blocks);
        }
        if (cfg.emit_kvpair_stats) {
          KvPairHists all;
          for (auto& h : r.hists) all.add(h);
          check_hists("step " + step.name + " (" + step.op + ")", all, r.logical);
        }
        if (scheme) {
          // the parts are the SAME ops split by store part: they must sum to the step's counters on
          // every attributed lane (the replay's parts + the committed direct parts)
          add_parts(r.parts, ex.direct_parts);
          std::string why;
          if (!parts_sum_equal(r.parts, r.logical, &why))
            throw std::runtime_error("step " + step.name + " (" + step.op + "): per-part counts do not sum to "
                                     "the step's counts (" + why + ")");
          for (size_t p = 0; cfg.emit_kvpair_stats && p < r.hists.size() && p < r.parts.size(); p++)   // a part's histograms against its counts
            check_hists("step " + step.name + " (" + step.op + "), part " + scheme->name(p), r.hists[p], r.parts[p]);
        }
        // KV-amp invariant, live from day one: used is the format-framed subset of what
        // was fetched, so it can NEVER exceed read on either channel -- a violation is a
        // used-rule bug (over-annotation), never data. scan_key/get_key are HIT key bytes:
        // a used annotation on a landing declared a miss trips this too
        if (r.logical.used_key > r.logical.scan_key + r.logical.get_key ||
            r.logical.used_value > r.logical.get_value + r.logical.scan_value ||
            r.logical.used_write_key > r.logical.write_key ||
            r.logical.used_write_value > r.logical.write_value)
          throw std::runtime_error("step " + step.name + " (" + step.op + "): used exceeds read (key " +
                                   std::to_string(r.logical.used_key) + ">" +
                                   std::to_string(r.logical.scan_key + r.logical.get_key) + "? value " +
                                   std::to_string(r.logical.used_value) + ">" +
                                   std::to_string(r.logical.get_value + r.logical.scan_value) +
                                   "?) -- a used-rule over-annotation");
        r.physical = ex.physical;
        r.work_ns = ex.work_ns;
        r.wait_ns = ex.wait_ns;
        r.task_max_ns = ex.task_max_ns;
        r.perf = ex.perf;
        for (int w = 0; w < pool.P(); w++) {     // the pooled iterators' deletes, timed outside the workers' windows
          if (cfg.emit_pinned_usage) {            // one cache: the largest reading over the worker stores
            r.pinned_at_end = std::max(r.pinned_at_end, pool.slot(w).store.pinned_at_end);
            r.usage_at_end = std::max(r.usage_at_end, pool.slot(w).store.usage_at_end);
            pool.slot(w).store.pinned_at_end = pool.slot(w).store.usage_at_end = 0;
          }
          auto [ns, calls] = pool.slot(w).store.take_closed();
          r.perf.kv_iter_close_ns += ns;
          r.perf.kv_iter_close_calls += calls;
          r.perf.kv_call_ns += ns;
        }
        r.codec = ex.codec;
        r.critical_ns = ex.critical_ns;
        r.ntasks = ex.ntasks;
        r.tickers = {p1.seek - p0.seek, p1.get - p0.get, p1.next - p0.next,
                     p1.keys_written - p0.keys_written, p1.bytes_written - p0.bytes_written};
        r.remote_wait_ns = ex.remote_wait_ns;        // mode operation: waited inside the tasks, already in the clock
        uint64_t waited_after = 0;
        if (remote().mode == Remote::kBatch || remote().mode == Remote::kStep) {
          // the step's wait, slept here (its round trips are known only after the replay) and added to the step's
          // clock: a real sleep, measured; the accounting between t1 and here is not in it
          const Counts& c = r.logical;
          uint64_t fetched = c.get_key + c.get_value + c.scan_key + c.scan_value + c.seek_miss_key + c.next_miss_key + c.get_miss_key;
          uint64_t trips = remote().mode == Remote::kBatch ? r.round_trips.critical : (r.round_trips.round_trips ? 1 : 0);
          waited_after = remote_sleep(trips * remote().delay_ns + uint64_t(double(fetched) * remote().ns_per_byte));
          r.remote_wait_ns += waited_after;
        }
        r.wall_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count() + waited_after;
        // the tasks' share of the wall at this P is at least work/P and at least critical
        uint64_t tasks_share = std::max(r.critical_ns, r.work_ns / uint64_t(cfg.P));
        r.overhead_ns = r.wall_ns > tasks_share ? r.wall_ns - tasks_share : 0;
        r.span_ns = r.critical_ns + r.overhead_ns;
        if (cfg.emit_tasks) r.tasks = ex.task_rows();
        runs[i].push_back(std::move(r));
      }
      if (free_rows) {
        auto a0 = Clock::now();
        if (measured) {                // size the output NOW, before anything is freed (a counts run: out_bytes is on)
          uint64_t nb = neutral_bytes(out);
          runs[i].back().out_bytes = nb;
          if (cfg.emit_row_digest) std::tie(runs[i].back().rows_digest, runs[i].back().rows_set_digest) = row_digests(out);
          out_size[step.name] = nb;
        }
        outputs[step.name] = std::move(out);
        auto f0 = Clock::now();
        // the step's own copies of its inputs go first: a row dies when its LAST holder lets go, and that is often
        // this copy, not the outputs map. Then every output whose last reader is this step (one nobody reads: itself)
        args = Args();
        for (auto it = outputs.begin(); it != outputs.end();) {
          auto lr = last_reader.find(it->first);
          if ((lr == last_reader.end() ? i : lr->second) <= i) {
            if (measured) { freed_steps++; freed_bytes += out_size[it->first]; }
            it = outputs.erase(it);
          } else {
            ++it;
          }
        }
        auto a1 = Clock::now();
        uint64_t fns = std::chrono::duration_cast<std::chrono::nanoseconds>(a1 - f0).count();
        if (measured) runs[i].back().free_ns = fns;
        free_ns_iter += fns;
        aside_ns += std::chrono::duration_cast<std::chrono::nanoseconds>(a1 - a0).count();
        continue;
      }
      if (measured && (cfg.emit_out_bytes || cfg.emit_row_digest)) {
        // size and digest the output NOW, as run.free_rows does: a later step may take it by move (its last reader),
        // so after the plan it can be emptied. The walk is accounting, not the op: its time is out of the plan wall
        auto a0 = Clock::now();
        if (cfg.emit_out_bytes) runs[i].back().out_bytes = neutral_bytes(out);
        if (cfg.emit_row_digest) std::tie(runs[i].back().rows_digest, runs[i].back().rows_set_digest) = row_digests(out);
        aside_ns += std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now() - a0).count();
      }
      outputs[step.name] = std::move(out);
    }
    auto plan_t1 = Clock::now();
    if (measured) {
      // the sizing and digest walks (and run.free_rows' frees) ran between the steps; their time is not the plan's
      plan_wall.push_back(std::chrono::duration_cast<std::chrono::nanoseconds>(plan_t1 - plan_t0).count() - aside_ns);
      if (free_rows) plan_free.push_back(free_ns_iter);
      plan_deltas.push_back(ticker_counts_delta(tickers_before, store.ticker_counts(opts.tickers), opts.tickers));
      if (cfg.emit_histograms) plan_hdeltas.push_back(histograms_delta(hists_before, store.histograms()));
      busy_per_iter.push_back(pool.busy_ns());
      if (free_rows) {
        // the one output that outlives the plan. A freed output must never read as empty: every other read of an
        // earlier output goes through resolve_args, which throws on a name that is not there (ir/plan.cc, "ref to
        // missing step output"); this is the only read by operator[] and under the option it throws too
        auto fin = outputs.find(plan.output);
        if (fin == outputs.end())
          throw std::runtime_error("run.free_rows: the plan's output '" + plan.output + "' is not among the live step outputs");
        final_rows = fin->second;
      } else {
        final_rows = outputs[plan.output];
      }
      // every step's output was sized and digested at its step's end (out of the plan wall): see the loop
    }
    // the outputs' free, after every walk over them and outside the plan's clock (run.free_outputs)
    if (reaper) {
      Reaper::get().give(std::move(outputs));
    } else {
      auto f0 = Clock::now();
      outputs.clear();
      if (measured) inline_free.push_back(uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now() - f0).count()));
    }
  }

  // --- assemble the response
  // run.emit.kvpair_stats off: the codec block is the three fields of before the decode counts
  // run.decode_timing off: the two clocks are null (not timed), so a reader that needs them fails instead of reading 0
  auto codec_json = [&](const CodecStats& c) {
    json j = cfg.emit_kvpair_stats ? c.to_json()
                                 : json{{"decode_ns", c.decode_ns}, {"decompress_ns", c.decompress_ns}, {"chunks_decoded", c.chunks_decoded}};
    if (!cfg.decode_timing) j["decode_ns"] = j["decompress_ns"] = nullptr;
    return j;
  };
  json steps = json::array();
  RtTotals rt_plan;
  uint64_t remote_wait_plan = 0;
  Counts totals;
  uint64_t work_ns = 0, span_ns = 0, overhead_ns = 0;
  for (size_t i = 0; i < nsteps; i++) {
    const Step& step = plan.steps[i];
    const std::vector<StepRun>& rs = runs[i];
    for (auto& r : rs)
      if (r.logical.S != rs[0].logical.S || r.logical.G != rs[0].logical.G || r.logical.N != rs[0].logical.N)
        throw std::runtime_error("step " + step.name + ": S/G/N differ across iterations");
    const Counts& c = rs[0].logical;
    for (auto& r : rs) {
      // RocksDB's tickers must equal what the workers did, at any P (both are physical).
      // A plan with writes is checkable ONLY in apply mode: in shadow mode overlay-served reads
      // never reach RocksDB. The uncounted raw_get reads (setup / sortvals fallback) are AUDITED (raw_gets), so
      // keys.read == G + raw_gets holds in both cases; likewise the last-chunk probe's uncounted seeks (raw_seeks:
      // exec/chunk_write.cc locate), so seek == S + raw_seeks; the write side (keys.written == P + D,
      // bytes.written == the single-op batch formula) closes the accounting for real writes.
      // (kvbench's shadow-mode rocks check stays n/a; the write-count oracle runs apply mode.)
      bool checkable = !plan_writes || cfg.apply_writes;
      if (checkable &&
          (r.tickers.seek != r.physical.S + r.physical.raw_seeks || r.tickers.get != r.physical.G + r.physical.raw_gets ||
           r.tickers.next != r.physical.N))
        throw std::runtime_error("step " + step.name + ": RocksDB tickers (seek " + std::to_string(r.tickers.seek) +
                                 ", get " + std::to_string(r.tickers.get) + ", next " + std::to_string(r.tickers.next) +
                                 ") != the workers' counts (S " + std::to_string(r.physical.S) + " + raw_seeks " +
                                 std::to_string(r.physical.raw_seeks) + ", G " +
                                 std::to_string(r.physical.G) + " + raw_gets " + std::to_string(r.physical.raw_gets) +
                                 ", N " + std::to_string(r.physical.N) + ")");
      // the used-write channels (the write-amplification study's numerator) are bounded by the
      // physical write channels by construction: an operator can never claim more logical
      // change than it wrote (the read-side twin: used <= read, gated per op in the kv_amp study)
      if (r.physical.used_write_key > r.physical.write_key || r.physical.used_write_value > r.physical.write_value)
        throw std::runtime_error("step " + step.name + ": used_write (" + std::to_string(r.physical.used_write_key) + " key B, " +
                                 std::to_string(r.physical.used_write_value) + " value B) exceeds write (" +
                                 std::to_string(r.physical.write_key) + ", " + std::to_string(r.physical.write_value) + ")");
      if (cfg.apply_writes &&                   // (apply mode is checkable by definition)
          (r.tickers.keys_written != r.physical.P + r.physical.D || r.tickers.bytes_written != r.physical.rocks_write_bytes))
        throw std::runtime_error("step " + step.name + ": RocksDB wrote " + std::to_string(r.tickers.keys_written) +
                                 " keys / " + std::to_string(r.tickers.bytes_written) + " B != the workers' P+D " +
                                 std::to_string(r.physical.P + r.physical.D) + " / expected batch bytes " +
                                 std::to_string(r.physical.rocks_write_bytes));
      // at P=1 the physical ops AND bytes are the logical ones, exactly (plan 8.2). (The G arm
      // is vacuous for pertype_csr, which never GETs; RocksDB ticks keys.read per Get call.)
      if (cfg.P == 1 && (r.physical.S != c.S || r.physical.G != c.G || r.physical.N != c.N ||
                         r.physical.scan_value != c.scan_value || r.physical.scan_key != c.scan_key ||
                         r.physical.S_miss != c.S_miss || r.physical.N_miss != c.N_miss ||
                         r.physical.G_miss != c.G_miss || r.physical.seek_miss_key != c.seek_miss_key ||
                         r.physical.next_miss_key != c.next_miss_key || r.physical.get_miss_key != c.get_miss_key))
        throw std::runtime_error("step " + step.name + ": at P=1 physical (S " + std::to_string(r.physical.S) + ", G " +
                                 std::to_string(r.physical.G) + ", N " + std::to_string(r.physical.N) +
                                 ") != logical (S " + std::to_string(c.S) + ", G " + std::to_string(c.G) + ", N " +
                                 std::to_string(c.N) + ")");
    }
    // the MEDIAN ITERATION (by wall; the upper middle for an even count): its wall, engine
    // timers and codec timers are reported together so they describe one and the same run
    std::vector<size_t> order(rs.size());
    std::iota(order.begin(), order.end(), 0);
    std::sort(order.begin(), order.end(), [&](size_t x, size_t y) { return rs[x].wall_ns < rs[y].wall_ns; });
    const StepRun& mid = rs[order[order.size() / 2]];
    json s = c.to_json();
    if (c.last_chunk.any()) s["last_chunk"] = c.last_chunk.to_json();   // only a write that touched a last chunk
    if (scheme) {                          // the non-empty parts, by name (logical: P-independent)
      for (size_t i = 1; i < rs.size(); i++)      // every later iteration against the first, field by field
        for (size_t p = 0; p < rs[i].parts.size(); p++)
          if (!part_same(rs[i].parts[p], rs[0].parts[p]))
            throw std::runtime_error("step " + step.name + ": per-part counts differ across iterations");
      for (size_t i = 1; i < rs.size(); i++)      // ... and what each part decoded (no part's credit may hang on timing)
        if (rs[i].decoded != rs[0].decoded)
          throw std::runtime_error("step " + step.name + ": per-part decode counts differ across iterations");
      json parts = json::object();
      for (size_t p = 0; p < rs[0].parts.size(); p++)
        if (!part_empty(rs[0].parts[p])) {
          parts[scheme->name(p)] = part_json(rs[0].parts[p]);
          if (cfg.emit_kvpair_stats && p < rs[0].hists.size()) parts[scheme->name(p)]["hist"] = rs[0].hists[p].to_json();
        }
      // what each part's reads decoded (the same fields as the step's codec block; a part that only decoded --
      // out of a chunk another probe fetched -- gets its entry here)
      for (size_t p = 0; cfg.emit_kvpair_stats && p < mid.decoded.size(); p++)
        if (mid.decoded[p].any()) {
          if (!parts.contains(scheme->name(p))) parts[scheme->name(p)] = part_json(rs[0].parts[p]);
          CodecStats c;
          c.decoded = mid.decoded[p];
          json j = c.to_json();
          parts[scheme->name(p)]["decoded"] = {{"decode_requests", j["decode_requests"]},
                                                {"fields_decoded", j["fields_decoded"]},
                                                {"decoded_kvbytes", j["decoded_kvbytes"]}};
        }
      s["parts"] = parts;
    }
    if (cfg.emit_kvpair_stats) {             // the step's histograms: its parts' summed (logical: P-independent)
      for (auto& r : rs)
        if (!(r.hists == rs[0].hists)) throw std::runtime_error("step " + step.name + ": KV-pair size histograms differ across iterations");
      KvPairHists all;
      for (auto& h : rs[0].hists) all.add(h);
      s["hist"] = all.to_json();
    }
    if (cfg.emit_kvpair_stats) {             // off: the step is the response of before these statistics, field for field
      s["chunks"] = rs[0].chunks.to_json();
      s["rows_in"] = rs[0].in.rows;
      if (cfg.emit_input_labels) s["rows_in_labelled"] = rs[0].in.labelled;
    }
    if (round_trips_on) {                  // with kvpair_stats, or when the remote regime waits by them
      for (auto& r : rs)
        if (!(r.round_trips == rs[0].round_trips) || r.rt_text.jobs != rs[0].rt_text.jobs)
          throw std::runtime_error("step " + step.name + ": round trips differ across iterations");
      s["round_trips"] = rs[0].round_trips.round_trips;
      s["round_trips_critical"] = rs[0].round_trips.critical;
      s["range_pages_extra"] = rs[0].round_trips.pages_extra;
      s["rt_levels"] = rs[0].rt_text.str();     // the same requests, level by level (store/round_trips.h RtText)
      if (RtTotals re = rt_text_totals(s["rt_levels"].get<std::string>());
          re.round_trips != rs[0].round_trips.round_trips || re.critical != rs[0].round_trips.critical)
        throw std::runtime_error("step " + step.name + ": rt_levels sums to " + std::to_string(re.round_trips) + " round trips, " +
                                 std::to_string(re.critical) + " critical; the step counted " +
                                 std::to_string(rs[0].round_trips.round_trips) + ", " + std::to_string(rs[0].round_trips.critical));
      rt_plan.add(rs[0].round_trips);
    }
    if (cfg.remote_delay_us) { s["remote_wait_ns"] = mid.remote_wait_ns; remote_wait_plan += mid.remote_wait_ns; }
    if (cfg.reuse_distance) { s["reuse"] = reuse_json(rs[0].reuse); s["reuse_blocks"] = reuse_json(rs[0].reuse_blocks);
                              s["reuse_request_blocks"] = reuse_json(rs[0].reuse_request_blocks); }
    s["name"] = step.name;
    s["op"] = step.op;
    s["rows"] = mid.rows;
    if (cfg.emit_out_bytes) s["out_bytes"] = mid.out_bytes;   // off: the field is absent, never a 0
    if (cfg.emit_pinned_usage && !is_flow_op(step.op)) {
      s["pinned_bytes_at_end"] = mid.pinned_at_end;
      s["cache_usage_at_end"] = mid.usage_at_end;
      s["block_cache_bytes"] = store.options().block_cache_bytes;
    }
    if (cfg.emit_row_digest) {             // off: the fields are absent; every measured iteration must agree
      for (auto& r : rs)
        if (r.rows_digest != rs[0].rows_digest || r.rows_set_digest != rs[0].rows_set_digest)
          throw std::runtime_error("step " + step.name + ": the output rows' digest differs across iterations");
      char hex[17];
      snprintf(hex, sizeof hex, "%016llx", static_cast<unsigned long long>(rs[0].rows_digest)); s["rows_digest"] = hex;
      snprintf(hex, sizeof hex, "%016llx", static_cast<unsigned long long>(rs[0].rows_set_digest)); s["rows_set_digest"] = hex;
    }
    if (free_rows) s["free_ns"] = mid.free_ns;                // the frees this step triggered (not in wall_ns)
    s["kind"] = is_flow_op(step.op) ? "flow" : "kv";
    if (cfg.trace_args && !is_flow_op(step.op)) {
      s["args"] = mid.args;
      s["out_vids"] = mid.out_vids;
      s["arg_tags"] = mid.arg_tags.is_null() ? json::object() : mid.arg_tags;   // always: a missing key = an older binary
    }
    // the write-count oracle's audit (apply mode): what RocksDB booked vs what the workers did.
    // Only for a plan with writes -- on a pure-read plan every field is zero (noise)
    if (plan_writes)
      s["audit"] = {{"checked", cfg.apply_writes}, {"raw_gets", mid.physical.raw_gets},
                    {"rocks_keys_written", mid.tickers.keys_written}, {"rocks_bytes_written", mid.tickers.bytes_written},
                    {"rocks_bytes_expected", mid.physical.rocks_write_bytes}};
    s["physical"] = {{"seek", mid.physical.S}, {"get", mid.physical.G}, {"next", mid.physical.N},
                     {"seek_miss", mid.physical.S_miss}, {"get_miss", mid.physical.G_miss},
                     {"next_miss", mid.physical.N_miss},
                     {"bytes", {{"get_value", mid.physical.get_value}, {"scan_value", mid.physical.scan_value},
                                {"scan_key", mid.physical.scan_key}, {"get_key", mid.physical.get_key},
                                {"seek_miss_key", mid.physical.seek_miss_key},
                                {"next_miss_key", mid.physical.next_miss_key},
                                {"get_miss_key", mid.physical.get_miss_key}}}};
    s["wall_ns"] = mid.wall_ns;
    s["wall_ns_min"] = rs[order.front()].wall_ns;
    s["wall_ns_max"] = rs[order.back()].wall_ns;
    s["work_ns"] = mid.work_ns;
    s["wait_ns"] = mid.wait_ns;
    s["span_ns"] = mid.span_ns;
    s["critical_ns"] = mid.critical_ns;
    s["overhead_ns"] = mid.overhead_ns;
    s["tasks"] = mid.ntasks;
    s["task_max_ns"] = mid.task_max_ns;
    s["kv"] = mid.perf.to_json();
    s["codec"] = codec_json(mid.codec);
    if (cfg.emit_iterations) {
      json per = json::array();
      for (auto& r : rs)
        per.push_back({{"wall_ns", r.wall_ns}, {"work_ns", r.work_ns}, {"wait_ns", r.wait_ns}, {"span_ns", r.span_ns},
                       {"physical", {{"seek", r.physical.S}, {"get", r.physical.G}, {"next", r.physical.N}}},
                       {"kv", r.perf.to_json()}, {"codec", codec_json(r.codec)}});
      if (!(opts.slim && rs.size() == 1 && json_within(per[0], s))) s["per_iteration"] = per;
    }
    if (cfg.emit_tasks) {
      json tj = json::array();
      for (auto& t : mid.tasks)
        tj.push_back({{"wall_ns", t.wall_ns}, {"slot", t.slot}, {"S", t.physical.S}, {"G", t.physical.G},
                      {"N", t.physical.N}, {"kv", t.perf ? t.perf->to_json() : json::object()}, {"codec", codec_json(t.codec)}});
      s["task_rows"] = tj;
    }
    steps.push_back(s);
    totals.add(c);
    work_ns += mid.work_ns;             // tasks only; the dispatch/merge overhead is in span
    overhead_ns += mid.overhead_ns;
    span_ns += mid.span_ns;
  }
  // the median-by-plan-wall measured iteration (upper middle for an even count)
  std::vector<size_t> porder(plan_wall.size());
  std::iota(porder.begin(), porder.end(), 0);
  std::sort(porder.begin(), porder.end(), [&](size_t x, size_t y) { return plan_wall[x] < plan_wall[y]; });
  size_t pmid = porder[porder.size() / 2];
  json resp;
  if (opts.rows) resp["rows"] = to_json(final_rows);
  if (reaper) Reaper::get().give(std::move(final_rows));   // the plan's output, once converted
  resp["steps"] = steps;
  json tot = totals.to_json();
  if (round_trips_on) {
    tot["round_trips"] = rt_plan.round_trips;          // the steps' summed: a step reads what the one before returned
    tot["round_trips_critical"] = rt_plan.critical;
    tot["range_pages_extra"] = rt_plan.pages_extra;
  }
  tot["work_ns"] = work_ns;          // sum of task walls (same meaning as steps[].work_ns)
  tot["overhead_ns"] = overhead_ns;  // per-step dispatch + merge, summed
  tot["span_ns"] = span_ns;          // critical path: longest task per fan-out + overhead, steps chained
  tot["wall_ns"] = plan_wall[pmid];  // the plan's clock (includes the interpreter's instrument work)
  tot["wall_ns_all"] = plan_wall;
  if (free_rows) {                   // off: absent
    tot["free_ns"] = plan_free[pmid];    // the frees' time in the median-wall iteration (not in wall_ns)
    tot["free_ns_all"] = plan_free;
  }
  tot["free_outputs"] = cfg.free_outputs;
  // the reaper's frees finished since the last reaper request took this: earlier requests' and this request's earlier
  // iterations', and, by thread timing, maybe this one's last hand-off. Not this plan's own cost
  if (reaper) tot["reaper_done_ns"] = Reaper::get().take_done_ns();
  else tot["free_outputs_ns"] = inline_free[pmid];   // the median-wall iteration's step outputs; the plan's output
                                                     // (final_rows, still referenced) is freed later, untimed
  json busy = json::array();
  for (uint64_t b : busy_per_iter[pmid]) busy.push_back(b);
  uint64_t wait_total = 0;
  for (size_t i = 0; i < nsteps; i++) wait_total += runs[i][pmid].wait_ns;
  tot["wait_ns"] = wait_total;
  tot["worker_busy_ns"] = busy;
  resp["totals"] = tot;
  resp["remote"] = {{"page_bytes", cfg.remote_page_bytes}};
  if (cfg.remote_delay_us) {   // off: absent, as the step's remote_wait_ns
    resp["remote"]["delay_us"] = cfg.remote_delay_us;
    resp["remote"]["mode"] = cfg.remote_mode;
    resp["remote"]["bandwidth_mbps"] = cfg.remote_bandwidth_mbps;
    tot["remote_wait_ns"] = remote_wait_plan;
    resp["totals"] = tot;
  }
  // one iteration's tickers (the median-by-plan-wall iteration), never a sum over iterations;
  // the histograms are cumulative since the store was (re)opened -- a snapshot, not a delta
  json rdb = json::object();
  rdb["statistics_delta"] = plan_deltas[pmid];
  if (!opts.slim) rdb["statistics_delta_note"] = "the median-wall measured iteration, one plan run";
  if (cfg.emit_histograms) {                       // off: both fields absent (RunConfig::emit_histograms)
    rdb["histograms_delta"] = plan_hdeltas[pmid];  // same iteration: per-histogram count+sum moved
    rdb["histograms_since_open"] = store.histograms();
  }
  if (cfg.sample_every) resp["op_samples"] = pool.take_op_samples();
  resp["rocksdb"] = rdb;
  if (free_rows)                     // off: absent, the response of before the option
    // an UPPER bound of what was released: a later step still holding a copy of a row keeps that row alive
    resp["freed"] = {{"steps", freed_steps}, {"bytes", freed_bytes}};
  if (cfg.kv_timing)                 // off: absent, the response of before the option
    resp["kv_timing"] = {{"clock", "steady_clock"}, {"timer_pair_ns", kv_timer_pair_ns()},
                         {"boundary", "Get, NewIterator + Seek / SeekForPrev, Next / Prev, iterator delete, Put, Delete"}};
  if (!opts.slim) resp["run"] = opts.run_echo ? *opts.run_echo : run_echo(cfg, store);
  resp["error"] = nullptr;
  return resp;
}

}  // namespace evalcpp
