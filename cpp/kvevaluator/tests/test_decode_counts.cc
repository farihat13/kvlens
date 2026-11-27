// The decode counters (codec/decode_counts.h): each kind's decoders move exactly their own counters, by exactly
// what they decoded. Per kind: a known value is built with the codec's own encoder, decoded, and the counters
// compared with the numbers written here by hand; then ONE more decode request is injected and the counters must
// move by that request alone (a counter that did not see the request, or one that moved for another kind, fails).
#include <iostream>
#include <string>
#include <vector>

#include "codec/chunks.h"
#include "codec/columns.h"
#include "codec/decode_counts.h"
#include "codec/lists.h"
#include "codec/posting.h"
#include "codec/prim.h"
#include "codec/rowv2.h"
#include "formats/arangodb/vpack.h"
#include "value/value.h"
#include "harness.h"

namespace {

using namespace evalcpp;


struct Want { uint64_t rows = 0, cells = 0, postings = 0, chunks = 0, docs = 0, fields = 0, bytes = 0; };

// the counters since the last call, then zeroed
static DecodeCounts take() { DecodeCounts d = decode_counts(); decode_counts().reset(); return d; }

static void expect(const char* what, const Want& w) {
  DecodeCounts d = take();
  checks++;
  if (d.rows != w.rows || d.cells != w.cells || d.postings != w.postings || d.chunks != w.chunks || d.docs != w.docs ||
      d.fields != w.fields || d.bytes != w.bytes) {
    fails++;
    std::cerr << "FAIL " << what << ": rows " << d.rows << "/" << w.rows << " cells " << d.cells << "/" << w.cells
              << " postings " << d.postings << "/" << w.postings << " chunks " << d.chunks << "/" << w.chunks
              << " docs " << d.docs << "/" << w.docs << " fields " << d.fields << "/" << w.fields
              << " bytes " << d.bytes << "/" << w.bytes << " (got/want)\n";
  }
}

TEST_CASE(test_decode_counts) {
  // three rows over (name str, age int, tags multi); row 1 has no age, row 2 no tags
  std::vector<ColDecl> cols = {{"name", "str", false}, {"age", "int", false}, {"tags", "str", true}};
  std::vector<Value> props = {
      make_row({{"name", Value("ann")}, {"age", Value(int64_t(30))}, {"tags", Value::list({Value("a"), Value("b")})}}),
      make_row({{"name", Value("bob")}, {"tags", Value::list({Value("c")})}}),
      make_row({{"name", Value("cy")}, {"age", Value(int64_t(41))}})};
  std::vector<PropRow> rows = {{10, &props[0]}, {11, &props[1]}, {12, &props[2]}};
  ValueRows src(cols, rows);
  take();

  // --- row: nebula's RowV2
  {
    std::string raw = rowv2_encode(cols, src, 0, 0);
    RowV2 r(&cols, raw);
    expect("rowv2 parse", {.bytes = raw.size()});                    // the header parse: the row's bytes, no record yet
    r.row();
    expect("rowv2 row", {.rows = 1, .fields = 3});
    r.cell(1);
    expect("rowv2 injected cell", {.cells = 1, .fields = 1});
    std::string raw1 = rowv2_encode(cols, src, 1, 0);
    RowV2 r1(&cols, raw1);
    r1.cell(1);                                                      // absent: nothing decoded
    expect("rowv2 absent cell", {.bytes = raw1.size()});
    r1.row();
    expect("rowv2 row with an absent column", {.rows = 1, .fields = 2});
  }
  // --- row + cell: the configurable format's columns block
  {
    std::string block;
    columns::encode(block, cols, src);
    columns::View v(&cols, block, 0, 3);
    expect("columns parse", {.bytes = block.size()});
    v.row(0); v.row(1); v.row(2);
    expect("columns rows", {.rows = 3, .fields = 7});
    v.cell(0, 2);
    expect("columns injected cell", {.cells = 1, .fields = 1});
    v.cell(1, 1);
    expect("columns absent cell", {});
    std::string one = columns::encode_cell(cols[0], Value("ann"));   // a Split vertex record: one cell alone
    columns::decode_cell(cols[0], one);
    expect("columns decode_cell", {.cells = 1, .fields = 1, .bytes = one.size()});
  }
  // --- cell: the tagged primitive value (a Dgraph scalar, a named facet dict)
  {
    std::string raw = prim::encode_leaf(Value(int64_t(7)));
    prim::decode_leaf(raw);
    expect("prim scalar", {.cells = 1, .fields = 1, .bytes = raw.size()});
    std::string dict = prim::encode_leaf(make_row({{"a", Value(int64_t(1))}, {"b", Value("x")}}));
    prim::decode_leaf(dict);
    expect("prim dict", {.cells = 1, .fields = 2, .bytes = dict.size()});
    prim::decode_leaf("");                                           // null: nothing stored, nothing decoded
    expect("prim null", {});
  }
  // --- posting: a uid posting and an adjacency posting (schema facets: a RowV2 row per entry)
  {
    std::string raw = posting::encode_posting({3, 5, 9, 1000});
    posting::decode_posting(raw);
    expect("uid posting", {.postings = 1, .fields = 4, .bytes = raw.size()});
    posting::decode_posting(raw);
    expect("uid posting injected", {.postings = 1, .fields = 4, .bytes = raw.size()});
    std::vector<ColDecl> ecols = {{"since", "int", false}};
    Value f1 = make_row({{"since", Value(int64_t(2010))}}), f2 = make_row({{"since", Value(int64_t(2012))}});
    posting::AdjCodec codec = posting::AdjCodec::schema(&ecols, 0);
    std::string adj = posting::encode_adjacency({{4, f1}, {8, f2}}, true, codec);
    posting::decode_adjacency(adj, codec);
    // one posting of 2 entries; each entry's facets are a row of 1 cell; the bytes are the posting's, once
    expect("adjacency posting", {.rows = 2, .postings = 1, .fields = 4, .bytes = adj.size()});
    posting::AdjCodec named = posting::AdjCodec::named();
    std::string nadj = posting::encode_adjacency({{4, f1}, {8, f2}}, true, named);
    posting::decode_adjacency(nadj, named);
    expect("adjacency posting, named facets", {.cells = 2, .postings = 1, .fields = 4, .bytes = nadj.size()});
  }
  // --- posting: the configurable format's list (ids + a props block)
  {
    std::vector<ColDecl> ecols = {{"since", "int", false}};
    lists::Entries es = {{4, make_row({{"since", Value(int64_t(2010))}}), 0}, {8, make_row({{"since", Value(int64_t(2012))}}), 0},
                         {9, make_row({}), 0}};
    std::string raw = lists::encode(es, 0, ecols);
    lists::View v(&ecols, raw, 0);
    expect("list parse", {.postings = 1, .fields = 3, .bytes = raw.size()});   // its props block is inside the bytes
    v.props_row(0);
    expect("list injected props row", {.rows = 1, .fields = 1});
    v.props_row(2);
    expect("list props row with no cell", {.rows = 1});
  }
  // --- chunk: a props chunk and an adjacency chunk (plain layout), and the configurable format's adjacency chunk
  {
    ChunkCodecs cc;
    std::string raw = encode_props(cc, cols, rows);
    auto ch = PropsChunk::decode(&cc, &cols, raw);
    expect("props chunk parse", {.chunks = 1, .bytes = raw.size()});
    ch->row(ch->find(11));
    expect("props chunk row", {.rows = 1, .fields = 2});
    ch->cell(0, 0);
    expect("props chunk injected cell", {.cells = 1, .fields = 1});
    ch->cell(1, 1);
    expect("props chunk absent cell", {});
    chunk_ids(cc, raw);
    expect("chunk ids", {.chunks = 1, .bytes = raw.size()});
    std::vector<AdjGroup> groups = {{10, {20, 21, 22}, {}, {}}, {11, {30}, {}, {}}};
    std::string araw = encode_adj(cc, groups, false);
    auto ach = AdjChunk::decode(&cc, araw, false);
    expect("adj chunk parse", {.chunks = 1, .bytes = araw.size()});
    ach->dsts(0);
    expect("adj chunk list", {.postings = 1, .fields = 3});
    ach->dsts(1);
    expect("adj chunk injected list", {.postings = 1, .fields = 1});
    std::vector<ColDecl> none;
    std::vector<lists::ChunkOwner> owners = {{10, {20, 21}, {}}, {11, {30}, {}}};
    std::string lraw = lists::encode_chunk(owners, 0, none, nullptr);
    lists::ChunkView cv(&none, lraw, 0);
    expect("list chunk parse", {.chunks = 1, .bytes = lraw.size()});
    cv.dsts(0);
    expect("list chunk injected list", {.postings = 1, .fields = 2});
  }
  // --- doc: an ArangoDB document, whole and by named fields
  {
    std::string raw;
    using namespace evalcpp::arangodb;
    vp_encode_obj(raw, make_row({{"name", Value("ann")}, {"age", Value(int64_t(30))}, {"city", Value("x")}}));
    vp_decode_doc(raw);
    expect("document", {.docs = 1, .fields = 3, .bytes = raw.size()});
    vp_decode_fields(raw, {"age"});
    expect("document, one field asked", {.docs = 1, .fields = 1, .bytes = raw.size()});
    vp_decode_fields(raw, {"age", "nope"});
    expect("document injected, a missing field", {.docs = 1, .fields = 1, .bytes = raw.size()});
  }
  std::cout << "decode counts: " << checks << " checks, " << fails << " failed\n";
  return fails ? 1 : 0;
}

}  // namespace
