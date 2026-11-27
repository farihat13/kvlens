// A null-valued index entry (only a write makes one: a declared index column with no value) must never be read as a
// value. Real engines keep nulls as one block before every value (VelocyPack null < numbers < strings; NebulaGraph's
// null flag), and so does the Python oracle (its empty term sorts first); here the null term is the INT64_MIN slot of
// a sorted column and the VPack null byte of a str column. Checked on the shared sorted index (NebulaGraph's) and on
// ArangoDB's own walks: range from an open lower bound, a window that holds a null entry's old position, top-k past
// every dated entry, eq on the empty string, and enumeration (which must still list the null-valued vertex).
#include <cstdint>
#include <filesystem>
#include <iostream>
#include <map>
#include <set>
#include <string>
#include <unistd.h>

#include "exec/pool.h"
#include "exec/step_exec.h"
#include "formats/arangodb/format.h"
#include "formats/format.h"
#include "index/sorted_index.h"
#include "schema/schema.h"
#include "store/counting.h"
#include "store/rocksdb_store.h"
#include "util/vid.h"
#include "repo_paths.h"
#include "harness.h"

namespace {

using namespace evalcpp;


using MemSink = kvt::MemSink;

static uint64_t uid(uint64_t tag, uint64_t id) { return (tag << kVidShift) | id; }

static std::set<std::string> strs(const ValueList& l) {
  std::set<std::string> out;
  for (auto& v : l) out.insert(v.is_null() ? std::string("<null>") : v.text());   // a vid or its str
  return out;
}

// Persons 1..6 with dates 1000..6000 and first names; two null-valued Persons as a write leaves them: one whose old
// null key sorted INSIDE the date window (tag 8, id 2500: its vid bytes read as a date between 2000 and 3000), one at
// the bottom (tag 0). The str index gets a null entry for a tag-4 vid: its first byte 0x40 = the empty string's term.
static const int64_t kDates[] = {1000, 2000, 3000, 4000, 5000, 6000};
static const uint64_t kInside = uid(8, 2500), kBottom = uid(0, 77), kNameNull = uid(4, 5);

static void sorted_index_case(const Schema& s, const std::string& dir) {
  SortedIndex ix(s);
  MemSink sink;
  for (int i = 0; i < 6; i++)
    ix.ingest_vertex(sink, "Person", uid(0, uint64_t(i + 1)),
                     Value::row({{"creationDate", Value(kDates[i])}, {"firstName", Value(std::string("N") + char('a' + i))}}));
  ix.finalize(sink);
  for (uint64_t u : {kInside, kBottom, kNameNull})
    for (auto& k : ix.vertex_write_keys("Person", u, Value::row({}))) sink.kv[k] = "";   // as an insert with no value writes them
  StoreOptions so;
  auto store = Store::open_read_write(dir, so, true);
  for (auto& [k, v] : sink.kv) store->db()->Put(rocksdb::WriteOptions(), k, v);
  CountingStore st(*store);
  std::set<std::string> dated;
  for (int i = 0; i < 6; i++) dated.insert(hex16(uid(0, uint64_t(i + 1))));

  for (auto [lo, hi] : {std::pair<int64_t, int64_t>{0, 10000}, {INT64_MIN, 10000}, {INT64_MIN, INT64_MAX}}) {
    std::set<std::string> got = strs(ix.range(st, {"Person"}, "creationDate", lo, hi));
    CHECK(got == dated, "sorted range [" << lo << "," << hi << "): " << got.size() << " vids, want the 6 dated");
    std::set<std::string> hits;
    for (auto& h : ix.range_hits(st, {"Person"}, "creationDate", lo, hi)) hits.insert(hex16(h.vid));
    CHECK(hits == dated, "sorted range_hits [" << lo << "," << hi << "): " << hits.size() << " vids, want the 6 dated");
  }
  {
    std::set<std::string> got;
    for (auto& h : ix.top_k_desc(st, {"Person"}, "creationDate", 100, std::nullopt, nullptr, {})) got.insert(hex16(h.vid));
    CHECK(got == dated, "sorted top_k_desc past every date: " << got.size() << " hits, want the 6 dated");
  }
  CHECK(ix.eq(st, "Person", "firstName", Value(std::string(""))).empty(), "sorted eq on \"\": a null entry matched");
  CHECK(ix.eq(st, "Person", "firstName", Value(std::string("Nc"))).size() == 1, "sorted eq on a real name");
  std::set<std::string> all = strs(ix.enumerate(st, "Person"));
  CHECK(all.size() == 9 && all.count(hex16(kInside)) && all.count(hex16(kBottom)),
        "sorted enumerate lists every Person, the null-valued ones too: " << all.size());
}

// the walks under test are protected (the engine twin reads through them): a test subclass names them
struct ArangoUnderTest : arangodb::Arango {
  using arangodb::Arango::Arango;
  using arangodb::Arango::index_write_key;
  using arangodb::Arango::read_index_eq;
  using arangodb::Arango::read_index_range;
  using arangodb::Arango::top_k_reads;
  using arangodb::Arango::term_bytes;
};

// a str index term from a vid: a vid is its hex16 str, so its term is the str's, never repr's (a named-codec
// document decodes a 16-hex str as a vid; an update then deletes its old index key by this term)
static void vid_term_case(const Schema& s) {
  ArangoUnderTest fmt(s, {});
  const IndexDecl& ix = s.index_decl("Person", "firstName");
  for (uint64_t u : {uint64_t(0x0123456789abcdefULL), uint64_t(7), kInside}) {
    Value v = Value::vid(u), t = Value(hex16(u));
    CHECK(index_value_bytes(s, ix, "Person", v) == index_value_bytes(s, ix, "Person", t),
          "sorted index: the term of vid " << hex16(u) << " differs from its str's");
    for (bool query : {false, true})
      CHECK(fmt.term_bytes("Person", "firstName", v, query) == fmt.term_bytes("Person", "firstName", t, query),
            "arango: the term of vid " << hex16(u) << " differs from its str's (query " << query << ")");
  }
}

static void arango_case(const Schema& s, const std::string& dir) {
  ArangoUnderTest fmt(s, {});
  MemSink sink;
  for (int i = 0; i < 6; i++)
    fmt.add_vertex(sink, uid(0, uint64_t(i + 1)), "Person",
                   Value::row({{"creationDate", Value(kDates[i])}, {"firstName", Value(std::string("N") + char('a' + i))}}));
  fmt.add_vertex(sink, kBottom, "Person", Value::row({}));   // docid 7: ingest writes no index entry for it
  fmt.finalize(sink);
  for (auto& p : s.index_cols_of("Person")) sink.kv[fmt.index_write_key("Person", p, Value(), 7)] = "";   // as an insert writes them
  StoreOptions so;
  auto store = Store::open_read_write(dir, so, true);
  for (auto& [k, v] : sink.kv) store->db()->Put(rocksdb::WriteOptions(), k, v);
  ThreadPool pool(*store, 1, {}, 2, false, nullptr, false);
  StepExec ex(pool, 1, false);
  std::set<std::string> dated;
  for (int i = 0; i < 6; i++) dated.insert(hex16(uid(0, uint64_t(i + 1))));
  for (auto [lo, hi] : {std::pair<int64_t, int64_t>{0, 10000}, {INT64_MIN, 10000}}) {
    std::set<std::string> got = strs(fmt.read_index_range(ex, {"Person"}, "creationDate", lo, hi).as_list());
    CHECK(got == dated, "arango range [" << lo << "," << hi << "): " << got.size() << " rows, want the 6 dated");
  }
  {
    std::set<std::string> got;
    kv::TopKArgs t{Value(), "creationDate", 100, Value(), {"Person"}, {}, false};   // the parsed args (ops/kv_contract.h)
    Value rows = top_k_over(ex, t, fmt.top_k_reads(ex, t));                                                 // a local: a
    for (auto& r : rows.as_list()) got.insert(r.get("_vid").text());                                       // temporary's list dangles
    CHECK(got == dated, "arango top-k past every date: " << got.size() << " hits, want the 6 dated");
  }
  CHECK(fmt.read_index_eq(ex, "Person", "firstName", Value(std::string(""))).as_list().empty(),
        "arango eq on \"\": a null entry matched");
}

TEST_CASE(test_index_nulls) {
  std::string path = ldbc_schema_dir() + "/schema.json";
  Schema s = Schema::load(path);
  char tmpl[] = "/tmp/test_index_nulls_XXXXXX";
  std::string dir = kvt::temp_dir(tmpl);
  sorted_index_case(s, dir + "/sorted");
  arango_case(s, dir + "/arango");
  vid_term_case(s);
  std::filesystem::remove_all(dir);
  if (fails) { std::cerr << fails << " of " << checks << " checks failed\n"; return 1; }
  std::cout << "test_index_nulls ok (" << checks << " checks)\n";
  return 0;
}

}  // namespace
