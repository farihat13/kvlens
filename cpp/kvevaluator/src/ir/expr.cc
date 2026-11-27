#include "ir/expr.h"

#include <stdexcept>
#include <unordered_map>

#include "schema/schema.h"
#include "util/vid.h"

namespace evalcpp {

// civil-from-days (Howard Hinnant), days since 1970-01-01 -> y/m/d
static void civil_from_days(int64_t z, int64_t& y, unsigned& m, unsigned& d) {
  z += 719468;
  const int64_t era = (z >= 0 ? z : z - 146096) / 146097;
  const unsigned doe = static_cast<unsigned>(z - era * 146097);
  const unsigned yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
  y = static_cast<int64_t>(yoe) + era * 400;
  const unsigned doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
  const unsigned mp = (5 * doy + 2) / 153;
  d = doy - (153 * mp + 2) / 5 + 1;
  m = mp < 10 ? mp + 3 : mp - 9;
  if (m <= 2) y++;
}

static int64_t floordiv(int64_t a, int64_t b) {
  if (b == 0) throw std::runtime_error("ZeroDivisionError");
  int64_t q = a / b;
  if ((a % b != 0) && ((a < 0) != (b < 0))) q--;
  return q;
}

static int64_t days_of_ms(int64_t ms) {
  int64_t secs = floordiv(ms, 1000);
  return floordiv(secs, 86400);
}

int64_t month_of_ms(int64_t ms) {
  int64_t y; unsigned m, d;
  civil_from_days(days_of_ms(ms), y, m, d);
  return m;
}
int64_t day_of_ms(int64_t ms) {
  int64_t y; unsigned m, d;
  civil_from_days(days_of_ms(ms), y, m, d);
  return d;
}
int64_t year_of_ms(int64_t ms) {
  int64_t y; unsigned m, d;
  civil_from_days(days_of_ms(ms), y, m, d);
  return y;
}

// SQL LIKE with `%` only (Python planir/expr.py _like): the first part at the start, the last at the end, the parts
// between each at its leftmost place after the one before. Bytes, which equals characters without `_`.
bool Expr::like_match(const std::string& text, const std::string& pattern) {
  if (pattern.find('_') != std::string::npos)
    throw std::runtime_error("like: a pattern with '_' is not supported: " + pattern);
  std::vector<std::string> parts;
  size_t from = 0;
  for (size_t i; (i = pattern.find('%', from)) != std::string::npos; from = i + 1) parts.push_back(pattern.substr(from, i - from));
  parts.push_back(pattern.substr(from));
  if (parts.size() == 1) return text == pattern;
  const std::string& first = parts.front();
  const std::string& last = parts.back();
  if (text.size() < first.size() + last.size()) return false;
  const size_t end = text.size() - last.size();
  if (text.compare(0, first.size(), first) != 0 || text.compare(end, last.size(), last) != 0) return false;
  size_t pos = first.size();
  for (size_t k = 1; k + 1 < parts.size(); k++) {
    const std::string& part = parts[k];
    if (part.size() > end - pos) return false;
    size_t i = text.find(part, pos);
    if (i == std::string::npos || i + part.size() > end) return false;
    pos = i + part.size();
  }
  return true;
}

bool Expr::is_digit(const Value& v) {
  std::string s;
  if (v.is_text()) s = v.text();
  else if (v.is_int()) s = std::to_string(v.as_int());
  else if (v.is_bool()) s = v.as_bool() ? "True" : "False";
  else if (v.is_null()) s = "None";
  else s = repr(v);
  if (s.empty()) return false;
  for (unsigned char c : s) {
    if (c >= 0x80) throw std::runtime_error("is_digit: non-ASCII input not supported: " + s);
    if (c < '0' || c > '9') return false;
  }
  return true;
}

Value Expr::vid_of(const Schema& schema, const Value& label, const Value& raw) {
  int64_t id;
  if (raw.is_int()) id = raw.as_int();
  else if (raw.is_text()) {
    const std::string s = raw.text();
    try {
      size_t pos = 0;
      id = std::stoll(s, &pos);
      if (pos != s.size()) throw std::invalid_argument("trailing");
    } catch (...) {
      throw std::runtime_error("vid: raw id is not an integer: " + s);
    }
  } else throw std::runtime_error("vid: raw id must be int or str, got " + std::string(raw.type_name()));
  if (id < 0 || uint64_t(id) >= kVidMaxId)
    throw std::runtime_error("vid: id " + std::to_string(id) + " exceeds the 59-bit vid budget");
  uint64_t tag = uint64_t(schema.tag_of_label(label.as_str()));
  return Value::vid((tag << kVidShift) | uint64_t(id));   // a param's vid, as the stores' reads emit them
}

Value Expr::key_id(const Value& v) {
  if (v.is_vid()) return Value(int64_t(id_of_uid(v.as_vid())));
  if (!v.is_str()) return Value();
  auto u = uid_of(v.as_str());
  if (!u) return Value();
  return Value(int64_t(id_of_uid(*u)));
}

std::shared_ptr<Expr> Expr::compile(const json& ast, const Bindings& b) {
  auto e = std::make_shared<Expr>();
  e->schema_ = b.schema;
  if (!ast.is_object() || ast.size() != 1) {   // a bare literal
    e->op_ = Op::Lit;
    e->lit_ = from_json(ast);
    return e;
  }
  const std::string op = ast.begin().key();
  const json& arg = ast.begin().value();
  auto kids_of = [&](const json& a) {
    if (!a.is_array()) throw std::runtime_error("expr " + op + ": expected a list of operands");
    for (auto& x : a) e->kids_.push_back(compile(x, b));
  };
  auto one = [&](const json& a) { e->kids_.push_back(compile(a, b)); };
  static const std::map<std::string, Op> table = {
      {"eq", Op::Eq}, {"ne", Op::Ne}, {"lt", Op::Lt}, {"le", Op::Le}, {"gt", Op::Gt}, {"ge", Op::Ge},
      {"add", Op::Add}, {"sub", Op::Sub}, {"mul", Op::Mul}, {"floordiv", Op::FloorDiv}, {"div", Op::Div},
      {"concat", Op::Concat}, {"chain_root", Op::ChainRoot},
      {"and", Op::And}, {"or", Op::Or}, {"in", Op::In}, {"coalesce", Op::Coalesce},
      {"if", Op::If}, {"tuple", Op::Tuple}, {"vid", Op::Vid}, {"like", Op::Like}, {"map", Op::Map}};
  if (op == "field") { e->op_ = Op::Field; e->name_ = arg.get<std::string>(); return e; }
  if (op == "get") { e->op_ = Op::Get; e->name_ = arg.get<std::string>(); return e; }
  if (op == "lit") { e->op_ = Op::Lit; e->lit_ = from_json(arg); return e; }
  if (op == "param") {
    auto it = b.params.find(arg.get<std::string>());
    if (it == b.params.end()) throw std::runtime_error("unbound param '" + arg.get<std::string>() + "'");
    e->op_ = Op::Lit; e->lit_ = it->second; return e;
  }
  if (op == "let") {
    auto it = b.lets.find(arg.get<std::string>());
    if (it == b.lets.end()) throw std::runtime_error("unknown let '" + arg.get<std::string>() + "'");
    e->op_ = Op::Lit; e->lit_ = it->second; return e;
  }
  if (op == "not") { e->op_ = Op::Not; one(arg); return e; }
  if (op == "neg") { e->op_ = Op::Neg; one(arg); return e; }
  if (op == "isnull") { e->op_ = Op::IsNull; one(arg); return e; }
  if (op == "notnull") { e->op_ = Op::NotNull; one(arg); return e; }
  if (op == "is_digit") { e->op_ = Op::IsDigit; one(arg); return e; }
  if (op == "month_of") { e->op_ = Op::MonthOf; one(arg); return e; }
  if (op == "day_of") { e->op_ = Op::DayOf; one(arg); return e; }
  if (op == "year_of") { e->op_ = Op::YearOf; one(arg); return e; }
  if (op == "key_id") { e->op_ = Op::KeyId; one(arg); return e; }
  auto it = table.find(op);
  if (it == table.end()) throw std::runtime_error("unknown expression op: '" + op + "'");
  e->op_ = it->second;
  kids_of(arg);
  size_t n = e->kids_.size();
  switch (e->op_) {
    case Op::Eq: case Op::Ne: case Op::Lt: case Op::Le: case Op::Gt: case Op::Ge:
    case Op::Add: case Op::Sub: case Op::Mul: case Op::FloorDiv: case Op::Div: case Op::In: case Op::Vid: case Op::ChainRoot: case Op::Like: case Op::Map:
      if (n != 2) throw std::runtime_error("expr " + op + ": expects 2 operands");
      break;
    case Op::If:
      if (n != 3) throw std::runtime_error("expr if: expects [cond, then, else]");
      break;
    default: break;
  }
  return e;
}

static int64_t int_operand(const Value& v, const char* what) {
  if (v.is_int()) return v.as_int();
  if (v.is_bool()) return v.as_bool() ? 1 : 0;   // Python: True + 1 == 2
  throw std::runtime_error(std::string("TypeError: ") + what + " on " + v.type_name() + " " + repr(v));
}

// An int / bool / float operand as a double (float arithmetic: Python promotes int to float; exact below 2^53).
static double float_operand(const Value& v, const char* what) {
  if (v.is_float()) return v.as_float();
  return double(int_operand(v, what));
}

// One concat operand's text: an int or a str only, as Python's planir/expr.py (no bool / float / None, whose text the
// two backends could print differently).
static std::string concat_text(const Value& v) {
  if (v.is_text()) return v.text();
  if (v.is_int()) return std::to_string(v.as_int());
  throw std::runtime_error(std::string("TypeError: concat: int or str operands only, got ") + v.type_name() + " " + repr(v));
}

Value Expr::eval(const Value& row) const {
  switch (op_) {
    case Op::Lit: return lit_;
    case Op::Field: return row.field(name_);
    case Op::Get: return row.get(name_);
    case Op::Not: return Value(!kids_[0]->eval(row).truthy());
    case Op::Neg: {
      Value x = kids_[0]->eval(row);
      if (x.is_float()) return Value(-x.as_float());
      int64_t a = int_operand(x, "neg");
      if (a == INT64_MIN) throw std::runtime_error("neg: int64 overflow");
      return Value(-a);
    }
    case Op::IsNull: return Value(kids_[0]->eval(row).is_null());
    case Op::NotNull: return Value(!kids_[0]->eval(row).is_null());
    case Op::And:
      for (auto& k : kids_)
        if (!k->eval(row).truthy()) return Value(false);
      return Value(true);
    case Op::Or:
      for (auto& k : kids_)
        if (k->eval(row).truthy()) return Value(true);
      return Value(false);
    case Op::Tuple: {
      ValueList items;
      for (auto& k : kids_) items.push_back(k->eval(row));
      return Value::tuple(std::move(items));
    }
    case Op::Map: {   // the body per element, against the one-field row {"item": x}; the body sees no outer row
      Value xs = kids_[0]->eval(row);
      if (!xs.is_list() && !xs.is_tuple()) throw std::runtime_error(std::string("TypeError: map expects a list, got ") + xs.type_name());
      ValueList out;
      for (const Value& x : xs.as_list()) out.push_back(kids_[1]->eval(Value::row({{"item", x}})));
      return Value::list(std::move(out));
    }
    case Op::Coalesce:
      for (auto& k : kids_) {
        Value v = k->eval(row);
        if (!v.is_null()) return v;
      }
      return Value();
    case Op::If:
      return kids_[0]->eval(row).truthy() ? kids_[1]->eval(row) : kids_[2]->eval(row);
    case Op::In: {
      Value needle = kids_[0]->eval(row);
      Value hay = kids_[1]->eval(row);
      if (hay.is_text()) {
        if (!needle.is_text()) throw std::runtime_error("TypeError: 'in <string>' requires string as left operand");
        return Value(hay.text().find(needle.text()) != std::string::npos);
      }
      for (auto& x : hay.as_list())
        if (equals(x, needle)) return Value(true);
      return Value(false);
    }
    case Op::Like: {
      Value text = kids_[0]->eval(row), pattern = kids_[1]->eval(row);
      if (!text.is_text() || !pattern.is_text()) throw std::runtime_error("TypeError: like: str operands only");
      return Value(like_match(text.text(), pattern.text()));
    }
    case Op::IsDigit: return Value(is_digit(kids_[0]->eval(row)));
    case Op::MonthOf: return Value(month_of_ms(int_operand(kids_[0]->eval(row), "month_of")));
    case Op::DayOf: return Value(day_of_ms(int_operand(kids_[0]->eval(row), "day_of")));
    case Op::YearOf: return Value(year_of_ms(int_operand(kids_[0]->eval(row), "year_of")));
    case Op::KeyId: return key_id(kids_[0]->eval(row));
    case Op::Vid: {
      if (!schema_) throw std::runtime_error("vid: no schema bound");
      return vid_of(*schema_, kids_[0]->eval(row), kids_[1]->eval(row));
    }
    case Op::Eq: return Value(equals(kids_[0]->eval(row), kids_[1]->eval(row)));
    case Op::Ne: return Value(!equals(kids_[0]->eval(row), kids_[1]->eval(row)));
    case Op::Lt: return Value(compare(kids_[0]->eval(row), kids_[1]->eval(row)) < 0);
    case Op::Le: return Value(compare(kids_[0]->eval(row), kids_[1]->eval(row)) <= 0);
    case Op::Gt: return Value(compare(kids_[0]->eval(row), kids_[1]->eval(row)) > 0);
    case Op::Ge: return Value(compare(kids_[0]->eval(row), kids_[1]->eval(row)) >= 0);
    case Op::Div: {   // TRUE division (Python /): always a float
      Value av = kids_[0]->eval(row), bv = kids_[1]->eval(row);
      for (const Value* v : {&av, &bv})   // above 2^53 a double holds an int only approximately (Python refuses it too)
        if (v->is_int() && (v->as_int() > (int64_t(1) << 53) || v->as_int() < -(int64_t(1) << 53)))
          throw std::runtime_error("div: an int operand above 2^53 would be rounded before dividing");
      double x = float_operand(av, "div"), y = float_operand(bv, "div");
      if (y == 0.0) throw std::runtime_error("ZeroDivisionError: div by zero");
      return Value(x / y);
    }
    case Op::ChainRoot: {   // the end of start's parent chain through (child, parent) pairs; in memory, no KV op
      Value start = kids_[0]->eval(row);
      Value pv = kids_[1]->eval(row);
      std::unordered_map<Value, Value, ValueHash, ValueEq> parent;
      for (auto& p : pv.as_list()) {
        const ValueList& kv = p.as_list();
        if (kv.size() != 2) throw std::runtime_error("chain_root: a pair must have 2 items, got " + repr(p));
        parent.insert_or_assign(kv[0], kv[1]);   // a later pair wins, as Python's dict(pairs)
      }
      Value v = start;
      for (size_t i = 0; i <= parent.size(); i++) {
        auto it = parent.find(v);
        if (it == parent.end()) return v;
        v = it->second;
      }
      throw std::runtime_error("chain_root: a cycle through " + repr(start));
    }
    case Op::Concat: {
      std::string s;
      for (auto& k : kids_) s += concat_text(k->eval(row));
      return Value(std::move(s));
    }
    case Op::Add: case Op::Sub: case Op::Mul: case Op::FloorDiv: {
      Value a = kids_[0]->eval(row), b = kids_[1]->eval(row);
      if (op_ == Op::Add && a.is_text() && b.is_text()) return Value(a.text() + b.text());
      if (a.is_float() || b.is_float()) {   // Python: int op float is a float
        if (op_ == Op::FloorDiv) throw std::runtime_error("floordiv on a float: not in the value model");
        double x = float_operand(a, "arithmetic"), y = float_operand(b, "arithmetic");
        return Value(op_ == Op::Add ? x + y : (op_ == Op::Sub ? x - y : x * y));
      }
      int64_t x = int_operand(a, "arithmetic"), y = int_operand(b, "arithmetic"), r;
      bool ovf = false;
      if (op_ == Op::Add) ovf = __builtin_add_overflow(x, y, &r);
      else if (op_ == Op::Sub) ovf = __builtin_sub_overflow(x, y, &r);
      else if (op_ == Op::Mul) ovf = __builtin_mul_overflow(x, y, &r);
      else r = floordiv(x, y);
      if (ovf) throw std::runtime_error("int64 overflow in arithmetic");
      return Value(r);
    }
  }
  throw std::runtime_error("expr: unreachable");
}

}  // namespace evalcpp
