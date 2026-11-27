#include "formats/arangodb/vpack.h"
#include "codec/decode_counts.h"

#include <cstring>
#include <map>
#include <stdexcept>

#include "util/uidset.h"
#include "util/vid.h"

namespace evalcpp::arangodb {

// the head registry (Python formats/arangodb.py, byte for byte)
namespace {
constexpr uint8_t kNull = 0x00, kFalse = 0x01, kTrue = 0x02, kDouble = 0x03;
constexpr uint8_t kVid = 0x04, kRef = 0x05;
constexpr uint8_t kSysBase = 0x06;                 // _key, _rev, _id, _from, _to
constexpr uint8_t kSmall = 0x10;                   // + (x+6): ints -6..9, head-only
constexpr uint8_t kInt = 0x20;                     // + width 1..8: signed little-endian
constexpr uint8_t kArray = 0x30;                   // + count(1) + per-element 1B length + bodies
constexpr uint8_t kObj = 0x40;                     // + wcode 0..3 (w = 1/2/4/8)
constexpr uint8_t kBytes = 0x50;                   // + wcode 0..3: a w-byte big-endian length, then the raw bytes
                                                   // (VelocyPack's own binary types 0xc0-0xc7 also carry a 1-8 B length)
constexpr uint8_t kStr = 0x80;                     // | len 0..126
constexpr uint8_t kStrLong = 0xFF;                 // + 8B length
const char* kSysNames[5] = {"_key", "_rev", "_id", "_from", "_to"};
constexpr size_t kObjW[4] = {1, 2, 4, 8};

int sys_code(const std::string& name) {
  for (int i = 0; i < 5; i++)
    if (name == kSysNames[i]) return kSysBase + i;
  return -1;
}

bool canonical_vid(const std::string& s) {   // EXACTLY vids.vid()'s 16-lowercase-hex rendering
  if (s.size() != 16) return false;
  for (char c : s)
    if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
  return true;
}

void put_be(std::string& out, uint64_t x, size_t w) {
  for (size_t i = w; i-- > 0;) out.push_back(char((x >> (8 * i)) & 0xff));
}
uint64_t get_be(std::string_view b, size_t i, size_t w) {
  // a truncated or corrupt document: throw before the read, never read past the buffer
  if (i > b.size() || w > b.size() - i) throw std::runtime_error("vpack codec: truncated");
  uint64_t x = 0;
  for (size_t k = 0; k < w; k++) x = (x << 8) | uint8_t(b[i + k]);
  return x;
}

size_t int_byte_width(int64_t x) {   // little-endian signed payload bytes (1..8)
  size_t n = 1;
  while (n < 9 && (x < -(int64_t(1) << (8 * n - 1)) || x >= (int64_t(1) << (8 * n - 1)))) n++;
  return n;
}

void vp_encode_val(std::string& out, const Value& v);

}  // namespace

std::string vp_encode_str(const std::string& s) {
  std::string out;
  if (canonical_vid(s)) {
    out.push_back(char(kVid));
    put_be(out, *uid_of(s), 8);
    return out;
  }
  size_t slash = s.find('/');
  if (slash != std::string::npos && slash > 0 && slash < 256) {
    std::string v = s.substr(slash + 1);
    if (canonical_vid(v) && slash < 256) {
      out.push_back(char(kRef));
      out.push_back(char(slash));
      out += s.substr(0, slash);
      put_be(out, *uid_of(v), 8);
      return out;
    }
  }
  if (s.size() <= 126) {
    out.push_back(char(kStr | uint8_t(s.size())));
    out += s;
  } else {
    out.push_back(char(kStrLong));
    put_be(out, s.size(), 8);
    out += s;
  }
  return out;
}

namespace {
void vp_encode_val(std::string& out, const Value& v) {
  if (v.is_null()) { out.push_back(char(kNull)); return; }
  if (v.is_bool()) { out.push_back(char(v.as_bool() ? kTrue : kFalse)); return; }
  if (v.is_int()) {
    int64_t x = v.as_int();
    if (-6 <= x && x <= 9) { out.push_back(char(kSmall + uint8_t(x + 6))); return; }
    size_t w = int_byte_width(x);
    if (w > 8) throw std::runtime_error("vpack codec: int exceeds 8 bytes");
    out.push_back(char(kInt + uint8_t(w)));
    for (size_t k = 0; k < w; k++) out.push_back(char((uint64_t(x) >> (8 * k)) & 0xff));   // little-endian
    return;
  }
  if (v.is_vid()) { out.push_back(char(kVid)); put_be(out, v.as_vid(), 8); return; }   // vp_encode_str's bytes for its hex16
  if (v.is_str()) { out += vp_encode_str(v.as_str()); return; }
  if (v.is_bytes()) {   // the smallest length width that fits, as an object's frame
    const std::string& raw = v.as_bytes();
    size_t wc = 0;
    while (wc < 3 && raw.size() >> (8 * kObjW[wc])) wc++;
    out.push_back(char(kBytes + wc));
    put_be(out, raw.size(), kObjW[wc]);
    out += raw;
    return;
  }
  if (v.is_row()) { vp_encode_obj(out, v); return; }
  if (v.is_list() || v.is_tuple()) {
    const ValueList& xs = v.as_list();
    std::vector<std::string> bodies;
    for (auto& e : xs) { std::string b; vp_encode_val(b, e); bodies.push_back(std::move(b)); }
    if (xs.size() > 255) throw std::runtime_error("vpack codec: array too large for the 1-byte element table");
    for (auto& b : bodies)
      if (b.size() > 255) throw std::runtime_error("vpack codec: array element too large for the 1-byte table");
    out.push_back(char(kArray));
    out.push_back(char(uint8_t(xs.size())));
    for (auto& b : bodies) out.push_back(char(uint8_t(b.size())));
    for (auto& b : bodies) out += b;
    return;
  }
  throw std::runtime_error("vpack codec: unsupported value type");
}
}  // namespace

void vp_encode_obj(std::string& out, const Value& doc) {
  // members (key + value) in insertion order, then the frame whose widths step up with size:
  // head(1) + byteLength(w) + members + one offset per member + nrItems(w)
  std::string members;
  std::vector<size_t> offsets;
  for (auto& [k, val] : doc.as_row()) {
    offsets.push_back(members.size());
    int code = sys_code(k);
    if (code >= 0) members.push_back(char(uint8_t(code)));
    else members += vp_encode_str(k);
    vp_encode_val(members, val);
  }
  size_t n = doc.as_row().size();
  const uint64_t thresh[4] = {256, 65536, 4294967296ull, ~0ull};
  for (int wcode = 0; wcode < 4; wcode++) {
    size_t w = kObjW[wcode];
    uint64_t total = members.size() + 1 + w + n * w + w;
    if (total < thresh[wcode]) {
      size_t head_off = 1 + w;                   // members start after head + byteLength
      out.push_back(char(kObj + uint8_t(wcode)));
      put_be(out, total, w);
      out += members;
      for (size_t o : offsets) put_be(out, head_off + o, w);
      put_be(out, n, w);
      return;
    }
  }
  throw std::runtime_error("vpack codec: object too large");
}

Value vp_decode(std::string_view b, size_t& i) {
  if (i >= b.size()) throw std::runtime_error("vpack codec: truncated");
  uint8_t h = uint8_t(b[i++]);
  if (h == kNull) return Value();
  if (h == kFalse) return Value(false);
  if (h == kTrue) return Value(true);
  if (h == kDouble) throw std::runtime_error("vpack codec: double values are not part of the LDBC data");
  if (h == kVid) { Value v = Value::vid(get_be(b, i, 8)); i += 8; return v; }   // written only for a canonical vid
  if (h == kRef) {
    size_t ln = uint8_t(b[i]);
    std::string label(b.substr(i + 1, ln));
    uint64_t u = get_be(b, i + 1 + ln, 8);
    i += 9 + ln;
    return Value(label + "/" + hex16(u));
  }
  if (kSmall <= h && h < kSmall + 16) return Value(int64_t(h - kSmall) - 6);
  if (kInt < h && h <= kInt + 8) {
    size_t w = h - kInt;
    uint64_t x = 0;
    for (size_t k = 0; k < w; k++) x |= uint64_t(uint8_t(b[i + k])) << (8 * k);
    i += w;
    // sign-extend the little-endian payload
    if (w < 8 && (x >> (8 * w - 1)) & 1) x |= ~((uint64_t(1) << (8 * w)) - 1);
    return Value(int64_t(x));
  }
  if (h == kArray) {
    size_t n = uint8_t(b[i]);
    std::string_view lens = b.substr(i + 1, n);
    size_t j = i + 1 + n;
    ValueList out;
    for (size_t k = 0; k < n; k++) {
      size_t j0 = j;
      out.push_back(vp_decode(b, j));
      if (j - j0 != uint8_t(lens[k])) throw std::runtime_error("vpack codec: array element length mismatch");
    }
    i = j;
    return Value::list(std::move(out));
  }
  if (kObj <= h && h < kObj + 4) {
    size_t w = kObjW[h - kObj];
    uint64_t total = get_be(b, i, w);
    size_t end = i - 1 + size_t(total);
    size_t n = size_t(get_be(b, end - w, w));
    size_t j = i + w;
    size_t stop = end - w * (n + 1);             // members end where the offset table starts
    RowFields f;
    while (j < stop) {
      std::string key;
      uint8_t kh = uint8_t(b[j]);
      if (kSysBase <= kh && kh < kSysBase + 5) { key = kSysNames[kh - kSysBase]; j++; }
      else key = vp_decode(b, j).text();   // a name in a vid's form decodes as a vid
      Value val = vp_decode(b, j);
      f.emplace_back(std::move(key), std::move(val));
    }
    if (f.size() != n) throw std::runtime_error("vpack codec: decoded member count != frame");
    i = end;
    return Value::row(std::move(f));
  }
  if (h == kStrLong) {
    size_t ln = size_t(get_be(b, i, 8));
    Value v(std::string(b.substr(i + 8, ln)));
    i += 8 + ln;
    return v;
  }
  if (kBytes <= h && h < kBytes + 4) {
    size_t w = kObjW[h - kBytes];
    size_t ln = size_t(get_be(b, i, w));
    if (i + w + ln > b.size()) throw std::runtime_error("vpack codec: truncated bytes");
    Value v = Value::bytes(std::string(b.substr(i + w, ln)));
    i += w + ln;
    return v;
  }
  if (h & 0x80) {
    size_t ln = h & 0x7F;
    Value v(std::string(b.substr(i, ln)));
    i += ln;
    return v;
  }
  throw std::runtime_error("vpack codec: unknown head");
}

// advance i past the value at b[i], exactly as vp_decode does, without building it
static void vp_skip(std::string_view b, size_t& i) {
  if (i >= b.size()) throw std::runtime_error("vpack codec: truncated");
  uint8_t h = uint8_t(b[i++]);
  if (h == kNull || h == kFalse || h == kTrue) return;
  if (h == kDouble) throw std::runtime_error("vpack codec: double values are not part of the LDBC data");
  if (h == kVid) { i += 8; return; }
  if (h == kRef) { i += 9 + uint8_t(b[i]); return; }
  if (kSmall <= h && h < kSmall + 16) return;
  if (kInt < h && h <= kInt + 8) { i += h - kInt; return; }
  if (h == kArray) {
    size_t n = uint8_t(b[i]);
    std::string_view lens = b.substr(i + 1, n);
    size_t j = i + 1 + n;
    for (size_t k = 0; k < n; k++) j += uint8_t(lens[k]);   // the element table gives the spans
    i = j;
    return;
  }
  if (kObj <= h && h < kObj + 4) {
    size_t w = kObjW[h - kObj];
    i = i - 1 + size_t(get_be(b, i, w));   // the frame's byteLength spans the whole object
    return;
  }
  if (h == kStrLong) { i += 8 + size_t(get_be(b, i, 8)); return; }
  if (kBytes <= h && h < kBytes + 4) { size_t w = kObjW[h - kBytes]; i += w + size_t(get_be(b, i, w)); return; }
  if (h & 0x80) { i += h & 0x7F; return; }
  throw std::runtime_error("vpack codec: unknown head");
}

// the member key at b[j] (a dictionary code or a string), advancing j
static std::string vp_member_key(std::string_view b, size_t& j) {
  uint8_t kh = uint8_t(b[j]);
  if (kSysBase <= kh && kh < kSysBase + 5) { j++; return kSysNames[kh - kSysBase]; }
  return vp_decode(b, j).text();
}

RowFields vp_decode_fields(std::string_view b, const std::vector<std::string>& fields) {
  if (b.empty()) throw std::runtime_error("vpack codec: truncated");
  size_t i = 0;
  uint8_t h = uint8_t(b[i++]);
  if (!(kObj <= h && h < kObj + 4)) throw std::runtime_error("vp_decode_fields: not an object");
  size_t w = kObjW[h - kObj];
  uint64_t total = get_be(b, i, w);
  size_t end = i - 1 + size_t(total);
  size_t n = size_t(get_be(b, end - w, w));
  size_t j = i + w;
  size_t stop = end - w * (n + 1);
  RowFields f;
  f.reserve(fields.size());
  size_t members = 0;
  while (j < stop) {
    std::string key = vp_member_key(b, j);
    bool wanted = false;
    for (auto& x : fields)
      if (x == key) { wanted = true; break; }
    if (wanted) f.emplace_back(std::move(key), vp_decode(b, j));
    else vp_skip(b, j);
    members++;
  }
  // the whole-doc decoder's integrity checks, kept: a corrupt doc throws, never yields wrong cells
  if (members != n) throw std::runtime_error("vpack codec: decoded member count != frame");
  if (end != b.size()) throw std::runtime_error("vpack codec: trailing bytes");
  DecodeCounts& d = decode_counts();    // one document walked whole, a field per member decoded (the others skipped)
  d.docs++; d.fields += f.size(); d.bytes += b.size();
  return f;
}

std::string vp_ref_vid(std::string_view b) {
  { DecodeCounts& d = decode_counts(); d.cells++; d.fields++; d.bytes += b.size(); }   // one reference value
  if (b.size() >= 2 && uint8_t(b[0]) == kRef) {
    size_t ln = uint8_t(b[1]);
    return hex16(get_be(b, 2 + ln, 8));
  }
  size_t i = 0;                                  // any other string form: the text after '/'
  std::string ref = vp_decode(b, i).text();
  return ref.substr(ref.find('/') + 1);
}

uint64_t vp_ref_uid(std::string_view b) {
  if (b.size() >= 2 && uint8_t(b[0]) == kRef) {
    { DecodeCounts& d = decode_counts(); d.cells++; d.fields++; d.bytes += b.size(); }   // as vp_ref_vid counts it
    return get_be(b, 2 + uint8_t(b[1]), 8);
  }
  std::string v = vp_ref_vid(b);                 // any other string form: its text after '/', parsed
  uint64_t u;
  if (!evalcpp::canonical_vid(v, &u)) throw std::runtime_error("vpack codec: reference '" + v + "' holds no vid (16 lowercase hex)");
  return u;
}

std::string vp_ref_label(std::string_view b) {
  // the other half of the same reference value: a field, no bytes (vp_ref_vid counted the value's)
  { DecodeCounts& d = decode_counts(); d.cells++; d.fields++; }
  if (b.size() >= 2 && uint8_t(b[0]) == kRef) return std::string(b.substr(2, uint8_t(b[1])));
  size_t i = 0;                                  // any other string form: the text before '/'
  std::string ref = vp_decode(b, i).text();
  return ref.substr(0, ref.find('/'));
}

size_t vp_object_size(std::string_view b) {
  if (b.empty() || !(kObj <= uint8_t(b[0]) && uint8_t(b[0]) < kObj + 4)) throw std::runtime_error("vp_object_size: not an object");
  size_t w = kObjW[uint8_t(b[0]) - kObj];
  if (b.size() < 1 + w) throw std::runtime_error("vpack codec: truncated");
  size_t total = size_t(get_be(b, 1, w));
  if (total > b.size()) throw std::runtime_error("vpack codec: truncated object");
  return total;
}

Value vp_decode_doc(std::string_view raw) {
  size_t i = 0;
  Value v = vp_decode(raw, i);
  if (i != raw.size()) throw std::runtime_error("vpack codec: trailing bytes");
  DecodeCounts& d = decode_counts();    // codec/decode_counts.h: one document, a field per member
  d.docs++; d.fields += v.is_row() ? v.as_row().size() : 1; d.bytes += raw.size();
  return v;
}

// KV-amp: the FRAMED bytes of the named members inside a doc -- each member's key bytes
// (dictionary code or string form) plus its value's encoded span. Walks the same member
// region the decoder does; the object frame (head, byteLength, offset table, nrItems) is
// shared framing, deliberately unattributed (amplification for a projection).
uint64_t vp_doc_field_bytes(std::string_view b, const std::vector<std::string>& fields) {
  size_t i = 0;
  uint8_t h = uint8_t(b[i++]);
  if (!(kObj <= h && h < kObj + 4)) throw std::runtime_error("vp_doc_field_bytes: not an object");
  size_t w = kObjW[h - kObj];
  uint64_t total = get_be(b, i, w);
  size_t end = i - 1 + size_t(total);
  size_t n = size_t(get_be(b, end - w, w));
  size_t j = i + w;
  size_t stop = end - w * (n + 1);
  uint64_t used = 0;
  while (j < stop) {
    size_t member_start = j;
    std::string key = vp_member_key(b, j);
    vp_skip(b, j);                               // advance past the value
    for (auto& f : fields)
      if (f == key) { used += j - member_start; break; }
  }
  return used;
}

}  // namespace evalcpp::arangodb
