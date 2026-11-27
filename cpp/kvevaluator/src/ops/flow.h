// The in-memory (0-KV) flow operators, one function per op, with the Python semantics of
// formats/base.py. Shared by every format.
#pragma once
#include <string>

#include "ir/plan.h"
#include "value/value.h"

namespace evalcpp {

bool is_flow_op(const std::string& op);
Value run_flow_op(const Step& step, const Args& a);
// the same, and an input the args hold alone (Args::owned_list: the executor moved it in) may be consumed: its rows
// handed on by move, a renamed row re-shaped in place. The executor's form; `a`'s lists are spent after it
Value run_flow_op_owned(const Step& step, Args& a);

// top_k's ordering, shared with group_top: keys = [[field, "asc"|"desc"], ...]
Value top_k(const Value& rows, const ValueList& keys, const Value& k, bool keep_ties);

}  // namespace evalcpp
