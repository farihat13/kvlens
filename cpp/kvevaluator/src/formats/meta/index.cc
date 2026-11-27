// meta's lookups by label or property value: a label's vertices, the two label filters, the index top-k.
#include "formats/meta/format.h"
#include "formats/meta/format_shared.h"
#include "codec/decode_counts.h"

#include <algorithm>
#include <iterator>
#include <optional>
#include <unordered_map>
#include <unordered_set>
#include <stdexcept>
#include <vector>

#include "codec/columns.h"
#include "codec/lists.h"
#include "formats/common.h"
#include "formats/meta/keys.h"
#include "store/unit_value.h"
#include "util/uidset.h"
#include "util/vid.h"

namespace evalcpp::meta {

// --- index ops: delegated to the controlled SortedIndex (index_), the same walks nebula does -------
// --- a label's vertices: its index's membership, or (props) ONE sequential scan of the label's own key run ----
// With `props` the op walks 'v'·vtype from its first key to its last and returns one row per vertex (label_rows_one_run:
// every props layout keys a label's records together). What the run holds is what the scan costs, 1 SEEK + 1 NEXT per
// key, each key's bytes and its whole value:
//   Chunked    one key per chunk; the chunk decoded whole
//   Condensed  one key per vertex and one more per extra props group ('v'·vtype·vid·[group]): EVERY group key is
//              crossed and charged, also a group that holds none of the asked props (a vertex's groups sit together,
//              so a forward scan cannot skip one)
//   Split      one key per stored property ('v'·vtype·vid·attr): EVERY property of the label, also when one is asked
// Used bytes are the asked properties' only -- on Condensed and Split; the Chunked branch counts none, as before and as
// pertype_csr's scan, so read-over-used of this op is not comparable across the three layouts. With only
// the key-derived "id" asked nothing is used on Condensed or Split (a point read reads nothing then).
// `keep` (a vid set, optional): rows of other vertices are not built. The
// set is tested in memory and costs nothing; every key is still crossed and counted, so counts do not depend on it.
Value Meta::read_vertices_by_label(StepExec& ex, const std::string& label, const Value& props, const Value& keep) {
  if (!props.is_null()) {
    const std::vector<ColDecl>* cols = &vcols(label);
    const Component& pl = cfg_.props_of(label);
    std::string prefix = vertex_family(vtype_of(label));
    bool want_id = false;
    std::vector<std::string> row_props;
    for (auto& p : props.as_list()) (p.as_str() == "id" ? want_id = true : (row_props.push_back(p.as_str()), false));
    bool filtered = !keep.is_null();
    std::unordered_set<uint64_t> kept;
    if (filtered)
      for (auto& v : keep.as_list()) kept.insert(uid_or_throw("meta", v));
    auto wanted = [&](uint64_t u) { return !filtered || kept.count(u) > 0; };
    // one output row, its fields in the order this layout's point read gives them (read_vertex_props: Condensed and
    // Chunked put the key-derived id first, Split keeps the asked order), so a body may swap one read for the other:
    // the vid (tagged with the label where the plan carries labels), then the props
    std::vector<int> field_at;   // per output field: its place in row_props, -1 = the id
    if (pl.grouped()) {
      if (want_id) field_at.push_back(-1);
      for (size_t k = 0; k < row_props.size(); k++) field_at.push_back(int(k));
    } else {
      size_t k = 0;
      for (auto& p : props.as_list()) field_at.push_back(p.as_str() == "id" ? -1 : int(k++));
    }
    std::vector<std::string> names = {"_vid"};
    for (int at : field_at) names.push_back(at < 0 ? std::string("id") : row_props[size_t(at)]);
    RowBuilder rb(names);                                   // the call's one shape (the Chunked loop's too: same order)
    auto emit = [&](ValueList& out, uint64_t u, std::vector<Value>& cells) {   // moves the cells out (reset after)
      Value v = Value::vid(u);
      RowBuilder::Row f = rb.start();
      f.push(tagged(v, label));   // the label's own key run (an index-sourced label: kept under every label_tags)
      for (int at : field_at) f.push(at < 0 ? key_id_value(u) : std::move(cells[size_t(at)]));
      out.push_back(f.done());
    };
    std::vector<TaskFn> tasks = {[&](WorkerSlot& slot, TaskResult& r) {   // one scan of the run
      Counts before = slot.store.counts();
      ValueList out;
      Cursor cur = slot.store.seek(prefix);
      if (pl.packed()) {   // Chunked: each chunk decoded whole. The row loop is the fused scan's own (each cell moved
        // straight into its row; no shared emit: 5.6% of the scan's warm wall), plus the keep test
        while (cur.valid() && has_prefix(cur.key(), prefix)) {
          std::string_view raw = cur.value();
          if (raw.size() > slot.scan_chunk_bytes_max) slot.scan_chunk_bytes_max = raw.size();   // held one at a time
          std::shared_ptr<PropsChunk> chunk = PropsChunk::decode(&cc_, cols, std::string(raw));
          chunk->bulk();   // every row is wanted: one whole-chunk decode
          std::vector<int> cidx;
          for (auto& p : row_props) cidx.push_back(chunk->col_index(p));
          uint64_t used_v = 0;   // a kept row's asked cells' framed bytes (Condensed's branch below); the key: none
          for (size_t i = 0; i < chunk->size(); i++) {
            if (filtered && !kept.count(chunk->ids()[i])) continue;
            Value v = Value::vid(chunk->ids()[i]);
            RowBuilder::Row f = rb.start();
            f.push(tagged(v, label));   // the label's own chunk run (index-sourced: kept under every label_tags)
            if (want_id) f.push(key_id_value(chunk->ids()[i]));
            for (size_t k = 0; k < row_props.size(); k++) {
              f.push(cidx[k] >= 0 ? chunk->cell(size_t(cidx[k]), i) : Value());
              if (cidx[k] >= 0) used_v += chunk->cell_bytes(size_t(cidx[k]), i);
            }
            out.push_back(f.done());
          }
          slot.store.count_used(0, used_v);
          cur.next();
        }
      } else {   // Condensed / Split: a vertex's keys are consecutive (vid, then group or attr); a row per vertex
        const RowGroups* rg = pl.grouped() ? &row_groups(label) : nullptr;
        std::vector<int> attr_slot(256, -1);   // Split: attr byte -> its place in row_props
        if (!rg)
          for (size_t k = 0; k < row_props.size(); k++) {
            int a = attr_id(label, row_props[k]);
            if (a >= 0) attr_slot[size_t(a)] = int(k);
          }
        // Condensed: the groups a point read of these props would GET (group 0 when none is the label's; none at all
        // when only the key-derived id is asked): their keys are the used ones, as in props_of / project_groups
        std::vector<size_t> touch = rg && !row_props.empty() ? groups_for(label, row_props) : std::vector<size_t>{};
        const size_t vid_at = prefix.size(), plain = prefix.size() + 8;   // 'v'·vtype·vid(8)·[group | attr]
        bool open = false, want = false;
        uint64_t at = 0, used_k = 0, used_v = 0;
        std::vector<Value> cells(row_props.size());
        auto close = [&] {
          if (open && want) emit(out, at, cells);
          for (auto& c : cells) c = Value();
        };
        while (cur.valid() && has_prefix(cur.key(), prefix)) {
          std::string_view k = cur.key();
          if (k.size() < plain || k.size() > plain + 1)
            throw std::runtime_error("meta read_vertices_by_label: a key of " + label + "'s run is not a vertex record");
          uint64_t u = trailing_u64(k.substr(vid_at, 8));
          uint8_t sub = k.size() > plain ? uint8_t(k[plain]) : 0;   // the props group (Condensed) or the attr (Split)
          std::string_view raw = cur.value();   // EVERY key's value is read and charged: a scan cannot skip one
          if (!open || u != at) { close(); open = true; at = u; want = wanted(u); }
          if (want && rg) {   // Condensed: this group's row; its asked cells
            if (sub >= rg->cols.size())
              throw std::runtime_error("meta read_vertices_by_label: " + label + " has no props group " + std::to_string(sub));
            columns::View row(&rg->cols[sub], raw, 0, 1);
            for (size_t i = 0; i < row_props.size(); i++) {
              int c = row.col_index(row_props[i]);
              if (c < 0) continue;
              cells[i] = row.cell(size_t(c), 0);
              used_v += row.cell_bytes(size_t(c), 0);
            }
            if (std::find(touch.begin(), touch.end(), size_t(sub)) != touch.end()) used_k += k.size();
          } else if (want) {   // Split: one property's record; asked or not, it is crossed
            int slot_i = attr_slot[sub];
            if (slot_i >= 0) {
              cells[size_t(slot_i)] = columns::decode_cell((*cols)[size_t(sub) - 1], raw);
              used_k += k.size();
              used_v += raw.size();
            }
          }
          cur.next();
        }
        close();
        slot.store.count_used(used_k, used_v);
      }
      end_scan(cur);
      r.stats.direct = slot.store.counts().since(before);
      slot.store.parts_commit(&r.stats.direct);   // a direct-count task commits its per-part split
      r.out = Value::list(std::move(out));
    }};
    return ex.run(tasks)[0].out;
  }
  if (!keep.is_null()) throw std::runtime_error("meta read_vertices_by_label: keep needs props (the scan of the label's rows)");
  vcols(label);
  std::vector<Value> r = ex.run_range_map(1, [&](WorkerSlot& slot, size_t, std::vector<Landing>&) {
      return Value::list(index_->enumerate(slot.store, label));   // 1 SEEK + N NEXT over the membership
  }, /*additive_counts=*/true);
  return tag_all(r[0], label);   // the label's own index: every vid carries it, under every label_tags setting
}

Value Meta::read_filter_by_label_probe(StepExec& ex, const ValueList& vids, const std::vector<std::string>& labels) {
  // one existence probe per DISTINCT vid (a repeated vid is kept at every occurrence). A plan reaches this op when its
  // variant filters by probing (e.g. no single-posting type index to intersect with), on any props layout. Grouped: a GET on the
  // row key; packed: locate the chunk; singleton: 1 SEEK to 'v'·vtype·vid -- the label is in the key, so
  // any attr record under that prefix proves membership (ingest refuses a vertex with no stored attr).
  if (vids.empty()) return Value::list({});
  if (labels.size() != 1) throw std::runtime_error("meta read_filter_by_label_probe needs one label");
  uint8_t vt = vtype_of(labels[0]);
  const Component& pl = cfg_.props_of(labels[0]);
  bool packed = pl.packed();                          // packed: the only 'v' keys are chunk keys, so a point
  FanOut fo;                                          // GET misses -- locate the chunk + find, like pertype_csr
  if (packed) register_props(fo, schema_);
  // each vid decided by its first TAGGED occurrence in the input, as read_vertex_props (distinct_vids): an untagged
  // twin of a tagged vid is not probed
  DistinctVids d = distinct_vids("meta", vids, ex.strict_tags);
  std::vector<Value> res = ex.run_range_map(d.uids.size(), [&](WorkerSlot& slot, size_t di, std::vector<Landing>& log) {
      uint64_t u = d.uids[di];
      const Value& carried = d.values[di];   // the label of the vid's first tagged occurrence
      // the label this value carries decides it (an upstream read found it) -- existence too, unless a vertex write ran
      // earlier in this run (a tag states a label, not that the vertex is still there)
      if (std::optional<std::string> k = tag_label(carried); k && !ex.after_vertex_write) return Value(*k == labels[0]);
      if (packed) {
        const std::vector<ColDecl>* cols = &vcols(labels[0]);
        ChunkReader rd(fo.table, slot, log, 0);
        const PropsChunk* chunk = rd.locate<PropsChunk>(vertex_family(vt), u,
            [cols, this](std::string_view raw) { return PropsChunk::decode(&cc_, cols, std::string(raw)); });
        return Value(chunk && chunk->find(u) >= 0);
      }
      if (!pl.grouped()) return Value(split_exists(slot.store, labels[0], u));   // singleton: the prefix SEEK
      std::string k = vertex_key(vt, u);
      bool got = slot.store.get(k).has_value();
      if (got) slot.store.count_used(k.size(), 0);   // existence by key; a fetched row = amp, a miss = 0 used
      return Value(got);
  }, /*additive_counts=*/!packed);
  ValueList out;
  for (size_t i = 0; i < vids.size(); i++)
    if (res[d.at.empty() ? i : d.at[i]].as_bool()) {
      out.push_back(ex.tags.probes ? tagged(vids[i], labels[0]) : vids[i]);   // a kept vid carries the label
    }
  return Value::list(std::move(out));
}

Value Meta::read_filter_by_label_index(StepExec& ex, const ValueList& vids, const std::vector<std::string>& labels) {
  if (vids.empty()) return Value::list({});
  if (labels.size() != 1) throw std::runtime_error("meta read_filter_by_label_index needs one label");
  UidSet want(vids.size());                            // probe the small candidate set during the walk
  UidSet found(vids.size());
  size_t unknown = 0;                                  // the label a vid's value carries decides it (no vertex write yet)
  for (auto& v : vids) {
    uint64_t u = uid_or_throw("meta", v);
    want.insert(u);
    std::optional<std::string> k = ex.after_vertex_write ? std::nullopt : tag_label(v);
    if (!k) unknown++;
    else if (*k == labels[0]) found.insert(u);
  }
  if (unknown)                                         // the walk only while some vid's label is unknown
    ex.run_range_map(1, [&](WorkerSlot& slot, size_t, std::vector<Landing>&) {
        index_->enumerate_uids(slot.store, labels[0], [&](uint64_t u) { if (want.contains(u)) found.insert(u); });
        return Value::list({});
    }, /*additive_counts=*/true);
  ValueList out;                                       // keep the members, in input order
  for (auto& v : vids)
    if (found.contains(uid_or_throw("meta", v))) {
      out.push_back(ex.tags.probes ? tagged(v, labels[0]) : v);   // the label's own index holds it: the vid carries it
    }
  return Value::list(std::move(out));
}

TopKReads Meta::top_k_reads(StepExec&, const kv::TopKArgs& t) {
  labels_or_throw("read_top_k_by_index", t.labels);   // each label is the schema's
  for (auto& l : t.labels) vcols(l);
  bool packed = props_layout("read_top_k_by_index", t.labels).packed();
  bool day = index_->date_granularity() != DateGranularity::full;
  TopKReads rd;
  rd.fmt = "meta";
  auto hit_of = [this](const IndexHit& h) {   // the label's index holds it: the row carries the label (always)
    return TopKHit{tagged(Value::vid(h.vid), h.label), h.value, key_id_value(h.vid).as_int(), h.label};
  };
  if (packed) {   // Chunked props (Per-type CSR's, always full): the walk reads no prop; the winners' chunk lookups
                  // replay from one cache for the call
    auto fo = std::make_shared<FanOut>();
    register_props(*fo, schema_);
    auto prb = std::make_shared<RowBuilder>(props_row_names(props_layout("read_top_k_by_index", t.labels), t.props));
    rd.winners_replayed = true;
    rd.walk = [this, &t, hit_of](WorkerSlot& slot, std::vector<Landing>&, const TopKScope& scope) {
      std::vector<TopKHit> hits;
      for (auto& h : index_->top_k_desc(slot.store, t.labels, t.prop, t.k, scope.hi, scope.cands(), {}))
        hits.push_back(hit_of(h));
      return hits;
    };
    rd.winner_props = [this, &t, fo, prb](WorkerSlot& slot, std::vector<Landing>& log, const TopKHit& h) {
      ChunkReader rdr(fo->table, slot, log, 0);
      Value row = props_of_packed(slot.store, rdr, h.vid.as_vid(), t.props, {h.label}, *prb);
      std::vector<Value> cells;
      for (auto& p : t.props) cells.push_back(row.field(p));
      return cells;
    };
    return rd;
  }
  // not Chunked. A DAY index resolves each collected candidate's EXACT value from its vertex (1 read), dropping
  // boundary-day entries at/past the exclusive hi; the grouped row blob is cached so the winners' props reuse it (no
  // second GET). Full precision needs no resolver.
  auto blobs = std::make_shared<std::map<uint64_t, std::pair<size_t, std::string>>>();   // uid -> (group, row blob)
  rd.walk = [this, &t, day, blobs, hit_of](WorkerSlot& slot, std::vector<Landing>&, const TopKScope& scope) {
    const std::string& prop = t.prop;
    TopKResolver resolve;
    if (day)
      resolve = [&, this](uint64_t uid, int64_t, const std::string& label) -> std::optional<int64_t> {
        if (cfg_.props_of(label).grouped()) {
          size_t g = groups_for(label, {prop})[0];            // the sort prop's own row
          std::string vk = vertex_group_key(vtype_of(label), uid, g);
          std::optional<std::string> blob = slot.store.get(vk);
          if (!blob) return std::nullopt;                    // in the index, no row: not a result
          columns::View rv(&row_groups(label).cols[g], *blob, 0, 1);
          int c = rv.col_index(prop);
          if (c < 0) return std::nullopt;
          Value ms = rv.cell(size_t(c), 0);
          slot.store.count_used(vk.size(), rv.cell_bytes(size_t(c), 0));
          if (ms.is_null() || (scope.hi && ms.as_int() >= *scope.hi)) return std::nullopt;
          if (!t.props.empty()) (*blobs)[uid] = {g, std::move(*blob)};   // kept for the winners' props
          return ms.as_int();
        }
        int a = attr_id(label, prop);                         // singleton: the sort prop's own key
        if (a < 0) return std::nullopt;
        std::string k2 = vertex_attr_key(vtype_of(label), uid, uint8_t(a));
        std::optional<std::string> raw = slot.store.get(k2);
        if (!raw) return std::nullopt;
        Value ms = columns::decode_cell(vcols(label)[size_t(a) - 1], *raw);
        slot.store.count_used(k2.size(), raw->size());
        if (ms.is_null() || (scope.hi && ms.as_int() >= *scope.hi)) return std::nullopt;
        return ms.as_int();
      };
    std::vector<TopKHit> hits;
    for (auto& h : index_->top_k_desc(slot.store, t.labels, prop, t.k, scope.hi, scope.cands(), resolve))
      hits.push_back(hit_of(h));
    return hits;
  };
  rd.winner_props = [this, &t, blobs](WorkerSlot& slot, std::vector<Landing>&, const TopKHit& h) {
    uint64_t vid = h.vid.as_vid();
    std::vector<Value> cells;
    if (cfg_.props_of(h.label).grouped()) {   // grouped: reuse the resolver's cached row, GET the other touched rows
      std::map<size_t, std::optional<std::string>> have;
      auto cached = blobs->find(vid);
      if (cached != blobs->end()) have[cached->second.first] = cached->second.second;
      std::vector<std::string> row_props;
      for (auto& p : t.props) if (p != "id") row_props.push_back(p);
      std::map<std::string, Value> got;
      if (!row_props.empty())   // only id: key-derived, no row read
        project_groups(slot.store, h.label, vid, row_props, have, got);
      for (auto& p : t.props) {
        if (p == "id") { cells.push_back(key_id_value(vid)); continue; }   // key-derived, free
        auto c = got.find(p);
        cells.push_back(c != got.end() ? c->second : Value());
      }
    } else {                       // singleton: the per-attr reads (props_of handles the counts)
      RowBuilder srb(props_names(t.props));   // a singleton winner's props row (split_props' order)
      PropsPlan pp = props_plan(cfg_.props_of(h.label), t.props, {h.label});
      PropsScratch ps;
      Value pr = props_of(slot.store, vid, t.props, {h.label}, pp, ps, srb);
      for (auto& p : t.props) cells.push_back(pr.field(p));
    }
    return cells;
  };
  return rd;
}

}  // namespace evalcpp::meta
