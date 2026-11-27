// The chunk codecs and the chunker's size estimate against Python-generated fixtures
// (tests/fixtures/chunks.json, from gen_chunk_fixtures.py): encode must be byte-identical,
// decode must round-trip, and the LOGICAL estimate must match (it decides chunk boundaries).
#include <fstream>
#include <iostream>

#include "codec/chunks.h"
#include "value/value.h"
#include "harness.h"

namespace {

using namespace evalcpp;


static std::string from_hex(const std::string& h) {
  std::string s;
  for (size_t i = 0; i + 1 < h.size(); i += 2) s.push_back(char(std::stoi(h.substr(i, 2), nullptr, 16)));
  return s;
}

static std::vector<ColDecl> cols_of(const json& j) {
  std::vector<ColDecl> cols;
  for (auto& c : j) cols.push_back({c[0].get<std::string>(), c[1].get<std::string>(), c[2].get<bool>()});
  return cols;
}

TEST_CASE(test_chunks) {
  std::string path = argc > 1 ? argv[1] : std::string(FIXTURE_DIR) + "/chunks.json";
  std::ifstream f(path);
  if (!f) { std::cerr << "no fixture at " << path << "\n"; return 1; }
  json cases = json::parse(f);
  ChunkCodecs cc;   // delta_varint + raw_offsets: Python's bytes
  for (auto& c : cases) {
    std::string kind = c["kind"].get<std::string>();
    std::string want = from_hex(c["hex"].get<std::string>());
    if (kind == "props" || kind == "ep") {
      std::vector<ColDecl> cols = cols_of(c["cols"]);
      std::vector<Value> props;
      std::vector<PropRow> rows;
      for (auto& r : c["rows"]) props.push_back(from_json(r[1]));
      for (size_t i = 0; i < props.size(); i++) rows.push_back({c["rows"][i][0].get<uint64_t>(), &props[i]});
      std::string got = kind == "props" ? encode_props(cc, cols, rows) : encode_ep(cc, cols, rows);
      CHECK(got == want, kind << " encode differs (" << got.size() << " vs " << want.size() << " bytes)");
      for (size_t i = 0; i < rows.size(); i++) {
        uint64_t est = kind == "props" ? est_vertex(cols, props[i]) : est_ep(props[i]);
        CHECK(est == c["est"][i].get<uint64_t>(), kind << " est row " << i << ": " << est << " vs " << c["est"][i]);
      }
      std::shared_ptr<PropsChunk> chunkp = PropsChunk::decode(&cc, &cols, want);
      PropsChunk& chunk = *chunkp;
      CHECK(chunk.size() == rows.size(), "row count");
      for (size_t i = 0; i < rows.size(); i++) {
        int64_t r = chunk.find(rows[i].key);
        CHECK(r == int64_t(i), "find row " << i);
        // every declared column: absent -> null, present -> equal
        for (size_t col = 0; col < cols.size(); col++) {
          Value got_cell = chunk.cell(col, i);
          Value want_cell = props[i].get(cols[col].name);
          CHECK(equals(got_cell, want_cell), kind << " cell " << cols[col].name << " row " << i << ": " << repr(got_cell) << " vs " << repr(want_cell));
        }
      }
      CHECK(chunk.find(rows.back().key + 1) < 0, "find missing");
    } else {
      bool with_ords = c["with_ordinals"].get<bool>();
      std::vector<AdjGroup> groups;
      for (auto& g : c["groups"]) {
        AdjGroup ag;
        ag.src = g[0].get<uint64_t>();
        for (auto& d : g[1]) ag.dsts.push_back(d.get<uint64_t>());
        if (with_ords) for (auto& o : g[2]) ag.ords.push_back(o.get<uint64_t>());
        groups.push_back(ag);
      }
      std::string got = encode_adj(cc, groups, with_ords);
      CHECK(got == want, "adj encode differs (ords=" << with_ords << ")");
      for (size_t i = 0; i < groups.size(); i++)
        CHECK(est_adj(groups[i].dsts.size(), with_ords) == c["est"][i].get<uint64_t>(), "adj est " << i);
      std::shared_ptr<AdjChunk> chunkp = AdjChunk::decode(&cc, want, with_ords);
      AdjChunk& chunk = *chunkp;
      CHECK(chunk.size() == groups.size() && chunk.first() == groups.front().src && chunk.last() == groups.back().src, "adj header");
      for (size_t i = 0; i < groups.size(); i++) {
        int64_t k = chunk.find(groups[i].src);
        CHECK(k == int64_t(i), "adj find " << i);
        CHECK(chunk.dsts(i) == groups[i].dsts, "adj dsts " << i);
        if (with_ords) CHECK(chunk.ordinals(i) == groups[i].ords, "adj ordinals " << i);
      }
      CHECK(chunk_ids(cc, want).size() == groups.size(), "chunk_ids on an adj header (srcs)");
      // the far-end label block (C++ only): bits 0 = the bytes above; else ceil(edges x bits / 8) more bytes, and
      // dsts, ordinals and codes all read back
      {
        size_t edges = 0;
        for (auto& g : groups) edges += g.dsts.size();
        CHECK(chunkp->labels(0).empty(), "a chunk without a label block gives no codes");
        for (unsigned bits : {1u, 2u, 3u}) {
          std::vector<AdjGroup> lg = groups;
          for (size_t i = 0; i < lg.size(); i++)
            for (size_t j = 0; j < lg[i].dsts.size(); j++) lg[i].labels.push_back(uint8_t((i * 7 + j * 3) % (1u << bits)));
          std::string enc = encode_adj(cc, lg, with_ords, bits);
          CHECK(enc.size() == got.size() + (edges * bits + 7) / 8, "label block size, bits " << bits << ": " << enc.size() << " vs " << got.size());
          std::shared_ptr<AdjChunk> lc = AdjChunk::decode(&cc, enc, with_ords, bits);
          for (size_t i = 0; i < lg.size(); i++) {
            CHECK(lc->find(lg[i].src) == int64_t(i) && lc->dsts(i) == lg[i].dsts, "labelled adj dsts " << i << " bits " << bits);
            CHECK(lc->labels(i) == lg[i].labels, "labelled adj codes " << i << " bits " << bits);
            if (with_ords) CHECK(lc->ordinals(i) == lg[i].ords, "labelled adj ordinals " << i << " bits " << bits);
          }
        }
        bool threw = false;
        try { encode_adj(cc, groups, with_ords, 1); } catch (const std::exception&) { threw = true; }
        CHECK(threw || edges == 0, "a label width without one code per neighbour is refused");
      }
    }
  }
  // the alternate encoders: same fixtures must round-trip (bytes differ from Python by design)
  ChunkCodecs alt = ChunkCodecs::from_names("for_bitpack", "zstd_column");
  for (auto& c : cases) {
    std::string kind = c["kind"].get<std::string>();
    if (kind == "adj") {
      bool with_ords = c["with_ordinals"].get<bool>();
      std::vector<AdjGroup> groups;
      for (auto& g : c["groups"]) {
        AdjGroup ag;
        ag.src = g[0].get<uint64_t>();
        for (auto& d : g[1]) ag.dsts.push_back(d.get<uint64_t>());
        if (with_ords) for (auto& o : g[2]) ag.ords.push_back(o.get<uint64_t>());
        groups.push_back(ag);
      }
      std::shared_ptr<AdjChunk> chunkp = AdjChunk::decode(&alt, encode_adj(alt, groups, with_ords), with_ords);
      AdjChunk& chunk = *chunkp;
      for (size_t i = 0; i < groups.size(); i++) {
        CHECK(chunk.find(groups[i].src) == int64_t(i) && chunk.dsts(i) == groups[i].dsts, "for_bitpack adj " << i);
        if (with_ords) CHECK(chunk.ordinals(i) == groups[i].ords, "for_bitpack ordinals " << i);
      }
      continue;
    }
    std::vector<ColDecl> cols = cols_of(c["cols"]);
    std::vector<Value> props;
    std::vector<PropRow> rows;
    for (auto& r : c["rows"]) props.push_back(from_json(r[1]));
    for (size_t i = 0; i < props.size(); i++) rows.push_back({c["rows"][i][0].get<uint64_t>(), &props[i]});
    std::shared_ptr<PropsChunk> chunkp = PropsChunk::decode(&alt, &cols, encode_props(alt, cols, rows));
    PropsChunk& chunk = *chunkp;
    CHECK(chunk_ids(alt, encode_props(alt, cols, rows)).size() == rows.size(), "for_bitpack ids");
    for (size_t i = 0; i < rows.size(); i++) {
      CHECK(chunk.find(rows[i].key) == int64_t(i), "alt find " << i);
      for (size_t col = 0; col < cols.size(); col++)
        CHECK(equals(chunk.cell(col, i), props[i].get(cols[col].name)), "alt cell " << cols[col].name << " row " << i);
    }
  }
  // big chunks: a cell's rank crosses the 64-row absent directory (the fixtures hold <= 3 rows); sizes at and
  // around the 64-row boundary, a fixed pseudo-random absent pattern plus a fully absent 64-row run
  size_t big_cells = 0;
  for (size_t n : {63, 64, 65, 129, 1000}) {
    std::vector<ColDecl> cols = {{"a", "int", false}, {"s", "str", false}};
    std::vector<Value> props;
    uint64_t lcg = 12345;
    for (size_t i = 0; i < n; i++) {
      lcg = lcg * 6364136223846793005ull + 1442695040888963407ull;
      bool run_absent = i >= 128 && i < 192;
      RowFields f;
      if (!run_absent && (lcg >> 33) % 3) f.emplace_back("a", Value(int64_t(i * 7)));
      if (!run_absent && (lcg >> 40) % 4) f.emplace_back("s", Value("v" + std::to_string(i)));
      props.push_back(Value::row(std::move(f)));
    }
    std::vector<PropRow> rows;
    for (size_t i = 0; i < n; i++) rows.push_back({uint64_t(10 + 3 * i), &props[i]});
    for (const ChunkCodecs* codecs : {&cc, &alt}) {
      std::shared_ptr<PropsChunk> chunkp = PropsChunk::decode(codecs, &cols, encode_props(*codecs, cols, rows));
      for (size_t i = 0; i < n; i++)
        for (size_t col = 0; col < cols.size(); col++, big_cells++)
          CHECK(equals(chunkp->cell(col, i), props[i].get(cols[col].name)),
                "big chunk n=" << n << " cell " << cols[col].name << " row " << i);
    }
  }
  CHECK(big_cells == 2 * 2 * (63 + 64 + 65 + 129 + 1000), "big chunk cells checked: " << big_cells);
  // for_bitpack edge cases: one element, equal deltas, big deltas
  for (std::vector<uint64_t> v : {std::vector<uint64_t>{}, {42}, {5, 5, 5}, {0, 1ull << 59, 1ull << 60}, {1, 2, 3, 4, 5, 6, 7, 8, 9}}) {
    std::string b;
    encode_uids(b, UidCodec::ForBitpack, v);
    size_t i = 0;
    CHECK(decode_uids(UidCodec::ForBitpack, b, i, v.size()) == v && i == b.size(), "for_bitpack round trip n=" << v.size());
  }
  if (fails) { std::cerr << fails << " failures\n"; return 1; }
  std::cout << "test_chunks OK (" << cases.size() << " fixture cases + alternate encoders + " << big_cells << " big-chunk cells)\n";
  return 0;
}

}  // namespace
