// HAND-DERIVED write-count fixtures (RocksDB-independent): the PUT/DELETE counts, write bytes and
// used_write bytes the posting write paths must report, computed by hand from the codecs:
//   'U' posting = 1 head + 8 base + 4 count + group-varint deltas (tag + 4 slots per group of
//   four, padded) -- so 2..5 uids of a dense run = 18 B, 6..9 uids = 23 B.
// Scenarios: PostingIndex insert / delete / same-value update / absent delete on a whole term,
// insert and delete on a SPLIT term (one part rewritten, an emptied part kept as 'U'), and the
// apply-mode RocksDB batch-bytes formula CountingStore accumulates (12-byte header + record).
#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <map>
#include <string>
#include <unistd.h>

#include "codec/posting.h"
#include "codec/prim.h"
#include "formats/format.h"
#include "index/posting_index.h"
#include "index/sorted_index.h"
#include "schema/schema.h"
#include "store/counting.h"
#include "store/rocksdb_store.h"
#include "store/unit_posting.h"
#include "store/unit_value.h"
#include "util/vid.h"
#include "repo_paths.h"
#include "harness.h"

namespace {

using namespace evalcpp;


using MemSink = kvt::MemSink;

static uint64_t make_uid(const Schema& s, const std::string& label, uint64_t id) {
  return (uint64_t(s.tag_of_label(label)) << kVidShift) | id;
}

TEST_CASE(test_write_counts) {
  Schema s = Schema::load(ldbc_schema_dir() + "/schema.json");
  char tmpl[] = "/tmp/evalcpp-wcount-XXXXXX";
  std::string dir = kvt::temp_dir(tmpl);
  // codec facts the expectations below rest on
  CHECK(posting::encode_posting({1, 2, 3}).size() == 18, "3 dense uids = 18 B");
  CHECK(posting::encode_posting({1, 2, 3, 4, 5}).size() == 18, "5 dense uids = 18 B (one group)");
  CHECK(posting::encode_posting({1, 2, 3, 4, 5, 6}).size() == 23, "6 dense uids = 23 B (two groups)");
  CHECK(posting::encode_posting({}).size() == 1, "an empty posting is the head byte alone");

  // --- whole-term scenarios: Person.firstName = "Bo" held by ids 1..3 -----------------------
  const IndexDecl& fn = s.index_decl("Person", "firstName");
  std::string term = PostingIndex::term_key(fn.id, index_value_bytes(s, fn, "Person", Value(std::string("Bo"))));
  {
    PostingIndex ix(s, 65536);
    MemSink sink;
    for (uint64_t i = 1; i <= 3; i++)
      ix.ingest_vertex(sink, "Person", make_uid(s, "Person", i), make_row({{"firstName", Value(std::string("Bo"))}}));
    ix.finalize(sink);
    StoreOptions so;
    auto store = Store::open_read_write(dir + "/whole", so, true);
    for (auto& [k, v] : sink.kv) store->db()->Put(rocksdb::WriteOptions(), k, v);
    ShadowOverlay ov;
    CountingStore st(*store, &ov);
    std::vector<std::pair<std::string, std::string>> puts;
    std::vector<std::string> dels;
    IndexWrites w{puts, dels};
    // insert id 4 -> ONE put of the 4-uid posting (18 B); the new entry's own width = 1 B (a
    // 1-byte delta) is the used_write value; the rest of the rewritten posting is amplification
    st.reset();
    ix.w_insert(st, "Person", make_uid(s, "Person", 4), make_row({{"firstName", Value(std::string("Bo"))}}), w);
    // (the type posting is a second term: Person has a type index -> 2 puts total)
    CHECK(puts.size() == 2 && dels.empty(), "insert = 2 puts (firstName term + type posting), 0 dels: " << puts.size());
    size_t at = puts[0].first == term ? 0 : 1;
    CHECK(puts[at].second.size() == 18, "the rewritten firstName posting is 18 B: " << puts[at].second.size());
    CHECK(posting::decode_posting(puts[at].second).size() == 4, "4 uids after the insert");
    CHECK(st.counts().used_write_value == 1 + 1, "used_write = 1 B per new entry, both terms: " << st.counts().used_write_value);
    CHECK(st.counts().used_write_key == term.size() + PostingIndex::term_key(s.type_index_decl("Person").id, "").size(),
          "used_write_key = the two term keys");
    CHECK(st.counts().P == 0 && st.counts().D == 0, "the index surface appends puts; the caller applies them");
    puts.clear(); dels.clear();
    // delete id 2 -> ONE put of the 2-uid posting (18 B still: one group), 0 logical value bytes
    st.reset();
    ix.w_delete(st, "Person", make_uid(s, "Person", 2), make_row({{"firstName", Value(std::string("Bo"))}}), w);
    at = puts[0].first == term ? 0 : 1;
    CHECK(puts.size() == 2 && posting::decode_posting(puts[at].second).size() == 2, "delete rewrites the posting minus one");
    CHECK(st.counts().used_write_value == 0, "a removal writes no logical value bytes");
    puts.clear(); dels.clear();
    // same-value update -> nothing; absent term delete -> nothing
    st.reset();
    Value row = make_row({{"firstName", Value(std::string("Bo"))}});
    ix.w_update(st, "Person", make_uid(s, "Person", 1), row, row, "firstName", w);
    CHECK(puts.empty() && dels.empty(), "same-value update = 0 writes");
    ix.w_delete(st, "Person", make_uid(s, "Person", 99), make_row({{"firstName", Value(std::string("Zz"))}}), w);
    CHECK(puts.empty() && dels.empty(), "absent term + absent entry in the type posting = 0 writes: " << puts.size());
    puts.clear(); dels.clear();
  }
  // --- multi-byte delta: the new entry's code width is its OWN delta's width -----------------
  // uids {1, 2, 3} + 300: deltas [1, 1, 297] -> group-varint widths [1, 1, 2] (+ a 1-byte pad
  // slot): tag 1 + 5 = 6 B of codes; posting = 1 + 8 + 4 + 6 = 19 B; the 300's attributable
  // width is 2 B (297 needs two bytes), which is exactly what used_write_value must charge
  {
    CHECK(posting::encode_posting({1, 2, 3, 300}).size() == 19, "4 uids with a 297 delta = 19 B");
    std::vector<uint8_t> w;
    posting::decode_posting_w(posting::encode_posting({1, 2, 3, 300}), &w);
    CHECK(w.size() == 4 && w[0] == 8 && w[1] == 1 && w[2] == 1 && w[3] == 2, "widths [8, 1, 1, 2]: the base is 8 B, the 297 delta 2 B");
    PostingIndex ix(s, 65536);
    MemSink sink;
    for (uint64_t i : {1, 2, 3})
      ix.ingest_vertex(sink, "Person", make_uid(s, "Person", i), make_row({{"firstName", Value(std::string("Wi"))}}));
    ix.finalize(sink);
    StoreOptions so;
    auto store = Store::open_read_write(dir + "/wide", so, true);
    for (auto& [k, v] : sink.kv) store->db()->Put(rocksdb::WriteOptions(), k, v);
    ShadowOverlay ov;
    CountingStore st(*store, &ov);
    std::vector<std::pair<std::string, std::string>> puts;
    std::vector<std::string> dels;
    IndexWrites w2{puts, dels};
    st.reset();
    ix.w_insert(st, "Person", make_uid(s, "Person", 300), make_row({{"firstName", Value(std::string("Wi"))}}), w2);
    std::string wterm = PostingIndex::term_key(fn.id, index_value_bytes(s, fn, "Person", Value(std::string("Wi"))));
    size_t at = puts[0].first == wterm ? 0 : 1;
    CHECK(puts.size() == 2 && puts[at].second.size() == 19, "the rewritten posting is 19 B: " << puts[at].second.size());
    // used_write_value = 2 (firstName term: the 297-delta entry) + 2 (type posting: the same
    // delta, ids 1..3 then 300) = 4; used_write_key = both term keys
    CHECK(st.counts().used_write_value == 4, "used_write_value = 2 B per term for the wide delta: " << st.counts().used_write_value);
  }
  // --- adjacency facet width: an edge entry's used_write = its uid code width + its facet
  // bytes. A knows facet {creationDate: i64} in prim bytes = 'd' + uvarint(1) + uvarint(12) +
  // "creationDate" + 'i' + 8 = 24 B. Entry {1, 2, 3} + 300 with facets: uid width 2 + facet 24.
  {
    Value facet = make_row({{"creationDate", Value(int64_t(1262304000000))}});
    std::string fb;
    prim::encode(fb, facet);
    CHECK(fb.size() == 24, "knows facet dict = 24 B in prim bytes: " << fb.size());
    std::vector<std::pair<uint64_t, Value>> postings = {{1, facet}, {2, facet}, {3, facet}, {300, facet}};
    std::string enc = posting::encode_adjacency(postings, true, posting::AdjCodec::named());
    // 'A' + uvarint(4) + UidPack(19 - 1 = 18 B: the 'U' head is not repeated) + 4 x 24 facet bytes
    CHECK(enc.size() == 1 + 1 + 18 + 4 * 24, "adjacency of 4 dated entries = 116 B: " << enc.size());
    std::vector<uint8_t> uw;
    std::vector<uint32_t> fw;
    posting::decode_adjacency_w(enc, &uw, &fw, posting::AdjCodec::named());
    CHECK(uw.size() == 4 && uw[3] == 2 && fw[3] == 24, "the 300 entry: uid width 2, facet width 24 (what an insert_edge charges as used_write)");
  }
  // --- update_edge_prop's unit rewrite (unit::adjacency_set): 4 dated entries at chunk_target 30 -> one
  // entry (1 + 1 + 9-byte uid pack + 24 B facet = 35 B) per part. Setting the facet of the entry starting part 30
  // rewrites exactly that part; used_write = the entry's new facet bytes (24); the others' facets stay.
  {
    Value f1 = make_row({{"creationDate", Value(int64_t(1000))}});
    Value f2 = make_row({{"creationDate", Value(int64_t(2000))}});
    std::vector<std::pair<uint64_t, Value>> postings = {{10, f1}, {20, f1}, {30, f1}, {40, f1}};
    MemSink sink;
    std::string ukey = "adjunit";
    unit::write(sink, ukey, postings.size(), [&](size_t lo, size_t hi) {
      return posting::encode_adjacency({postings.begin() + lo, postings.begin() + hi}, true, posting::AdjCodec::named());
    }, 30, [&](size_t e) { return postings[e].first; });
    size_t nparts = 0;
    for (auto& [k, v] : sink.kv) nparts += unit::is_part_key(k, ukey.size());
    CHECK(nparts == 4, "4 one-entry parts at a 30 B target: " << nparts);
    StoreOptions so;
    auto store = Store::open_read_write(dir + "/adjset", so, true);
    for (auto& [k, v] : sink.kv) store->db()->Put(rocksdb::WriteOptions(), k, v);
    ShadowOverlay ov;
    CountingStore st(*store, &ov);
    std::vector<std::pair<std::string, std::string>> puts;
    st.reset();
    bool hit = unit::adjacency_set(st, ukey, 30, f2, puts, posting::AdjCodec::named());
    CHECK(hit && puts.size() == 1 && puts[0].first == unit::part_key(ukey, 30), "one put, of the part holding uid 30 (start 30)");
    auto after = posting::decode_adjacency(puts.empty() ? std::string() : puts[0].second, posting::AdjCodec::named());
    CHECK(after.size() == 1 && after[0].first == 30 && after[0].second.get("creationDate").as_int() == 2000,
          "the entry carries the new facet");
    CHECK(st.counts().used_write_value == 24, "used_write = the new facet's 24 B: " << st.counts().used_write_value);
    CHECK(st.counts().G == 0 && st.counts().S == 2 && st.counts().N == 0 && st.counts().raw_gets == 0,
          "the RMW read is counted (unit::rmw_locate: a look at the unit key lands on part 0, then ONE reverse seek onto "
          "the part holding the id): S " << st.counts().S << " N " << st.counts().N << " raw_gets " << st.counts().raw_gets);
    // the split locate's two landings: part 0 (the look at the unit key: a key crossed, its value unread), then the
    // part holding the id (its value read). Two part keys in scan_key; the unit key used once over both landings
    // (used_key alone is the same on a one-seek locate: the landing count is what makes it a check of "once")
    const uint64_t pk = unit::part_key(ukey, 0).size();
    CHECK(st.counts().scan_key == 2 * pk, "scan_key = two part keys of " << pk << " B (part 0's landing + the part read): "
          << st.counts().scan_key);
    {
      st.parts_commit();
      HistParts h;
      st.take_committed_hists(h);
      uint64_t crossed = 0, values_read = 0;
      for (auto& p : h) { crossed += p.keys_crossed; values_read += p.values_read; }
      CHECK(crossed == 1 && values_read == 1, "part 0's landing is a key crossed, the part holding the id a value read: crossed "
            << crossed << " values_read " << values_read);
    }
    CHECK(st.counts().S == 2 && st.counts().used_key == ukey.size(),
          "the unit key is used once per locate over its two landings, not per seek: S " << st.counts().S << " used_key "
          << st.counts().used_key);
    puts.clear();
    CHECK(!unit::adjacency_set(st, ukey, 35, f2, puts, posting::AdjCodec::named()) && puts.empty(), "an entry no part holds: false, nothing written");
    CHECK(!unit::adjacency_set(st, "no-such-unit", 30, f2, puts, posting::AdjCodec::named()) && puts.empty(), "an absent unit: false, nothing written");
    // a whole unit: the look at its key is the landing, 1 SEEK
    store->db()->Put(rocksdb::WriteOptions(), "adjwhole", posting::encode_adjacency({{10, f1}}, true, posting::AdjCodec::named()));
    st.reset();
    CHECK(unit::adjacency_set(st, "adjwhole", 10, f2, puts, posting::AdjCodec::named()) && puts.size() == 1 && puts[0].first == "adjwhole",
          "a whole unit is rewritten under its own key");
    CHECK(st.counts().S == 1 && st.counts().S_miss == 0 && st.counts().N == 0, "a whole unit's locate is 1 SEEK: S " << st.counts().S);
    puts.clear();
    // an absent unit whose forward landing is a foreign key AFTER it ("adjunis" < "adjunit|0x00|0"): 1 SEEK, a miss, the
    // landed key in the miss lane (a reverse seek from "adjunis|0x00|id" lands before it: on nothing here)
    st.reset();
    CHECK(!unit::adjacency_set(st, "adjunis", 30, f2, puts, posting::AdjCodec::named()) && puts.empty(), "absent: nothing written");
    CHECK(st.counts().S == 1 && st.counts().S_miss == 1 && st.counts().seek_miss_key == unit::part_key(ukey, 0).size() &&
          st.counts().used_key == 0, "the absent landing: 1 SEEK, a miss on the next unit's part 0 key (16 B), nothing used: S "
          << st.counts().S << " S_miss " << st.counts().S_miss << " seek_miss_key " << st.counts().seek_miss_key);
  }
  // --- split-term scenarios: 12 dense uids at chunk_target 23 -> greedy maximal parts: 9 uids
  // (8 deltas = 2 groups = 23 B) + 3 uids (18 B) --------------------------------------------
  {
    PostingIndex ix(s, 23);
    MemSink sink;
    for (uint64_t i = 1; i <= 12; i++)
      ix.ingest_vertex(sink, "Post", make_uid(s, "Post", i), make_row({{"creationDate", Value(int64_t(5000))}}));
    ix.finalize(sink);
    const IndexDecl& cd = s.index_decl("Post", "creationDate");
    std::string tkey = PostingIndex::term_key(cd.id, index_value_bytes(s, cd, "Post", Value(int64_t(5000))));
    size_t parts = 0;
    for (auto& [k, v] : sink.kv) parts += unit::is_part_key(k, tkey.size());
    CHECK(parts == 2, "12 dense uids at a 23 B target split into 2 parts (9 + 3 uids): " << parts);
    StoreOptions so;
    auto store = Store::open_read_write(dir + "/split", so, true);
    for (auto& [k, v] : sink.kv) store->db()->Put(rocksdb::WriteOptions(), k, v);
    ShadowOverlay ov;
    CountingStore st(*store, &ov);
    std::vector<std::pair<std::string, std::string>> puts;
    std::vector<std::string> dels;
    IndexWrites w{puts, dels};
    // insert id 13: lands in the LAST part (first uid 10 <= 13): one put of that part only
    // (3 -> 4 uids, still 18 B), the type posting aside
    st.reset();
    ix.w_insert(st, "Post", make_uid(s, "Post", 13), make_row({{"creationDate", Value(int64_t(5000))}}), w);
    std::vector<std::pair<std::string, std::string>> term_puts;
    for (auto& p : puts) if (unit::owns(p.first, tkey)) term_puts.push_back(p);
    CHECK(term_puts.size() == 1, "a split-term insert rewrites exactly one part: " << term_puts.size());
    CHECK(term_puts.size() == 1 && term_puts[0].first == unit::part_key(tkey, make_uid(s, "Post", 10)),
          "…the covering part (keyed by its first uid, Post 10)");
    CHECK(term_puts.size() == 1 && posting::decode_posting(term_puts[0].second).size() == 4,
          "…now 4 uids: " << (term_puts.empty() ? 0 : posting::decode_posting(term_puts[0].second).size()));
    CHECK(term_puts.size() == 1 && term_puts[0].second.size() == 18, "…still 18 B (one group)");
    puts.clear(); dels.clear();
    // delete every uid of part 0 (ids 1..9) -> nine single-part rewrites, the last leaving an
    // EMPTY 'U' part that stays in place (its range [0, Post 10) keeps a part to land on)
    st.reset();
    term_puts.clear();
    for (uint64_t i = 1; i <= 9; i++) {
      ix.w_delete(st, "Post", make_uid(s, "Post", i), make_row({{"creationDate", Value(int64_t(5000))}}), w);
      for (auto& p : puts) {                    // apply each delete's puts (the overlay) before the next
        if (unit::owns(p.first, tkey)) term_puts.push_back(p);
        st.put(p.first, p.second);
      }
      puts.clear(); dels.clear();
    }
    CHECK(term_puts.size() == 9 && term_puts.back().first == unit::part_key(tkey, 0) && term_puts.back().second == "U",
          "nine deletes rewrite part 0 nine times; the emptied part is kept as 'U' (" << term_puts.size() << ", last "
          << (term_puts.empty() ? std::string("-") : term_puts.back().second.substr(0, 1)) << ")");
    // (each delete's put replaces the previous in the overlay; the store never sees a hole)
  }
  // --- the apply-mode RocksDB batch-bytes formula: 12 B header + record per single-op batch ----
  {
    StoreOptions so;
    auto store = Store::open_read_write(dir + "/apply", so, true);
    CountingStore st(*store, nullptr, /*apply_writes=*/true);
    st.put("k1", "value");            // 12 + 1 + 1 + 2 + 1 + 5 = 22
    st.del_key("k2");                 // 12 + 1 + 1 + 2 = 16
    CHECK(st.counts().P == 1 && st.counts().D == 1, "one put, one delete");
    CHECK(st.counts().rocks_write_bytes == 38, "expected batch bytes 22 + 16 = 38: " << st.counts().rocks_write_bytes);
    CHECK(store->ticker(rocksdb::NUMBER_KEYS_WRITTEN) == 2, "RocksDB counts 2 keys written");
    CHECK(store->ticker(rocksdb::BYTES_WRITTEN) == 38, "RocksDB's bytes written == the formula: " << store->ticker(rocksdb::BYTES_WRITTEN));
    // the audited RMW read: a raw_get hit moves RocksDB's keys.read and our raw_gets alike
    uint64_t kr0 = store->ticker(rocksdb::NUMBER_KEYS_READ);
    auto v = st.raw_get("k1");
    CHECK(v && *v == "value" && st.counts().raw_gets == 1 && store->ticker(rocksdb::NUMBER_KEYS_READ) == kr0 + 1,
          "raw_get hit: raw_gets 1, keys.read +1, logical G unchanged (" << st.counts().G << ")");
    CHECK(st.counts().G == 0, "raw_get is not a logical GET");
    // a MISS is a Get call too: RocksDB ticks keys.read per call, so the audit counts calls
    auto miss = st.raw_get("no-such-key");
    CHECK(!miss && st.counts().raw_gets == 2 && store->ticker(rocksdb::NUMBER_KEYS_READ) == kr0 + 2,
          "raw_get miss: raw_gets 2, keys.read +2");
  }
  if (fails) { std::cerr << fails << " failures\n"; return 1; }
  std::cout << "test_write_counts ok\n";
  return 0;
}

}  // namespace
