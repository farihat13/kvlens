// The property codec knob, one place for its values and its default (every format that can store property
// NAMES per value reads it: dgraph (posting-entry facets), arangodb
// (edge and vertex documents); meta does not: its spec has had no codec field since meta-v4).
//   named  = every property NAME stored with its value, per edge (dgraph's facets, arango's documents):
//            the engines' habit, kept only to reproduce them
//   schema = values only, the RowV2 row of the etype's declared columns (the row a Split edge record
//            holds): the fair setting, the default (the fairness rule "one edge-property codec for every layout")
#pragma once
#include <stdexcept>
#include <string>

#include "value/value.h"

namespace evalcpp {

enum class PropsCodec { named, schema };

constexpr PropsCodec kPropsCodecDefault = PropsCodec::schema;

inline PropsCodec parse_props_codec(const std::string& s) {
  if (s == "named") return PropsCodec::named;
  if (s == "schema") return PropsCodec::schema;
  throw std::runtime_error("props_codec must be \"named\" or \"schema\", got \"" + s + "\"");
}

inline const char* props_codec_name(PropsCodec c) { return c == PropsCodec::schema ? "schema" : "named"; }

// a format Config's "props_codec" field; absent = the default (a request that names no codec gets the fair one)
inline PropsCodec props_codec_from_json(const json& j) {
  if (!j.is_object() || !j.contains("props_codec")) return kPropsCodecDefault;
  if (!j.at("props_codec").is_string()) throw std::runtime_error("props_codec must be a string (named | schema)");
  return parse_props_codec(j.at("props_codec").get<std::string>());
}

}  // namespace evalcpp
