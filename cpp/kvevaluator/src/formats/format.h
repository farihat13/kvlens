// A format = its KV ops + its ingest, over the shared store / index / codec infrastructure.
// One directory per format under formats/; nothing under formats/<x>/ is imported by a
// sibling (what two formats share lives in index/, codec/, store/).
#pragma once
#include <memory>
#include <cstdint>
#include <optional>
#include <tuple>
#include <utility>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

#include "contract/contract.h"
#include "exec/step_exec.h"
#include "ir/plan.h"
#include "schema/schema.h"
#include "store/counting.h"
#include "value/value.h"

namespace evalcpp {

// Where ingest puts its (key, value) pairs. Keys are written in RUNS: inside a run they must
// be strictly increasing and every run must be a key range disjoint from every other run (one
// prefix per run); the sink streams each run to its own SST file. Index entries arrive out of
// order (one vertex at a time) and go through `put_index`, which the sink sorts at the end.
struct KvSink {
  virtual ~KvSink() = default;
  virtual void begin_run(const std::string& name) = 0;
  virtual void put(std::string_view key, std::string_view value) = 0;
  virtual void end_run() = 0;
  virtual void put_index(int index_id, std::string key) = 0;   // empty value
};

// What one stored key is, as its format decodes it (Format::classify). The first five fields are census's group:
// part (vertex_props | adjacency | edge_props | index | other | unclassified), type (a vertex label, an edge type, an
// index's label; "" where the key does not name it), direction (out | in where the key says it), owner (the label or
// label group an adjacency key names), detail (an index's property; "" = the label's type index). role and kind are
// the size breakdown's pair: role one of vertex | edge | index-vertex | index-edge | other, kind the format's own name.
struct KeyClass {
  std::string part, type, direction, owner, detail;
  std::string role, kind;
};

// The class of a key whose format decodes no type from it (dgraph, arangodb): the part follows the role.
inline KeyClass role_class(std::string role, std::string kind) {
  std::string part = role == "vertex" ? "vertex_props"
                     : role == "edge" ? "adjacency"
                     : role.rfind("index", 0) == 0 ? "index" : "unclassified";
  return {part, "", "", "", "", std::move(role), std::move(kind)};
}

// The decoders that read a stored VALUE, for the inspection commands. Each default answers "no such decoder".
struct ValueInspector {
  virtual ~ValueInspector() = default;
  // The PAYLOAD bytes one stored KV holds: its property cells as the columns codec defines a cell's payload (int /
  // date 8 B, a str its bytes, a multi its blob), whatever the layout frames them with -- the census's
  // payload / framing split. nullopt = no such decoder.
  virtual std::optional<uint64_t> payload_bytes(std::string_view, std::string_view) const { return std::nullopt; }
  // Does one stored KV hold NOTHING (an empty list, part or chunk: a key that should have been deleted)? nullopt = no
  // such decoder; census counts these as empty_values.
  virtual std::optional<bool> empty_value(std::string_view, std::string_view) const { return std::nullopt; }
  // A chunk's (family, first id, last id): a Chunked adjacency chunk's ("etype:dir", first owner, last owner), a
  // Chunked props chunk's ("props:<label>", first vid, last vid); nullopt for any other KV. census --chunk-keys lists
  // them for the landing oracle.
  virtual std::optional<std::tuple<std::string, uint64_t, uint64_t>> chunk_range(std::string_view, std::string_view) const {
    return std::nullopt;
  }
  // The far-end LABELS one stored adjacency KV holds: per entry (family "etype:dir", owner, the neighbour, the label it
  // stores). Empty for a KV that stores none. census --far-labels lists them for the symmetry content check.
  using FarLabel = std::tuple<std::string, uint64_t, uint64_t, std::string>;
  virtual std::vector<FarLabel> far_label_entries(std::string_view, std::string_view) const { return {}; }
};

class Format {
 public:
  virtual ~Format() = default;
  virtual std::string name() const = 0;
  // The capability vocabulary this format provides for `schema` (the
  // format owns its capabilities; they may depend on the schema).
  virtual std::set<std::string> capabilities() const = 0;
  // The KEY-LAYOUT version this format writes/reads. Stamped into the store manifest at
  // ingest and CHECKED at session open, so a store built under an older key layout refuses
  // loudly instead of silently missing keys (e.g. the arango edge-doc coll offset:
  // a store of the old layout under a new binary would return NULL edge props with
  // identical op counts -- wrong rows, right costs). Bump per FORMAT when its keys change.
  // Declared as a per-class constant (kKeyLayout) the virtual returns, so `key_layouts()`
  // can list every format's layout WITHOUT building a Format (no schema needed): the
  // `--version` provenance carries it and a driver's freshness check compares a store's
  // manifest key_codec to it -- a stale store is caught before the session guard refuses.
  static constexpr const char* kKeyLayout = "realistic";
  virtual std::string key_codec() const { return kKeyLayout; }
  // The caps that hold for ops dispatched on `label` -- a plan variant annotated
  // `"on": {cap: [label,...]}` is checked against THIS, so a format whose layout differs per label (meta) grants a
  // cap where that label's layout has it (mirrors kvevaluator.formats.caps.caps_resolver). For a format
  // with one layout the label changes nothing.
  virtual std::set<std::string> capabilities_for(const std::string&) const { return capabilities(); }
  // The bytes one landing on a vertex row decodes when the layout PACKS many rows into a chunk, else nullopt (rows
  // are not packed). pertype_csr: its chunk target; meta: the smallest vertex-props pack over the labels. dgraph's
  // chunk target is a SPLIT threshold (only a unit larger than it splits), not a pack: nullopt.
  // Mirrors Python Format.pack_target.
  virtual std::optional<uint64_t> pack_target() const { return std::nullopt; }
  // The caps a plan is SELECTED with on this store: the format's own (above) plus the STORE caps -- coarse_chunks iff
  // pack_target() >= contract::chunk_coarse(); property_index iff the store's schema declares a property index (an index body is
  // valid only there). Non-virtual: one derivation for every format; mirrors planir/capabilities.py
  // store_caps / caps_for(chunk=...). A format never declares a store cap itself.
  std::set<std::string> store_capabilities() const { return with_store_caps(capabilities()); }
  std::set<std::string> store_capabilities_for(const std::string& label) const {
    if (has_edge_type(label)) return edge_capabilities_for(label);   // an `on` that names an edge type
    return with_store_caps(capabilities_for(label));
  }
  // The caps of ONE EDGE TYPE's adjacency: what a variant annotated `"on": {cap: [etype]}` is checked against
  // (mirrors kvevaluator.formats.caps.caps_resolver on an edge-type name; Python Format.edge_capabilities_for):
  //   reverse_adjacency  the store keeps this edge type's in lists (a read_edge_exists read with dir in is valid)
  //   adjacency_lists    this edge type's adjacency is a list per owner in EVERY stored direction: there is no
  //                      per-edge key to point-GET. Conservative for a store whose directions differ (meta's
  //                      adjacency_by_etype_dir): one per-edge direction and it is not advertised. A cap per
  //                      direction is the later refinement, if a mix ever needs what this one gates.
  virtual bool has_edge_type(const std::string&) const { return false; }
  virtual std::set<std::string> edge_capabilities_for(const std::string&) const { return {}; }
  virtual bool has_kv_op(const std::string& op) const = 0;
  // Runs one KV op as tasks on `ex` (fan-out per the op's access path); returns the output.
  virtual Value run_kv_op(const Step& step, const Args& a, StepExec& ex) = 0;

  // streaming ingest (the per-record physical insert), then finalize -> all pairs
  virtual void add_vertex(KvSink& sink, uint64_t uid, const std::string& label, const Value& props) = 0;
  virtual void add_edge(KvSink& sink, const std::string& etype, uint64_t src, uint64_t dst, const Value& props,
                        const std::string& src_label, const std::string& dst_label) = 0;
  virtual void finalize(KvSink& sink) = 0;
  virtual json ingest_counts() const = 0;
  // the value-codec configuration (its hash is part of the store identity)
  virtual json codec_config() const = 0;

 private:
  std::set<std::string> with_store_caps(std::set<std::string> caps) const {
    if (caps.count("coarse_chunks") || caps.count("property_index"))
      throw std::logic_error(name() + " declares a store property (coarse_chunks / property_index): derived here");
    if (!store_schema_)
      throw std::logic_error(name() + ": store caps need the store's schema (build the format with make_format)");
    const std::optional<uint64_t> pack = pack_target();
    if (pack && *pack >= contract::chunk_coarse()) caps.insert("coarse_chunks");   // inputs/contract/capabilities.json
    if (store_schema_->has_property_index()) caps.insert("property_index");
    return caps;
  }
  const Schema* store_schema_ = nullptr;   // set by make_format: the schema the store caps derive from

 public:
  void set_store_schema(const Schema& schema) { store_schema_ = &schema; }

 public:
  // one-time derived state after a store opens, OUTSIDE any measured window (Python fmt.warm):
  // arango rebuilds its vid->collection map here so no query's tickers see the pk sweep
  virtual void warm(Store&) {}
  // Per-part attribution (store/counting.h PartScheme; the executor's run.parts): the format names
  // the store PART each of its keys belongs to, so a step's counts can be split by part. Default: no
  // scheme -- nothing is recorded and nothing is emitted (every format but meta).
  virtual const PartScheme* part_scheme() const { return nullptr; }
  // What one stored key is: the format's ONE key decoder (KeyClass above). A pure function of the key bytes. Default:
  // role and kind "other" (part unclassified) -- a format without a classifier is visible, never silently folded into a real role
  // (the sizes gate refuses a non-empty "other").
  virtual KeyClass classify(std::string_view) const { return role_class("other", "other"); }
  // The ROLE of one stored key, for the size breakdown (`kvevaluator size`'s per-role table): {role, kind} of classify.
  std::pair<std::string, std::string> key_role(std::string_view key) const {
    KeyClass c = classify(key);
    return {c.role, c.kind};
  }
  // The LABEL one stored key belongs to, for the size study's per-type table: {bucket, type} with bucket vertex | edge |
  // index, from classify's part and type; {"", ""} where the key names no type.
  std::pair<std::string, std::string> key_label(std::string_view key) const {
    KeyClass c = classify(key);
    if (c.type.empty()) return {"", ""};
    if (c.part == "vertex_props") return {"vertex", c.type};
    if (c.part == "adjacency" || c.part == "edge_props") return {"edge", c.type};
    if (c.part == "index") return {"index", c.type};
    return {"", ""};
  }
  // The value decoders the inspection commands use (census, tally, predict's chunk ranges); nullptr = the format has
  // none (every format but meta and pertype_csr).
  virtual const ValueInspector* values() const { return nullptr; }
};

// Bumped whenever a key_role classifier changes: a cached per-role size table (capture/store_identity.py store_roles' sizes.json)
// is keyed by it, so a classifier fix never serves a stale table. Reported by --version and by `size --roles`.
constexpr int kKeyRolesVersion = 6;   // 2: + the per-label table (key_label); 3: dgraph type-scoped index keys; 4: arangodb edge pk; 5: nebula reads the tag / etype where the store's key order puts it; 6: one classifier per format (meta gains the labels table; pertype_csr's edge-props chunks are edge_props in census)

// name -> format over `schema` with `codec_config` (the registry). Throws on an unknown name.
std::unique_ptr<Format> make_format(const std::string& name, const Schema& schema, const json& codec_config);
// {format name -> the key layout this binary reads/writes} for every COMPARED format make_format knows.
json key_layouts();
// The same for the ENGINE formats (engine fidelity only; make_format builds them by exact name).
json engine_key_layouts();
// {"seam": {"read", "write"}, "formats": {name: {"read", "write"}}} for every format make_format knows, engines too.
json counting_versions();
// The four versions one run is counted under on `f`: {"seam", "format", "write_seam", "write_format"}.
json counting_of(const Format& f);

}  // namespace evalcpp
