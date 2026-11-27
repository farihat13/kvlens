// The tagged primitive value codec (Python store/valcodec.py, byte-identical): the reversible
// byte form of the neutral leaf shapes that a format stores outside its packed layouts (dgraph's
// scalar predicates, facet dicts, the [label] type record). One tag byte per value:
//   i + i64 BE | f + f64 BE | s + uvarint len + UTF-8 | n (null) | T / F (bool) |
//   l / t + uvarint count + tagged elements (list / tuple) | d + uvarint count + (uvarint
//   key len + key + tagged value) pairs in insertion order.
// Top-level convention: null <-> "" (encode_leaf / decode_leaf); any other value is >= 1 byte.
#pragma once
#include <string>
#include <string_view>

#include "value/value.h"

namespace evalcpp::prim {

void encode(std::string& out, const Value& v);      // the tagged form (containers nest it)
std::string encode_leaf(const Value& v);            // null -> "", else the tagged form
Value decode(std::string_view b, size_t& i);        // one tagged value at b[i]; advances i
Value decode_leaf(std::string_view raw);            // "" -> null; throws on trailing bytes

}  // namespace evalcpp::prim
