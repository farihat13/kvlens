// meta's format spec: a KV format is a POINT in the co-location design space
// (the README's "Concepts"), written as a config. meta reads the spec and lays out / reads ONE
// shared RocksDB accordingly, with one property encoding (codec/columns) and one list encoding (codec/lists) in
// every layout, and the shared secondary index. There are no presets (the engine layouts are
// the separate formats, and the full specs the presets expanded to are inputs/specs/corners/*.json). A spec no
// engine implements is an interior point we can still measure.
//
// Per COMPONENT (vertex_props, adjacency, index), the layout is three orthogonal fields:
//   group   what ONE record's value holds --
//             "owner": all of one owner's items in one record   (a row of columns / a list)
//             "item" : one item per record, the item in the KEY (dgraph per-attr / per-edge)
//   owners  how many owners share a physical record --
//             "one"          : each owner its own record(s)
//             {"pack": bytes}: greedily pack consecutive owners into ~bytes chunks (pertype_csr)
//   split   overflow of ONE owner's value -- "never", or into ordered parts at `bytes` (the All-Condensed adjacency and the posting indexes: 64Ki)
// Plus top-level: direction (which physical directions ingest writes). meta-v4 has ONE property encoding in
// every layout, the columns block (codec/columns.h), and one list encoding (codec/lists.h); the v3 field `value`
// (row_timestamp, the Condensed list codec) is refused. Edge-prop PLACEMENT is a field of the adjacency
// component (so a per-etype override carries its own): "adjacency" (the default) = the props inline, a copy in each
// stored direction's record / list / chunk; "own" = their own chunks, keyed by ordinal, beside a packed adjacency
// (pertype_csr's design) -- built on Chunked adjacency only (validate refuses it on Split / Condensed). Its adjacency
// chunk is the inline chunk with ordinals where the columns are, so it carries the far-end label block the same way.
// validate() rejects every combination meta does not build, so a spec never silently measures another cell.
//
// PER-TYPE layouts (optional): "vertex_props_by_label" {label: component} and "adjacency_by_etype"
// {etype: component} override the default component for one vertex label / one edge type. PER DIRECTION:
// "adjacency_by_etype_dir" {etype: {"out": component, "in": component}} overrides one direction's lists of an
// edge type; a direction it does not name keeps the etype's component (adjacency_by_etype, else the default).
// Every layout decision asks props_of(label) / adj_of(etype, out); a spec without the maps is exactly the
// uniform store it always was. Edge-prop PLACEMENT stays per edge type (own placement's chunks are keyed by the
// OUT list's ordinals: one family per etype), so an etype's two directions must agree on it. The label / etype
// names are checked against the schema by the Meta ctor (Config has no schema), as are the per-group rule (the
// labels of one vid group share a props layout), the placement rule, and an "in" given for an etype whose in
// lists the store does not keep.
#pragma once
#include <cstdint>
#include <map>
#include <optional>
#include <string>

#include "value/value.h"  // json

namespace evalcpp::meta {

enum class Group { owner, item };
enum class Placement { adjacency, own };   // where an adjacency component keeps its edge props
// the form of own placement's adjacency chunk ("own_chunk", part of the store's recorded config): list_ordinals, the
// inline chunk (codec/lists.h) with one ordinal per edge where the columns block is -- one adjacency chunk codec for
// every layout of this format. One value: no second form is kept. A store whose spec carries no own_chunk
// field (it holds another form) and is refused at open.
inline constexpr const char* kOwnChunk = "list_ordinals";
// the header form of the lists chunk (codec/lists.h: "skip16", the header), written as
// "chunk_header" on every packed adjacency component (the chunk both placements store). One value: no
// second codec is kept. A store from before the field existed holds the old header and is refused at open.
inline constexpr const char* kChunkHeader = "skip16";

struct Owners {
  uint64_t pack = 0;               // 0 = "one" (unpacked); else the greedy-pack target in bytes
  bool packed() const { return pack != 0; }
};

struct Component {
  Group group = Group::item;
  Owners owners;
  uint64_t split = 0;              // 0 = "never"; else split one owner's value at this many bytes
  Placement placement = Placement::adjacency;   // adjacency components only: where the edge props live
  // adjacency only: each entry carries its far-end vertex's label where the schema leaves it open (k > 1 far-end
  // labels: ceil(log2 k) bits per entry), so a hop yields (vid, label); off = the format without it, byte for byte
  bool far_label = true;
  bool grouped() const { return group == Group::owner; }   // owner = one record per owner (row / posting)
  bool packed() const { return owners.packed(); }          // {pack} = many owners per chunk
  bool props_own() const { return placement == Placement::own; }   // edge props in their own (ordinal) chunks
  bool lists_chunk() const { return packed(); }   // adjacency: stored as codec/lists' chunk
  bool operator==(const Component& o) const {
    return group == o.group && owners.pack == o.owners.pack && split == o.split && placement == o.placement &&
           far_label == o.far_label;
  }
  bool operator!=(const Component& o) const { return !(*this == o); }
};

// The secondary index is a co-location COMPONENT too, structurally identical to adjacency: the owner is
// an index VALUE, the items are the vids matching it. So it uses the same {group, owners, split}:
//   group item  = one key per (value, vid), empty value           = nebula's SortedIndex
//   group owner = one posting of vids per value                    = the PostingIndex (dgraph/ptcsr)
//   owners {pack: N} = many values packed into one chunk           = a packed index (interior cell) [IC3]
//   split N     = one value's vid list overflow-splits into parts  = dgraph's posting split (chunk_target)
// Plus `term` -- the value's GRANULARITY (exact ms | day bucket), orthogonal to the co-location.
enum class IndexTerm { day, exact };

// one edge type's per-direction overrides: a direction not given keeps the etype's component
struct DirComponents {
  std::optional<Component> out, in;
};

struct Config {
  int version = 1;

  Component vertex_props;          // the default: group item = one record per prop / group owner = one row
  Component adjacency;             // the default: group owner = grouped posting / group item = per-edge
  std::map<std::string, Component> vertex_props_by_label;   // per-label overrides (empty = uniform)
  // grouped props: per label, the props split into several rows (spec vertex_props.groups). Only on a
  // Condensed props layout (group owner, owners one). Group 0 (the first list) is the existence row, always
  // written; a later group is written only when one of its cells is present. A label not listed keeps ONE
  // group of every declared prop -- today's Condensed row, byte for byte. The names are checked against the
  // schema by the Meta ctor; each group's props are kept sorted (the canonical spec; the row's column order
  // is the schema's declared order, so it does not depend on how a hand-written list is ordered).
  std::map<std::string, std::vector<std::vector<std::string>>> vertex_groups;
  std::map<std::string, Component> adjacency_by_etype;      // per-edge-type overrides (empty = uniform)
  std::map<std::string, DirComponents> adjacency_by_etype_dir;   // per (edge type, direction) overrides

  bool dir_both = false;           // ingest writes both directions (nebula/pertype_csr) vs declared only
  Component index;                 // group item = sorted_keys / owner = posting (split = overflow) / {pack} = packed
  IndexTerm index_term = IndexTerm::exact;    // the value granularity: exact = full ms / day = day bucket

  static Config from_json(const json& j);   // a full spec, or fields over the defaults; strict keys; "preset" refused
  json to_json() const;                     // the full settled spec shape (store identity)
  void validate() const;                    // throws on an invalid or unbuilt combination (the Meta ctor calls it)

  // the ops read the LAYOUT (never a format name), per label / per (edge type, direction): the override, else
  // the default. There is no per-etype accessor on purpose: every site says which direction's lists it means.
  // Edge props follow their adjacency: inline in each direction's record / list / chunk, or (own placement, per
  // edge type) their own chunk family beside a packed adjacency.
  const Component& props_of(const std::string& label) const {
    auto it = vertex_props_by_label.find(label);
    return it == vertex_props_by_label.end() ? vertex_props : it->second;
  }
  const Component& adj_of(const std::string& etype, bool out) const {
    if (!adjacency_by_etype_dir.empty()) {
      auto d = adjacency_by_etype_dir.find(etype);
      if (d != adjacency_by_etype_dir.end()) {
        const std::optional<Component>& c = out ? d->second.out : d->second.in;
        if (c) return *c;
      }
    }
    auto it = adjacency_by_etype.find(etype);
    return it == adjacency_by_etype.end() ? adjacency : it->second;
  }
  bool index_grouped() const { return index.group == Group::owner; }   // owner = posting; item = sorted_keys
};

}  // namespace evalcpp::meta
