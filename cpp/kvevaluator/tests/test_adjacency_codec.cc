// The adjacency posting's facet codec (codec/posting.h AdjCodec): named (a prim dict per entry, the
// default -- byte-identical to before the codec existed, pinned by a golden) vs schema (a RowV2 row per
// entry over the etype's declared columns, the Split edge record's exact bytes; none for an etype with no
// columns). Covers round-trips, the used-bytes widths, a split unit's RMW writes, the wrong-codec throw, and that
// meta-v4 refuses the value.edge_props key (meta's own list encoding is tested in test_meta_codecs).
#include <cstdlib>
#include <iostream>
#include <map>
#include <sstream>
#include <string>
#include <unistd.h>
#include <vector>

#include "cli/session.h"
#include "codec/posting.h"
#include "codec/rowv2.h"
#include "formats/meta/config.h"
#include "meta_corners.h"
#include "ingest/ingest.h"
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
using evalcpp::test::meta_corner;

using Postings = std::vector<std::pair<uint64_t, Value>>;

static std::string hex(const std::string& s) {
  static const char* d = "0123456789abcdef";
  std::string o;
  for (unsigned char c : s) { o += d[c >> 4]; o += d[c & 15]; }
  return o;
}
static bool same(const Value& a, const Value& b) { return to_json(a) == to_json(b); }
template <class F>
static bool throws(F f) {
  try { f(); } catch (const std::exception&) { return true; }
  return false;
}

static const std::vector<ColDecl> kDate = {{"creationDate", "date", false}};
static const std::vector<ColDecl> kTwo = {{"creationDate", "date", false}, {"note", "str", false}};
static const std::vector<ColDecl> kNone = {};

// named: exactly the pre-codec bytes (golden captured from the code before AdjCodec existed)
static void check_named_golden() {
  Postings p = {{0x0100000000000007ull, make_row({{"creationDate", Value(int64_t(1262304000000))}})},
                {0x0100000000000009ull, make_row({{"creationDate", Value(int64_t(5))}})},
                {0x01000000000001f4ull, Value::row({})}};
  const std::string golden =
      "41030100000000000007000000030402eb01000064010c6372656174696f6e446174656900000125e72e780064010c63726561"
      "74696f6e446174656900000000000000056400";
  CHECK(hex(posting::encode_adjacency(p, true, posting::AdjCodec::named())) == golden, "named bytes changed: " << hex(posting::encode_adjacency(p, true, posting::AdjCodec::named())));
  CHECK(hex(posting::encode_adjacency(p, true, posting::AdjCodec{})) == golden, "explicit named codec differs from default");
  std::vector<uint8_t> uw;
  std::vector<uint32_t> fw;
  Postings back = posting::decode_adjacency_w(posting::encode_adjacency(p, true, posting::AdjCodec::named()), &uw, &fw, posting::AdjCodec::named());
  CHECK(back.size() == 3 && same(back[0].second, p[0].second) && same(back[2].second, p[2].second), "named round-trip");
  // named facet width = the whole prim dict: 'd' 1 + count 1 + len 1 + name 12 + 'i' 1 + 8 = 24; empty dict = 2
  CHECK(fw.size() == 3 && fw[0] == 24 && fw[1] == 24 && fw[2] == 2, "named widths 24/24/2");
  CHECK(posting::empty_adjacency(posting::AdjCodec::named()) == std::string("A\0", 2), "named empty posting = 'A' + 0");
}

// schema: RowV2 rows, same facets back, widths = the present cells' bytes, entries unframed
static void check_schema_roundtrip() {
  posting::AdjCodec sc = posting::AdjCodec::schema(&kTwo, 0);
  Postings p = {{0x0100000000000007ull, make_row({{"creationDate", Value(int64_t(1262304000000))}, {"note", Value(std::string("hi"))}})},
                {0x0100000000000009ull, make_row({{"creationDate", Value(int64_t(5))}})},   // note absent
                {0x01000000000001f4ull, Value::row({})}};                                  // both absent
  std::string enc = posting::encode_adjacency(p, true, sc);
  CHECK(!enc.empty() && enc[0] == 'R', "schema ascending head 'R'");
  std::vector<uint8_t> uw;
  std::vector<uint32_t> fw;
  Postings back = posting::decode_adjacency_w(enc, &uw, &fw, sc);
  CHECK(back.size() == 3, "schema round-trip size " << back.size());
  for (size_t i = 0; i < back.size() && i < p.size(); i++) {
    CHECK(back[i].first == p[i].first, "schema uid " << i);
    CHECK(same(back[i].second, p[i].second), "schema facets " << i << ": " << to_json(back[i].second).dump());
  }
  // widths: int 8 + str slot 8 + 2 = 18; int 8; nothing present = 0
  CHECK(fw.size() == 3 && fw[0] == 18 && fw[1] == 8 && fw[2] == 0, "schema widths 18/8/0");
  // the entry bytes ARE the Split record's: rowv2_encode of the same row over the same columns
  std::string rows;
  for (auto& [u, f] : p) {
    std::vector<PropRow> r = {{0, &f}};
    ValueRows rs(kTwo, r);
    rows += rowv2_encode(kTwo, rs, 0, 0);
  }
  CHECK(enc.size() >= rows.size() && enc.substr(enc.size() - rows.size()) == rows,
        "schema entries = concatenated Split rows, no framing");
  // row sizes: header 1 + bitmap 1 + slots + var; 1+1+16+2 = 20, 1+1+8 = 10, 1+1 = 2
  CHECK(rows.size() == 20 + 10 + 2, "row bytes 20+10+2: " << rows.size());
  // two var columns (str + multi), one empty string: rowv2_size must span the whole var area
  {
    std::vector<ColDecl> vv = {{"a", "str", false}, {"b", "str", true}, {"c", "str", false}};
    Value r = make_row({{"a", Value(std::string(""))}, {"b", Value::list({Value(std::string("xy")), Value(std::string("z"))})},
                        {"c", Value(std::string("tail"))}});
    posting::AdjCodec vc = posting::AdjCodec::schema(&vv, 2);
    Postings pv = {{1, r}, {2, r}};
    Postings bv = posting::decode_adjacency(posting::encode_adjacency(pv, true, vc), vc);
    CHECK(bv.size() == 2 && same(bv[1].second, r), "two var columns + timestamp round-trip");
  }
  // the timestamp: 3 trailing bytes per row, still decodable
  posting::AdjCodec ts = posting::AdjCodec::schema(&kTwo, 3);
  std::string enc3 = posting::encode_adjacency(p, true, ts);
  CHECK(enc3.size() == enc.size() + 9, "row_timestamp 3 adds 3 B per row");
  CHECK(posting::decode_adjacency(enc3, ts).size() == 3 && same(posting::decode_adjacency(enc3, ts)[0].second, p[0].second),
        "timestamped rows round-trip");
  // caller order: head 'Q'
  Postings rev = {p[2], p[0]};
  std::string q = posting::encode_adjacency(rev, false, sc);
  CHECK(q[0] == 'Q' && posting::decode_adjacency(q, sc).size() == 2 && posting::decode_adjacency(q, sc)[0].first == p[2].first,
        "schema caller-order 'Q' round-trip");
  // one date column (the LDBC shape: knows/likes/...): 10 B per entry vs named 24 -> 14 B of name per edge
  posting::AdjCodec d1 = posting::AdjCodec::schema(&kDate, 0);
  Postings kn = {{0x0100000000000007ull, make_row({{"creationDate", Value(int64_t(7))}})}};
  CHECK(posting::encode_adjacency(kn, true, posting::AdjCodec::named()).size() - posting::encode_adjacency(kn, true, d1).size() == 14,
        "a creationDate entry: named - schema = 14 B");
}

// an etype with no declared columns: no per-entry bytes at all (named pays an empty dict, 2 B each)
static void check_propless() {
  posting::AdjCodec sc = posting::AdjCodec::schema(&kNone, 0);
  Postings p = {{0x0100000000000007ull, Value::row({})}, {0x0100000000000009ull, Value::row({})},
                {0x01000000000001f4ull, Value::row({})}};
  std::string named = posting::encode_adjacency(p, true, posting::AdjCodec::named()), schema = posting::encode_adjacency(p, true, sc);
  CHECK(named.size() - schema.size() == 2 * 3, "prop-less: schema = named - 2 B per entry: " << named.size() << " vs " << schema.size());
  CHECK(schema.substr(1) == named.substr(1, schema.size() - 1), "prop-less schema = head + n + UidPack only");
  std::vector<uint8_t> uw;
  std::vector<uint32_t> fw;
  Postings back = posting::decode_adjacency_w(schema, &uw, &fw, sc);
  CHECK(back.size() == 3 && fw == std::vector<uint32_t>({0, 0, 0}) && back[0].second.is_row() && back[0].second.as_row().empty(),
        "prop-less decode: empty rows, 0 widths");
  std::vector<uint64_t> d = {1, 2};
  CHECK(throws([&] { posting::encode_adjacency_bytes(d, {"x", ""}, true, sc); }), "prop-less: a non-empty entry throws");
  posting::AdjCodec d1 = posting::AdjCodec::schema(&kDate, 0);
  CHECK(throws([&] { posting::encode_adjacency_bytes(d, {"", ""}, true, d1); }), "declared columns: an empty entry throws");
  CHECK(posting::empty_adjacency(sc) == std::string("R\0", 2), "schema empty posting = 'R' + 0");
}

// decoding with the other codec throws (distinct head bytes), both ways
static void check_wrong_codec() {
  posting::AdjCodec sc = posting::AdjCodec::schema(&kDate, 0);
  Postings p = {{5, make_row({{"creationDate", Value(int64_t(1))}})}};
  std::string named = posting::encode_adjacency(p, true, posting::AdjCodec::named()), schema = posting::encode_adjacency(p, true, sc);
  CHECK(throws([&] { posting::decode_adjacency(schema, posting::AdjCodec::named()); }), "named decode of a schema posting throws");
  CHECK(throws([&] { posting::decode_adjacency(named, sc); }), "schema decode of a named posting throws");
  CHECK(throws([&] { posting::decode_adjacency(posting::encode_adjacency(p, false, sc), posting::AdjCodec::named()); }), "named decode of 'Q' throws");
  CHECK(throws([&] { posting::decode_adjacency(posting::encode_adjacency(p, false, posting::AdjCodec::named()), sc); }), "schema decode of 'S' throws");
  posting::AdjCodec none;
  none.mode = posting::AdjCodec::Mode::schema;   // schema without columns: a caller bug
  CHECK(throws([&] { posting::encode_adjacency(p, true, none); }), "schema codec without cols throws");
}

// a split unit written and rewritten with the schema codec: add lands in its part, used bytes =
// uid width + cell bytes, remove empties to 'R' + 0, tomb every part, set rewrites one entry
static void check_split_unit() {
  char tmpl[] = "/tmp/evalcpp-adjcodec-XXXXXX";
  std::string dir = kvt::temp_dir(tmpl);
  StoreOptions so;
  auto store = Store::open_read_write(dir + "/db", so, true);
  ShadowOverlay ov;
  CountingStore st(*store, &ov);
  posting::AdjCodec sc = posting::AdjCodec::schema(&kDate, 0);
  auto facet = [](int64_t d) { return make_row({{"creationDate", Value(d)}}); };
  kvt::MemSink sink;
  const std::string key = "unit-key-0001";
  Postings six;
  for (uint64_t i = 10; i < 16; i++) six.emplace_back(i, facet(int64_t(i)));
  // HAND-COMPUTED: one entry = 'R' 1 + uvarint(1) 1 + UidPack(1 uid) 12 + row 10 = 24 B; two entries =
  // 1 + 1 + 17 + 20 = 39 B > 30 -> a 30 B target gives 6 one-entry parts
  unit::write(sink, key, six.size(), [&](size_t lo, size_t hi) {
    return posting::encode_adjacency(Postings(six.begin() + lo, six.begin() + hi), true, sc);
  }, 30, [&](size_t e) { return six[e].first; });
  size_t parts = 0;
  for (auto& [k, v] : sink.kv) {
    store->db()->Put(rocksdb::WriteOptions(), k, v);
    parts += unit::is_part_key(k, key.size());
    CHECK(v.size() == 24 && v[0] == 'R', "a one-entry schema part is 24 B: " << v.size());
  }
  CHECK(parts == 6, "six one-entry parts: " << parts);
  // the parts are keyed by their range's start: 0 for the first, else the first uid (10..15 -> 0, 11, ..., 15)
  CHECK(sink.kv.count(unit::part_key(key, 0)) && sink.kv.count(unit::part_key(key, 11)) &&
        sink.kv.count(unit::part_key(key, 15)) && !sink.kv.count(unit::part_key(key, 10)), "part keys = range starts");
  unit::Puts puts;
  // add 114 -> the last part (first uid 15): delta 99 -> 1 B code + 8 B cell = 9 used value B
  st.reset();
  unit::adjacency_add(st, key, 114, facet(114), puts, sc);
  CHECK(puts.size() == 1 && puts[0].first == unit::part_key(key, 15), "add rewrites exactly the last part");
  CHECK(st.counts().S == 2 && st.counts().N == 0 && st.counts().G == 0,
        "the add's read is a look at the unit key + ONE reverse seek, whatever the part count: S " << st.counts().S
        << " N " << st.counts().N);
  CHECK(st.counts().used_write_value == 1 + 8, "schema add used_write = uid 1 + cell 8: " << st.counts().used_write_value);
  Postings got = puts.empty() ? Postings{} : posting::decode_adjacency(puts[0].second, sc);
  CHECK(got.size() == 2 && got[1].first == 114 && same(got[1].second, facet(114)), "the part holds 15 and 114");
  CHECK(throws([&] { unit::Puts p2; unit::adjacency_add(st, key, 114, facet(1), p2, posting::AdjCodec::named()); }),
        "a named-codec RMW of a schema unit throws");
  for (auto& p : puts) st.put(p.first, p.second);
  puts.clear();
  // set: 12's date -> 99; used_write = the entry's cell bytes (8)
  st.reset();
  CHECK(unit::adjacency_set(st, key, 12, facet(99), puts, sc), "set finds 12");
  CHECK(puts.size() == 1 && puts[0].first == unit::part_key(key, 12) && st.counts().used_write_value == 8,
        "set rewrites the part starting at 12, used 8: " << st.counts().used_write_value);
  CHECK(st.counts().S == 2 && st.counts().N == 0, "the set's read is a look at the unit key + one reverse seek: S "
        << st.counts().S << " N " << st.counts().N);
  CHECK(!puts.empty() && same(posting::decode_adjacency(puts[0].second, sc)[0].second, facet(99)), "set merged the new date");
  for (auto& p : puts) st.put(p.first, p.second);
  puts.clear();
  // a two-column unit: setting ONE column charges only that cell (8), not the whole entry (8 + 8 + 2)
  {
    posting::AdjCodec two = posting::AdjCodec::schema(&kTwo, 0);
    const std::string k2 = "unit-key-0002";
    Postings one = {{7, make_row({{"creationDate", Value(int64_t(1))}, {"note", Value(std::string("hi"))}})}};
    store->db()->Put(rocksdb::WriteOptions(), k2, posting::encode_adjacency(one, true, two));
    unit::Puts p2;
    st.reset();
    CHECK(unit::adjacency_set(st, k2, 7, facet(2), p2, two), "two-column set finds 7");
    CHECK(st.counts().used_write_value == 8, "two-column set charges the changed cell only: " << st.counts().used_write_value);
    Postings back = p2.empty() ? Postings{} : posting::decode_adjacency(p2[0].second, two);
    CHECK(back.size() == 1 && same(back[0].second, make_row({{"creationDate", Value(int64_t(2))}, {"note", Value(std::string("hi"))}})),
          "the untouched column survives the set");
  }
  // remove 12: its part becomes the empty schema posting 'R' + 0
  st.reset();
  CHECK(unit::adjacency_remove(st, key, 12, puts, sc), "remove finds 12");
  CHECK(puts.size() == 1 && puts[0].second == std::string("R\0", 2), "removed-empty part = 'R' + 0");
  for (auto& p : puts) st.put(p.first, p.second);
  puts.clear();
  // the landing: 13 -> its part; 12 -> the emptied part (its range stays its own); an id below every
  // entry -> the first part (start 0); an absent unit -> nullopt, one miss; a whole unit -> its bare key
  auto at = unit::locate_unit(st, key, 13);
  CHECK(at && at->key == unit::part_key(key, 13), "locate 13 -> the part starting at 13");
  at = unit::locate_unit(st, key, 12);
  CHECK(at && at->key == unit::part_key(key, 12) && posting::decode_adjacency(at->raw, sc).empty(),
        "locate 12 -> the emptied part starting at 12");
  at = unit::locate_unit(st, key, 3);
  CHECK(at && at->key == unit::part_key(key, 0), "locate 3 (below every entry) -> the first part");
  at = unit::locate_unit(st, key, UINT64_MAX);
  CHECK(at && at->key == unit::part_key(key, 15), "locate max -> the last part");
  st.reset();
  CHECK(!unit::locate_unit(st, "unit-key-0000", 13), "an absent unit (a key sorting before ours) -> nullopt");
  CHECK(st.counts().S == 1 && st.counts().S_miss == 1, "the absent landing is one seek and a miss: S " << st.counts().S << " S_miss " << st.counts().S_miss);
  at = unit::locate_unit(st, "unit-key-0002", 1);
  CHECK(at && at->key == "unit-key-0002", "a whole unit -> its bare key");
  // a sibling unit whose key extends ours ("unit-key-0001x" sorts after every part of "unit-key-0001": 'x' > 0x00) is
  // never landed on: our parts stay found, and the sibling's own landing finds the sibling
  store->db()->Put(rocksdb::WriteOptions(), key + "x", posting::encode_adjacency(Postings{{7, facet(7)}}, true, sc));
  at = unit::locate_unit(st, key, UINT64_MAX);
  CHECK(at && at->key == unit::part_key(key, 15), "a sibling sorting after the parts: max still lands on our last part");
  at = unit::locate_unit(st, key + "x", 7);
  CHECK(at && at->key == key + "x", "the sibling lands on its own whole key");
  CHECK(!unit::locate_unit(st, "unit-key-000", 3), "a key that is a prefix of ours (\"unit-key-000\"): absent, not our part");
  // a part boundary between equal ids would put one id in two ranges: refused at ingest
  {
    kvt::MemSink s2;
    std::vector<uint64_t> ids = {5, 5, 6};
    CHECK(throws([&] {
      unit::write(s2, "dup-key", ids.size(), [&](size_t lo, size_t hi) {
        return posting::encode_adjacency(Postings(six.begin() + lo, six.begin() + hi), true, sc);
      }, 30, [&](size_t e) { return ids[e]; });
    }), "a boundary between equal ids throws");
    CHECK(unit::part_key_at("k", 0, [&](size_t e) { return ids[e]; }) == unit::part_key("k", 0) &&
          unit::part_key_at("k", 2, [&](size_t e) { return ids[e]; }) == unit::part_key("k", 6), "part_key_at: start 0, else the id");
  }
  // tomb empties every part to 'R' + 0
  st.reset();
  unit::adjacency_tomb(st, key, puts, sc);
  bool all_r = puts.size() == parts;
  for (auto& p : puts) all_r = all_r && p.second == std::string("R\0", 2) && posting::decode_adjacency(p.second, sc).empty();
  CHECK(all_r, "tomb: every part 'R' + 0 and reads back empty under the schema codec (" << puts.size() << " puts)");
}

// meta-v4 has no list codec setting: its Condensed lists are codec/lists.h (ids + the entries' columns), tested in
// test_meta_codecs. The v3 spec field `value` (edge_props, row_timestamp) must be refused in any form.
static void check_config() {
  auto accepted = [](const json& j) {
    try { meta::Config::from_json(j).validate(); return true; } catch (const std::exception&) { return false; }
  };
  CHECK(accepted(meta_corner("dgraph")) && accepted(meta_corner("dgneb")), "the Condensed-list corners are accepted");
  for (json v : {json{{"edge_props", "schema"}}, json{{"edge_props", "named"}}, json{{"row_timestamp", 0}}, json::object()})
    CHECK(!accepted(meta_corner("dgraph", {{"value", v}})), "a spec stating value " << v.dump() << " is refused (meta-v4)");
  json d = meta::Config::from_json(meta_corner("dgraph")).to_json();
  CHECK(!d.contains("value"), "to_json emits no value field: " << d.dump());
}

// a store ingested from records, and a run on it (check_arango_e2e)
static std::string ingest_store(const Schema& s, const std::string& schema_path, const std::string& records, json cfg,
                                const std::string& format = "meta") {
  std::string t = "/tmp/evalcpp-adjcodec-meta-XXXXXX";
  std::vector<char> b(t.begin(), t.end()); b.push_back('\0');
  std::string dir = kvt::temp_dir(b.data());
  IngestOptions o; o.format = format; o.codec_config = cfg; o.targets = {{dir + "/store", "none"}};
  std::istringstream in(records); ingest(in, s, schema_path, o);
  return dir;
}
static json run(const std::string& dir, const std::string& schema_path, json steps, const std::string& format = "meta") {
  Session sess;
  return sess.handle({{"store", {{"dir", dir + "/store"}, {"schema", schema_path}, {"format", format}}},
                      {"plan", {{"plan", "t"}, {"params", json::object()}, {"steps", steps}, {"output", "out"}}},
                      {"run", json::object()}});
}
static void check_arango_e2e(const Schema& s, const std::string& schema_path) {
  auto person = [&](int i) { return hex16((uint64_t(s.tag_of_label("Person")) << kVidShift) | uint64_t(i)); };
  std::string r;
  for (int i = 1; i <= 4; i++)
    r += "[\"v\",\"" + person(i) + "\",\"Person\",{\"id\":" + std::to_string(i) + ",\"firstName\":\"Bo\"}]\n";
  for (auto& [a, b] : std::vector<std::pair<int, int>>{{1, 2}, {1, 3}, {2, 4}})
    r += "[\"e\",\"knows\",\"" + person(a) + "\",\"" + person(b) + "\",{\"creationDate\":" + std::to_string(1262304000000LL + b) +
         "},\"Person\",\"Person\"]\n";
  std::string dn = ingest_store(s, schema_path, r, {{"props_codec", "named"}}, "arangodb");
  std::string ds = ingest_store(s, schema_path, r, {{"props_codec", "schema"}}, "arangodb");
  json step = {{"name", "out"}, {"op", "read_neighbors"}, {"srcs", json::array({person(1), person(2)})}, {"etype", "knows"},
               {"dir", "out"}, {"pairs", true}, {"edge_props", json::array({"creationDate"})}};
  json rn = run(dn, schema_path, json::array({step}), "arangodb"), rs = run(ds, schema_path, json::array({step}), "arangodb");
  CHECK(rn["rows"] == rs["rows"] && rn["rows"].size() == 3, "arango: rows differ or not 3: " << rn["rows"].size() << " / " << rs["rows"].size());
  for (const char* c : {"S", "G", "N", "P", "D"})
    CHECK(rn["steps"][0][c] == rs["steps"][0][c], "arango " << c << " differs");
  int64_t gn = rn["steps"][0]["bytes"]["get_value"].get<int64_t>(), gs = rs["steps"][0]["bytes"]["get_value"].get<int64_t>();
  CHECK(gn - gs == 11 * 3, "arango: edge-doc GET bytes named " << gn << " - schema " << gs << " != 11 x 3");
  std::cerr << "[arango e2e] rows=" << rs["rows"].size() << " edge-doc GET bytes named " << gn << " schema " << gs << "\n";
  // vertex documents follow the same knob: the same rows and S/G/N for a vertex-prop read under both codecs
  json vstep = {{"name", "out"}, {"op", "read_vertex_props"}, {"vids", json::array({person(1), person(3)})},
                {"label", "Person"}, {"props", json::array({"firstName", "id"})}};
  json vn = run(dn, schema_path, json::array({vstep}), "arangodb"), vs = run(ds, schema_path, json::array({vstep}), "arangodb");
  CHECK(vn["rows"] == vs["rows"] && vn["rows"].size() == 2, "arango vertex: rows differ or not 2");
  for (const char* c : {"S", "G", "N", "P", "D"})
    CHECK(vn["steps"][0][c] == vs["steps"][0][c], "arango vertex " << c << " differs");
  std::cerr << "[arango e2e] vertex GET bytes named " << vn["steps"][0]["bytes"]["get_value"] << " schema "
            << vs["steps"][0]["bytes"]["get_value"] << "\n";
}

TEST_CASE(test_adjacency_codec) {
  std::string schema_path = ldbc_schema_dir() + "/schema.json";
  Schema s = Schema::load(schema_path);
  check_arango_e2e(s, schema_path);
  check_named_golden();
  check_schema_roundtrip();
  check_propless();
  check_wrong_codec();
  check_split_unit();
  check_config();
  if (fails) { std::cerr << fails << " failures\n"; return 1; }
  std::cout << "test_adjacency_codec ok\n";
  return 0;
}

}  // namespace
