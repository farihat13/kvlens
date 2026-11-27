// name -> format: the one place that knows every format directory (formats/format.h's
// make_format). A format never includes a sibling; this file includes them all.
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>

#include "formats/format.h"
#include "store/counting.h"
#include "formats/arangodb/engine.h"
#include "formats/arangodb/format.h"
#include "formats/dgraph/engine.h"
#include "formats/dgraph/format.h"
#include "formats/meta/format.h"
#include "formats/nebula/engine.h"
#include "formats/nebula/format.h"
#include "formats/pertype_csr/format.h"

namespace evalcpp {

// the format itself; make_format below adds what every format needs from the factory
static std::unique_ptr<Format> build_format(const std::string& name, const Schema& schema, const json& codec_config) {
  if (name == "pertype_csr")
    return std::make_unique<pertype_csr::PerTypeCsr>(schema, pertype_csr::Config::from_json(codec_config));
  if (name == "nebula") return std::make_unique<nebula::Nebula>(schema, nebula::Config::from_json(codec_config));
  if (name == "dgraph") return std::make_unique<dgraph::Dgraph>(schema, dgraph::Config::from_json(codec_config));
  if (name == "arangodb") return std::make_unique<arangodb::Arango>(schema, arangodb::Config::from_json(codec_config));
  // the config-driven format: one format, any co-location point, from codec_config (formats/meta/)
  if (name == "meta") return std::make_unique<meta::Meta>(schema, meta::Config::from_json(codec_config));
  // the ENGINE formats (engine-fidelity only, never compared): built by exact name, listed in engine_key_layouts()
  if (name == "dgraph_engine")
    return std::make_unique<dgraph::DgraphEngine>(schema, dgraph::Config::from_json(codec_config));
  if (name == "nebula_engine")
    return std::make_unique<nebula::NebulaEngine>(schema, nebula::Config::from_json(codec_config));
  if (name == "arangodb_engine")
    return std::make_unique<arangodb::ArangoEngine>(schema, arangodb::Config::from_json(codec_config));
  throw std::runtime_error("unknown format '" + name + "' (known: pertype_csr, nebula, dgraph, "
                           "arangodb, meta; engine: dgraph_engine, nebula_engine, arangodb_engine)");
}

std::unique_ptr<Format> make_format(const std::string& name, const Schema& schema, const json& codec_config) {
  std::unique_ptr<Format> f = build_format(name, schema, codec_config);
  f->set_store_schema(schema);   // the store caps (property_index) derive from it
  return f;
}

json key_layouts() {
  // the same names make_format accepts
  return {{"pertype_csr", pertype_csr::PerTypeCsr::kKeyLayout},
          {"nebula", nebula::Nebula::kKeyLayout},
          {"dgraph", dgraph::Dgraph::kKeyLayout},
          {"arangodb", arangodb::Arango::kKeyLayout},
          {"meta", meta::Meta::kKeyLayout}};
}

// The COUNTING versions of each format's own operator rules, {read, write}: what its reads and its writes charge,
// beside the seam's (store/counting.h). Bump a format's pair here, by hand, when one of its rules changes a count with
// no store or plan change; `just golden-counts` fails a moved count with no bump. An engine twin is counted under its
// parent's pair (a twin-only rule change bumps the parent's: coarser, never stale). A capture records the versions it
// ran under, and a driver re-measures when the binary's differ (python/kvevaluator/measure/counting.py).
static const std::map<std::string, std::pair<int, int>> kFormatCounting = {
    {"pertype_csr", {2, 1}}, {"nebula", {3, 1}}, {"dgraph", {2, 1}}, {"arangodb", {3, 1}},
    {"meta", {3, 1}}};   // nebula, arangodb read 3: a chase hop stops on the first edge, with no NEXT past it
                         // (their engine twins share the version and still scan the whole list);
                         // meta read 2: an index read's output keeps its label under every label_tags setting;
                         // pertype_csr, nebula, dgraph, arangodb read 2: a chase reads a node once per call; edge_exists
                         // counts the via walk's entries used and reads a candidate's independent probes together.
                         // meta read 3, two changes under one version: its Split probes'
                         // round trips (read together), and a chunk's size estimate counts a string's bytes
                         // (codec/chunks.cc), so a Chunked props read's counts move with the store (its key layout
                         // moved too); the golden counts re-record only on a counting-version move
static const std::map<std::string, std::string> kEngineParent = {
    {"dgraph_engine", "dgraph"}, {"nebula_engine", "nebula"}, {"arangodb_engine", "arangodb"}};

static std::pair<int, int> format_counting(const std::string& name) {
  auto parent = kEngineParent.find(name);
  auto it = kFormatCounting.find(parent == kEngineParent.end() ? name : parent->second);
  if (it == kFormatCounting.end()) throw std::logic_error("no counting version for format " + name + " (registry.cc)");
  return it->second;
}

json counting_versions() {
  auto pair = [](std::pair<int, int> p) { return json{{"read", p.first}, {"write", p.second}}; };
  json formats = json::object();
  for (auto& [name, v] : kFormatCounting) formats[name] = pair(v);
  for (auto& kv : kEngineParent) formats[kv.first] = pair(format_counting(kv.first));
  return {{"seam", pair({kCountingSeamVersion, kWriteCountingSeamVersion})}, {"formats", formats}};
}

json counting_of(const Format& f) {
  auto v = format_counting(f.name());
  return {{"seam", kCountingSeamVersion}, {"format", v.first},
          {"write_seam", kWriteCountingSeamVersion}, {"write_format", v.second}};
}

json engine_key_layouts() {
  // the engine-fidelity formats: apart from key_layouts() so no "every format" list ever picks one up
  return {{"dgraph_engine", dgraph::DgraphEngine::kKeyLayout},
          {"nebula_engine", nebula::NebulaEngine::kKeyLayout},     // nebula's store, unchanged
          {"arangodb_engine", arangodb::ArangoEngine::kKeyLayout}};  // arangodb's store, named documents
}

}  // namespace evalcpp
