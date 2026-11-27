#include "store/counting.h"

#include "store/perf_snapshot.h"
#include "util/tsc.h"

#include <sys/prctl.h>

#include <chrono>
#include <stdexcept>
#include <thread>

namespace evalcpp {

namespace {
// Times one RocksDB call at the API boundary while run.kv_timing is on (store/perf_snapshot.h): the scope holds
// exactly the engine call, so the timer pair is the only overhead it adds (kv_timer_pair_ns per call).
struct KvCall {
  uint64_t KvCallTimes::*ns;
  uint64_t KvCallTimes::*calls;
  uint64_t t0 = 0;
  static uint64_t now() {
    return uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
  }
  KvCall(uint64_t KvCallTimes::*n, uint64_t KvCallTimes::*c) : ns(n), calls(c) {
    if (kv_timing_on().load(std::memory_order_relaxed)) t0 = now();
  }
  ~KvCall() {
    if (!t0) return;
    uint64_t d = now() - t0;
    KvCallTimes& k = kv_call_times();
    k.kv_call_ns += d;
    k.*ns += d;
    k.*calls += 1;
  }
};
#define EVALCPP_KV_CALL(kind) KvCall kv_call_(&KvCallTimes::kv_##kind##_ns, &KvCallTimes::kv_##kind##_calls)
}  // namespace

void Counts::add(const Counts& o) {
  S += o.S; G += o.G; N += o.N; P += o.P; D += o.D;
  get_value += o.get_value; scan_value += o.scan_value; scan_key += o.scan_key; get_key += o.get_key;
  write_key += o.write_key; write_value += o.write_value;
  used_key += o.used_key; used_value += o.used_value;
  used_write_key += o.used_write_key; used_write_value += o.used_write_value;
  S_miss += o.S_miss; G_miss += o.G_miss; N_miss += o.N_miss;
  seek_miss_key += o.seek_miss_key; next_miss_key += o.next_miss_key; get_miss_key += o.get_miss_key;
  raw_gets += o.raw_gets; rocks_write_bytes += o.rocks_write_bytes;
  raw_seeks += o.raw_seeks;
  last_chunk.add(o.last_chunk);
}

void Counts::LastChunk::add(const LastChunk& o) {
  reads += o.reads; read_bytes += o.read_bytes; read_target += o.read_target;
  writes += o.writes; write_bytes += o.write_bytes; write_base += o.write_base; write_target += o.write_target;
}

Counts::LastChunk Counts::LastChunk::since(const LastChunk& b) const {
  LastChunk d;
  d.reads = reads - b.reads; d.read_bytes = read_bytes - b.read_bytes; d.read_target = read_target - b.read_target;
  d.writes = writes - b.writes; d.write_bytes = write_bytes - b.write_bytes; d.write_base = write_base - b.write_base;
  d.write_target = write_target - b.write_target;
  return d;
}

// the response's "last_chunk" block (inputs/contract/capture_columns.json response_keys last_chunk)
json Counts::LastChunk::to_json() const {
  return {{"reads", reads}, {"read_bytes", read_bytes}, {"read_target", read_target}, {"writes", writes},
          {"write_bytes", write_bytes}, {"write_base", write_base}, {"write_target", write_target}};
}

Counts Counts::since(const Counts& b) const {
  Counts d;
  d.S = S - b.S; d.G = G - b.G; d.N = N - b.N; d.P = P - b.P; d.D = D - b.D;
  d.get_value = get_value - b.get_value; d.scan_value = scan_value - b.scan_value;
  d.scan_key = scan_key - b.scan_key; d.get_key = get_key - b.get_key;
  d.write_key = write_key - b.write_key; d.write_value = write_value - b.write_value;
  d.used_key = used_key - b.used_key; d.used_value = used_value - b.used_value;
  d.used_write_key = used_write_key - b.used_write_key; d.used_write_value = used_write_value - b.used_write_value;
  d.S_miss = S_miss - b.S_miss; d.G_miss = G_miss - b.G_miss; d.N_miss = N_miss - b.N_miss;
  d.seek_miss_key = seek_miss_key - b.seek_miss_key; d.next_miss_key = next_miss_key - b.next_miss_key;
  d.get_miss_key = get_miss_key - b.get_miss_key;
  d.raw_gets = raw_gets - b.raw_gets; d.rocks_write_bytes = rocks_write_bytes - b.rocks_write_bytes;
  d.raw_seeks = raw_seeks - b.raw_seeks;
  d.last_chunk = last_chunk.since(b.last_chunk);
  return d;
}

json Counts::to_json() const {
  return {{"S", S}, {"G", G}, {"N", N}, {"P", P}, {"D", D},
          {"S_miss", S_miss}, {"G_miss", G_miss}, {"N_miss", N_miss},
          {"bytes", {{"get_value", get_value}, {"scan_value", scan_value}, {"scan_key", scan_key}, {"get_key", get_key},
                     {"seek_miss_key", seek_miss_key}, {"next_miss_key", next_miss_key}, {"get_miss_key", get_miss_key},
                     {"write_key", write_key}, {"write_value", write_value},
                     {"used_key", used_key}, {"used_value", used_value},
                     {"used_write_key", used_write_key}, {"used_write_value", used_write_value}}}};
}

// --- the merged cursor ----------------------------------------------------------------------
// Python store/shadow.py's _MergeCursor: `oi_` sits at the first overlay key at-or-after the
// base position walking forward, at-or-before it walking backward; on a tie the overlay wins
// and a step consumes BOTH sides; a tombstoned head is skipped before the cursor rests.

Cursor::Cursor(std::unique_ptr<rocksdb::Iterator> it, CountingStore* owner, std::string_view target, int dir,
               uint64_t epoch)
    : it_(std::move(it)), it_epoch_(epoch), owner_(owner), dir_(dir) {
  ask_part_ = owner_->part(target, &ask_catchall_);   // parts on: the part this cursor asks about
  ask_pid_ = land_pid_ = uint32_t(owner_->last_part());
  if (owner_->kvpair_stats_) rr_slot_ = owner_->range_open(owner_->last_part());   // one range read, of the asked part
  owner_->rt_range();
  if (owner_->overlay_ && !owner_->overlay_->empty()) {
    ov_ = &owner_->overlay_->map();
    if (dir_ > 0) {
      oi_ = ov_->lower_bound(target);
    } else {
      oi_ = ov_->upper_bound(target);          // last key <= target = the one before upper_bound
      if (oi_ == ov_->begin()) obegin_ = true;
      else --oi_;
    }
    settle();
  }
  charge_key();
}

bool Cursor::ovalid() const { return ov_ && !obegin_ && oi_ != ov_->end(); }

bool Cursor::overlay_leads() const {
  if (!ovalid()) return false;
  if (!it_->Valid()) return true;
  std::string_view bk(it_->key().data(), it_->key().size());
  return dir_ > 0 ? std::string_view(oi_->first) <= bk : std::string_view(oi_->first) >= bk;
}

void Cursor::step_base() {
  EVALCPP_KV_CALL(next);
  if (dir_ > 0) it_->Next();
  else it_->Prev();
}

void Cursor::settle() {
  while (ovalid() && overlay_leads() && oi_->second.tomb) {
    if (it_->Valid() && std::string_view(it_->key().data(), it_->key().size()) == std::string_view(oi_->first))
      step_base();                              // the tombstone consumes the shadowed base copy
    if (dir_ > 0) ++oi_;
    else if (oi_ == ov_->begin()) obegin_ = true;
    else --oi_;
  }
}

void Cursor::advance() {
  if (!valid()) return;
  if (overlay_leads()) {
    if (it_->Valid() && std::string_view(it_->key().data(), it_->key().size()) == std::string_view(oi_->first))
      step_base();                              // tie: drop the shadowed base copy with it
    if (dir_ > 0) ++oi_;
    else if (oi_ == ov_->begin()) obegin_ = true;
    else --oi_;
  } else {
    step_base();
  }
  settle();
}

bool Cursor::valid() const { return it_->Valid() || ovalid(); }

// keys_crossed: the landing being left was on a key, was not declared a miss, and its value was never read
// (the state lives in the cursor's live-range slot, so a commit can book it for a cursor still alive: ranges_flush)
void Cursor::close_landing() {
  CountingStore::LiveRange& lr = owner_->live_[rr_slot_];
  if (lr.cross && lr.open) owner_->hist(lr.cross_part).keys_crossed++;
  lr.cross = false;
}

Cursor::~Cursor() {
  if (!it_) return;                              // a moved-from cursor holds no iterator, no landing, no range read
  if (reuse_req_)                                // the reuse-distance pass: this request's bytes per bucket, rounded up once
    for (int i = 0; i < ReuseTracker::kBuckets; i++)
      if (reuse_req_[i]) owner_->reuse_req_blocks_[i] += reuse_kvpair_blocks(reuse_req_[i]);
  if (owner_->kvpair_stats_) {
    close_landing();
    owner_->range_close(rr_slot_);
  }
  owner_->give_iter(std::move(it_), it_epoch_);   // pooled: back to the pool, deleted at the job's end (end_step)
}

void Cursor::charge_key() {
  const bool stats = owner_->kvpair_stats_;        // run.emit.kvpair_stats (off: a latency run)
  if (stats) close_landing();
  value_read_ = false;                           // a new landing re-arms the read-once guard
  missed_ = false;
  bool v = valid();
  if (v) {
    const uint64_t n = key().size();
    owner_->c_.scan_key += n;
    if (owner_->rt_on_) rt_grow(n);
    if (stats) {
      CountingStore::LiveRange& lr = owner_->live_[rr_slot_];
      lr.bytes += n;
      lr.keys++;
      lr.kvpair_done();                            // the landing before this one is a finished KV pair
      lr.kvpair = n;
    }
  }
  if (v && owner_->reuse_) {                     // a landing fetches its key, hit or miss
    reuse_bucket_ = owner_->reuse_->touch(key(), key().size());
    owner_->reuse_bytes_[reuse_bucket_] += key().size();
    reuse_kvpair_bytes_ = key().size();
    owner_->reuse_blocks_[reuse_bucket_] += reuse_kvpair_blocks(reuse_kvpair_bytes_);
    if (!reuse_req_) reuse_req_.reset(new uint64_t[ReuseTracker::kBuckets]());
    reuse_req_[reuse_bucket_] += key().size();
  }
  if (ask_part_) {                               // parts on: the landing op + its key go to the asked part
    land_part_ = (v && ask_catchall_) ? owner_->part(key()) : ask_part_;   // (a catch-all target: the landed key's)
    land_pid_ = (v && ask_catchall_) ? uint32_t(owner_->last_part()) : ask_pid_;
    owner_->decodes_part(land_pid_);             // what is decoded from here on belongs to this landing's part
    if (from_seek_) land_part_->S++;
    else land_part_->N++;
    if (v) land_part_->scan_key += key().size();
  }
  if (stats) {                                   // (kvpair_stats off: the cursor has no slot)
    owner_->live_[rr_slot_].cross = v;           // a key crossed unless its value is read or it is declared a miss
    owner_->live_[rr_slot_].cross_part = land_pid_;
  }
}

void Cursor::mark_miss() {
  if (missed_)
    throw std::runtime_error("Cursor::mark_miss() called twice at one landing");
  if (value_read_)
    throw std::runtime_error("Cursor::mark_miss() after value() at the same landing -- a landing "
                             "whose value was read is a hit, not a miss");
  missed_ = true;
  if (owner_->kvpair_stats_) owner_->live_[rr_slot_].cross = false;
  uint64_t n = valid() ? key().size() : 0;      // an invalid landing (past the end) has no key
  if (owner_->c_.scan_key < n)                   // the landing was charged into THIS tally on
    throw std::runtime_error("Cursor::mark_miss(): the landing's key bytes are not in scan_key "
                             "(the counter was reset or snapshotted since the landing)");
  owner_->c_.scan_key -= n;                      // charged on landing as a hit; reclassified
  if (valid() && owner_->kvpair_stats_) { owner_->live_[rr_slot_].bytes -= n; owner_->live_[rr_slot_].keys--; owner_->live_[rr_slot_].kvpair -= n; }
  if (valid() && owner_->rt_on_) rt_bytes_ -= n;
  if (from_seek_) { owner_->c_.S_miss++; owner_->c_.seek_miss_key += n; }
  else            { owner_->c_.N_miss++; owner_->c_.next_miss_key += n; }
  if (land_part_) {                              // parts on: the whole landing sits in the ASKED part (moved
                                                 // there when a catch-all target charged the landed key's)
    if (land_part_->scan_key < n || (from_seek_ ? land_part_->S : land_part_->N) == 0)
      throw std::runtime_error("Cursor::mark_miss(): the landing is not in its part's pending bucket "
                               "(the parts were committed or discarded since the landing)");
    land_part_->scan_key -= n;
    if (from_seek_) { land_part_->S--; ask_part_->S++; ask_part_->S_miss++; ask_part_->seek_miss_key += n; }
    else            { land_part_->N--; ask_part_->N++; ask_part_->N_miss++; ask_part_->next_miss_key += n; }
    land_part_ = ask_part_;
    land_pid_ = ask_pid_;
  }
}

// a range read's bytes for the round-trip counts: a further page each time the bytes returned pass a multiple of
// the page (never taken back: a landing later declared a miss gives its key bytes back, and may have opened a page);
// in the remote regime's mode operation the bytes also wait
void Cursor::rt_grow(uint64_t n) {
  if (remote().mode == Remote::kOperation) owner_->remote_bytes(n);
  rt_bytes_ += n;
  if (uint64_t pg = rt_pages_after_first(rt_bytes_); pg > rr_pages_) { owner_->rt_page(pg - rr_pages_); rr_pages_ = pg; }
}

std::string_view Cursor::key() const {
  if (overlay_leads()) return oi_->first;
  rocksdb::Slice k = it_->key();
  return std::string_view(k.data(), k.size());
}

std::string_view Cursor::value() {
  if (value_read_)
    throw std::runtime_error("Cursor::value() called twice at one landing -- a double decode "
                             "would inflate read bytes; borrow the first read");
  if (missed_)
    throw std::runtime_error("Cursor::value() at a landing declared a miss -- a miss reads no value");
  value_read_ = true;
  if (owner_->kvpair_stats_) {                     // a value that holds a byte is READ; an empty one leaves the key crossed
    CountingStore::LiveRange& lr = owner_->live_[rr_slot_];
    if (overlay_leads() ? !oi_->second.value.empty() : it_->value().size() > 0) {
      if (lr.cross && lr.open) owner_->hist(lr.cross_part).values_read++;
      lr.cross = false;
    }
  }
  if (owner_->sizes_) owner_->sizes_->note(key(), key().size() + (overlay_leads() ? oi_->second.value.size() : it_->value().size()));
  if (owner_->reuse_) {                          // the landing's KV pair grows by its value: the same fetch
    uint64_t n = overlay_leads() ? oi_->second.value.size() : it_->value().size();
    owner_->reuse_->grow(key(), n);
    owner_->reuse_bytes_[reuse_bucket_] += n;
    owner_->reuse_blocks_[reuse_bucket_] += reuse_kvpair_blocks(reuse_kvpair_bytes_ + n) - reuse_kvpair_blocks(reuse_kvpair_bytes_);
    reuse_kvpair_bytes_ += n;
    reuse_req_[reuse_bucket_] += n;
  }
  if (overlay_leads()) {
    if (owner_->kvpair_stats_) { owner_->live_[rr_slot_].bytes += oi_->second.value.size(); owner_->live_[rr_slot_].kvpair += oi_->second.value.size(); }
    if (owner_->rt_on_) rt_grow(oi_->second.value.size());
    owner_->c_.scan_value += oi_->second.value.size();
    if (land_part_) land_part_->scan_value += oi_->second.value.size();
    return oi_->second.value;
  }
  rocksdb::Slice v = it_->value();
  if (owner_->kvpair_stats_) { owner_->live_[rr_slot_].bytes += v.size(); owner_->live_[rr_slot_].kvpair += v.size(); }
  if (owner_->rt_on_) rt_grow(v.size());
  owner_->c_.scan_value += v.size();
  if (land_part_) land_part_->scan_value += v.size();
  return std::string_view(v.data(), v.size());
}

void Cursor::next() {
  if (ov_ && dir_ < 0) throw std::runtime_error("shadow cursor: a cursor from seek_for_prev() walks backward; "
                                                "next() would invalidate the merge invariant");
  owner_->c_.N++;
  if (owner_->sampler_.tick(OpSampler::kNext)) {
    uint64_t t0 = rdtsc();
    if (ov_) advance(); else { EVALCPP_KV_CALL(next); it_->Next(); }
    owner_->sampler_.record(OpSampler::kNext, rdtsc() - t0);
  } else if (ov_) advance();
  else { EVALCPP_KV_CALL(next); it_->Next(); }   // no overlay: the pre-shadow behavior
  from_seek_ = false;
  charge_key();
}

void Cursor::prev() {
  if (ov_ && dir_ > 0) throw std::runtime_error("shadow cursor: a cursor from seek() walks forward; "
                                                "prev() would invalidate the merge invariant");
  owner_->c_.N++;
  if (owner_->sampler_.tick(OpSampler::kPrev)) {
    uint64_t t0 = rdtsc();
    if (ov_) advance(); else { EVALCPP_KV_CALL(next); it_->Prev(); }
    owner_->sampler_.record(OpSampler::kPrev, rdtsc() - t0);
  } else if (ov_) advance();
  else { EVALCPP_KV_CALL(next); it_->Prev(); }   // no overlay: the pre-shadow behavior (index walks)
  from_seek_ = false;
  charge_key();
}

// --- the store ------------------------------------------------------------------------------

std::optional<std::string> CountingStore::base_get(std::string_view key) {
  std::string v;
  rocksdb::Status st;
  {
    EVALCPP_KV_CALL(get);
    st = store_.db()->Get(read_options(), rocksdb::Slice(key.data(), key.size()), &v);
  }
  if (st.ok()) return v;
  if (st.IsNotFound()) return std::nullopt;
  throw std::runtime_error("get: " + st.ToString());
}

std::optional<std::string> CountingStore::get(std::string_view key) {
  c_.G++;
  std::optional<std::string> v;
  const ShadowOverlay::Entry* e = overlay_ ? overlay_->find(key) : nullptr;
  if (e) v = e->tomb ? std::nullopt : std::optional<std::string>(e->value);
  else if (sampler_.tick(OpSampler::kGet)) {
    uint64_t t0 = rdtsc();
    v = base_get(key);
    sampler_.record(OpSampler::kGet, rdtsc() - t0);
  } else v = base_get(key);
  // the lookup key: a hit's to get_key, a miss's to get_miss_key (the probe touched a real key
  // either way -- nebula's try-each-tag misses look up an existing key)
  if (v) { c_.get_key += key.size(); c_.get_value += v->size(); }
  else   { c_.G_miss++; c_.get_miss_key += key.size(); }
  if (Counts* p = part(key)) {                   // parts on: the whole GET goes to the asked key's part
    p->G++;
    if (v) { p->get_key += key.size(); p->get_value += v->size(); }
    else   { p->G_miss++; p->get_miss_key += key.size(); }
  }
  if (kvpair_stats_) hist(last_part()).add_get(v ? key.size() + v->size() : 0);
  decodes_part(last_part());                     // what is decoded from here on belongs to this GET's part
  rt_get();
  if (remote().mode == Remote::kOperation) remote_request(v ? key.size() + v->size() : key.size());
  if (sizes_ && v) sizes_->note(key, key.size() + v->size());
  if (reuse_ && v) {
    int b = reuse_->touch(key, key.size() + v->size());
    reuse_bytes_[b] += key.size() + v->size();
    reuse_blocks_[b] += reuse_kvpair_blocks(key.size() + v->size());
    reuse_req_blocks_[b] += reuse_kvpair_blocks(key.size() + v->size());   // a GET is its own request
  }
  return v;
}

std::optional<std::string> CountingStore::rmw_seek(std::string_view key) {
  std::optional<std::string> v = seek_exact(key);
  if (v) count_used(key.size(), 0);
  return v;
}

std::optional<std::string> CountingStore::raw_get(std::string_view key) {
  const ShadowOverlay::Entry* e = overlay_ ? overlay_->find(key) : nullptr;
  if (e) return e->tomb ? std::nullopt : std::optional<std::string>(e->value);
  c_.raw_gets++;                               // audited: RocksDB ticks keys.read per Get CALL, hit or miss
  return base_get(key);
}

std::optional<std::string> CountingStore::raw_last_key(std::string_view prefix, std::string_view bound) {
  c_.raw_seeks++;                              // audited: RocksDB ticks number.db.seek per SeekForPrev
  std::optional<std::string> last;
  {
    std::unique_ptr<rocksdb::Iterator> it(store_.db()->NewIterator(read_options()));
    it->SeekForPrev(rocksdb::Slice(bound.data(), bound.size()));
    // a base key the overlay deleted is skipped (shadow mode only). Each Prev ticks number.db.prev, which the ticker
    // check folds into NEXT unaudited: harmless while no shadow plan with writes is ticker-checked
    for (; it->Valid(); it->Prev()) {
      std::string_view k(it->key().data(), it->key().size());
      if (k.size() < prefix.size() || k.substr(0, prefix.size()) != prefix) break;
      const ShadowOverlay::Entry* e = overlay_ ? overlay_->find(k) : nullptr;
      if (e && e->tomb) continue;
      last = std::string(k);
      break;
    }
  }
  if (overlay_ && !overlay_->empty()) {        // a key the overlay put past the base's last
    const ShadowOverlay::Map& m = overlay_->map();
    for (auto it = m.upper_bound(bound); it != m.begin();) {
      --it;
      if (it->first.size() < prefix.size() || std::string_view(it->first).substr(0, prefix.size()) != prefix) break;
      if (it->second.tomb) continue;
      if (!last || it->first > *last) last = it->first;
      break;
    }
  }
  return last;
}

void CountingStore::note_last_chunk_read(std::string_view key, uint64_t value_bytes, uint64_t target) {
  auto note = [&](Counts::LastChunk& l) { l.reads++; l.read_bytes += value_bytes; l.read_target += target; };
  note(c_.last_chunk);
  if (scheme_) { pending_dirty_ = true; note(pending_[scheme_->part_of(key)].last_chunk); }
}

void CountingStore::note_last_chunk_write(std::string_view key, uint64_t value_bytes, uint64_t base_bytes, uint64_t target) {
  auto note = [&](Counts::LastChunk& l) {
    l.writes++; l.write_bytes += value_bytes; l.write_base += base_bytes; l.write_target += target;
  };
  note(c_.last_chunk);
  if (scheme_) { pending_dirty_ = true; note(pending_[scheme_->part_of(key)].last_chunk); }
}

// the bytes RocksDB books for a single-op write batch: 12-byte header + one record of
// 1 type byte + varint32(key len) + key [+ varint32(value len) + value] (write_batch.cc)
static uint64_t varint32_len(uint64_t n) { return n < 128 ? 1 : n < 16384 ? 2 : n < 2097152 ? 3 : n < 268435456 ? 4 : 5; }
static uint64_t batch_bytes(size_t klen, size_t vlen, bool has_value) {
  return 12 + 1 + varint32_len(klen) + klen + (has_value ? varint32_len(vlen) + vlen : 0);
}

Cursor CountingStore::seek(std::string_view key) {
  c_.S++;
  bool samp = sampler_.tick(OpSampler::kSeek);   // iterator creation is part of a seek's cost
  uint64_t t0 = samp ? rdtsc() : 0;
  std::pair<std::unique_ptr<rocksdb::Iterator>, uint64_t> it;
  {
    EVALCPP_KV_CALL(seek);                       // the pooled (or a new) iterator + Seek
    it = take_iter();
    it.first->Seek(rocksdb::Slice(key.data(), key.size()));
  }
  if (samp) sampler_.record(OpSampler::kSeek, rdtsc() - t0);
  return Cursor(std::move(it.first), this, key, +1, it.second);
}

std::optional<std::string> CountingStore::seek_exact(std::string_view key) {
  Cursor cur = seek(key);
  if (cur.valid() && cur.key() == key) return std::string(cur.value());
  cur.mark_miss();
  return std::nullopt;
}

Cursor CountingStore::seek_for_prev(std::string_view key) {
  c_.S++;
  bool samp = sampler_.tick(OpSampler::kSeekForPrev);
  uint64_t t0 = samp ? rdtsc() : 0;
  std::pair<std::unique_ptr<rocksdb::Iterator>, uint64_t> it;
  {
    EVALCPP_KV_CALL(seek);
    it = take_iter();
    it.first->SeekForPrev(rocksdb::Slice(key.data(), key.size()));
  }
  if (samp) sampler_.record(OpSampler::kSeekForPrev, rdtsc() - t0);
  return Cursor(std::move(it.first), this, key, -1, it.second);
}

std::pair<std::unique_ptr<rocksdb::Iterator>, uint64_t> CountingStore::take_iter() {
  if (!pool_iters_) return {std::unique_ptr<rocksdb::Iterator>(store_.db()->NewIterator(read_options())), 0};
  const uint64_t now = store_.db()->GetLatestSequenceNumber();
  if (iter_pool_.empty())
    return {std::unique_ptr<rocksdb::Iterator>(store_.db()->NewIterator(read_options())), now};
  std::pair<std::unique_ptr<rocksdb::Iterator>, uint64_t> it = std::move(iter_pool_.back());
  iter_pool_.pop_back();
  if (it.second != now) {                        // a write since (any path): the iterator's view is older
    if (!it.first->Refresh().ok()) it.first.reset(store_.db()->NewIterator(read_options()));
    it.second = now;
  }
  return it;
}

void CountingStore::give_iter(std::unique_ptr<rocksdb::Iterator> it, uint64_t epoch) {
  if (pool_iters_) {
    iter_pool_.emplace_back(std::move(it), epoch);
    return;
  }
  EVALCPP_KV_CALL(iter_close);                   // the iterator's delete, an engine call (pins released)
  it.reset();
}

void CountingStore::end_step() {
  if (iter_pool_.empty()) return;
  // the iterator's delete, an engine call (pins released). Timed here, not into this thread's KV-boundary totals:
  // a step's deletes run after the workers' perf windows closed (StepExec::end_job), and execute adds them
  if (read_pinned) {
    pinned_at_end = store_.cache_pinned_bytes();
    usage_at_end = store_.cache_usage_bytes();
  }
  const bool timed = kv_timing_on().load(std::memory_order_relaxed);
  while (!iter_pool_.empty()) {
    uint64_t t0 = timed ? KvCall::now() : 0;
    iter_pool_.pop_back();
    if (timed) {
      closed_ns_ += KvCall::now() - t0;
      closed_calls_++;
    }
  }
}

void CountingStore::put(std::string_view key, std::string_view value) {
  if (apply_) {
    // the write-latency mode: one logical PUT = one engine write (default WriteOptions:
    // WAL on, sync off -- the read-side store config's counterpart). Never batched, so the
    // per-op distribution is of real single writes.
    rocksdb::Status st;
    {
      EVALCPP_KV_CALL(put);
      st = store_.db()->Put(rocksdb::WriteOptions(), rocksdb::Slice(key.data(), key.size()),
                            rocksdb::Slice(value.data(), value.size()));
    }
    if (!st.ok()) throw std::runtime_error("apply put: " + st.ToString());
    c_.rocks_write_bytes += batch_bytes(key.size(), value.size(), true);
  } else if (overlay_) {
    overlay_->put(key, value);
  } else {
    throw std::runtime_error("put: this run has no write overlay (writes only run over one)");
  }
  c_.P++;
  c_.write_key += key.size();
  c_.write_value += value.size();
  if (Counts* p = part(key)) { p->P++; p->write_key += key.size(); p->write_value += value.size(); }
  if (kvpair_stats_) hist(last_part()).add_put(key.size() + value.size());
  decodes_part(last_part());                     // the value this PUT wrote was built (and parsed) just before it
  if (rt_on_) rt_pending_.writes++;
  if (sizes_) sizes_->note(key, key.size() + value.size());
}

void CountingStore::del_key(std::string_view key, int64_t kvpair_bytes) {
  if (apply_) {
    rocksdb::Status st;
    {
      EVALCPP_KV_CALL(delete);
      st = store_.db()->Delete(rocksdb::WriteOptions(), rocksdb::Slice(key.data(), key.size()));
    }
    if (!st.ok()) throw std::runtime_error("apply delete: " + st.ToString());
    c_.rocks_write_bytes += batch_bytes(key.size(), 0, false);
  } else if (overlay_) {
    overlay_->del(key);
  } else {
    throw std::runtime_error("delete: this run has no write overlay (writes only run over one)");
  }
  c_.D++;
  c_.write_key += key.size();
  if (Counts* p = part(key)) { p->D++; p->write_key += key.size(); }
  if (rt_on_) rt_pending_.writes++;
  if (kvpair_bytes < 0 && sizes_) kvpair_bytes = sizes_->find(key);
  if (!kvpair_stats_) {}
  else if (kvpair_bytes >= 0) hist(last_part()).add_del(uint64_t(kvpair_bytes));
  else hist(last_part()).add_del_unknown();
  decodes_part(last_part());
}

// --- the remote regime (store/round_trips.h) -------------------------------------------------

uint64_t remote_sleep(uint64_t ns) {
  if (!ns) return 0;
  // Linux lets a sleep run late by the thread's timer slack, 50 us by default: a 200 us wait took 255 us
  // as measured). 1 ns of slack, set once per thread that waits
  thread_local bool slack_set = false;
  if (!slack_set) { prctl(PR_SET_TIMERSLACK, 1UL, 0UL, 0UL, 0UL); slack_set = true; }
  auto t0 = std::chrono::steady_clock::now();
  std::this_thread::sleep_for(std::chrono::nanoseconds(ns));
  return uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - t0).count());
}

void CountingStore::remote_request(uint64_t bytes) {
  remote_owed_ns_ += double(bytes) * remote().ns_per_byte;
  uint64_t owed = uint64_t(remote_owed_ns_);
  remote_owed_ns_ -= double(owed);
  remote_wait_ns_ += remote_sleep(remote().delay_ns + owed);
}

void CountingStore::remote_bytes(uint64_t bytes) {
  remote_owed_ns_ += double(bytes) * remote().ns_per_byte;
  if (remote_owed_ns_ < 100000.0) return;        // a sleep under 100 us is inaccurate: carried to the next wait
  uint64_t owed = uint64_t(remote_owed_ns_);
  remote_owed_ns_ -= double(owed);
  remote_wait_ns_ += remote_sleep(owed);
}

// --- per-part attribution ------------------------------------------------------------------

void CountingStore::set_part_scheme(const PartScheme* s) {
  scheme_ = s;
  pending_.assign(s ? s->size() : 0, Counts());
  committed_.assign(s ? s->size() : 0, Counts());
  pending_dirty_ = committed_dirty_ = false;
  size_t n = s ? s->size() : 1;                  // the histograms: one part (id 0) without a scheme
  hpending_.assign(n, KvPairHists());
  hcommitted_.assign(n, KvPairHists());
  hflag_.assign(n, 0);
  hcflag_.assign(n, 0);
  hdirty_.clear();
  hcdirty_.clear();
  dparts_.assign(s ? s->size() : 0, DecodeCounts());
  dmark_ = decode_counts();
  dpart_ = 0;
  dpart_known_ = false;
}

void CountingStore::decodes_at(std::string_view key) {
  if (scheme_) decodes_part(scheme_->part_of(key));
}

void CountingStore::take_decode_parts(std::vector<DecodeCounts>& into) {
  if (into.size() < dparts_.size()) into.resize(dparts_.size());
  for (size_t id = 0; id < dparts_.size(); id++)
    if (dparts_[id].any()) { into[id].add(dparts_[id]); dparts_[id].reset(); }
}

uint32_t CountingStore::range_open(size_t part) {
  uint32_t slot;
  if (live_free_.empty()) { slot = uint32_t(live_.size()); live_.emplace_back(); }
  else { slot = live_free_.back(); live_free_.pop_back(); }
  live_[slot] = LiveRange{0, 0, uint32_t(part), true, true};
  live_open_++;
  return slot;
}

void CountingStore::range_close(uint32_t slot) {
  LiveRange& lr = live_[slot];
  if (lr.open) {
    KvPairHists& h = hist(lr.part);
    lr.kvpair_done();
    h.add_range_bytes(lr.bytes, lr.big, lr.small);
    h.range_keys.add(lr.keys);
    live_open_--;
  }
  lr.in_use = lr.open = false;
  live_free_.push_back(slot);
}

void CountingStore::ranges_flush(bool record) {
  if (!live_open_) return;
  for (LiveRange& lr : live_) {
    if (!lr.open) continue;
    if (record) {
      KvPairHists& h = hist(lr.part);
      lr.kvpair_done();
      h.add_range_bytes(lr.bytes, lr.big, lr.small);
      h.range_keys.add(lr.keys);
      if (lr.cross) hist(lr.cross_part).keys_crossed++;   // the landing its cursor rests on, still unread
    }
    lr.cross = false;
    lr.open = false;                             // what the cursor reads from here on is in no entry
  }
  live_open_ = 0;
}

void CountingStore::hists_commit() {
  ranges_flush(true);
  if (!rt_pending_.empty()) { rt_committed_.add(rt_pending_); rt_pending_.clear(); }
  for (uint32_t id : hdirty_) {
    hcommitted_[id].add(hpending_[id]);
    if (!hcflag_[id]) { hcflag_[id] = 1; hcdirty_.push_back(id); }
    hpending_[id].clear();
    hflag_[id] = 0;
  }
  hdirty_.clear();
}

void CountingStore::hists_discard() {
  ranges_flush(false);
  rt_pending_.clear();
  if (hdirty_.empty()) return;
  for (uint32_t id : hdirty_) { hpending_[id].clear(); hflag_[id] = 0; }
  hdirty_.clear();
}

void CountingStore::take_committed_hists(HistParts& into) {
  if (hcdirty_.empty()) return;                  // nothing committed (always so with kvpair_stats off): no allocation
  if (into.size() < hcommitted_.size()) into.resize(hcommitted_.size());
  for (uint32_t id : hcdirty_) {
    into[id].add(hcommitted_[id]);
    hcommitted_[id].clear();
    hcflag_[id] = 0;
  }
  hcdirty_.clear();
}

void CountingStore::parts_commit(const Counts* expect) {
  hists_commit();
  if (!scheme_) return;
  if (expect) {
    std::string why;
    if (!parts_sum_equal(pending_, *expect, &why))
      throw std::runtime_error("parts_commit: the task's pending parts do not sum to its direct counts (" + why +
                               ") -- a direct_counts `before` taken after the task's first op?");
  }
  if (!pending_dirty_) return;
  add_parts(committed_, pending_);
  committed_dirty_ = true;
  parts_discard();
}

void CountingStore::parts_discard() {
  hists_discard();
  if (!scheme_ || !pending_dirty_) return;
  for (auto& c : pending_) c = Counts();
  pending_dirty_ = false;
}

void CountingStore::take_committed_parts(PartCounts& into) {
  if (!scheme_) return;
  if (into.size() < committed_.size()) into.resize(committed_.size());
  if (!committed_dirty_) return;
  add_parts(into, committed_);
  for (auto& c : committed_) c = Counts();
  committed_dirty_ = false;
}

void add_parts(PartCounts& into, const PartCounts& from) {
  if (into.size() < from.size()) into.resize(from.size());
  for (size_t i = 0; i < from.size(); i++) into[i].add(from[i]);
}

bool parts_sum_equal(const PartCounts& parts, const Counts& t, std::string* why) {
  Counts s;
  for (auto& p : parts) s.add(p);
  struct Lane { const char* name; uint64_t Counts::*f; };
  static const Lane kLanes[] = {
      {"S", &Counts::S}, {"G", &Counts::G}, {"N", &Counts::N}, {"P", &Counts::P}, {"D", &Counts::D},
      {"S_miss", &Counts::S_miss}, {"G_miss", &Counts::G_miss}, {"N_miss", &Counts::N_miss},
      {"get_key", &Counts::get_key}, {"get_value", &Counts::get_value},
      {"scan_key", &Counts::scan_key}, {"scan_value", &Counts::scan_value},
      {"seek_miss_key", &Counts::seek_miss_key}, {"next_miss_key", &Counts::next_miss_key},
      {"get_miss_key", &Counts::get_miss_key},
      {"write_key", &Counts::write_key}, {"write_value", &Counts::write_value}};
  for (auto& l : kLanes)
    if (s.*l.f != t.*l.f) {
      if (why) *why = std::string(l.name) + ": parts " + std::to_string(s.*l.f) + " != step " + std::to_string(t.*l.f);
      return false;
    }
  if (!(s.last_chunk == t.last_chunk)) {      // the last-chunk lane splits by part too
    if (why) *why = "last_chunk: parts " + s.last_chunk.to_json().dump() + " != step " + t.last_chunk.to_json().dump();
    return false;
  }
  return true;
}

}  // namespace evalcpp
