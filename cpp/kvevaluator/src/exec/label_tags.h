// Which reads tag the vertex ids they output with a label (Value::label: the label travels on the row). A step knows a
// vertex's label only if the value that reached it carries it -- what an upstream read of the same run found, carried
// by the plan's own dataflow; nothing is remembered outside it. run.label_tags = {"hops": bool, "probes": bool}.
#pragma once

#include <algorithm>
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

#include "schema/schema.h"
#include "value/value.h"

namespace evalcpp {

struct LabelTagPolicy {
  bool hops = true;     // a hop tags its neighbours (meta: the schema's one label, or the far end's stored code)
  bool probes = true;   // a probe that hit (a props read, a filter, an existence check) tags what it outputs
};

// {vid: the label its first tagged occurrence in one step's input carries}: how a step reads a vid that arrives more
// than once. strict (the checkers' trace mode): a vid carrying two different labels is a bug upstream, so it throws
inline std::unordered_map<std::string, int16_t> first_tags(const ValueList& vs, bool strict) {
  std::unordered_map<std::string, int16_t> tag;
  for (auto& v : vs) {
    if (v.label < 0) continue;
    auto [it, fresh] = tag.emplace(v.text(), v.label);   // a vid by its text, as distinct_strs keys it
    if (strict && !fresh && it->second != v.label)
      throw std::runtime_error("labels on rows: vid " + v.text() + " carries two labels in one step's input (ids " +
                               std::to_string(it->second) + " and " + std::to_string(v.label) + ")");
  }
  return tag;
}

// --- what every format does with a label on a value (neutral: no format owns these) ---------------------------------

// the label a value carries, by name; none when it carries none
inline std::optional<std::string> tag_label(const Schema& schema, const Value& v) {
  if (v.label < 0) return std::nullopt;
  for (auto& vd : schema.vertices())
    if (vd.id == v.label) return vd.label;
  return std::nullopt;
}

// `v` carrying `label` (an empty label: unchanged)
inline Value tagged(const Schema& schema, Value v, const std::string& label) {
  if (!label.empty()) v.label = int16_t(schema.vertex(label).id);
  return v;
}

// every vid of a list carrying `label`: the output of a read that is per label (an index, a label scan, a by-id read)
inline Value tag_all(const Schema& schema, const Value& vids, const std::string& label) {
  ValueList out;
  out.reserve(vids.as_list().size());
  for (auto& v : vids.as_list()) out.push_back(tagged(schema, v, label));
  return Value::list(std::move(out));
}

// the candidate labels of one vid: the carried label alone when it is one of `labs`, none when it is another label,
// `labs` as given when the value carries none
inline std::vector<std::string> narrow(const Schema& schema, const Value& vid, const std::vector<std::string>& labs) {
  std::optional<std::string> k = tag_label(schema, vid);
  if (!k) return labs;
  if (std::find(labs.begin(), labs.end(), *k) != labs.end()) return {*k};
  return {};
}

// each distinct vid as the step's input carries it: its first TAGGED occurrence (first_tags), else the bare string.
// `distinct`: the input's distinct vids in first-seen order (formats/common.h distinct_strs)
inline std::vector<Value> distinct_values(const ValueList& vs, const std::vector<StrPtr>& distinct, bool strict) {
  std::vector<Value> out;
  out.reserve(distinct.size());
  for (auto& d : distinct) out.push_back(Value(d));
  bool any = false;
  for (auto& v : vs) any = any || v.label >= 0;
  if (!any) return out;
  std::unordered_map<std::string, int16_t> tag = first_tags(vs, strict);
  for (auto& v : out) {
    auto it = tag.find(v.as_str());
    if (it != tag.end()) v.label = it->second;
  }
  return out;
}

}  // namespace evalcpp
