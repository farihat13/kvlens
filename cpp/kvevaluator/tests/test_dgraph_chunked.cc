// dgraph's chunk knob (Config::chunk_target): dgraph-c∞ (today's single-KV postings) and a
// chunked dgraph must give IDENTICAL answers, with the chunked arm's extra cost exactly the
// parts (N = parts on a point read; one landing per part on the day-token walks). Both arms
// are ingested from one tiny record stream and driven through the REAL path (Session: bind +
// execute), so the part-aware cursor walks -- read_index_range, read_top_k_by_index -- run
// their production code. chunk_target = 23 B: 'U' postings split 9 uids per part
// (test_write_counts), an adjacency entry (uid + 24 B facet) is a part of its own.
// The top-k trap this pins: on the split day the NEWEST persons sit in the LOWEST uids
// (part 0, the last landing of a backward walk); a walk that checked k mid-token would
// stop before part 0 and return the wrong 41.
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <unistd.h>

#include "cli/session.h"
#include "formats/dgraph/format.h"
#include "formats/dgraph/keys.h"
#include "ingest/ingest.h"
#include "schema/schema.h"
#include "store/counting.h"
#include "store/rocksdb_store.h"
#include "store/unit_value.h"
#include "util/vid.h"
#include "repo_paths.h"
#include "harness.h"

namespace {

using namespace evalcpp;


static constexpr int64_t kDay = 86400000;
static constexpr int64_t kMs0 = 15000 * kDay;   // day D0's first ms

TEST_CASE(test_dgraph_chunked) {
  std::string schema_path = ldbc_schema_dir() + "/schema.json";
  Schema s = Schema::load(schema_path);
  uint64_t tag = uint64_t(s.tag_of_label("Person")) << kVidShift;
  auto person = [&](int i) { return hex16(tag | uint64_t(i)); };
  // persons 1..40 on day D0, ms DEscending with uid (1 = newest); 41..50 on D0-1; 51..60 on D0+1
  auto ms_of = [&](int i) -> int64_t {
    if (i <= 40) return kMs0 + int64_t(41 - i) * 1000;
    if (i <= 50) return kMs0 - kDay + int64_t(i) * 1000;
    return kMs0 + kDay + int64_t(i) * 1000;
  };
  std::string records;
  for (int i = 1; i <= 60; i++)
    records += "[\"v\",\"" + person(i) + "\",\"Person\",{\"id\":" + std::to_string(i) + ",\"firstName\":\"Bo\",\"lastName\":\"L" +
               std::to_string(i) + "\",\"gender\":\"male\",\"birthday\":1000,\"creationDate\":" + std::to_string(ms_of(i)) + "}]\n";
  for (int i = 2; i <= 60; i++)   // the hub: person 1 knows everyone (59 entries, a 24 B facet each)
    records += "[\"e\",\"knows\",\"" + person(1) + "\",\"" + person(i) + "\",{\"creationDate\":" + std::to_string(ms_of(i)) +
               "},\"Person\",\"Person\"]\n";
  char t1[] = "/tmp/evalcpp-dgc-inf-XXXXXX", t2[] = "/tmp/evalcpp-dgc-c23-XXXXXX";
  std::string dir_inf = kvt::temp_dir(t1), dir_ch = kvt::temp_dir(t2);
  for (auto& [dir, cfg] : {std::pair<std::string, json>{dir_inf, json::object()},
                           std::pair<std::string, json>{dir_ch, json{{"chunk_target", 23}}}}) {
    IngestOptions o;
    o.format = "dgraph";
    o.codec_config = cfg;
    o.targets = {{dir + "/store", "none"}};
    std::istringstream in(records);
    ingest(in, s, schema_path, o);
  }
  {
    json m_inf = read_manifest(dir_inf + "/store"), m_ch = read_manifest(dir_ch + "/store");
    // chunk_target only when finite (the anchor's shape); edge_props always (store identity)
    json ep = props_codec_name(kPropsCodecDefault);
    CHECK(m_inf["value_codec"] == json({{"props_codec", ep}}), "the unbounded arm's manifest carries no chunk knob: " << m_inf["value_codec"]);
    CHECK(m_ch["value_codec"] == json({{"chunk_target", 23}, {"props_codec", ep}}), "the chunked arm's manifest records the target: " << m_ch["value_codec"]);
  }

  Session sess;
  int errors = 0;
  auto run = [&](const std::string& dir, json steps, const std::string& out) {
    json req = {{"store", {{"dir", dir + "/store"}, {"schema", schema_path}, {"format", "dgraph"}}},
                {"plan", {{"plan", "t"}, {"params", json::object()}, {"steps", steps}, {"output", out}}},
                {"run", json::object()}};
    json r = sess.handle(req);
    if (r.contains("error") && !r["error"].is_null()) { errors++; std::cerr << "ERROR " << r["error"].dump() << "\n"; }
    return r;
  };
  auto N = [](const json& r, size_t i = 0) { return r["steps"][i]["N"].get<int64_t>(); };
  auto S = [](const json& r, size_t i = 0) { return r["steps"][i]["S"].get<int64_t>(); };
  auto P = [](const json& r) { int64_t p = 0; for (auto& st : r["steps"]) p += st["P"].get<int64_t>(); return p; };
  auto wv = [](const json& r) { int64_t b = 0; for (auto& st : r["steps"]) b += st["bytes"]["write_value"].get<int64_t>(); return b; };
  // both arms on the same steps: rows must be identical (order included), S identical unless the
  // case names the chunked arm's extra SEEKs (s_extra); returns (inf, ch)
  auto both = [&](const char* what, json steps, const std::string& out = "out", int64_t s_extra = 0) {
    json a = run(dir_inf, steps, out), b = run(dir_ch, steps, out);
    CHECK(a["rows"] == b["rows"], what << ": rows differ between dgraph-c∞ and the chunked arm: "
                                       << a["rows"].dump().substr(0, 200) << " vs " << b["rows"].dump().substr(0, 200));
    CHECK(S(a) + s_extra == S(b), what << ": S differs " << S(a) << " (+" << s_extra << ") vs " << S(b));
    return std::pair<json, json>{a, b};
  };
  json all;
  for (int i = 1; i <= 60; i++) all.push_back(person(i));

  // --- point reads: N = parts on the chunked arm, 0 on the whole KV ------------------------
  {
    auto [a, b] = both("hub neighbors", json::array({{{"name", "out"}, {"op", "read_neighbors"}, {"srcs", json::array({person(1)})}, {"etype", "knows"}, {"dir", "out"}}}));
    CHECK(a["rows"].size() == 59, "59 neighbours: " << a["rows"].size());
    CHECK(N(a) == 0 && N(b) == 59, "whole: 0 NEXT; 59 one-entry parts: N = 59 (58 steps + the end miss): " << N(a) << " / " << N(b));
  }
  {
    auto [a, b] = both("reverse of one", json::array({{{"name", "out"}, {"op", "read_neighbors"}, {"srcs", json::array({person(2)})}, {"etype", "knows"}, {"dir", "in"}}}));
    CHECK(a["rows"].size() == 1 && N(b) == 0, "a one-entry unit never splits: N 0 on both: " << N(b));
  }
  {
    auto [a, b] = both("index eq", json::array({{{"name", "out"}, {"op", "read_index_eq"}, {"label", "Person"}, {"prop", "firstName"}, {"value", "Bo"}}}));
    CHECK(a["rows"].size() == 60 && N(a) == 0 && N(b) == 7, "60 dense uids at 23 B = 7 parts (9 per part): N " << N(b));
  }
  {
    auto [a, b] = both("enumerate", json::array({{{"name", "out"}, {"op", "read_vertices_by_label"}, {"label", "Person"}}}));
    CHECK(a["rows"].size() == 60 && N(a) == 0 && N(b) == 7, "the type posting: 7 parts: N " << N(b));
  }
  {
    auto [a, b] = both("filter by label index", json::array({{{"name", "out"}, {"op", "read_filter_by_label_index"},
                                                              {"vids", json::array({person(5), person(50), person(99)})}, {"label", "Person"}}}));
    CHECK(a["rows"] == json::array({person(5), person(50)}) && N(b) == 7, "members kept in input order; the whole type posting scanned: N " << N(b));
  }

  // --- the day-token range walk: one landing per token (∞) vs per part (chunked) ----------
  {
    // D0-1 (10 uids: parts 9+1), D0 (40: 9,9,9,9,4), D0+1 (10: 9+1) = 3 tokens / 9 parts
    auto [a, b] = both("range 3 days", json::array({{{"name", "out"}, {"op", "read_index_range"}, {"labels", json::array({"Person"})},
                                                    {"prop", "creationDate"}, {"lo", kMs0 - kDay}, {"hi", kMs0 + 2 * kDay - 1}}}));
    CHECK(a["rows"].size() == 60 && N(a) == 3 && N(b) == 9, "3 token landings vs 9 part landings: N " << N(a) << " / " << N(b));
    auto [c, d] = both("range 1 day", json::array({{{"name", "out"}, {"op", "read_index_range"}, {"labels", json::array({"Person"})},
                                                   {"prop", "creationDate"}, {"lo", kMs0}, {"hi", kMs0 + kDay - 1}}}));
    CHECK(c["rows"].size() == 40 && N(c) == 1 && N(d) == 5, "one token vs its 5 parts: N " << N(c) << " / " << N(d));
    auto [e, f] = both("range buckets", json::array({{{"name", "out"}, {"op", "index_range_buckets"}, {"labels", json::array({"Person"})},
                                                     {"prop", "creationDate"}, {"lo", kMs0 - kDay}, {"hi", kMs0 + 2 * kDay - 1}}}));
    CHECK(e["rows"].size() == 60 && N(f) == 9, "buckets: the same walk: N " << N(f));
  }

  // --- top-k newest-first over the split day --------------------------------------------
  auto topk = [&](const char* what, int k, json hi) {
    json step = {{"name", "out"}, {"op", "read_top_k_by_index"}, {"vids", all}, {"prop", "creationDate"}, {"k", k}};
    if (!hi.is_null()) step["hi"] = hi;
    auto [a, b] = both(what, json::array({step}));
    CHECK(a["rows"].size() == size_t(k), what << ": " << k << " rows: " << a["rows"].size());
    return std::pair<json, json>{a, b};
  };
  {
    auto [a, b] = topk("top 3 (newest day, no boundary)", 3, nullptr);
    CHECK(a["rows"][0]["_vid"] == person(60) && a["rows"][2]["_vid"] == person(58), "60, 59, 58 are the newest");
    CHECK(N(a) == 0 && N(b) == 1, "one landing on D0+1's last key, the k reached there: ∞ 0 NEXT, chunked 1 (part 1 then part 0 of the 9+1 split): " << N(a) << " / " << N(b));
  }
  {
    // boundary day: hi mid-D0 -> only persons with ms < hi (i >= 21); the newest 5 of them are 21..25,
    // spread over parts 2 and 3 -- EVERY part of the boundary token must be read before the exact-ms check
    auto [a, b] = topk("top 5 under a mid-day hi", 5, kMs0 + 20 * 1000 + 500);
    CHECK(a["rows"][0]["_vid"] == person(21) && a["rows"][4]["_vid"] == person(25), "21..25: " << a["rows"].dump().substr(0, 160));
    CHECK(N(a) == 1 && N(b) == 5, "∞: the boundary landing + 1 prev; chunked: 5 parts (4 prevs) + 1: " << N(a) << " / " << N(b));
  }
  {
    // the mid-token trap: D0+1 (10) + D0's parts 4..1 (uids 10..40 = 31) already make 41 candidates,
    // but D0's NEWEST 9 (uids 1..9) sit in part 0 -- the answer needs them
    auto [a, b] = topk("top 41 across the split day", 41, nullptr);
    CHECK(a["rows"][10]["_vid"] == person(1) && a["rows"][40]["_vid"] == person(31), "after D0+1's 10 come persons 1..31: " << a["rows"][10]["_vid"] << " " << a["rows"][40]["_vid"]);
    CHECK(N(b) > N(a), "the chunked walk visits every part: N " << N(a) << " / " << N(b));
  }
  {
    auto [a, b] = topk("top 55 into the oldest day", 55, nullptr);
    CHECK(a["rows"][54]["_vid"] == person(46), "the 55th newest is person 46 (D0-1, ms 46000): " << a["rows"][54]["_vid"]);
  }

  // --- writes (shadow): the same puts, the chunked arm rewrites ONE part -----------------
  {
    auto [a, b] = both("insert_edge + read", json::array({
        {{"name", "w"}, {"op", "insert_edge"}, {"etype", "knows"}, {"src", person(1)}, {"dst", person(61)}, {"props", {{"creationDate", kMs0}}}},
        {{"name", "out"}, {"op", "read_neighbors"}, {"srcs", json::array({person(1)})}, {"etype", "knows"}, {"dir", "out"}}}),
        "out", 1);   // +1 SEEK: the hub's split posting is located by a look at its key, then a reverse seek to the part
    CHECK(a["rows"].size() == 60 && P(a) == 2 && P(b) == 2, "60 after the insert; 2 puts (out + reverse) on both: " << P(a) << " / " << P(b));
    CHECK(wv(b) < wv(a), "the chunked arm rewrites one part, not the whole hub posting: " << wv(b) << " < " << wv(a));
    CHECK(N(a) == 0 && N(b) == 0, "the write lands on its part with no NEXT over the hub's 59 parts: N "
          << N(a) << " / " << N(b));
  }
  {
    auto [a, b] = both("delete_edge + read", json::array({
        {{"name", "w"}, {"op", "delete_edge"}, {"etype", "knows"}, {"src", person(1)}, {"dst", person(30)}},
        {{"name", "out"}, {"op", "read_neighbors"}, {"srcs", json::array({person(1)})}, {"etype", "knows"}, {"dir", "out"}}}),
        "out", 1);   // +1 SEEK: the hub's split posting, as insert_edge
    CHECK(a["rows"].size() == 58 && P(a) == 2 && P(b) == 2, "58 after the delete; 2 puts on both: " << P(a) << " / " << P(b));
    CHECK(wv(b) < wv(a), "one part rewritten minus the entry: " << wv(b) << " < " << wv(a));
    CHECK(N(a) == 0 && N(b) == 0, "the delete reads one part: N " << N(a) << " / " << N(b));
  }
  {
    // dgraph's blind re-put, kept on the chunked arm (the fairness ruling): an edge that never
    // existed still costs 2 tombstone puts on BOTH arms
    auto [a, b] = both("delete of an absent edge", json::array({
        {{"name", "out"}, {"op", "delete_edge"}, {"etype", "knows"}, {"src", person(3)}, {"dst", person(5)}}}));
    CHECK(P(a) == 2 && P(b) == 2 && wv(a) == wv(b), "blind re-put on both arms: P " << P(a) << " / " << P(b) << ", bytes " << wv(a) << " / " << wv(b));
  }
  {
    // update_edge_prop: the entry's facet replaced in place -- the whole hub posting (∞) or the ONE
    // part holding the entry (chunked); both sides of the edge (knows declares @reverse)
    int64_t nd = kMs0 + 7 * kDay;
    auto [a, b] = both("update_edge_prop + read", json::array({
        {{"name", "w"}, {"op", "update_edge_prop"}, {"etype", "knows"}, {"src", person(1)}, {"dst", person(10)},
         {"props", {{"creationDate", nd}}}},
        {{"name", "out"}, {"op", "read_neighbors"}, {"srcs", json::array({person(1)})}, {"etype", "knows"}, {"dir", "out"},
         {"edge_props", json::array({"creationDate"})}}}), "out", 1);   // +1 SEEK: the hub's split posting
    int hits = 0;
    for (auto& r : a["rows"]) hits += r["creationDate"] == nd && r["dst"] == person(10);
    CHECK(a["rows"].size() == 59 && hits == 1, "59 rows, the updated one carries the new date: " << hits);
    CHECK(P(a) == 2 && P(b) == 2, "2 puts (out + reverse) on both arms: " << P(a) << " / " << P(b));
    CHECK(wv(b) < wv(a), "the chunked arm rewrites the one part holding the entry: " << wv(b) << " < " << wv(a));
    auto [c, d] = both("update_edge_prop, reverse side", json::array({
        {{"name", "w"}, {"op", "update_edge_prop"}, {"etype", "knows"}, {"src", person(1)}, {"dst", person(10)},
         {"props", {{"creationDate", nd}}}},
        {{"name", "out"}, {"op", "read_neighbors"}, {"srcs", json::array({person(10)})}, {"etype", "knows"}, {"dir", "in"},
         {"edge_props", json::array({"creationDate"})}}}), "out", 1);   // the same write: +1 SEEK on the hub's posting
    CHECK(c["rows"].size() == 1 && c["rows"][0]["creationDate"] == nd, "the reverse copy carries it too: " << c["rows"].dump());
    // an absent edge: nothing to rewrite, no put (unlike delete_edge's blind re-put)
    auto [e, f] = both("update_edge_prop of an absent edge", json::array({
        {{"name", "out"}, {"op", "update_edge_prop"}, {"etype", "knows"}, {"src", person(3)}, {"dst", person(5)},
         {"props", {{"creationDate", nd}}}}}));
    CHECK(P(e) == 0 && P(f) == 0, "absent edge: 0 puts on both arms: " << P(e) << " / " << P(f));
  }
  {
    // the multi-column update: one blind put per column, the indexed one moves its token
    auto [a, b] = both("multi update_prop + read", json::array({
        {{"name", "w"}, {"op", "update_prop"}, {"vid", person(7)}, {"label", "Person"},
         {"props", {{"firstName", "Zed"}, {"lastName", "Q"}, {"gender", "female"}}}},
        {{"name", "t"}, {"op", "read_index_eq"}, {"label", "Person"}, {"prop", "firstName"}, {"value", "Zed"}},
        {{"name", "out"}, {"op", "read_vertex_props"}, {"vids", {{"ref", "t"}}}, {"props", json::array({"firstName", "lastName", "gender"})},
         {"label", json::array({"Person"})}}}), "out", 1);   // +1 SEEK: the old token's posting (firstName Bo, 60 uids) is split
    CHECK(a["rows"].size() == 1 && a["rows"][0]["lastName"] == "Q" && a["rows"][0]["gender"] == "female",
          "the vertex is under the new token with every changed column: " << a["rows"].dump());
    CHECK(a["steps"][0]["P"] == b["steps"][0]["P"] && a["steps"][0]["G"] == 1,
          "the same puts on both arms, 1 GET (the one indexed column's old value): P " << a["steps"][0]["P"] << " G " << a["steps"][0]["G"]);
  }
  {
    // The chunked arm reads person 2's OWN adjacency unit once (unit::adjacency_tomb_read): that read names the
    // incident edges AND the parts to empty, as dgraph-∞'s one read names the edges before its blind tomb. (Until
    // the chunked arm read the unit a second time to name its parts: +1 SEEK.) s_extra 4: four of the
    // delete's unit writes land on split units, each located by a look at its key and a reverse seek: the Person type
    // posting, firstName Bo, creationDate's day D0 (40 uids) and the hub's knows posting.
    auto [a, b] = both("delete_vertex + read", json::array({
        {{"name", "w"}, {"op", "delete_vertex"}, {"vid", person(2)}, {"label", "Person"}, {"with_edges", true}},
        {{"name", "out"}, {"op", "read_neighbors"}, {"srcs", json::array({person(1)})}, {"etype", "knows"}, {"dir", "out"}}}), "out", 4);
    CHECK(a["rows"].size() == 58 && P(a) == P(b), "person 2 gone from the hub; the same put count: " << P(a) << " / " << P(b));
    CHECK(wv(b) < wv(a), "the hub's rewrite is one part: " << wv(b) << " < " << wv(a));
  }
  CHECK(errors == 0, errors << " request error(s)");

  // --- the part layout the N expectations above rest on (the session must be closed first) -
  sess = Session();
  {
    StoreOptions so;
    auto store = Store::open_read_write(dir_ch + "/store/db", so, false);
    ShadowOverlay ov;
    CountingStore st(*store, &ov);
    auto parts = [&](const std::string& key) { auto p = unit::rmw_read(st, key); return p ? p->size() : 0; };
    CHECK(parts(dgraph::uid_key("knows", dgraph::kData, tag | 1)) == 59, "hub adjacency: 59 parts");
    CHECK(parts(dgraph::term_key("firstName", "Bo")) == 7, "firstName=Bo: 7 parts");
    CHECK(parts(dgraph::term_key("dgraph.type", "Person")) == 7, "type posting: 7 parts");
    CHECK(parts(dgraph::term_key("creationDate", dgraph::day_token(kMs0))) == 5, "D0's token: 5 parts");
    CHECK(parts(dgraph::term_key("creationDate", dgraph::day_token(kMs0 - kDay))) == 2, "D0-1's token: 2 parts");
  }
  if (fails == 0) std::cout << "test_dgraph_chunked OK\n";
  return fails == 0 ? 0 : 1;
}

}  // namespace
