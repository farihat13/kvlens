// Unit tests: the value model's rows (a shared shape + a values array) and vids (a uint64 that is its hex16 string to
// equality, order, hashing, JSON and neutral_bytes). The hashes are checked against the formula the digests used
// before the shapes (rows_digest must stay byte-identical across binaries).
#include <algorithm>
#include <atomic>
#include <cstdlib>
#include <new>
#include <thread>
#include <type_traits>
#include <vector>
#include <cctype>
#include <functional>
#include <iostream>
#include <unordered_set>
#include <stdexcept>

#include "formats/common.h"
#include "ir/expr.h"
#include "ir/plan.h"
#include "util/vid.h"
#include "value/value.h"
#include "harness.h"


using namespace evalcpp;


// The plain operator new / delete of this test binary, counted while `counting` (a row is one block). Only
// new(size_t) and the two plain deletes are replaced; array, aligned and nothrow forms are not counted (no row uses them).
static std::atomic<bool> counting{false};
static std::atomic<long> news{0}, deletes{0};
static std::atomic<void*> watched{nullptr};     // a block whose frees are counted (the throw-mid-fill case)
static std::atomic<int> watched_frees{0};
void* operator new(size_t n) {
  if (counting) news++;
  if (void* p = std::malloc(n ? n : 1)) return p;
  throw std::bad_alloc();
}
void operator delete(void* p) noexcept {
  if (p && counting) deletes++;
  if (p && p == watched) watched_frees++;
  std::free(p);
}
void operator delete(void* p, size_t) noexcept { operator delete(p); }

// the global operators above stay global (replacing them is the point); the rest is this file's own
namespace {

static Value S(const char* s) { return Value(s); }
static Value I(int64_t i) { return Value(i); }

// The row hash as written before the shapes: XOR over (hash(name) * 31 + hash_value(value)), from 0x7654321.
static size_t old_row_hash(const RowFields& f) {
  size_t h = 0x7654321;
  for (auto& kv : f) h ^= std::hash<std::string>()(kv.first) * 31 + hash_value(kv.second);
  return h;
}

TEST_CASE(test_value) {
  // --- a vid is its hex16 string
  const uint64_t us[] = {0, 1, 0xf, 0x10, 0x1000000000000005ULL, 0x2fffffffffffffffULL, 0x8000000000000000ULL,
                         0xffffffffffffffffULL};
  for (uint64_t u : us) {
    Value v = Value::vid(u), s = Value(hex16(u));
    CHECK(v.is_vid() && !v.is_str() && v.as_vid() == u);
    CHECK(equals(v, s) && equals(s, v) && equals(v, Value::vid(u)));
    CHECK(hash_value(v) == hash_value(s));                       // the digest of a vid == the digest of its str
    CHECK(hash_value(v) == std::hash<std::string>()(hex16(u)));
    CHECK(to_json(v) == json(hex16(u)) && to_json(v).dump() == to_json(s).dump());
    CHECK(neutral_bytes(v) == 16 && neutral_bytes(s) == 16);
    CHECK(v.truthy());
    CHECK(compare(v, s) == 0 && neutral_compare(v, s) == 0);
    CHECK(!equals(v, Value(int64_t(u))));                        // never an int
    CHECK(!equals(v, Value::bytes(hex16(u))));                   // never a bytes (a bytes != any str)
  }
  {   // the uppercase text is another str (Python compares strings), and a vid != a 15-char str
    std::string up = hex16(0xabcdef);
    std::transform(up.begin(), up.end(), up.begin(), ::toupper);
    CHECK(!equals(Value::vid(0xabcdef), Value(up)));
    CHECK(!equals(Value::vid(1), Value(hex16(1).substr(1))));
  }
  {   // order: vid vs vid, vid vs str, str vs str agree with the strings' order, in compare and neutral_compare
    std::vector<uint64_t> xs(std::begin(us), std::end(us));
    int pairs = 0;
    for (uint64_t a : xs)
      for (uint64_t b : xs) {
        int want = hex16(a) < hex16(b) ? -1 : (hex16(a) > hex16(b) ? 1 : 0);
        Value va = Value::vid(a), vb = Value::vid(b), sa = Value(hex16(a)), sb = Value(hex16(b));
        CHECK(compare(va, vb) == want && compare(va, sb) == want && compare(sa, vb) == want);
        CHECK(neutral_compare(va, vb) == want && neutral_compare(va, sb) == want && neutral_compare(sa, vb) == want);
        pairs++;
      }
    CHECK(pairs == 64);
    CHECK(compare(Value::vid(5), S("zz")) < 0 && compare(S("00"), Value::vid(0)) < 0);   // a prefix sorts first
    CHECK(neutral_compare(Value::vid(5), I(5)) > 0 && neutral_compare(Value::vid(5), Value::list({})) < 0);   // a str's rank
    bool threw = false;
    try { compare(Value::vid(1), I(1)); } catch (const std::runtime_error&) { threw = true; }
    CHECK(threw);                                                // Python: str < int is a TypeError
  }
  {   // a vid as a hash-set key meets its str: one entry
    std::unordered_set<Value, ValueHash, ValueEq> set;
    set.insert(Value::vid(42));
    set.insert(Value(hex16(42)));
    CHECK(set.size() == 1);
  }

  // --- rows: a shape + values, read through as_row()'s view as name/value pairs
  Value r = make_row({{"a", I(1)}, {"b", S("x")}, {"a", I(3)}, {"c", Value::vid(7)}});
  {
    auto v = r.as_row();
    CHECK(v.size() == 3 && !v.empty());
    CHECK(v[0].first == "a" && v[0].second.as_int() == 3);      // a repeated name: first position, last value
    CHECK(v[1].first == "b" && v[2].first == "c");
    std::vector<std::string> names;
    for (auto& [k, x] : r.as_row()) names.push_back(k);
    CHECK((names == std::vector<std::string>{"a", "b", "c"}));
    for (auto& kv : v) CHECK(&kv.first == &v.shape()->names[size_t(&kv.second - v.values().data())]);   // refs into the row
    auto it = std::find_if(v.begin(), v.end(), [](const FieldRef& kv) { return kv.first == "b"; });
    CHECK(it != v.end() && it->second.as_str() == "x");
    RowFields f = v.to_fields();
    CHECK(f.size() == 3 && f[2].first == "c" && f[2].second.as_vid() == 7);
    CHECK(make_row({}).as_row().empty() && !make_row({}).truthy() && r.truthy());
  }
  CHECK(r.field("b").as_str() == "x" && r.find("zz") == nullptr && r.get("zz").is_null());
  {
    bool threw = false;
    try { r.field("zz"); } catch (const std::runtime_error&) { threw = true; }
    CHECK(threw);
    threw = false;
    try { I(1).as_row(); } catch (const std::runtime_error&) { threw = true; }
    CHECK(threw);
    threw = false;
    try { Value::row(make_shape({"a", "b"}), {I(1)}); } catch (const std::runtime_error&) { threw = true; }
    CHECK(threw);                                                // values must match the shape
  }
  {   // equality: by name in any order, one shape or two; hash order-insensitive and the old formula
    Value p = make_row({{"x", I(1)}, {"y", S("q")}});
    Value q = make_row({{"y", S("q")}, {"x", I(1)}});
    ShapePtr sh = make_shape({"x", "y"});
    Value p1 = Value::row(sh, {I(1), S("q")}), p2 = Value::row(sh, {I(1), S("q")}), p3 = Value::row(sh, {I(2), S("q")});
    CHECK(equals(p, q) && equals(p1, p2) && equals(p1, q) && !equals(p1, p3) && !equals(p, make_row({{"x", I(1)}})));
    CHECK(hash_value(p) == hash_value(q) && hash_value(p1) == hash_value(q));
    CHECK(hash_value(p) == old_row_hash({{"x", I(1)}, {"y", S("q")}}));
    CHECK(hash_value(p3) != hash_value(p1));                     // the control: a changed value changes the hash
    Value pv = make_row({{"_vid", Value::vid(9)}, {"n", I(2)}});
    Value ps = make_row({{"_vid", Value(hex16(9))}, {"n", I(2)}});
    CHECK(equals(pv, ps) && hash_value(pv) == hash_value(ps) && to_json(pv).dump() == to_json(ps).dump());
    CHECK(hash_value(pv) == old_row_hash({{"_vid", Value(hex16(9))}, {"n", I(2)}}));
    CHECK(neutral_bytes(pv) == 24 && to_json(p).dump() == "{\"x\":1,\"y\":\"q\"}");
  }
  {   // row_with: replace in place (same shape) or append; row_merge: b overwrites in a's position, new names append
    Value w = row_with(r, "b", I(5));
    CHECK(w.as_row().shape() == r.as_row().shape() && w.field("b").as_int() == 5 && r.field("b").as_str() == "x");
    Value w2 = row_with(r, "d", I(6));
    CHECK(w2.as_row().size() == 4 && w2.as_row()[3].first == "d" && r.as_row().size() == 3);
    Value a = make_row({{"a", I(1)}, {"b", I(2)}});
    Value b = make_row({{"b", I(9)}, {"c", I(3)}, {"k", I(4)}});
    std::string skip = "k";
    Value m = row_merge(a, b, &skip);
    CHECK(m.as_row().size() == 3 && m.as_row()[1].first == "b" && m.field("b").as_int() == 9 && m.as_row()[2].first == "c");
    CHECK(row_merge(a, b).as_row().size() == 4);
    CHECK(to_json(from_json(to_json(m))).dump() == to_json(m).dump());
  }

  // --- the shared consumers take a vid where they took its str
  {
    const uint64_t u = 0x1000000000000007ULL;
    Value v = Value::vid(u), s = Value(hex16(u));
    CHECK(v.is_text() && s.is_text() && !I(1).is_text() && v.text() == hex16(u) && s.text() == hex16(u));
    bool threw = false;
    try { I(1).text(); } catch (const std::runtime_error&) { threw = true; }
    CHECK(threw);
    Args a;
    a.set("vid", v);
    a.set("vids", Value::list({v, s}));
    CHECK(a.str("vid") == hex16(u) && (a.str_list("vids") == std::vector<std::string>{hex16(u), hex16(u)}));
    CHECK(uid_or_throw("t", v) == u && uid_or_throw("t", s) == u && uid_or_throw("t", hex16(u)) == u);
    threw = false;
    try { uid_or_throw("t", I(3)); } catch (const std::runtime_error&) { threw = true; }
    CHECK(threw);
    ValueList vs = {Value::vid(1), Value(hex16(2)), Value(hex16(1)), Value::vid(2), Value::vid(3)};
    std::vector<StrPtr> d = distinct_strs(vs);
    CHECK(d.size() == 3 && *d[0] == hex16(1) && *d[1] == hex16(2) && *d[2] == hex16(3));
    std::vector<size_t> at = task_index_of(vs, d);
    CHECK((at == std::vector<size_t>{0, 1, 0, 1, 2}));
    CHECK(Expr::key_id(v).as_int() == int64_t(id_of_uid(u)) && Expr::key_id(s).as_int() == int64_t(id_of_uid(u)));
    CHECK(equals(key_id_value(u), Expr::key_id(v)) && equals(key_id_value(u), key_id_value(hex16(u))));
  }

  // --- distinct_vids is distinct_strs + distinct_values + task_index_of in uid space
  {
    Value t2 = Value(hex16(2));
    t2.label = 4;
    Value t2b = Value::vid(2);
    t2b.label = 5;
    ValueList vs = {Value::vid(1), Value(hex16(2)), Value(hex16(1)), t2, Value::vid(3), t2b};
    DistinctVids d = distinct_vids("t", vs, /*strict=*/false);
    CHECK((d.uids == std::vector<uint64_t>{1, 2, 3}) && (d.at == std::vector<size_t>{0, 1, 0, 1, 2, 1}));
    CHECK(d.values[0].is_vid() && d.values[0].label < 0 && d.values[1].label == 4);   // the first TAGGED occurrence's
    CHECK(equals(d.values[1], Value(hex16(2))));
    bool threw = false;
    try { distinct_vids("t", vs, /*strict=*/true); } catch (const std::runtime_error&) { threw = true; }
    CHECK(threw);                                                                    // two labels on one vid
    CHECK(distinct_vids("t", {Value::vid(7), Value::vid(8)}, true).at.empty());     // all distinct: no `at`
    for (Value bad : {Value("00000000000000AB"), Value("ab"), I(3), Value()}) {      // never a silent second spelling
      threw = false;
      try { distinct_vids("t", {bad}, false); } catch (const std::runtime_error&) { threw = true; }
      CHECK(threw);
    }
  }

  // --- RowBuilder = make_row's rows over one shared shape; RowWith = row_with's
  {
    RowBuilder rb({"_vid", "id", "x", "id", "y"});              // a repeated name: first position, last value
    auto mk = [&](uint64_t u, int64_t id1, int64_t x, int64_t id2) {
      RowBuilder::Row r = rb.start();
      r.push(Value::vid(u)); r.push(I(id1)); r.push(I(x)); r.push(I(id2)); r.push(Value());
      return r.done();
    };
    Value a = mk(1, 10, 5, 11), b = mk(2, 20, 6, 21);
    Value want = make_row({{"_vid", Value::vid(1)}, {"id", I(10)}, {"x", I(5)}, {"id", I(11)}, {"y", Value()}});
    CHECK(equals(a, want) && hash_value(a) == hash_value(want) && to_json(a).dump() == to_json(want).dump());
    CHECK(a.as_row().size() == 4 && a.as_row()[1].first == "id" && a.field("id").as_int() == 11);
    CHECK(a.as_row().shape() == b.as_row().shape() && b.as_row().shape() == rb.shape());   // one shape for the call
    RowBuilder::Row r = rb.start();
    r.push(Value::vid(3));
    r[0] = Value::vid(4);                                        // overwrite a pushed value
    CHECK(r[0].as_vid() == 4);
    bool threw = false;
    try { r[1]; } catch (const std::runtime_error&) { threw = true; }
    CHECK(threw);                                                // not pushed yet
    threw = false;
    try { r.done(); } catch (const std::runtime_error&) { threw = true; }
    CHECK(threw);                                                // 1 of 5 pushed
    RowBuilder one({"a"});
    RowBuilder::Row r1 = one.start();
    r1.push(I(1));
    threw = false;
    try { r1.push(I(2)); } catch (const std::runtime_error&) { threw = true; }
    CHECK(threw);                                                // a push past the names
    CHECK(RowBuilder({}).start().done().as_row().empty());
    RowWith with("k");
    Value w1 = with(a, I(1)), w2 = with(b, I(2)), w3 = with(make_row({{"k", I(0)}}), I(3));
    CHECK(equals(w1, row_with(a, "k", I(1))) && equals(w3, row_with(make_row({{"k", I(0)}}), "k", I(3))));
    CHECK(w1.as_row().shape() == w2.as_row().shape() && w3.as_row().size() == 1);
  }

  // --- closure_over over vids: a vid and its str are one vertex; first-seen order, the srcs first
  {
    auto nb = [](uint64_t, const ValueList& fr) {
      ValueList out;
      for (auto& x : fr) {
        uint64_t u = uid_or_throw("t", x);
        if (u == 1) { out.push_back(Value::vid(2)); out.push_back(Value(hex16(3))); }
        if (u == 2) { out.push_back(Value(hex16(3))); out.push_back(Value(hex16(1))); }
        if (u == 3) { out.push_back(Value::vid(4)); out.push_back(Value::vid(2)); }
      }
      return Value::list(std::move(out));
    };
    Value c = closure_over(nb, {Value::vid(1), Value(hex16(1))}, Value());
    const ValueList& l = c.as_list();
    CHECK(l.size() == 4 && l[0].as_vid() == 1 && uid_or_throw("t", l[1]) == 2 && uid_or_throw("t", l[2]) == 3 &&
          uid_or_throw("t", l[3]) == 4);
    CHECK(closure_over(nb, {Value::vid(1)}, Value(int64_t(1))).as_list().size() == 3);   // one level
  }

  // --- a row is one heap block (RowData header + its values), owned by an intrusive RowPtr
  {
    CHECK(kRowValsOffset % alignof(Value) == 0 && kRowValsOffset >= sizeof(RowData));   // computed, not assumed
    Value probe = make_row({{"a", I(1)}, {"b", I(2)}});
    const Value& b = probe.field("b");
    CHECK(reinterpret_cast<uintptr_t>(&b) % alignof(Value) == 0 && &b == &probe.field("a") + 1);   // one array
    RowBuilder rb({"a", "b", "c"});
    counting = true; news = 0; deletes = 0;
    {   // a reader's row of ints: ONE allocation, ONE free (before: the builder's vector + make_shared's block)
      RowBuilder::Row r = rb.start();
      r.push(I(1)); r.push(I(2)); r.push(I(3));
      Value row = r.done();
      CHECK(news == 1);
    }
    CHECK(news == 1 && deletes == 1);
    news = 0; deletes = 0;
    {   // RowWith / row_with / row_merge: one block per output row (their shape once per call, or per row for row_merge)
      Value a = make_row({{"a", I(1)}});
      long before = news;
      RowWith with("k");
      Value w1 = with(a, I(2));                 // the first: its shape (2 allocations: names + shape) + the row
      long first = news - before;
      Value w2 = with(a, I(3));                 // the next: the row's block only
      CHECK(news - before - first == 1 && equals(w2, make_row({{"a", I(1)}, {"k", I(3)}})));
    }
    CHECK(news == deletes);                     // everything built here is freed
    counting = false;

    // a throw while a row is being filled: its values are destroyed (the string's count drops back), the block is
    // freed exactly once
    StrPtr held = std::make_shared<const std::string>("held");
    counting = true; news = 0; deletes = 0;
    bool threw = false;
    try {
      RowSlots s(make_shape({"a", "b", "c"}));
      watched = reinterpret_cast<char*>(&s[0]) - kRowValsOffset;   // the block: header, then the values
      s[0] = Value(held);
      CHECK(held.use_count() == 2);
      s[3] = I(1);                              // out of range: throws mid-fill
    } catch (const std::runtime_error&) { threw = true; }
    // the value built is destroyed, the block is freed once, nothing leaks (the error's own strings included)
    CHECK(threw && held.use_count() == 1 && watched_frees == 1 && news == deletes);
    watched = nullptr;
    counting = false;
    threw = false;
    try { RowSlots s(make_shape({"a"})); s.done(); s.done(); } catch (const std::runtime_error&) { threw = true; }
    CHECK(threw);                               // done() twice
    threw = false;
    try { RowSlots s(make_shape({"a"})); s.done(); s[0] = I(1); } catch (const std::runtime_error&) { threw = true; }
    CHECK(threw);                               // a slot after done()
    threw = false;
    try { RowBuilder({"a"}).start().done(); } catch (const std::runtime_error&) { threw = true; }
    CHECK(threw);                               // a Row with a name not pushed

    threw = false;
    try {   // a moved-from RowSlots holds no block: a slot throws (never a write past another block)
      RowSlots a(make_shape({"a", "b", "c"}));
      RowSlots b(std::move(a));
      CHECK(a.size() == 0 && b.size() == 3);
      a[0] = I(1);
    } catch (const std::runtime_error&) { threw = true; }
    CHECK(threw);
    static_assert(!std::is_move_assignable_v<RowSlots>, "a RowSlots is never re-pointed");
    // RowPtr's own copy is nothrow (as shared_ptr's); Value's variant reports false either way in this libstdc++
    static_assert(std::is_nothrow_copy_constructible_v<RowPtr> && std::is_nothrow_move_constructible_v<RowPtr>, "");
    {   // a move-assign frees the old row now (as shared_ptr's), not when the moved-from Value dies
      StrPtr old_s = std::make_shared<const std::string>("old");
      Value x = make_row({{"s", Value(old_s)}}), y = make_row({{"t", I(1)}});
      CHECK(old_s.use_count() == 2);
      x = std::move(y);
      CHECK(old_s.use_count() == 1 && x.field("t").as_int() == 1);
    }

    // a 0-field row; a row that outlives its builder and the shape's other holders
    Value empty = RowBuilder({}).start().done();
    CHECK(empty.as_row().empty() && !empty.truthy() && to_json(empty).dump() == "{}" && neutral_bytes(empty) == 0);
    Value kept;
    {
      RowBuilder tmp({"p", "q"});
      RowBuilder::Row r = tmp.start();
      r.push(Value(std::string(40, 'z'))); r.push(Value::vid(9));
      kept = r.done();
    }
    CHECK(kept.field("p").as_str() == std::string(40, 'z') && kept.field("q").as_vid() == 9 &&
          kept.as_row().shape()->names.size() == 2);

    // copies and releases of one row from 8 threads: the count ends where it began, the row intact
    StrPtr inner = std::make_shared<const std::string>("inner");
    Value shared = make_row({{"s", Value(inner)}, {"n", I(5)}});
    {
      std::vector<std::thread> ts;
      for (int t = 0; t < 8; t++)
        ts.emplace_back([&shared] {
          for (int i = 0; i < 20000; i++) { Value c = shared; ValueList l{c, c}; (void)l; }
        });
      for (auto& t : ts) t.join();
    }
    CHECK(inner.use_count() == 2 && shared.field("n").as_int() == 5);   // `inner` + the row's one copy
    shared = Value();
    CHECK(inner.use_count() == 1);                                      // the last reference freed the row
  }

  std::cout << (fails ? "FAILED " : "ok ") << checks - fails << "/" << checks << " checks\n";
  return fails ? 1 : 0;
}

}  // namespace
