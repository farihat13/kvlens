// The used-bytes units of the chunk decoders (the KV-amplification meter): PropsChunk::cell_bytes and ChunkView's
// id_widths / props_bytes each equal the Condensed unit of the same data (columns::View::cell_bytes over a one-row
// block, lists::View's id_bytes / props_bytes over the owner's own list), so a Chunked read and a Condensed read of the
// same request can book the same used bytes. AdjChunk::id_widths (Per-type CSR's chunk) equals each entry's stored
// width as a Condensed list codes it (lists::View's id_bytes, tagged vids); the accessors move no decode count.
#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>

#include "codec/chunks.h"
#include "codec/columns.h"
#include "codec/decode_counts.h"
#include "codec/lists.h"
#include "formats/rows.h"
#include "value/value.h"
#include "harness.h"

namespace {

using namespace evalcpp;

static const std::vector<ColDecl> kCols = {{"name", "str", false}, {"born", "date", false}, {"emails", "str", true},
                                           {"blob", "bytes", false}};
static const std::vector<ColDecl> kEdge = {{"creationDate", "date", false}, {"note", "str", false}};

static Value person(int i) {
  RowFields f;
  if (i % 3 != 0) f.emplace_back("name", Value(std::string(size_t(i), 'a' + char(i % 26))));   // absent every 3rd row
  f.emplace_back("born", Value(int64_t(1000 + i)));
  if (i % 2) f.emplace_back("emails", Value::list({Value(std::string("x@y")), Value(std::string(size_t(i), 'e'))}));
  if (i % 4 == 1) f.emplace_back("blob", Value::bytes(std::string(size_t(i * 3), '\x80')));
  return Value::row(std::move(f));
}

// a compressed string column (zstd_column) books 0 per string cell, fixed cells their 8
static void check_zstd_strings() {
  std::vector<Value> rows;
  for (int i = 0; i < 40; i++) rows.push_back(person(i));
  std::vector<PropRow> pr;
  for (size_t i = 0; i < rows.size(); i++) pr.push_back({100 + 7 * i, &rows[i]});
  ChunkCodecs cc;
  cc.strings = StrCodec::ZstdColumn;
  auto chunk = PropsChunk::decode(&cc, &kCols, encode_props(cc, kCols, pr));
  size_t strs = 0, fixed = 0;
  for (size_t r = 0; r < rows.size(); r++) {
    CHECK(chunk->cell_bytes(0, r) == 0 && chunk->cell_bytes(2, r) == 0 && chunk->cell_bytes(3, r) == 0, "zstd row " << r << ": string cells book 0");
    CHECK(chunk->cell_bytes(1, r) == 8, "zstd row " << r << ": the date cell books 8");
    strs += 3; fixed++;
  }
  CHECK(strs == 120 && fixed == 40, "zstd cells " << strs << " + " << fixed);
}

static void check_props_chunk() {
  std::vector<Value> rows;
  for (int i = 0; i < 40; i++) rows.push_back(person(i));
  std::vector<PropRow> pr;
  for (size_t i = 0; i < rows.size(); i++) pr.push_back({100 + 7 * i, &rows[i]});
  ChunkCodecs cc;                                       // the plain layout, raw-offset strings (the grid's)
  auto chunk = PropsChunk::decode(&cc, &kCols, encode_props(cc, kCols, pr));
  size_t compared = 0;
  DecodeCounts in_accessors;                            // the decode counts the accessors alone move
  for (size_t r = 0; r < rows.size(); r++) {
    std::vector<PropRow> one = {{0, &rows[r]}};         // the Condensed row: a one-row columns block
    std::string raw;
    columns::encode(raw, kCols, ValueRows(kCols, one));
    columns::View cv(&kCols, raw, 0, 1);
    for (size_t c = 0; c < kCols.size(); c++, compared++) {
      DecodeCounts b = decode_counts();
      size_t got = chunk->cell_bytes(c, r);
      in_accessors.add(decode_counts().since(b));
      CHECK(got == cv.cell_bytes(c, 0), "props chunk row " << r << " col " << kCols[c].name << ": " << got << " vs Condensed " << cv.cell_bytes(c, 0));
    }
  }
  CHECK(compared == 160, "compared " << compared << " cells");
  CHECK(in_accessors == DecodeCounts(), "cell_bytes moves no decode count");
}

static void check_chunk_view() {
  // owners with small and large gaps (1-3 B uvarints), one owner with no edge props asked of an absent cell
  const uint64_t T = uint64_t(3) << 60;   // tagged vids, as the stores' ids
  std::vector<lists::ChunkOwner> owners = {{T | 100, {T | 3, T | 7, T | 300}, {}}, {T | 105, {T | 7}, {}},
                                           {T | 300, {1, 2, 9, 70000, 70001, T | 9}, {}}};
  std::vector<Value> rows;
  size_t edges = 0;
  for (auto& o : owners) edges += o.dsts.size();
  for (size_t e = 0; e < edges; e++)
    rows.push_back(e % 4 == 2 ? Value::row({{"creationDate", Value(int64_t(e))}})
                              : Value::row({{"creationDate", Value(int64_t(e))}, {"note", Value(std::string(e, 'n'))}}));
  std::vector<PropRow> pr;
  for (size_t i = 0; i < rows.size(); i++) pr.push_back({i, &rows[i]});
  ValueRows vr(kEdge, pr);
  lists::ChunkView cv(&kEdge, lists::encode_chunk(owners, 0, kEdge, &vr), 0);
  DecodeCounts in_accessors;
  size_t base = 0, compared = 0;
  for (size_t i = 0; i < owners.size(); i++) {
    std::vector<PropRow> mine;
    for (size_t j = 0; j < owners[i].dsts.size(); j++) mine.push_back({j, &rows[base + j]});
    ValueRows mr(kEdge, mine);
    std::string list = lists::encode(owners[i].dsts, nullptr, 0, kEdge, &mr);   // the owner's Condensed list
    lists::View lv(&kEdge, list, 0);
    DecodeCounts b = decode_counts();
    std::vector<uint8_t> w = cv.id_widths(i);
    std::vector<size_t> pb;
    for (size_t j = 0; j < w.size(); j++) pb.push_back(cv.props_bytes(i, j));
    in_accessors.add(decode_counts().since(b));
    CHECK(w.size() == lv.size(), "owner " << i << ": " << w.size() << " widths for " << lv.size() << " entries");
    for (size_t j = 0; j < lv.size() && j < w.size(); j++, compared++) {
      CHECK(w[j] == lv.id_bytes(j), "owner " << i << " entry " << j << " id width " << int(w[j]) << " vs Condensed " << lv.id_bytes(j));
      CHECK(pb[j] == lv.props_bytes(j), "owner " << i << " entry " << j << " props " << pb[j] << " vs Condensed " << lv.props_bytes(j));
    }
    base += owners[i].dsts.size();
  }
  CHECK(compared == edges, "compared " << compared << " of " << edges << " entries");
  CHECK(in_accessors == DecodeCounts(), "id_widths / props_bytes move no decode count");
}

static void check_adj_chunk() {
  // tagged vids (the tag nibble at the top, as the stores' ids): the first id is where a delta-from-0 and a Condensed
  // list's group-base coding differ
  const uint64_t T = uint64_t(3) << 60;
  std::vector<AdjGroup> groups = {{T | 100, {T | 3, T | 7, T | 300}, {}, {}}, {T | 105, {}, {}, {}},
                                  {T | 300, {1, 2, 9, 70000, 70001, T | 5}, {}, {}}};
  static const std::vector<ColDecl> none;
  {
    ChunkCodecs cc;
    auto ac = AdjChunk::decode(&cc, encode_adj(cc, groups, false), false);
    size_t compared = 0;
    DecodeCounts in_accessors;
    for (size_t i = 0; i < groups.size(); i++) {
      const auto& d = groups[i].dsts;
      std::string list = lists::encode(d, nullptr, 0, none, nullptr);   // the owner's Condensed list
      lists::View lv(&none, list, 0);
      DecodeCounts b = decode_counts();
      std::vector<uint8_t> w = ac->id_widths(i);
      in_accessors.add(decode_counts().since(b));
      CHECK(w.size() == d.size(), "plain owner " << i << ": " << w.size() << " widths for " << d.size());
      for (size_t j = 0; j < d.size() && j < w.size(); j++, compared++) {
        size_t want = lv.id_bytes(j);
        CHECK(w[j] == want, "plain owner " << i << " entry " << j << ": " << int(w[j]) << " vs Condensed " << want);
      }
    }
    CHECK(compared == 9, "adj chunk compared " << compared);
    CHECK(in_accessors == DecodeCounts(), "AdjChunk::id_widths moves no decode count");
  }
}

TEST_CASE(test_used_units) {
  check_props_chunk();
  check_chunk_view();
  check_adj_chunk();
  check_zstd_strings();
  std::cerr << "test_used_units: " << checks << " checks, " << fails << " failed\n";
  return fails ? 1 : 0;
}

}  // namespace
