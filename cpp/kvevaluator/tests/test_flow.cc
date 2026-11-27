// Unit tests: the format-neutral flow ops that BI added (left_join, group_agg), against the Python oracle's
// documented semantics (formats/base.py): unmatched rows keep defaults; groups in first-seen order; sums as
// Python sums (bool = 0 / 1, int + float = float); min / max by the neutral compare.
#include <map>
#include <algorithm>
#include <numeric>
#include <iostream>

#include "ir/plan.h"
#include "ops/flow.h"
#include "util/vid.h"
#include "value/value.h"
#include "harness.h"

namespace {

using namespace evalcpp;


static Value rows(std::initializer_list<RowFields> rs) {
  ValueList out;
  for (auto& r : rs) out.push_back(make_row(r));
  return Value::list(std::move(out));
}

TEST_CASE(test_flow) {
  // --- left_join: matched rows merge the right fields (right_on dropped); unmatched get the default's
  Step s;
  s.op = "left_join";
  Args a;
  a.set("left", rows({{{"p", Value("a")}, {"x", Value(int64_t(1))}}, {{"p", Value("b")}, {"x", Value(int64_t(2))}}}));
  a.set("right", rows({{{"_vid", Value("a")}, {"n", Value(int64_t(7))}}}));
  a.set("left_on", Value("p"));
  a.set("default", make_row({{"n", Value(int64_t(0))}}));
  Value lj = run_flow_op(s, a);
  CHECK(lj.as_list().size() == 2);
  CHECK(lj.as_list()[0].field("n").as_int() == 7);
  CHECK(lj.as_list()[1].field("n").as_int() == 0);
  CHECK(lj.as_list()[0].find("_vid") == nullptr);          // right_on is not copied
  CHECK(lj.as_list()[1].field("x").as_int() == 2);         // the left fields stay

  // --- group_sum: sums per group (a bool as 0 / 1); an int64 overflow throws, as group_agg's sum does
  {
    Step gs;
    gs.op = "group_sum";
    Args c;
    c.set("rows", rows({{{"k", Value("a")}, {"v", Value(int64_t(5))}}, {{"k", Value("a")}, {"v", Value(true)}},
                        {{"k", Value("b")}, {"v", Value(int64_t(2))}}}));
    c.set("by", Value("k"));
    c.set("field", Value("v"));
    Value sums = run_flow_op(gs, c);
    CHECK(sums.as_list().size() == 2 && sums.as_list()[0].field("sum").as_int() == 6 && sums.as_list()[1].field("sum").as_int() == 2);
    Args big;
    big.set("rows", rows({{{"k", Value("a")}, {"v", Value(INT64_MAX)}}, {{"k", Value("a")}, {"v", Value(int64_t(1))}}}));
    big.set("by", Value("k"));
    big.set("field", Value("v"));
    bool threw = false;
    try { run_flow_op(gs, big); } catch (const std::runtime_error&) { threw = true; }
    CHECK(threw);
  }

  // --- group_agg: two keys, count + sum + min + max; first-seen group order; a bool sums as 1
  Step g;
  g.op = "group_agg";
  Args b;
  b.set("rows", rows({{{"y", Value(int64_t(2011))}, {"c", Value(true)}, {"len", Value(int64_t(30))}},
                      {{"y", Value(int64_t(2010))}, {"c", Value(false)}, {"len", Value(int64_t(5))}},
                      {{"y", Value(int64_t(2011))}, {"c", Value(true)}, {"len", Value(int64_t(90))}},
                      {{"y", Value(int64_t(2011))}, {"c", Value(false)}, {"len", Value(int64_t(1))}}}));
  b.set("by", Value::list({Value("y"), Value("c")}));
  b.set("aggs", make_row({{"n", Value::list({Value("count"), Value()})},
                          {"total", Value::list({Value("sum"), Value("len")})},
                          {"lo", Value::list({Value("min"), Value("len")})},
                          {"hi", Value::list({Value("max"), Value("len")})},
                          {"flags", Value::list({Value("sum"), Value("c")})}}));
  Value ga = run_flow_op(g, b);
  const ValueList& out = ga.as_list();
  CHECK(out.size() == 3);
  CHECK(out[0].field("y").as_int() == 2011 && out[0].field("c").as_bool() == true);
  CHECK(out[0].field("n").as_int() == 2 && out[0].field("total").as_int() == 120);
  CHECK(out[0].field("lo").as_int() == 30 && out[0].field("hi").as_int() == 90);
  CHECK(out[0].field("flags").as_int() == 2);
  CHECK(out[1].field("y").as_int() == 2010 && out[1].field("n").as_int() == 1 && out[1].field("flags").as_int() == 0);
  CHECK(out[2].field("y").as_int() == 2011 && out[2].field("c").as_bool() == false && out[2].field("total").as_int() == 1);
  // the row's field order: the by fields, then the aggregates in aggs order
  CHECK(out[0].as_row()[0].first == "y" && out[0].as_row()[2].first == "n" && out[0].as_row()[6].first == "flags");

  // --- left_join edge cases: an empty left; duplicate right keys (the last wins, as Python's dict); an unmatched
  // row with no default keeps its own fields only; "default": null is no default (Python: `default or {}`); a left
  // row without the key throws (Python's KeyError)
  {
    Args e;
    e.set("left", Value::list(ValueList{}));
    e.set("right", rows({{{"_vid", Value("a")}, {"n", Value(int64_t(7))}}}));
    e.set("left_on", Value("p"));
    CHECK(run_flow_op(s, e).as_list().empty());
    Args d;
    d.set("left", rows({{{"p", Value("a")}}, {{"p", Value("z")}}}));
    d.set("right", rows({{{"_vid", Value("a")}, {"n", Value(int64_t(1))}}, {{"_vid", Value("a")}, {"n", Value(int64_t(2))}}}));
    d.set("left_on", Value("p"));
    Value r = run_flow_op(s, d);
    CHECK(r.as_list().size() == 2 && r.as_list()[0].field("n").as_int() == 2);   // the last duplicate wins
    CHECK(r.as_list()[1].find("n") == nullptr && r.as_list()[1].field("p").as_str() == "z");
    d.set("default", Value());                                                    // null: as if absent
    r = run_flow_op(s, d);
    CHECK(r.as_list()[1].find("n") == nullptr && r.as_list()[1].field("p").as_str() == "z");
    d.set("default", make_row({{"n", Value(int64_t(0))}}));
    CHECK(run_flow_op(s, d).as_list()[1].field("n").as_int() == 0);
    Args m;
    m.set("left", rows({{{"q", Value("a")}}}));
    m.set("right", Value::list(ValueList{}));
    m.set("left_on", Value("p"));
    bool threw = false;
    try { run_flow_op(s, m); } catch (const std::exception&) { threw = true; }
    CHECK(threw);
  }

  // --- group_agg edge cases: an empty input gives no groups; an int + float sum is a float (Python's sum)
  {
    Args e;
    e.set("rows", Value::list(ValueList{}));
    e.set("by", Value::list({Value("y")}));
    e.set("aggs", make_row({{"n", Value::list({Value("count"), Value()})}}));
    CHECK(run_flow_op(g, e).as_list().empty());
    Args f;
    f.set("rows", rows({{{"y", Value(int64_t(1))}, {"len", Value(int64_t(1))}},
                        {{"y", Value(int64_t(1))}, {"len", Value(2.5)}}}));
    f.set("by", Value::list({Value("y")}));
    f.set("aggs", make_row({{"total", Value::list({Value("sum"), Value("len")})}}));
    Value r = run_flow_op(g, f);
    CHECK(r.as_list().size() == 1 && r.as_list()[0].field("total").is_float() && r.as_list()[0].field("total").as_float() == 3.5);
  }

  // --- the ops over shapes keep row_set / row_with / row_merge's rules, and the uid-space indexes match a
  // vid with its str and migrate on the first other key
  auto op = [](const char* name, Args& x) { Step st; st.op = name; return run_flow_op(st, x); };
  auto I = [](int64_t i) { return Value(i); };
  {   // rename onto an existing name: the first position, the last value (row_set); a row with no renamed field is shared
    Args x;
    Value plain = make_row({{"q", I(9)}});
    x.set("rows", Value::list({make_row({{"a", I(1)}, {"b", I(2)}, {"c", I(3)}}), plain}));
    x.set("names", make_row({{"c", Value("a")}}));
    Value r = op("rename", x);
    const Value& r0 = r.as_list()[0];
    CHECK(r0.as_row().size() == 2 && r0.as_row()[0].first == "a" && r0.field("a").as_int() == 3 && r0.as_row()[1].first == "b");
    CHECK(r.as_list()[1].as_row().shape() == plain.as_row().shape());
  }
  {   // project: a missing field is null; the order is the fields' (a repeat collapses)
    Args x;
    x.set("rows", rows({{{"a", I(1)}, {"b", I(2)}}, {{"b", I(5)}}}));
    x.set("fields", Value::list({Value("b"), Value("zz"), Value("b")}));
    Value r = op("project", x);
    CHECK(r.as_list()[0].as_row().size() == 2 && r.as_list()[0].as_row()[0].first == "b" && r.as_list()[0].field("zz").is_null());
    CHECK(r.as_list()[1].field("b").as_int() == 5);
  }
  {   // annotate: a field named _vid keeps one field, holding the value (make_row's rule)
    Args x;
    x.set("items", Value::list({Value("v1")}));
    x.set("field", Value("_vid"));
    x.set("value", I(7));
    Value r = op("annotate", x);
    CHECK(r.as_list()[0].as_row().size() == 1 && r.as_list()[0].field("_vid").as_int() == 7);
  }
  {   // attach / derive's row_with: an existing name is replaced in place, a new one appended
    Args x;
    x.set("rows", rows({{{"a", I(1)}, {"b", I(2)}}, {{"b", I(5)}}}));
    x.set("values", Value::list({I(10), I(11)}));
    x.set("into", Value("b"));
    Value r = op("attach", x);
    CHECK(r.as_list()[0].as_row()[1].first == "b" && r.as_list()[0].field("b").as_int() == 10 && r.as_list()[1].field("b").as_int() == 11);
  }
  {   // group_count: a vid and its str are one group; a non-vid item migrates the index, the groups stay first-seen
    Args x;
    x.set("items", Value::list({Value::vid(5), Value(hex16(5)), Value::vid(6), I(3), Value(hex16(6)), I(3)}));
    Value r = op("group_count", x);
    const ValueList& g = r.as_list();
    CHECK(g.size() == 3 && g[0].field("count").as_int() == 2 && g[1].field("count").as_int() == 2 && g[2].field("count").as_int() == 2);
    CHECK(g[0].field("item").is_vid() && g[2].field("item").as_int() == 3);
  }
  {   // the member ops: a vid and its str are one member, in fast (all vids) and migrated (a non-vid) sets
    Value v1 = Value::vid(1), s1 = Value(hex16(1)), v2 = Value::vid(2), s2 = Value(hex16(2)), v3 = Value::vid(3);
    for (bool migrate : {false, true}) {
      ValueList members = {s1, v2};
      if (migrate) members.push_back(I(7));                      // a non-vid member: the set leaves uid space
      Args d;
      d.set("items", Value::list({v1, s1, v2, s2, v3, v1}));
      Value r = op("dedup", d);
      CHECK(r.as_list().size() == 3 && r.as_list()[0].is_vid() && r.as_list()[2].as_vid() == 3);   // first seen kept
      Args e;
      e.set("items", Value::list({v1, s2, v3, s1}));
      e.set("remove", Value::list(members));
      r = op("exclude", e);
      CHECK(r.as_list().size() == 1 && r.as_list()[0].as_vid() == 3);
      Args n;
      n.set("items", Value::list({v1, s2, v3, s1}));
      n.set("other", Value::list(members));
      r = op("intersect", n);
      CHECK(r.as_list().size() == 3 && r.as_list()[0].is_vid() && r.as_list()[1].is_str() && r.as_list()[2].is_str());
      Args f;
      f.set("rows", rows({{{"p", v1}}, {{"p", s2}}, {{"p", v3}}, {{"p", Value("zz")}}}));
      f.set("field", Value("p"));
      f.set("allowed", Value::list(members));
      r = op("filter_in", f);
      CHECK(r.as_list().size() == 2 && r.as_list()[0].field("p").is_vid() && r.as_list()[1].field("p").is_str());
    }
  }
  {   // join_multi: right rows by key in right order; a vid left key meets its str right key; right_on dropped
    Args x;
    x.set("left", rows({{{"p", Value::vid(1)}, {"x", I(1)}}, {{"p", Value::vid(2)}, {"x", I(2)}}}));
    x.set("right", rows({{{"k", Value(hex16(1))}, {"n", I(10)}}, {{"k", Value(hex16(1))}, {"n", I(11)}}, {{"k", Value("other")}, {"n", I(12)}}}));
    x.set("left_on", Value("p"));
    x.set("right_on", Value("k"));
    Value r = op("join_multi", x);
    CHECK(r.as_list().size() == 2 && r.as_list()[0].field("n").as_int() == 10 && r.as_list()[1].field("n").as_int() == 11);
    CHECK(r.as_list()[0].as_row().size() == 3 && r.as_list()[0].find("k") == nullptr);
  }
  {   // join_multi from either side's index (left_n 1 and 2: the left one; 4: the right one, as many rows): duplicate
      // keys on both sides, interleaved right keys, and a non-vid key after vid keys in the index (KeyIndex's migrate)
    const std::vector<Value> lkeys = {Value::vid(1), Value("zz"), Value::vid(1), Value::vid(2)};
    const std::map<int, std::vector<int64_t>> want = {{1, {10, 12}}, {2, {10, 12, 13}}, {4, {10, 12, 13, 10, 12, 11}}};
    for (auto& [left_n, ns] : want) {
      Args x;
      ValueList left;
      for (int i = 0; i < left_n; i++) left.push_back(make_row({{"p", lkeys[size_t(i)]}, {"i", I(i)}}));
      x.set("left", Value::list(left));
      x.set("right", rows({{{"k", Value(hex16(1))}, {"n", I(10)}}, {{"k", Value::vid(2)}, {"n", I(11)}},
                           {{"k", Value(hex16(1))}, {"n", I(12)}}, {{"k", Value("zz")}, {"n", I(13)}}}));
      x.set("left_on", Value("p"));
      x.set("right_on", Value("k"));
      Value r = op("join_multi", x);
      CHECK(r.as_list().size() == ns.size());
      for (size_t i = 0; i < ns.size() && i < r.as_list().size(); i++) CHECK(r.as_list()[i].field("n").as_int() == ns[i]);
    }
  }
  {   // join_multi's pair tests, from either side's index: a failing pair is absent, an all-pass test changes nothing,
      // where_in / where_not_in split the pairs by the key, and a key test with no set (or both sets) throws
    Bindings b;
    auto with = [&](Args& x, const char* where, const char* key) {
      Step st;
      st.op = "join_multi";
      if (where) st.exprs["where"] = Expr::compile(json::parse(where), b);
      if (key) st.exprs["where_key"] = Expr::compile(json::parse(key), b);
      return run_flow_op(st, x);
    };
    const char* key = R"({"tuple": [{"field": "p"}, {"field": "n"}]})";
    for (int left_n : {1, 3}) {
      Args x;
      ValueList left;
      for (int i = 0; i < left_n; i++) left.push_back(make_row({{"p", Value::vid(1)}, {"i", I(i)}}));
      x.set("left", Value::list(left));
      x.set("right", rows({{{"k", Value::vid(1)}, {"n", I(10)}}, {{"k", Value::vid(1)}, {"n", I(11)}}}));
      x.set("left_on", Value("p"));
      x.set("right_on", Value("k"));
      size_t all = op("join_multi", x).as_list().size();
      CHECK(all == size_t(2 * left_n));
      CHECK(with(x, R"({"ge": [{"field": "n"}, {"lit": 0}]})", nullptr).as_list().size() == all);
      Value w = with(x, R"({"gt": [{"field": "n"}, {"lit": 10}]})", nullptr);
      CHECK(w.as_list().size() == size_t(left_n) && w.as_list()[0].field("n").as_int() == 11);
      Args in = x;
      in.set("where_in", Value::list({Value::tuple({Value::vid(1), I(10)})}));
      Value r = with(in, nullptr, key);
      CHECK(r.as_list().size() == size_t(left_n) && r.as_list()[0].field("n").as_int() == 10);
      CHECK(with(in, R"({"gt": [{"field": "n"}, {"lit": 10}]})", key).as_list().empty());   // both tests: none pass
      Args out = x;
      out.set("where_not_in", Value::list({Value::tuple({Value::vid(1), I(10)})}));
      r = with(out, nullptr, key);
      CHECK(r.as_list().size() == size_t(left_n) && r.as_list()[0].field("n").as_int() == 11);
      bool threw = false;
      try { with(x, nullptr, key); } catch (const std::runtime_error&) { threw = true; }
      CHECK(threw);
      Args both = in;
      both.set("where_not_in", Value::list({}));
      threw = false;
      try { with(both, nullptr, key); } catch (const std::runtime_error&) { threw = true; }
      CHECK(threw);
    }
  }
  {   // join: the last duplicate right row wins, from either side's index (right smaller, then left smaller)
    for (int left_n : {1, 4}) {
      Args x;
      ValueList left;
      for (int i = 0; i < left_n; i++) left.push_back(make_row({{"p", Value(hex16(1))}, {"i", I(i)}}));
      x.set("left", Value::list(left));
      x.set("right", rows({{{"_vid", Value::vid(1)}, {"n", I(10)}}, {{"_vid", Value::vid(1)}, {"n", I(11)}}}));
      x.set("left_on", Value("p"));
      Value r = op("join", x);
      CHECK(int(r.as_list().size()) == left_n && r.as_list()[0].field("n").as_int() == 11);
    }
  }
  {   // top_k: a missing key throws only when a comparison reaches it (a lone row never is compared)
    Value one = rows({{{"a", I(1)}}});
    CHECK(top_k(one, Value::list({Value::list({Value("zz"), Value("asc")})}).as_list(), Value(), false).as_list().size() == 1);
    bool threw = false;
    try { top_k(rows({{{"a", I(1)}}, {{"a", I(2)}}}), Value::list({Value::list({Value("zz"), Value("asc")})}).as_list(), Value(), false); }
    catch (const std::runtime_error&) { threw = true; }
    CHECK(threw);
    Value r = top_k(rows({{{"a", I(1)}}, {{"a", I(3)}}, {{"a", I(2)}}}), Value::list({Value::list({Value("a"), Value("desc")})}).as_list(), I(2), false);
    CHECK(r.as_list().size() == 2 && r.as_list()[0].field("a").as_int() == 3 && r.as_list()[1].field("a").as_int() == 2);
  }
  {   // top_k over many rows in ascending key order, desc (every row beats the heap top): the bounded heap, the full
      // sort and keep_ties all equal a stable sort of the keys (a ~2.3 MB comparator copied per pop made this 13 s)
    const int n = 50000;
    ValueList big;
    for (int i = 0; i < n; i++) big.push_back(make_row({{"d", I(i / 3)}, {"p", I(i % 7)}}));   // d ties in threes
    Value bigv = Value::list(big);
    Value keys = Value::list({Value::list({Value("d"), Value("desc")}), Value::list({Value("p"), Value("asc")})});
    std::vector<int> want(n);
    std::iota(want.begin(), want.end(), 0);
    std::stable_sort(want.begin(), want.end(), [](int x, int y) {
      return x / 3 != y / 3 ? x / 3 > y / 3 : x % 7 < y % 7; });
    auto same = [&](const Value& got, size_t m) {
      if (got.as_list().size() != m) return false;
      for (size_t i = 0; i < m; i++)
        if (got.as_list()[i].field("d").as_int() != want[i] / 3 || got.as_list()[i].field("p").as_int() != want[i] % 7)
          return false;
      return true;
    };
    CHECK(same(top_k(bigv, keys.as_list(), I(10), false), 10));
    CHECK(same(top_k(bigv, keys.as_list(), Value(), false), n));
    Value tied = top_k(bigv, Value::list({Value::list({Value("d"), Value("desc")})}).as_list(), I(4), true);
    // the top d has 2 rows (49998, 49999), the next 3: k = 4 ends inside them, so the tie keeps all 3 (5 rows)
    CHECK(tied.as_list().size() == 5 && tied.as_list()[4].field("d").as_int() == (n - 1) / 3 - 1);
  }

  if (fails) { std::cerr << fails << " failure(s)\n"; return 1; }
  std::cout << "test_flow: ok\n";
  return 0;
}

}  // namespace
