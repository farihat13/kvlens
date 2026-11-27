// The row/value model: the Python values a plan moves between steps, with Python's
// equality, hashing and ordering rules. Containers are shared and
// immutable, so copying a step output or a row field is a pointer copy.
#pragma once
#include <nlohmann/json.hpp>

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <iterator>
#include <memory>
#include <optional>
#include <string>
#include <utility>
#include <variant>
#include <vector>

namespace evalcpp {

using json = nlohmann::ordered_json;   // ordered: `let` blocks are evaluated in file order

struct Value;
using ValueList = std::vector<Value>;
// A row's fields as name/value pairs, insertion-ordered like a dict: the form a row is BUILT in (make_row, row_set) and
// the copy as_row().to_fields() returns. A stored row is a RowData: a shared shape and a values array.
using RowFields = std::vector<std::pair<std::string, Value>>;
using ListPtr = std::shared_ptr<const ValueList>;

// A row's field names, in order (one per distinct field list): rows of one shape share it, so a name is stored once
// per shape, not once per row (NebulaGraph reads a row against its schema the same way).
struct RowShape {
  std::vector<std::string> names;
  int index_of(const std::string& name) const;   // -1 when absent; linear (rows hold few fields)
};
using ShapePtr = std::shared_ptr<const RowShape>;
ShapePtr make_shape(std::vector<std::string> names);   // names must be unique (make_row dedups)

// A row is ONE heap block: this header, then its n values (vals()[i] is the field shape->names[i]), made by one
// allocation and freed by one (not a shared_ptr's block plus a vector's buffer). Used only by
// value.h/.cc (declared here for Value's variant): a row is built through RowSlots / RowBuilder / Value::row and read
// through RowView.
struct RowData {
  mutable std::atomic<uint32_t> refs;
  uint32_t n;
  ShapePtr shape;
  inline const Value* vals() const;   // the trailing array (below Value: it needs Value complete)
  inline Value* vals();
};

// The owner of a row block: an intrusive count, 8 bytes (relaxed add, acq_rel release: enough before the destruction,
// as Boost's and libc++'s counts; libstdc++'s shared_ptr adds with acq_rel).
class RowPtr {
 public:
  RowPtr() = default;
  explicit RowPtr(RowData* adopt) : p_(adopt) {}   // takes the block's first reference
  RowPtr(const RowPtr& o) noexcept : p_(o.p_) { if (p_) p_->refs.fetch_add(1, std::memory_order_relaxed); }
  RowPtr(RowPtr&& o) noexcept : p_(o.p_) { o.p_ = nullptr; }
  RowPtr& operator=(const RowPtr& o) noexcept { RowPtr t(o); std::swap(p_, t.p_); return *this; }
  RowPtr& operator=(RowPtr&& o) noexcept {   // the old block is released now, as shared_ptr's move-assign does
    if (this != &o) { if (p_) release(p_); p_ = o.p_; o.p_ = nullptr; }
    return *this;
  }
  ~RowPtr() { if (p_) release(p_); }
  const RowData* get() const { return p_; }
  const RowData& operator*() const { return *p_; }
  const RowData* operator->() const { return p_; }
  // this pointer is the block's only holder: nothing else can see a change to it
  bool sole() const { return p_ && p_->refs.load(std::memory_order_acquire) == 1; }
  // the same values under another shape of the same size (rename on a row its step holds alone); throws otherwise
  void reshape(ShapePtr shape);
 private:
  static void release(RowData* p);     // the last reference destroys the n values and frees the block, once
  RowData* p_ = nullptr;
};
// Strings are shared too: a vid is 16 hex chars, one over std::string's inline limit, so a
// by-value string would heap-allocate on every row copy (join/rename/pluck were 26% malloc).
using StrPtr = std::shared_ptr<const std::string>;

struct Tuple {          // a Python tuple: same payload as a list, a different type for ==
  ListPtr items;
};

struct Bytes {          // a Python bytes: raw bytes (a LinkBench data blob), never text; != any str
  StrPtr data;
};

struct Vid {            // a vertex id: the uint64 of util/vid.h. It IS its 16 lowercase hex chars to the value model:
  uint64_t u;           // it equals, hashes, orders, sizes and prints as hex16(u), so a result, a digest or a count
};                      // never tells a vid from that string (a typed id, as Dgraph's uid and NebulaGraph's int vid)

class RowView;          // a row's fields by reference (below, after Value)


struct Value {
  // null | bool | int | str | list | tuple | row | float | bytes | vid. A float exists only as an in-memory derived value
  // (the expression grammar's `div`): never stored, never parsed from JSON. Each later kind is appended, so every other
  // index holds. bytes crosses JSON as {"$bytes": "<base64>"} (to_json / from_json); a vid as its hex16 string.
  std::variant<std::monostate, bool, int64_t, StrPtr, ListPtr, Tuple, RowPtr, double, Bytes, Vid> v;
  // A vertex id's LABEL, when the read that produced this value knew it (meta: a hop's far end, a probe that hit):
  // a label id, -1 = none. It travels with the value through every copy (rows, lists, flow ops), so a later step knows
  // a label only if the row that reached it carries it. Ignored by equality, hashing, ordering, JSON and neutral_bytes
  // (they read `v` only): no result, hash or count depends on it. Costs one word per Value (24 -> 32 B).
  int16_t label = -1;

  Value() = default;
  Value(std::nullptr_t) {}
  Value(bool b) : v(b) {}
  Value(int64_t i) : v(i) {}
  Value(int i) : v(int64_t(i)) {}
  Value(long long i) : v(int64_t(i)) {}
  // no uint64 constructor: a uid is a vid (Value::vid) and a count an int (Value(int64_t(n))), never silently the other
  Value(uint64_t) = delete;
  Value(std::string s) : v(std::make_shared<const std::string>(std::move(s))) {}
  Value(const char* s) : v(std::make_shared<const std::string>(s)) {}
  Value(StrPtr s) : v(std::move(s)) {}
  Value(ListPtr l) : v(std::move(l)) {}
  Value(Tuple t) : v(std::move(t)) {}
  Value(RowPtr r) : v(std::move(r)) {}
  Value(double d) : v(d) {}
  Value(Bytes b) : v(std::move(b)) {}
  Value(Vid d) : v(d) {}

  static Value bytes(std::string raw) { return Value(Bytes{std::make_shared<const std::string>(std::move(raw))}); }
  // the list object itself is not const (only the pointer's view is): a holder that is provably alone may change it
  // (Args::owned_list, take_items) without undefined behaviour
  static Value list(ValueList items) { return Value(ListPtr(std::make_shared<ValueList>(std::move(items)))); }
  static Value tuple(ValueList items) { return Value(Tuple{ListPtr(std::make_shared<ValueList>(std::move(items)))}); }
  static Value vid(uint64_t u) { return Value(Vid{u}); }
  static Value row(RowFields f);       // names must be unique (make_row dedups); a new shape for this row
  static Value row(ShapePtr shape, ValueList values);   // values in shape order: rows of one shape share it

  bool is_null() const { return v.index() == 0; }
  bool is_bool() const { return v.index() == 1; }
  bool is_int() const { return v.index() == 2; }
  bool is_str() const { return v.index() == 3; }
  bool is_list() const { return v.index() == 4; }
  bool is_tuple() const { return v.index() == 5; }
  bool is_row() const { return v.index() == 6; }
  bool is_float() const { return v.index() == 7; }
  bool is_bytes() const { return v.index() == 8; }
  bool is_vid() const { return v.index() == 9; }
  bool is_text() const { return is_str() || is_vid(); }   // a str, or a vid (which is its hex16 str to Python)
  const char* type_name() const;

  // Typed access; each throws a descriptive error on the wrong type.
  bool as_bool() const;
  int64_t as_int() const;              // a bool is NOT an int here (plans never mix them)
  const std::string& as_str() const;
  const StrPtr& str_ptr() const;       // the shared string itself (a copy is a pointer copy)
  const std::string& as_bytes() const; // the raw bytes of a bytes value
  const ValueList& as_list() const;    // list OR tuple items
  RowView as_row() const;              // the fields by reference: valid while this row lives
  uint64_t as_vid() const;
  std::string text() const;            // a str's chars, or a vid's hex16: what Python's str ops see (a copy)
  double as_float() const;

  // Python truthiness: None/False/0/""/[] are false.
  bool truthy() const;

  // Row field access. `field` throws on a missing key (row[x]); `get` returns null (row.get(x)).
  const Value& field(const std::string& name) const;
  const Value* find(const std::string& name) const;
  Value get(const std::string& name) const;
};

// The trailing array starts at the first multiple of alignof(Value) past the header (never an assumed 16).
constexpr size_t kRowValsOffset = (sizeof(RowData) + alignof(Value) - 1) / alignof(Value) * alignof(Value);
static_assert(alignof(Value) <= __STDCPP_DEFAULT_NEW_ALIGNMENT__ && alignof(RowData) <= __STDCPP_DEFAULT_NEW_ALIGNMENT__,
              "a row block comes from plain operator new: its alignment must cover the header and the values");
inline const Value* RowData::vals() const {
  return reinterpret_cast<const Value*>(reinterpret_cast<const char*>(this) + kRowValsOffset);
}
inline Value* RowData::vals() { return reinterpret_cast<Value*>(reinterpret_cast<char*>(this) + kRowValsOffset); }

// A row's values in place (RowView::values()): begin / end / size / [] over the block's array.
struct ValueSpan {
  const Value* b;
  size_t n;
  const Value* data() const { return b; }
  const Value* begin() const { return b; }
  const Value* end() const { return b + n; }
  size_t size() const { return n; }
  bool empty() const { return n == 0; }
  const Value& operator[](size_t i) const { return b[i]; }
};

// A row being built: the block for `shape` is allocated at once with every value null, filled by slot, then handed
// to a Value by done(). A throw before done() (a fill that throws) frees the block once, destroying its n values;
// done() twice, or a slot after done(), throws.
class RowSlots {
 public:
  explicit RowSlots(ShapePtr shape);
  RowSlots(RowSlots&& o) noexcept : p_(std::move(o.p_)), n_(o.n_) { o.n_ = 0; }
  RowSlots& operator=(RowSlots&&) = delete;   // a moved-into slot set would keep its own n over another block
  Value& operator[](size_t i) {               // inline: every builder's per-field write
    if (!p_.get()) throw_after_done();
    if (i >= n_) throw_out_of_range(i);
    return const_cast<RowData*>(p_.get())->vals()[i];   // the block is ours alone until done()
  }
  size_t size() const { return n_; }
  Value done();
 private:
  [[noreturn]] static void throw_after_done();
  [[noreturn]] void throw_out_of_range(size_t i) const;
  RowPtr p_;
  size_t n_ = 0;
};

// One field of a row as a reader sees it: (name, value), both references into the row.
using FieldRef = std::pair<const std::string&, const Value&>;

// A row's fields in order, by reference (Value::as_row): iterates as (name, value) pairs, so `for (auto& [k, v] :
// r.as_row())`, `kv.first` / `kv.second`, size(), empty() and [i] read as they did over RowFields. Valid while the row
// lives. to_fields() is the one copy, and explicit: a hidden copy per row is a cost, and a pointer into it dangles.
// The iterator is an INPUT iterator: *it is a pair held in the iterator and rebuilt by the next *it, so a reference
// from *it is valid until the iterator moves or is gone (a range-for's `auto& kv` is; `const auto& x = *view.begin()`
// is not). kv.first and kv.second themselves refer into the row and live as long as it does.
class RowView {
 public:
  class iterator {
   public:
    using iterator_category = std::input_iterator_tag;
    using value_type = FieldRef;
    using difference_type = std::ptrdiff_t;
    using pointer = const FieldRef*;
    using reference = const FieldRef&;
    iterator() : d_(nullptr), i_(0) {}
    iterator(const RowData* d, size_t i) : d_(d), i_(i) {}
    iterator(const iterator& o) : d_(o.d_), i_(o.i_) {}
    iterator& operator=(const iterator& o) { d_ = o.d_; i_ = o.i_; cur_.reset(); return *this; }
    reference operator*() const { cur_.emplace(d_->shape->names[i_], d_->vals()[i_]); return *cur_; }
    pointer operator->() const { return &**this; }
    iterator& operator++() { ++i_; return *this; }
    iterator operator++(int) { iterator t = *this; ++i_; return t; }
    bool operator==(const iterator& o) const { return i_ == o.i_ && d_ == o.d_; }
    bool operator!=(const iterator& o) const { return !(*this == o); }
   private:
    const RowData* d_;
    size_t i_;
    mutable std::optional<FieldRef> cur_;   // the pair *it hands out: an lvalue, so `auto& kv` binds to it
  };
  explicit RowView(const RowData* d) : d_(d) {}
  iterator begin() const { return iterator(d_, 0); }
  iterator end() const { return iterator(d_, d_->n); }
  size_t size() const { return d_->n; }
  bool empty() const { return d_->n == 0; }
  FieldRef operator[](size_t i) const { return {d_->shape->names[i], d_->vals()[i]}; }
  const ShapePtr& shape() const { return d_->shape; }
  ValueSpan values() const { return {d_->vals(), d_->n}; }
  RowFields to_fields() const;
 private:
  const RowData* d_;
};

// Python `==`: 1 == True, None == None, 1 == 1.0, int != str, list != tuple, dict by content; a vid == the str hex16(u).
bool equals(const Value& a, const Value& b);
// Python `<` ordering (-1/0/1); throws TypeError-style on None or mixed types.
int compare(const Value& a, const Value& b);
// The neutral total order used by `nest` (null < bool < int < str < list/tuple < row), never throws.
int neutral_compare(const Value& a, const Value& b);
size_t hash_value(const Value& v);     // consistent with equals()

struct ValueHash { size_t operator()(const Value& v) const { return hash_value(v); } };
struct ValueEq { bool operator()(const Value& a, const Value& b) const { return equals(a, b); } };

json to_json(const Value& v);          // rows -> objects (field order kept), tuples -> arrays
// The NEUTRAL content size (Python graph.value_bytes, the same rule on both sides): null 0,
// bool 1, int 8, str = its length in CODE POINTS (Python len(), not UTF-8 bytes), bytes = its length,
// list/tuple/row = the sum of their items. The result of a step is sized with this so a
// count's pre-aggregation payload (read amplification's denominator) is available on cpp.
// A float is 0: Python's content_bytes has no float case (a float is an in-memory derived value, never stored).
size_t neutral_bytes(const Value& v);
Value from_json(const json& j);        // arrays -> lists, objects -> rows, numbers -> int64 (a float is refused)

std::string repr(const Value& v);      // for error messages only

// Build a row by field name in one go: `make_row({{"_vid", v}, {"id", 3}})`. A repeated name
// keeps its first position and the LAST value, as a Python dict does.
Value make_row(RowFields f);
// Append or overwrite in place (dict assignment).
void row_set(RowFields& f, const std::string& name, Value value);
// A copy of `r` with `name` set (appended if new, replaced in place if present) -- Python's
// `{**r, name: value}`.
Value row_with(const Value& r, const std::string& name, Value value);
// `{**a, **b}`: b's fields overwrite a's (same-named keep a's position), new ones append.
Value row_merge(const Value& a, const Value& b, const std::string* skip_b = nullptr);

// A reader's row layout, made once per operator call from the names it sets on each row, in order; each row then
// pushes its values in that order into the one shared shape (NebulaGraph reads every row of a tag against one schema).
// A repeated name keeps its first position and the last value pushed, as row_set does. Immutable once made, so the
// lanes of one call share it; each lane builds its own Row.
class RowBuilder {
 public:
  explicit RowBuilder(const std::vector<std::string>& names);
  class Row {
   public:
    void push(Value v);                 // the next name's value
    Value& operator[](size_t k);        // the k-th name's value, once pushed (to overwrite it)
    Value done();                       // throws unless every name was pushed
   private:
    friend class RowBuilder;
    explicit Row(const RowBuilder* b);
    const RowBuilder* b_;
    RowSlots slots_;                    // the row's block itself: no vector per row
    size_t n_ = 0;
  };
  Row start() const { return Row(this); }
  const ShapePtr& shape() const { return shape_; }
 private:
  ShapePtr shape_;
  std::vector<size_t> slot_;            // the k-th name given -> its position in the shape
};

// row_with(r, name, v) over many rows: the output shape made once per input shape (one per call, not per row).
class RowWith {
 public:
  explicit RowWith(std::string name) : name_(std::move(name)) {}
  Value operator()(const Value& r, Value v);
 private:
  std::string name_;
  ShapePtr in_, out_;
  int idx_ = -1;
};

}  // namespace evalcpp
