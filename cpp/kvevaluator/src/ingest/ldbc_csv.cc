#include "ingest/ldbc_csv.h"

#include <algorithm>
#include <cstring>
#include <cstdio>
#include <fstream>
#include <stdexcept>
#include <unordered_map>

#include "util/vid.h"

namespace evalcpp::ldbc_csv {

namespace {

// The CSV file mapping -- loader knowledge, ported verbatim from Python VERTEX_FILES /
// EDGE_FILES / PERSON_MULTI (everything else comes from the schema).
struct VFile { const char* subdir; const char* fname; std::string label; };
struct EFile { const char* subdir; const char* fname; std::string etype, src_label, dst_label; };
const std::vector<VFile>& vertex_files() {
  static const std::vector<VFile> v = {
      {"dynamic", "person_0_0.csv", "Person"},
      {"dynamic", "post_0_0.csv", "Post"},
      {"dynamic", "comment_0_0.csv", "Comment"},
      {"dynamic", "forum_0_0.csv", "Forum"},
      {"static", "organisation_0_0.csv", "Organisation"},
      {"static", "place_0_0.csv", "Place"},
      {"static", "tag_0_0.csv", "Tag"},
      {"static", "tagclass_0_0.csv", "TagClass"},
  };
  return v;
}
const std::vector<EFile>& edge_files() {
  static const std::vector<EFile> e = {
      {"dynamic", "person_knows_person_0_0.csv", "knows", "Person", "Person"},
      {"dynamic", "person_likes_post_0_0.csv", "likes", "Person", "Post"},
      {"dynamic", "person_likes_comment_0_0.csv", "likes", "Person", "Comment"},
      {"dynamic", "person_hasInterest_tag_0_0.csv", "hasInterest", "Person", "Tag"},
      {"dynamic", "person_isLocatedIn_place_0_0.csv", "isLocatedIn", "Person", "Place"},
      {"dynamic", "person_studyAt_organisation_0_0.csv", "studyAt", "Person", "Organisation"},
      {"dynamic", "person_workAt_organisation_0_0.csv", "workAt", "Person", "Organisation"},
      {"dynamic", "post_hasCreator_person_0_0.csv", "hasCreator", "Post", "Person"},
      {"dynamic", "comment_hasCreator_person_0_0.csv", "hasCreator", "Comment", "Person"},
      {"dynamic", "post_hasTag_tag_0_0.csv", "hasTag", "Post", "Tag"},
      {"dynamic", "comment_hasTag_tag_0_0.csv", "hasTag", "Comment", "Tag"},
      {"dynamic", "post_isLocatedIn_place_0_0.csv", "isLocatedIn", "Post", "Place"},
      {"dynamic", "comment_isLocatedIn_place_0_0.csv", "isLocatedIn", "Comment", "Place"},
      {"dynamic", "forum_containerOf_post_0_0.csv", "containerOf", "Forum", "Post"},
      {"dynamic", "forum_hasMember_person_0_0.csv", "hasMember", "Forum", "Person"},
      {"dynamic", "forum_hasModerator_person_0_0.csv", "hasModerator", "Forum", "Person"},
      {"dynamic", "forum_hasTag_tag_0_0.csv", "hasTag", "Forum", "Tag"},
      {"dynamic", "comment_replyOf_post_0_0.csv", "replyOf", "Comment", "Post"},
      {"dynamic", "comment_replyOf_comment_0_0.csv", "replyOf", "Comment", "Comment"},
      {"static", "organisation_isLocatedIn_place_0_0.csv", "isLocatedIn", "Organisation", "Place"},
      {"static", "place_isPartOf_place_0_0.csv", "isPartOf", "Place", "Place"},
      {"static", "tag_hasType_tagclass_0_0.csv", "hasType", "Tag", "TagClass"},
      {"static", "tagclass_isSubclassOf_tagclass_0_0.csv", "isSubclassOf", "TagClass", "TagClass"},
  };
  return e;
}
// person multi-valued [string] props, one value per row, in their own CSVs; the FILE order
// fixes the per-person key insertion order Python's setdefault produces
const std::vector<std::pair<const char*, const char*>>& person_multi_files() {
  static const std::vector<std::pair<const char*, const char*>> m = {
      {"person_email_emailaddress_0_0.csv", "email"},
      {"person_speaks_language_0_0.csv", "language"}};
  return m;
}

// One buffered pipe-delimited CSV: getline + split. LDBC CsvBasic never quotes (verified over
// sf0.1/sf3/sf30; the record-hash gate re-verifies per SF), so a plain '|' split matches Python's
// csv.reader. Trailing \r stripped (csv module is newline-agnostic).
class Csv {
 public:
  explicit Csv(const std::string& path) : f_(path) {
    if (!f_) throw std::runtime_error("ldbc_csv: cannot open " + path);
    f_.rdbuf()->pubsetbuf(buf_, sizeof buf_);
  }
  // -> false at EOF; fields are string_views into the line buffer (valid until the next call)
  bool row(std::vector<std::string_view>& fields) {
    if (!std::getline(f_, line_)) return false;
    if (!line_.empty() && line_.back() == '\r') line_.pop_back();
    fields.clear();
    size_t p = 0;
    while (true) {
      // a field STARTING with a quote would be interpreted by Python's csv.reader and
      // silently diverge here -- absent from every LDBC dataset (verified sf0.1/3/30),
      // so a future dataset that quotes fails LOUD, not wrong
      if (p < line_.size() && line_[p] == '"')
        throw std::runtime_error("ldbc_csv: quoted field ('\"' at field start) -- this plain "
                                 "'|' splitter does not implement CSV quoting: " + line_.substr(0, 80));
      size_t q = line_.find('|', p);
      if (q == std::string::npos) { fields.emplace_back(line_.data() + p, line_.size() - p); break; }
      fields.emplace_back(line_.data() + p, q - p);
      p = q + 1;
    }
    return true;
  }

 private:
  std::ifstream f_;
  std::string line_;
  char buf_[1 << 20];
};

int64_t to_int(std::string_view s, const std::string& where) {
  int64_t x = 0;
  bool neg = !s.empty() && s[0] == '-';
  size_t i = neg ? 1 : 0;
  if (i == s.size()) throw std::runtime_error("ldbc_csv: empty int in " + where);
  for (; i < s.size(); i++) {
    if (s[i] < '0' || s[i] > '9')
      throw std::runtime_error("ldbc_csv: bad int '" + std::string(s) + "' in " + where);
    x = x * 10 + (s[i] - '0');
  }
  return neg ? -x : x;
}

uint64_t make_vid(int tag, int64_t raw_id, const std::string& where) {
  uint64_t i = uint64_t(raw_id);
  if (i >= kVidMaxId) throw std::runtime_error("ldbc_csv: LDBC id over the vid budget in " + where);
  return (uint64_t(tag) << kVidShift) | i;
}

size_t col_of(const std::vector<std::string_view>& header, const std::string& name, const std::string& fname) {
  for (size_t i = 0; i < header.size(); i++)
    if (header[i] == name) return i;
  // loud, like Python's header.index: a schema prop absent from the CSV is a mapping bug
  throw std::runtime_error("ldbc_csv: column '" + name + "' not in " + fname + " header");
}

}  // namespace

void stream(const std::string& root, const Schema& schema, const std::function<void(const Record&)>& emit) {
  // person multi props: {raw person id -> [(prop, values)] in first-seen order} -- the same
  // per-person key order Python's setdefault chain yields (email file is read first)
  std::unordered_map<int64_t, std::vector<std::pair<const char*, std::vector<std::string>>>> multi;
  {
    std::vector<std::string_view> f;
    for (auto& [fname, prop] : person_multi_files()) {
      Csv csv(root + "/dynamic/" + fname);
      csv.row(f);                                        // header: Person.id|<value>
      while (csv.row(f)) {
        auto& lists = multi[to_int(f[0], fname)];
        auto it = std::find_if(lists.begin(), lists.end(), [&](auto& e) { return std::strcmp(e.first, prop) == 0; });
        if (it == lists.end()) { lists.push_back({prop, {}}); it = std::prev(lists.end()); }
        it->second.emplace_back(f[1]);
      }
    }
  }

  Record r;
  std::vector<std::string_view> f;
  for (auto& vf : vertex_files()) {
    const VertexDecl& decl = schema.vertex(vf.label);
    int tag = schema.tag_of_label(vf.label);
    Csv csv(root + "/" + vf.subdir + "/" + vf.fname);
    if (!csv.row(f)) throw std::runtime_error("ldbc_csv: empty " + std::string(vf.fname));
    std::vector<std::string> hold;
    for (auto& h : f) hold.emplace_back(h);
    std::vector<std::string_view> header(hold.begin(), hold.end());   // header views outlive the line buffer
    size_t id_col = col_of(header, "id", vf.fname);
    // (name, is_int, column) per non-multi schema prop -- Python's fields, in schema order
    struct Col { const std::string* name; bool is_int; size_t col; };
    std::vector<Col> cols;
    for (auto& p : decl.props) {
      if (p.multi) continue;                             // email/language ride in their own CSVs
      if (p.type == "bytes") throw std::runtime_error("ldbc csv: " + vf.label + "." + p.name + " is bytes: LDBC's CSVs are text");
      cols.push_back({&p.name, p.type != "str", col_of(header, p.name, vf.fname)});
    }
    bool is_person = vf.label == "Person";
    size_t need = id_col;
    for (auto& c : cols) need = std::max(need, c.col);
    while (csv.row(f)) {
      if (f.size() <= need)
        throw std::runtime_error("ldbc_csv: short row (" + std::to_string(f.size()) + " fields) in " + vf.fname);
      int64_t raw_id = to_int(f[id_col], vf.fname);
      RowFields props;
      props.emplace_back("id", Value(raw_id));           // the implicit id prop, first (no format stores it: vertex_cols)
      for (auto& c : cols) {
        std::string_view v = f[c.col];
        if (v.empty()) continue;                         // empty CSV field -> absent prop
        props.emplace_back(*c.name, c.is_int ? Value(to_int(v, vf.fname)) : Value(std::string(v)));
      }
      if (is_person) {
        auto it = multi.find(raw_id);
        if (it != multi.end())
          for (auto& [prop, vals] : it->second) {
            ValueList xs;
            for (auto& s : vals) xs.emplace_back(s);
            props.emplace_back(prop, Value::list(std::move(xs)));
          }
      }
      r.is_vertex = true;
      r.uid = make_vid(tag, raw_id, vf.fname);
      r.label = &vf.label;
      r.props = Value::row(std::move(props));
      emit(r);
    }
  }

  for (auto& ef : edge_files()) {
    int stag = schema.tag_of_label(ef.src_label), dtag = schema.tag_of_label(ef.dst_label);
    const std::vector<PropDecl>& eprops = schema.edge_props(ef.etype);
    Csv csv(root + "/" + ef.subdir + "/" + ef.fname);
    if (!csv.row(f)) throw std::runtime_error("ldbc_csv: empty " + std::string(ef.fname));
    std::vector<std::string> hold;
    for (auto& h : f) hold.emplace_back(h);
    std::vector<std::string_view> header(hold.begin(), hold.end());
    struct Col { const std::string* name; bool is_int; size_t col; };
    std::vector<Col> cols;                               // named prop columns (ids repeat names,
    for (auto& p : eprops) {                             // so cols 0/1 are positional like Python)
      if (p.type == "bytes") throw std::runtime_error("ldbc csv: " + ef.etype + "." + p.name + " is bytes: LDBC's CSVs are text");
      cols.push_back({&p.name, p.type != "str", col_of(header, p.name, ef.fname)});
    }
    size_t need = 1;
    for (auto& c : cols) need = std::max(need, c.col);
    while (csv.row(f)) {
      if (f.size() <= need)
        throw std::runtime_error("ldbc_csv: short row (" + std::to_string(f.size()) + " fields) in " + ef.fname);
      RowFields props;
      for (auto& c : cols) {
        std::string_view v = f[c.col];
        if (v.empty()) continue;
        props.emplace_back(*c.name, c.is_int ? Value(to_int(v, ef.fname)) : Value(std::string(v)));
      }
      r.is_vertex = false;
      r.src = make_vid(stag, to_int(f[0], ef.fname), ef.fname);
      r.dst = make_vid(dtag, to_int(f[1], ef.fname), ef.fname);
      r.etype = &ef.etype;
      r.src_label = &ef.src_label;
      r.dst_label = &ef.dst_label;
      r.props = Value::row(std::move(props));
      emit(r);
    }
  }
}

// --- the byte-exact JSON line (records_dump.py's json.dumps(..., separators, ensure_ascii=False))
namespace {
void put_json_str(std::string& o, std::string_view s) {
  o.push_back('"');
  for (unsigned char c : s) {
    switch (c) {
      case '"': o += "\\\""; break;
      case '\\': o += "\\\\"; break;
      case '\b': o += "\\b"; break;
      case '\f': o += "\\f"; break;
      case '\n': o += "\\n"; break;
      case '\r': o += "\\r"; break;
      case '\t': o += "\\t"; break;
      default:
        if (c < 0x20) {
          char b[8];
          std::snprintf(b, sizeof b, "\\u%04x", c);
          o += b;
        } else o.push_back(char(c));                     // UTF-8 passes through (ensure_ascii=False)
    }
  }
  o.push_back('"');
}
void put_json_val(std::string& o, const Value& v) {
  if (v.is_int()) { o += std::to_string(v.as_int()); return; }
  if (v.is_str()) { put_json_str(o, v.as_str()); return; }
  if (v.is_list()) {
    o.push_back('[');
    bool first = true;
    for (auto& e : v.as_list()) { if (!first) o.push_back(','); first = false; put_json_val(o, e); }
    o.push_back(']');
    return;
  }
  throw std::runtime_error("ldbc_csv: unexpected value type in a record");
}
void put_json_props(std::string& o, const Value& props) {
  o.push_back('{');
  bool first = true;
  for (auto& [k, v] : props.as_row()) {
    if (!first) o.push_back(',');
    first = false;
    put_json_str(o, k);
    o.push_back(':');
    put_json_val(o, v);
  }
  o.push_back('}');
}
}  // namespace

void jsonl_line(const Record& r, std::string& out) {
  out.clear();
  if (r.is_vertex) {
    out += "[\"v\",\"";
    out += hex16(r.uid);
    out += "\",";
    put_json_str(out, *r.label);
    out.push_back(',');
    put_json_props(out, r.props);
    out.push_back(']');
  } else {
    out += "[\"e\",";
    put_json_str(out, *r.etype);
    out += ",\"";
    out += hex16(r.src);
    out += "\",\"";
    out += hex16(r.dst);
    out += "\",";
    put_json_props(out, r.props);
    out.push_back(',');
    put_json_str(out, *r.src_label);
    out.push_back(',');
    put_json_str(out, *r.dst_label);
    out.push_back(']');
  }
}

}  // namespace evalcpp::ldbc_csv
