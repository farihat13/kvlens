// meta-v4's two encodings (codec/columns.h, codec/lists.h) and the list unit's writes (store/unit_list.h):
// round trips (n = 1, n > 1, absent cells, str, multi, 0 columns), the payload / framing split on a hand-computed
// example, the per-entry used-bytes widths, and a split list's RMW (add / set / remove / tomb rewrite ONE part).
#include <cstdlib>
#include <iostream>
#include <map>
#include <string>
#include <unistd.h>
#include <vector>

#include "codec/chunks.h"
#include "codec/columns.h"
#include "codec/decode_counts.h"
#include "codec/lists.h"
#include "exec/chunk_cache.h"
#include "exec/chunk_write.h"
#include "formats/common.h"
#include "formats/rows.h"
#include "store/counting.h"
#include "store/rocksdb_store.h"
#include "store/unit_list.h"
#include "store/unit_value.h"
#include "value/value.h"
#include "harness.h"

namespace {

using namespace evalcpp;

static bool same(const Value& a, const Value& b) { return to_json(a) == to_json(b); }

static const std::vector<ColDecl> kPerson = {{"firstName", "str", false}, {"birthday", "date", false},
                                             {"email", "str", true}};
static const std::vector<ColDecl> kDate = {{"creationDate", "date", false}};
static const std::vector<ColDecl> kNone = {};

static std::string encode_rows(const std::vector<ColDecl>& cols, const std::vector<Value>& rows) {
  std::vector<PropRow> pr;
  for (size_t i = 0; i < rows.size(); i++) pr.push_back({i, &rows[i]});
  ValueRows vr(cols, pr);
  std::string out;
  columns::encode(out, cols, vr);
  return out;
}

static void check_columns() {
  // one row, every kind of cell, one absent: HAND-COMPUTED bytes (one bitmap over cols x n = 3 bits: 1 B)
  //   bitmap        : 1 B (bit 2 set: email absent)                         = 1
  //   firstName "Bo": offset 4 B + "Bo" 2 B                                 = 6
  //   birthday 99   : 8 B                                                   = 8
  //   email absent  : no cells                                              = 0      -> 15 B
  Value r1 = make_row({{"firstName", Value(std::string("Bo"))}, {"birthday", Value(int64_t(99))}});
  std::string b1 = encode_rows(kPerson, {r1});
  CHECK(b1.size() == 15, "one row = 15 B: " << b1.size());
  columns::View v1(&kPerson, b1, 0, 1);
  CHECK(v1.end() == b1.size() && same(v1.row(0), r1), "one row round-trips: " << to_json(v1.row(0)).dump());
  CHECK(!v1.present(2, 0) && v1.cell(2, 0).is_null(), "the absent cell reads null");
  CHECK(v1.payload_total() == 2 + 8 && v1.framing_total() == 5, "payload 10 B (\"Bo\" + 8), framing 5 B (1 bitmap + 1 offset): "
        << v1.payload_total() << " / " << v1.framing_total());
  CHECK(v1.cell_bytes(0, 0) == 6 && v1.cell_bytes(1, 0) == 8 && v1.cell_bytes(2, 0) == 0, "framed cell widths 6 / 8 / 0");
  // three rows, a multi cell: n = 3 -> bitmaps 1 B each; the multi blob = uvarint count + (uvarint len + bytes)...
  Value r2 = make_row({{"email", Value::list({Value(std::string("a@x")), Value(std::string("bb@y"))})}});
  Value r3 = make_row({{"firstName", Value(std::string("Al"))}, {"birthday", Value(int64_t(-5))}});
  std::string b3 = encode_rows(kPerson, {r1, r2, r3});
  columns::View v3(&kPerson, b3, 0, 3);
  CHECK(v3.end() == b3.size(), "the block is self-delimiting");
  CHECK(same(v3.row(0), r1) && same(v3.row(1), r2) && same(v3.row(2), r3), "three rows round-trip");
  CHECK(v3.payload_bytes(2, 1) == 1 + (1 + 3) + (1 + 4), "the multi payload = its blob, 10 B: " << v3.payload_bytes(2, 1));
  CHECK(v3.payload_total() == 2 + 2 + 8 + 8 + 10, "payload over the block = the cells' payloads: " << v3.payload_total());
  // the same cell's payload is the same as a one-cell (Split vertex) record's value
  CHECK(columns::encode_cell(kPerson[2], r2.get("email")).size() == v3.payload_bytes(2, 1), "a Split record = the payload");
  CHECK(same(columns::decode_cell(kPerson[2], columns::encode_cell(kPerson[2], r2.get("email"))), r2.get("email")),
        "a multi Split record round-trips");
  CHECK(same(columns::decode_cell(kPerson[1], columns::encode_cell(kPerson[1], Value(int64_t(-5)))), Value(int64_t(-5))),
        "an int Split record round-trips (negative)");
  // 0 columns: no bytes at all (a propless etype's per-edge record is an empty value)
  CHECK(encode_rows(kNone, {make_row({})}).empty(), "0 columns encode to 0 bytes");
  // View::reset: one view reused over blocks of other row counts and column sets reads each as a fresh View does, and
  // counts the same decoded bytes per parse
  std::string bd = encode_rows(kDate, {make_row({{"creationDate", Value(int64_t(7))}}), make_row({})});
  struct Blk { const std::vector<ColDecl>* cols; const std::string* raw; size_t n; };
  std::vector<Blk> seq = {{&kPerson, &b3, 3}, {&kPerson, &b1, 1}, {&kDate, &bd, 2}, {&kPerson, &b3, 3}, {&kPerson, &b1, 1}};
  columns::View reused;
  size_t parsed = 0;
  for (const Blk& k : seq) {
    uint64_t b0 = decode_counts().bytes;
    reused.reset(k.cols, *k.raw, 0, k.n);
    uint64_t b_reused = decode_counts().bytes - b0;
    b0 = decode_counts().bytes;
    columns::View fresh(k.cols, *k.raw, 0, k.n);
    uint64_t b_fresh = decode_counts().bytes - b0;
    bool eq = reused.end() == fresh.end() && reused.rows() == fresh.rows() && reused.cols() == fresh.cols() &&
              reused.payload_total() == fresh.payload_total() && b_reused == b_fresh;
    for (size_t r = 0; r < k.n; r++) {
      eq = eq && same(reused.row(r), fresh.row(r));
      for (size_t c = 0; c < k.cols->size(); c++)
        eq = eq && reused.present(c, r) == fresh.present(c, r) && reused.cell_bytes(c, r) == fresh.cell_bytes(c, r) &&
             same(reused.cell(c, r), fresh.cell(c, r));
    }
    CHECK(eq, "a reused View reads block " << parsed << " (" << k.n << " rows, " << k.cols->size() << " cols) as a fresh one");
    parsed++;
  }
  std::cerr << "[columns] one row 17 B (payload 10, framing 7); three rows " << b3.size() << " B; 0 columns 0 B\n";
}

static void check_lists() {
  // ids 10, 12, 300 with one date column, the middle entry without it: HAND-COMPUTED
  //   uvarint n 1 + the first id from its group base 10 << 4 | tag 0 = 160 (2) + deltas 2 (1) + 288 (2) = 6;
  //   columns: bitmap 1 + 2 x 8 = 17  -> 23 B (a first id is coded from its vid group's base)
  lists::Entries e = {{10, make_row({{"creationDate", Value(int64_t(7))}}), 0}, {12, make_row({}), 0},
                      {300, make_row({{"creationDate", Value(int64_t(9))}}), 0}};
  std::string b = lists::encode(e, 0, kDate);
  CHECK(b.size() == 23, "a 3-entry list = 23 B: " << b.size());
  lists::View v(&kDate, b, 0);
  CHECK(v.size() == 3 && v.dsts() == std::vector<uint64_t>({10, 12, 300}), "ids round-trip");
  CHECK(v.id_bytes(0) == 2 && v.id_bytes(2) == 2, "each entry's own id width (used bytes)");
  CHECK(same(v.props_row(0), e[0].props) && same(v.props_row(1), make_row({})) && v.props_bytes(1) == 0 &&
            v.props_bytes(2) == 8, "the entries' props and widths");
  // no props: ids only; empty: 1 B
  std::string np = lists::encode(lists::Entries{{5, make_row({}), 0}, {6, make_row({}), 0}}, 0, kNone);
  CHECK(np.size() == 3 && !lists::View(&kNone, np, 0).has_props(), "a propless list = n + ids, 3 B: " << np.size());
  CHECK(lists::empty(kDate).size() == 1 && lists::View(&kDate, lists::empty(kDate), 0).size() == 0, "the empty list = 1 B");
  // a Condensed list holds the same props columns a one-owner chunk would: the payload is the cells' payloads
  CHECK(v.props()->payload_total() == 16, "the list's props payload = 2 dates: " << v.props()->payload_total());
  std::cerr << "[lists] 3 entries 22 B; propless 2 entries 3 B; empty 1 B\n";
}

static void check_list_unit() {
  char tmpl[] = "/tmp/evalcpp-metacodecs-XXXXXX";
  std::string dir = kvt::temp_dir(tmpl);
  StoreOptions so;
  auto store = Store::open_read_write(dir + "/db", so, true);
  ShadowOverlay ov;
  CountingStore st(*store, &ov);
  kvt::MemSink sink;
  const std::string key = "list-key-0001";
  lists::Entries six;
  for (uint64_t i = 10; i < 16; i++) six.push_back({i, make_row({{"creationDate", Value(int64_t(i))}}), 0});
  // HAND-COMPUTED: one entry = n 1 + id 1 + bitmap 1 + date 8 = 11 B; two entries = 1 + 2 + 1 + 16 = 20 B > 15 ->
  // a 15 B target gives 6 one-entry parts (12 B each)
  unit::write(sink, key, six.size(), [&](size_t lo, size_t hi) {
    return lists::encode(lists::Entries(six.begin() + lo, six.begin() + hi), 0, kDate);
  }, 15, [&](size_t e) { return six[e].id; });
  size_t parts = 0;
  for (auto& [k, v] : sink.kv) {
    store->db()->Put(rocksdb::WriteOptions(), k, v);
    parts += unit::is_part_key(k, key.size());
    CHECK(v.size() == 12, "a one-entry part is 12 B: " << v.size());   // the id from its group base: 2 B
  }
  CHECK(parts == 6, "six one-entry parts: " << parts);
  unit::ListPuts puts;
  // add 114 -> the last part (first id 15): delta 99 -> 1 B + the date cell 8 B = 9 used value B
  st.reset();
  unit::list_add(st, key, 114, 0, make_row({{"creationDate", Value(int64_t(114))}}), puts, kDate, 0);
  CHECK(puts.size() == 1 && puts[0].first == unit::part_key(key, 15), "add rewrites exactly the last part");
  CHECK(st.counts().used_write_value == 1 + 8, "add used_write = id 1 + cell 8: " << st.counts().used_write_value);
  CHECK(st.counts().S == 2 && st.counts().N == 0, "the RMW read = a look at the unit key + ONE reverse seek onto the part holding the id: S="
        << st.counts().S << " N=" << st.counts().N);
  lists::View added(&kDate, puts.empty() ? lists::empty(kDate) : puts[0].second, 0);
  CHECK(added.dsts() == std::vector<uint64_t>({15, 114}), "the last part now holds 15, 114");
  // set: only the changed cell is used; the part holding 12 is rewritten
  puts.clear();
  st.reset();
  CHECK(unit::list_set(st, key, 12, make_row({{"creationDate", Value(int64_t(777))}}), puts, kDate, 0), "set finds 12");
  CHECK(puts.size() == 1 && puts[0].first == unit::part_key(key, 12) && st.counts().used_write_value == 8,
        "set rewrites the part holding 12, used = the changed cell 8 B: " << st.counts().used_write_value);
  CHECK(same(lists::View(&kDate, puts[0].second, 0).props_row(0), make_row({{"creationDate", Value(int64_t(777))}})),
        "the new value is in the rewritten part");
  // remove: the emptied part is DELed, not kept as an empty list
  puts.clear();
  std::vector<std::string> dels;
  CHECK(unit::list_remove(st, key, 13, puts, dels, kDate, 0) && puts.empty() && dels.size() == 1 &&
        dels[0] == unit::part_key(key, 13), "remove empties the part holding 13 and DELs it");
  for (auto& d : dels) st.del_key(d);
  puts.clear();
  dels.clear();
  // its range joins the part below: a new 13 lands on the part starting at 12
  st.reset();
  unit::list_add(st, key, 13, 0, make_row({{"creationDate", Value(int64_t(13))}}), puts, kDate, 0);
  CHECK(puts.size() == 1 && puts[0].first == unit::part_key(key, 12) && st.counts().S == 2 && st.counts().N == 0,
        "after the DEL, 13 lands on the part starting at 12 (the look at the key + one reverse seek)");
  puts.clear();
  // the FIRST part, emptied, stays as an empty list (no DEL): every id keeps a part to land on
  CHECK(unit::list_remove(st, key, 10, puts, dels, kDate, 0) && dels.empty() && puts.size() == 1 &&
        puts[0].first == unit::part_key(key, 0) && lists::View(&kDate, puts[0].second, 0).size() == 0,
        "an emptied first part is rewritten empty, not DELed");
  for (auto& p : puts) st.put(p.first, p.second);
  puts.clear();
  st.reset();
  unit::list_add(st, key, 5, 0, make_row({{"creationDate", Value(int64_t(5))}}), puts, kDate, 0);
  CHECK(puts.size() == 1 && puts[0].first == unit::part_key(key, 0) && st.counts().S_miss == 0,
        "an id below every entry lands on the kept first part");
  puts.clear();
  // a whole list that empties is DELed
  {
    const std::string k1 = "list-key-0002";
    store->db()->Put(rocksdb::WriteOptions(), k1, lists::encode(lists::Entries(six.begin(), six.begin() + 1), 0, kDate));
    CHECK(unit::list_remove(st, k1, 10, puts, dels, kDate, 0) && puts.empty() && dels.size() == 1 && dels[0] == k1,
          "an emptied whole list is DELed");
    dels.clear();
  }
  CHECK(!unit::list_remove(st, key, 999, puts, dels, kDate, 0) && puts.empty() && dels.empty(),
        "removing an absent entry writes nothing");
  std::cerr << "[list unit] 6 parts; add / set / remove rewrite one part\n";
}

// the k-owner inline chunk (meta-v4's adjacency placement on Chunked), skip16 header (codec/lists.h): its bytes, hand-
// computed; with props a per-edge column and the columns block follow and round-trip; the shared chunk RMW (kind 3)
// adds / sets / removes an edge with its props
static void check_inline_chunk() {
  std::vector<lists::ChunkOwner> owners = {{100, {3, 7}, {}}, {105, {7}, {}}, {300, {1, 2, 9}, {}}};
  std::string a = lists::encode_chunk(owners, 0, kNone, nullptr);
  // HAND-COMPUTED (and by check_chunk_header_bytes.encode_d16): k 03 | first 100 << 4 = 1600: c0 0c | span 200: c8 01 |
  // w_b = bits(6) = 3: 03, ENDs 2, 3, 6 at 3 bits = 2 + 3 << 3 + 6 << 6 = 410: 9a 01 | no edge column (nothing per
  // edge) | G = 3: 03 | no sample (k - 1 < 16) | gaps 5, 195: 05 c3 01 | dsts 3 << 4, +4 | 7 << 4 | 1 << 4, +1, +7:
  // 30 04 70 10 01 07
  CHECK(a == std::string("\x03\xc0\x0c\xc8\x01\x03\x9a\x01\x03\x05\xc3\x01\x30\x04\x70\x10\x01\x07", 18),
        "the propless inline chunk's skip16 bytes (" << a.size() << " B)");
  std::vector<Value> rows;
  for (int64_t d = 1; d <= 6; d++) rows.push_back(d == 4 ? make_row({}) : make_row({{"creationDate", Value(d * 10)}}));
  std::vector<PropRow> pr;
  for (size_t i = 0; i < rows.size(); i++) pr.push_back({i, &rows[i]});
  ValueRows vr(kDate, pr);
  std::string c = lists::encode_chunk(owners, 0, kDate, &vr);
  // HAND-COMPUTED: the propless chunk + the edge column (degrees 2, 1, 3: w_e = bits(6) = 3, CUM 2, 3, 6 = 03 9a 01,
  // after the ENDs) + the columns block over 6 edges (bitmap 1 B + 5 present dates x 8 B = 41 B)
  CHECK(c.size() == a.size() + 3 + 41 && c.compare(0, 8, a, 0, 8) == 0 && c.compare(8, 3, "\x03\x9a\x01") == 0 &&
            c.compare(11, a.size() - 8, a, 8, a.size() - 8) == 0,
        "with props = the propless chunk + a 3 B edge column + a 41 B columns block: " << c.size() << " vs " << a.size());
  lists::ChunkView v(&kDate, c, 0);
  CHECK(v.size() == 3 && v.first() == 100 && v.last() == 300 && v.find(105) == 1 && v.find(106) == -1, "owners");
  CHECK(v.dsts(2) == std::vector<uint64_t>({1, 2, 9}), "owner 300's dsts");
  CHECK(same(v.props_row(0, 1), rows[1]) && same(v.props_row(2, 0), rows[3]) && same(v.props_row(2, 2), rows[5]),
        "each edge's props, owner order then dst order");
  // the RMW through the shared chunk_rmw, kind 3
  char tmpl[] = "/tmp/evalcpp-inlinechunk-XXXXXX";
  std::string dir = kvt::temp_dir(tmpl);
  StoreOptions so;
  auto store = Store::open_read_write(dir + "/db", so, true);
  const std::string prefix("e" "\x07" "a" "\x03" "\x00", 5);   // a packed-adjacency family prefix (keys.h padj_prefix)
  store->db()->Put(rocksdb::WriteOptions(), chunk_with_id(prefix, 100), c);   // keyed by its first owner, 100
  ShadowOverlay ov;
  CountingStore st(*store, &ov);
  ChunkCodecs cc;
  ChunkWriteReader rd(cc, st);
  ChunkWrites w;
  chunk_rmw(cc, rd, w, prefix, 65536, 105, 3, &kDate, false, [&](ChunkWriteReader::Chunk& ch) {
    chunk_adj_add_inline(ch.groups, 105, 8, make_row({{"creationDate", Value(int64_t(888))}}), true, std::nullopt);
  });
  CHECK(w.puts.size() == 1 && w.dels.empty(), "add rewrites the one chunk under its key");
  lists::ChunkView after(&kDate, w.puts.empty() ? c : w.puts[0].second, 0);
  CHECK(after.dsts(1) == std::vector<uint64_t>({7, 8}) &&
            same(after.props_row(1, 1), make_row({{"creationDate", Value(int64_t(888))}})), "the edge and its props added");
  bool set = false, gone = false;
  chunk_rmw(cc, rd, w, prefix, 65536, 300, 3, &kDate, false, [&](ChunkWriteReader::Chunk& ch) {
    set = chunk_adj_set_inline(ch.groups, 300, 2, make_row({{"creationDate", Value(int64_t(5))}}));
    gone = chunk_adj_remove_inline(ch.groups, 300, 9);
  });
  lists::ChunkView after2(&kDate, w.puts.back().second, 0);
  CHECK(set && gone && after2.dsts(2) == std::vector<uint64_t>({1, 2}) &&
            same(after2.props_row(2, 1), make_row({{"creationDate", Value(int64_t(5))}})), "set and remove, props follow");
  std::cerr << "[inline chunk] propless skip16 bytes (" << a.size() << " B); +3 B edge column +41 B with props; kind-3 RMW ok\n";
}

// own placement's adjacency chunk (edge_props "own"): the inline chunk with one ordinal per edge where the columns
// block is, and so with the edge column. For an edge type with no props it IS the inline chunk, byte for byte (one
// function, no second codec). A 17-owner chunk holds one skip sample (golden bytes).
static void check_own_chunk() {
  std::vector<lists::ChunkOwner> owners = {{100, {3, 7}, {}}, {105, {7}, {}}, {300, {1, 2, 9}, {}}};
  std::string a = lists::encode_chunk(owners, 0, kNone, nullptr);
  std::vector<uint64_t> ords = {0, 1, 200, 3, 4, 5};
  std::string o = lists::encode_chunk(owners, 0, kNone, nullptr, &ords);
  // HAND-COMPUTED: the propless chunk + the 3 B edge column after its ENDs (03 9a 01: CUM 2, 3, 6 at 3 bits) + one
  // uvarint per edge: five 1 B ordinals and 200 (2 B) = 7 B
  CHECK(o.size() == a.size() + 3 + 7 && o.compare(0, 8, a, 0, 8) == 0 && o.compare(8, 3, "\x03\x9a\x01") == 0 &&
            o.compare(11, a.size() - 8, a, 8, a.size() - 8) == 0,
        "own chunk = the propless chunk + a 3 B edge column + a 7 B ordinals block: " << o.size() << " vs " << a.size());
  // HAND-COMPUTED: the tail is 00 01 c8 01 03 04 05 (200 = 0xc8 0x01 as a uvarint)
  CHECK(o.substr(a.size() + 3) == std::string("\x00\x01\xc8\x01\x03\x04\x05", 7), "the ordinals block's bytes");
  // 17 owners 1000, 1010, ..., 1160, one dst o + 1 each, ordinals 0..16: constant degree (edge column 00 01) and one
  // skip sample for owner 16 = (160 in bits(160) = 8 bits, where owner 17's gap starts = G = 16 in bits(16) = 5 bits)
  // = 160 + 16 << 8 = 0x10a0: a0 10. HAND-COMPUTED field by field; the bytes also from encode_d16 (gate (a)'s encoder)
  std::vector<lists::ChunkOwner> seventeen;
  std::vector<uint64_t> ords17;
  for (uint64_t i = 0; i < 17; i++) { seventeen.push_back({1000 + 10 * i, {i + 1}, {}}); ords17.push_back(i); }
  std::string s17 = lists::encode_chunk(seventeen, 0, kNone, nullptr, &ords17);
  const std::string g17(
      "\x11\x80\x7d\xa0\x01\x05\x41\x0c\x52\xcc\x49\xab\xbd\x38\xeb\xcd\x1b\x00\x01\x10\xa0\x10"
      "\x0a\x0a\x0a\x0a\x0a\x0a\x0a\x0a\x0a\x0a\x0a\x0a\x0a\x0a\x0a\x0a"
      "\x10\x20\x30\x40\x50\x60\x70\x80\x01\x90\x01\xa0\x01\xb0\x01\xc0\x01\xd0\x01\xe0\x01\xf0\x01\x80\x02\x90\x02"
      "\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f\x10", 82);
  CHECK(s17 == g17, "the 17-owner own chunk's skip16 bytes (" << s17.size() << " B, want 82)");
  lists::ChunkView v17(&kNone, s17, 0, true);
  bool ok17 = v17.last() == 1160 && v17.find(1160) == 16 && v17.find(1155) == -1 && v17.src(16) == 1160;
  for (uint64_t i = 0; i < 17; i++) ok17 = ok17 && v17.dsts(i) == std::vector<uint64_t>{i + 1} && v17.ordinal(i, 0) == i;
  CHECK(ok17, "the 17-owner chunk reads back through its sample");
  lists::ChunkView v(&kNone, o, 0, true);
  CHECK(v.size() == 3 && v.dsts(2) == std::vector<uint64_t>({1, 2, 9}), "owners and dsts");
  CHECK(v.ordinal(0, 0) == 0 && v.ordinal(0, 1) == 1 && v.ordinal(1, 0) == 200 && v.ordinal(2, 2) == 5, "each edge's ordinal");
  // with the far-end label block (1 bit per edge): 6 codes = 1 B, between the neighbour area and the ordinals
  std::vector<lists::ChunkOwner> lab = {{100, {3, 7}, {1, 0}}, {105, {7}, {1}}, {300, {1, 2, 9}, {0, 1, 1}}};
  std::string ol = lists::encode_chunk(lab, 1, kNone, nullptr, &ords);
  // HAND-COMPUTED: codes 1,0,1,0,1,1 low bit first = 0b110101 = 0x35
  CHECK(ol.size() == o.size() + 1 && ol[a.size() + 3] == '\x35' && ol.substr(a.size() + 4) == o.substr(a.size() + 3),
        "label block (0x35) then the ordinals: " << ol.size() << " vs " << o.size());
  lists::ChunkView vl(&kNone, ol, 1, true);
  CHECK(vl.label(0, 0) == 1 && vl.label(0, 1) == 0 && vl.label(2, 0) == 0 && vl.label(2, 2) == 1 && vl.ordinal(1, 0) == 200,
        "labels and ordinals read back");
  bool threw = false;
  try { lists::encode_chunk(owners, 0, kDate, nullptr, &ords); } catch (const std::exception&) { threw = true; }
  CHECK(threw, "ordinals beside a columns block are refused");
  // the RMW through the shared chunk_rmw, kind 3 with ordinals
  char tmpl[] = "/tmp/evalcpp-ownchunk-XXXXXX";
  std::string dir = kvt::temp_dir(tmpl);
  StoreOptions so;
  auto store = Store::open_read_write(dir + "/db", so, true);
  const std::string prefix("e" "\x07" "a" "\x03" "\x00", 5);
  store->db()->Put(rocksdb::WriteOptions(), chunk_with_id(prefix, 100), ol);
  ShadowOverlay ov;
  CountingStore st(*store, &ov);
  ChunkCodecs cc;
  ChunkWriteReader rd(cc, st);
  ChunkWrites w;
  chunk_rmw(cc, rd, w, prefix, 65536, 105, 3, &kNone, true, [&](ChunkWriteReader::Chunk& ch) {
    chunk_adj_add(ch.groups, 105, 8, 9, uint8_t(0));
  }, 1);
  CHECK(w.puts.size() == 1 && w.dels.empty(), "add rewrites the one chunk under its key");
  lists::ChunkView after(&kNone, w.puts.empty() ? ol : w.puts[0].second, 1, true);
  CHECK(after.dsts(1) == std::vector<uint64_t>({7, 8}) && after.ordinal(1, 1) == 9 && after.label(1, 1) == 0 &&
            after.ordinal(1, 0) == 200 && after.label(1, 0) == 1 && after.ordinal(2, 0) == 3 && after.label(2, 0) == 0,
        "the edge added with its ordinal and label; the others keep theirs");
  std::optional<uint64_t> removed;
  chunk_rmw(cc, rd, w, prefix, 65536, 300, 3, &kNone, true, [&](ChunkWriteReader::Chunk& ch) {
    removed = chunk_adj_remove(ch.groups, 300, 2);
  }, 1);
  lists::ChunkView after2(&kNone, w.puts.back().second, 1, true);
  CHECK(removed && *removed == 4 && after2.dsts(2) == std::vector<uint64_t>({1, 9}) && after2.ordinal(2, 1) == 5 &&
            after2.label(2, 1) == 1, "remove returns the edge's ordinal; its label goes with it");
  std::cerr << "[own chunk] the propless chunk + ordinals (" << o.size() << " B), + the label block (" << ol.size()
            << " B); kind-3 RMW with ordinals ok\n";
}

// the far-end LABEL block (adjacency far_label): n codes of `bits` bits, low bit first; 0 bits = no byte, so a list
// with no label block is byte for byte the list before far_label; a list / chunk keeps each entry's label through
// every rewrite (add, set, remove)
static void check_labels() {
  // HAND-COMPUTED: codes 1, 0, 3, 2, 1 at 2 bits, low bit first: code 1 sets bit 0, code 3 bits 4-5, code 2 bit 7,
  // the last code 1 bit 8 -> byte 0 = 1 + 16 + 32 + 128 = 0xB1, byte 1 = 0x01 -> 2 B
  std::string blk;
  lists::put_labels(blk, {1, 0, 3, 2, 1}, 2);
  CHECK(blk == std::string("\xB1\x01", 2), "5 codes at 2 bits = 2 B 0xB1 0x01");
  size_t at = 0;
  CHECK(lists::get_labels(blk, at, 5, 2) == std::vector<uint8_t>({1, 0, 3, 2, 1}) && at == 2, "the codes read back");
  std::string none;
  lists::put_labels(none, {0, 0, 0}, 0);
  CHECK(none.empty(), "0 bits = no byte");
  // the 3-entry list of check_lists with a 1-bit block: 23 B + 1 B; the ids and props unchanged
  lists::Entries e = {{10, make_row({{"creationDate", Value(int64_t(7))}}), 1}, {12, make_row({}), 0},
                      {300, make_row({{"creationDate", Value(int64_t(9))}}), 1}};
  std::string b0 = lists::encode(e, 0, kDate), b1 = lists::encode(e, 1, kDate);
  CHECK(b0.size() == 23 && b1.size() == 24, "1-bit labels add 1 B to the 23 B list: " << b1.size());
  lists::View v(&kDate, b1, 1);
  CHECK(v.label(0) == 1 && v.label(1) == 0 && v.label(2) == 1 && v.dsts() == std::vector<uint64_t>({10, 12, 300}) &&
            same(v.props_row(2), e[2].props), "labels, ids and props read back");
  CHECK(lists::View(&kDate, b0, 0).label(1) == 0, "no block: every label reads 0");
  // the list unit keeps labels through add / set / remove
  char tmpl[] = "/tmp/evalcpp-labels-XXXXXX";
  std::string dir = kvt::temp_dir(tmpl);
  StoreOptions so;
  auto store = Store::open_read_write(dir + "/db", so, true);
  const std::string key = "label-list-01";
  store->db()->Put(rocksdb::WriteOptions(), key, b1);
  ShadowOverlay ov;
  CountingStore st(*store, &ov);
  unit::ListPuts puts;
  std::vector<std::string> dels;
  unit::list_add(st, key, 11, 1, make_row({}), puts, kDate, 1);
  lists::View added(&kDate, puts.at(0).second, 1);
  CHECK(added.dsts() == std::vector<uint64_t>({10, 11, 12, 300}) && added.label(0) == 1 && added.label(1) == 1 &&
            added.label(2) == 0 && added.label(3) == 1, "add inserts the new label at its position");
  puts.clear();
  CHECK(unit::list_remove(st, key, 10, puts, dels, kDate, 1), "remove finds 10");
  lists::View removed(&kDate, puts.at(0).second, 1);
  CHECK(removed.dsts() == std::vector<uint64_t>({12, 300}) && removed.label(0) == 0 && removed.label(1) == 1,
        "remove keeps the others' labels aligned");
  // the inline chunk: 6 edges at 2 bits = a 2 B block between the neighbour area and the columns
  std::vector<lists::ChunkOwner> owners = {{100, {3, 7}, {2, 0}}, {105, {7}, {1}}, {300, {1, 2, 9}, {3, 1, 2}}};
  std::vector<lists::ChunkOwner> bare = {{100, {3, 7}, {}}, {105, {7}, {}}, {300, {1, 2, 9}, {}}};
  std::vector<Value> rows;
  for (int64_t d = 1; d <= 6; d++) rows.push_back(make_row({{"creationDate", Value(d)}}));
  std::vector<PropRow> pr;
  for (size_t i = 0; i < rows.size(); i++) pr.push_back({i, &rows[i]});
  ValueRows vr(kDate, pr);
  std::string c0 = lists::encode_chunk(bare, 0, kDate, &vr), c2 = lists::encode_chunk(owners, 2, kDate, &vr);
  CHECK(c2.size() == c0.size() + 2, "6 edges at 2 bits = +2 B: " << c2.size() << " vs " << c0.size());
  lists::ChunkView cv(&kDate, c2, 2);
  CHECK(cv.label(0, 0) == 2 && cv.label(1, 0) == 1 && cv.label(2, 0) == 3 && cv.label(2, 2) == 2 &&
            same(cv.props_row(2, 2), rows[5]), "each edge's label, owner order; props after the block");
  const std::string prefix("e" "\x07" "a" "\x03" "\x00", 5);
  store->db()->Put(rocksdb::WriteOptions(), chunk_with_id(prefix, 100), c2);
  ChunkCodecs cc;
  ChunkWriteReader rd(cc, st);
  ChunkWrites w;
  chunk_rmw(cc, rd, w, prefix, 65536, 105, 3, &kDate, false, [&](ChunkWriteReader::Chunk& ch) {
    chunk_adj_add_inline(ch.groups, 105, 5, make_row({}), true, uint8_t(3));
    chunk_adj_remove_inline(ch.groups, 300, 1);
  }, 2);
  lists::ChunkView after(&kDate, w.puts.at(0).second, 2);
  CHECK(after.dsts(1) == std::vector<uint64_t>({5, 7}) && after.label(1, 0) == 3 && after.label(1, 1) == 1 &&
            after.dsts(2) == std::vector<uint64_t>({2, 9}) && after.label(2, 0) == 1 && after.label(2, 1) == 2 &&
            after.label(0, 0) == 2, "the kind-3 RMW adds the new label and keeps every other one aligned");
  std::cerr << "[labels] block 0xB1 0x01; list +1 B at 1 bit; chunk +2 B at 2 bits; labels survive add / remove\n";
}

// the last-chunk lane (Counts::LastChunk): a chunk RMW that lands on the family's last chunk records a read touch at the
// decode and a write touch at the put, with the chunk's bytes and the family's target; an interior chunk, an empty
// family and a run without the statistics record none. The probe is one uncounted seek per charged decode (raw_seeks),
// and it sees the overlay (a chunk the overlay deleted is not the last).
static void check_last_chunk() {
  std::vector<lists::ChunkOwner> lo = {{100, {3, 7}, {}}, {105, {7}, {}}}, hi = {{300, {1, 2, 9}, {}}};
  std::string c_lo = lists::encode_chunk(lo, 0, kNone, nullptr), c_hi = lists::encode_chunk(hi, 0, kNone, nullptr);
  char tmpl[] = "/tmp/evalcpp-lastchunk-XXXXXX";
  std::string dir = kvt::temp_dir(tmpl);
  StoreOptions so;
  auto store = Store::open_read_write(dir + "/db", so, true);
  const std::string prefix("e" "\x07" "a" "\x03" "\x00", 5), next("e" "\x07" "a" "\x03" "\x01", 5), empty("e" "\x09", 2);
  store->db()->Put(rocksdb::WriteOptions(), chunk_with_id(prefix, 100), c_lo);
  store->db()->Put(rocksdb::WriteOptions(), chunk_with_id(prefix, 300), c_hi);
  store->db()->Put(rocksdb::WriteOptions(), chunk_with_id(next, 1), c_lo);   // the next family: never this one's last
  ShadowOverlay ov;
  CountingStore st(*store, &ov);
  ChunkCodecs cc;
  const uint64_t T = 4096;
  auto add = [](uint64_t src, uint64_t dst) {
    return [=](ChunkWriteReader::Chunk& ch) { chunk_adj_add(ch.groups, src, dst, 0, std::nullopt); };   // propless
  };
  {   // an interior chunk: no touch, one probe
    ChunkWriteReader rd(cc, st);
    ChunkWrites w;
    chunk_rmw(cc, rd, w, prefix, T, 105, 3, &kNone, false, add(105, 8));
    w.apply(st);
    CHECK(!st.counts().last_chunk.any() && st.counts().raw_seeks == 1,
          "an interior chunk records no touch: " << st.counts().last_chunk.to_json().dump());
  }
  st.reset();
  ov.reset();
  {   // the last chunk, rewritten twice in one op: 1 read (the decode), 2 writes; the second's base is the first put
    ChunkWriteReader rd(cc, st);
    ChunkWrites w;
    chunk_rmw(cc, rd, w, prefix, T, 300, 3, &kNone, false, add(300, 11));
    chunk_rmw(cc, rd, w, prefix, T, 300, 3, &kNone, false, add(300, 12));
    CHECK(w.puts.size() == 2 && w.last_puts.size() == 2, "two puts of the last chunk: " << w.puts.size());
    uint64_t p1 = w.puts[0].second.size(), p2 = w.puts[1].second.size();
    w.apply(st);
    const Counts::LastChunk& l = st.counts().last_chunk;
    CHECK(l.reads == 1 && l.read_bytes == c_hi.size() && l.read_target == T, "one read touch: " << l.to_json().dump());
    CHECK(l.writes == 2 && l.write_bytes == p1 + p2 && l.write_base == c_hi.size() + p1 && l.write_target == 2 * T,
          "two write touches, the second from the first put: " << l.to_json().dump());
    CHECK(st.counts().raw_seeks == 1, "one probe (the second RMW reads the reader's copy)");
  }
  st.reset();
  ov.reset();
  {   // an empty family: the SEEK misses, a new chunk is put, no touch and no probe
    ChunkWriteReader rd(cc, st);
    ChunkWrites w;
    chunk_rmw(cc, rd, w, empty, T, 5, 3, &kNone, false, add(5, 6));
    w.apply(st);
    CHECK(!st.counts().last_chunk.any() && st.counts().raw_seeks == 0 && w.puts.size() == 1, "an empty family: no touch");
  }
  st.reset();
  ov.reset();
  {   // the overlay deleted the high chunk: the low one is now the family's last
    st.del_key(chunk_with_id(prefix, 300));
    st.reset();
    ChunkWriteReader rd(cc, st);
    ChunkWrites w;
    chunk_rmw(cc, rd, w, prefix, T, 105, 3, &kNone, false, add(105, 8));
    w.apply(st);
    CHECK(st.counts().last_chunk.reads == 1 && st.counts().last_chunk.writes == 1,
          "the overlay's delete makes the low chunk the last: " << st.counts().last_chunk.to_json().dump());
  }
  st.reset();
  ov.reset();
  {   // the statistics off (a latency run): no probe, no touch
    st.set_kvpair_stats(false);
    ChunkWriteReader rd(cc, st);
    ChunkWrites w;
    chunk_rmw(cc, rd, w, prefix, T, 300, 3, &kNone, false, add(300, 11));
    w.apply(st);
    CHECK(!st.counts().last_chunk.any() && st.counts().raw_seeks == 0, "statistics off: no probe, no touch");
    st.set_kvpair_stats(true);
  }
  std::cerr << "[last chunk] interior none; last 1 read + 2 writes; empty family none; overlay delete seen; off none\n";
}

TEST_CASE(test_meta_codecs) {
  check_columns();
  check_lists();
  check_list_unit();
  check_inline_chunk();
  check_own_chunk();
  check_labels();
  check_last_chunk();
  if (fails == 0) std::cerr << "test_meta_codecs ok\n";
  return fails ? 1 : 0;
}

}  // namespace
