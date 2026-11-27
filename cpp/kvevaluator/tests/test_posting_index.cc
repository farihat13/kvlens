// PostingIndex must answer every index question with the SAME vids as SortedIndex (only the
// counts differ): eq / range / enumerate / top_k_desc (with and without a candidate set, with
// and without a hi bound) over the real LDBC schema and a synthetic vertex set with many ties;
// repeated with a tiny chunk_target so every posting splits into parts (store/unit_value.h),
// where the read counts must follow the part count; then the write surface (insert / delete /
// update) on both backends, re-checked for equality; plus the str-term prefix guard.
#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <map>
#include <set>
#include <string>
#include <unistd.h>

#include "formats/format.h"
#include "index/index_backend.h"
#include "index/posting_index.h"
#include "index/sorted_index.h"
#include "schema/schema.h"
#include "store/counting.h"
#include "store/rocksdb_store.h"
#include "util/uidset.h"
#include "util/vid.h"
#include "repo_paths.h"
#include "harness.h"

namespace {

using namespace evalcpp;


// a KvSink that just remembers (key, value) pairs; index keys (put_index) carry empty values
using MemSink = kvt::MemSink;

struct Vertex { std::string label; uint64_t uid; Value props; };

static uint64_t make_uid(const Schema& s, const std::string& label, uint64_t id) {
  return (uint64_t(s.tag_of_label(label)) << kVidShift) | id;
}

static std::vector<Vertex> synth(const Schema& s) {
  // small value pools -> many ties per term (multi-uid postings), a few near-unique dates
  std::vector<std::string> names = {"John", "Johnny", "Jo", "Alice", "Bob", "John"};
  std::vector<int64_t> dates = {1000, 1000, 1000, 2000, 3000, 3000, 4000, 5000, 6000, 7000, 7000};
  std::vector<Vertex> out;
  uint64_t seed = 12345;
  auto rnd = [&]() { seed = seed * 6364136223846793005ull + 1442695040888963407ull; return seed >> 33; };
  for (auto& label : {"Person", "Post", "Comment"}) {
    for (uint64_t i = 1; i <= 300; i++) {
      RowFields f;
      f.emplace_back("id", Value(int64_t(i)));
      f.emplace_back("creationDate", Value(dates[rnd() % dates.size()]));
      if (std::string(label) == "Person") f.emplace_back("firstName", Value(names[rnd() % names.size()]));
      out.push_back({label, make_uid(s, label, i), Value::row(std::move(f))});
    }
  }
  return out;
}

struct Built {
  std::unique_ptr<IndexBackend> ix;
  std::unique_ptr<Store> store;
  std::unique_ptr<CountingStore> st;
  size_t keys = 0;
};

static Built build(const Schema& s, std::unique_ptr<IndexBackend> ix, const std::vector<Vertex>& vs, const std::string& dir) {
  MemSink sink;
  for (auto& v : vs) ix->ingest_vertex(sink, v.label, v.uid, v.props);
  ix->finalize(sink);
  StoreOptions so;
  Built b;
  b.store = Store::open_read_write(dir, so, true);
  for (auto& [k, v] : sink.kv)
    if (!b.store->db()->Put(rocksdb::WriteOptions(), k, v).ok()) throw std::runtime_error("put failed");
  b.keys = sink.kv.size();
  b.st = std::make_unique<CountingStore>(*b.store);
  b.ix = std::move(ix);
  return b;
}

static std::multiset<std::string> vids(const ValueList& l) {
  std::multiset<std::string> out;
  for (auto& v : l) out.insert(v.text());   // a vid or its str
  return out;
}
static std::multiset<std::string> hits(const std::vector<IndexHit>& h) {
  std::multiset<std::string> out;
  for (auto& x : h) out.insert(x.label + ":" + hex16(x.vid) + ":" + std::to_string(x.value));
  return out;
}

static void apply(Built& b, std::vector<std::pair<std::string, std::string>>& puts, std::vector<std::string>& dels) {
  for (auto& k : dels) b.store->db()->Delete(rocksdb::WriteOptions(), k);
  for (auto& [k, v] : puts) b.store->db()->Put(rocksdb::WriteOptions(), k, v);
  puts.clear();
  dels.clear();
}

// every read question, asked of both backends; `tag` names the configuration under test
static void compare_reads(const Schema& s, Built& a, Built& b, const std::string& tag) {
  for (auto& name : {"John", "Johnny", "Jo", "Alice", "Bob", "Nobody"}) {
    ValueList x = a.ix->eq(*a.st, "Person", "firstName", Value(std::string(name)));
    ValueList y = b.ix->eq(*b.st, "Person", "firstName", Value(std::string(name)));
    CHECK(vids(x) == vids(y), tag << " eq firstName=" << name << ": " << x.size() << " vs " << y.size());
    if (std::string(name) == "John") CHECK(!x.empty(), tag << " John has entries");
  }
  for (auto& label : {"Person", "Post", "Comment"}) {
    ValueList x = a.ix->enumerate(*a.st, label), y = b.ix->enumerate(*b.st, label);
    CHECK(vids(x) == vids(y), tag << " enumerate " << label << ": " << x.size() << " vs " << y.size());
    for (auto [lo, hi] : {std::pair<int64_t, int64_t>{0, 10000}, {1000, 1001}, {2000, 4000}, {3000, 3000}, {5500, 20000}, {-5, 0}}) {
      ValueList rx = a.ix->range(*a.st, {label}, "creationDate", lo, hi);
      ValueList ry = b.ix->range(*b.st, {label}, "creationDate", lo, hi);
      CHECK(vids(rx) == vids(ry), tag << " range " << label << " [" << lo << "," << hi << "): " << rx.size() << " vs " << ry.size());
    }
  }
  // top-k: several k / hi combos, global and with a candidate set
  std::vector<std::string> labels = {"Post", "Comment"};
  UidSet cand(64);
  for (uint64_t i = 1; i <= 300; i += 7) { cand.insert(make_uid(s, "Post", i)); cand.insert(make_uid(s, "Comment", i + 1)); }
  for (int64_t k : {1, 3, 20, 1000}) {
    for (std::optional<int64_t> hi : {std::optional<int64_t>{}, std::optional<int64_t>{7000}, std::optional<int64_t>{7001},
                                      std::optional<int64_t>{3500}, std::optional<int64_t>{1000}, std::optional<int64_t>{INT64_MIN}}) {
      const UidSet* cands[2] = {nullptr, &cand};
      for (const UidSet* c : cands) {
        std::vector<IndexHit> hx = a.ix->top_k_desc(*a.st, labels, "creationDate", k, hi, c, {});
        std::vector<IndexHit> hy = b.ix->top_k_desc(*b.st, labels, "creationDate", k, hi, c, {});
        CHECK(hits(hx) == hits(hy), tag << " top_k k=" << k << " hi=" << (hi ? std::to_string(*hi) : "none")
                                       << (c ? " cand" : " global") << ": " << hx.size() << " vs " << hy.size());
        // the k-th rule: at least k hits when available, every extra one a tie at the k-th value
        if (int64_t(hx.size()) > k) {
          std::vector<IndexHit> sorted = hx;
          std::stable_sort(sorted.begin(), sorted.end(), [](auto& p, auto& q) { return p.value > q.value; });
          CHECK(sorted.back().value == sorted[size_t(k - 1)].value, tag << " top_k extra hits are ties");
        }
      }
    }
  }
}

TEST_CASE(test_posting_index) {
  std::string path = ldbc_schema_dir() + "/schema.json";
  Schema s = Schema::load(path);
  std::vector<Vertex> vs = synth(s);
  char tmpl[] = "/tmp/evalcpp-pidx-XXXXXX";
  std::string dir = kvt::temp_dir(tmpl);

  Built sorted = build(s, std::make_unique<SortedIndex>(s), vs, dir + "/sorted");
  Built post = build(s, std::make_unique<PostingIndex>(s, 65536), vs, dir + "/posting");
  Built split = build(s, std::make_unique<PostingIndex>(s, 48), vs, dir + "/split");   // every posting in parts
  CHECK(post.keys < sorted.keys, "posting has fewer keys (" << post.keys << " vs " << sorted.keys << ")");
  CHECK(split.keys > post.keys, "the split store has part keys (" << split.keys << " vs " << post.keys << ")");

  compare_reads(s, sorted, post, "whole");
  compare_reads(s, sorted, split, "split");

  // counts of one split unit read: the Person type posting (300 uids) is several parts
  {
    split.st->reset();
    ValueList e = split.ix->enumerate(*split.st, "Person");
    const Counts& c = split.st->counts();
    size_t parts = 0;
    std::string tk = PostingIndex::term_key(s.type_index_decl("Person").id, "");
    std::unique_ptr<rocksdb::Iterator> it(split.store->db()->NewIterator(rocksdb::ReadOptions()));
    for (it->Seek(tk); it->Valid() && it->key().starts_with(tk); it->Next()) parts++;
    CHECK(parts > 1, "split Person type posting has parts: " << parts);
    // greedy maximal packing: the 300-uid posting at 48 B/part needs about 300 / ((48 - 13) / 1 + 1)
    // parts -- no part but the last may be under half the target
    {
      std::unique_ptr<rocksdb::Iterator> it2(split.store->db()->NewIterator(rocksdb::ReadOptions()));
      size_t seen = 0;
      for (it2->Seek(tk); it2->Valid() && it2->key().starts_with(tk); it2->Next()) {
        seen++;
        if (seen < parts) CHECK(it2->value().size() > 24, "part " << seen << " is packed (" << it2->value().size() << " B of 48)");
        CHECK(it2->value().size() <= 48, "part " << seen << " within the target (" << it2->value().size() << " B)");
      }
    }
    // 1 SEEK (part 0) + 1 NEXT per further part + the NEXT past the last part (the absence miss,
    // counted in N as every scan end is)
    CHECK(c.S == 1 && c.N == parts && c.N_miss == 1 && c.S_miss == 0,
          "split unit read counts: S=" << c.S << " N=" << c.N << " N_miss=" << c.N_miss << " parts=" << parts);
    CHECK(e.size() == 300, "split enumerate size " << e.size());
    // and a whole unit is exactly 1 SEEK, 0 NEXT
    post.st->reset();
    post.ix->enumerate(*post.st, "Person");
    CHECK(post.st->counts().S == 1 && post.st->counts().N == 0 && post.st->counts().N_miss == 0,
          "whole unit read is 1 SEEK: S=" << post.st->counts().S << " N=" << post.st->counts().N);
    // an absent term: 1 SEEK in the miss lane
    post.st->reset();
    post.ix->eq(*post.st, "Person", "firstName", Value(std::string("Nobody")));
    CHECK(post.st->counts().S == 1 && post.st->counts().S_miss == 1, "absent term is a miss");
  }
  // str terms: "Jo" / "John" / "Johnny" are distinct terms even though each is a byte-prefix of the next
  {
    std::string kjo = PostingIndex::term_key(s.index_decl("Person", "firstName").id, index_value_bytes(s, s.index_decl("Person", "firstName"), "Person", Value(std::string("Jo"))));
    std::string kjohn = PostingIndex::term_key(s.index_decl("Person", "firstName").id, index_value_bytes(s, s.index_decl("Person", "firstName"), "Person", Value(std::string("John"))));
    CHECK(kjohn.substr(0, kjo.size()) != kjo, "a longer str term does not extend a shorter term's key");
  }

  // writes: insert a vertex, update its indexed props, delete another -- on all three, then re-compare
  for (Built* b : {&sorted, &post, &split}) {
    std::vector<std::pair<std::string, std::string>> puts;
    std::vector<std::string> dels;
    IndexWrites w{puts, dels};
    Value np = make_row({{"id", Value(int64_t(9001))}, {"creationDate", Value(int64_t(3000))}, {"firstName", Value(std::string("John"))}});
    b->ix->w_insert(*b->st, "Person", make_uid(s, "Person", 9001), np, w);
    apply(*b, puts, dels);
    Value up = make_row({{"id", Value(int64_t(9001))}, {"creationDate", Value(int64_t(8000))}, {"firstName", Value(std::string("John"))}});
    b->ix->w_update(*b->st, "Person", make_uid(s, "Person", 9001), np, up, "creationDate", w);
    apply(*b, puts, dels);
    b->ix->w_update(*b->st, "Person", make_uid(s, "Person", 9001), up, up, "lastName", w);   // unindexed: no-op
    // an unindexed prop changes no index, on every layout: an update moves only the CHANGED column's entry
    // (re-writing every index on any update is NebulaGraph's engine rule -- the hand-written nebula format
    // keeps its own index code for that; these backends price the layout)
    CHECK(puts.empty() && dels.empty(), "unindexed update writes nothing (" + b->ix->layout() + ")");
    apply(*b, puts, dels);
    if (b->ix->layout() == "posting") {
      // pinned: a same-value indexed update and a delete of an absent entry / absent term are 0 writes
      b->ix->w_update(*b->st, "Person", make_uid(s, "Person", 9001), up, up, "creationDate", w);
      CHECK(puts.empty() && dels.empty(), "same-value indexed update writes nothing on posting");
      b->ix->w_delete(*b->st, "Person", make_uid(s, "Person", 7777),
                      make_row({{"creationDate", Value(int64_t(3000))}, {"firstName", Value(std::string("Zed"))}}), w);
      CHECK(puts.empty() && dels.empty(), "delete of an absent entry / absent term writes nothing on posting");
    }
    // delete vertex 5 of every label (its stored row is what the caller knows)
    for (auto& v : vs)
      if (v.props.get("id").as_int() == 5) b->ix->w_delete(*b->st, v.label, v.uid, v.props, w);
    apply(*b, puts, dels);
    // and a delete inside a SPLIT posting: Person 150 (mid-range of the type posting)
    for (auto& v : vs)
      if (v.label == "Person" && v.props.get("id").as_int() == 150) b->ix->w_delete(*b->st, v.label, v.uid, v.props, w);
    apply(*b, puts, dels);
  }
  compare_reads(s, sorted, post, "after-writes whole");
  compare_reads(s, sorted, split, "after-writes split");
  {
    ValueList e = post.ix->enumerate(*post.st, "Person");
    CHECK(e.size() == 299, "Person count after insert + 2 deletes: " << e.size());
    ValueList r = post.ix->range(*post.st, {"Person"}, "creationDate", 8000, 8001);
    CHECK(r.size() == 1 && r[0].text() == hex16(make_uid(s, "Person", 9001)), "updated date found at its new term");
    ValueList r0 = post.ix->range(*post.st, {"Person"}, "creationDate", 3000, 3001);
    for (auto& v : r0) CHECK(v.text() != hex16(make_uid(s, "Person", 9001)), "updated date gone from its old term");
  }
  if (fails) { std::cerr << fails << " failures\n"; return 1; }
  std::cout << "test_posting_index ok\n";
  return 0;
}

}  // namespace
