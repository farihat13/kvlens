// Helpers every format's KV ops need and none owns: the shapes the IR contract fixes
// (neighbor items, dedup of inputs, key-derived ids, direction words) and the composed ops
// that are pure functions of read_neighbors. A format directory includes this, never a sibling.
#pragma once
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include "exec/tasks.h"
#include "formats/format.h"
#include "formats/rows.h"
#include "ir/plan.h"
#include "ops/kv_contract.h"
#include "schema/schema.h"
#include "store/counting.h"
#include "util/uidset.h"
#include "value/value.h"

namespace evalcpp {

// The unified absence rule: a scan's last landing -- off-prefix, past a
// bound, past the end of the store, or an entry the walk stopped on without consuming -- is an
// absence miss, declared to the seam (Cursor::mark_miss) so its key never counts as read.
// Every prefix/range loop calls it on exit; only a loop that breaks AFTER consuming its landing
// skips it (that landing's value was read: a hit).
inline void end_scan(Cursor& cur) { cur.mark_miss(); }

// the distinct strings of a vid list, first occurrence order (Python dict.fromkeys) -- the
// input's own shared strings (no copy); dedup runs in uid space while every item is a
// canonical vid (util/uidset.h VidStrSet), with string equality either way
std::vector<StrPtr> distinct_strs(const ValueList& vs);
// A fan-out's per-source lists joined into one list, in order (with `second`: a source's first list, then its second).
// The items of a list nobody else holds are moved, not copied.
// PROTOTYPE (sizing only): the move is a const_cast (a Value's list is shared_ptr<const ...>); the real change gives
// the range primitive a result it may consume.
Value joined_lists(std::vector<Value>& per_src, std::vector<Value>* second = nullptr);
// The task index each input vid maps to, given the DISTINCT list its fan-out ran over (one task
// per distinct vid, first-occurrence order): EMPTY when the input is already distinct (the
// identity, the common case), else one index per input row. Replaces the string-keyed
// vid -> row map every read_vertex_props re-assembled its output through.
std::vector<size_t> task_index_of(const ValueList& vids, const std::vector<StrPtr>& distinct);
// an encoded vid's uid, or a descriptive error naming the format
uint64_t uid_or_throw(const std::string& format, const std::string& vid);
uint64_t uid_or_throw(const std::string& format, const Value& vid);   // a vid, or its encoded str

// A step's distinct input vids in uid space, one pass over a UidMap (the converted families' read path, in place of
// distinct_strs + distinct_values + task_index_of): `uids` in first-seen order; `values`, each as the read outputs it
// (a vid carrying its first TAGGED occurrence's label, distinct_values' rule; strict: two labels on one vid throw);
// `at`, where each input landed (input order, duplicates included; empty when every input is distinct). An input is
// a vid or its canonical str (16 lowercase hex chars); anything else throws, never a silent merge of two spellings.
struct DistinctVids {
  std::vector<uint64_t> uids;
  std::vector<Value> values;
  std::vector<size_t> at;
};
DistinctVids distinct_vids(const std::string& format, const ValueList& vs, bool strict);
Value key_id_value(uint64_t uid);   // the LDBC id a vid's key carries (Expr::key_id), from the uid
// "id" is key-derived in every format: Expr::key_id on the vid
Value key_id_value(const std::string& vid);
// a vertex label's stored columns: its declared props in schema order. Never `id`: the vid IS the id
// (key_id_value), so no format stores a copy (the loader's `id` prop is ignored by name).
std::vector<ColDecl> vertex_cols(const VertexDecl& v);
// a vertex's props minus `id` (the loader and the write params carry it): what a format that stores the props
// as given (arangodb's documents) stores -- vertex_cols' rule for a format with no column list to filter by
Value without_key_id(const Value& props);
// a decoded field list's value of `name` (its first), null when absent: Value::row(f).get(name) without the row
Value field_or_null(const RowFields& f, const std::string& name);
// a props row's names: _vid, then the props (a RowBuilder's names for one vertex's props row)
std::vector<std::string> props_names(const std::vector<std::string>& props);
// the same with `id` second when asked (the readers that take id from the key first): _vid, id?, the rest in order
std::vector<std::string> props_names_id_first(bool want_id, const std::vector<std::string>& row_props);
// read_neighbors' items (base.neighbor_item): the dst, or a row {src?, dst, edge props...}, every row of one list over
// one shape (made once per list). The src and dst keep the labels they carry (Value::label).
class NeighborItems {
 public:
  NeighborItems(const std::vector<std::string>& edge_props, bool pairs);
  Value operator()(const Value& src, const Value& dst, const Value* props) const;
  Value operator()(const Value& src, const Value& dst, const RowFields& props) const;   // props as decoded, no row
 private:
  const std::vector<std::string>& edge_props_;
  bool pairs_;
  std::optional<RowBuilder> rb_;      // none: an item is the bare dst
};
// read_neighbors' items, one NeighborItems per worker (WorkerSlot::id): a worker's rows share one shape, and no two
// workers bump one shape's count per row (8 workers on one shared shape made a P=8 hop step 1.9x slower)
class WorkerItems {
 public:
  WorkerItems(int P, const std::vector<std::string>& edge_props, bool pairs) {
    v_.reserve(size_t(P));
    for (int i = 0; i < P; i++) v_.emplace_back(edge_props, pairs);
  }
  const NeighborItems& of(const WorkerSlot& slot) const { return v_.at(size_t(slot.id)); }
 private:
  std::vector<NeighborItems> v_;
};
// dir word -> the directions walked (true = out), in Python's order: out, in, both
std::vector<bool> parse_dirs(const std::string& op, const std::string& dir);
// One read of a read_edge_exists step. The question per row: is there an edge between the row's candidate c and an
// anchor a, either way. The PLAN lists the reads that answer it, in order (the step's `reads`); a format does each
// read by the layout of the one (etype, dir) family it names, and nothing else:
//   dir  out | in     the family read
//   at   anchor       the anchors' keys of that family: out = a -> c, in = c -> a
//        cand         the candidates' keys:             out = c -> a, in = a -> c
//   unless_found      only for the candidates no EARLIER read of the step found. The invariant: this depends on which
//                     candidates the earlier reads found -- the same set in every layout -- never on how a layout read
//                     them. A read with no candidate left reads nothing.
// A read runs over every anchor before the next read starts.
struct EdgeRead {
  bool out, at_anchor, unless_found;
};
// the step's reads; throws when the step has none (the direction-less form is gone: a plan names its reads)
std::vector<EdgeRead> edge_reads(const Args& a, const std::string& fmt);

// read_edge_exists's meaning, once (Python base.read_edge_exists): the anchors (`others`, or the via walk's), the
// distinct candidates, each read in the plan's order over the candidates it is for (unless_found: the ones no earlier
// read found; none left: nothing read), a run of consecutive anchor-side list reads that are not unless_found read as
// one, and each row with `into` = found. The format supplies its reads; the fields below are its choices, named so
// that a difference between formats is seen here, not inside a body.
struct EdgeExistsReads {
  std::string fmt;
  bool strict_tags = false;    // distinct_vids' strict flag (Per-type CSR: the step's strict_tags)
  bool carry_labels = false;   // the owners handed to a list read carry their rows' labels (Per-type CSR: a label
                               // opens one family)
  // the via walk when the step gives no `others`: via_etype's out lists of via_srcs (the format's used-bytes rule)
  std::function<ValueList(const ValueList& via_srcs, const std::string& via_etype)> via;
  // true: read r as each owner's list, read once; false: a point probe per (candidate, anchor) until a hit
  std::function<bool(const EdgeRead& r)> list_like = [](const EdgeRead&) { return true; };
  // the neighbours of every anchor in the directions `outs` (true = out); group 1: the step's batch, 2 + i: read i's
  std::function<ValueList(const ValueList& anchors, const std::vector<bool>& outs, uint64_t group)> anchor_lists;
  // per owner of `todo`, whether its list in direction `out` holds a vid of `anchors`
  std::function<std::vector<bool>(const ValueList& todo, bool out, const UidSet& anchors, uint64_t group)> cand_lists;
  // the point form: per owner of `todo`, whether any of the reads' families holds an edge between it and an anchor.
  // The reads are independent (none waits on another's result): an owner's reads go together (one batch)
  std::function<std::vector<bool>(const std::vector<uint64_t>& todo, const std::vector<uint64_t>& anchors,
                                  const std::vector<EdgeRead>& rs)> probe;
};
Value edge_exists_over(const Args& a, const EdgeExistsReads& io);
// an order-independent walk: the task's physical counts since `before` ARE its logical ones
void direct_counts(WorkerSlot& slot, const Counts& before, TaskResult& r);
// a write op's result row (base._apply_writes): identical across formats on purpose
Value write_result(const std::string& op, const std::string& target);
// the write ops every format serves (base.Format's six)
bool is_write_op(const std::string& op);
// update_prop's cells: the single form (prop, value) as one cell, or the multi form's `props`
// row ({col: value, ...}), in the row's order. Throws when both or neither form is given.
struct UpdateCell { std::string prop; Value value; };
std::vector<UpdateCell> update_cells(const Args& a);
// update_prop's result target: "vid.prop" (single form), "vid.a,b,c" (multi form: the names sorted)
std::string update_target(const std::string& vid, const Args& a, const std::vector<UpdateCell>& cells);
// `row` with every cell set (Python {**row, **props})
Value row_with_cells(const Value& row, const std::vector<UpdateCell>& cells);
// update_edge_prop's `props` row: non-empty, every column declared on the etype (throws otherwise --
// an undeclared column has no slot in a row-encoded edge value, so every format refuses it alike)
const Value& edge_update_props(const Args& a, const EdgeDecl& decl);

// read_closure over any read_neighbors: levels of one call each (group = level), the seen set
// in first-seen order (base.read_closure).
using NeighborsFn = std::function<Value(uint64_t group, const ValueList& frontier)>;
Value closure_over(const NeighborsFn& neighbors, const ValueList& srcs, const Value& max_levels);

// read_chase's meaning, once (Python base.read_chase): each src follows its first neighbour until there is none or
// max_hops; the result is each chain's last vertex, in src order. A hop is one batch: one task per distinct node not
// read before, so a node is read once per call (chains that meet, and repeated srcs, share its read; decided
// every format). A node's first neighbour is its first direction's, in the step's order (out, then in):
// a later direction is read only when the earlier ones have none (every format; an engine twin reads
// every direction: the engine's plan)
struct ChaseReads {
  std::string fmt;
  // an engine twin's chase: each src walks alone, a node read again per chain (the engine runs the chase as a
  // per-row subquery; engine fidelity counts the engine's plan, not the format's cheapest)
  bool per_source = false;
  bool additive_counts = true;   // the tasks' counts are direct (false: replayed from their landings)
  // a hop's new batch, before hop `hops`'s tasks (its own chunk cache)
  std::function<void(int64_t hops)> level = [](int64_t) {};
  std::vector<bool> dirs;   // the step's directions, in order (true = out)
  // `node`'s first neighbour in direction `out` on hop `hops` (null: none); `node` keeps the tag its chain carries
  std::function<Value(WorkerSlot& slot, std::vector<Landing>& log, const Value& node, int64_t hops, bool out)> first;
};
Value chase_over(StepExec& ex, const ValueList& srcs, const Value& max_hops, const ChaseReads& rd);

// read_top_k_by_index's meaning, the parts every format shares (Python's per-format bodies, the contract in
// ops/kv_contract.h): the scope (the candidate set in uid space, none for a global walk; hi exclusive), the order and
// the cut at k, and a winner's row {_vid, prop, id, props...}. The walk and the winners' reads stay the format's.
struct TopKScope {
  std::shared_ptr<UidSet> cand;    // null: global (the step names no vids)
  std::optional<int64_t> hi;       // the exclusive upper bound on the value (none: no bound)
  const UidSet* cands() const { return cand.get(); }
};
TopKScope top_k_scope(const std::string& fmt, const kv::TopKArgs& t);
// the contract's order (kv::top_k_order), then the first k
template <class Row, class ValueOf, class IdOf>
void top_k_cut(std::vector<Row>& rows, const kv::TopKArgs& t, ValueOf value_of, IdOf id_of) {
  kv::top_k_order(rows, t.id_desc, value_of, id_of);
  if (int64_t(rows.size()) > t.k) rows.resize(size_t(t.k));
}
// a winner's row names: _vid, the sort prop, id, then the props asked
std::vector<std::string> top_k_row_names(const kv::TopKArgs& t);

// read_top_k_by_index's meaning, once: ONE task. The format's walk gives the candidates in its walk order (the walk and
// where it stops are the index layout's); the shared cut keeps the contract's first k; each winner's row is
// {_vid (tagged with its index's label), value, id, props...}, the winners' props read together (one parallel member
// per winner, every format) through the format's winner read.
struct TopKHit {
  Value vid;            // the winner as its index names it (arangodb: the doc's _key), tagged with `label` (tagged())
  int64_t value = 0;    // the sort value
  int64_t id = 0;       // the LDBC id, key-derived (0 ops)
  std::string label;    // the label whose index held it; "" when one index serves several labels (untagged)
  size_t at = 0;        // the hit's place in the walk's own side table, for a format that keeps state per hit
};
struct TopKReads {
  std::string fmt;
  // the candidates, in walk order; reads through a chunk cache land in `log`
  using Walk = std::function<std::vector<TopKHit>(WorkerSlot& slot, std::vector<Landing>& log, const TopKScope& scope)>;
  Walk walk;
  // one winner's props, in the step's props order ("id" included: key-derived); called only when props are asked
  std::function<std::vector<Value>(WorkerSlot& slot, std::vector<Landing>& log, const TopKHit& h)> winner_props;
  // the winners' reads go through a chunk cache: their counts come from replaying `log`, so the task's direct
  // counts are taken after the walk (Per-type CSR, meta's Chunked props)
  bool winners_replayed = false;
};
Value top_k_over(StepExec& ex, const kv::TopKArgs& t, const TopKReads& rd);

// The four operators whose meaning is written once (closure_over, chase_over, edge_exists_over, top_k_over). A format
// supplies only their reads; run_operator parses the step and runs the meaning. A primitive's returned closures may
// hold the call's arguments by reference (they live in run_operator's frame through the call); a primitive's own
// locals they keep are owned by value or through a shared_ptr.
class OperatorReads {
 public:
  virtual ~OperatorReads() = default;
  // read_closure: the neighbours of one level's frontier; `level` is the level's batch (its cache and replay group)
  virtual Value closure_level(StepExec& ex, uint64_t level, const ValueList& frontier, const std::string& etype,
                              const std::string& dir) = 0;
  // read_chase: a chain head's first neighbour
  virtual ChaseReads chase_reads(StepExec& ex, const std::string& etype, const std::string& dir) = 0;
  // read_edge_exists: the step's reads
  virtual EdgeExistsReads edge_exists_reads(StepExec& ex, const Args& a) = 0;
  // read_top_k_by_index: the walk and the winners' props
  virtual TopKReads top_k_reads(StepExec& ex, const kv::TopKArgs& t) = 0;
};
// one of the four ops (nullopt: `op` is another), for every format alike: the step's args, each label of top_k
// checked against the schema, the meaning over the format's reads
std::optional<Value> run_operator(OperatorReads& f, const Schema& schema, const std::string& op, const Args& a,
                                  StepExec& ex);

// The key_role of an 'i' index_id:u32 ... key of the shared index layouts (index/sorted_index.h,
// index/posting_index.h): the empty-prop type index enumerates a label, any other is a value / date index --
// both serve VERTICES (Python's "idx-type-enum" / "idx-value").
std::pair<std::string, std::string> index_key_role(const Schema& schema, std::string_view key);
// the schema names behind a key's ids (the per-label size table): "" when the id is unknown
std::string vertex_label_of_id(const Schema& schema, uint32_t id);
std::string etype_of_id(const Schema& schema, uint32_t id);
std::string index_label_of_key(const Schema& schema, std::string_view key);   // an 'i' index_id:u32 key
// The class of an 'i' key of those shared index layouts (nebula, pertype_csr): part index, its label, the role above.
KeyClass index_key_class(const Schema& schema, std::string_view key);

}  // namespace evalcpp
