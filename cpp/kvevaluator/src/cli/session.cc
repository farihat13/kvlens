#include "cli/session.h"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <stdexcept>
#include <thread>

#include "exec/executor.h"
#include "formats/common.h"
#include "ingest/ingest.h"
#include "ir/plan.h"
#include "provenance.h"

namespace evalcpp {

void Session::open(const json& sr) {
  std::string dir = sr.at("dir").get<std::string>();
  std::string schema_path = sr.at("schema").get<std::string>();
  std::string format = sr.at("format").get<std::string>();
  size_t cache_bytes = sr.value("block_cache_bytes", StoreOptions().block_cache_bytes);   // defaults: store_config.json
  bool direct_io = sr.value("direct_io", false);
  bool cache_if = sr.value("cache_index_and_filter_blocks", StoreOptions().cache_index_and_filter_blocks);
  size_t write_buffer = sr.value("write_buffer_bytes", StoreOptions().write_buffer_bytes);
  bool auto_compactions = sr.value("auto_compactions", false);
  if (store_ && dir == dir_ && schema_path == schema_path_ && format_ && format_->name() == format &&
      store_->options().block_cache_bytes == cache_bytes && store_->options().direct_io == direct_io &&
      store_->options().cache_index_and_filter_blocks == cache_if &&
      store_->options().write_buffer_bytes == write_buffer && store_->options().auto_compactions == auto_compactions) {
    // the store stays open across requests; a request naming ANOTHER scale for it is still refused
    std::string r_scale = sr.value("scale", "");
    if (!r_scale.empty() && !scale_.empty() && r_scale != scale_)
      throw std::runtime_error("store " + dir + " is at scale '" + scale_ + "' (its manifest, or the first request), "
                               "the request says '" + r_scale + "'");
    if (scale_.empty()) scale_ = r_scale;   // an unstamped store whose first request named no scale: adopt this one
    return;
  }
  store_.reset();
  format_.reset();
  schema_.reset();
  json m = read_manifest(dir);
  // the store's scale (the dataset tag it was built from: "0.1" / "3" / "30" / "n1m"): the manifest's `scale` and the
  // request's store.scale (the driver chose the store dir by it); both when present must agree; it selects a
  // scale-tagged bundle variant (ir/plan.cc select_variant). An old manifest has none: the request's stands.
  std::string m_scale = m.value("scale", ""), r_scale = sr.value("scale", "");
  if (!m_scale.empty() && !r_scale.empty() && m_scale != r_scale)
    throw std::runtime_error("store " + dir + " was built at scale '" + m_scale + "', the request says '" + r_scale + "'");
  scale_ = m_scale.empty() ? r_scale : m_scale;
  if (m.value("format", "") != format)
    throw std::runtime_error("store " + dir + " holds format '" + m.value("format", "") + "', request says '" + format + "'");
  if (!m.value("settled", false)) throw std::runtime_error("store " + dir + " is not settled");
  // key-layout stamp: a store written under another key layout must refuse loudly (see
  // Format::key_codec) -- reads would otherwise miss silently with correct-looking op counts
  auto schema = std::make_unique<Schema>(Schema::load(schema_path));
  if (schema->hash() != m.at("schema").value("hash", ""))
    throw std::runtime_error("schema hash mismatch: store was built with " + m["schema"].value("hash", "").substr(0, 12) +
                             ", " + schema_path + " has " + schema->hash().substr(0, 12));
  // the dataset hash excludes the adj/prop_groups blocks, so it alone would let an
  // in-place edit of only those lines run against a stale store; the content_sha pins the
  // whole file. Absent on pre-stamp manifests (older stores): skip, the hash still holds.
  std::string want_sha = m["schema"].value("content_sha", "");
  if (!want_sha.empty() && want_sha != schema->content_sha())
    throw std::runtime_error("schema content mismatch (adj/prop_groups changed since ingest?): "
                             "store " + dir + " was built with " + want_sha.substr(0, 12) + ", " +
                             schema_path + " has " + schema->content_sha().substr(0, 12) +
                             " -- re-ingest the store");
  StoreOptions so = store_options_of(m, dir);
  so.block_cache_bytes = cache_bytes;
  so.direct_io = direct_io;
  so.cache_index_and_filter_blocks = cache_if;
  so.write_buffer_bytes = write_buffer;
  if (auto_compactions) {
    // a compaction rewrites the store's files: like a real write, only a checkpoint-managed ARENA may take it
    std::ifstream marker(dir + "/arena.json");
    if (!marker.good())
      throw std::runtime_error("store.auto_compactions needs a checkpoint-managed arena (no " + dir + "/arena.json)");
  }
  so.auto_compactions = auto_compactions;
  // read-write (this fork's read-only Get path records no tickers); written
  // ONLY by the apply-writes mode, which the arena.json guard below confines to arenas
  store_ = Store::open_read_write(dir + "/db", so, false);
  format_ = make_format(format, *schema, m.value("value_codec", json::object()));
  check_store_codec(m, *format_, dir, "session");   // key layout + the config round trip (ingest/ingest.h)
  format_->warm(*store_);                   // derived state outside any measured window
  schema_ = std::move(schema);
  manifest_ = m;
  dir_ = dir;
  schema_path_ = schema_path;
}

namespace {
uint64_t now_ns() {
  return uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::steady_clock::now().time_since_epoch()).count());
}
}  // namespace

// The store's LSM shape (files and bytes per level, each file's name and size) and its background work (flushes and
// compactions running or pending), read inside the session that holds the store open (`sst-props` would need the
// LOCK). opt.wait_idle_s > 0: first wait, polling every 10 ms, until no flush or compaction runs or is pending.
json Session::lsm_state(const json& opt) {
  rocksdb::DB* db = store_->db();
  auto prop = [db](const char* name) { uint64_t v = 0; db->GetIntProperty(name, &v); return v; };
  auto busy = [&] {
    return prop("rocksdb.num-running-compactions") + prop("rocksdb.num-running-flushes") +
           prop("rocksdb.compaction-pending") + prop("rocksdb.mem-table-flush-pending");
  };
  double wait_s = opt.is_object() ? opt.value("wait_idle_s", 0.0) : 0.0;
  uint64_t t0 = now_ns();
  while (wait_s > 0 && busy() && double(now_ns() - t0) < wait_s * 1e9)
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  uint64_t waited = now_ns() - t0;
  rocksdb::ColumnFamilyMetaData cf;
  db->GetColumnFamilyMetaData(&cf);
  json levels = json::array();
  for (auto& l : cf.levels) {
    if (l.files.empty()) continue;
    json files = json::array();
    for (auto& f : l.files) files.push_back({{"name", f.name}, {"bytes", f.size}});
    levels.push_back({{"level", l.level}, {"files", l.files.size()}, {"bytes", l.size}, {"file_list", files}});
  }
  return {{"levels", levels}, {"files", cf.file_count}, {"bytes", cf.size},
          {"running_compactions", prop("rocksdb.num-running-compactions")},
          {"running_flushes", prop("rocksdb.num-running-flushes")},
          {"compaction_pending", prop("rocksdb.compaction-pending")},
          {"flush_pending", prop("rocksdb.mem-table-flush-pending")},
          {"pending_compaction_bytes", prop("rocksdb.estimate-pending-compaction-bytes")},
          {"immutable_memtables", prop("rocksdb.num-immutable-mem-table")},
          {"memtable_bytes", prop("rocksdb.cur-size-all-mem-tables")},
          {"idle", busy() == 0}, {"waited_ns", waited}};
}

// A batch: every line of spec.requests ({"seq", "plan", "params"}, and optionally "run" and "apply") run in file order on
// the open store, back to back in this process (no round trip between requests), one response per line to
// spec.responses (refused if it exists). spec.plans maps each plan name to its IR (sent once; bound once per name, its
// params per line). spec.run is every request's run config; a line's `run` is merged over it (its keys replace
// spec.run's). A plan with a write op applies its writes when the line's `apply` (default spec.apply_writes) is true
// (an arena only), and runs in shadow mode (the overlay; the store never changes) when it is false. Each response keeps
// what a single request's would, except: rows only with spec.emit_rows (each step's row digest stands for them when
// run.emit.row_digest is on), and no provenance block (once, in the batch's own response). Each response carries
// seq, plan and its start and end on the batch's clock (batch_t_start_ns / batch_t_end_ns), so the gap between two
// requests is measured. spec.tickers (names): each response's statistics_delta over those tickers only (absent: all).
// spec.slim: each response drops what only repeats (exec/executor.h ResponseOpts) and names its run by `run_id`, an
// index into the batch response's `runs`. The batch's response: counts, the batch wall, and the RocksDB ticker delta
// over the whole batch, every ticker (flushes, compactions, stalls included; across a cold line's reopen too, Store
// carries the counts: the reopen's own file reads land between lines, in this total and in no line's delta). The first failing request stops the
// batch and names its seq; the responses file then holds the lines before it (in slim mode their run_ids point into
// a `runs` that a failed batch does not return). A line's `run` replaces spec.run's keys at the top level only: a line
// `run: {"emit": {...}}` replaces the whole emit object.
json Session::batch(const json& spec) {
  const json& plans = spec.at("plans");
  const std::string in_path = spec.at("requests").get<std::string>();
  const std::string out_path = spec.at("responses").get<std::string>();
  const bool apply_default = spec.value("apply_writes", false);
  const bool emit_rows = spec.value("emit_rows", false);
  const bool slim = spec.value("slim", false);
  const json run_spec = spec.value("run", json::object());
  std::vector<size_t> ticker_idx;
  if (spec.contains("tickers")) ticker_idx = ticker_indices(spec["tickers"].get<std::vector<std::string>>());
  bool arena = false, arena_checked = false;
  auto need_arena = [&] {
    if (!arena_checked) {
      arena = std::ifstream(dir_ + "/arena.json").good();
      arena_checked = true;
    }
    if (!arena) throw std::runtime_error("a batch's apply needs a checkpoint-managed arena (no " + dir_ + "/arena.json)");
  };
  // one parsed config per distinct run (a line's run merged over spec.run), the read one and, on first use, the apply one
  struct RunEntry {
    size_t id;
    json run;
    RunConfig read;
    json read_echo;
    std::unique_ptr<RunConfig> write;
    json write_echo;
  };
  std::map<std::string, RunEntry> runs;
  std::vector<const RunEntry*> run_order;
  auto run_for = [&](const json* line_run) -> RunEntry& {
    json run = run_spec;
    if (line_run) {
      if (!line_run->is_object()) throw std::runtime_error("a line's run must be an object");
      for (auto it = line_run->begin(); it != line_run->end(); ++it) run[it.key()] = it.value();
    }
    std::string key = run.dump();
    auto f = runs.find(key);
    if (f != runs.end()) return f->second;
    RunEntry e{run_order.size(), run, RunConfig::from_json(run), json(), nullptr, json()};
    if (e.read.apply_writes) throw std::runtime_error("batch: run.apply_writes is decided per request (spec.apply_writes / a line's apply)");
    e.read_echo = run_echo(e.read, *store_);
    auto& ref = runs.emplace(key, std::move(e)).first->second;
    run_order.push_back(&ref);
    return ref;
  };
  RunEntry& spec_run = run_for(nullptr);               // spec.run is checked before the first line
  if (apply_default) {                                  // and its apply config too, so a bad one fails before any line runs
    need_arena();
    json w = spec_run.run;
    w["apply_writes"] = true;
    spec_run.write = std::make_unique<RunConfig>(RunConfig::from_json(w));
    spec_run.write_echo = run_echo(*spec_run.write, *store_);
  }
  // one bound body per plan name: the variant selection, fragment expansion and checks run once
  struct BodyEntry {
    PlanBody body;
    bool writes = false;
  };
  std::map<std::string, BodyEntry> bodies;
  if (std::filesystem::exists(out_path)) throw std::runtime_error("batch: responses file " + out_path + " exists (never overwritten)");
  std::ifstream in(in_path);
  if (!in) throw std::runtime_error("batch: cannot open requests file " + in_path);
  std::ofstream out(out_path);
  if (!out) throw std::runtime_error("batch: cannot write responses file " + out_path);
  auto caps_for = [this](const std::string& l) { return format_->store_capabilities_for(l); };
  uint64_t n = 0, writes = 0, applied = 0;
  std::vector<uint64_t> tick0 = store_->ticker_counts();
  uint64_t t0 = now_ns();
  std::string line;
  while (std::getline(in, line)) {
    if (line.empty()) continue;
    json r = json::parse(line);
    int64_t seq = r.value("seq", int64_t(n));
    const std::string name = r.at("plan").get<std::string>();
    try {
      auto b = bodies.find(name);
      if (b == bodies.end()) {
        if (!plans.contains(name)) throw std::runtime_error("no IR for plan '" + name + "' in batch.plans");
        BodyEntry e;
        e.body = prepare_plan(plans[name], format_->store_capabilities(), caps_for, scale_);
        for (auto& st : e.body.steps) e.writes = e.writes || is_write_op(st.op);
        b = bodies.emplace(name, std::move(e)).first;
      }
      const json* params = r.contains("params") && !r["params"].is_null() ? &r["params"] : nullptr;
      Plan plan = bind_plan(b->second.body, params, *schema_);
      const bool plan_writes = b->second.writes;
      RunEntry& re = run_for(r.contains("run") ? &r["run"] : nullptr);
      const bool apply = plan_writes && r.value("apply", apply_default);   // a read plan never applies
      if (apply) {
        need_arena();
        if (!re.write) {
          json w = re.run;
          w["apply_writes"] = true;
          re.write = std::make_unique<RunConfig>(RunConfig::from_json(w));
          re.write_echo = run_echo(*re.write, *store_);
        }
      }
      ResponseOpts opts;
      opts.rows = emit_rows;
      opts.slim = slim;
      opts.tickers = spec.contains("tickers") ? &ticker_idx : nullptr;
      opts.run_echo = apply ? &re.write_echo : &re.read_echo;
      uint64_t ts = now_ns() - t0;
      json resp = execute(*format_, *store_, plan, apply ? *re.write : re.read, opts);
      uint64_t te = now_ns() - t0;
      resp["seq"] = seq;
      resp["plan"] = plan.name;
      resp["body"] = plan.body_name.empty() ? plan.name : plan.body_name;
      resp["body_sha"] = plan.body_sha;
      resp["batch_t_start_ns"] = ts;
      resp["batch_t_end_ns"] = te;
      if (slim) {
        resp["run_id"] = re.id;
        if (plan_writes) resp["applied"] = apply;      // which run echo the line took: runs[run_id].read / .apply
      }
      out << resp << "\n";                               // streamed: no intermediate string
      n++;
      writes += plan_writes;
      applied += apply;
    } catch (const std::exception& e) {
      throw std::runtime_error("batch request seq " + std::to_string(seq) + " (plan " + name + "): " + e.what());
    }
  }
  out.flush();
  if (!out) throw std::runtime_error("batch: writing " + out_path + " failed");
  uint64_t wall = now_ns() - t0;
  json res = {{"requests", n}, {"writes", writes}, {"reads", n - writes}, {"wall_ns", wall}, {"responses", out_path},
              {"statistics_delta", ticker_counts_delta(tick0, store_->ticker_counts())},
              {"statistics_delta_note", "the whole batch: every request and the background work (flushes, compactions) "
                                        "that ran during it"}};
  if (applied != writes) res["applied"] = applied;   // some write lines ran in shadow mode
  if (slim) {
    json rj = json::array();
    for (auto* e : run_order) {
      json one = {{"read", e->read_echo}};
      if (e->write) one["apply"] = e->write_echo;
      rj.push_back(one);
    }
    res["runs"] = rj;
  }
  return res;
}

json Session::handle(const json& req) {
  json resp;
  std::string where = "request";
  try {
    where = "store";
    open(req.at("store"));
    if (req.contains("lsm_state")) {
      where = "lsm_state";
      return {{"lsm_state", lsm_state(req["lsm_state"])}, {"error", nullptr}};
    }
    if (req.contains("batch")) {
      where = "batch";
      resp["batch"] = batch(req["batch"]);
      resp["provenance"] = provenance();
      resp["provenance"]["scale"] = scale_;
      resp["provenance"]["counting"] = counting_of(*format_);   // the counting versions this run is charged under
      resp["provenance"]["capabilities"] = json::array();
      for (auto& c : format_->store_capabilities()) resp["provenance"]["capabilities"].push_back(c);
      resp["provenance"]["store_manifest"] = manifest_;
      resp["provenance"]["config_hash"] = manifest_.value("value_codec_hash", "");
      resp["store_options"] = store_->options().to_json();
      resp["error"] = nullptr;
      return resp;
    }
    if (req.value("warmup_write", false)) {
      // an untimed first write, so the timed sequence after it does not carry the memtable's first allocation
      // and the WAL's first record: a Put + Delete of one key ABOVE every format's key space (8 x 0xFF), so no
      // read ever sees it. Arenas only, like every real write.
      where = "warmup_write";
      std::ifstream marker(dir_ + "/arena.json");
      if (!marker.good()) throw std::runtime_error("warmup_write needs a checkpoint-managed arena (no " + dir_ + "/arena.json)");
      std::string key(8, '\xff');
      key += "__warmup__";
      rocksdb::Status st = store_->db()->Put(rocksdb::WriteOptions(), key, "x");
      if (st.ok()) st = store_->db()->Delete(rocksdb::WriteOptions(), key);
      if (!st.ok()) throw std::runtime_error("warmup_write: " + st.ToString());
      resp["warmup_write"] = {{"key", "ffffffffffffffff" "5f5f7761726d75705f5f"}, {"ops", {"put", "delete"}}};
      return resp;
    }
    where = "plan";
    const json& ir = req.at("plan");
    const json* params = req.contains("params") && !req["params"].is_null() ? &req["params"] : nullptr;
    Plan plan = bind_plan(ir, params, format_->store_capabilities(), *schema_,
                          [this](const std::string& l) { return format_->store_capabilities_for(l); }, scale_);
    where = "run";
    RunConfig cfg = RunConfig::from_json(req.value("run", json::object()));
    if (cfg.apply_writes) {
      // real writes mutate the store: only a checkpoint-managed ARENA (a disposable
      // hard-link copy) may take them -- a measured pristine store must never be written
      std::ifstream marker(dir_ + "/arena.json");
      if (!marker.good())
        throw std::runtime_error("run.apply_writes needs a checkpoint-managed arena (no " + dir_ +
                                 "/arena.json); create one: kvevaluator checkpoint <store-dir> <arena-dir>");
    }
    resp = execute(*format_, *store_, plan, cfg);
    resp["plan"] = plan.name;
    resp["provenance"] = provenance();
    // the capability set the body was selected with, and the body it ran -- a
    // driver with no Python twin of this format (kvbench's cpp path) binds the same plan by name
    resp["provenance"]["body"] = plan.body_name.empty() ? plan.name : plan.body_name;
    resp["provenance"]["body_sha"] = plan.body_sha;        // the body's identity (plan.h body_sha_of)
    resp["provenance"]["scale"] = scale_;                   // the scale the variant was selected at ("" = unknown)        // the body's identity (plan.h body_sha_of)
    resp["provenance"]["counting"] = counting_of(*format_);   // the counting versions this run is charged under
    resp["provenance"]["capabilities"] = json::array();
    for (auto& c : format_->store_capabilities()) resp["provenance"]["capabilities"].push_back(c);
    resp["provenance"]["store_manifest"] = manifest_;
    resp["provenance"]["config_hash"] = manifest_.value("value_codec_hash", "");
  } catch (const std::exception& e) {
    resp = json::object();
    resp["rows"] = nullptr;
    resp["error"] = {{"where", where}, {"what", e.what()}};
  }
  return resp;
}

}  // namespace evalcpp
