// The KV-op contract: every KV op a plan step may name, its arguments with their defaults, and what it refuses -- one
// table every format reads (formats/*/format.cc run_kv_op), so a default or a refusal is written once. A format
// supplies only what its layout decides (the index walk, the row fetch, the neighbour read, the label probe).
// The table is the data file inputs/contract/kv_ops.json, compiled in; Python reads the same file. `kvevaluator ops`
// prints it.
#pragma once
#include <algorithm>
#include <cstdint>
#include <string>
#include <vector>

#include "ir/plan.h"
#include "value/value.h"

namespace evalcpp::kv {

// one argument of an op: its name, whether a plan must give it, its default when it may be left out (JSON text;
// empty: none, the op reads it as absent)
struct OpArg {
  std::string name;
  bool required;
  std::string default_json;
};

// one op. every_format: every format serves it (false: only the formats that list it, e.g. index_range_buckets)
struct OpSpec {
  std::string name;
  std::string kind;   // "read" or "write"
  bool every_format;
  std::vector<OpArg> args;
  std::vector<std::string> refusals;   // what a step is refused for, in words: the parsers below enforce order, k and
                                       // id_order; a format enforces the rest (dir: parse_dirs; labels; keep; reads)
};

const std::vector<OpSpec>& ops();                  // the table, reads first, then writes
const OpSpec* find_op(const std::string& op);      // null when the table has no such op
bool is_read_op(const std::string& op);            // a read of the table (every format's or an optional one)
bool is_every_format_read_op(const std::string& op);
bool is_write_op(const std::string& op);
json ops_json();                                   // `kvevaluator ops`

// --- the arguments, parsed once, with the table's defaults --------------------------------------------------------

inline constexpr const char* kDefaultDir = "out";
std::string dir_of(const Args& a);                 // "dir", default out
std::string dir_of(const json& a);                 // the same on a step's raw args (predict mode)

struct NeighborsArgs {
  const ValueList* srcs;
  std::string etype, dir;
  std::vector<std::string> edge_props;
  bool pairs;
  std::vector<std::string> labels;                 // "label": the sources' label, when the plan knows it
};
NeighborsArgs neighbors_args(const Args& a);

struct WalkArgs {                                  // read_chase (max = max_hops), read_closure (max = max_levels)
  const ValueList* srcs;
  std::string etype, dir;
  Value max;
};
WalkArgs chase_args(const Args& a);
WalkArgs closure_args(const Args& a);

struct IndexRangeArgs {                            // read_index_range, index_range_buckets
  std::vector<std::string> labels;                 // one walk per label, in the plan's order (empty: a format whose
                                                   // index is per predicate walks it whole; the others refuse)
  std::string prop;
  int64_t lo, hi;
};
IndexRangeArgs index_range_args(const Args& a, const std::string& op);   // `op`: kept for the error text

struct TopKArgs {
  Value vids;                                      // null: the whole index (global)
  std::string prop;
  int64_t k;                                       // >= 1
  Value hi;                                        // null: no upper bound
  std::vector<std::string> labels;                 // empty: as for IndexRangeArgs
  std::vector<std::string> props;                  // the winners' props asked; empty: none
  bool id_desc;                                    // ties by id descending (id_order=desc), else ascending
};
TopKArgs top_k_args(const Args& a);

// The order every top-k answers in (the LDBC shape): by value descending, ties by id ascending (descending under
// id_order=desc), equal rows in the order the walk gave them. Two stable sorts, the second deciding.
template <class Row, class ValueOf, class IdOf>
void top_k_order(std::vector<Row>& rows, bool id_desc, ValueOf value_of, IdOf id_of) {
  std::stable_sort(rows.begin(), rows.end(),
                   [&](const Row& a, const Row& b) { return id_desc ? id_of(a) > id_of(b) : id_of(a) < id_of(b); });
  std::stable_sort(rows.begin(), rows.end(), [&](const Row& a, const Row& b) { return value_of(a) > value_of(b); });
}

}  // namespace evalcpp::kv
