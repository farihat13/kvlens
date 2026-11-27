// Unit tests: the value model + expression grammar against the plan's section 9.4 table.
#include <algorithm>
#include <cstdio>
#include <cmath>
#include <fstream>
#include <map>
#include <cstdlib>
#include <iostream>

#include "ir/expr.h"
#include "schema/schema.h"
#include "value/value.h"
#include "harness.h"

namespace {

using namespace evalcpp;

#define THROWS(stmt) do { bool t = false; try { stmt; } catch (const std::exception&) { t = true; } if (!t) { fails++; std::cerr << "FAIL " << __LINE__ << " expected throw: " #stmt "\n"; } } while (0)

static json J(const char* s) { return json::parse(s); }

// The test's own JSON -> Value: like from_json, but a JSON float becomes a double (from_json refuses floats on purpose).
static Value value_f(const json& j) {
  if (j.is_number_float()) return Value(j.get<double>());
  if (j.is_array()) {
    ValueList items;
    for (auto& x : j) items.push_back(value_f(x));
    return Value::list(std::move(items));
  }
  return from_json(j);
}

TEST_CASE(test_expr) {
  json sj = J(R"({"name":"t","vertices":[
     {"label":"Person","id":0,"group":"Person","tag":0,"props":[{"name":"firstName","type":"str"}]},
     {"label":"Post","id":1,"group":"Message","tag":1,"props":[]},
     {"label":"Comment","id":2,"group":"Message","tag":1,"props":[]}],
    "edges":[],"indexes":[],"vid_tags":{"Person":0,"Message":1}})");
  Schema schema = Schema::from_json(sj);
  Bindings b;
  b.schema = &schema;
  b.params = {{"maxDate", Value(100)}, {"days", Value(3)}, {"person", Value("933")}, {"name", Value("Ann")}};
  b.lets = {{"windowEnd", Value(100 + 3 * 86400000LL)}};
  auto ev = [&](const char* ast, Value row) { return Expr::compile(J(ast), b)->eval(row); };
  auto row = [](RowFields f) { return Value::row(std::move(f)); };

  // --- the Python selftest cases
  CHECK(ev(R"({"lt":[{"field":"d"},{"param":"maxDate"}]})", row({{"d", 50}})).as_bool() == true);
  CHECK(ev(R"({"lt":[{"field":"d"},{"param":"maxDate"}]})", row({{"d", 150}})).as_bool() == false);
  CHECK(ev(R"({"not":{"field":"used"}})", row({{"used", false}})).as_bool() == true);
  CHECK(ev(R"({"add":[{"field":"x"},{"field":"y"}]})", row({{"x", 2}, {"y", 3}})).as_int() == 5);
  CHECK(ev(R"({"mul":[{"param":"days"},{"lit":86400000}]})", row({})).as_int() == 3 * 86400000LL);
  CHECK(ev(R"({"eq":[{"get":"type"},{"lit":"country"}]})", row({})).as_bool() == false);
  CHECK(ev(R"({"coalesce":[{"get":"a"},{"lit":9}]})", row({{"a", Value()}})).as_int() == 9);
  CHECK(ev(R"({"if":[{"field":"c"},{"lit":1},{"lit":-1}]})", row({{"c", true}})).as_int() == 1);
  CHECK(equals(ev(R"({"tuple":[{"field":"a"},{"field":"b"}]})", row({{"a", 1}, {"b", 2}})), Value::tuple({1, 2})));
  CHECK(ev(R"({"le":[{"field":"d"},{"let":"windowEnd"}]})", row({{"d", Value(100 + 3 * 86400000LL)}})).as_bool());
  CHECK(ev(R"({"in":[{"field":"b"},{"lit":[1,2,3]}]})", row({{"b", 2}})).as_bool());
  CHECK(ev(R"({"not":{"in":[{"field":"b"},{"lit":[1,2]}]}})", row({{"b", 9}})).as_bool());

  // --- section 9.4 semantics
  CHECK(ev(R"({"floordiv":[{"lit":-7},{"lit":2}]})", row({})).as_int() == -4);        // floors
  CHECK(ev(R"({"sub":[{"lit":-7},{"mul":[{"floordiv":[{"lit":-7},{"lit":2}]},{"lit":2}]}]})", row({})).as_int() == 1);  // mod
  CHECK(month_of_ms(1354060800000LL) == 11 && day_of_ms(1354060800000LL) == 28);       // 2012-11-28 UTC
  CHECK(month_of_ms(0) == 1 && day_of_ms(0) == 1);
  CHECK(month_of_ms(-1) == 12 && day_of_ms(-1) == 31);                                 // floors below 0
  CHECK(month_of_ms(1354060800000LL + 86399999) == 11);                                 // non-aligned
  CHECK(Expr::is_digit(Value("933")) && !Expr::is_digit(Value("-5")) && !Expr::is_digit(Value("")));
  CHECK(Expr::is_digit(Value(42)) && !Expr::is_digit(Value(-1)) && !Expr::is_digit(Value()));
  CHECK(equals(Value(1), Value(true)) && !equals(Value(1), Value("1")) && equals(Value(), Value()));
  CHECK(!equals(Value::list({1}), Value::tuple({1})));
  THROWS(compare(Value(1), Value("1")));
  THROWS(compare(Value(), Value(1)));
  CHECK(compare(Value(true), Value(2)) < 0);
  // and/or short-circuit: month_of on a missing field is never reached
  CHECK(ev(R"({"or":[{"lit":true},{"month_of":{"field":"nope"}}]})", row({})).as_bool());
  CHECK(!ev(R"({"and":[{"lit":0},{"month_of":{"field":"nope"}}]})", row({})).as_bool());
  CHECK(ev(R"({"and":[{"lit":1},{"lit":"x"}]})", row({})).is_bool());                  // returns a bool
  CHECK(!ev(R"({"not":{"lit":[]}})", row({})).as_bool() == false);                      // [] is false
  CHECK(ev(R"({"coalesce":[{"lit":0},{"lit":5}]})", row({})).as_int() == 0);            // first non-None, not truthy
  CHECK(ev(R"({"in":[{"lit":"an"},{"lit":"Anna"}]})", row({})).as_bool() == false);      // substring, case-sensitive
  CHECK(ev(R"({"in":[{"lit":"nn"},{"lit":"Anna"}]})", row({})).as_bool() == true);
  THROWS(ev(R"({"field":"missing"})", row({})));
  CHECK(ev(R"({"get":"missing"})", row({})).is_null());
  THROWS(ev(R"({"add":[{"lit":9223372036854775807},{"lit":1}]})", row({})));
  // vid / key_id
  {   // a param's vid is a vid (C2): its text is the hex16 the stores' reads emit
    Value v = ev(R"({"vid":[{"lit":"Person"},{"param":"person"}]})", row({}));
    CHECK(v.is_vid() && v.text() == "00000000000003a5");
  }
  CHECK(ev(R"({"vid":[{"lit":"Comment"},{"lit":7}]})", row({})).text() == "1000000000000007");
  CHECK(ev(R"({"key_id":{"lit":"1000000000000007"}})", row({})).as_int() == 7);
  CHECK(ev(R"({"key_id":{"lit":"A"}})", row({})).is_null());
  THROWS(ev(R"({"vid":[{"lit":"Person"},{"lit":"576460752303423488"}]})", row({})));    // 2^59
  // compose_vid idiom: is_digit short-circuits so vid() is never called on "A"
  CHECK(ev(R"({"if":[{"is_digit":{"lit":"A"}},{"vid":[{"lit":"Person"},{"lit":"A"}]},{"lit":"A"}]})", row({})).as_str() == "A");
  // param coercion happens before compile: an int param stays int; a str param is_digit works
  CHECK(ev(R"({"is_digit":{"param":"person"}})", row({})).as_bool());

  // --- value model
  CHECK(hash_value(Value(1)) == hash_value(Value(true)));
  CHECK(equals(from_json(J(R"({"a":[1,"x",null]})")), row({{"a", Value::list({1, "x", Value()})}})));
  CHECK(to_json(Value::tuple({1, "a"})).dump() == "[1,\"a\"]");
  CHECK(neutral_compare(Value(), Value(false)) < 0 && neutral_compare(Value(2010), Value("999")) < 0);
  CHECK(neutral_compare(Value::tuple({"a", 2010}), Value::tuple({"a", 999})) > 0);   // numeric, not repr
  Value r = row({{"a", 1}, {"b", 2}});
  Value m = row_merge(r, row({{"b", 9}, {"c", 3}}));
  CHECK(m.as_row().size() == 3 && m.field("b").as_int() == 9 && m.as_row()[1].first == "b");
  std::string skip = "c";
  CHECK(row_merge(r, row({{"c", 3}}), &skip).as_row().size() == 2);

  // --- the float alternative (`div` only): the value model's rules
  CHECK(equals(Value(2.0), Value(2)) && equals(Value(1.0), Value(true)) && !equals(Value(2.5), Value(2)));
  CHECK(compare(Value(2.5), Value(3)) < 0 && compare(Value(3), Value(2.5)) > 0 && compare(Value(2.0), Value(2)) == 0);
  THROWS(compare(Value(2.5), Value("3")));
  CHECK(hash_value(Value(2.0)) == hash_value(Value(2)) && hash_value(Value(-0.0)) == hash_value(Value(0)));
  CHECK(neutral_compare(Value(2.5), Value(3)) < 0 && neutral_compare(Value(2.5), Value("a")) < 0);
  CHECK(neutral_bytes(Value(2.5)) == 0);                        // Python's content_bytes has no float case
  CHECK(Value(0.0).truthy() == false && Value(0.5).truthy());
  CHECK(std::string(Value(0.5).type_name()) == "float");
  THROWS(from_json(J("2.5")));                                    // a float never comes from JSON
  // a plan with integer literals still parses every number as int64 (a float appears only from `div`)
  {
    Value v = from_json(J(R"({"a":1,"b":[2,9007199254740993],"c":-5})"));
    CHECK(v.field("a").is_int() && v.field("b").as_list()[1].is_int() && v.field("b").as_list()[1].as_int() == 9007199254740993LL);
    CHECK(to_json(v).dump() == R"({"a":1,"b":[2,9007199254740993],"c":-5})");
    CHECK(ev(R"({"add":[{"lit":2},{"lit":3}]})", row({})).is_int());
  }

  // --- parity with Python: tests/fixtures/expr.json (gen_expr_fixtures.py) -- the same AST and row, Python's result
  {
    std::ifstream in(std::string(FIXTURE_DIR) + "/expr.json");
    CHECK(in.good());
    json fixture = json::parse(in);
    json cases = fixture["cases"];
    static const std::map<std::string, std::string> py_type = {{"float", "float"}, {"int", "int"}, {"str", "str"},
        {"bool", "bool"}, {"tuple", "tuple"}, {"NoneType", "null"}, {"list", "list"}};
    int n = 0;
    for (auto& c : cases) {
      Value r = from_json(c["row"]);
      if (c.contains("error")) { THROWS(Expr::compile(c["ast"], b)->eval(r)); n++; continue; }
      Value got = Expr::compile(c["ast"], b)->eval(r);
      std::string want = c["json"].get<std::string>();
      bool type_ok = py_type.at(c["type"].get<std::string>()) == got.type_name();
      bool value_ok = to_json(got) == json::parse(want);
      // A scalar float: the same JSON text, except 1e15 <= |x| < 1e16, where nlohmann writes an exponent ("1e+15")
      // and Python does not ("1000000000000000.0") -- the same number on the wire (Python parses both alike), and no
      // C++ code a float can reach today hashes or compares a value's text (index terms and is_digit do use repr(),
      // but only ints and strs reach them; a float is only ever a derived, in-memory value).
      bool text_band = got.is_float() && std::fabs(got.as_float()) >= 1e15 && std::fabs(got.as_float()) < 1e16;
      bool text_ok = !got.is_float() || text_band || to_json(got).dump() == want;
      bool bytes_ok = neutral_bytes(got) == c["bytes"].get<size_t>();   // the same neutral byte model as Python
      if (!(type_ok && value_ok && text_ok && bytes_ok)) {
        fails++;
        std::cerr << "FAIL parity " << c["ast"].dump() << ": python " << c["type"].get<std::string>() << " " << want
                  << " (" << c["bytes"].get<size_t>() << " B), cpp " << got.type_name() << " " << to_json(got).dump()
                  << " (" << neutral_bytes(got) << " B)\n";
      }
      n++;
    }
    CHECK(n == int(cases.size()) && n > 0);
    std::cout << "expr parity: " << n << " cases vs Python\n";
    // nest's neutral order: C++ neutral_compare sorts the values exactly as Python's _neutral_key did
    ValueList want, got;
    for (auto& v : fixture["neutral_order"]) want.push_back(value_f(v));
    got.assign(want.rbegin(), want.rend());
    std::stable_sort(got.begin(), got.end(), [](const Value& a, const Value& b) { return neutral_compare(a, b) < 0; });
    bool same = got.size() == want.size();
    for (size_t i = 0; same && i < got.size(); i++)
      same = std::string(got[i].type_name()) == want[i].type_name() && neutral_compare(got[i], want[i]) == 0;
    if (!same) { fails++; std::cerr << "FAIL neutral order differs from Python's\n"; }
    std::cout << "neutral order: " << want.size() << " values vs Python\n";
  }

  if (fails) { std::cerr << fails << " failures\n"; return 1; }
  std::cout << "test_expr OK\n";
  return 0;
}

}  // namespace
