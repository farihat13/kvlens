// The KV-op contract (src/ops/kv_contract.h): the table's flags, the parsers' defaults and refusals, and the top-k order;
// and the compiled-in data files (inputs/contract) against the code that does not read them: the parsers' written-out
// defaults, the byte lanes a step's counts carry, the capture file kinds.
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

#include "contract/contract.h"
#include "harness.h"
#include "ops/kv_contract.h"
#include "store/counting.h"

namespace {

using namespace evalcpp;

// true when f throws std::runtime_error
template <class F>
bool refused(F f) {
  try {
    f();
  } catch (const std::runtime_error&) {
    return true;
  }
  return false;
}

Value strs(const std::vector<std::string>& xs) {
  ValueList l;
  for (auto& x : xs) l.push_back(Value(x));
  return Value::list(std::move(l));
}

}  // namespace

TEST_CASE(kv_contract_table) {
  CHECK(kv::is_read_op("read_neighbors"));
  CHECK(kv::is_every_format_read_op("read_edge_exists"));
  CHECK(kv::is_read_op("index_range_buckets"), "an optional read is a read");
  CHECK(!kv::is_every_format_read_op("index_range_buckets"), "only the formats that list it serve it");
  CHECK(kv::is_write_op("insert_edge") && !kv::is_read_op("insert_edge"));
  CHECK(!kv::is_read_op("read_no_such_op"), "a read_ prefix alone is not an op");
  CHECK(!kv::is_read_op("filter"), "a flow op is not a KV op");
  json j = kv::ops_json();
  CHECK_EQ(j["ops"].size(), kv::ops().size());
  return fails ? 1 : 0;
}

TEST_CASE(kv_contract_defaults) {
  Args a;
  a.set("srcs", strs({"person_1"}));
  a.set("etype", Value("knows"));
  kv::NeighborsArgs n = kv::neighbors_args(a);
  CHECK_EQ(n.dir, std::string("out"));
  CHECK(n.edge_props.empty() && !n.pairs && n.labels.empty());
  a.set("dir", Value("both"));
  CHECK_EQ(kv::neighbors_args(a).dir, std::string("both"));
  CHECK_EQ(kv::chase_args(a).dir, std::string("both"));
  CHECK(kv::closure_args(a).max.is_null());
  CHECK_EQ(kv::dir_of(json{{"etype", "knows"}}), std::string("out"));
  CHECK(kv::dir_of(json{{"dir", nullptr}}) == "out", "a null dir takes the default");
  CHECK_EQ(kv::dir_of(json{{"dir", "in"}}), std::string("in"));

  Args t;
  t.set("prop", Value("creationDate"));
  t.set("k", Value(20));
  t.set("labels", strs({"Post", "Comment"}));
  kv::TopKArgs k = kv::top_k_args(t);
  CHECK(!k.id_desc && k.props.empty() && k.vids.is_null() && k.hi.is_null());
  CHECK_EQ(k.k, int64_t(20));
  t.set("id_order", Value("desc"));
  CHECK(kv::top_k_args(t).id_desc);
  return fails ? 1 : 0;
}

TEST_CASE(kv_contract_refusals) {
  auto topk = [](const std::string& key, Value v) {
    Args t;
    t.set("prop", Value("creationDate"));
    t.set("k", Value(20));
    t.set("labels", strs({"Post"}));
    t.set(key, std::move(v));
    return kv::top_k_args(t);
  };
  CHECK(!refused([&] { topk("order", Value("desc")); }), "desc is the one order");
  CHECK(refused([&] { topk("order", Value("asc")); }), "only order=desc");
  CHECK(refused([&] { topk("k", Value(0)); }), "k must be >= 1");
  CHECK(refused([&] { topk("k", Value(-3)); }), "a negative k is refused, not resized to");
  CHECK(!refused([&] { topk("labels", strs({})); }), "no labels: the format decides (dgraph's index is per predicate)");
  CHECK(refused([&] { topk("id_order", Value("sideways")); }), "id_order is asc or desc");
  Args r;
  r.set("prop", Value("creationDate"));
  r.set("lo", Value(0));
  r.set("hi", Value(10));
  CHECK(kv::index_range_args(r, "read_index_range").labels.empty(), "no labels: the format decides");
  r.set("labels", strs({"Post"}));
  CHECK_EQ(kv::index_range_args(r, "read_index_range").labels.size(), size_t(1));
  return fails ? 1 : 0;
}

TEST_CASE(kv_contract_top_k_order) {
  struct Row { int64_t value, id; int seen; };
  // value desc; ties by id asc; full ties keep the walk's order
  std::vector<Row> rows = {{5, 9, 0}, {7, 3, 1}, {5, 2, 2}, {7, 3, 3}, {5, 4, 4}};
  kv::top_k_order(rows, false, [](const Row& r) { return r.value; }, [](const Row& r) { return r.id; });
  std::vector<int> got;
  for (auto& r : rows) got.push_back(r.seen);
  CHECK(got == std::vector<int>({1, 3, 2, 4, 0}), "value desc, id asc, then the walk's order");
  kv::top_k_order(rows, true, [](const Row& r) { return r.value; }, [](const Row& r) { return r.id; });
  got.clear();
  for (auto& r : rows) got.push_back(r.seen);
  CHECK(got == std::vector<int>({1, 3, 0, 4, 2}), "id_order=desc: ties by id descending");
  return fails ? 1 : 0;
}

// the table's default of one op's argument, as JSON text ("" when it has none)
static std::string table_default(const std::string& op, const std::string& arg) {
  for (auto& a : kv::find_op(op)->args)
    if (a.name == arg) return a.default_json;
  return "";
}

TEST_CASE(contract_data_matches_code) {
  // the parsers write their defaults out (ops/kv_contract.cc): each equals inputs/contract/kv_ops.json's
  for (const char* op : {"read_neighbors", "read_chase", "read_closure"})
    CHECK_EQ(table_default(op, "dir"), std::string("\"") + kv::kDefaultDir + "\"");
  CHECK_EQ(table_default("read_top_k_by_index", "order"), std::string("\"desc\""));   // top_k_args accepts desc only
  Args t;
  t.set("prop", Value("creationDate"));
  t.set("k", Value(1));
  t.set("labels", strs({"Post"}));
  kv::TopKArgs k = kv::top_k_args(t);
  CHECK_EQ(table_default("read_top_k_by_index", "id_order"), std::string(k.id_desc ? "\"desc\"" : "\"asc\""));
  CHECK(table_default("read_top_k_by_index", "props") == "[]" && k.props.empty());
  Args a;
  a.set("srcs", strs({"person_1"}));
  a.set("etype", Value("knows"));
  kv::NeighborsArgs n = kv::neighbors_args(a);
  CHECK_EQ(table_default("read_neighbors", "pairs"), std::string(n.pairs ? "true" : "false"));
  CHECK(table_default("read_neighbors", "edge_props") == "[]" && n.edge_props.empty());

  // a step's counts (store/counting.cc Counts::to_json) carry exactly the data file's op counts and byte lanes
  json c = Counts{}.to_json();
  std::set<std::string> got, want;
  for (auto& [k2, v] : c.items())
    if (k2 != "bytes") got.insert(k2);
  for (auto& [k2, v] : c["bytes"].items()) got.insert(k2);
  for (const char* g : {"op_counts_read", "op_counts_write", "op_counts_miss", "byte_lanes_hit", "byte_lanes_miss",
                        "byte_lanes_write", "byte_lanes_used"})
    for (auto& col : contract::columns(g)) want.insert(col);
  CHECK(got == want, "Counts::to_json's lanes differ from inputs/contract/capture_columns.json's groups");

  // every file kind names columns once
  int nfiles = 0;
  for (auto& [kind, list] : contract::capture_columns().at("files").items()) {
    std::vector<std::string> cols = contract::columns(kind);
    std::set<std::string> uniq(cols.begin(), cols.end());
    CHECK(uniq.size() == cols.size(), kind << " names a column twice");
    nfiles++;
  }
  CHECK_EQ(nfiles, 6);
  // the last-chunk lane: its response keys are LastChunk::to_json's, one for one
  std::set<std::string> lc_got, lc_want;
  json lc = Counts::LastChunk().to_json();   // a named value: items() of a temporary would dangle
  for (auto& [k, v] : lc.items()) lc_got.insert(k);
  for (auto& [col, key] : contract::capture_columns().at("response_keys").at("last_chunk").items())
    lc_want.insert(key.get<std::string>());
  CHECK(lc_got == lc_want, "Counts::LastChunk::to_json's keys differ from capture_columns.json's last_chunk");
  return fails ? 1 : 0;
}
