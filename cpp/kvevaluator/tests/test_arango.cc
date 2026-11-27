// arangodb's VPack-mirror codec against Python-generated fixtures (tests/fixtures/arango.json):
// documents and packed strings byte-identical, decode round-trips; plus the key codecs' shapes.
#include <fstream>
#include <iostream>

#include <map>

#include <filesystem>
#include <unistd.h>

#include "formats/arangodb/engine.h"
#include "formats/arangodb/format.h"
#include "formats/arangodb/keys.h"
#include "formats/arangodb/vpack.h"
#include "formats/common.h"
#include "schema/schema.h"
#include "store/rocksdb_store.h"
#include "value/value.h"
#include "harness.h"

namespace {

using namespace evalcpp;
using namespace evalcpp::arangodb;


static std::string from_hex(const std::string& h) {
  std::string s;
  for (size_t i = 0; i + 1 < h.size(); i += 2) s.push_back(char(std::stoi(h.substr(i, 2), nullptr, 16)));
  return s;
}

TEST_CASE(test_arango) {
  std::string path = argc > 1 ? argv[1] : std::string(FIXTURE_DIR) + "/arango.json";
  std::ifstream f(path);
  if (!f) { std::cerr << "no fixture at " << path << "\n"; return 1; }
  json fx = json::parse(f);
  for (auto& c : fx["docs"]) {
    Value doc = from_json(c["doc"]);
    std::string want = from_hex(c["hex"].get<std::string>());
    std::string got;
    vp_encode_obj(got, doc);
    CHECK(got == want, "doc encode differs (" << got.size() << " vs " << want.size() << " bytes): " << repr(doc));
    CHECK(equals(vp_decode_doc(want), doc), "doc decode differs: " << repr(vp_decode_doc(want)));
  }
  for (auto& c : fx["strs"]) {
    std::string s = c["s"].get<std::string>();
    std::string want = from_hex(c["hex"].get<std::string>());
    CHECK(vp_encode_str(s) == want, "str encode differs for '" << s << "'");
    size_t i = 0;
    Value d = vp_decode(want, i);   // a canonical hex16 decodes as a vid, any other str as a str: the same text
    CHECK(d.text() == s && i == want.size(), "str decode round trip for '" << s << "'");
  }
  {   // a `_key` decodes as a vid only when it was written as one (16 lowercase hex); any other spelling stays a str and
      // is refused as an id, never read as another spelling of a uid (it is another string in Python)
    for (std::string k : {"0000000000000abc", "0000000000000ABC", "abc", "Person/0000000000000abc", "0000000000000abcd"}) {
      std::string enc;
      vp_encode_obj(enc, make_row({{"_key", Value(k)}}));
      Value got = vp_decode_doc(enc).get("_key");
      bool canon = k == "0000000000000abc";
      CHECK(got.is_vid() == canon && got.text() == k, "_key '" << k << "' decodes as " << repr(got));
      bool threw = false;
      try { uid_or_throw("arangodb", got); } catch (const std::runtime_error&) { threw = true; }
      CHECK(threw == !canon, "_key '" << k << "': uid_or_throw " << (threw ? "threw" : "passed"));
    }
    std::string a, b, c;   // the RMW path: a decoded vid re-encodes byte for byte, and a vid value as its hex16 str
    vp_encode_obj(a, make_row({{"_key", Value(std::string("0000000000000abc"))}}));
    vp_encode_obj(b, vp_decode_doc(a));
    vp_encode_obj(c, make_row({{"_key", Value::vid(0xabc)}}));
    CHECK(a == b && a == c, "a vid _key encodes as its canonical str does");
  }
  CHECK(doc_key(3, 5).size() == 13 && pk_key(3, 5).size() == 13 && edge_key(true, 7, 5, 9).size() == 21, "key shapes");
  CHECK(has_prefix(edge_key(true, 7, 5, 9), edge_prefix(true, 7, 5)), "edge prefix");
  CHECK(!has_prefix(edge_key(false, 7, 5, 9), edge_prefix(true, 7, 5)), "f/t are different prefixes");
  Loc l = loc_of(loc_value(3, (uint64_t(1) << 63) | 42));
  CHECK(l.coll_id == 3 && l.docid == ((uint64_t(1) << 63) | 42), "loc round trip");
  // The doc-key collision regression (sf0.3 shadowed 81,066 Comment docs): vertex-label ids
  // and edge-type ids are independent 0-based spaces sharing the 'D' namespace, so an edge
  // doc's coll MUST be offset -- the same raw id must never produce the same doc key, for any
  // docid, and edge_doc_coll must stay injective across a realistic id range.
  CHECK(doc_key(2, 7) != doc_key(edge_doc_coll(2), 7),
        "vertex coll 2 and edge coll 2 must not share a doc key (Comment vs hasInterest)");
  for (int id = 0; id < 256; id++)
    CHECK(doc_key(id, 1) != doc_key(edge_doc_coll(id), 1), "colliding raw id " << id);
  CHECK(edge_doc_coll(0) > 255 && edge_doc_coll(5) - edge_doc_coll(0) == 5,
        "edge coll space is disjoint from any plausible label space and order-preserving");
  // ...and the CALL SITES actually use the offset: drive the real ingest path with a schema
  // whose vertex-label id COLLIDES with its edge-type id (the sf0.3 Comment/hasInterest shape)
  // through a recording sink, and require every emitted key to be unique. A revert of any
  // doc_key site back to the raw id fails here (the vertex doc and the edge doc land on one
  // key), where the pure edge_doc_coll checks above would stay green.
  struct RecordingSink : KvSink {
    std::map<std::string, int> seen;
    std::map<std::string, std::string> kv;
    void begin_run(const std::string&) override {}
    void end_run() override {}
    void put(std::string_view k, std::string_view v) override { seen[std::string(k)]++; kv[std::string(k)] = std::string(v); }
    void put_index(int, std::string k) override { kv[k] = ""; seen[std::move(k)]++; }
  };
  json sj = json::parse(R"({
    "vertices": [
      {"label": "A", "id": 2, "tag": 0,
       "props": [{"name": "p", "type": "int", "multi": false, "nullable": true}]},
      {"label": "B", "id": 3, "tag": 1, "props": []}],
    "edges": [{"etype": "E", "id": 2, "reverse": true, "endpoints": [["A", "B"]], "props": []}],
    "indexes": [],
    "vid_tags": {"A": 0, "B": 1}
  })");
  Schema ts = Schema::from_json(sj);
  Arango fmt(ts, {});
  RecordingSink rec;
  fmt.add_vertex(rec, 1, "A", Value::row({{"p", Value(int64_t(7))}}));
  fmt.add_vertex(rec, 2, "B", Value::row({}));
  fmt.add_edge(rec, "E", 1, 2, Value::row({}), "A", "B");
  fmt.finalize(rec);
  int dups = 0;
  for (auto& [k, n] : rec.seen) dups += (n > 1);
  CHECK(dups == 0, dups << " duplicate ingest key(s) -- a doc_key site lost its edge_doc_coll");
  // every document has its pk entry, edges too: 2 vertex docs + 2 pk + the edge doc + its pk + f + t
  CHECK(rec.seen.size() == 8, "ingest keys: " << rec.seen.size() << " (want 8: 2 vertex docs + 2 pk + edge doc + edge pk + f + t)");
  CHECK(rec.seen.count(pk_key(edge_doc_coll(2), 1)) == 1, "the edge document's pk entry (edge collection, eid 1)");
  // regression, the ENGINE TWIN's map (the fair format has none): warm() rebuilds vid -> collection from the 'P' family,
  // which now also holds edge documents' entries; the edge (eid 1) must not be read as a vertex -- vid 1 is still A,
  // 2 still B (before the skip, warm threw)
  {
    char tmpl[] = "/tmp/test_arango_warm_XXXXXX";
    std::string dir = kvt::temp_dir(tmpl);
    {
      StoreOptions so;
      auto store = Store::open_read_write(dir + "/db", so, true);
      for (auto& [k, v] : rec.kv) store->db()->Put(rocksdb::WriteOptions(), k, v);
      Config named;
      named.props_codec = PropsCodec::named;
      ArangoEngine fresh(ts, named);
      try {
        fresh.warm(*store);
        CHECK(fresh.coll_of(1) == "A" && fresh.coll_of(2) == "B", "warm: vid -> collection after edges' pk entries");
      } catch (const std::exception& e) {
        CHECK(false, "warm threw on a store with an edge pk entry: " << e.what());
      }
    }
    std::filesystem::remove_all(dir);
  }
  if (fails) { std::cerr << fails << " failures\n"; return 1; }
  std::cout << "test_arango ok\n";
  return 0;
}

}  // namespace
