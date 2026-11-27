#include "ir/plan.h"

#include <stdexcept>

#include "contract/contract.h"
#include "schema/schema.h"
#include "util/sha256.h"

namespace evalcpp {

const std::set<std::string>& capability_vocabulary() {
  // the names of inputs/contract/capabilities.json (compiled in; Python reads the same file): a bundle naming any of
  // these parses identically in both languages, even a cap no C++ format grants yet (a variant requiring it then
  // falls through)
  static const std::set<std::string> v = [] {
    std::set<std::string> out;
    for (auto& [name, cap] : contract::capabilities().at("capabilities").items()) out.insert(name);
    return out;
  }();
  return v;
}

static const std::set<std::string> kValueKeys = {"ref", "param", "lit", "let"};
static const std::set<std::string> kExprArgs = {"pred", "fn", "where", "where_key"};   // join_multi's: ops/flow.cc
static const std::set<std::string> kPairTestArgs = {"where", "where_key", "where_in", "where_not_in"};   // join_multi only

static bool is_value_form(const json& v) {
  return v.is_object() && v.size() == 1 && kValueKeys.count(v.begin().key());
}

// --- bundle: variant selection + fragment inlining (planir/bundle.py) --------------------

static std::string select_variant(const json& bundle, const std::set<std::string>& caps,
                                  const CapsOfLabel& of_label, const std::string& scale) {
  for (auto& v : bundle.value("variants", json::array())) {
    auto cap_list = [&](const char* key) {
      std::set<std::string> out;
      for (auto& r : v.value(key, json::array())) {
        std::string cap = r.get<std::string>();
        if (!capability_vocabulary().count(cap))
          throw std::runtime_error("variant '" + v.value("plan", "") + "' " + key + " unknown capability '" + cap + "'");
        out.insert(cap);
      }
      return out;
    };
    std::set<std::string> req = cap_list("requires");
    // `lacks`: the NEGATIVE selector (bundle.py) -- the variant is for formats that hold every
    // `requires` cap and NONE of the `lacks` caps (a cost-driven pick that separates formats
    // whose capability sets are subset-ordered: the more capable one declines this variant)
    std::set<std::string> lacks = cap_list("lacks");
    // a cap annotated `"on": {cap: [label,...]}` is checked against the OWNER of each label
    // (per label for meta; identical to the global set for a format with one layout);
    // un-annotated caps (and callers with no resolver) use the format-global set (bundle.py)
    const json on = v.value("on", json::object());
    auto holds = [&](const std::string& cap, bool want) {   // want: every checked set holds it / none does
      if (of_label && on.contains(cap)) {
        for (auto& l : on.at(cap))
          if (bool(of_label(l.get<std::string>()).count(cap)) != want) return false;
        return true;
      }
      return bool(caps.count(cap)) == want;
    };
    bool ok = true;
    for (auto& r : req)
      if (!holds(r, true)) { ok = false; break; }
    for (auto& r : lacks)
      if (ok && !holds(r, false)) { ok = false; break; }
    if (!ok) continue;
    if (v.contains("scales")) {
      // a per-scale pin (planir/bundle.py select_variant): the scale decides only among variants whose caps match;
      // a matching tagged variant with no scale known throws -- never a guess
      const json& tags = v.at("scales");
      if (!tags.is_array() || tags.empty())
        throw std::runtime_error("variant '" + v.value("plan", "") + "': `scales` must be a non-empty list of strings");
      bool listed = false;
      for (auto& t : tags) {
        if (!t.is_string())
          throw std::runtime_error("variant '" + v.value("plan", "") + "': `scales` must be a non-empty list of strings");
        listed = listed || t.get<std::string>() == scale;
      }
      if (scale.empty())
        throw std::runtime_error("variant '" + v.value("plan", "") + "' is scale-tagged but the store's scale is unknown "
                                 "(no manifest `scale`, no request store.scale)");
      if (!listed) continue;
    }
    return v.at("plan").get<std::string>();
  }
  std::string c;
  for (auto& x : caps) c += (c.empty() ? "" : ", ") + x;
  throw std::runtime_error("no variant runs on a format with capabilities [" + c + "]");
}

// the body an IR runs: a one-body IR (`plans` with one body, no `variants`: Python names the body, the pick record's)
// runs it; an IR with `variants` (the parked label_only / adj_sort files) selects by caps; several bodies and no
// `variants` is refused: the caller must name the body
static std::string body_to_run(const json& ir, const std::set<std::string>& caps, const CapsOfLabel& of_label,
                               const std::string& scale) {
  if (ir.contains("variants")) return select_variant(ir, caps, of_label, scale);
  const json& plans = ir.at("plans");
  if (plans.size() != 1)
    throw std::runtime_error("plan '" + ir.value("plan", "") + "' has " + std::to_string(plans.size()) +
                             " bodies and no `variants`: send the one body to run");
  return plans.begin().key();
}

static json sub_fragment(const json& v, const std::string& host, const json& args,
                         const std::set<std::string>& internal, const std::string& fragname) {
  if (v.is_object() && v.size() == 1) {
    const std::string& k = v.begin().key();
    const json& a = v.begin().value();
    if (k == "in") {
      std::string name = a.get<std::string>();
      if (!args.contains(name)) throw std::runtime_error("fragment '" + fragname + "' input '" + name + "' not supplied");
      return args.at(name);
    }
    if (k == "ref" && internal.count(a.get<std::string>())) return json{{"ref", host + "." + a.get<std::string>()}};
  }
  if (v.is_object()) {
    json o = json::object();
    for (auto it = v.begin(); it != v.end(); ++it) o[it.key()] = sub_fragment(it.value(), host, args, internal, fragname);
    return o;
  }
  if (v.is_array()) {
    json a = json::array();
    for (auto& x : v) a.push_back(sub_fragment(x, host, args, internal, fragname));
    return a;
  }
  return v;
}

static json rewrite_ref(const json& v, const std::string& from, const std::string& to) {
  if (v.is_object()) {
    if (v.size() == 1 && v.contains("ref") && v["ref"].is_string() && v["ref"] == from) return json{{"ref", to}};
    json o = json::object();
    for (auto it = v.begin(); it != v.end(); ++it) o[it.key()] = rewrite_ref(it.value(), from, to);
    return o;
  }
  if (v.is_array()) {
    json a = json::array();
    for (auto& x : v) a.push_back(rewrite_ref(x, from, to));
    return a;
  }
  return v;
}

static std::vector<json> inline_use(const json& use_step, const json& fragment) {
  std::string host = use_step.at("name").get<std::string>();
  json args = use_step.value("args", json::object());
  std::set<std::string> internal;
  for (auto& st : fragment.at("steps")) internal.insert(st.at("name").get<std::string>());
  std::string fragname = fragment.at("fragment").get<std::string>();
  std::string exported = fragment.at("output").get<std::string>();
  std::vector<json> steps;
  for (auto& st : fragment.at("steps")) {
    json o = json::object();
    for (auto it = st.begin(); it != st.end(); ++it) {
      if (it.key() == "name") o["name"] = host + "." + it.value().get<std::string>();
      else o[it.key()] = sub_fragment(it.value(), host, args, internal, fragname);
    }
    steps.push_back(o);
  }
  std::string old = host + "." + exported;
  std::vector<json> renamed;
  for (auto& st : steps) {
    json s = st;
    if (s.value("name", "") == old) s["name"] = host;
    renamed.push_back(rewrite_ref(s, old, host));
  }
  return renamed;
}

static json expand_fragments(const json& plan, const json& fragments) {
  std::map<std::string, json> by_name;
  for (auto& f : fragments) by_name[f.at("fragment").get<std::string>()] = f;
  json steps = plan.value("steps", json::array());
  int depth = 0;
  auto has_use = [](const json& ss) {
    for (auto& s : ss)
      if (s.contains("use")) return true;
    return false;
  };
  while (has_use(steps)) {
    if (++depth > 100) throw std::runtime_error("fragment expansion did not terminate (a cycle?)");
    json out = json::array();
    for (auto& s : steps) {
      if (s.contains("use")) {
        std::string u = s["use"].get<std::string>();
        if (!by_name.count(u)) throw std::runtime_error("unknown fragment '" + u + "'");
        for (auto& x : inline_use(s, by_name[u])) out.push_back(x);
      } else out.push_back(s);
    }
    steps = out;
  }
  json flat = plan;
  flat["steps"] = steps;
  return flat;
}

std::string body_sha_of(const json& flat) {
  json body = json::object();
  body["steps"] = flat.value("steps", json::array());
  if (flat.contains("output")) body["output"] = flat.at("output");
  // canonical form: keys SORTED (our `json` is ordered_json, file order; re-parse into the std::map-backed
  // nlohmann::json, whose dump() is compact with sorted keys == Python's json.dumps(sort_keys, compact separators))
  nlohmann::json canonical = nlohmann::json::parse(body.dump());
  return sha256_hex(canonical.dump()).substr(0, 12);
}

json flatten_ir(const json& ir, const std::set<std::string>& caps, const CapsOfLabel& of_label,
                const std::string& scale) {
  json plan;
  if (ir.contains("steps")) {
    // a bare plan may name store caps it needs (`requires`, stamped by dump_planir: property_index); a store without
    // one refuses it as a bundle with no fitting variant (planir/bundle.py require_bare, the same message)
    for (auto& r : ir.value("requires", json::array())) {
      std::string cap = r.get<std::string>();
      if (!capability_vocabulary().count(cap))
        throw std::runtime_error("plan '" + ir.value("plan", "") + "' requires unknown capability '" + cap + "'");
      if (!caps.count(cap)) {
        std::string c;
        for (auto& x : caps) c += (c.empty() ? "" : ", ") + x;
        throw std::runtime_error("no variant runs on a format with capabilities [" + c + "] (the plan requires " + cap + ")");
      }
    }
    plan = ir;
  } else {
    std::string name = body_to_run(ir, caps, of_label, scale);
    plan = ir.at("plans").at(name);
  }
  return expand_fragments(plan, ir.value("fragments", json::array()));
}

// --- bind ---------------------------------------------------------------------------------

std::map<std::string, Value> coerce_params(const json& raw, const json& decls) {
  std::map<std::string, Value> out;
  for (auto it = raw.begin(); it != raw.end(); ++it) {
    const std::string& k = it.key();
    const json& v = it.value();
    std::string type = decls.contains(k) ? decls[k].value("type", "") : "";
    if (type == "int") {
      if (v.is_number_unsigned() && v.get<uint64_t>() > uint64_t(INT64_MAX))
        throw std::runtime_error("param '" + k + "' is out of int64 range: " + v.dump());
      if (v.is_number_integer() || v.is_number_unsigned()) out[k] = Value(v.get<int64_t>());
      else if (v.is_string()) {
        std::string t = v.get<std::string>();          // Python int() ignores surrounding whitespace
        while (!t.empty() && isspace(uint8_t(t.back()))) t.pop_back();
        while (!t.empty() && isspace(uint8_t(t.front()))) t.erase(0, 1);
        try {
          size_t pos = 0;
          int64_t n = std::stoll(t, &pos);
          if (pos != t.size()) throw std::invalid_argument("trailing");
          out[k] = Value(n);
        } catch (...) {
          throw std::runtime_error("param '" + k + "' declared int, got '" + v.get<std::string>() + "'");
        }
      } else throw std::runtime_error("param '" + k + "' declared int, got " + v.dump());
    } else if (type == "bytes") {   // only the tagged form: a str here is a params file written before the bytes type
      if (!(v.is_object() && v.contains("$bytes")))
        throw std::runtime_error("param '" + k + "' declared bytes, got " + v.dump().substr(0, 80) +
                                 " (a params file written before the bytes type? regenerate it)");
      out[k] = from_json(v);
    } else {
      if (v.is_boolean() || v.is_number_float() || v.is_object())
        throw std::runtime_error("param '" + k + "': unsupported value " + v.dump());
      out[k] = from_json(v);   // id / untyped: untouched (a string stays a string)
    }
  }
  return out;
}

static void collect_refs(const json& v, std::vector<std::string>& refs) {
  if (v.is_object()) {
    if (v.size() == 1 && v.contains("ref") && v["ref"].is_string()) { refs.push_back(v["ref"].get<std::string>()); return; }
    for (auto it = v.begin(); it != v.end(); ++it) collect_refs(it.value(), refs);
  } else if (v.is_array()) {
    for (auto& x : v) collect_refs(x, refs);
  }
}

PlanBody prepare_plan(const json& ir, const std::set<std::string>& caps, const CapsOfLabel& of_label,
                      const std::string& scale) {
  PlanBody b;
  b.name = ir.value("plan", "plan");
  if (!ir.contains("steps")) b.body_name = body_to_run(ir, caps, of_label, scale);   // named for the response
  b.bind = ir.value("bind", json::object());
  b.params_decl = ir.value("params", json::object());
  b.lets = ir.value("let", json::object());
  b.hints = ir.value("estimate_hints", json::object());
  json flat = flatten_ir(ir, caps, of_label, scale);
  b.body_sha = body_sha_of(flat);
  std::set<std::string> seen;
  for (auto& s : flat.at("steps")) {
    Step st;
    std::vector<std::pair<std::string, json>> asts;
    st.name = s.at("name").get<std::string>();
    st.op = s.at("op").get<std::string>();
    for (auto it = s.begin(); it != s.end(); ++it) {
      if (it.key() == "name" || it.key() == "op" || it.key() == "kind") continue;
      if (kPairTestArgs.count(it.key()) && st.op != "join_multi")   // another op would ignore it: refused here
        throw std::runtime_error("step '" + st.name + "' (" + st.op + "): " + it.key() + " is join_multi's only");
      if (kExprArgs.count(it.key())) asts.emplace_back(it.key(), it.value());
      else {
        st.args[it.key()] = it.value();
        collect_refs(it.value(), st.refs);
      }
    }
    for (auto& r : st.refs)
      if (!seen.count(r)) throw std::runtime_error("step '" + st.name + "' refs unknown/later step '" + r + "'");
    seen.insert(st.name);
    b.steps.push_back(std::move(st));
    b.expr_asts.push_back(std::move(asts));
  }
  b.output = flat.value("output", b.steps.empty() ? "" : b.steps.back().name);
  if (!seen.count(b.output)) throw std::runtime_error("output step '" + b.output + "' does not exist");
  return b;
}

Plan bind_plan(const PlanBody& b, const json* params, const Schema& schema) {
  Plan p;
  p.name = b.name;
  p.body_name = b.body_name;
  p.body_sha = b.body_sha;
  p.hints = b.hints;
  p.output = b.output;
  p.bindings.schema = &schema;
  p.bindings.params = coerce_params(params ? *params : b.bind, b.params_decl);
  // lets: declaration order, each against params + earlier lets, never a row
  for (auto it = b.lets.begin(); it != b.lets.end(); ++it) {
    auto e = Expr::compile(it.value(), p.bindings);
    p.bindings.lets[it.key()] = e->eval(Value::row({}));
  }
  p.steps = b.steps;
  for (size_t i = 0; i < p.steps.size(); i++)
    for (auto& [arg, ast] : b.expr_asts[i]) {
      p.steps[i].exprs[arg] = Expr::compile(ast, p.bindings);
      p.steps[i].expr_asts[arg] = ast;
    }
  return p;
}

Plan bind_plan(const json& ir, const json* params, const std::set<std::string>& caps, const Schema& schema,
               const CapsOfLabel& of_label, const std::string& scale) {
  return bind_plan(prepare_plan(ir, caps, of_label, scale), params, schema);
}

// --- run-time arg resolution -------------------------------------------------------------

static Value resolve_value(const json& v, const Bindings& b, const std::map<std::string, Value>& outputs) {
  if (is_value_form(v)) {
    const std::string& kind = v.begin().key();
    const json& arg = v.begin().value();
    if (kind == "ref") {
      auto it = outputs.find(arg.get<std::string>());
      if (it == outputs.end()) throw std::runtime_error("ref to missing step output '" + arg.get<std::string>() + "'");
      return it->second;
    }
    if (kind == "param") {
      auto it = b.params.find(arg.get<std::string>());
      if (it == b.params.end()) throw std::runtime_error("unbound param '" + arg.get<std::string>() + "'");
      return it->second;
    }
    if (kind == "let") {
      auto it = b.lets.find(arg.get<std::string>());
      if (it == b.lets.end()) throw std::runtime_error("unknown let '" + arg.get<std::string>() + "'");
      return it->second;
    }
    return from_json(arg);   // lit
  }
  if (v.is_array()) {
    ValueList items;
    items.reserve(v.size());
    for (auto& x : v) items.push_back(resolve_value(x, b, outputs));
    return Value::list(std::move(items));
  }
  if (v.is_object()) {
    RowFields f;
    for (auto it = v.begin(); it != v.end(); ++it) f.emplace_back(it.key(), resolve_value(it.value(), b, outputs));
    return Value::row(std::move(f));
  }
  return from_json(v);
}

static void count_rows_in(const json& v, const std::map<std::string, Value>& outputs, RowsIn& n, bool labels) {
  static const std::string kVid = "_vid";         // built once: this runs per input row
  auto labelled = [](const Value& x) {
    const Value* id = x.is_row() ? x.find(kVid) : &x;
    return id && id->label >= 0;
  };
  if (is_value_form(v)) {
    if (v.begin().key() != "ref") return;
    auto it = outputs.find(v.begin().value().get<std::string>());
    if (it == outputs.end()) return;             // resolve_args reports a missing ref
    const Value& out = it->second;
    if (out.is_list() || out.is_tuple()) {
      n.rows += out.as_list().size();
      if (labels) for (auto& x : out.as_list()) n.labelled += labelled(x);
    } else {
      n.rows += 1;
      if (labels) n.labelled += labelled(out);
    }
  } else if (v.is_array() || v.is_object()) {
    for (auto& x : v) count_rows_in(x, outputs, n, labels);
  }
}

RowsIn rows_in(const Step& s, const std::map<std::string, Value>& outputs, bool labels) {
  RowsIn n;
  count_rows_in(s.args, outputs, n, labels);
  return n;
}

Args resolve_args(const Step& s, const Bindings& b, const std::map<std::string, Value>& outputs) {
  Args a;
  for (auto it = s.args.begin(); it != s.args.end(); ++it) a.set(it.key(), resolve_value(it.value(), b, outputs));
  return a;
}

const Value Args::null_ = Value();

ValueList* Args::owned_list(const std::string& k) {
  auto it = m_.find(k);
  if (it == m_.end()) return nullptr;
  ListPtr* p = std::get_if<ListPtr>(&it->second.v);
  if (!p || !*p || p->use_count() != 1) return nullptr;
  return const_cast<ValueList*>(p->get());   // the one holder: a list is const only while it may be shared
}

Value Args::take(const std::string& k) {
  auto it = m_.find(k);
  if (it == m_.end()) return Value();
  return std::move(it->second);
}

json Args::to_json() const {
  json o = json::object();
  for (auto& [k, v] : m_) o[k] = evalcpp::to_json(v);
  return o;
}

json Args::tags_json() const {
  json o = json::object();
  auto tag_of = [](const Value& v) -> json {
    const Value* x = v.is_row() ? v.find("_vid") : &v;
    return x && x->label >= 0 ? json(int(x->label)) : json(nullptr);
  };
  for (auto& [k, v] : m_) {
    if (v.is_list()) {
      json a = json::array();
      bool any = false;
      for (auto& item : v.as_list()) {
        a.push_back(tag_of(item));
        any = any || !a.back().is_null();
      }
      if (any) o[k] = a;
    } else if (v.label >= 0) {
      o[k] = int(v.label);
    }
  }
  return o;
}

bool Args::has(const std::string& k) const {
  auto it = m_.find(k);
  return it != m_.end() && !it->second.is_null();
}
const Value& Args::get(const std::string& k) const {
  auto it = m_.find(k);
  if (it == m_.end()) throw std::runtime_error("missing argument '" + k + "'");
  return it->second;
}
const Value& Args::opt(const std::string& k) const {
  auto it = m_.find(k);
  return it == m_.end() ? null_ : it->second;
}
std::string Args::str(const std::string& k) const { return get(k).text(); }   // a vid as its hex16 text
int64_t Args::integer(const std::string& k) const { return get(k).as_int(); }
bool Args::flag(const std::string& k, bool dflt) const {
  const Value& v = opt(k);
  return v.is_null() ? dflt : v.truthy();
}
const ValueList& Args::list(const std::string& k) const { return get(k).as_list(); }
std::vector<std::string> Args::str_list(const std::string& k) const {
  std::vector<std::string> out;
  for (auto& x : list(k)) out.push_back(x.text());
  return out;
}
std::vector<std::string> Args::labels(const std::string& k) const {
  const Value& v = opt(k);
  if (v.is_null()) return {};
  if (v.is_str()) return {v.as_str()};
  return str_list(k);
}

}  // namespace evalcpp
