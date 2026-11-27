#include "codec/prim.h"
#include "codec/decode_counts.h"

#include <cstring>
#include <stdexcept>

#include "codec/varint.h"

namespace evalcpp::prim {

static void put_i64be(std::string& out, int64_t v) {
  uint64_t x = uint64_t(v);
  for (int i = 7; i >= 0; i--) out.push_back(char((x >> (8 * i)) & 0xff));
}

void encode(std::string& out, const Value& v) {
  if (v.is_null()) { out.push_back('n'); return; }
  if (v.is_bool()) { out.push_back(v.as_bool() ? 'T' : 'F'); return; }
  if (v.is_int()) { out.push_back('i'); put_i64be(out, v.as_int()); return; }
  if (v.is_str()) {
    const std::string& s = v.as_str();
    out.push_back('s');
    put_uvarint(out, s.size());
    out += s;
    return;
  }
  if (v.is_bytes()) {   // the same framing as a str, under its own tag (valcodec.py's _BYTES)
    const std::string& s = v.as_bytes();
    out.push_back('b');
    put_uvarint(out, s.size());
    out += s;
    return;
  }
  if (v.is_list() || v.is_tuple()) {
    out.push_back(v.is_list() ? 'l' : 't');
    put_uvarint(out, v.as_list().size());
    for (auto& x : v.as_list()) encode(out, x);
    return;
  }
  if (v.is_row()) {
    out.push_back('d');
    put_uvarint(out, v.as_row().size());
    for (auto& kv : v.as_row()) {
      put_uvarint(out, kv.first.size());
      out += kv.first;
      encode(out, kv.second);
    }
    return;
  }
  throw std::runtime_error("prim: unsupported value type");
}

std::string encode_leaf(const Value& v) {
  std::string out;
  if (!v.is_null()) encode(out, v);
  return out;
}

static Value decode_one(std::string_view b, size_t& i) {
  if (i >= b.size()) throw std::runtime_error("prim: truncated");
  char tag = b[i++];
  switch (tag) {
    case 'n': return Value();
    case 'T': return Value(true);
    case 'F': return Value(false);
    case 'i': {
      if (i + 8 > b.size()) throw std::runtime_error("prim: truncated int");
      uint64_t x = 0;
      for (size_t k = 0; k < 8; k++) x = (x << 8) | uint8_t(b[i + k]);
      i += 8;
      return Value(int64_t(x));
    }
    case 'f': throw std::runtime_error("prim: float values are not part of the LDBC data");
    case 's': {
      size_t n = size_t(get_uvarint(b, i));
      if (i + n > b.size()) throw std::runtime_error("prim: truncated str");
      Value v(std::string(b.substr(i, n)));
      i += n;
      return v;
    }
    case 'b': {
      size_t n = size_t(get_uvarint(b, i));
      if (i + n > b.size()) throw std::runtime_error("prim: truncated bytes");
      Value v = Value::bytes(std::string(b.substr(i, n)));
      i += n;
      return v;
    }
    case 'l': case 't': {
      size_t n = size_t(get_uvarint(b, i));
      ValueList items;
      items.reserve(n);
      for (size_t k = 0; k < n; k++) items.push_back(decode_one(b, i));
      return tag == 'l' ? Value::list(std::move(items)) : Value::tuple(std::move(items));
    }
    case 'd': {
      size_t n = size_t(get_uvarint(b, i));
      RowFields f;
      for (size_t k = 0; k < n; k++) {
        size_t kl = size_t(get_uvarint(b, i));
        std::string key(b.substr(i, kl));
        i += kl;
        Value val = decode_one(b, i);
        f.emplace_back(std::move(key), std::move(val));
      }
      return Value::row(std::move(f));
    }
  }
  throw std::runtime_error(std::string("prim: unknown tag '") + tag + "'");
}

// one stored prim value, counted (codec/decode_counts.h): a cell; a dict counts a field per member
Value decode(std::string_view b, size_t& i) {
  size_t at = i;
  Value v = decode_one(b, i);
  DecodeCounts& d = decode_counts();
  d.cells++; d.fields += v.is_row() ? v.as_row().size() : 1; d.bytes += i - at;
  return v;
}

Value decode_leaf(std::string_view raw) {
  if (raw.empty()) return Value();
  size_t i = 0;
  Value v = decode(raw, i);
  if (i != raw.size()) throw std::runtime_error("prim: trailing bytes");
  return v;
}

}  // namespace evalcpp::prim
