#include "formats/common.h"

#include <algorithm>
#include <stdexcept>
#include <unordered_map>
#include <unordered_set>

#include "exec/label_tags.h"
#include "ir/expr.h"
#include "util/uidset.h"
#include "util/vid.h"
#include "ops/kv_contract.h"

namespace evalcpp {

static void take_items(ValueList& out, Value& v) {
  const ListPtr* p = std::get_if<ListPtr>(&v.v);
  if (p && p->use_count() == 1) {
    for (auto& x : const_cast<ValueList&>(**p)) out.push_back(std::move(x));
    return;
  }
  for (auto& x : v.as_list()) out.push_back(x);
}

Value joined_lists(std::vector<Value>& per_src, std::vector<Value>* second) {
  size_t total = 0;
  for (auto& v : per_src) total += v.as_list().size();
  if (second)
    for (auto& v : *second) total += v.as_list().size();
  ValueList out;
  out.reserve(total);
  for (size_t i = 0; i < per_src.size(); i++) {
    take_items(out, per_src[i]);
    if (second) take_items(out, (*second)[i]);
  }
  return Value::list(std::move(out));
}

// A vid or a str into a VidStrSet: a vid by its uid (its text is canonical: the same member), a str as before.
static bool insert_text(VidStrSet& s, const Value& v) { return v.is_vid() ? s.insert_uid(v.as_vid()) : s.insert(v.as_str()); }
static bool contains_text(const VidStrSet& s, const Value& v) {
  return v.is_vid() ? s.contains_uid(v.as_vid()) : s.contains(v.as_str());
}

std::vector<StrPtr> distinct_strs(const ValueList& vs) {
  std::vector<StrPtr> out;
  out.reserve(vs.size());
  VidStrSet seen(vs.size());                                      // on nearly every read op's hot path
  for (auto& v : vs) {
    if (!insert_text(seen, v)) continue;
    out.push_back(v.is_vid() ? std::make_shared<const std::string>(v.text()) : v.str_ptr());   // a vid: its text
  }
  return out;
}

std::vector<size_t> task_index_of(const ValueList& vids, const std::vector<StrPtr>& distinct) {
  if (distinct.size() == vids.size()) return {};
  std::vector<size_t> at;
  at.reserve(vids.size());
  UidMap<uint32_t> by_uid(distinct.size());
  bool canonical = true;
  for (size_t i = 0; i < distinct.size() && canonical; i++) {
    uint64_t u;
    if (canonical_vid(*distinct[i], &u)) by_uid.insert(u, uint32_t(i));
    else canonical = false;
  }
  if (canonical) {
    for (auto& v : vids) {
      uint64_t u = 0;
      bool ok = v.is_vid() ? (u = v.as_vid(), true) : canonical_vid(v.as_str(), &u);
      const uint32_t* i = ok ? by_uid.find(u) : nullptr;
      if (!i) throw std::runtime_error("task_index_of: vid '" + v.text() + "' is not in its distinct list");
      at.push_back(*i);
    }
    return at;
  }
  std::unordered_map<std::string, size_t> by_str;   // a non-canonical vid: string keys, as before
  by_str.reserve(distinct.size() * 2);
  for (size_t i = 0; i < distinct.size(); i++) by_str.emplace(*distinct[i], i);
  for (auto& v : vids) {
    auto it = by_str.find(v.text());
    if (it == by_str.end()) throw std::runtime_error("task_index_of: vid '" + v.text() + "' is not in its distinct list");
    at.push_back(it->second);
  }
  return at;
}

uint64_t uid_or_throw(const std::string& format, const std::string& vid) {
  uint64_t u;   // the canonical form only (16 lowercase hex): another spelling of a uid is another string in Python
  if (!canonical_vid(vid, &u)) throw std::runtime_error(format + ": '" + vid + "' is not an encoded vid (16 lowercase hex)");
  return u;
}

uint64_t uid_or_throw(const std::string& format, const Value& vid) {
  if (vid.is_vid()) return vid.as_vid();
  return uid_or_throw(format, vid.as_str());
}

Value key_id_value(const std::string& vid) { return Expr::key_id(Value(vid)); }

Value key_id_value(uint64_t uid) { return Value(int64_t(id_of_uid(uid))); }

DistinctVids distinct_vids(const std::string& format, const ValueList& vs, bool strict) {
  DistinctVids d;
  d.uids.reserve(vs.size());
  d.values.reserve(vs.size());
  std::vector<size_t> at;
  at.reserve(vs.size());
  UidMap<uint32_t> pos(vs.size());
  for (auto& v : vs) {
    uint64_t u;
    if (v.is_vid()) u = v.as_vid();
    else if (!(v.is_str() && canonical_vid(v.as_str(), &u)))
      throw std::runtime_error(format + ": '" + (v.is_text() ? v.text() : repr(v)) + "' is not a canonical vid");
    auto [p, fresh] = pos.insert(u, uint32_t(d.uids.size()));
    size_t i = *p;
    if (fresh) {
      d.uids.push_back(u);
      d.values.push_back(Value::vid(u));
    }
    at.push_back(i);
    if (v.label >= 0) {
      Value& dv = d.values[i];
      if (dv.label < 0) dv.label = v.label;
      else if (strict && dv.label != v.label)
        throw std::runtime_error("labels on rows: vid " + hex16(u) + " carries two labels in one step's input (ids " +
                                 std::to_string(dv.label) + " and " + std::to_string(v.label) + ")");
    }
  }
  if (d.uids.size() != vs.size()) d.at = std::move(at);
  return d;
}

Value without_key_id(const Value& props) {
  if (!props.is_row()) return props;
  RowFields f;
  for (auto& kv : props.as_row())
    if (kv.first != "id") f.push_back(kv);
  return Value::row(std::move(f));
}

std::vector<ColDecl> vertex_cols(const VertexDecl& v) {
  std::vector<ColDecl> cols;
  for (auto& p : v.props) cols.push_back({p.name, p.type, p.multi});
  return cols;
}

std::vector<EdgeRead> edge_reads(const Args& a, const std::string& fmt) {
  const char* want = " (want reads: [{\"dir\": \"out\"|\"in\", \"at\": \"anchor\"|\"cand\", \"unless_found\": bool}, ...])";
  if (!a.has("reads")) throw std::runtime_error(fmt + " read_edge_exists: the step names no reads" + want);
  std::vector<EdgeRead> out;
  for (auto& r : a.list("reads")) {
    if (!r.is_row()) throw std::runtime_error(fmt + " read_edge_exists: a read must be an object" + want);
    std::string dir = r.get("dir").is_str() ? r.get("dir").as_str() : "", at = r.get("at").is_str() ? r.get("at").as_str() : "";
    if ((dir != "out" && dir != "in") || (at != "anchor" && at != "cand"))
      throw std::runtime_error(fmt + " read_edge_exists: bad read " + repr(r) + want);
    for (auto& [k, v] : r.as_row())
      if (k != "dir" && k != "at" && k != "unless_found")
        throw std::runtime_error(fmt + " read_edge_exists: unknown key '" + k + "' in a read" + want);
    Value u = r.get("unless_found");
    if (!u.is_null() && !u.is_bool()) throw std::runtime_error(fmt + " read_edge_exists: unless_found must be true or false");
    out.push_back({dir == "out", at == "anchor", u.is_bool() && u.as_bool()});
  }
  if (out.empty()) throw std::runtime_error(fmt + " read_edge_exists: the step names no reads" + want);
  if (out[0].unless_found) throw std::runtime_error(fmt + " read_edge_exists: the first read cannot be unless_found (nothing was read before it)");
  return out;
}

Value edge_exists_over(const Args& a, const EdgeExistsReads& io) {
  const ValueList& rows = a.list("rows");
  if (rows.empty()) return Value::list({});
  std::string field = a.str("field"), into = a.str("into");
  std::vector<EdgeRead> reads = edge_reads(a, io.fmt);
  ValueList others = a.has("others") ? a.list("others") : io.via(a.list("via_srcs"), a.str("via_etype"));
  DistinctVids da = distinct_vids(io.fmt, others, io.strict_tags);
  const std::vector<uint64_t>& anchors = da.uids;
  ValueList cands;
  for (auto& r : rows) cands.push_back(r.field(field));
  DistinctVids d = distinct_vids(io.fmt, cands, io.strict_tags);
  UidSet have(d.uids.size());             // the candidates found so far
  UidSet anchor_set(anchors.size());
  for (uint64_t u : anchors) anchor_set.insert(u);
  auto todo_of = [&](const EdgeRead& r) {   // the candidates this read is for (EdgeRead)
    if (!r.unless_found) return d.uids;
    std::vector<uint64_t> t;
    for (uint64_t c : d.uids)
      if (!have.contains(c)) t.push_back(c);
    return t;
  };
  // the owners as a list read gets them: bare vids, or (carry_labels) as their rows carry them (the first tagged
  // occurrence among the anchors, then among the candidates)
  std::unordered_map<uint64_t, int16_t> carried;
  if (io.carry_labels)
    for (const DistinctVids* x : {&da, &d})
      for (size_t i = 0; i < x->uids.size(); i++)
        if (x->values[i].label >= 0) carried.emplace(x->uids[i], x->values[i].label);
  auto values_of = [&carried](const std::vector<uint64_t>& vids) {
    ValueList v;
    v.reserve(vids.size());
    for (uint64_t u : vids) {
      v.push_back(Value::vid(u));
      if (auto it = carried.find(u); it != carried.end()) v.back().label = it->second;
    }
    return v;
  };
  for (size_t i = 0; i < reads.size(); i++) {
    const EdgeRead& r = reads[i];
    std::vector<uint64_t> todo = todo_of(r);
    if (todo.empty()) continue;           // every candidate found: this read reads nothing
    const uint64_t group = r.unless_found ? 2 + i : 1;   // unless_found: a batch of its own (its keys depend on earlier reads)
    if (!io.list_like(r)) {
      std::vector<EdgeRead> rs = {r};   // the run of independent point reads that follow: one batch
      while (!r.unless_found && i + 1 < reads.size() && !io.list_like(reads[i + 1]) && !reads[i + 1].unless_found)
        rs.push_back(reads[++i]);
      std::vector<bool> found = io.probe(todo, anchors, rs);
      for (size_t t = 0; t < todo.size(); t++)
        if (found[t]) have.insert(todo[t]);
    } else if (r.at_anchor) {
      std::vector<bool> outs = {r.out};
      while (!r.unless_found && i + 1 < reads.size() && io.list_like(reads[i + 1]) && reads[i + 1].at_anchor &&
             !reads[i + 1].unless_found)
        outs.push_back(reads[++i].out);
      for (auto& n : io.anchor_lists(values_of(anchors), outs, group))
        have.insert(uid_or_throw(io.fmt, n));   // an anchor's neighbour: found if it is a candidate
    } else {
      std::vector<bool> found = io.cand_lists(values_of(todo), r.out, anchor_set, group);
      for (size_t t = 0; t < todo.size(); t++)
        if (found[t]) have.insert(todo[t]);
    }
  }
  ValueList out;
  out.reserve(rows.size());
  RowWith with(into);                                       // the output shape made once per input shape
  for (size_t i = 0; i < rows.size(); i++)
    out.push_back(with(rows[i], Value(have.contains(d.uids[d.at.empty() ? i : d.at[i]]))));
  return Value::list(std::move(out));
}

Value chase_over(StepExec& ex, const ValueList& srcs, const Value& max_hops, const ChaseReads& rd) {
  auto more = [&](int64_t hops) { return max_hops.is_null() || hops < max_hops.as_int(); };
  if (rd.dirs.empty()) throw std::runtime_error(rd.fmt + " read_chase: the format set no direction to read");
  // the first direction with a neighbour gives it; a later direction is not read. An engine twin reads every
  // direction (the engine's plan) and takes the first neighbour found
  auto first = [&](WorkerSlot& slot, std::vector<Landing>& log, const Value& node, int64_t hops) {
    Value found;
    for (bool out : rd.dirs) {
      Value n = rd.first(slot, log, node, hops, out);
      if (found.is_null()) found = n;
      if (!found.is_null() && !rd.per_source) break;
    }
    return found;
  };
  std::vector<Value> cur(srcs.begin(), srcs.end());   // the chain heads: a head keeps its tag
  if (rd.per_source) {
    cur = ex.run_range_map(srcs.size(), [&](WorkerSlot& slot, size_t i, std::vector<Landing>& log) {
        Value at = srcs[i];
        for (int64_t hops = 0; more(hops); hops++) {
          Value n = first(slot, log, at, hops);
          if (n.is_null()) break;
          at = n;
        }
        return at;
    }, rd.additive_counts);
  } else {
    std::vector<bool> done(cur.size(), false);
    std::unordered_map<uint64_t, std::optional<Value>> next_of;   // node -> its first neighbour (none: nullopt)
    std::vector<uint64_t> cur_u(cur.size());
    for (size_t i = 0; i < cur.size(); i++) cur_u[i] = uid_or_throw(rd.fmt, cur[i]);
    for (int64_t hops = 0; more(hops); hops++) {
      std::vector<uint64_t> frontier;                     // the level's distinct unread heads, in chain order
      std::unordered_set<uint64_t> in_level;
      std::unordered_map<uint64_t, Value> head_of;        // the head as its chain holds it (its label tag kept)
      for (size_t i = 0; i < cur.size(); i++)
        if (!done[i] && !next_of.count(cur_u[i]) && in_level.insert(cur_u[i]).second) {
          frontier.push_back(cur_u[i]);
          head_of.emplace(cur_u[i], cur[i]);
        }
      rd.level(hops);
      std::vector<Value> firsts = ex.run_range_map(frontier.size(), [&](WorkerSlot& slot, size_t ti, std::vector<Landing>& log) {
          return first(slot, log, head_of.at(frontier[ti]), hops);
      }, rd.additive_counts);
      for (size_t t = 0; t < frontier.size(); t++)
        next_of[frontier[t]] = firsts[t].is_null() ? std::nullopt : std::optional<Value>(firsts[t]);
      bool moved = false;
      for (size_t i = 0; i < cur.size(); i++) {
        if (done[i]) continue;
        const auto& n = next_of.at(cur_u[i]);
        if (!n) { done[i] = true; continue; }
        cur[i] = *n;
        cur_u[i] = uid_or_throw(rd.fmt, *n);
        moved = true;
      }
      if (!moved) break;
    }
  }
  ValueList out;
  for (auto& v : cur) out.push_back(v);
  return Value::list(std::move(out));
}

TopKScope top_k_scope(const std::string& fmt, const kv::TopKArgs& t) {
  TopKScope s;
  if (!t.vids.is_null()) {
    s.cand = std::make_shared<UidSet>(t.vids.as_list().size());   // uid space, flat
    for (auto& v : t.vids.as_list()) s.cand->insert(uid_or_throw(fmt, v));
  }
  if (!t.hi.is_null()) s.hi = t.hi.as_int();
  return s;
}

std::vector<std::string> top_k_row_names(const kv::TopKArgs& t) {
  std::vector<std::string> names = {"_vid", t.prop, "id"};
  names.insert(names.end(), t.props.begin(), t.props.end());
  return names;
}

Value top_k_over(StepExec& ex, const kv::TopKArgs& t, const TopKReads& rd) {
  TopKScope scope = top_k_scope(rd.fmt, t);
  std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {
    Counts before = slot.store.counts();
    std::vector<TopKHit> hits = rd.walk(slot, r.landings, scope);
    if (rd.winners_replayed) direct_counts(slot, before, r);
    top_k_cut(hits, t, [](const TopKHit& h) { return h.value; }, [](const TopKHit& h) { return h.id; });   // the contract's order, then k
    RowBuilder wb(top_k_row_names(t));                      // the winners' one shape
    ValueList out;
    RtParallel par(slot.store);                             // one member per winner's props
    for (auto& h : hits) {
      par.member();
      RowBuilder::Row f = wb.start();
      f.push(h.vid);
      f.push(Value(h.value));
      f.push(Value(h.id));
      if (!t.props.empty())
        for (auto& v : rd.winner_props(slot, r.landings, h)) f.push(std::move(v));
      out.push_back(f.done());
    }
    if (!rd.winners_replayed) direct_counts(slot, before, r);
    r.out = Value::list(std::move(out));
  }};
  return ex.run(tasks)[0].out;
}

Value field_or_null(const RowFields& f, const std::string& name) {
  for (auto& kv : f)
    if (kv.first == name) return kv.second;
  return Value();
}

std::vector<std::string> props_names(const std::vector<std::string>& props) {
  std::vector<std::string> names = {"_vid"};
  names.insert(names.end(), props.begin(), props.end());
  return names;
}

std::vector<std::string> props_names_id_first(bool want_id, const std::vector<std::string>& row_props) {
  std::vector<std::string> names = {"_vid"};
  if (want_id) names.push_back("id");
  names.insert(names.end(), row_props.begin(), row_props.end());
  return names;
}

NeighborItems::NeighborItems(const std::vector<std::string>& edge_props, bool pairs)
    : edge_props_(edge_props), pairs_(pairs) {
  if (!pairs && edge_props.empty()) return;
  std::vector<std::string> names;
  if (pairs) names.push_back("src");
  names.push_back("dst");
  names.insert(names.end(), edge_props.begin(), edge_props.end());   // a repeated name overwrites, like a dict
  rb_.emplace(names);
}

Value NeighborItems::operator()(const Value& src, const Value& dst, const Value* props) const {
  if (!rb_) return dst;
  RowBuilder::Row r = rb_->start();
  if (pairs_) r.push(src);
  r.push(dst);
  for (auto& p : edge_props_) r.push(props ? props->get(p) : Value());
  return r.done();
}

Value NeighborItems::operator()(const Value& src, const Value& dst, const RowFields& props) const {
  if (!rb_) return dst;
  RowBuilder::Row r = rb_->start();
  if (pairs_) r.push(src);
  r.push(dst);
  for (auto& p : edge_props_) r.push(field_or_null(props, p));
  return r.done();
}

std::vector<bool> parse_dirs(const std::string& op, const std::string& dir) {
  if (dir == "out") return {true};
  if (dir == "in") return {false};
  if (dir == "both") return {true, false};
  throw std::runtime_error(op + ": bad dir '" + dir + "'");
}

void direct_counts(WorkerSlot& slot, const Counts& before, TaskResult& r) {
  r.stats.direct = slot.store.counts().since(before);   // every field, the miss lanes included
  slot.store.parts_commit(&r.stats.direct);   // parts on: this task's parts are logical too (checked == direct)
}

Value write_result(const std::string& op, const std::string& target) {
  return Value::list({make_row({{"op", Value(op)}, {"target", Value(target)}})});
}

bool is_write_op(const std::string& op) { return kv::is_write_op(op); }   // the contract's table (ops/kv_contract.cc)

std::vector<UpdateCell> update_cells(const Args& a) {
  if (a.has("props")) {
    if (a.has("prop")) throw std::runtime_error("update_prop: give either prop+value or props, not both");
    const Value& props = a.get("props");
    if (!props.is_row() || props.as_row().empty())
      throw std::runtime_error("update_prop: props must be a non-empty {col: value} row");
    std::vector<UpdateCell> cells;
    for (auto& [name, v] : props.as_row()) cells.push_back({name, v});
    return cells;
  }
  return {{a.str("prop"), a.get("value")}};
}

std::string update_target(const std::string& vid, const Args& a, const std::vector<UpdateCell>& cells) {
  if (!a.has("props")) return vid + "." + cells[0].prop;
  std::vector<std::string> names;
  for (auto& c : cells) names.push_back(c.prop);
  std::sort(names.begin(), names.end());
  std::string t = vid + ".";
  for (size_t i = 0; i < names.size(); i++) t += (i ? "," : "") + names[i];
  return t;
}

Value row_with_cells(const Value& row, const std::vector<UpdateCell>& cells) {
  Value r = row;
  for (auto& c : cells) r = row_with(r, c.prop, c.value);
  return r;
}

const Value& edge_update_props(const Args& a, const EdgeDecl& decl) {
  const Value& props = a.get("props");
  if (!props.is_row() || props.as_row().empty())
    throw std::runtime_error("update_edge_prop: props must be a non-empty {col: value} row");
  for (auto& [name, v] : props.as_row()) {
    bool declared = false;
    for (auto& p : decl.props) declared = declared || p.name == name;
    if (!declared)
      throw std::runtime_error("update_edge_prop: '" + decl.etype + "' declares no edge property '" + name + "'");
  }
  return props;
}

Value closure_over(const NeighborsFn& neighbors, const ValueList& srcs, const Value& max_levels) {
  VidStrSet seen_set(srcs.size());
  ValueList seen, frontier;
  for (auto& s : srcs)
    if (insert_text(seen_set, s)) seen.push_back(s);
  frontier = seen;
  int64_t levels = 0;
  while (!frontier.empty() && (max_levels.is_null() || levels < max_levels.as_int())) {
    Value nxt = neighbors(uint64_t(levels), frontier);   // one level = one Python-level call
    ValueList next_frontier;
    VidStrSet local(nxt.as_list().size());
    for (auto& x : nxt.as_list()) {
      if (!insert_text(local, x)) continue;
      if (contains_text(seen_set, x)) continue;
      next_frontier.push_back(x);
    }
    for (auto& x : next_frontier) { insert_text(seen_set, x); seen.push_back(x); }
    frontier = std::move(next_frontier);
    levels++;
  }
  return Value::list(std::move(seen));
}

std::pair<std::string, std::string> index_key_role(const Schema& schema, std::string_view key) {
  if (key.size() < 5 || key[0] != 'i') return {"other", "other"};
  uint32_t id = (uint32_t(uint8_t(key[1])) << 24) | (uint32_t(uint8_t(key[2])) << 16) |
                (uint32_t(uint8_t(key[3])) << 8) | uint32_t(uint8_t(key[4]));
  for (auto& ix : schema.indexes())
    if (uint32_t(ix.id) == id) return {"index-vertex", ix.prop.empty() ? "idx-type-enum" : "idx-value"};
  return {"other", "other"};
}

KeyClass index_key_class(const Schema& schema, std::string_view key) {
  auto [role, kind] = index_key_role(schema, key);
  if (key.size() < 5) return {"unclassified", "", "", "", "", role, kind};
  return {"index", index_label_of_key(schema, key), "", "", "", role, kind};
}

std::string vertex_label_of_id(const Schema& schema, uint32_t id) {
  for (auto& v : schema.vertices())
    if (uint32_t(v.id) == id) return v.label;
  return "";
}

std::string etype_of_id(const Schema& schema, uint32_t id) {
  for (auto& e : schema.edges())
    if (uint32_t(e.id) == id) return e.etype;
  return "";
}

std::string index_label_of_key(const Schema& schema, std::string_view key) {
  if (key.size() < 5 || key[0] != 'i') return "";
  uint32_t id = (uint32_t(uint8_t(key[1])) << 24) | (uint32_t(uint8_t(key[2])) << 16) |
                (uint32_t(uint8_t(key[3])) << 8) | uint32_t(uint8_t(key[4]));
  for (auto& ix : schema.indexes())
    if (uint32_t(ix.id) == id) return ix.label;
  return "";
}

std::optional<Value> run_operator(OperatorReads& f, const Schema& schema, const std::string& op, const Args& a,
                                  StepExec& ex) {
  if (op == "read_closure") {
    kv::WalkArgs w = kv::closure_args(a);
    return closure_over([&](uint64_t level, const ValueList& frontier) {
                          return f.closure_level(ex, level, frontier, w.etype, w.dir);
                        },
                        *w.srcs, w.max);
  }
  if (op == "read_chase") {
    kv::WalkArgs w = kv::chase_args(a);
    return chase_over(ex, *w.srcs, w.max, f.chase_reads(ex, w.etype, w.dir));
  }
  if (op == "read_edge_exists") return edge_exists_over(a, f.edge_exists_reads(ex, a));
  if (op == "read_top_k_by_index") {
    kv::TopKArgs t = kv::top_k_args(a);
    for (auto& l : t.labels) schema.vertex(l);   // each label is the schema's (throws)
    return top_k_over(ex, t, f.top_k_reads(ex, t));
  }
  return std::nullopt;
}

}  // namespace evalcpp
