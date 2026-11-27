// The closed plan-IR expression grammar (Python planir/expr.py), compiled once per step
// into a node tree and evaluated per row with Python's semantics.
#pragma once
#include <map>
#include <memory>
#include <string>
#include <vector>

#include "value/value.h"

namespace evalcpp {

class Schema;

struct Bindings {
  std::map<std::string, Value> params;
  std::map<std::string, Value> lets;
  const Schema* schema = nullptr;   // for {"vid": [label, raw]}: label -> vid tag
};

class Expr {
 public:
  // Compile an AST. `param`/`let` references are resolved to literals now.
  static std::shared_ptr<Expr> compile(const json& ast, const Bindings& b);
  Value eval(const Value& row) const;

  // Grammar helpers shared with the interpreter.
  static Value vid_of(const Schema& schema, const Value& label, const Value& raw);   // {"vid": ...}
  static Value key_id(const Value& v);                                                // {"key_id": ...}
  static bool is_digit(const Value& v);                                               // str(x).isdigit()
  static bool like_match(const std::string& text, const std::string& pattern);        // {"like": ...}: `%` only

 private:
  enum class Op {
    Lit, Field, Get, Eq, Ne, Lt, Le, Gt, Ge, And, Or, Not, In, IsNull, NotNull,
    Add, Sub, Mul, FloorDiv, Neg, Coalesce, If, Tuple, IsDigit, MonthOf, DayOf, YearOf, KeyId, Vid, Div, Concat, ChainRoot, Like, Map
  };
  Op op_ = Op::Lit;
  Value lit_;
  std::string name_;
  std::vector<std::shared_ptr<Expr>> kids_;
  const Schema* schema_ = nullptr;
};

// month / day-of-month (UTC) of an epoch-milliseconds value, integer floor division.
int64_t month_of_ms(int64_t ms);
int64_t day_of_ms(int64_t ms);
int64_t year_of_ms(int64_t ms);

}  // namespace evalcpp
