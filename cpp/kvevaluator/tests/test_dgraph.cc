// dgraph's codecs against Python-generated fixtures (tests/fixtures/dgraph.json, from
// gen_dgraph_fixtures.py): the prim leaf codec, UidPack, the adjacency and index posting values
// -- encode byte-identical, decode round-trips; plus the key codec's shape and the day token.
#include <algorithm>
#include <fstream>
#include <iostream>

#include "codec/prim.h"
#include "formats/dgraph/format.h"
#include "formats/dgraph/keys.h"
#include "codec/uidpack.h"
#include "util/vid.h"
#include "value/value.h"
#include "harness.h"

namespace {

using namespace evalcpp;
using namespace evalcpp::dgraph;


static std::string from_hex(const std::string& h) {
  std::string s;
  for (size_t i = 0; i + 1 < h.size(); i += 2) s.push_back(char(std::stoi(h.substr(i, 2), nullptr, 16)));
  return s;
}

TEST_CASE(test_dgraph) {
  std::string path = argc > 1 ? argv[1] : std::string(FIXTURE_DIR) + "/dgraph.json";
  std::ifstream f(path);
  if (!f) { std::cerr << "no fixture at " << path << "\n"; return 1; }
  json fx = json::parse(f);
  for (auto& c : fx["prim"]) {
    Value v = from_json(c["value"]);
    std::string want = from_hex(c["hex"].get<std::string>());
    CHECK(prim::encode_leaf(v) == want, "prim encode differs for " << repr(v));
    CHECK(equals(prim::decode_leaf(want), v), "prim decode differs for " << repr(v));
  }
  for (auto& c : fx["uidpack"]) {
    std::vector<uint64_t> uids = c["uids"].get<std::vector<uint64_t>>();
    std::string want = from_hex(c["hex"].get<std::string>());
    std::string got;
    uidpack_encode(got, uids);
    CHECK(got == want, "uidpack encode differs for n=" << uids.size() << " (" << got.size() << " vs " << want.size() << ")");
    size_t i = 0;
    CHECK(uidpack_decode(want, i, -1) == uids && i == want.size(), "uidpack decode n=" << uids.size());
  }
  for (auto& c : fx["adjacency"]) {
    std::vector<std::pair<uint64_t, Value>> postings;
    for (auto& p : c["postings"]) postings.emplace_back(p[0].get<uint64_t>(), from_json(p[1]));
    std::string want = from_hex(c["hex"].get<std::string>());
    CHECK(posting::encode_adjacency(postings, true, posting::AdjCodec::named()) == want, "adjacency encode differs for n=" << postings.size());
    auto back = posting::decode_adjacency(want, posting::AdjCodec::named());
    CHECK(back.size() == postings.size(), "adjacency decode size");
    for (size_t k = 0; k < back.size() && k < postings.size(); k++)
      CHECK(back[k].first == postings[k].first && equals(back[k].second, postings[k].second), "adjacency entry " << k);
  }
  for (auto& c : fx["posting"]) {
    std::vector<uint64_t> uids = c["uids"].get<std::vector<uint64_t>>();
    std::string want = from_hex(c["hex"].get<std::string>());
    CHECK(Dgraph::encode_posting(uids) == want, "posting encode differs for n=" << uids.size());
    CHECK(Dgraph::decode_posting(want) == uids, "posting decode n=" << uids.size());
  }
  // --- the unordered ('S') adjacency form: the hybrid's date-sorted postings (step-3 S2).
  // No Python byte fixture: the form is C++-only by doctrine (Python's overlay never meets
  // the codec); what must hold is exact-ORDER round-trip, the self-describing head byte,
  // and that the guards fire.
  {
    std::vector<std::pair<uint64_t, Value>> dated;   // date-desc == descending-ish uids, arbitrary
    for (uint64_t u : {900007ull, 13ull, 512000ull, 42ull, (0x9ull << 32) + 5, 7ull})
      dated.emplace_back(u, Value::row({{"creationDate", Value(int64_t(u * 3))}}));
    std::string s = posting::encode_adjacency(dated, /*ordered=*/false, posting::AdjCodec::named());
    CHECK(s[0] == 'S', "unordered adjacency carries the 'S' head byte");
    auto back = posting::decode_adjacency(s, posting::AdjCodec::named());
    CHECK(back.size() == dated.size(), "S round-trip size");
    bool same_order = true;
    for (size_t j = 0; j < back.size(); j++)
      same_order = same_order && back[j].first == dated[j].first && equals(back[j].second, dated[j].second);
    CHECK(same_order, "S round-trip preserves CALLER order exactly (uids + facets)");
    // verify-the-verifier: a decoder that silently re-sorted would still round-trip a set --
    // this fixture is order-sensitive, so it MUST differ from the ascending arrangement
    std::vector<std::pair<uint64_t, Value>> asc(dated);
    std::sort(asc.begin(), asc.end(), [](auto& a, auto& b) { return a.first < b.first; });
    bool differs = false;
    for (size_t j = 0; j < back.size(); j++) differs = differs || back[j].first != asc[j].first;
    CHECK(differs, "the order fixture actually distinguishes stored order from ascending");
    // the ordered form still refuses this input (regression), and 'S' refuses duplicates
    bool threw = false;
    try { posting::encode_adjacency(dated, true, posting::AdjCodec::named()); } catch (const std::exception&) { threw = true; }
    CHECK(threw, "'A' encode still refuses non-ascending input");
    auto dup = dated;
    dup.push_back(dated[0]);
    threw = false;
    try { posting::encode_adjacency(dup, false, posting::AdjCodec::named()); } catch (const std::exception&) { threw = true; }
    CHECK(threw, "'S' encode refuses a duplicate uid");
    // the price is real: the same SET costs at least as many bytes unordered as ordered
    std::string a = posting::encode_adjacency(asc, true, posting::AdjCodec::named());
    CHECK(a[0] == 'A' && s.size() >= a.size(), "'S' forfeits the delta compression (>= 'A' bytes)");
  }
  CHECK(day_token(1354060800000) == "0015672" && day_token(0) == "0000000" && day_token(-1) == "-000001", "day token");
  CHECK(uid_key("knows", kData, 5).size() == 5 + 1 + 8 && has_prefix(uid_key("knows", kData, 5), prefix("knows", kData)), "uid key");
  CHECK(!has_prefix(uid_key("knows", kReverse, 5), prefix("knows", kData)), "kinds are different prefixes");
  CHECK(term_key("creationDate", "0015672") < term_key("creationDate", "0015673"), "day tokens order as strings");
  // an index term from a vid: Python's str(value) of a vid is its hex16 str
  for (uint64_t u : {uint64_t(0x0123456789abcdefULL), uint64_t(1)}) {
    std::string t;
    try { t = term_of_value(Value::vid(u)); } catch (const std::exception& e) { t = std::string("<threw> ") + e.what(); }
    CHECK(t == hex16(u), "term_of_value(vid " << hex16(u) << ") = " << t);
  }
  if (fails) { std::cerr << fails << " failures\n"; return 1; }
  std::cout << "test_dgraph ok\n";
  return 0;
}

}  // namespace
