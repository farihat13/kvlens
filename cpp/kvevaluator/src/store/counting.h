// The counting seam: every KV op goes through here and is tallied with the exact cursor
// (A change to WHAT an op is charged here -- ops, byte channels, miss lanes, the overlay merge -- bumps
// kCountingSeamVersion below, or kWriteCountingSeamVersion for the write path: a capture reuses cells while the
// binary states the versions it was measured under (python/kvevaluator/measure/counting.py), and
// `just golden-counts` fails a moved count with no bump. A format's own operator rules have their own pair,
// formats/registry.cc kFormatCounting.)
// convention of Python store/kvstore.py:
//   seek()       lands at the first key >= target (1 SEEK, landing key bytes charged once
//                per position)
//   next()/prev() 1 NEXT each, charging the new landing key
//   key()        free
//   value()      charges its raw length on every call
//   get()        1 GET, charging the lookup key and the value on a hit; on a miss the key goes
//                to get_miss_key (G_miss) instead
//   mark_miss()  the operator declares the current landing is NOT what it asked for (a point
//                SEEK that landed on a foreign key, an empty prefix, the NEXT past a range's
//                end): the landing key moves from scan_key to seek_miss_key / next_miss_key,
//                S_miss / N_miss count it, and value() at that landing throws
//   seek_exact() seek + the exact-key check + mark_miss on a miss (the point-lookup idiom)
//   put()        1 PUT into the shadow overlay, charging the key and value bytes
//   del_key()    1 DELETE into the overlay (a tombstone), charging the key bytes
// Every GET, range read (one cursor's life), PUT and DELETE also adds one entry to an KV-pair size histogram
// (store/size_hist.h), per store part, under the same pending / commit / discard rule as the per-part counts below.
// A CountingStore may carry a ShadowOverlay (write runs): reads then merge the overlay with the
// base exactly as Python's WriteShadowKV does -- overlay wins a tie, tombstones hide base keys,
// a cursor never flips direction -- and the counts are of the MERGED view (counting sits above
// the shadow). An empty overlay costs one branch per op.
#pragma once
#include <rocksdb/db.h>

#include <cstdint>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

#include "store/rocksdb_store.h"
#include "store/reuse_distance.h"
#include "store/shadow.h"
#include "codec/decode_counts.h"
#include "store/round_trips.h"
#include "store/size_hist.h"
#include "value/value.h"

namespace evalcpp {

// The seam's counting versions, as `kvevaluator --version` and every run response state them ("counting"). 4 and 1
// continue the hand constants they replace (the Python MEASUREMENT_VERSION and WRITE_MEASUREMENT_VERSION).
constexpr int kCountingSeamVersion = 4;        // reads: what a SEEK / GET / NEXT and each byte lane charge
constexpr int kWriteCountingSeamVersion = 1;   // writes: a write's reads, PUTs and DELETEs (1: the unit's own key looked up first)
                                               // (the last-chunk lane changes no count: not a bump; the
                                               // writes capture's shape is, grid_writes.CAPTURE_SHAPE 11)


struct Counts {
  uint64_t S = 0, G = 0, N = 0, P = 0, D = 0;
  uint64_t get_value = 0, scan_value = 0, scan_key = 0, get_key = 0;   // read bytes (KVCost's split);
                                                                       // key bytes of HIT landings only
  // absence misses: ops whose landing was not the asked-for key/range (subsets of S/G/N --
  // the totals stay the op counts) and the foreign landing key's bytes, per landing op. A
  // miss never reads a value (guarded). The rocksdb-comparable scan total is
  // scan_key + seek_miss_key + next_miss_key + scan_value; get_key + get_miss_key = the
  // point-lookup key lens. Kept apart from read so key read-amp measures granularity only,
  // while the miss cost stays reported (a format signal: dgraph pays a foreign posting key
  // for "no", a row store one edge key per scan end).
  uint64_t S_miss = 0, G_miss = 0, N_miss = 0;
  uint64_t seek_miss_key = 0, next_miss_key = 0, get_miss_key = 0;
  uint64_t write_key = 0, write_value = 0;                             // write bytes (P/D lanes)
  // the KV-amplification metric's used channels (operator-annotated, format-framed packed
  // currency): the bytes the op logically NEEDED of what it fetched -- the store owns
  // read/write above, the OPERATOR owns used via CountingStore::count_used.
  // used_write_*: the write-side twin (the LOGICAL change of what was put -- the changed
  // field/entry vs the whole rewritten posting/row; write_extra = write - used_write)
  uint64_t used_key = 0, used_value = 0;
  uint64_t used_write_key = 0, used_write_value = 0;
  // AUDIT tallies (never part of KVCost; not in to_json's channels): raw_gets = the UNCOUNTED
  // raw_get reads served from the base store (setup / the hybrids' sortvals fallback; a write's
  // read-modify-write reads are counted) -- every such CALL moves RocksDB's
  // keys.read (RocksDB ticks NUMBER_KEYS_READ per Get call, hit or miss), so the apply-mode self-check needs
  // them (tickers.get == G + raw_gets); rocks_write_bytes =
  // the exact bytes RocksDB books for the single-op write batches apply mode issues (12-byte
  // batch header + 1 type byte + varint32 lens + key + value), so bytes.written can be asserted.
  uint64_t raw_gets = 0, rocks_write_bytes = 0;
  // raw_seeks: the UNCOUNTED seeks of raw_last_key (the last-chunk probe), audited as raw_gets is
  // (tickers.seek == S + raw_seeks)
  uint64_t raw_seeks = 0;
  // a write's touches of a Chunked family's LAST chunk (exec/chunk_write.cc): raw channels beside the
  // lanes above (each byte count is already inside scan_value / write_value), so a cost view can recompute
  // them (python/kvcost/layout_cost/compose.py chunk_view, where the reason is written). Counts runs only
  struct LastChunk {
    uint64_t reads = 0, read_bytes = 0, read_target = 0;                     // read touches; value bytes; targets summed
    uint64_t writes = 0, write_bytes = 0, write_base = 0, write_target = 0;  // write touches; bytes put; the chunk before; targets
    void add(const LastChunk& o);
    LastChunk since(const LastChunk& b) const;
    bool any() const { return reads || writes; }
    bool operator==(const LastChunk& o) const {
      return reads == o.reads && read_bytes == o.read_bytes && read_target == o.read_target && writes == o.writes &&
             write_bytes == o.write_bytes && write_base == o.write_base && write_target == o.write_target;
    }
    json to_json() const;
  } last_chunk;
  void add(const Counts& o);
  Counts since(const Counts& before) const;   // this - before, every field (the per-task delta)
  json to_json() const;
};

class CountingStore;

// PER-PART attribution (experiments/layout_choice: price a per-type layout without building it).
// A format that can name the store PART a key belongs to (meta: a label's props, one adjacency
// list family, one index -- formats/meta/keys.h) hands the counting store a PartScheme; every
// charged op is then ALSO added to its part's bucket, with the same op counts and the same byte
// lanes. Which part an op goes to -- always the part that was ASKED about:
//   GET / PUT / DELETE        the part of the key it names
//   a SEEK or NEXT landing    the part of the cursor's seek TARGET, hit or miss (a miss's foreign
//                             key belongs to no question of the part it happens to sit in; the
//                             chunk replay, exec/replay.h, uses the same rule). Only when the target
//                             decodes to no part (id 0, the catch-all) does a landing go to the part
//                             of the key it landed on.
// The per-part buckets of a step sum EXACTLY to the step's counters on every attributed lane
// (S G N P D, the miss counts, get/scan key+value, the miss key lanes, write key+value); the
// executor asserts it. The used_* lanes and the audit tallies are not attributed. No scheme (the
// default, every format but meta) = nothing recorded, one null-pointer branch per op.
struct PartScheme {
  virtual ~PartScheme() = default;
  virtual size_t size() const = 0;                          // part ids are [0, size())
  // total: id 0 is the CATCH-ALL ("unknown") for a key that decodes to no part (never expected; the
  // Python checks refuse it), so the sums always close
  virtual size_t part_of(std::string_view key) const = 0;
  virtual std::string name(size_t id) const = 0;
};
using PartCounts = std::vector<Counts>;                     // indexed by part id

// Stage B sampler: every `every`-th call of each op kind gets an
// rdtsc pair around the ENGINE call only (never the counting/merge bookkeeping), bucketed
// into log2-of-ticks histograms. Per CountingStore = per worker slot, no contention. every=0
// (the default) is fully off -- the counts and results are byte-identical either way (gated
// by a sampling-neutral check); the executor arms it only on MEASURED iterations.
struct OpSampler {
  enum Kind { kGet = 0, kSeek, kSeekForPrev, kNext, kPrev, kKinds };
  // quarter-octave buckets (2 mantissa bits): index = 4*floor(log2 t) + next-2-bits. An
  // octave is too coarse for an equality claim between op kinds; quarter
  // octaves resolve a 1.19x difference. Keys are emitted as the raw INDEX (+ ns_per_tick),
  // never pre-converted ns strings -- a wobbly calibration must not split buckets.
  static constexpr int kBuckets = 4 * 48;
  uint64_t every = 0;                              // 0 = off
  uint64_t seen[kKinds] = {};
  uint64_t buckets[kKinds][kBuckets] = {};
  bool tick(int k) { return every && (seen[k]++ % every) == 0; }
  void record(int k, uint64_t ticks) {
    int t = 0;
    while ((uint64_t(1) << (t + 1)) <= ticks && t < 47) t++;
    int m = t >= 2 ? int((ticks >> (t - 2)) & 3) : 0;
    buckets[k][t * 4 + m]++;
  }
  void add(const OpSampler& o) {
    for (int k = 0; k < kKinds; k++) {
      seen[k] += o.seen[k];
      for (int b = 0; b < kBuckets; b++) buckets[k][b] += o.buckets[k][b];
    }
  }
  // bucket idx -> lower edge in ticks: 2^t * (1 + m/4)
  static double edge_ticks(int idx) {
    int t = idx / 4, m = idx % 4;
    return double(uint64_t(1) << t) * (1.0 + m / 4.0);
  }
};

class Cursor {
 public:
  // epoch: the write epoch the iterator's view is of (CountingStore's pool refreshes an older one before reuse)
  Cursor(std::unique_ptr<rocksdb::Iterator> it, CountingStore* owner, std::string_view target, int dir, uint64_t epoch);
  Cursor(Cursor&&) = default;
  ~Cursor();                          // the last landing, if it was a hit never read, is a key crossed
  bool valid() const;
  std::string_view key() const;       // free (charged on landing)
  std::string_view value();           // charges scan_value bytes ONCE per landing; a second
                                      // call at the same landing THROWS (the read-once guard:
                                      // a double-decode must never inflate read bytes --
                                      // borrow the first read instead)
  void next();                        // 1 NEXT (throws on a cursor from seek_for_prev)
  void prev();                        // 1 NEXT (throws on a cursor from seek)
  void mark_miss();                   // this landing is outside what was asked for (header note);
                                      // throws if its value was already read or it is marked twice
 private:
  void charge_key();
  void close_landing();               // keys_crossed: the landing just left was a hit whose value nobody read
  uint32_t ask_pid_ = 0, land_pid_ = 0;           // the part ids of ask_part_ / land_part_ (0 without a scheme)
  bool overlay_leads() const;         // is the overlay entry the merged head? (ties: overlay)
  bool ovalid() const;
  void settle();                      // skip tombstoned heads (consuming shadowed base copies)
  void step_base();
  void advance();
  std::unique_ptr<rocksdb::Iterator> it_;
  uint64_t it_epoch_ = 0;                         // the DB sequence number it_ was created or refreshed at (pooled)
  CountingStore* owner_;
  bool value_read_ = false;                       // the read-once guard's per-landing flag
  bool missed_ = false;                           // mark_miss() called at this landing
  bool from_seek_ = true;                         // this landing came from the seek (else a next/prev)
  Counts* ask_part_ = nullptr;                    // parts on: the seek target's bucket (every landing goes here)
  bool ask_catchall_ = false;                     // ... unless the target decodes to no part: then the landed key's
  Counts* land_part_ = nullptr;                   // parts on: this landing's bucket
  // this cursor's range read in the KV-pair size histograms: its bytes and keys add up in a slot of the store
  // (CountingStore::LiveRange), two additions per landing; the entry is written when the cursor dies, or at the
  // task's commit / discard if the cursor is still alive then (a task takes its counts before its cursors die)
  uint32_t rr_slot_ = 0;
  int reuse_bucket_ = 0;                          // the reuse-distance pass: this landing's bucket (its value adds to it)
  uint64_t reuse_kvpair_bytes_ = 0;                 // and the landing's KV pair bytes so far (its blocks grow with the value)
  std::unique_ptr<uint64_t[]> reuse_req_;         // and this range read's bytes per bucket (allocated by the pass only)
  uint64_t rr_pages_ = 0;                         // pages after the first already counted as requests (round_trips.h)
  uint64_t rt_bytes_ = 0;                         // the kvbytes this range read returned so far (round trips on only)
  void rt_grow(uint64_t n);
  int dir_;                                     // +1 = from seek (next), -1 = from seek_for_prev (prev)
  const ShadowOverlay::Map* ov_ = nullptr;        // null: no overlay in play (the fast path)
  ShadowOverlay::Map::const_iterator oi_;         // first at-or-after (fwd) / at-or-before (bwd) the base
  bool obegin_ = false;                           // bwd: oi_ has walked past the first entry
};

class CountingStore {
 public:
  // apply_writes: put/del go to the REAL store (WAL on, sync off) instead of an overlay --
  // the write-latency mode. Counts are identical either way (that equality is the mode's
  // cross-check); the session only allows it on a checkpoint-managed arena dir.
  explicit CountingStore(Store& store, ShadowOverlay* overlay = nullptr, bool apply_writes = false)
      : store_(store), overlay_(overlay), apply_(apply_writes) {
    if (overlay_ && apply_) throw std::runtime_error("CountingStore: overlay and apply_writes are exclusive");
  }
  std::optional<std::string> get(std::string_view key);   // 1 GET
  Cursor seek(std::string_view key);                       // 1 SEEK: first key >= key
  // the point-lookup idiom: 1 SEEK, the whole value if the landing IS `key`, else a miss
  std::optional<std::string> seek_exact(std::string_view key);
  Cursor seek_for_prev(std::string_view key);              // 1 SEEK: last key <= key
  void put(std::string_view key, std::string_view value);  // 1 PUT (overlay only; throws without one)
  // 1 DELETE (a tombstone in the overlay). Its histogram entry is the deleted KV pair's key + value bytes: kvpair_bytes
  // when the caller has them in hand, else the size this run saw when it read or put that key (ReadSizes), else the
  // "unknown" count (never a read to learn it)
  void del_key(std::string_view key, int64_t kvpair_bytes = -1);
  // a WRITE's read-modify-write read of a posting (the current value a write rewrites): the
  // posting stores' point-read idiom, seek_exact (1 SEEK; a miss is the absence lane) -- every read
  // a write does is charged -- with the key as its used bytes (an ideal store adds one entry
  // without reading the rest: the value read is RMW amp). Split units: unit::rmw_locate / rmw_read.
  std::optional<std::string> rmw_seek(std::string_view key);
  // UNCOUNTED overlay-aware read: setup, and a value the op already holds (a date it read or
  // scanned earlier in the op, a sort comparison); tallied as raw_gets so the ticker check balances
  std::optional<std::string> raw_get(std::string_view key);
  // UNCOUNTED overlay-aware: the last key with `prefix` at or before `bound` (a chunk family's final chunk key), or
  // nullopt. One base seek, audited as raw_seeks. The last-chunk probe of exec/chunk_write.cc (counts runs only)
  std::optional<std::string> raw_last_key(std::string_view prefix, std::string_view bound);
  // a write's touch of a Chunked family's last chunk (Counts::LastChunk), on the step and on `key`'s part
  void note_last_chunk_read(std::string_view key, uint64_t value_bytes, uint64_t target);
  void note_last_chunk_write(std::string_view key, uint64_t value_bytes, uint64_t base_bytes, uint64_t target);
  bool kvpair_stats() const { return kvpair_stats_; }
  // the operator's used-annotation seam (KV amplification): called at the extraction point
  // where the op consumes what it fetched -- key_n/value_n in the format's OWN packed
  // currency, so used <= read by construction (gated, not assumed)
  void count_used(uint64_t key_n, uint64_t value_n) { c_.used_key += key_n; c_.used_value += value_n; }
  // the write-side twin: the logical change within what was put (the 3-case write rules)
  void count_used_write(uint64_t key_n, uint64_t value_n) {
    c_.used_write_key += key_n; c_.used_write_value += value_n;
  }
  Counts& counts() { return c_; }
  void reset() { c_ = Counts(); }
  // the end of a job (StepExec, main thread, no worker running): the pooled iterators are deleted, so no iterator --
  // its pinned block, its position -- outlives the step that made it (nothing is cached between steps)
  void end_step();
  // the iterator pool (below) on: only the executor's worker stores (exec/executor.cc), whose every step ends in
  // end_step(). Off (the default), each cursor deletes its iterator, as before the pool
  void pool_iterators(bool on) { pool_iters_ = on; }
  // the pooled iterators' deletes (end_step), timed with the KV-boundary timer: they run after the workers' perf
  // windows closed, so execute adds them to the step's kv block (kv_iter_close_*) itself. Returns and zeroes them
  std::pair<uint64_t, uint64_t> take_closed() { auto r = std::make_pair(closed_ns_, closed_calls_); closed_ns_ = closed_calls_ = 0; return r; }
  ~CountingStore() { end_step(); }               // a pooled iterator never outlives its store's DB
  CountingStore(CountingStore&&) = default;      // the worker slots hold them by value (the destructor above hides it)
  // --- per-part attribution (PartScheme above). Every op lands in PENDING; a task whose counts are
  // logical (direct_counts, an additive range op) COMMITS it, anything else (a replayed chunk
  // reader's physical seeks -- its parts come from the replay) is DISCARDED. The StepExec drives
  // both between jobs, so pending never mixes two tasks' ops.
  void set_part_scheme(const PartScheme* s);
  const PartScheme* part_scheme() const { return scheme_; }
  // pending -> committed. `expect`: the task's direct delta -- the pending parts must sum to it
  // exactly on the attributed lanes (throws otherwise: a `before` taken mid-task)
  void parts_commit(const Counts* expect = nullptr);
  void parts_discard();
  void take_committed_parts(PartCounts& into);   // adds committed into `into` (sized to the scheme), zeroes it
  // the KV-pair size histograms follow parts_commit / parts_discard with or without a scheme (one part, id 0, without)
  void take_committed_hists(HistParts& into);    // adds committed into `into`, zeroes it
  // DECODE COUNTS PER PART (codec/decode_counts.h; parts on only): what this worker's thread decoded since the
  // last store op goes to the part that op touched. Physical, like the codec's own counters: never discarded
  // with a task's pending counts (a chunk decoded by a replayed read was still decoded). The pool opens and
  // closes the window where it reads the thread's codec counters, so the parts sum to the step's totals.
  bool parts_on() const { return scheme_ != nullptr; }
  void decodes_begin() { if (scheme_) { dmark_ = decode_counts(); dpart_known_ = false; } }
  void decodes_attribute() {
    if (!scheme_) return;
    const DecodeCounts& now = decode_counts();
    if (now == dmark_) return;
    // before the window's first store op there is no part yet: what was decoded stays unattributed (in the step's
    // totals only) unless an op follows -- then it is that op's (decodes_part: a write parses the value it builds
    // BEFORE it puts it)
    if (dpart_known_) dparts_[dpart_].add(now.since(dmark_));
    dmark_ = now;
  }
  // after an op on part `id`: the decodes since the last op are the last op's; before any op, this one's
  void decodes_part(size_t id) {
    if (!scheme_) return;
    if (!dpart_known_) { dpart_ = id; dpart_known_ = true; }
    decodes_attribute();
    dpart_ = id;
  }
  void decodes_at(std::string_view key);         // ... an op that touched no store (a cached chunk): the key's part
  // ... a decode credited elsewhere (a shared chunk's parse: ChunkParse): kept out of this thread's window
  void decodes_exclude(const DecodeCounts& d) { if (scheme_) dmark_.add(d); }
  void take_decode_parts(std::vector<DecodeCounts>& into);   // adds into `into` (sized to the scheme), zeroes
  // --- round trips (store/round_trips.h): every GET, range read, page and write is noted at the unit's current
  // level, under the same pending / commit / discard rule as the histograms. The pool starts a unit before each
  // source of a fan-out and before each task.
  void rt_unit() { rt_level_ = 0; }
  // the remote regime, mode operation: what this store's requests waited (measured), taken per job; and the
  // bandwidth share too small to sleep on its own (under 100 us), which the next wait carries
  uint64_t take_remote_wait_ns() { uint64_t w = remote_wait_ns_; remote_wait_ns_ = 0; return w; }
  uint64_t take_remote_owed_ns() { uint64_t w = uint64_t(remote_owed_ns_); remote_owed_ns_ = 0; return w; }
  void take_committed_round_trips(RtLevels& into) { if (!rt_committed_.empty()) { into.add(rt_committed_); rt_committed_.clear(); } }
  void set_read_sizes(ReadSizes* r) { sizes_ = r; }   // a write plan's run (null: a read-only plan)
  // run.emit.kvpair_stats: the per-request bookkeeping of the read and write paths (the KV-pair size histograms, a
  // cursor's range read and its crossed keys). On for a counts run; a latency run turns it off, and then a landing
  // and a GET cost one untaken branch more than before the histograms existed
  void set_kvpair_stats(bool on) { kvpair_stats_ = on; }
  void set_round_trips(bool on) { rt_on_ = on; }   // on with kvpair_stats, and whenever the remote regime waits
  // the opt-in reuse-distance pass (store/reuse_distance.h; P=1): every fetched KV pair is looked up in `t` and its
  // bytes are added to its distance bucket here; the executor takes the buckets per step
  void set_reuse_tracker(ReuseTracker* t) { reuse_ = t; }
  uint64_t* reuse_bytes() { return reuse_bytes_; }
  uint64_t* reuse_blocks() { return reuse_blocks_; }   // per bucket: the sum of each KV pair's own 4 KB blocks
  uint64_t* reuse_request_blocks() { return reuse_req_blocks_; }   // per bucket: each request's bytes rounded up once
  OpSampler& sampler() { return sampler_; }
  Store& store() { return store_; }
  ShadowOverlay* overlay() { return overlay_; }
 private:
  friend class Cursor;
  std::optional<std::string> base_get(std::string_view key);
  // the iterator pool: a SEEK takes an idle base iterator (made at the first SEEK of a job) instead of a new one per
  // SEEK; a cursor's death gives it back. An engine keeps an iterator per reader; which iterator object serves a SEEK
  // is the engine's cost, not the format's (S/G/N and bytes are booked here, not by RocksDB). One made before the DB's
  // latest sequence number (a write since, by any path) is refreshed first. Emptied by end_step()
  std::pair<std::unique_ptr<rocksdb::Iterator>, uint64_t> take_iter();
  void give_iter(std::unique_ptr<rocksdb::Iterator> it, uint64_t epoch);
  std::vector<std::pair<std::unique_ptr<rocksdb::Iterator>, uint64_t>> iter_pool_;
  bool pool_iters_ = false;
  uint64_t closed_ns_ = 0, closed_calls_ = 0;    // end_step's deletes, timed (take_closed)
 public:
  // run.emit.pinned_usage: the block cache's pinned and total bytes at the last end_step with a pool, read before its
  // deletes (what the idle pooled iterators held at the step's end). Off: not read
  bool read_pinned = false;
  size_t pinned_at_end = 0, usage_at_end = 0;
 private:
  // the pending bucket of `key`'s part, or null when no scheme is set (the fast path); `catchall`
  // (optional) = the key decoded to no part (id 0)
  Counts* part(std::string_view key, bool* catchall = nullptr) {
    if (!scheme_) return nullptr;
    pending_dirty_ = true;
    size_t id = scheme_->part_of(key);
    last_part_ = id;
    if (catchall) *catchall = id == 0;
    return &pending_[id];
  }
  // the pending histograms of part `id` (0 without a scheme), marked dirty
  KvPairHists& hist(size_t id) {
    if (!hflag_[id]) { hflag_[id] = 1; hdirty_.push_back(uint32_t(id)); }
    return hpending_[id];
  }
  size_t last_part() const { return scheme_ ? last_part_ : 0; }   // the part id part() just decoded (one decode per op)
  friend class RtParallel;
  // round trips are noted with run.emit.kvpair_stats on, or when the remote regime runs (its waits follow them)
  bool rt_on_ = true;
  void rt_get() { if (rt_on_) rt_pending_.at(rt_level_++).gets++; }
  void rt_range() {
    if (!rt_on_) return;
    rt_pending_.at(rt_level_++).ranges++;
    if (remote().mode == Remote::kOperation) remote_request(0);
  }
  void remote_request(uint64_t bytes);           // mode operation: one request's wait (+ the bytes it fetched)
  void remote_bytes(uint64_t bytes);             // mode operation: bytes a range read fetched since its last wait
  uint64_t remote_wait_ns_ = 0;
  double remote_owed_ns_ = 0;
  void rt_page(uint64_t n) { rt_pending_.pages_extra += n; for (; n; n--) rt_range(); }
  RtLevels rt_pending_, rt_committed_;
  size_t rt_level_ = 0;                          // the level the unit's next request is noted at
  void hists_commit();
  void hists_discard();
  Store& store_;
  ShadowOverlay* overlay_;
  bool apply_ = false;
  Counts c_;
  HistParts hpending_ = HistParts(1), hcommitted_ = HistParts(1);
  std::vector<uint8_t> hflag_ = std::vector<uint8_t>(1, 0), hcflag_ = std::vector<uint8_t>(1, 0);
  std::vector<uint32_t> hdirty_, hcdirty_;       // the part ids that hold an entry (pending / committed)
  size_t last_part_ = 0;
  ReadSizes* sizes_ = nullptr;
  bool kvpair_stats_ = true;
  ReuseTracker* reuse_ = nullptr;
  uint64_t reuse_bytes_[ReuseTracker::kBuckets] = {};
  uint64_t reuse_blocks_[ReuseTracker::kBuckets] = {};
  uint64_t reuse_req_blocks_[ReuseTracker::kBuckets] = {};
  // the range reads of the cursors alive now (a handful at most): what each returned so far
  // cross: the cursor's CURRENT landing is on a key, not declared a miss, its value not read: a key crossed once the
  // cursor leaves it, dies, or its task commits (cross_part: that landing's part)
  struct LiveRange { uint64_t bytes = 0, keys = 0; uint32_t part = 0; bool in_use = false, open = false;
                     bool cross = false; uint32_t cross_part = 0;
                     // the read's KV pairs for the store-block count: the landing's KV pair so far, and the KV pairs left behind
                     uint64_t kvpair = 0, big = 0, small = 0;
                     void kvpair_done() { if (kvpair >= 4096) big++; else small += kvpair; kvpair = 0; } };
  std::vector<LiveRange> live_;
  std::vector<uint32_t> live_free_;
  uint32_t live_open_ = 0;
  uint32_t range_open(size_t part);              // a cursor's range read begins
  void range_close(uint32_t slot);               // its cursor died: the entry, unless a commit or discard took it
  void ranges_flush(bool record);                // commit: every open read becomes an entry; discard: dropped
  std::vector<DecodeCounts> dparts_;             // by part id; empty without a scheme
  DecodeCounts dmark_;                           // the thread's decode counters at the last attribution
  size_t dpart_ = 0;                             // the part the decodes since then belong to
  bool dpart_known_ = false;                     // a store op happened in this window
  OpSampler sampler_;
  const PartScheme* scheme_ = nullptr;
  PartCounts pending_, committed_;
  bool pending_dirty_ = false, committed_dirty_ = false;
};

// The requests of each member do not need the answers of the other members': every member starts at the level the
// scope was opened at, and the scope ends at the deepest level a member reached. Inside a member the default holds
// (each request needs the one before it). Nests.
//   RtParallel par(store);
//   for (auto& key : keys) { par.member(); store.get(key); }     // one batch of GETs: one level
class RtParallel {
 public:
  explicit RtParallel(CountingStore& s) : s_(s), base_(s.rt_level_), end_(s.rt_level_) {}
  void member() { if (s_.rt_level_ > end_) end_ = s_.rt_level_; s_.rt_level_ = base_; }
  ~RtParallel() { if (s_.rt_level_ < end_) s_.rt_level_ = end_; }
  RtParallel(const RtParallel&) = delete;
  RtParallel& operator=(const RtParallel&) = delete;
 private:
  CountingStore& s_;
  size_t base_, end_;
};

// the lanes a part bucket carries (the attributed ones); used by the sum checks
bool parts_sum_equal(const PartCounts& parts, const Counts& total, std::string* why = nullptr);
void add_parts(PartCounts& into, const PartCounts& from);

}  // namespace evalcpp
