#include "ops/flow.h"

#include <algorithm>
#include <numeric>
#include <optional>
#include <set>
#include <stdexcept>
#include <unordered_map>
#include <unordered_set>

#include "util/uidset.h"
#include "util/vid.h"

namespace evalcpp {

using ValueSet = std::unordered_set<Value, ValueHash, ValueEq>;
template <class T>
using ValueMap = std::unordered_map<Value, T, ValueHash, ValueEq>;

static const std::set<std::string> kFlowOps = {
    "attach", "filter", "dedup", "as_row", "gate", "dedup_rows", "exclude", "count", "concat",
    "intersect", "annotate", "group_count", "filter_in", "flag_in", "derive", "group_top",
    "group_sum", "join_multi", "pluck", "project", "zip_rows", "join", "rename", "nest", "top_k",
    "left_join", "group_agg"};

bool is_flow_op(const std::string& op) { return kFlowOps.count(op) > 0; }

// A vid's uid: a vid, or its canonical string (16 lowercase hex chars). Two such values are equal exactly when their
// uids are (a vid == the str hex16(u)), and neither equals any other value: the fast paths below key on it.
static bool vid_key(const Value& v, uint64_t* u) {
  if (v.is_vid()) { *u = v.as_vid(); return true; }
  return v.is_str() && canonical_vid(v.as_str(), u);
}

// Membership over Values with Python equality, kept in uid space (util/uidset.h) while every
// inserted item is a vid or a canonical vid string -- the vid lists dedup / intersect / filter_in see.
// The first other item migrates the set to the general ValueSet; a vid key equals only the same
// uid, so the answer is identical in both modes.
class ValueMembers {
 public:
  explicit ValueMembers(size_t expected) : fast_(expected), expected_(expected) {}
  bool insert(const Value& v) {   // true = new
    if (fast_mode_) {
      uint64_t u;
      if (vid_key(v, &u)) return fast_.insert(u);
      migrate();
    }
    return slow_.insert(v).second;
  }
  bool contains(const Value& v) const {
    if (fast_mode_) {
      uint64_t u;
      return vid_key(v, &u) && fast_.contains(u);
    }
    return slow_.count(v) > 0;
  }
 private:
  void migrate() {
    slow_.reserve(expected_ * 2);
    fast_.for_each([&](uint64_t u) { slow_.insert(Value::vid(u)); });
    fast_ = UidSet();
    fast_mode_ = false;
  }
  bool fast_mode_ = true;
  UidSet fast_;
  ValueSet slow_;
  size_t expected_;
};

// --- per-call helpers. Rows of one step mostly share one shape, so each resolves a field's position or an output
// shape when the input shape changes and then works by position (an engine binds a column's position once per batch
// layout). Each lives inside one operator call: nothing is kept across steps or plans. A held ShapePtr keeps the
// shape it compares against alive, so a new shape can never reuse its address.

// row.field(name) / row.find(name), the name looked up once per row shape.
class FieldReader {
 public:
  explicit FieldReader(std::string name) : name_(std::move(name)) {}
  const Value* find(const Value& r) {
    RowView v = r.as_row();
    if (v.shape() != shape_) { shape_ = v.shape(); idx_ = shape_->index_of(name_); }
    return idx_ < 0 ? nullptr : &v.values()[size_t(idx_)];
  }
  const Value& operator()(const Value& r) {   // KeyError (Value::field's) when absent
    const Value* p = find(r);
    return p ? *p : r.field(name_);
  }
  Value get(const Value& r) {                 // null when absent (Value::get)
    const Value* p = find(r);
    return p ? *p : Value();
  }
 private:
  std::string name_;
  ShapePtr shape_;
  int idx_ = -1;
};

// row_merge(a, b, skip): {**a, **b} without b's `skip` field; the merged shape and where each of b's fields lands
// made once per (a shape, b shape).
class RowMerge {
 public:
  explicit RowMerge(const std::string* skip = nullptr) : skip_(skip ? std::optional<std::string>(*skip) : std::nullopt) {}
  Value operator()(const Value& a, const Value& b) {
    RowView va = a.as_row(), vb = b.as_row();
    if (va.shape() != sa_ || vb.shape() != sb_) {
      sa_ = va.shape();
      sb_ = vb.shape();
      std::vector<std::string> names = sa_->names;
      slot_.assign(sb_->names.size(), -1);
      for (size_t j = 0; j < sb_->names.size(); j++) {
        const std::string& n = sb_->names[j];
        if (skip_ && n == *skip_) continue;
        int i = sa_->index_of(n);               // a's names are unique and b's are: only a's can collide
        if (i >= 0) slot_[j] = i;
        else { slot_[j] = int(names.size()); names.push_back(n); }
      }
      out_ = names.size() == sa_->names.size() ? sa_ : make_shape(std::move(names));
    }
    RowSlots s(out_);                           // straight into the merged row's block
    for (size_t i = 0; i < va.size(); i++) s[i] = va.values()[i];
    for (size_t j = 0; j < slot_.size(); j++)
      if (slot_[j] >= 0) s[size_t(slot_[j])] = vb.values()[j];
    return s.done();
  }
 private:
  std::optional<std::string> skip_;
  ShapePtr sa_, sb_, out_;
  std::vector<int> slot_;                       // b's field j -> its position in the merged row, -1 = skipped
};

// Key -> int (a group or row number, the caller's), in uid space (UidMap) while every key is a vid key, else in a
// ValueMap; the first other key migrates it. Equal keys get one entry in both modes (vid_key), so the caller's
// numbering, kept in first-seen order, is the same either way. A hash join on integer keys.
class KeyIndex {
 public:
  explicit KeyIndex(size_t expected) : fast_(expected), expected_(expected) {}
  // the entry of `key`: (its int, inserted). A new key gets `fresh`
  std::pair<int*, bool> insert(const Value& key, int fresh) {
    if (fast_mode_) {
      uint64_t u;
      if (vid_key(key, &u)) return fast_.insert(u, fresh);
      migrate();
    }
    auto [it, ins] = slow_.emplace(key, fresh);
    return {&it->second, ins};
  }
  const int* find(const Value& key) const {
    if (fast_mode_) {
      uint64_t u;
      return vid_key(key, &u) ? fast_.find(u) : nullptr;   // a non-vid key equals no vid key
    }
    auto it = slow_.find(key);
    return it == slow_.end() ? nullptr : &it->second;
  }
 private:
  void migrate() {
    slow_.reserve(expected_ * 2);
    fast_.for_each([&](uint64_t u, int v) { slow_.emplace(Value::vid(u), v); });
    fast_ = UidMap<int>();
    fast_mode_ = false;
  }
  bool fast_mode_ = true;
  UidMap<int> fast_;
  ValueMap<int> slow_;
  size_t expected_;
};

static ValueMembers to_members(const ValueList& items) {
  ValueMembers s(items.size());
  for (auto& x : items) s.insert(x);
  return s;
}

// join_multi's optional tests on each merged pair: `where` (an expression, as filter's pred), then `where_key` (an
// expression) in the step output `where_in`, or not in `where_not_in` (as filter_in's `allowed`). A pair that fails is
// never kept, so a join whose pairs a filter would drop holds only the kept ones (bi17 at SF30)
struct PairTest {
  const Expr* where = nullptr;
  const Expr* key = nullptr;
  std::optional<ValueMembers> set;
  bool member = true;
  PairTest(const Step& s, const Args& a) {
    if (auto it = s.exprs.find("where"); it != s.exprs.end()) where = it->second.get();
    auto k = s.exprs.find("where_key");
    bool in = a.has("where_in"), not_in = a.has("where_not_in");
    if (in && not_in) throw std::runtime_error("join_multi: where_in and where_not_in together");
    if ((k != s.exprs.end()) != (in || not_in))
      throw std::runtime_error("join_multi: where_key goes with one of where_in, where_not_in");
    if (k == s.exprs.end()) return;
    key = k->second.get();
    member = in;
    set.emplace(to_members(a.list(in ? "where_in" : "where_not_in")));
  }
  bool keep(const Value& row) const {
    if (where && !where->eval(row).truthy()) return false;
    return !key || set->contains(key->eval(row)) == member;
  }
};

// An input list's items, handed on by move when the step holds the list alone (Args::owned_list: the executor moved
// it in, this step is its last reader), else copied. Read items[i] before push_to(.., i); push each index at most once.
// Straight into the output: a returned temporary would add a move and a destruction per item (a vid's copy is one)
struct Input {
  const ValueList& items;
  ValueList* own;
  Input(const Args& a, Args* o, const std::string& k) : items(a.list(k)), own(o ? o->owned_list(k) : nullptr) {}
  void push_to(ValueList& out, size_t i) const {
    if (own) out.push_back(std::move((*own)[i]));
    else out.push_back(items[i]);
  }
};

static Value dedup(const Input& in) {
  const ValueList& items = in.items;
  ValueMembers seen(items.size());
  ValueList out;
  out.reserve(items.size());
  for (size_t i = 0; i < items.size(); i++)
    if (seen.insert(items[i])) in.push_to(out, i);
  return Value::list(std::move(out));
}

// One comparator for the chained stable sorts of top_k: keys in priority order, then the
// original position (so equal rows keep input order, exactly as Python's stable sort does).
// Each row's key values are read once (`kv`, row-major); a missing one (null) is read again
// with Value::field when a comparison reaches it, so it throws the KeyError it always did.
// Never copied: kv holds a pointer per row and key, and the sorts copy a comparator by value (libstdc++'s heap does on
// every pop: a top-10 over 287k date-ordered rows copied 2.3 MB per pop, 13 s). They get a reference (cmp below);
// passing `ord` itself no longer compiles.
struct RowOrder {
  const ValueList& rows;
  std::vector<std::pair<std::string, bool>> keys;   // (field, desc)
  std::vector<const Value*> kv;
  RowOrder(const ValueList& r) : rows(r) {}
  RowOrder(const RowOrder&) = delete;
  RowOrder& operator=(const RowOrder&) = delete;
  const Value& key(size_t i, size_t j) const {
    const Value* p = kv[i * keys.size() + j];
    return p ? *p : rows[i].field(keys[j].first);
  }
  bool operator()(size_t i, size_t j) const {
    for (size_t k = 0; k < keys.size(); k++) {
      int c = compare(key(i, k), key(j, k));
      if (c) return keys[k].second ? c > 0 : c < 0;
    }
    return i < j;
  }
};

Value top_k(const Value& rows_v, const ValueList& keys_v, const Value& k_v, bool keep_ties) {
  const ValueList& rows = rows_v.as_list();
  RowOrder ord(rows);
  for (auto& kv : keys_v) {
    const ValueList& pair = kv.as_list();
    if (pair.size() != 2) throw std::runtime_error("top_k: a key is [field, asc|desc]");
    ord.keys.emplace_back(pair[0].as_str(), pair[1].as_str() == "desc");
  }
  ord.kv.assign(rows.size() * ord.keys.size(), nullptr);
  for (size_t j = 0; j < ord.keys.size(); j++) {
    FieldReader fr(ord.keys[j].first);
    for (size_t i = 0; i < rows.size(); i++)
      if (rows[i].is_row()) ord.kv[i * ord.keys.size() + j] = fr.find(rows[i]);
  }
  std::vector<size_t> idx(rows.size());
  std::iota(idx.begin(), idx.end(), 0);
  auto cmp = [&ord](size_t i, size_t j) { return ord(i, j); };   // what the sorts copy: one pointer
  bool bounded = !k_v.is_null() && !keep_ties && size_t(k_v.as_int()) < rows.size();
  if (bounded) {
    // the bounded-heap form: same result as a full stable sort cut at k, O(n log k)
    size_t k = size_t(k_v.as_int());
    std::partial_sort(idx.begin(), idx.begin() + k, idx.end(), cmp);
    idx.resize(k);
  } else {
    std::sort(idx.begin(), idx.end(), cmp);   // total order (position tie-break) -> stable
  }
  ValueList out;
  out.reserve(idx.size());
  for (size_t i : idx) out.push_back(rows[i]);
  if (k_v.is_null() || out.size() <= size_t(k_v.as_int())) return Value::list(std::move(out));
  size_t k = size_t(k_v.as_int());
  if (keep_ties) {
    const std::string& pf = ord.keys[0].first;
    const Value& boundary = out[k - 1].field(pf);
    size_t i = k;
    while (i < out.size() && equals(out[i].field(pf), boundary)) i++;
    out.resize(i);
    return Value::list(std::move(out));
  }
  out.resize(k);
  return Value::list(std::move(out));
}

// `own`: the same args when the step may consume its inputs (run_flow_op_owned), else null
static Value flow_op(const Step& s, const Args& a, Args* own) {
  const std::string& op = s.op;
  if (op == "attach") {
    const ValueList& rows = a.list("rows");
    const ValueList& vals = a.list("values");
    RowWith with(a.str("into"));
    ValueList out;
    size_t n = std::min(rows.size(), vals.size());
    out.reserve(n);
    for (size_t i = 0; i < n; i++) out.push_back(with(rows[i], vals[i]));
    return Value::list(std::move(out));
  }
  if (op == "filter") {
    auto it = s.exprs.find("pred");
    if (it == s.exprs.end()) throw std::runtime_error("filter: missing pred");
    Input in(a, own, "rows");
    ValueList out;
    out.reserve(in.items.size());
    for (size_t i = 0; i < in.items.size(); i++)
      if (it->second->eval(in.items[i]).truthy()) in.push_to(out, i);
    return Value::list(std::move(out));
  }
  if (op == "dedup") return dedup(Input(a, own, "items"));
  if (op == "as_row") return Value::list({make_row({{a.str("field"), a.get("value")}})});
  if (op == "gate") {
    const ValueList& items = a.list("items");
    return a.get("when").truthy() ? Value::list(items) : Value::list({});
  }
  if (op == "dedup_rows") {
    FieldReader key(a.has("field") ? a.str("field") : "_vid");
    Input in(a, own, "rows");
    ValueMembers seen(in.items.size());
    ValueList out;
    out.reserve(in.items.size());
    for (size_t i = 0; i < in.items.size(); i++)
      if (seen.insert(key(in.items[i]))) in.push_to(out, i);
    return Value::list(std::move(out));
  }
  if (op == "exclude") {
    ValueMembers rm = to_members(a.list("remove"));
    Input in(a, own, "items");
    ValueList out;
    out.reserve(in.items.size());
    for (size_t i = 0; i < in.items.size(); i++)
      if (!rm.contains(in.items[i])) in.push_to(out, i);
    return Value::list(std::move(out));
  }
  if (op == "count") return Value(int64_t(a.list("items").size()));
  if (op == "concat") {
    Input l(a, own, "a"), r(a, own, "b");
    ValueList out;
    out.reserve(l.items.size() + r.items.size());
    for (size_t i = 0; i < l.items.size(); i++) l.push_to(out, i);
    for (size_t i = 0; i < r.items.size(); i++) r.push_to(out, i);
    return Value::list(std::move(out));
  }
  if (op == "intersect") {
    ValueMembers keep = to_members(a.list("other"));
    ValueList out;
    out.reserve(a.list("items").size());
    for (auto& x : a.list("items"))
      if (keep.contains(x)) out.push_back(x);
    return Value::list(std::move(out));
  }
  if (op == "annotate") {
    std::string field = a.str("field");
    const Value& value = a.get("value");
    // make_row({{"_vid", x}, {field, value}}): one shape for the call; a field named _vid keeps one field, the value
    ShapePtr shape = make_shape(field == "_vid" ? std::vector<std::string>{"_vid"} : std::vector<std::string>{"_vid", field});
    ValueList out;
    out.reserve(a.list("items").size());
    for (auto& x : a.list("items")) {
      RowSlots s(shape);
      if (field == "_vid") s[0] = value;
      else { s[0] = x; s[1] = value; }
      out.push_back(s.done());
    }
    return Value::list(std::move(out));
  }
  if (op == "group_count") {
    KeyIndex pos(a.list("items").size());
    std::vector<std::pair<Value, int64_t>> groups;
    for (auto& x : a.list("items")) {
      auto [g, fresh] = pos.insert(x, int(groups.size()));
      if (fresh) groups.emplace_back(x, 1);
      else groups[size_t(*g)].second++;
    }
    ShapePtr shape = make_shape({"item", "count"});
    ValueList out;
    out.reserve(groups.size());
    for (auto& g : groups) {
      RowSlots s(shape);
      s[0] = g.first;
      s[1] = Value(g.second);
      out.push_back(s.done());
    }
    return Value::list(std::move(out));
  }
  if (op == "filter_in" || op == "flag_in") {
    FieldReader key(a.str("field"));
    ValueMembers keep = to_members(a.list("allowed"));
    ValueList out;
    if (op == "filter_in") {
      Input in(a, own, "rows");
      out.reserve(in.items.size());
      for (size_t i = 0; i < in.items.size(); i++)
        if (keep.contains(key(in.items[i]))) in.push_to(out, i);
    } else {
      RowWith with(a.str("into"));
      out.reserve(a.list("rows").size());
      for (auto& r : a.list("rows")) out.push_back(with(r, Value(keep.contains(key(r)))));
    }
    return Value::list(std::move(out));
  }
  if (op == "derive") {
    auto it = s.exprs.find("fn");
    if (it == s.exprs.end()) throw std::runtime_error("derive: missing fn");
    RowWith with(a.str("into"));
    ValueList out;
    out.reserve(a.list("rows").size());
    for (auto& r : a.list("rows")) out.push_back(with(r, it->second->eval(r)));
    return Value::list(std::move(out));
  }
  if (op == "group_top") {
    FieldReader by(a.str("by"));
    Value sorted = top_k(a.get("rows"), a.list("keys"), Value(), false);
    ValueMembers seen(sorted.as_list().size());
    ValueList out;
    out.reserve(sorted.as_list().size());
    for (auto& r : sorted.as_list())
      if (seen.insert(by(r))) out.push_back(r);
    return Value::list(std::move(out));
  }
  if (op == "group_sum") {
    FieldReader by(a.str("by")), field(a.str("field"));
    KeyIndex pos(a.list("rows").size());
    std::vector<std::pair<Value, int64_t>> groups;
    for (auto& r : a.list("rows")) {
      const Value& g = by(r);
      const Value& fv = field(r);
      int64_t v = fv.is_bool() ? (fv.as_bool() ? 1 : 0) : fv.as_int();   // Python sums True as 1
      auto [gi, fresh] = pos.insert(g, int(groups.size()));
      if (fresh) groups.emplace_back(g, v);
      else if (__builtin_add_overflow(groups[size_t(*gi)].second, v, &groups[size_t(*gi)].second))
        throw std::runtime_error("group_sum: int64 overflow in sum");   // as group_agg
    }
    ShapePtr shape = make_shape({"item", "sum"});
    ValueList out;
    out.reserve(groups.size());
    for (auto& g : groups) {
      RowSlots s(shape);
      s[0] = g.first;
      s[1] = Value(g.second);
      out.push_back(s.done());
    }
    return Value::list(std::move(out));
  }
  if (op == "join_multi") {
    // Every (left, right) pair with equal keys, in left order, then right order within a left row. Hash the SMALLER
    // side, as `join` does: the table costs an insert per row, a probe costs a lookup (ic5's post_forum at sf3: 0.6-0.7 M
    // left rows, 2.6-2.8 M right). Either way the output is the same rows in the same order. With PairTest's tests,
    // the pairs that pass them, in the same order.
    std::string ron = a.str("right_on");
    FieldReader lkey(a.str("left_on")), rkey(ron);
    const ValueList& left = a.list("left");
    const ValueList& right = a.list("right");
    RowMerge merge(&ron);
    const PairTest test(s, a);
    auto put = [&](ValueList& out, Value row) { if (test.keep(row)) out.push_back(std::move(row)); };
    ValueList out;
    if (right.size() <= left.size()) {
      KeyIndex by(right.size());                   // right key -> its group of right rows, in right order
      std::vector<std::vector<const Value*>> groups;
      for (auto& r : right) {
        auto [g, fresh] = by.insert(rkey(r), int(groups.size()));
        if (fresh) groups.emplace_back();
        groups[size_t(*g)].push_back(&r);
      }
      for (auto& l : left) {
        const int* g = by.find(lkey(l));
        if (!g) continue;
        for (const Value* r : groups[size_t(*g)]) put(out, merge(l, *r));
      }
    } else {
      KeyIndex at(left.size());                    // left key -> its slot
      std::vector<int> slot(left.size());          // each left row's slot
      int n = 0;
      for (size_t i = 0; i < left.size(); i++) {
        auto [g, fresh] = at.insert(lkey(left[i]), n);
        if (fresh) n++;
        slot[i] = *g;
      }
      std::vector<std::vector<const Value*>> groups(static_cast<size_t>(n));   // per left key: its right rows, in right order
      for (auto& r : right) {
        const int* g = at.find(rkey(r));
        if (g) groups[size_t(*g)].push_back(&r);
      }
      for (size_t i = 0; i < left.size(); i++)
        for (const Value* r : groups[size_t(slot[i])]) put(out, merge(left[i], *r));
    }
    return Value::list(std::move(out));
  }
  if (op == "pluck") {
    FieldReader key(a.str("field"));
    ValueList out;
    out.reserve(a.list("rows").size());
    for (auto& r : a.list("rows")) out.push_back(key(r));
    return Value::list(std::move(out));
  }
  if (op == "project") {
    std::vector<std::string> fields = a.str_list("fields");
    std::vector<std::string> names;   // a repeated name collapses: same r.get either time, so
    for (auto& n : fields)            // collapse the list once, not per row
      if (std::find(names.begin(), names.end(), n) == names.end()) names.push_back(n);
    std::vector<FieldReader> readers;
    for (auto& n : names) readers.emplace_back(n);
    ShapePtr shape = make_shape(names);
    ValueList out;
    out.reserve(a.list("rows").size());
    for (auto& r : a.list("rows")) {
      RowSlots s(shape);
      for (size_t i = 0; i < readers.size(); i++) s[i] = readers[i].get(r);
      out.push_back(s.done());
    }
    return Value::list(std::move(out));
  }
  if (op == "zip_rows") {
    const ValueList& l = a.list("left");
    const ValueList& r = a.list("right");
    RowMerge merge;
    ValueList out;
    size_t n = std::min(l.size(), r.size());
    out.reserve(n);
    for (size_t i = 0; i < n; i++) out.push_back(merge(l[i], r[i]));
    return Value::list(std::move(out));
  }
  if (op == "join") {
    // Python: by = {r[right_on]: r for r in right} (last duplicate wins); [l | by[l[left_on]] for l in left].
    // Hash the SMALLER side: the table costs an allocation per entry, a probe costs none, and
    // LDBC joins are lopsided (ic3: 2.75M right rows for a few thousand left rows).
    std::string lon = a.str("left_on");
    std::string ron = a.has("right_on") ? a.str("right_on") : "_vid";
    FieldReader lkey(lon), rkey(ron);
    const ValueList& left = a.list("left");
    const ValueList& right = a.list("right");
    std::vector<const Value*> match(left.size(), nullptr);   // the right row of each left row
    if (right.size() <= left.size()) {
      KeyIndex by(right.size());                  // right key -> the last right row carrying it
      for (size_t j = 0; j < right.size(); j++) *by.insert(rkey(right[j]), int(j)).first = int(j);
      for (size_t i = 0; i < left.size(); i++) {
        const int* j = by.find(lkey(left[i]));
        if (j) match[i] = &right[size_t(*j)];
      }
    } else {
      KeyIndex at(left.size());                   // left key -> its group of left rows
      std::vector<std::vector<size_t>> groups;
      for (size_t i = 0; i < left.size(); i++) {
        auto [g, fresh] = at.insert(lkey(left[i]), int(groups.size()));
        if (fresh) groups.emplace_back();
        groups[size_t(*g)].push_back(i);
      }
      for (auto& r : right) {             // in right order, so the last duplicate wins
        const int* g = at.find(rkey(r));
        if (!g) continue;
        for (size_t i : groups[size_t(*g)]) match[i] = &r;
      }
    }
    RowMerge merge(&ron);
    ValueList out;
    out.reserve(left.size());
    for (size_t i = 0; i < left.size(); i++) {
      if (!match[i]) throw std::runtime_error("join: KeyError, no right row for " + repr(left[i].field(lon)));
      out.push_back(merge(left[i], *match[i]));
    }
    return Value::list(std::move(out));
  }
  if (op == "left_join") {
    // Python: by = {r[right_on]: r for r in right}; an unmatched left row gets `default`'s fields instead
    // of a right row's (BI's OPTIONAL MATCH + count). Matched rows merge as `join` does.
    std::string ron = a.has("right_on") ? a.str("right_on") : "_vid";
    FieldReader lkey(a.str("left_on")), rkey(ron);
    const ValueList& right = a.list("right");
    KeyIndex by(right.size());
    for (size_t j = 0; j < right.size(); j++) *by.insert(rkey(right[j]), int(j)).first = int(j);   // the last duplicate wins, as Python's dict
    Value dflt = a.has("default") ? a.get("default") : Value::row({});
    RowMerge merge(&ron), merge_default;
    ValueList out;
    out.reserve(a.list("left").size());
    for (auto& l : a.list("left")) {
      const int* j = by.find(lkey(l));
      out.push_back(j ? merge(l, right[size_t(*j)]) : merge_default(l, dflt));
    }
    return Value::list(std::move(out));
  }
  if (op == "group_agg") {
    // GROUP BY `by` fields with several aggregates: aggs = {name: [kind, field]}, kind count / sum / min / max.
    // One row per distinct key tuple, first-seen order, carrying the by fields then the aggregates --
    // the Python oracle's dict order. sum follows group_sum (a bool sums as 0 / 1); min / max use `compare`
    // (Python's `<`: it throws on None or mixed types, as the oracle's `r[field] < g[name]` does); a slot is None
    // only before the group's first row.
    std::vector<std::string> by = a.str_list("by");
    struct Agg { std::string name, kind, field; };
    std::vector<Agg> aggs;
    for (auto& kv : a.get("aggs").as_row()) {
      const ValueList& spec = kv.second.as_list();
      if (spec.size() != 2) throw std::runtime_error("group_agg: an aggregate is [kind, field]");
      aggs.push_back({kv.first, spec[0].as_str(), spec[1].is_null() ? std::string() : spec[1].as_str()});
      const std::string& k = aggs.back().kind;
      if (k != "count" && k != "sum" && k != "min" && k != "max")
        throw std::runtime_error("group_agg: unknown aggregate kind '" + k + "'");
    }
    std::vector<FieldReader> by_readers, agg_readers;
    for (auto& f : by) by_readers.emplace_back(f);
    for (auto& ag : aggs) agg_readers.emplace_back(ag.field);
    std::vector<std::string> names = by;          // one shape for every group row: the by fields, then the aggregates
    for (auto& ag : aggs) names.push_back(ag.name);
    ShapePtr shape = make_shape(std::move(names));
    KeyIndex pos(a.list("rows").size());
    std::vector<ValueList> groups;   // by fields, then one slot per aggregate
    for (auto& r : a.list("rows")) {
      ValueList key;
      key.reserve(by.size());
      for (auto& fr : by_readers) key.push_back(fr(r));
      auto [gi, fresh] = pos.insert(Value::tuple(key), int(groups.size()));
      if (fresh) {
        ValueList g = key;
        for (auto& ag : aggs) g.push_back((ag.kind == "count" || ag.kind == "sum") ? Value(int64_t(0)) : Value());
        groups.push_back(std::move(g));
      }
      ValueList& g = groups[size_t(*gi)];
      for (size_t i = 0; i < aggs.size(); i++) {
        Value& slot = g[by.size() + i];
        const Agg& ag = aggs[i];
        if (ag.kind == "count") { slot = Value(slot.as_int() + 1); continue; }
        const Value& fv = agg_readers[i](r);
        if (ag.kind == "sum") {
          if (fv.is_float() || slot.is_float()) {
            double x = slot.is_float() ? slot.as_float() : double(slot.as_int());
            double y = fv.is_float() ? fv.as_float() : (fv.is_bool() ? (fv.as_bool() ? 1.0 : 0.0) : double(fv.as_int()));
            slot = Value(x + y);
          } else {
            int64_t v = fv.is_bool() ? (fv.as_bool() ? 1 : 0) : fv.as_int(), s;
            if (__builtin_add_overflow(slot.as_int(), v, &s)) throw std::runtime_error("group_agg: int64 overflow in sum");
            slot = Value(s);
          }
        } else if (slot.is_null() || (ag.kind == "min" ? compare(fv, slot) < 0 : compare(fv, slot) > 0)) {
          slot = fv;
        }
      }
    }
    ValueList out;
    out.reserve(groups.size());
    for (auto& g : groups) out.push_back(Value::row(shape, std::move(g)));
    return Value::list(std::move(out));
  }
  if (op == "rename") {
    const Value& names = a.get("names");
    // a row's own names are unique, so only a name that is a rename TARGET can collide (a
    // renamed field landing on an existing name, or vice versa): those take row_set's rule (the first position, the
    // last value), every other field is a plain append -- same rows as row_set on every field
    std::vector<std::string> targets;
    for (auto& kv : names.as_row())
      if (kv.second.is_str()) targets.push_back(kv.second.as_str());   // a non-str target still throws below, on a row that carries the key
    auto is_target = [&](const std::string& n) { return std::find(targets.begin(), targets.end(), n) != targets.end(); };
    // per input shape, once: the output shape and where each input field lands (or that no field is renamed)
    ShapePtr in_shape, out_shape;
    std::vector<size_t> slot;
    bool renames = false, in_place = false;   // in_place: every field keeps its position (no collision)
    Input rows(a, own, "rows");
    ValueList out;
    out.reserve(rows.items.size());
    for (size_t ri = 0; ri < rows.items.size(); ri++) {
      const Value& r = rows.items[ri];
      RowView in = r.as_row();
      if (in.shape() != in_shape) {
        in_shape = in.shape();
        renames = false;
        std::vector<std::string> out_names;
        slot.assign(in.size(), 0);
        in_place = true;
        for (size_t i = 0; i < in.size(); i++) {
          const std::string& old_name = in_shape->names[i];
          const Value* nn = names.find(old_name);
          const std::string& name = nn ? nn->as_str() : old_name;
          renames = renames || nn;
          int at = is_target(name) ? int(std::find(out_names.begin(), out_names.end(), name) - out_names.begin()) : int(out_names.size());
          if (at == int(out_names.size())) out_names.push_back(name);
          slot[i] = size_t(at);
          in_place = in_place && slot[i] == i;
        }
        out_shape = renames ? make_shape(std::move(out_names)) : in_shape;
      }
      if (!renames) {                  // no field of this row is renamed: the row itself, shared (rows are immutable)
        rows.push_to(out, ri);
        continue;
      }
      // a row this step holds alone, every field in place: the same block under the new names (an engine renames a
      // batch's columns, not its tuples). The row is the input list's only reference to it, so no one sees the change
      if (in_place && rows.own) {
        Value& mine = (*rows.own)[ri];
        RowPtr* rp = std::get_if<RowPtr>(&mine.v);
        if (rp && rp->sole()) {
          rp->reshape(out_shape);
          mine.label = -1;             // as a new row (the copy path below): a row's tag lives on its _vid
          out.push_back(std::move(mine));
          continue;
        }
      }
      RowSlots s(out_shape);
      for (size_t i = 0; i < in.size(); i++) s[slot[i]] = in.values()[i];   // in order: a collision keeps the last
      out.push_back(s.done());
    }
    return Value::list(std::move(out));
  }
  if (op == "nest") {
    FieldReader on(a.str("on")), child_on(a.str("child_on"));
    std::vector<FieldReader> fields;
    for (auto& f : a.str_list("fields")) fields.emplace_back(f);
    RowWith with(a.str("into"));
    ValueMap<ValueSet> by;
    by.reserve(a.list("children").size() * 2);
    for (auto& c : a.list("children")) {
      ValueList t;
      for (auto& fr : fields) t.push_back(fr.get(c));
      by[child_on(c)].insert(Value::tuple(std::move(t)));
    }
    ValueList out;
    out.reserve(a.list("rows").size());
    for (auto& r : a.list("rows")) {
      ValueList items;
      auto it = by.find(on(r));
      if (it != by.end()) {
        for (auto& t : it->second) items.push_back(t);
        std::sort(items.begin(), items.end(), [](const Value& x, const Value& y) { return neutral_compare(x, y) < 0; });
      }
      out.push_back(with(r, Value::list(std::move(items))));
    }
    return Value::list(std::move(out));
  }
  if (op == "top_k") return top_k(a.get("rows"), a.list("keys"), a.opt("k"), a.flag("keep_ties", false));
  throw std::runtime_error("unknown flow op '" + op + "'");
}

Value run_flow_op(const Step& s, const Args& a) { return flow_op(s, a, nullptr); }
Value run_flow_op_owned(const Step& s, Args& a) { return flow_op(s, a, &a); }

}  // namespace evalcpp
