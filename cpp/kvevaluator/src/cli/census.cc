// `census <store-dir>`: the keys and bytes of one store per (part, type, direction): what each vertex label's props,
// each edge type's adjacency per direction, and each index occupy -- the per-type sizes a per-type layout change
// (a migration) is priced from. One READ-ONLY walk; every key is classified by the FORMAT'S OWN decoder, never a
// second copy of a key layout:
//   every format          its classify (formats/format.h KeyClass): meta's from its part scheme (MetaParts::part_of,
//                         the run.parts naming of keys.h "Parts"); nebula and pertype_csr name the type, not the
//                         direction; dgraph and arangodb only the part (type "": the keys are not decoded per type)
// The totals must equal the manifest's keys and logical_bytes, else exit 1 (after printing the JSON).
#include <rocksdb/db.h>

#include <chrono>
#include <fstream>
#include <iostream>
#include <map>
#include <optional>
#include <tuple>

#include "cli/census_groups.h"
#include "cli/commands.h"
#include "formats/format.h"
#include "ingest/ingest.h"
#include "provenance.h"
#include "util/vid.h"
#include "schema/schema_path.h"
#include "store/rocksdb_store.h"

namespace evalcpp {

using namespace census_groups;

int cmd_census(const std::vector<std::string>& args) {
  bool chunk_keys = false;   // + each Chunked adjacency chunk's owner range
  std::string far_out;       // + every stored far-end label, one TSV line each (etype:dir, owner, neighbour, label)
  std::vector<std::string> rest;
  for (size_t i = 0; i < args.size(); i++) {
    if (args[i] == "--chunk-keys") chunk_keys = true;
    else if (args[i] == "--far-labels" && i + 1 < args.size()) far_out = args[++i];
    else rest.push_back(args[i]);
  }
  if (rest.size() != 1) {
    std::cerr << "usage: kvevaluator census [--chunk-keys] [--far-labels <out.tsv>] <store-dir>   (read-only; JSON on "
                 "stdout; exit 1 if the totals differ from the manifest)\n";
    return 2;
  }
  const std::string dir = rest[0];
  std::ofstream far_tsv;
  uint64_t far_n = 0;
  if (!far_out.empty()) {
    far_tsv.open(far_out);
    if (!far_tsv) throw std::runtime_error("census: cannot write " + far_out);
  }
  std::map<std::string, json> chunk_ranges;
  auto t0 = std::chrono::steady_clock::now();
  json m = read_manifest(dir);
  Schema schema = load_manifest_schema(m, dir, "census");
  const std::string format = m.value("format", "");
  auto fmt = make_format(format, schema, m.value("value_codec", json::object()));
  // census decodes values (--far-labels, --chunk-keys): a store of another key layout or codec config would be read
  // under widths or a layout it was not written with -- the session's check, one function (ingest/ingest.h)
  check_store_codec(m, *fmt, dir, "census");
  StoreOptions so = store_options_of(m, dir);
  auto store = Store::open_read_only(dir + "/db", so);

  const ValueInspector* vals = fmt->values();   // null: the format decodes no values (no payload, chunk or label columns)
  const bool parts = fmt->part_scheme() != nullptr;   // meta: the groups are its store parts
  std::map<GroupKey, Tally> groups;
  uint64_t labelled = 0;   // keys whose class names a type (non-meta: the note below says how much the groups say)
  bool has_payload = false;   // the format decodes payload (meta): the payload / framing columns are emitted
  Tally total;
  std::unique_ptr<rocksdb::Iterator> it(store->db()->NewIterator(rocksdb::ReadOptions()));
  for (it->SeekToFirst(); it->Valid(); it->Next()) {
    std::string_view k(it->key().data(), it->key().size());
    std::string_view v(it->value().data(), it->value().size());
    KeyClass c = fmt->classify(k);
    if (!c.type.empty()) labelled++;
    Tally& t = groups[group_of(c)];
    std::optional<uint64_t> pb = vals ? vals->payload_bytes(k, v) : std::nullopt;
    has_payload = has_payload || pb.has_value();
    if (chunk_keys && vals)
      if (auto cr = vals->chunk_range(k, v)) {
        auto& [fam, first, last] = *cr;
        chunk_ranges[fam].push_back(json::array({hex16(first), hex16(last)}));
      }
    if (far_tsv.is_open() && vals)
      for (auto& [fam, owner, other, label] : vals->far_label_entries(k, v)) {
        far_tsv << fam << '\t' << hex16(owner) << '\t' << hex16(other) << '\t' << label << '\n';
        far_n++;
      }
    std::optional<bool> ev = vals ? vals->empty_value(k, v) : std::nullopt;
    for (Tally* x : {&t, &total}) {
      x->keys++;
      x->key_bytes += it->key().size();
      x->value_bytes += it->value().size();
      x->payload_bytes += pb.value_or(0);
      x->empty_values += ev.value_or(false) ? 1 : 0;
    }
  }
  if (!it->status().ok()) throw std::runtime_error("census: iterator: " + it->status().ToString());

  json out_groups = json::array();
  for (auto& [g, t] : groups) {
    json row = {{"part", std::get<0>(g)}, {"type", std::get<1>(g)}, {"direction", std::get<2>(g)},
                {"owner", std::get<3>(g)}, {"detail", std::get<4>(g)},
                {"keys", t.keys}, {"key_bytes", t.key_bytes}, {"value_bytes", t.value_bytes}};
    if (has_payload) {   // payload = the property cells; framing = everything else (keys, ids, headers, bitmaps)
      if (t.payload_bytes > t.key_bytes + t.value_bytes)
        throw std::runtime_error("census: payload exceeds the stored bytes in a group (a decoder bug)");
      row["payload_bytes"] = t.payload_bytes;
      row["framing_bytes"] = t.key_bytes + t.value_bytes - t.payload_bytes;
      row["empty_values"] = t.empty_values;
    }
    out_groups.push_back(row);
  }
  uint64_t mkeys = m.value("keys", uint64_t(0)), mbytes = m.value("logical_bytes", uint64_t(0));
  bool keys_ok = total.keys == mkeys, bytes_ok = total.key_bytes + total.value_bytes == mbytes;
  std::string classifier = parts ? "part_scheme" : labelled ? "key_label" : "key_role";
  std::string note =
      parts ? "meta part scheme (keys.h \"Parts\"). An adjacency key (every layout) and an own edge-prop chunk name "
              "the owner's vid GROUP, not its label: Post and Comment are both owner=Message, so they cannot be split "
              "by label; type (the edge type) and direction are exact. A Chunked chunk holds owners of ONE group. "
              "Props and indexes are exact per label."
      : labelled ? "the format's key_label names the type (vertex label / edge type / index label) but not the "
                   "direction: direction is empty"
                 : "this format has no per-type key decoder: groups by key role only (type empty)";
  json out = {{"dir", dir},
              {"format", format},
              {"scale", m.value("scale", "")},
              {"classifier", classifier},
              {"note", note},
              {"groups", out_groups},
              {"chunk_ranges", chunk_keys ? json(chunk_ranges) : json(nullptr)},
              {"far_labels", far_out.empty() ? json(nullptr) : json({{"file", far_out}, {"entries", far_n}})},
              {"totals", {{"keys", total.keys}, {"key_bytes", total.key_bytes}, {"value_bytes", total.value_bytes},
                          {"logical_bytes", total.key_bytes + total.value_bytes}}},
              {"manifest", {{"keys", mkeys}, {"logical_bytes", mbytes}, {"sst_bytes", m.value("sst_bytes", uint64_t(0))}}},
              {"check", {{"keys_match", keys_ok}, {"logical_bytes_match", bytes_ok}}},
              {"wall_s", std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count()},
              {"provenance", provenance()}};
  std::cout << out.dump(1) << "\n";
  if (!keys_ok || !bytes_ok) {
    std::cerr << "census: totals differ from the manifest (" << total.keys << " vs " << mkeys << " keys, "
              << total.key_bytes + total.value_bytes << " vs " << mbytes << " bytes)\n";
    return 1;
  }
  return 0;
}

}  // namespace evalcpp
