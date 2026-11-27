#include "value/value.h"

#include "util/base64.h"

#include <cmath>
#include <cstdint>
#include <functional>
#include <new>
#include <stdexcept>
#include <string_view>
#include <type_traits>

namespace evalcpp {

namespace {
// A vid's text, hex16(u), in a stack buffer: a vid compares, hashes and sizes as this string, with no allocation.
struct VidText {
  char c[16];
  explicit VidText(uint64_t u) {
    static const char* d = "0123456789abcdef";
    for (int i = 15; i >= 0; i--) { c[i] = d[u & 0xf]; u >>= 4; }
  }
  std::string_view sv() const { return std::string_view(c, 16); }
};

// The text of a str or a vid (a vid's in `buf`): the two are one kind to equality, order and hashing.
std::string_view text_of(const Value& x, std::optional<VidText>& buf) {
  if (x.is_vid()) return buf.emplace(std::get<Vid>(x.v).u).sv();
  return *std::get<StrPtr>(x.v);
}
bool textual(const Value& x) { return x.is_str() || x.is_vid(); }

// A row's storage; the wrong type throws as_row()'s error.
const RowData& row_data(const Value& x) {
  if (!x.is_row()) x.as_row();
  return *std::get<RowPtr>(x.v);
}
int sign(int c) { return c < 0 ? -1 : (c > 0 ? 1 : 0); }
}  // namespace

int RowShape::index_of(const std::string& name) const {
  for (size_t i = 0; i < names.size(); i++)
    if (names[i] == name) return int(i);
  return -1;
}

ShapePtr make_shape(std::vector<std::string> names) {
  auto sh = std::make_shared<RowShape>();
  sh->names = std::move(names);
  return sh;
}

RowFields RowView::to_fields() const {
  RowFields f;
  f.reserve(size());
  for (size_t i = 0; i < size(); i++) f.emplace_back(d_->shape->names[i], d_->vals()[i]);
  return f;
}

// --- the row block (value.h RowData): one allocation per row, freed once by the last RowPtr ---
static_assert(std::is_nothrow_default_constructible_v<Value> && std::is_nothrow_destructible_v<Value>,
              "a row block constructs its n values null at once and destroys them in release: neither may throw");

void RowPtr::reshape(ShapePtr shape) {
  if (!sole()) throw std::logic_error("RowPtr::reshape: the row is shared");
  if (!shape || shape->names.size() != p_->n) throw std::logic_error("RowPtr::reshape: the shape's size differs");
  p_->shape = std::move(shape);
}

void RowPtr::release(RowData* p) {
  if (p->refs.fetch_sub(1, std::memory_order_acq_rel) != 1) return;
  Value* v = p->vals();
  for (uint32_t i = 0; i < p->n; i++) v[i].~Value();
  size_t bytes = kRowValsOffset + size_t(p->n) * sizeof(Value);
  p->~RowData();
  ::operator delete(static_cast<void*>(p), bytes);   // sized: tcmalloc skips the size lookup
}

RowSlots::RowSlots(ShapePtr shape) {
  size_t n = shape->names.size();
  if (n > UINT32_MAX) throw std::runtime_error("a row of " + std::to_string(n) + " fields: more than a row block holds");
  void* mem = ::operator new(kRowValsOffset + n * sizeof(Value));   // a throw here: nothing built, nothing to free
  RowData* d = new (mem) RowData{{1}, uint32_t(n), std::move(shape)};
  Value* v = d->vals();
  for (size_t i = 0; i < n; i++) new (v + i) Value();   // nothrow (static_assert above): every value is built
  p_ = RowPtr(d);
  n_ = n;
}

void RowSlots::throw_after_done() { throw std::runtime_error("RowSlots: a slot after done()"); }
void RowSlots::throw_out_of_range(size_t i) const {
  throw std::runtime_error("RowSlots: slot " + std::to_string(i) + " of a row of " + std::to_string(n_));
}

Value RowSlots::done() {
  if (!p_.get()) throw std::runtime_error("RowSlots: done() twice");
  return Value(std::move(p_));
}

Value Value::row(RowFields f) {
  std::vector<std::string> names;
  names.reserve(f.size());
  for (auto& kv : f) names.push_back(std::move(kv.first));
  RowSlots s(make_shape(std::move(names)));
  for (size_t i = 0; i < f.size(); i++) s[i] = std::move(f[i].second);
  return s.done();
}

Value Value::row(ShapePtr shape, ValueList values) {
  if (shape->names.size() != values.size())
    throw std::runtime_error("a row of " + std::to_string(values.size()) + " values for a shape of " +
                             std::to_string(shape->names.size()) + " names");
  RowSlots s(std::move(shape));
  for (size_t i = 0; i < values.size(); i++) s[i] = std::move(values[i]);
  return s.done();
}

const char* Value::type_name() const {
  static const char* names[] = {"null", "bool", "int", "str", "list", "tuple", "row", "float", "bytes", "vid"};
  return names[v.index()];
}

bool Value::as_bool() const {
  if (!is_bool()) throw std::runtime_error(std::string("expected bool, got ") + type_name());
  return std::get<bool>(v);
}
int64_t Value::as_int() const {
  if (!is_int()) throw std::runtime_error(std::string("expected int, got ") + type_name() + ": " + repr(*this));
  return std::get<int64_t>(v);
}
const std::string& Value::as_str() const {
  if (!is_str()) throw std::runtime_error(std::string("expected str, got ") + type_name() + ": " + repr(*this));
  return (*std::get<StrPtr>(v));
}
const StrPtr& Value::str_ptr() const {
  if (!is_str()) throw std::runtime_error(std::string("expected str, got ") + type_name() + ": " + repr(*this));
  return std::get<StrPtr>(v);
}
const std::string& Value::as_bytes() const {
  if (!is_bytes()) throw std::runtime_error(std::string("expected bytes, got ") + type_name() + ": " + repr(*this));
  return *std::get<Bytes>(v).data;
}
const ValueList& Value::as_list() const {
  if (is_list()) return *std::get<ListPtr>(v);
  if (is_tuple()) return *std::get<Tuple>(v).items;
  throw std::runtime_error(std::string("expected list, got ") + type_name() + ": " + repr(*this));
}
RowView Value::as_row() const {
  if (!is_row()) throw std::runtime_error(std::string("expected row, got ") + type_name() + ": " + repr(*this));
  return RowView(std::get<RowPtr>(v).get());
}
uint64_t Value::as_vid() const {
  if (!is_vid()) throw std::runtime_error(std::string("expected vid, got ") + type_name() + ": " + repr(*this));
  return std::get<Vid>(v).u;
}
std::string Value::text() const {
  if (is_vid()) return std::string(VidText(std::get<Vid>(v).u).sv());
  return as_str();                       // the wrong-type error otherwise
}
double Value::as_float() const {
  if (!is_float()) throw std::runtime_error(std::string("expected float, got ") + type_name() + ": " + repr(*this));
  return std::get<double>(v);
}

bool Value::truthy() const {
  switch (v.index()) {
    case 0: return false;
    case 1: return std::get<bool>(v);
    case 2: return std::get<int64_t>(v) != 0;
    case 3: return !(*std::get<StrPtr>(v)).empty();
    case 4: return !std::get<ListPtr>(v)->empty();
    case 5: return !std::get<Tuple>(v).items->empty();
    case 6: return std::get<RowPtr>(v)->n != 0;
    case 7: return std::get<double>(v) != 0.0;
    case 8: return !std::get<Bytes>(v).data->empty();
    case 9: return true;   // its text is 16 chars: never empty
  }
  return false;
}

const Value* Value::find(const std::string& name) const {
  const RowData& d = row_data(*this);
  int i = d.shape->index_of(name);
  return i < 0 ? nullptr : &d.vals()[size_t(i)];
}
const Value& Value::field(const std::string& name) const {
  const Value* p = find(name);
  if (!p) throw std::runtime_error("KeyError: row has no field '" + name + "' (row: " + repr(*this) + ")");
  return *p;
}
Value Value::get(const std::string& name) const {
  const Value* p = find(name);
  return p ? *p : Value();
}

// bool and int compare numerically (Python: True == 1).
static bool numeric(const Value& x, int64_t& out) {
  if (x.is_int()) { out = std::get<int64_t>(x.v); return true; }
  if (x.is_bool()) { out = std::get<bool>(x.v) ? 1 : 0; return true; }
  return false;
}

// An int against a double, EXACTLY (Python compares int with float by value, never by rounding the int): -1 / 0 / 1.
static int cmp_int_double(int64_t i, double d) {
  if (std::isnan(d)) throw std::runtime_error("NaN in a comparison: not in the value model");
  if (d >= 9223372036854775808.0) return -1;      // 2^63: above every int64
  if (d < -9223372036854775808.0) return 1;
  double fl = std::floor(d);
  int64_t f = int64_t(fl);                          // in range: the cast is exact
  if (i != f) return i < f ? -1 : 1;
  return d > fl ? -1 : 0;                           // i == floor(d): smaller only if d has a fraction
}

// Two numbers (int / bool / float), when either is a float: -1 / 0 / 1, exact as Python. False if either is not a number.
static bool num_cmp(const Value& a, const Value& b, int& out) {
  int64_t x, y;
  bool ai = numeric(a, x), bi = numeric(b, y);
  if (!(ai || a.is_float()) || !(bi || b.is_float())) return false;
  if (a.is_float() && b.is_float()) {
    double p = std::get<double>(a.v), q = std::get<double>(b.v);
    if (std::isnan(p) || std::isnan(q)) throw std::runtime_error("NaN in a comparison: not in the value model");
    out = p < q ? -1 : (p > q ? 1 : 0);
  } else if (a.is_float()) {
    out = -cmp_int_double(y, std::get<double>(a.v));
  } else {
    out = cmp_int_double(x, std::get<double>(b.v));
  }
  return true;
}

bool equals(const Value& a, const Value& b) {
  int64_t x, y;
  if (numeric(a, x) && numeric(b, y)) return x == y;
  int c;
  if ((a.is_float() || b.is_float()) && num_cmp(a, b, c)) return c == 0;
  if (a.is_vid() && b.is_vid()) return std::get<Vid>(a.v).u == std::get<Vid>(b.v).u;
  if (textual(a) && textual(b)) {
    std::optional<VidText> x, y;
    return text_of(a, x) == text_of(b, y);
  }
  if (a.v.index() != b.v.index()) return false;
  switch (a.v.index()) {
    case 0: return true;
    case 8: return a.as_bytes() == b.as_bytes();
    case 4: case 5: {
      const ValueList& la = a.as_list();
      const ValueList& lb = b.as_list();
      if (la.size() != lb.size()) return false;
      for (size_t i = 0; i < la.size(); i++)
        if (!equals(la[i], lb[i])) return false;
      return true;
    }
    case 6: {
      const RowData& ra = *std::get<RowPtr>(a.v);
      const RowData& rb = *std::get<RowPtr>(b.v);
      if (ra.n != rb.n) return false;
      if (ra.shape == rb.shape) {          // one shape: field by field, in place
        for (size_t i = 0; i < ra.n; i++)
          if (!equals(ra.vals()[i], rb.vals()[i])) return false;
        return true;
      }
      for (size_t i = 0; i < ra.n; i++) {   // a dict: by name, in any order
        int j = rb.shape->index_of(ra.shape->names[i]);
        if (j < 0 || !equals(ra.vals()[i], rb.vals()[size_t(j)])) return false;
      }
      return true;
    }
  }
  return false;
}

int compare(const Value& a, const Value& b) {
  int64_t x, y;
  if (numeric(a, x) && numeric(b, y)) return x < y ? -1 : (x > y ? 1 : 0);
  int c;
  if ((a.is_float() || b.is_float()) && num_cmp(a, b, c)) return c;
  if (a.is_vid() && b.is_vid()) {   // fixed-width hex: the text order is the number order
    uint64_t x = std::get<Vid>(a.v).u, y = std::get<Vid>(b.v).u;
    return x < y ? -1 : (x > y ? 1 : 0);
  }
  if (textual(a) && textual(b)) {
    std::optional<VidText> x, y;
    return sign(text_of(a, x).compare(text_of(b, y)));
  }
  if (a.is_bytes() && b.is_bytes()) {   // Python: bytes order by their unsigned bytes, as std::string::compare does
    int c = a.as_bytes().compare(b.as_bytes());
    return c < 0 ? -1 : (c > 0 ? 1 : 0);
  }
  if ((a.is_list() && b.is_list()) || (a.is_tuple() && b.is_tuple())) {
    const ValueList& la = a.as_list();
    const ValueList& lb = b.as_list();
    size_t n = la.size() < lb.size() ? la.size() : lb.size();
    for (size_t i = 0; i < n; i++) {
      int c = compare(la[i], lb[i]);
      if (c) return c;
    }
    return la.size() < lb.size() ? -1 : (la.size() > lb.size() ? 1 : 0);
  }
  throw std::runtime_error(std::string("TypeError: '<' not supported between ") + a.type_name() +
                           " and " + b.type_name() + " (" + repr(a) + " vs " + repr(b) + ")");
}

int neutral_compare(const Value& a, const Value& b) {
  // rank: null 0 < bool 1 < int / float 2 < str 3 < bytes 4 < list/tuple 5 < row 6
  auto rank = [](const Value& x) {
    switch (x.v.index()) {
      case 0: return 0;
      case 1: return 1;
      case 2: case 7: return 2;
      case 3: case 9: return 3;
      case 8: return 4;
      case 4: case 5: return 5;
      default: return 6;
    }
  };
  int ra = rank(a), rb = rank(b);
  if (ra != rb) return ra < rb ? -1 : 1;
  switch (ra) {
    case 0: return 0;
    case 1: case 2: {
      int c;
      if ((a.is_float() || b.is_float()) && num_cmp(a, b, c)) return c;
      int64_t x = 0, y = 0;
      numeric(a, x); numeric(b, y);
      return x < y ? -1 : (x > y ? 1 : 0);
    }
    case 3: {   // str or vid
      if (a.is_vid() && b.is_vid()) {
        uint64_t x = std::get<Vid>(a.v).u, y = std::get<Vid>(b.v).u;
        return x < y ? -1 : (x > y ? 1 : 0);
      }
      std::optional<VidText> x, y;
      return sign(text_of(a, x).compare(text_of(b, y)));
    }
    case 4: {   // bytes
      int c = a.as_bytes().compare(b.as_bytes());
      return c < 0 ? -1 : (c > 0 ? 1 : 0);
    }
    case 5: {
      const ValueList& la = a.as_list();
      const ValueList& lb = b.as_list();
      size_t n = la.size() < lb.size() ? la.size() : lb.size();
      for (size_t i = 0; i < n; i++) {
        int c = neutral_compare(la[i], lb[i]);
        if (c) return c;
      }
      return la.size() < lb.size() ? -1 : (la.size() > lb.size() ? 1 : 0);
    }
    default: {
      std::string x = repr(a), y = repr(b);   // rows: by their printed form (never used by plans)
      int c = x.compare(y);
      return c < 0 ? -1 : (c > 0 ? 1 : 0);
    }
  }
}

size_t hash_value(const Value& v) {
  int64_t n;
  if (numeric(v, n)) return std::hash<int64_t>()(n) ^ 0x9e3779b9;
  if (v.is_float()) {   // consistent with equals(): an integral float hashes as its int (Python: hash(2.0) == hash(2))
    double d = std::get<double>(v.v);
    if (d >= -9223372036854775808.0 && d < 9223372036854775808.0 && d == std::floor(d))   // range first: the cast is UB
      return std::hash<int64_t>()(int64_t(d)) ^ 0x9e3779b9;                                 // outside it (NaN fails it)
    return std::hash<double>()(d);
  }
  switch (v.v.index()) {
    case 0: return 0x51ed270b;
    case 3: return std::hash<std::string>()((*std::get<StrPtr>(v.v)));
    case 9: return std::hash<std::string_view>()(VidText(std::get<Vid>(v.v).u).sv());   // == the hash of its str
    case 8: return std::hash<std::string>()(v.as_bytes()) ^ 0x62797465;   // never equal to a str: keep the hashes apart
    case 4: case 5: {
      size_t h = 0x345678;
      for (auto& x : v.as_list()) h = h * 1000003 ^ hash_value(x);
      return h;
    }
    case 6: {
      size_t h = 0x7654321;   // order-insensitive, like a frozenset of items
      const RowData& d = *std::get<RowPtr>(v.v);
      for (size_t i = 0; i < d.n; i++)
        h ^= std::hash<std::string>()(d.shape->names[i]) * 31 + hash_value(d.vals()[i]);
      return h;
    }
  }
  return 0;
}

json to_json(const Value& v) {
  switch (v.v.index()) {
    case 0: return nullptr;
    case 1: return std::get<bool>(v.v);
    case 2: return std::get<int64_t>(v.v);
    case 7: return std::get<double>(v.v);
    case 3: return (*std::get<StrPtr>(v.v));
    case 9: return std::string(VidText(std::get<Vid>(v.v).u).sv());
    case 8: return json{{"$bytes", base64_encode(v.as_bytes())}};
    case 4: case 5: {
      json a = json::array();
      for (auto& x : v.as_list()) a.push_back(to_json(x));
      return a;
    }
    case 6: {
      json o = json::object();
      const RowData& d = *std::get<RowPtr>(v.v);
      for (size_t i = 0; i < d.n; i++) o[d.shape->names[i]] = to_json(d.vals()[i]);
      return o;
    }
  }
  return nullptr;
}

size_t neutral_bytes(const Value& v) {
  switch (v.v.index()) {
    case 0: return 0;
    case 1: return 1;
    case 2: return 8;
    case 7: return 0;   // Python's content_bytes has no float case: a float is in-memory only, never stored
    case 3: {
      size_t n = 0;
      for (unsigned char c : *std::get<StrPtr>(v.v)) n += (c & 0xC0) != 0x80;   // UTF-8 lead bytes
      return n;
    }
    case 8: return v.as_bytes().size();
    case 9: return 16;   // its str: 16 hex chars, one code point each
    case 4: case 5: {
      size_t n = 0;
      for (auto& x : v.as_list()) n += neutral_bytes(x);
      return n;
    }
    case 6: {
      size_t n = 0;
      const RowData& d = *std::get<RowPtr>(v.v);
      for (size_t i = 0; i < d.n; i++) n += neutral_bytes(d.vals()[i]);
      return n;
    }
  }
  return 0;
}

Value from_json(const json& j) {
  if (j.is_null()) return Value();
  if (j.is_boolean()) return Value(j.get<bool>());
  if (j.is_number_integer()) return Value(j.get<int64_t>());
  if (j.is_number_unsigned()) return Value(int64_t(j.get<uint64_t>()));
  // a float comes only from the grammar's `div`, never from a plan, a param or a response: refused here
  if (j.is_number_float()) throw std::runtime_error("floats are not part of the value model: " + j.dump());
  if (j.is_string()) return Value(j.get<std::string>());
  if (j.is_array()) {
    ValueList items;
    items.reserve(j.size());
    for (auto& x : j) items.push_back(from_json(x));
    return Value::list(std::move(items));
  }
  if (j.contains("$bytes")) {   // a bytes value: {"$bytes": "<base64>"} and nothing else (a row never has that field)
    if (j.size() != 1 || !j["$bytes"].is_string())
      throw std::runtime_error("a {\"$bytes\": ...} object must hold exactly one base64 string: " + j.dump());
    return Value::bytes(base64_decode(j["$bytes"].get<std::string>()));
  }
  RowFields f;
  for (auto it = j.begin(); it != j.end(); ++it) f.emplace_back(it.key(), from_json(it.value()));
  return Value::row(std::move(f));
}

std::string repr(const Value& v) {
  std::string s = to_json(v).dump();
  if (s.size() > 200) s = s.substr(0, 200) + "...";
  return s;
}

void row_set(RowFields& f, const std::string& name, Value value) {
  for (auto& kv : f)
    if (kv.first == name) { kv.second = std::move(value); return; }
  f.emplace_back(name, std::move(value));
}

Value make_row(RowFields f) {
  RowFields out;
  out.reserve(f.size());
  for (auto& kv : f) row_set(out, kv.first, std::move(kv.second));
  return Value::row(std::move(out));
}

Value row_with(const Value& r, const std::string& name, Value value) {
  const RowData& src = row_data(r);
  int i = src.shape->index_of(name);
  ShapePtr shape = src.shape;
  if (i < 0) {                          // appended: a new shape (RowWith makes it once per call)
    std::vector<std::string> names = src.shape->names;
    names.push_back(name);
    shape = make_shape(std::move(names));
  }
  RowSlots s(std::move(shape));         // the copy goes straight into the new row's block
  for (size_t k = 0; k < src.n; k++) s[k] = src.vals()[k];
  s[i >= 0 ? size_t(i) : src.n] = std::move(value);
  return s.done();
}

RowBuilder::RowBuilder(const std::vector<std::string>& names) {
  std::vector<std::string> uniq;
  slot_.reserve(names.size());
  for (auto& n : names) {
    size_t i = 0;
    while (i < uniq.size() && uniq[i] != n) i++;
    if (i == uniq.size()) uniq.push_back(n);
    slot_.push_back(i);
  }
  shape_ = make_shape(std::move(uniq));
}

RowBuilder::Row::Row(const RowBuilder* b) : b_(b), slots_(b->shape_) {}

void RowBuilder::Row::push(Value v) {
  if (n_ == b_->slot_.size())
    throw std::runtime_error("RowBuilder: a push past the " + std::to_string(n_) + " names of the row");
  slots_[b_->slot_[n_++]] = std::move(v);
}

Value& RowBuilder::Row::operator[](size_t k) {
  if (k >= n_) throw std::runtime_error("RowBuilder: name " + std::to_string(k) + " read before it was pushed");
  return slots_[b_->slot_[k]];
}

Value RowBuilder::Row::done() {
  if (n_ != b_->slot_.size())
    throw std::runtime_error("RowBuilder: " + std::to_string(n_) + " values pushed for " +
                             std::to_string(b_->slot_.size()) + " names");
  return slots_.done();
}

Value RowWith::operator()(const Value& r, Value v) {
  RowView in = r.as_row();
  if (in.shape() != in_) {
    in_ = in.shape();
    idx_ = in_->index_of(name_);
    if (idx_ >= 0) out_ = in_;                // replaced in place: the same shape
    else {
      std::vector<std::string> names = in_->names;
      names.push_back(name_);
      out_ = make_shape(std::move(names));
    }
  }
  RowSlots s(out_);
  for (size_t k = 0; k < in.size(); k++) s[k] = in.values()[k];
  s[idx_ >= 0 ? size_t(idx_) : in.size()] = std::move(v);
  return s.done();
}

Value row_merge(const Value& a, const Value& b, const std::string* skip_b) {
  const RowData& ra = row_data(a);
  const RowData& rb = row_data(b);
  std::vector<std::string> names;
  names.reserve(ra.n + rb.n);           // the upper bound: one allocation per merged row's shape
  names.assign(ra.shape->names.begin(), ra.shape->names.end());
  for (size_t j = 0; j < rb.n; j++) {   // pass 1: the names (a's names are unique and b's are: only a's collide)
    const std::string& n = rb.shape->names[j];
    if (!(skip_b && n == *skip_b) && ra.shape->index_of(n) < 0) names.push_back(n);
  }
  RowSlots s(make_shape(std::move(names)));
  for (size_t i = 0; i < ra.n; i++) s[i] = ra.vals()[i];
  size_t next = ra.n;
  for (size_t j = 0; j < rb.n; j++) {   // pass 2: b's values, in the same order as pass 1's names
    const std::string& n = rb.shape->names[j];
    if (skip_b && n == *skip_b) continue;
    int i = ra.shape->index_of(n);
    s[i >= 0 ? size_t(i) : next++] = rb.vals()[j];
  }
  return s.done();
}

}  // namespace evalcpp
