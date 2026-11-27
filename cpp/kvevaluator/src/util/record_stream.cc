// See record_stream.h.
#include "util/record_stream.h"

#include <filesystem>
#include <fstream>
#include <optional>
#include <string>
#include <vector>
#include <stdexcept>

#include "util/sha256.h"

namespace evalcpp {

std::string file_sha256(const std::string& path) {
  std::ifstream f(path, std::ios::binary);
  if (!f) throw std::runtime_error("cannot read " + path + " to hash it");
  Sha256 h;
  char buf[1 << 16];
  while (f.read(buf, sizeof buf) || f.gcount() > 0) h.update(buf, static_cast<size_t>(f.gcount()));
  return h.hexdigest();
}

json record_stream_at_open(const std::string& path) {
  // the stream by its place, <dataset>/<scale>/<file> (its last three parts), never by where one machine kept it: its
  // identity is the key and content shas below
  std::vector<std::string> parts;
  for (const auto& p : std::filesystem::path(path)) parts.push_back(p.string());
  std::filesystem::path place;
  for (size_t i = parts.size() > 3 ? parts.size() - 3 : 0; i < parts.size(); i++) place /= parts[i];
  json r = {{"path", place.string()}};
  std::string key = path + ".key.json";
  if (std::filesystem::exists(key)) {
    r["key_sha256"] = file_sha256(key);
    // the stream by what its records depend on (the reader's sources, the schema, the raw files): the key without the
    // commit that dumped it, so a re-dump by another commit of the same sources is the same stream (a dirty dump stays
    // apart from a clean one: binary_dirty is kept)
    std::ifstream g(key);
    json k = json::parse(g, nullptr, false);
    if (k.is_object()) {
      // the stream's bytes, written by the dump (records_key.py's content_sha256): two dumps of the same bytes match
      if (k.contains("content_sha256") && k["content_sha256"].is_string()) r["content_sha256"] = k["content_sha256"];
      if (k.contains("cpp") && k["cpp"].is_object()) k["cpp"].erase("binary_git_sha");
      Sha256 s;
      std::string text = k.dump();
      s.update(text.data(), text.size());
      r["sources_sha256"] = s.hexdigest();
    }
  }
  return r;
}

// when both name `field`: "" if equal, else `what` with both values; nullopt when one does not (the next rule decides)
static std::optional<std::string> compare_by(const json& a, const json& b, const char* field, const std::string& what,
                                             const std::string& why) {
  if (!a.contains(field) || !b.contains(field)) return std::nullopt;
  if (a[field] == b[field]) return std::string();
  return what + " " + a[field].get<std::string>().substr(0, 12) + " and " + b[field].get<std::string>().substr(0, 12) +
         " (" + why + ")";
}

std::string record_stream_mismatch(const json& a, const json& b) {
  if (!a.is_object() || !b.is_object()) return "";
  // the line count first: the path may differ (a copied or committed file)
  if (a.contains("lines") && b.contains("lines") && a["lines"] != b["lines"])
    return "record streams of " + a["lines"].dump() + " and " + b["lines"].dump() + " lines";
  // by the bytes when both name them (a re-dump of the same bytes is the same stream); else by sources when both name
  // them; else by the whole key (a file made before sources_sha256 was recorded)
  // a tally encodes with its schema (indexes, property kinds): two schemas are two tallies of one stream
  if (auto r = compare_by(a, b, "schema_sha256", "tallies under other schemas", "the schema changed: build it again"))
    if (!r->empty()) return *r;
  if (auto r = compare_by(a, b, "content_sha256", "record streams of other content",
                          "the stream was dumped again with other bytes"))
    return *r;
  if (auto r = compare_by(a, b, "sources_sha256", "record streams from other sources",
                          "the stream was dumped again by another reader"))
    return *r;
  bool ka = a.contains("key_sha256"), kb = b.contains("key_sha256");
  if (ka && kb && a["key_sha256"] != b["key_sha256"])
    return "record streams with freshness keys " + a["key_sha256"].get<std::string>().substr(0, 12) + " and " +
           b["key_sha256"].get<std::string>().substr(0, 12) + " (the stream was dumped again)";
  if (ka != kb)
    return std::string("one names its record stream's freshness key and the other does not (") +
           (ka ? "the second" : "the first") + " was made before the key was recorded: make it again)";
  return "";
}

}  // namespace evalcpp
