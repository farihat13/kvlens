// What a ONE-ROW read of a props chunk costs, split by part: the value copy, the chunk decode (and the id
// decode inside it), the find, one cell and all cells. Person-like columns, sorted ids with small gaps, chunks
// filled to 64K and 512K by the chunker's estimate, for the plain body (delta_varint, for_bitpack).
// Also a check: every timed read returns the row's own values. Prints ns per read; ~1 s.
#include <chrono>
#include <iostream>
#include <random>

#include "codec/chunks.h"
#include "codec/varint.h"
#include "value/value.h"
#include "harness.h"

namespace {

using namespace evalcpp;
using clk = std::chrono::steady_clock;


static double ns_since(clk::time_point t0) { return double(std::chrono::duration_cast<std::chrono::nanoseconds>(clk::now() - t0).count()); }

TEST_CASE(bench_chunk_point_read) {
  std::vector<ColDecl> cols = {{"firstName", "str", false}, {"lastName", "str", false}, {"gender", "str", false},
                               {"birthday", "int", false}, {"creationDate", "int", false},
                               {"locationIP", "str", false}, {"browserUsed", "str", false},
                               {"email", "str", true}, {"speaks", "str", true}};
  std::mt19937_64 rng(7);
  std::cout << "one-row read of a Person-like props chunk, ns per read (mean)\n"
            << "codec              target  rows   bytes   copy   decode  (ids)   find  1 cell  all cells  total\n";
  for (uint64_t target : {uint64_t(65536), uint64_t(524288)}) {
    // rows until the chunker's estimate reaches the target
    std::vector<Value> props;
    std::vector<uint64_t> ids;
    uint64_t est = 0, id = 1000;
    while (est < target) {
      id += 1 + rng() % 6;
      RowFields f;
      f.emplace_back("firstName", Value("Name" + std::to_string(rng() % 500)));
      f.emplace_back("lastName", Value("Surname" + std::to_string(rng() % 2000)));
      f.emplace_back("gender", Value(rng() % 2 ? "male" : "female"));
      f.emplace_back("birthday", Value(int64_t(300000000000 + rng() % 900000000000)));
      f.emplace_back("creationDate", Value(int64_t(1260000000000 + rng() % 90000000000)));
      if (rng() % 10) f.emplace_back("locationIP", Value(std::to_string(rng() % 256) + "." + std::to_string(rng() % 256) + ".1.2"));
      f.emplace_back("browserUsed", Value(rng() % 3 ? "Firefox" : "Chrome"));
      ValueList em, sp;
      for (size_t k = 0; k < rng() % 3; k++) em.push_back(Value("user" + std::to_string(rng()) + "@mail.com"));
      for (size_t k = 0; k < 1 + rng() % 2; k++) sp.push_back(Value(k ? "en" : "zh"));
      if (!em.empty()) f.emplace_back("email", Value::list(std::move(em)));
      f.emplace_back("speaks", Value::list(std::move(sp)));
      props.push_back(Value::row(std::move(f)));
      ids.push_back(id);
      est += est_vertex(cols, props.back());
    }
    std::vector<PropRow> rows;
    for (size_t i = 0; i < props.size(); i++) rows.push_back({ids[i], &props[i]});
    for (const char* name : {"plain delta_varint", "plain for_bitpack"}) {
      ChunkCodecs cc = std::string(name) == "plain for_bitpack" ? ChunkCodecs::from_names("for_bitpack", "raw_offsets")
                     : ChunkCodecs();
      const std::string raw = encode_props(cc, cols, rows);
      const int reps = target > 100000 ? 300 : 2000;
      double t_copy = 0, t_dec = 0, t_ids = 0, t_find = 0, t_one = 0, t_all = 0;
      for (int r = 0; r < reps; r++) {
        size_t row = rng() % ids.size();
        auto t0 = clk::now();
        std::string copy(raw);                                   // the lookup's std::string(raw)
        t_copy += ns_since(t0);
        t0 = clk::now();
        std::shared_ptr<PropsChunk> chunk = PropsChunk::decode(&cc, &cols, std::move(copy));
        t_dec += ns_since(t0);
        if (cc.layout == Layout::Plain) {                        // the id decode alone (part of the decode above)
          size_t i = 0;
          get_uvarint(raw, i);
          t0 = clk::now();
          std::vector<uint64_t> v = decode_uids(cc.uids, raw, i, ids.size());
          t_ids += ns_since(t0);
          CHECK(v.size() == ids.size() && v[row] == ids[row], name << " ids");
        }
        t0 = clk::now();
        int64_t at = chunk->find(ids[row]);
        t_find += ns_since(t0);
        CHECK(at == int64_t(row), name << " find row " << row);
        t0 = clk::now();
        Value c0 = chunk->cell(0, size_t(at));
        t_one += ns_since(t0);
        CHECK(equals(c0, props[row].get("firstName")), name << " cell 0 row " << row);
        t0 = clk::now();
        for (size_t c = 0; c < cols.size(); c++) {
          Value v = chunk->cell(c, size_t(at));
          if (c == cols.size() - 1) CHECK(equals(v, props[row].get(cols[c].name)), name << " last cell row " << row);
        }
        t_all += ns_since(t0);
      }
      auto m = [&](double t) { return t / reps; };
      printf("%-18s %6luK %5zu %7zu %6.0f %8.0f %7s %6.0f %7.0f %10.0f %6.0f\n", name, (unsigned long)(target / 1024),
             ids.size(), raw.size(), m(t_copy), m(t_dec), cc.layout == Layout::Plain ? std::to_string(long(m(t_ids))).c_str() : "-",
             m(t_find), m(t_one), m(t_all), m(t_copy) + m(t_dec) + m(t_find) + m(t_all));
    }
  }
  if (fails) { std::cerr << fails << " failures\n"; return 1; }
  std::cout << "bench_chunk_point_read OK\n";
  return 0;
}

}  // namespace
