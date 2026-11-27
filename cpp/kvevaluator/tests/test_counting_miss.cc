// The absence-miss injection test: feed the counting seam known misses and check each lands in
// its miss lane, NOT in read (scan_key / get_key = hit key bytes only), and that the grand
// totals (S/G/N, scan_key + miss keys) are unchanged. Runs BEFORE the operator charge points
// adopt the seam, so the split is proven non-circular on a store of three keys.
#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <string>
#include <unistd.h>

#include "store/counting.h"
#include "store/rocksdb_store.h"
#include "exec/replay.h"
#include "harness.h"

namespace {

using namespace evalcpp;

#define THROWS(stmt) do { bool t = false; try { stmt; } catch (const std::exception&) { t = true; } if (!t) { fails++; std::cerr << "FAIL " << __LINE__ << " expected throw: " #stmt "\n"; } } while (0)

TEST_CASE(test_counting_miss) {
  char tmpl[] = "/tmp/evalcpp-miss-XXXXXX";
  std::string dir = kvt::temp_dir(tmpl);
  StoreOptions so;
  auto store = Store::open_read_write(dir + "/db", so, true);
  // keys: "aa1"=v1 "aa2"=v22 "cc1"=v333 -- prefix "aa" has two rows, "bb" none, "cc" one
  for (auto [k, v] : {std::pair{"aa1", "1"}, std::pair{"aa2", "22"}, std::pair{"cc1", "333"}})
    if (!store->db()->Put(rocksdb::WriteOptions(), k, v).ok()) return 2;
  CountingStore st(*store);

  // 1. point SEEK miss: seek_exact("bb1") lands on "cc1" (foreign) -> seek_miss, no read
  {
    st.reset();
    auto v = st.seek_exact("bb1");
    CHECK(!v);
    const Counts& c = st.counts();
    CHECK_EQ(c.S, 1u); CHECK_EQ(c.S_miss, 1u);
    CHECK_EQ(c.scan_key, 0u); CHECK_EQ(c.seek_miss_key, 3u);
    CHECK_EQ(c.scan_value, 0u); CHECK_EQ(c.N, 0u);
  }
  // 2. point SEEK hit: the landing key is a hit, the value is read
  {
    st.reset();
    auto v = st.seek_exact("aa2");
    CHECK(v && *v == "22");
    const Counts& c = st.counts();
    CHECK_EQ(c.S, 1u); CHECK_EQ(c.S_miss, 0u);
    CHECK_EQ(c.scan_key, 3u); CHECK_EQ(c.seek_miss_key, 0u); CHECK_EQ(c.scan_value, 2u);
  }
  // 3. point SEEK miss past the end of the store: a miss with no landing key (0 bytes)
  {
    st.reset();
    auto v = st.seek_exact("zz");
    CHECK(!v);
    const Counts& c = st.counts();
    CHECK_EQ(c.S, 1u); CHECK_EQ(c.S_miss, 1u); CHECK_EQ(c.scan_key, 0u); CHECK_EQ(c.seek_miss_key, 0u);
  }
  // 4. GET miss vs hit
  {
    st.reset();
    CHECK(!st.get("bb1"));
    CHECK_EQ(st.counts().G, 1u); CHECK_EQ(st.counts().G_miss, 1u);
    CHECK_EQ(st.counts().get_key, 0u); CHECK_EQ(st.counts().get_miss_key, 3u); CHECK_EQ(st.counts().get_value, 0u);
    st.reset();
    CHECK(st.get("cc1").value() == "333");
    CHECK_EQ(st.counts().G, 1u); CHECK_EQ(st.counts().G_miss, 0u);
    CHECK_EQ(st.counts().get_key, 3u); CHECK_EQ(st.counts().get_miss_key, 0u); CHECK_EQ(st.counts().get_value, 3u);
  }
  // 5. prefix scan "aa": two hits, then the terminal NEXT lands on "cc1" -> next_miss (the
  //    range-end sentinel); scan_key holds the two hit keys only; S/N totals unchanged
  {
    st.reset();
    Cursor cur = st.seek("aa");
    int rows = 0;
    while (cur.valid() && cur.key().substr(0, 2) == "aa") { (void)cur.value(); rows++; cur.next(); }
    CHECK(cur.valid());
    cur.mark_miss();
    const Counts& c = st.counts();
    CHECK_EQ(rows, 2);
    CHECK_EQ(c.S, 1u); CHECK_EQ(c.N, 2u); CHECK_EQ(c.S_miss, 0u); CHECK_EQ(c.N_miss, 1u);
    CHECK_EQ(c.scan_key, 6u); CHECK_EQ(c.next_miss_key, 3u); CHECK_EQ(c.seek_miss_key, 0u);
    CHECK_EQ(c.scan_value, 3u);
    CHECK_EQ(c.scan_key + c.seek_miss_key + c.next_miss_key, 9u);   // the grand total: 3 landings x 3 B
    THROWS(cur.value());                                             // a miss reads no value
    THROWS(cur.mark_miss());                                         // marked twice
  }
  // 6. prefix scan "bb": empty -- the SEEK itself is the off-prefix landing -> seek_miss
  {
    st.reset();
    Cursor cur = st.seek("bb");
    int rows = 0;
    while (cur.valid() && cur.key().substr(0, 2) == "bb") { rows++; cur.next(); }
    cur.mark_miss();
    const Counts& c = st.counts();
    CHECK_EQ(rows, 0);
    CHECK_EQ(c.S, 1u); CHECK_EQ(c.S_miss, 1u); CHECK_EQ(c.N_miss, 0u);
    CHECK_EQ(c.scan_key, 0u); CHECK_EQ(c.seek_miss_key, 3u);
  }
  // 7. prefix scan "cc": one hit, the terminal NEXT falls off the end -> next_miss with 0 bytes
  {
    st.reset();
    Cursor cur = st.seek("cc");
    while (cur.valid() && cur.key().substr(0, 2) == "cc") cur.next();
    CHECK(!cur.valid());
    cur.mark_miss();
    const Counts& c = st.counts();
    CHECK_EQ(c.N, 1u); CHECK_EQ(c.N_miss, 1u); CHECK_EQ(c.next_miss_key, 0u); CHECK_EQ(c.scan_key, 3u);
  }
  // 8. a landing whose value was read cannot be declared a miss (the classification guard)
  {
    st.reset();
    Cursor cur = st.seek("cc");
    (void)cur.value();
    THROWS(cur.mark_miss());
  }
  // 9. the pertype chunk replay: a probe that found no chunk of its prefix is a seek miss,
  //    its foreign landing key in the miss lane; a landed probe charges the chunk key as a hit
  {
    std::vector<Landing> L = {
        {0, "aa", 5, false, 0, 0, 0, 0, true, 3},      // not landed, a foreign key of 3 B
        {0, "bb", 5, false, 0, 0, 0, 0, false, 0},     // not landed, past the end
        {0, "cc", 5, true, 1, 1, 9, 40, true, 3},      // landed: chunk key 3 B, chunk 40 B
    };
    Counts c = replay_landings(L);
    CHECK_EQ(c.S, 3u); CHECK_EQ(c.S_miss, 2u);
    CHECK_EQ(c.seek_miss_key, 3u); CHECK_EQ(c.scan_key, 3u); CHECK_EQ(c.scan_value, 40u);
  }
  // 10. since()/add() carry the miss lanes
  {
    st.reset();
    Counts before = st.counts();
    (void)st.get("bb1");
    Counts d = st.counts().since(before);
    CHECK_EQ(d.G_miss, 1u); CHECK_EQ(d.get_miss_key, 3u);
    Counts sum; sum.add(d); sum.add(d);
    CHECK_EQ(sum.G_miss, 2u); CHECK_EQ(sum.get_miss_key, 6u);
    json j = d.to_json();
    CHECK_EQ(j["G_miss"].get<uint64_t>(), 1u); CHECK_EQ(j["bytes"]["get_miss_key"].get<uint64_t>(), 3u);
  }
  // --- the KV-pair size histograms (store/size_hist.h): one entry per request, beside the counts ---------------
  // 11. buckets: 0 = size 0, i = [2^(i-1), 2^i); move() re-buckets one entry and keeps the exact sum
  {
    CHECK_EQ(SizeHist::bucket(0), 0); CHECK_EQ(SizeHist::bucket(1), 1); CHECK_EQ(SizeHist::bucket(2), 2);
    CHECK_EQ(SizeHist::bucket(3), 2); CHECK_EQ(SizeHist::bucket(4), 3); CHECK_EQ(SizeHist::bucket(4095), 12);
    CHECK_EQ(SizeHist::bucket(4096), 13);
    SizeHist h; h.add(0); h.move(0, 5); h.move(5, 9);
    CHECK_EQ(h.n, 1u); CHECK_EQ(h.sum, 9u); CHECK_EQ(h.b[4], 1u); CHECK_EQ(h.b[0], 0u); CHECK_EQ(h.b[3], 0u);
  }
  // 12. a GET hit, a GET miss, one range read of two keys ended by a foreign landing: entries = operations, sums =
  // the lanes; taken while the cursor is still alive (a task takes its counts before its cursors die)
  {
    st.reset(); st.parts_discard();
    HistParts hp;
    (void)st.get("aa2"); (void)st.get("bb1");
    Cursor cur = st.seek("aa");
    (void)cur.value(); cur.next(); (void)cur.value(); cur.next(); cur.mark_miss();
    const Counts c = st.counts();
    st.parts_commit();
    st.take_committed_hists(hp);
    CHECK_EQ(hp.size(), 1u);
    const KvPairHists& h = hp[0];
    CHECK_EQ(h.get.n, c.G); CHECK_EQ(h.get.sum, c.get_key + c.get_value); CHECK_EQ(h.get.b[0], 1u); CHECK_EQ(h.get.b[3], 1u);
    CHECK_EQ(h.range_bytes.n, c.S); CHECK_EQ(h.range_bytes.sum, c.scan_key + c.scan_value);
    CHECK_EQ(h.range_bytes.sum, 9u); CHECK_EQ(h.range_bytes.b[4], 1u);
    CHECK_EQ(h.range_keys.n, 1u); CHECK_EQ(h.range_keys.sum, 2u); CHECK_EQ(h.range_keys.b[2], 1u);
    CHECK_EQ(h.to_json().dump(), std::string(R"({"get":{"0":1,"3":1},"range_bytes":{"4":1},"range_keys":{"2":1},"get_fetch_4k":1,"get_store_blocks":1,"range_fetch_4k":1,"range_store_blocks":1})"));
    // exact Fetch in its two units: a miss is 0; a 60,000 byte KV pair is 15 units of 4 KB and ONE store block; a range
    // read of two large KV pairs (4,096 and 5,000 bytes) and 100 bytes of small ones is 3 units and 2 + 1 store blocks
    {
      KvPairHists x;
      x.add_get(0); x.add_get(60000); x.add_get(4096);
      CHECK_EQ(x.get_fetch_4k, 16u); CHECK_EQ(x.get_store_blocks, 2u);
      x.add_range_bytes(4096 + 5000 + 100, 2, 100); x.add_range_bytes(0, 0, 0); x.add_range_bytes(300, 0, 300);
      CHECK_EQ(x.range_fetch_4k, 4u); CHECK_EQ(x.range_store_blocks, 4u);
      KvPairHists y; y.add(x); CHECK_EQ(y == x, true); y.clear(); CHECK_EQ(y.range_fetch_4k + y.get_store_blocks, 0u);
    }
    cur.next();                                  // after the commit: the entry is gone from pending, nothing underflows
    HistParts again; st.parts_commit(); st.take_committed_hists(again);
    CHECK(again.empty());                      // nothing was committed: nothing is handed over (not even sized)
  }
  // 13. discarded work leaves no entry (a replayed chunk reader's physical seeks); INJECTION: counts taken while
  // the entries were dropped is exactly the mismatch the executor's check throws on (entries != operations)
  {
    st.reset(); st.parts_discard();
    (void)st.get("aa1"); (void)st.seek_exact("aa1");
    const Counts c = st.counts();
    st.parts_discard();
    HistParts hp; st.parts_commit(); st.take_committed_hists(hp);
    CHECK(hp.empty());
    CHECK(c.G == 1u && c.S == 1u);               // the counts exist; their entries do not: what the executor throws on
  }
  // --- round trips (store/round_trips.h) ------------------------------------------------------------------
  // 13a. inside a unit each request needs the one before it: 3 requests, 3 levels. Two units of one job run level by
  // level: their GETs at one level are one batch, their range reads go side by side
  {
    st.reset(); st.parts_discard();
    { RtLevels earlier; st.take_committed_round_trips(earlier); }   // what the cases above committed
    for (int unit = 0; unit < 2; unit++) {
      st.rt_unit();
      (void)st.get("aa1"); (void)st.seek_exact("aa2"); (void)st.get("cc1");
    }
    st.parts_commit();
    RtLevels job; st.take_committed_round_trips(job);
    RtTotals t = job.totals();
    CHECK_EQ(t.critical, 3u); CHECK_EQ(t.round_trips, 4u); CHECK_EQ(t.pages_extra, 0u);   // 1 + 2 + 1
  }
  // 13b. RtParallel: the members' requests are one level; a chain inside a member goes deeper, and what follows the
  // scope comes after the deepest member
  {
    st.reset(); st.parts_discard(); st.rt_unit();
    (void)st.seek_exact("aa1");                       // level 0
    {
      RtParallel par(st);
      par.member(); (void)st.get("aa1");              // level 1
      par.member(); (void)st.get("aa2"); (void)st.get("cc1");   // levels 1 and 2
      par.member(); (void)st.get("bb1");              // level 1
    }
    (void)st.get("cc1");                              // level 3
    st.parts_commit();
    RtLevels job; st.take_committed_round_trips(job);
    CHECK_EQ(job.lv.size(), 4u); CHECK_EQ(job.lv[1].gets, 3u); CHECK_EQ(job.lv[2].gets, 1u);
    RtTotals t = job.totals();
    CHECK_EQ(t.critical, 4u); CHECK_EQ(t.round_trips, 4u);
  }
  // 13c. a range read past the page: one more request per page, each after the one before; discarded work and
  // INJECTION: with a 4-byte page the 9-byte range read of "aa" is 3 requests, with the default page it is 1
  {
    uint64_t page = rt_page_bytes();
    for (uint64_t pg : {uint64_t(4), page}) {
      rt_page_bytes() = pg;
      st.reset(); st.parts_discard(); st.rt_unit();
      { Cursor cur = st.seek("aa"); (void)cur.value(); cur.next(); (void)cur.value(); cur.next(); cur.mark_miss(); }
      st.parts_commit();
      RtLevels job; st.take_committed_round_trips(job);
      RtTotals t = job.totals();
      CHECK_EQ(t.pages_extra, pg == 4 ? 2u : 0u); CHECK_EQ(t.round_trips, pg == 4 ? 3u : 1u);
      CHECK_EQ(t.critical, pg == 4 ? 3u : 1u);
    }
    rt_page_bytes() = page;
    st.rt_unit(); (void)st.get("aa1"); st.parts_discard();
    RtLevels none; st.parts_commit(); st.take_committed_round_trips(none);
    CHECK(none.empty());
  }
  // 13d. a job's writes are one batch after its reads
  {
    ShadowOverlay ov;
    CountingStore w(*store, &ov);
    w.rt_unit(); (void)w.get("aa1"); w.put("kk1", "vv"); w.put("kk2", "vv"); w.del_key("aa1", 4);
    w.parts_commit();
    RtLevels job; w.take_committed_round_trips(job);
    CHECK_EQ(job.totals().round_trips, 2u); CHECK_EQ(job.totals().critical, 2u);
  }
  // 13e. the remote regime, mode operation: every request sleeps the delay (measured); off, nothing waits; the
  // counts are the same either way
  {
    st.reset(); st.parts_discard(); st.rt_unit();
    (void)st.get("aa1"); (void)st.seek_exact("aa2");
    const Counts off = st.counts();
    CHECK_EQ(st.take_remote_wait_ns(), 0u);
    remote().mode = Remote::kOperation; remote().delay_ns = 200000;
    st.reset(); st.parts_discard(); st.rt_unit();
    (void)st.get("aa1"); (void)st.seek_exact("aa2");
    const Counts on = st.counts();
    remote() = Remote();
    uint64_t waited = st.take_remote_wait_ns();
    CHECK(waited >= 400000u); CHECK(waited < 400000u + 50000000u);
    CHECK_EQ(on.to_json().dump(), off.to_json().dump());
    st.parts_discard();
  }
  // 14. PUT and DELETE (an overlay): a delete's size is known only when the writer passes it
  {
    ShadowOverlay ov;
    CountingStore w(*store, &ov);
    w.put("kk1", "vv"); w.del_key("kk1"); w.del_key("aa1", 4);
    const Counts c = w.counts();
    HistParts hp; w.parts_commit(); w.take_committed_hists(hp);
    CHECK_EQ(hp[0].put.n, c.P); CHECK_EQ(hp[0].put.sum, 5u); CHECK_EQ(hp[0].put.b[3], 1u);
    CHECK_EQ(hp[0].del.n, c.D); CHECK_EQ(hp[0].del.unknown, 1u); CHECK_EQ(hp[0].del.b[3], 1u);
    CHECK_EQ(hp[0].del.to_json().dump(), std::string(R"({"3":1,"u":1})"));
  }
  // 14b. a write plan's run remembers the sizes of the KV pairs it read or put (ReadSizes): a delete of one of them
  // is a known size, with no read; a key the run never touched stays unknown
  {
    ShadowOverlay ov;
    CountingStore w(*store, &ov);
    ReadSizes rs;
    w.set_read_sizes(&rs);
    (void)w.get("aa2"); (void)w.seek_exact("cc1"); w.put("kk1", "vv");
    const Counts before = w.counts();
    w.del_key("aa2"); w.del_key("cc1"); w.del_key("kk1"); w.del_key("aa1");
    CHECK_EQ(w.counts().G, before.G); CHECK_EQ(w.counts().S, before.S);   // no read to learn a size
    HistParts hp; w.parts_commit(); w.take_committed_hists(hp);
    CHECK_EQ(hp[0].del.n, 4u); CHECK_EQ(hp[0].del.unknown, 1u); CHECK_EQ(hp[0].del.sum, 5u + 6u + 5u);
  }
  // 14c. exact write units (1 KB, rounded up per request): PUTs of 1023, 1024, 1025 and 5000 bytes (key + value) are
  // 1 + 1 + 2 + 5 units; a DELETE of a known 1025 bytes is 2, one of unknown size 1
  {
    ShadowOverlay ov;
    CountingStore w(*store, &ov);
    for (size_t n : {1023u, 1024u, 1025u, 5000u}) w.put("kk" + std::to_string(n), std::string(n - 6, 'v'));
    w.del_key("kk1025", 1025); w.del_key("zz1");
    HistParts hp; w.parts_commit(); w.take_committed_hists(hp);
    CHECK_EQ(hp[0].put.sum, 1023u + 1024u + 1025u + 5000u);
    CHECK_EQ(hp[0].put_units_1k, 9u);
    CHECK_EQ(hp[0].del_units_1k, 3u); CHECK_EQ(hp[0].del.unknown, 1u);
    json j = hp[0].to_json();
    CHECK_EQ(j["put_units_1k"].get<uint64_t>(), 9u); CHECK_EQ(j["del_units_1k"].get<uint64_t>(), 3u);
    KvPairHists y; y.add(hp[0]); CHECK_EQ(y == hp[0], true);
    y.clear(); CHECK_EQ(y.put_units_1k + y.del_units_1k, 0u);
    CHECK_EQ(KvPairHists::write_units_1k(0), 1u);   // a request is at least one unit
  }
  // 15. the replay: one range-read entry per charged SEEK, and the chunk statistics per batch (a Landing's group).
  // Batch 7 probes chunk [1,9] twice (the second probe is free) and chunk [20,29] once; batch 8 lands on chunk
  // [1,9] again (its own cache: charged again) and misses once. The order of the records within a batch is the
  // task order, so the same at every P.
  {
    std::vector<Landing> L = {
        {7, "cc", 5, true, 1, 1, 9, 40, true, 3},
        {7, "cc", 6, true, 1, 1, 9, 40, true, 3},      // inside a decoded chunk: no SEEK, no entry
        {7, "cc", 25, true, 20, 20, 29, 100, true, 3},
        {8, "cc", 5, true, 1, 1, 9, 40, true, 3},
        {8, "bb", 5, false, 0, 0, 0, 0, true, 3},
    };
    HistParts hp; ChunkStats cs;
    Counts c = replay_landings(L, nullptr, nullptr, &hp, &cs);
    CHECK_EQ(c.S, 4u); CHECK_EQ(c.S_miss, 1u); CHECK_EQ(c.scan_value, 180u); CHECK_EQ(c.scan_key, 9u);
    CHECK_EQ(hp[0].range_bytes.n, c.S); CHECK_EQ(hp[0].range_bytes.sum, c.scan_key + c.scan_value);
    CHECK_EQ(hp[0].range_keys.sum, 3u); CHECK_EQ(hp[0].range_keys.b[0], 1u);
    CHECK_EQ(cs.probes, 5u); CHECK_EQ(cs.charged, 3u); CHECK_EQ(cs.batches, 2u);
    CHECK_EQ(cs.distinct_max_batch, 2u); CHECK_EQ(cs.held_bytes_peak, 140u);
  }
  // 16. the reuse-distance tracker (store/reuse_distance.h): the distinct bytes fetched since a KV pair's last fetch
  {
    ReuseTracker t;
    CHECK_EQ(t.touch("A", 10), ReuseTracker::kNew);
    CHECK_EQ(t.touch("B", 20), ReuseTracker::kNew);
    CHECK_EQ(t.touch("A", 10), SizeHist::bucket(20));    // only B since
    CHECK_EQ(t.touch("A", 10), 0);                       // nothing since
    CHECK_EQ(t.touch("C", 100), ReuseTracker::kNew);
    CHECK_EQ(t.touch("B", 20), SizeHist::bucket(110));   // A and C, each once however often fetched
    t.grow("B", 1000);                                   // B's value read after its key: the same fetch, a larger KV pair
    CHECK_EQ(t.touch("A", 10), SizeHist::bucket(1120));  // C + B (20 + 1000)
    for (int i = 0; i < 200000; i++) (void)t.touch("x" + std::to_string(i % 1000), 1);   // past the tree's first size
    CHECK_EQ(t.touch("x0", 1), SizeHist::bucket(999));   // the 999 other x KV pairs since x0
    CHECK_EQ(t.touch("A", 10), SizeHist::bucket(1000));  // the 1000 x KV pairs; B and C were fetched before A
    t.clear();
    CHECK_EQ(t.touch("A", 10), ReuseTracker::kNew);
  }
  // 17. the store feeds it: a GET hit is a KV pair of key + value; a cursor landing its key, grown by the value read
  {
    ReuseTracker t;
    CountingStore r(*store);
    r.set_reuse_tracker(&t);
    (void)r.get("aa2"); (void)r.get("bb1");               // a hit (5 B, new); a miss fetches no KV pair
    { Cursor cur = r.seek("aa"); (void)cur.value(); cur.next(); (void)cur.value(); }   // aa1 (new, 4 B), aa2 again
    CHECK_EQ(r.reuse_bytes()[ReuseTracker::kNew], 5u + 4u);
    CHECK_EQ(r.reuse_bytes()[SizeHist::bucket(4)], 5u);   // aa2: only aa1's 4 bytes since its GET
    CHECK_EQ(t.kvpairs(), 2u);
    // the KV pairs' own 4 KB blocks by the same bucket: two new small KV pairs, one block each; aa2 again, one block
    CHECK_EQ(r.reuse_blocks()[ReuseTracker::kNew], 2u);
    CHECK_EQ(r.reuse_blocks()[SizeHist::bucket(4)], 1u);
    // per request: the GET of aa2 (new) is one block; the range read fetched aa1 (new) and aa2 (bucket 4): a block each
    CHECK_EQ(r.reuse_request_blocks()[ReuseTracker::kNew], 2u);
    CHECK_EQ(r.reuse_request_blocks()[SizeHist::bucket(4)], 1u);
    // both again in one read: aa1 at distance 5 (aa2 since), aa2 at distance 4: the same bucket [4, 8)
    { Cursor cur = r.seek("aa"); (void)cur.value(); cur.next(); (void)cur.value(); }
    CHECK_EQ(r.reuse_blocks()[SizeHist::bucket(4)], 3u);            // a KV pair block each, on top of the one before
    CHECK_EQ(r.reuse_request_blocks()[SizeHist::bucket(4)], 2u);    // the read's 9 bytes are one block
    CHECK_EQ(reuse_kvpair_blocks(1), 1u); CHECK_EQ(reuse_kvpair_blocks(4096), 1u); CHECK_EQ(reuse_kvpair_blocks(4097), 2u);
    CHECK_EQ(reuse_kvpair_blocks(65536), 16u);
  }
  store->close();
  std::system(("rm -rf " + dir).c_str());
  if (fails) { std::cerr << fails << " failure(s)\n"; return 1; }
  std::cout << "test_counting_miss OK\n";
  return 0;
}

}  // namespace
