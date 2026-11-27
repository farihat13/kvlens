// nebula's RowV2 codec against Python-generated fixtures (tests/fixtures/nebula.json, from
// gen_nebula_fixtures.py): encode must be byte-identical, every declared cell must decode to the
// row's value (null when absent); plus the key codec's shape.
#include <fstream>
#include <iostream>

#include "formats/nebula/keys.h"
#include "codec/rowv2.h"
#include "formats/rows.h"
#include "value/value.h"
#include "harness.h"

namespace {

using namespace evalcpp;
using namespace evalcpp::nebula;


static std::string from_hex(const std::string& h) {
  std::string s;
  for (size_t i = 0; i + 1 < h.size(); i += 2) s.push_back(char(std::stoi(h.substr(i, 2), nullptr, 16)));
  return s;
}

TEST_CASE(test_nebula) {
  std::string path = argc > 1 ? argv[1] : std::string(FIXTURE_DIR) + "/nebula.json";
  std::ifstream f(path);
  if (!f) { std::cerr << "no fixture at " << path << "\n"; return 1; }
  json cases = json::parse(f);
  for (auto& c : cases) {
    std::vector<ColDecl> cols;
    for (auto& d : c["cols"]) cols.push_back({d[0].get<std::string>(), d[1].get<std::string>(), d[2].get<bool>()});
    Value props = from_json(c["props"]);
    std::vector<PropRow> rows = {{1, &props}};
    ValueRows src(cols, rows);
    std::string want = from_hex(c["hex"].get<std::string>());
    std::string got = rowv2_encode(cols, src, 0, c["timestamp"].get<int>());
    CHECK(got == want, c["kind"] << " " << (c.contains("label") ? c["label"] : c["etype"]) << " encode differs ("
                                 << got.size() << " vs " << want.size() << " bytes, ts " << c["timestamp"] << ")");
    RowV2 row(&cols, want);
    for (size_t k = 0; k < cols.size(); k++) {
      Value want_cell = props.get(cols[k].name);
      CHECK(equals(row.cell(k), want_cell), "cell " << cols[k].name << ": " << repr(row.cell(k)) << " vs " << repr(want_cell));
      CHECK(row.present(k) == !want_cell.is_null(), "present " << cols[k].name);
    }
    CHECK(equals(row.row(), props), "row() differs: " << repr(row.row()) << " vs " << repr(props));
  }
  // keys: shape and prefix relations, in both orders (Config::key_order); the same fields and widths either way
  for (KeyOrder order : {KeyOrder::VidFirst, KeyOrder::TypeFirst}) {
    Keys keys{order};
    std::string vk = keys.vertex_key(3, 0x1122334455667788ull);
    CHECK(vk.size() == 13 && vk[0] == 'v' && keys.type_field(vk) == 3, "vertex key shape");
    std::string ep = keys.edge_prefix(7, true, 42), ek = keys.edge_key(7, true, 42, 99);
    CHECK(ek.size() == 21 && has_prefix(ek, ep) && edge_dst(ek) == 99 && keys.type_field(ek) == 7, "edge key shape");
    std::string rk = keys.edge_key(7, false, 42, 99);
    CHECK(!has_prefix(rk, ep) && keys.type_field(rk) == (7u | 0x80000000u), "reverse edges are a different prefix");
    CHECK(has_prefix(keys.edge_key_dated(7, false, 42, 1234, 99), keys.edge_prefix(7, false, 42)) &&
          edge_dst(keys.edge_key_dated(7, false, 42, 1234, 99)) == 99, "a dated key keeps the (src, etype) prefix and dst last");
    if (order == KeyOrder::VidFirst) {
      // the engine's order: a vertex's tags are one key run, and so are all of a src's edges
      CHECK(has_prefix(keys.vertex_key(4, 0x1122334455667788ull), vk.substr(0, 9)), "a vid's tags share the 'v' vid prefix");
      CHECK(keys.edge_prefix(7, true, 42) < keys.edge_prefix(8, true, 42) && keys.edge_prefix(8, true, 42) < keys.edge_prefix(7, true, 43),
            "etype order within a src");
      CHECK(vk.substr(1, 8) == std::string("\x11\x22\x33\x44\x55\x66\x77\x88", 8), "the vid leads");
    } else {
      CHECK(keys.edge_prefix(7, true, 42) < keys.edge_prefix(7, true, 43) && keys.edge_prefix(7, true, 43) < keys.edge_prefix(8, true, 42),
            "src order within an etype");
      CHECK(vk.substr(1, 4) == std::string("\0\0\0\3", 4), "the tag leads");
    }
  }
  if (fails) { std::cerr << fails << " failures\n"; return 1; }
  std::cout << "test_nebula ok (" << cases.size() << " rows)\n";
  return 0;
}

}  // namespace
