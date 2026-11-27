// The bytes value kind end to end (a LinkBench data blob): base64 and the {"$bytes": ...} JSON form, the value rules
// (bytes != str, order, hash, size), and a round trip through every codec that stores a cell -- prim, columns, the
// props chunk, rowv2 and the ArangoDB vpack -- each at the blob's RAW length. Also the refusals:
// a str in a bytes column, a malformed tag.
#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>

#include "codec/chunks.h"
#include "codec/columns.h"
#include "codec/prim.h"
#include "codec/rowv2.h"
#include "formats/arangodb/vpack.h"
#include "formats/rows.h"
#include "util/base64.h"
#include "value/value.h"
#include "harness.h"

namespace {

using namespace evalcpp;

static std::string all_bytes() {   // every byte value once, NUL and invalid UTF-8 included
  std::string s;
  for (int i = 0; i < 256; i++) s.push_back(char(i));
  return s;
}

template <class F> static bool throws(F f) {
  try { f(); } catch (const std::exception&) { return true; }
  return false;
}

static void check_base64() {
  std::string raw = all_bytes();
  for (size_t n = 0; n <= 6; n++) CHECK(base64_decode(base64_encode(raw.substr(0, n))) == raw.substr(0, n), "base64 len " << n);
  CHECK(base64_decode(base64_encode(raw)) == raw, "base64 all 256 bytes");
  CHECK(base64_encode(std::string("\x00\x01\x7f\x80\xc3\xff", 6)) == "AAF/gMP/", "base64 known value");   // Python: base64.b64encode
  CHECK(throws([] { base64_decode("abc"); }), "base64: a bad length is refused");
  CHECK(throws([] { base64_decode("ab!d"); }), "base64: a character outside the alphabet is refused");
  CHECK(throws([] { base64_decode("a=bc"); }), "base64: misplaced padding is refused");
}

static void check_value() {
  Value b = Value::bytes(all_bytes());
  CHECK(b.is_bytes() && !b.is_str() && std::string(b.type_name()) == "bytes", "kind");
  CHECK(b.as_bytes().size() == 256, "as_bytes keeps all 256 bytes");
  CHECK(throws([&] { b.as_str(); }), "as_str on bytes throws");
  json j = to_json(b);
  CHECK(j.is_object() && j.size() == 1 && j.contains("$bytes"), "to_json is {\"$bytes\": ...}");
  CHECK(json::parse(j.dump()) == j, "the JSON text parses back (base64 is plain ASCII)");
  CHECK(equals(from_json(j), b), "from_json(to_json(b)) == b");
  CHECK(!equals(Value::bytes("abc"), Value(std::string("abc"))), "bytes != str with the same content (Python)");
  CHECK(equals(Value::bytes("abc"), Value::bytes("abc")), "bytes == bytes");
  CHECK(hash_value(Value::bytes("abc")) == hash_value(Value::bytes("abc")), "hash consistent with equals");
  CHECK(compare(Value::bytes(std::string("\x01", 1)), Value::bytes(std::string("\xff", 1))) < 0, "order is by unsigned bytes");
  CHECK(neutral_compare(Value(std::string("zz")), Value::bytes("a")) < 0 &&
        neutral_compare(Value::bytes("a"), Value::list({})) < 0, "neutral order: str < bytes < list");
  CHECK(neutral_bytes(b) == 256, "neutral size = the raw length");
  CHECK(Value::bytes("").truthy() == false && b.truthy(), "truthiness");
  CHECK(throws([] { from_json(json::parse(R"({"$bytes": 1})")); }), "a tag holding a non-string is refused");
  CHECK(throws([] { from_json(json::parse(R"({"$bytes": "AA==", "x": 1})")); }), "a tag beside other fields is refused");
  CHECK(from_json(json::parse(R"({"x": "AA=="})")).is_row(), "an ordinary row stays a row");
}

static void check_prim() {
  Value b = Value::bytes(all_bytes());
  std::string enc = prim::encode_leaf(b);
  CHECK(enc.size() == 1 + 2 + 256 && enc[0] == 'b', "prim: tag 'b' + uvarint 256 (2 B) + the raw bytes, size " << enc.size());
  CHECK(equals(prim::decode_leaf(enc), b), "prim round trip");
  Value row = Value::row({{"data", b}, {"n", Value(int64_t(7))}});
  std::string er;
  prim::encode(er, row);
  size_t i = 0;
  CHECK(equals(prim::decode(er, i), row) && i == er.size(), "prim round trip inside a row");
}

static const std::vector<ColDecl> kNode = {{"ntype", "int", false}, {"data", "bytes", false}};

static std::vector<Value> node_rows() {
  return {Value::row({{"ntype", Value(int64_t(1))}, {"data", Value::bytes(all_bytes())}}),
          Value::row({{"ntype", Value(int64_t(2))}, {"data", Value::bytes(std::string("\xc3\xa9\x00", 3))}}),
          Value::row({{"ntype", Value(int64_t(3))}})};   // data absent
}

static void check_columns() {
  CHECK(columns::encode_cell(kNode[1], Value::bytes(all_bytes())).size() == 256, "columns: a bytes cell is its raw bytes");
  CHECK(equals(columns::decode_cell(kNode[1], columns::encode_cell(kNode[1], Value::bytes(all_bytes()))),
               Value::bytes(all_bytes())), "columns: cell round trip, a bytes value back");
  CHECK(throws([] { columns::encode_cell(kNode[1], Value(std::string("abc"))); }), "columns: a str in a bytes column is refused");
  std::vector<Value> rows = node_rows();
  std::vector<PropRow> pr;
  for (size_t r = 0; r < rows.size(); r++) pr.push_back({r + 1, &rows[r]});
  ValueRows vr(kNode, pr);
  std::string out;
  columns::encode(out, kNode, vr);
  columns::View v(&kNode, out, 0, rows.size());
  for (size_t r = 0; r < rows.size(); r++)
    CHECK(equals(v.cell(1, r), rows[r].get("data")), "columns: block round trip, row " << r);
  CHECK(v.cell(1, 0).is_bytes(), "columns: the block decodes a bytes value");
}

static void check_chunks() {
  std::vector<Value> rows = node_rows();
  std::vector<PropRow> pr;
  for (size_t r = 0; r < rows.size(); r++) pr.push_back({r + 1, &rows[r]});
  for (Layout layout : {Layout::Plain}) {
    ChunkCodecs cc;
    cc.layout = layout;
    std::string raw = encode_props(cc, kNode, pr);
    auto ch = PropsChunk::decode(&cc, &kNode, raw);
    for (size_t r = 0; r < rows.size(); r++)
      CHECK(equals(ch->cell(1, r), rows[r].get("data")), "chunk (layout " << int(layout) << "): row " << r);
    CHECK(ch->cell(1, 0).is_bytes(), "chunk: a bytes value back");
    ch->bulk();
    CHECK(equals(ch->row(0).get("data"), rows[0].get("data")), "chunk: after bulk()");
    // the absolute size: a 256 B blob costs what a str of the same 256 raw bytes costs in a str column (length + bytes)
    static const std::vector<ColDecl> kText = {{"ntype", "int", false}, {"data", "str", false}};
    Value b = Value::row({{"ntype", Value(int64_t(1))}, {"data", Value::bytes(all_bytes())}});
    Value t = Value::row({{"ntype", Value(int64_t(1))}, {"data", Value(all_bytes())}});
    std::vector<PropRow> pb = {{1, &b}}, pt = {{1, &t}};
    CHECK(encode_props(cc, kNode, pb).size() == encode_props(cc, kText, pt).size(),
          "chunk (layout " << int(layout) << "): a 256 B blob is the size of a str of the same bytes");
  }
}

static void check_rowv2() {
  std::vector<Value> rows = node_rows();
  std::vector<PropRow> pr = {{1, &rows[0]}};
  ValueRows vr(kNode, pr);
  std::string raw = rowv2_encode(kNode, vr, 0, 0);
  RowV2 row(&kNode, raw);
  CHECK(equals(row.cell(1), rows[0].get("data")) && row.cell(1).is_bytes(), "rowv2 round trip");
  CHECK(row.cell_bytes(1) == 8 + 256, "rowv2: the slot + the raw length, got " << row.cell_bytes(1));
}

static void check_vpack() {
  Value doc = Value::row({{"ntype", Value(int64_t(1))}, {"data", Value::bytes(all_bytes())}});
  std::string raw;
  arangodb::vp_encode_obj(raw, doc);
  CHECK(equals(arangodb::vp_decode_doc(raw), doc), "vpack round trip");
  for (size_t n : {size_t(0), size_t(255), size_t(256), size_t(70000)}) {
    Value d = Value::row({{"data", Value::bytes(std::string(n, '\x80'))}});
    std::string r;
    arangodb::vp_encode_obj(r, d);
    CHECK(equals(arangodb::vp_decode_doc(r), d), "vpack round trip, " << n << " bytes");
  }
  // the 211-byte blob's header: 1 head + a 1-byte length (a str of that length takes the 9-byte long form)
  Value small = Value::row({{"data", Value::bytes(std::string(211, '\x80'))}});
  Value text = Value::row({{"data", Value(std::string(211, 'a'))}});
  std::string rs, rt;
  arangodb::vp_encode_obj(rs, small);
  arangodb::vp_encode_obj(rt, text);
  CHECK(rt.size() - rs.size() == 7, "vpack: a 211 B blob's header is 2 B, a str's 9 B (difference " << rt.size() - rs.size() << ")");
}

TEST_CASE(test_bytes) {
  check_base64();
  check_value();
  check_prim();
  check_columns();
  check_chunks();
  check_rowv2();
  check_vpack();
  std::cerr << "test_bytes: " << checks << " checks, " << fails << " failed\n";
  return fails ? 1 : 0;
}

}  // namespace
