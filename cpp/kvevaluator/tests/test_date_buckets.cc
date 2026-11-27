// The date index at DAY granularity vs FULL precision: the same 60-person stream ingested twice and driven
// through the real Session path, on meta at its All-Condensed corner (tests/fixtures/meta_corner_dgneb.json, index
// term day | exact) with the posting index at a 23 B part target (make_index_backend; dgneb, retired, gave
// the same counts on every assertion below). A day index answers a range with the
// boundary-day SUPERSET (the plan's precise filter trims it: the ic3 shape) and a top-k with
// the whole k-th day resolved to exact dates by one row GET per hit; both must return exactly
// the full-precision rows. The concise win: one term per day (N = #days) instead of one per
// timestamp (N = #timestamps), and a fraction of the index keys.
#include <algorithm>
#include <cstdlib>
#include <iostream>
#include <map>
#include <sstream>
#include <string>
#include <unistd.h>

#include "cli/session.h"
#include "index/date_buckets.h"
#include "index/sorted_index.h"
#include "meta_corners.h"
#include "ingest/ingest.h"
#include "schema/schema.h"
#include "store/rocksdb_store.h"
#include "util/vid.h"
#include "repo_paths.h"
#include "harness.h"

namespace {

using namespace evalcpp;


static constexpr int64_t kMs0 = 15000 * kDayMs;   // day D0's first ms

TEST_CASE(test_date_buckets) {
  // --- the scheme: order-preserving, floor-correct, fixed width ------------------------------
  CHECK(date_bucket_of(kMs0, DateGranularity::day) == 15000 && date_bucket_of(kMs0 - 1, DateGranularity::day) == 14999, "day edge");
  CHECK(date_bucket_of(-1, DateGranularity::day) == -1 && date_bucket_of(-kDayMs, DateGranularity::day) == -1 &&
        date_bucket_of(-kDayMs - 1, DateGranularity::day) == -2, "negative ms floor like Python");
  CHECK(date_bucket_of(12345, DateGranularity::full) == 12345, "full = the ms");
  CHECK(IndexKeys::sorted_bytes(date_bucket_of(-1, DateGranularity::day)) < IndexKeys::sorted_bytes(date_bucket_of(0, DateGranularity::day)) &&
        IndexKeys::sorted_bytes(14999) < IndexKeys::sorted_bytes(15000) && IndexKeys::sorted_bytes(15000).size() == 8, "bucket bytes order-preserving, 8 B");
  {
    auto [a, b] = date_bucket_window(kMs0 + 5, kMs0 + 2 * kDayMs - 7, DateGranularity::day);
    CHECK(a == 15000 && b == 15001, "a mid-day window covers its two boundary days");
    auto [c, d] = date_bucket_window(kMs0 + 5, kMs0 + 5, DateGranularity::day);
    CHECK(c > d, "an empty window covers no bucket");
    auto [e, f] = date_bucket_window(10, 20, DateGranularity::full);
    CHECK(e == 10 && f == 19, "full: [lo, hi-1]");
  }

  // --- two stores from one stream: persons 1..40 on D0 (ms DEscending with uid: 1 = newest),
  //     41..50 on D0-1, 51..60 on D0+1 ------------------------------------------------------
  std::string schema_path = ldbc_schema_dir() + "/schema.json";
  Schema s = Schema::load(schema_path);
  uint64_t tag = uint64_t(s.tag_of_label("Person")) << kVidShift;
  auto person = [&](int i) { return hex16(tag | uint64_t(i)); };
  auto ms_of = [&](int i) -> int64_t {
    if (i <= 40) return kMs0 + int64_t(41 - i) * 1000;
    if (i <= 50) return kMs0 - kDayMs + int64_t(i) * 1000;
    return kMs0 + kDayMs + int64_t(i) * 1000;
  };
  std::string records;
  for (int i = 1; i <= 60; i++)
    records += "[\"v\",\"" + person(i) + "\",\"Person\",{\"id\":" + std::to_string(i) + ",\"firstName\":\"Bo\",\"lastName\":\"L" +
               std::to_string(i) + "\",\"gender\":\"male\",\"birthday\":1000,\"creationDate\":" + std::to_string(ms_of(i)) + "}]\n";
  // a second label with INTERLEAVED days and ms TIES: posts 1..6 on D0+1 (ms = day start + 7000,
  // i.e. older than every D0+1 person), 7..12 on D0 at ms kMs0 + 20000 (a tie with person 21)
  uint64_t ptag = uint64_t(s.tag_of_label("Post")) << kVidShift;
  auto post = [&](int i) { return hex16(ptag | uint64_t(i)); };
  auto post_ms = [&](int i) -> int64_t { return i <= 6 ? kMs0 + kDayMs + 7000 : kMs0 + 20000; };
  for (int i = 1; i <= 12; i++)
    records += "[\"v\",\"" + post(i) + "\",\"Post\",{\"id\":" + std::to_string(i) + ",\"creationDate\":" + std::to_string(post_ms(i)) +
               ",\"browserUsed\":\"x\",\"locationIP\":\"1\",\"content\":\"c" + std::to_string(i) + "\",\"length\":1}]\n";
  // the format under test, at a 23 B part target (a day's posting splits into parts, 9 uids each)
  struct Arm { std::string name, format; json full, day; json::json_pointer term_at; std::string full_term, day_term; };
  std::vector<Arm> arms = {
      {"meta", "meta", evalcpp::test::meta_corner("dgneb", {{"index", {{"term", "exact"}, {"split", 23}}}}),
       evalcpp::test::meta_corner("dgneb", {{"index", {{"term", "day"}, {"split", 23}}}}),
       json::json_pointer("/index/term"), "exact", "day"}};
  for (const Arm& arm : arms) {
    std::cerr << "== " << arm.name << "\n";
    auto tmpdir = [&](const std::string& tag) {
      std::string t = "/tmp/evalcpp-date-buckets-" + arm.name + "-" + tag + "-XXXXXX";
      std::vector<char> b(t.begin(), t.end()); b.push_back('\0');
      return std::string(kvt::temp_dir(b.data()));
    };
    std::string dir_full = tmpdir("full"), dir_day = tmpdir("day");
    for (auto& [dir, cfg] : {std::pair<std::string, json>{dir_full, arm.full}, std::pair<std::string, json>{dir_day, arm.day}}) {
      IngestOptions o;
      o.format = arm.format;
      o.codec_config = cfg;
      o.targets = {{dir + "/store", "none"}};
      std::istringstream in(records);
      ingest(in, s, schema_path, o);
    }
    CHECK(read_manifest(dir_day + "/store")["value_codec"][arm.term_at] == arm.day_term &&
          read_manifest(dir_full + "/store")["value_codec"][arm.term_at] == arm.full_term, "the manifests record the granularity");

    Session sess;
    int errors = 0;
    auto run = [&](const std::string& dir, json steps, const std::string& out) {
      json req = {{"store", {{"dir", dir + "/store"}, {"schema", schema_path}, {"format", arm.format}}},
                  {"plan", {{"plan", "t"}, {"params", json::object()}, {"steps", steps}, {"output", out}}},
                  {"run", json::object()}};
      json r = sess.handle(req);
      if (r.contains("error") && !r["error"].is_null()) { errors++; std::cerr << "ERROR " << r["error"].dump() << "\n"; }
      return r;
    };
    auto N = [](const json& r, size_t i = 0) { return r["steps"][i]["N"].get<int64_t>(); };
    auto G = [](const json& r, size_t i = 0) { return r["steps"][i]["G"].get<int64_t>(); };
    auto P = [](const json& r) { int64_t p = 0; for (auto& st : r["steps"]) p += st["P"].get<int64_t>(); return p; };
    // rows identical; as_set: identical as a SET (a range answer's order is the index's -- by
    // timestamp under full, by uid within a day bucket -- and kvbench's result_hash normalizes it)
    auto both = [&](const char* what, json steps, const std::string& out = "out", bool as_set = false) {
      json a = run(dir_full, steps, out), b = run(dir_day, steps, out);
      json ra = a["rows"], rb = b["rows"];
      if (as_set) {
        std::vector<std::string> sa, sb;
        for (auto& v : ra) sa.push_back(v.dump());
        for (auto& v : rb) sb.push_back(v.dump());
        std::sort(sa.begin(), sa.end());
        std::sort(sb.begin(), sb.end());
        ra = sa; rb = sb;
      }
      CHECK(ra == rb, what << ": rows differ between full and day: " << ra.dump().substr(0, 200) << " vs " << rb.dump().substr(0, 200));
      return std::pair<json, json>{a, b};
    };
    json all;
    for (int i = 1; i <= 60; i++) all.push_back(person(i));
    json labels = json::array({"Person"});

    // --- the coarse range: a superset on day, exact once the plan's precise filter runs ---------
    int64_t lo = kMs0 + 10 * 1000 + 500, hi = kMs0 + kDayMs + 55 * 1000 + 500;   // mid-D0 .. mid-(D0+1)
    {
      json step = {{"name", "out"}, {"op", "read_index_range"}, {"labels", labels}, {"prop", "creationDate"}, {"lo", lo}, {"hi", hi}};
      json a = run(dir_full, json::array({step}), "out"), b = run(dir_day, json::array({step}), "out");
      // exact: D0 persons with (41-i)*1000 >= 10500 -> i <= 30 (30 of them), D0+1 with i*1000 < 55500 -> 51..55 (5)
      CHECK(a["rows"].size() == 35, "full-precision range: 35 exact vids: " << a["rows"].size());
      CHECK(b["rows"].size() == 50, "day range: the two boundary days whole = 40 + 10 vids: " << b["rows"].size());
      CHECK(N(a) == 35 && N(b) == 7, "N: one landing per timestamp (35) vs per day PART (D0 5 parts + D0+1 2): " << N(a) << " / " << N(b));
      std::map<std::string, bool> in_day;
      for (auto& v : b["rows"]) in_day[v.get<std::string>()] = true;
      bool superset = true;
      for (auto& v : a["rows"]) superset = superset && in_day[v.get<std::string>()];
      CHECK(superset, "the day answer contains every exact vid");
    }
    {
      // the ic3 shape: range -> read the date -> the precise filter -> the vids
      json steps = json::array({
          {{"name", "w"}, {"op", "read_index_range"}, {"labels", labels}, {"prop", "creationDate"}, {"lo", lo}, {"hi", hi}},
          {{"name", "d"}, {"op", "read_vertex_props"}, {"vids", {{"ref", "w"}}}, {"props", json::array({"creationDate"})}, {"label", labels}},
          {{"name", "f"}, {"op", "filter"}, {"rows", {{"ref", "d"}}},
           {"pred", {{"and", json::array({{{"le", json::array({lo, {{"field", "creationDate"}}})}}, {{"lt", json::array({{{"field", "creationDate"}}, hi})}}})}}}},
          {{"name", "out"}, {"op", "pluck"}, {"rows", {{"ref", "f"}}}, {"field", "_vid"}}});
      auto [a, b] = both("ic3-shaped range + precise filter", steps, "out", /*as_set=*/true);
      CHECK(a["rows"].size() == 35, "35 exact vids on both: " << a["rows"].size() << " / " << b["rows"].size());
      CHECK(G(b, 1) == 50 && G(a, 1) == 35, "the precise read costs one GET per superset vid (50) vs per exact vid (35): " << G(a, 1) << " / " << G(b, 1));
    }
    {
      json step = {{"name", "out"}, {"op", "index_range_buckets"}, {"labels", labels}, {"prop", "creationDate"}, {"lo", lo}, {"hi", hi}};
      json a = run(dir_full, json::array({step}), "out"), b = run(dir_day, json::array({step}), "out");
      CHECK(a["rows"].size() == 35 && b["rows"].size() == 50, "buckets: exact vids (full) vs the boundary days (day)");
      CHECK(b["rows"][0]["_bucket"] == 15000 && b["rows"][49]["_bucket"] == 15001 && a["rows"][0]["_bucket"] == 15000, "_bucket = the day on both arms");
    }

    // --- top-k: identical rows; day resolves exactness by one GET per collected hit ------------
    auto topk = [&](const char* what, int k, json hi_v, json vids) {
      json step = {{"name", "out"}, {"op", "read_top_k_by_index"}, {"labels", labels}, {"prop", "creationDate"}, {"k", k}};
      if (!hi_v.is_null()) step["hi"] = hi_v;
      if (!vids.is_null()) step["vids"] = vids;
      auto [a, b] = both(what, json::array({step}));
      CHECK(a["rows"].size() == size_t(k), what << ": " << k << " rows: " << a["rows"].size());
      return std::pair<json, json>{a, b};
    };
    {
      auto [a, b] = topk("top 3 newest", 3, nullptr, all);
      CHECK(a["rows"][0]["_vid"] == person(60) && a["rows"][0]["creationDate"] == ms_of(60), "60 first, with its exact ms");
      CHECK(G(a) == 0 && G(b) == 10, "day: the whole newest day (10 persons) resolved, one GET each: " << G(b));
      CHECK(N(a) == 3 && N(b) == 2, "N: 3 timestamps vs the newest day's 2 parts: " << N(a) << " / " << N(b));
    }
    {
      // hi mid-D0: exact survivors i >= 21 (20 of them) -> top 5 = 21..25; the day arm collects the
      // whole day (40 candidates), drops the 20 past hi via the resolver, and must NOT continue into
      // D0-1 (the 20 survivors already exceed k)
      auto [a, b] = topk("top 5 under a mid-day hi", 5, kMs0 + 20 * 1000 + 500, all);
      CHECK(a["rows"][0]["_vid"] == person(21) && a["rows"][4]["_vid"] == person(25), "21..25");
      CHECK(G(b) == 40 && N(b) == 5, "day: 40 GETs (the boundary day), its 5 parts walked: G " << G(b) << " N " << N(b));
    }
    {
      // hi mid-D0 with only 3 survivors (i >= 38) and k 5: the walk must go on into D0-1
      auto [a, b] = topk("top 5 past a thin boundary day", 5, kMs0 + 3 * 1000 + 500, all);
      CHECK(a["rows"][0]["_vid"] == person(38) && a["rows"][2]["_vid"] == person(40) && a["rows"][3]["_vid"] == person(50), "38, 39, 40 then D0-1's newest (50)");
      CHECK(G(b) == 50, "day: 40 (boundary) + 10 (D0-1) GETs: " << G(b));
    }
    {
      auto [a, b] = topk("top 41 across the split day", 41, nullptr, all);
      CHECK(a["rows"][40]["_vid"] == person(31), "the 41st is person 31");
    }
    {
      json some = json::array({person(5), person(45), person(55), person(7)});
      auto [a, b] = topk("top 2 within candidates", 2, nullptr, some);
      CHECK(a["rows"][0]["_vid"] == person(55) && a["rows"][1]["_vid"] == person(5), "55 (D0+1) then 5 (D0's newest candidate)");
      CHECK(G(b) == 3, "day: the candidates of D0+1 (55) and of D0 (5, 7) resolved: " << G(b));
    }
    {
      // props served from the resolver's blob: no second GET on day
      json step = {{"name", "out"}, {"op", "read_top_k_by_index"}, {"labels", labels}, {"prop", "creationDate"}, {"k", 2}, {"vids", all}, {"props", json::array({"lastName"})}};
      auto [a, b] = both("top 2 with props", json::array({step}));
      CHECK(a["rows"][0]["lastName"] == "L60", "the winner's prop");
      CHECK(G(a) == 2 && G(b) == 10, "full: one GET per winner; day: one per resolved hit, none extra: " << G(a) << " / " << G(b));
    }

    {
      // a boundary day with ZERO survivors: hi = kMs0 + 1000 -> every D0 person (ms >= kMs0 + 1000)
      // is dropped by the resolver; hits stay empty and the walk must continue into D0-1
      auto [a, b] = topk("top 2 past a zero-survivor boundary day", 2, kMs0 + 1000, all);
      CHECK(a["rows"][0]["_vid"] == person(50) && a["rows"][1]["_vid"] == person(49), "50, 49 (D0-1's newest)");
      CHECK(G(b) == 50, "day: 40 dropped + 10 resolved GETs: " << G(b));
    }
    {
      // hi exactly on a day boundary: nothing of D0+1 qualifies, D0 is fully in range (no drops)
      auto [a, b] = topk("top 3 with hi on a day start", 3, kMs0 + kDayMs, all);
      CHECK(a["rows"][0]["_vid"] == person(1) && a["rows"][2]["_vid"] == person(3), "1, 2, 3 (D0's newest)");
      CHECK(G(b) == 40, "day: only D0 resolved: " << G(b));
    }
    {
      // two labels, interleaved days: posts 1..6 are D0+1's OLDEST, so top 12 newest = persons
      // 51..60, then posts 1..6 (older ms, D0+1), then D0's newest 1..; both walkers' k-th day is
      // consumed whole
      json both_labels = json::array({"Person", "Post"});
      json step = {{"name", "out"}, {"op", "read_top_k_by_index"}, {"labels", both_labels}, {"prop", "creationDate"}, {"k", 17}};
      auto [a, b] = both("top 17 over Person + Post", json::array({step}));
      CHECK(a["rows"].size() == 17 && a["rows"][10]["_vid"] == post(1) && a["rows"][15]["_vid"] == post(6) && a["rows"][16]["_vid"] == person(1),
            "persons 60..51, posts 1..6, then person 1: " << a["rows"].dump().substr(0, 300));
    }
    {
      // ms ties across labels: person 21 (kMs0 + 20000) and posts 7..12 share a timestamp; the
      // re-rank breaks ties by id (asc by default, desc on request) identically on both arms
      json both_labels = json::array({"Person", "Post"});
      json some = json::array({person(21), person(22), post(7), post(8), post(12)});
      json step = {{"name", "out"}, {"op", "read_top_k_by_index"}, {"labels", both_labels}, {"prop", "creationDate"}, {"k", 3}, {"vids", some}, {"hi", kMs0 + 20001}};
      auto [a, b] = both("ties by id asc", json::array({step}));
      CHECK(a["rows"][0]["id"] == 7 && a["rows"][1]["id"] == 8 && a["rows"][2]["id"] == 12, "ids 7, 8, 12 at the tied ms (asc), person 21 (id 21) after them");
      step["id_order"] = "desc";
      auto [c, d] = both("ties by id desc", json::array({step}));
      CHECK(c["rows"][0]["id"] == 21 && c["rows"][1]["id"] == 12 && c["rows"][2]["id"] == 8, "ids 21, 12, 8 (desc)");
    }
    {
      // the buckets op's contract is dgraph's: hi INCLUSIVE at day granularity. The plan passes
      // end - 1; with startDate = D0 + 1 ms and one day, end = (D0+1) + 1 ms, end - 1 = the start
      // of D0+1 -- day D0+1 must be walked (persons 51..60 are boundary candidates)
      json step = {{"name", "out"}, {"op", "index_range_buckets"}, {"labels", labels}, {"prop", "creationDate"}, {"lo", kMs0 + 1}, {"hi", kMs0 + kDayMs}};
      json a = run(dir_full, json::array({step}), "out"), b = run(dir_day, json::array({step}), "out");
      CHECK(b["rows"].size() == 50 && b["rows"][49]["_bucket"] == 15001, "day: both days walked, 50 rows, the last in bucket D0+1: " << b["rows"].size());
      CHECK(a["rows"].size() == 40, "full: the 40 D0 persons (ms in [D0 + 1, D0+1 start]; no D0+1 person is at the day start): " << a["rows"].size());
    }
    {
      // an equality on a bucketed date column is refused (it would answer with the whole day)
      json step = {{"name", "out"}, {"op", "read_index_eq"}, {"label", "Person"}, {"prop", "creationDate"}, {"value", ms_of(5)}};
      json a = run(dir_full, json::array({step}), "out");
      CHECK(a["rows"].size() == 1, "full: an exact term");
      json req = {{"store", {{"dir", dir_day + "/store"}, {"schema", schema_path}, {"format", arm.format}}},
                  {"plan", {{"plan", "t"}, {"params", json::object()}, {"steps", json::array({step})}, {"output", "out"}}}, {"run", json::object()}};
      json r = sess.handle(req);
      CHECK(r.contains("error") && !r["error"].is_null(), "day: read_index_eq on a date column is refused");
    }

    // --- writes: the index moves the same way; a same-day date update writes no index put ------
    {
      auto [a, b] = both("insert then top", json::array({
          {{"name", "w"}, {"op", "insert_vertex"}, {"vid", person(61)}, {"label", "Person"}, {"props", {{"id", 61}, {"firstName", "Bo"}, {"creationDate", kMs0 + kDayMs + 61 * 1000}}}},
          {{"name", "out"}, {"op", "read_top_k_by_index"}, {"labels", labels}, {"prop", "creationDate"}, {"k", 1}}}));
      CHECK(a["rows"][0]["_vid"] == person(61) && P(a) == P(b), "the new newest on both; same puts: " << P(a) << " / " << P(b));
    }
    {
      json up = {{"name", "w"}, {"op", "update_prop"}, {"vid", person(30)}, {"label", "Person"}, {"prop", "creationDate"}, {"value", kMs0 + 5}};
      json a = run(dir_full, json::array({up}), "w"), b = run(dir_day, json::array({up}), "w");
      CHECK(P(a) == 3 && P(b) == 1, "a same-day date change: full moves the term (row + 2 index puts), day rewrites the row only: " << P(a) << " / " << P(b));
      json up2 = {{"name", "w"}, {"op", "update_prop"}, {"vid", person(30)}, {"label", "Person"}, {"prop", "creationDate"}, {"value", kMs0 - 5}};
      json c = run(dir_full, json::array({up2}), "w"), d = run(dir_day, json::array({up2}), "w");
      CHECK(P(c) == 3 && P(d) == 3, "a cross-day change moves the term on both: " << P(c) << " / " << P(d));
    }
    CHECK(errors == 0, errors << " request error(s)");

    // --- the census: one key per day vs one per timestamp ------------------------------------
    sess = Session();
    auto count_index_keys = [&](const std::string& dir, int index_id) {
      StoreOptions so;
      auto store = Store::open_read_write(dir + "/store/db", so, false);
      std::string prefix = IndexKeys::prefix(index_id);
      std::unique_ptr<rocksdb::Iterator> it(store->db()->NewIterator(rocksdb::ReadOptions()));
      size_t n = 0;
      for (it->Seek(prefix); it->Valid() && it->key().starts_with(prefix); it->Next()) n++;
      return n;
    };
    int date_id = s.index_decl("Person", "creationDate").id, post_id = s.index_decl("Post", "creationDate").id;
    CHECK(count_index_keys(dir_full, date_id) == 60 && count_index_keys(dir_day, date_id) == 3 + 6,
          "Person.creationDate: 60 timestamp keys vs 3 days in 9 part keys (D0-1 2, D0 5, D0+1 2): " << count_index_keys(dir_full, date_id) << " / " << count_index_keys(dir_day, date_id));
    CHECK(count_index_keys(dir_full, post_id) == 2 && count_index_keys(dir_day, post_id) == 2,
          "Post.creationDate: 2 timestamps = 2 days (one term each, no split): " << count_index_keys(dir_full, post_id) << " / " << count_index_keys(dir_day, post_id));
  }
  if (fails == 0) std::cout << "test_date_buckets OK\n";
  return fails == 0 ? 0 : 1;
}

}  // namespace
