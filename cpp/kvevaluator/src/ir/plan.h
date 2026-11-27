// The plan IR (Python planir/{interp,bundle,capabilities}.py): load a plan or capability
// bundle, select the variant a format's capabilities allow, inline fragments, bind params
// and lets, and resolve each step's args at run time.
#pragma once
#include <functional>
#include <map>
#include <memory>
#include <set>
#include <string>
#include <vector>

#include "ir/expr.h"
#include "value/value.h"

namespace evalcpp {

// The fixed capability vocabulary (inputs/contract/capabilities.json, compiled in) -- extend deliberately.
const std::set<std::string>& capability_vocabulary();

struct Step {
  std::string name;
  std::string op;
  json args;                                            // raw args (name/op/kind stripped)
  std::map<std::string, std::shared_ptr<Expr>> exprs;   // compiled pred / fn
  std::map<std::string, json> expr_asts;                // the same pred / fn as written (predict mode reads them)
  std::vector<std::string> refs;                        // step names this step reads
};

struct Plan {
  std::string name;
  std::string body_name;   // the body run: the one body, or the parked files' selector's ("" for a bare plan)
  std::string body_sha;  // body_sha_of(): sha256[:12] of the selected, fragment-expanded body {steps, output}
  json hints;            // the plan file's estimate_hints: what predict's cardinality rules may assume (no hash reads it)
  std::vector<Step> steps;
  std::string output;
  Bindings bindings;
};

// Resolved arguments of one step: value-forms replaced by values, refs by the producing
// step's output (a shared copy). Typed accessors throw on a missing or mistyped arg.
class Args {
 public:
  void set(const std::string& k, Value v) { m_[k] = std::move(v); }
  bool has(const std::string& k) const;                 // present and not null
  const Value& get(const std::string& k) const;         // throws if absent
  const Value& opt(const std::string& k) const;         // null Value if absent
  std::string str(const std::string& k) const;
  int64_t integer(const std::string& k) const;
  bool flag(const std::string& k, bool dflt) const;
  const ValueList& list(const std::string& k) const;
  // the list of `k` when these args hold the only reference to it (the executor moved the input in: this step is its
  // last reader), else null: an op may then move its items out instead of copying them. No one else can see the list.
  ValueList* owned_list(const std::string& k);
  Value take(const std::string& k);                     // the value of `k`, moved out (null if absent)
  std::vector<std::string> str_list(const std::string& k) const;
  // "label" | ["Post", "Comment"] -> list of labels; empty if absent/null
  std::vector<std::string> labels(const std::string& k) const;
  json to_json() const;   // every resolved argument (the trace_args run option)
  // the vertex labels the arguments' values carry (Value::label), for the trace: per list argument its items' label
  // ids (a row: its _vid's; null = none); only the arguments that carry any
  json tags_json() const;
 private:
  std::map<std::string, Value> m_;
  static const Value null_;
};

// A variant `"on"` annotation's per-label capability resolver (Format::capabilities_for
// bound to the running format): per-label selection for a format whose layout differs per label (meta); leave
// empty for the format-global behavior. Mirrors planir/bundle.py's caps_of_label.
using CapsOfLabel = std::function<std::set<std::string>(const std::string&)>;
// The flat step list of `ir` (a plan or a bundle) for a format with `caps`, fragments inlined.
// `scale`: the store's dataset tag (its manifest's `scale` / the request's store.scale: "0.1", "3", "30", "n1m").
// A variant carrying `scales` runs only when the tag is listed; one without runs at every scale (the default).
// A scale-tagged variant with no scale known throws: a pin that differs by scale never guesses.
json flatten_ir(const json& ir, const std::set<std::string>& caps, const CapsOfLabel& of_label = {},
                const std::string& scale = "");
// The identity of the body a store runs: sha256[:12] of the compact, key-sorted JSON of {steps, output} of the
// selected variant after fragment expansion (a bare plan: its own steps). The Python twin is
// planir/bundle.py body_sha; tests/gates/check_cpp_vs_python.py asserts they agree on every cell (bodies carry no
// floats or non-ASCII, so the two serializers agree byte for byte).
std::string body_sha_of(const json& flat);
// Coerce raw params per the plan's declarations (section 9.3): int -> int64 strictly; else string.
std::map<std::string, Value> coerce_params(const json& raw, const json& decls);
// The params-free half of a bound plan: the selected variant, fragment-expanded and checked (refs, output), with each
// step's args and refs; the expressions stay ASTs (they resolve params and lets when compiled). A batch prepares a plan
// once per name and binds it per request.
struct PlanBody {
  std::string name, body_name, body_sha, output;
  json bind, params_decl, lets;                                          // the IR's own `bind`, `params`, `let`
  json hints;                                                            // the IR's estimate_hints (a query field)
  std::vector<Step> steps;                                               // exprs empty
  std::vector<std::vector<std::pair<std::string, json>>> expr_asts;      // per step: (arg, AST) of pred / fn
};
PlanBody prepare_plan(const json& ir, const std::set<std::string>& caps, const CapsOfLabel& of_label = {},
                      const std::string& scale = "");
// Bind: params (or the plan's own `bind` block), lets in declaration order, compile expressions.
Plan bind_plan(const PlanBody& body, const json* params, const Schema& schema);
Plan bind_plan(const json& ir, const json* params, const std::set<std::string>& caps, const Schema& schema,
               const CapsOfLabel& of_label = {}, const std::string& scale = "");
// The rows a step takes in, by one rule: every {"ref": step} anywhere in its arguments counts that step's output --
// a list's or tuple's length, 1 for anything else (the rule of a step's `rows`). `labelled`: how many of those top-level
// values carry a vertex label (Value::label; a row: its _vid's). Params, lets and literals are not rows in.
// `labels` off: `labelled` is not counted (it visits every input row; a latency run turns it off).
struct RowsIn { uint64_t rows = 0, labelled = 0; };
RowsIn rows_in(const Step& s, const std::map<std::string, Value>& outputs, bool labels = true);
// Resolve one step's args against the outputs so far.
Args resolve_args(const Step& s, const Bindings& b, const std::map<std::string, Value>& outputs);

}  // namespace evalcpp
