// meta's bulk ingest: flat buffers in, sorted runs out (the incremental writes are in writes.cc).
#include "formats/meta/format.h"
#include "formats/meta/format_shared.h"
#include "ingest/chunker.h"
#include "schema/far_labels.h"
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

json Meta::ingest_counts() const {
  return {{"vertices", n_vertices_}, {"edges", n_edges_},
          {"adjacency_split_units", n_split_units_}, {"adjacency_parts", n_parts_}};
}

// --- ingest: flat buffers in, sorted runs out. One run per label (vertex props) and one per (etype, dir)
// (unpacked adjacency), each written straight to the sink in key order; no key-value pair is held. The runs'
// key ranges overlap (edge keys are owner-major), which the sink takes (it ingests them in disjoint batches).
Value Meta::cell_value(const RowBuffer& b, size_t row, size_t col, const ColDecl& cd) const {
  if (cd.multi) {
    ValueList items;
    size_t n = b.multi_count(row, col);
    for (size_t k = 0; k < n; k++) items.push_back(Value(std::string(b.multi_at(row, col, k))));
    return Value::list(std::move(items));
  }
  if (stringlike(cd)) return string_value(cd, b.str_at(row, col));
  return Value(b.int_at(row, col));
}

void Meta::flush_label(KvSink& sink) {
  if (!cur_rows_) return;
  RowBuffer& rows = *cur_rows_;
  const std::vector<ColDecl>& cols = vcols(labels_.label());
  uint8_t vt = vtype_of(labels_.label());
  // a label's keys are 'v'·vtype·vid·[attr | group]: vid order is key order, whatever the layout
  std::vector<uint32_t> perm(rows.size());
  for (uint32_t i = 0; i < rows.size(); i++) perm[i] = i;
  std::stable_sort(perm.begin(), perm.end(), [&](uint32_t a, uint32_t b) { return rows.key(a) < rows.key(b); });
  const Component& pl = cfg_.props_of(labels_.label());   // this label's props layout
  sink.begin_run("v-" + labels_.label());
  if (pl.packed()) {   // chunked: pack uid-sorted rows into ~pack-byte blocks, one KV per chunk keyed by its first vid
    GreedyChunker chunk(pl.owners.pack);
    size_t begin = 0;
    for (size_t i = 0; i < rows.size(); i++) {
      if (chunk.add(rows.est(perm[i])) || i + 1 == rows.size()) {   // greedy-close-after: the crossing owner is included
        BufferRows window(rows, perm, begin, i + 1);
        sink.put(vertex_key(vt, rows.key(perm[begin])), encode_props(cc_, cols, window));
        begin = i + 1;
      }
    }
  } else if (pl.grouped()) {   // group=owner: one columns row per (vtype, vid, group); one group = the Condensed row
    BufferRows view(rows, perm, 0, rows.size());
    const RowGroups& rg = row_groups(labels_.label());
    std::vector<ColumnSubset> sub;
    for (auto& at : rg.at) sub.emplace_back(view, at);
    for (size_t i = 0; i < rows.size(); i++)
      for (size_t g = 0; g < rg.cols.size(); g++) {
        bool any = g == 0;   // group 0 is the existence row: always written; a later one only with a present cell
        for (size_t c = 0; !any && c < rg.at[g].size(); c++) any = sub[g].present(i, c);
        if (!any) continue;
        std::string enc;
        columns::encode_row(enc, rg.cols[g], sub[g], i);
        sink.put(vertex_group_key(vt, rows.key(perm[i]), g), enc);
      }
  } else {   // singleton: one prim record per present (row, column); attr = attr_of_col(column)
    for (size_t i = 0; i < rows.size(); i++) {
      uint32_t r = perm[i];
      bool any = false;
      for (size_t c = 0; c < cols.size(); c++) {   // attr = column + 1: column order is key order
        if (!rows.present(r, c)) continue;
        sink.put(vertex_attr_key(vt, rows.key(r), attr_of_col(c)), columns::encode_cell(cols[c], cell_value(rows, r, c, cols[c])));
        any = true;
      }
      // a vertex with no stored attr would leave no key: its existence (the label probe) is lost. LDBC has
      // none; refuse loudly rather than build a store that silently drops vertices.
      if (!any) throw std::runtime_error("meta ingest: a " + labels_.label() + " vertex has no stored property -- "
                                         "singleton vertex_props cannot record its existence (not built)");
    }
  }
  sink.end_run();
  cur_rows_.reset();
  labels_.finish();
}

void Meta::add_vertex(KvSink& sink, uint64_t uid, const std::string& label, const Value& props) {
  if (labels_.changes(label)) {
    flush_label(sink);
    labels_.start(label);
    cur_rows_ = std::make_unique<RowBuffer>(vcols(label));
  }
  cur_rows_->append(uid, props, cfg_.props_of(label).packed() ? uint32_t(est_vertex(vcols(label), props)) : 0);
  index_->ingest_vertex(sink, label, uid, props);   // index entries stream to the sink (buffered per id)
  n_vertices_++;
}

void Meta::add_edge(KvSink& sink, const std::string& etype, uint64_t src, uint64_t dst, const Value& props,
                    const std::string& src_label, const std::string& dst_label) {
  flush_label(sink);
  check_edge_endpoints(schema_, etype, src_label, dst_label, "meta");   // the one refusal rule (schema/far_labels.h)
  bool rev = reverse_available(etype);
  // the far end's label code per stored direction (out: the dst's label, in: the src's); a label outside the
  // schema's endpoints throws (far_code)
  auto far = [&](bool out, std::map<EdgePart, std::vector<uint8_t>>& into) {
    // checked at every k and in every setting: a hop learns the label (k = 1 always, a stored code when on)
    uint8_t code = far_code(etype, out, out ? dst_label : src_label);
    if (far_bits(etype, out)) into[{etype, out}].push_back(code);
  };
  // each stored direction by its own layout (config.h adj_of): a Chunked direction's pairs go to its partition
  // (padj_), an unpacked one is written from the etype's edge list (edges_, finalize). Placement is per etype.
  bool p_out = cfg_.adj_of(etype, true).packed(), p_in = rev && cfg_.adj_of(etype, false).packed();
  bool u_out = !p_out, u_in = rev && !p_in;
  if (p_out) {   // chunked: partition by (owner label, etype, dir)
    padj_[{etype, true}].append(src, dst);
    far(true, pfar_);
  }
  // the reverse list only where the store keeps it (direction both, or schema @reverse) -- the same
  // rule in every layout, so `direction` means the same thing in each
  if (p_in) { padj_[{etype, false}].append(dst, src); far(false, pfar_); }
  if (u_out || u_in) {
    edges_[etype].append(src, dst);
    if (u_out) far(true, ufar_);
    if (u_in) far(false, ufar_);
  }
  const std::vector<ColDecl>& cols = ecols(etype);
  if (cfg_.adj_of(etype, true).props_own()) {   // own placement (per etype: both directions Chunked, the ctor's rule)
    if (!cols.empty() && props.is_row() && !props.as_row().empty()) {   // ep row of the OUT edge; sparse
      auto& buf = peprops_[etype];
      if (!buf) buf = std::make_unique<RowBuffer>(cols);
      buf->append(padj_[{etype, true}].size() - 1, props, uint32_t(est_ep(props)));
    }
  } else if (!cols.empty()) {   // inline: the edge's props once (row = the edge's number), read by every stored copy
    auto& buf = efacets_[etype];
    if (!buf) buf = std::make_unique<RowBuffer>(cols);
    Value row = props.is_row() ? props : Value::row({});
    uint32_t e = uint32_t(buf->size());
    buf->append(e, row, uint32_t(est_props(row) - 8));   // the cells' estimate (the row base is the dst's; the split rule)
    if (p_out) pinline_[{etype, true}].push_back(e);
    if (p_in) pinline_[{etype, false}].push_back(e);
  }
  n_edges_++;
}

// a unique edge type holds at most one (src, etype, dst): refused at ingest in every layout (an out list sorted by
// (src, dst); Split would fold a parallel edge into one key, a list would keep both)
static void refuse_parallel(const std::string& etype, const std::vector<uint64_t>& src, const std::vector<uint64_t>& dst,
                            const std::vector<uint32_t>& perm) {
  for (size_t i = 1; i < perm.size(); i++)
    if (src[perm[i]] == src[perm[i - 1]] && dst[perm[i]] == dst[perm[i - 1]])
      throw std::runtime_error("meta ingest: a parallel " + etype + " edge " + hex16(src[perm[i]]) + " -> " +
                               hex16(dst[perm[i]]) + " (the edge type is unique; multi is not built)");
}

void Meta::finalize(KvSink& sink) {
  flush_label(sink);
  // unpacked adjacency (edges_ holds only the unpacked etypes): owner-major keys, one run per (etype, dir)
  // in (owner, other) order. packed = chunks (finalize_packed_adjacency, padj_). A mixed store
  // writes both.
  for (auto& [etype, eb] : edges_) {
    uint8_t eid = uint8_t(schema_.edge(etype).id);
    const std::vector<ColDecl>& cols = ecols(etype);
    RowBuffer* fbuf = efacets_.count(etype) ? efacets_[etype].get() : nullptr;
    for (bool out : {true, false}) {
      if (!out && !reverse_available(etype)) continue;
      const Component& al = cfg_.adj_of(etype, out);   // this direction's layout
      if (al.packed()) continue;                       // a Chunked direction: finalize_packed_adjacency
      uint64_t split_target = al.split ? al.split : UINT64_MAX;   // never = one unit (NOT 0)
      unsigned bits = far_bits(etype, out);
      const std::vector<uint8_t>* far = bits ? &ufar_.at({etype, out}) : nullptr;   // by edge number
      std::vector<uint32_t> perm = eb.sorted_perm(!out);   // by (owner, other)
      if (out) refuse_parallel(etype, eb.src, eb.dst, perm);
      sink.begin_run("uadj-" + etype + (out ? "-out" : "-in"));
      if (al.grouped()) {   // group=owner: one list per (owner, dir), codec/lists.h: ids + the entries' columns
        size_t i = 0;
        while (i < perm.size()) {
          uint64_t owner = out ? eb.src[perm[i]] : eb.dst[perm[i]];
          std::vector<uint64_t> others;
          std::vector<uint32_t> rows;   // the entries' edge rows in fbuf (edge index = its row there)
          while (i < perm.size() && (out ? eb.src[perm[i]] : eb.dst[perm[i]]) == owner) {
            others.push_back(out ? eb.dst[perm[i]] : eb.src[perm[i]]);
            rows.push_back(perm[i]);
            i++;
          }
          // whole, or parts at adjacency.split (never = one unit), in key order. A part closes by the chunker's rule
          // -- greedy close-after on the logical estimate (lists::est_chunk_owner: 9 + 5 per id + the entries' props
          // estimate) -- so an encoding never moves a boundary, the same rule a Chunked chunk closes by
          std::string k = adj_prefix(eid, out, owner);
          auto encode = [&](size_t lo, size_t hi) {
            std::vector<uint64_t> d(others.begin() + lo, others.begin() + hi);
            std::vector<uint8_t> labels;
            if (far)
              for (size_t e = lo; e < hi; e++) labels.push_back((*far)[rows[e]]);
            if (cols.empty()) return lists::encode(d, &labels, bits, cols, nullptr);
            BufferRows view(*fbuf, rows, lo, hi);
            return lists::encode(d, &labels, bits, cols, &view);
          };
          std::vector<std::pair<size_t, size_t>> parts;
          size_t lo = 0;
          uint64_t props_est = 0;
          for (size_t e = 0; e < others.size(); e++) {
            if (fbuf) props_est += fbuf->est(rows[e]);
            if (lists::est_chunk_owner(e + 1 - lo, props_est) >= split_target) {
              parts.push_back({lo, e + 1});
              lo = e + 1;
              props_est = 0;
            }
          }
          if (lo < others.size()) parts.push_back({lo, others.size()});
          if (parts.size() == 1) {
            sink.put(k, encode(0, others.size()));
          } else {
            auto id_of = [&](size_t e) { return others[e]; };
            for (size_t i = 0; i < parts.size(); i++)
              sink.put(unit::part_key_at(k, parts[i].first, id_of), encode(parts[i].first, parts[i].second));
            n_split_units_++;
            n_parts_ += parts.size();
          }
        }
      } else {   // singleton_per_edge: [far label] one byte of code + the edge's columns, one row (neither = an empty value)
        for (size_t idx = 0; idx < perm.size(); idx++) {
          uint32_t k = perm[idx];
          uint64_t s = out ? eb.src[k] : eb.dst[k], d = out ? eb.dst[k] : eb.src[k];
          std::string val;
          if (far) val.push_back(char((*far)[k]));
          if (!cols.empty()) {
            std::vector<uint32_t> one = {k};
            BufferRows view(*fbuf, one, 0, 1);
            columns::encode(val, cols, view);
          }
          sink.put(edge_key(eid, out, s, d), val);
        }
      }
      sink.end_run();
    }
  }
  if (!padj_.empty()) finalize_packed_adjacency(sink);   // adj + ep chunks, each its own run
  index_->finalize(sink);   // sorted_keys buffers nothing; the sink sorts the index runs
  edges_.clear();
  efacets_.clear();
  ufar_.clear();
  pfar_.clear();
  padj_.clear();
  peprops_.clear();
  pinline_.clear();
}

// packed adjacency + edge-prop chunks, keyed by (owner label, etype, dir), greedy-close-after; mirrors
// pertype_csr's finalize with meta's keys. Ordinals: out = sequential in the sorted out list, in = the
// edge's rank in the src-label's sorted out list (so an ep row is found by the same ordinal from either dir).
namespace {
class OrdinalRows : public RowSource {   // ep rows in ordinal order: row k = buffer row order[k], key = ordinals[k]
 public:
  OrdinalRows(const RowBuffer& b, const std::vector<uint32_t>& order, const std::vector<uint64_t>& ordinals,
              size_t begin, size_t end)
      : b_(b), perm_(order), ordinals_(ordinals), begin_(begin), end_(end) {}
  size_t size() const override { return end_ - begin_; }
  uint64_t key(size_t row) const override { return ordinals_[begin_ + row]; }
  bool present(size_t row, size_t col) const override { return b_.present(perm_[begin_ + row], col); }
  int64_t int_at(size_t row, size_t col) const override { return b_.int_at(perm_[begin_ + row], col); }
  std::string_view str_at(size_t row, size_t col) const override { return b_.str_at(perm_[begin_ + row], col); }
  size_t multi_count(size_t row, size_t col) const override { return b_.multi_count(perm_[begin_ + row], col); }
  std::string_view multi_at(size_t row, size_t col, size_t k) const override { return b_.multi_at(perm_[begin_ + row], col, k); }
 private:
  const RowBuffer& b_;
  const std::vector<uint32_t>& perm_;
  const std::vector<uint64_t>& ordinals_;
  size_t begin_, end_;
};
}  // namespace

// a chunk holds one vid group's owners (keys.h): the chunker closes the current chunk before an owner of another
static bool other_group(uint64_t first_owner, uint64_t owner) { return tag_of_uid(first_owner) != tag_of_uid(owner); }

// inline placement: one (etype, dir) partition's chunks, greedy close-after on est_chunk_owner (the inline props
// counted) and at every group boundary; a chunk = codec/lists' k-owner block, its edges' props rows read from
// efacets_ through the partition's edge numbers
void Meta::finalize_inline_chunks(KvSink& sink, const EdgePart& key, const EdgeBuffer& eb,
                                  const std::vector<uint32_t>& perm, uint64_t target) {
  auto& [etype, out] = key;
  const std::vector<ColDecl>& cols = ecols(etype);
  RowBuffer* fbuf = cols.empty() ? nullptr : efacets_.at(etype).get();
  const std::vector<uint32_t>* edge_no = cols.empty() ? nullptr : &pinline_.at(key);
  unsigned bits = far_bits(etype, out);
  const std::vector<uint8_t>* far = bits ? &pfar_.at(key) : nullptr;   // parallel to the partition's appends
  std::string prefix = padj_prefix(uint8_t(schema_.edge(etype).id), out);
  sink.begin_run("adj-" + etype + (out ? "-out" : "-in"));
  std::vector<lists::ChunkOwner> owners;
  std::vector<uint32_t> rows;   // the chunk's edges' rows in fbuf, owner order then dst order
  GreedyChunker chunk(target);
  auto flush = [&] {
    std::string v;
    if (fbuf) {
      BufferRows view(*fbuf, rows, 0, rows.size());
      v = lists::encode_chunk(owners, bits, cols, &view);
    } else {
      v = lists::encode_chunk(owners, bits, cols, nullptr);
    }
    sink.put(with_owner(prefix, owners.front().src), v);
    owners.clear();
    rows.clear();
    chunk.reset();
  };
  size_t i = 0;
  while (i < perm.size()) {
    lists::ChunkOwner o{eb.src[perm[i]], {}, {}};
    if (!owners.empty() && other_group(owners.front().src, o.src)) flush();
    uint64_t props_est = 0;
    while (i < perm.size() && eb.src[perm[i]] == o.src) {
      o.dsts.push_back(eb.dst[perm[i]]);
      if (far) o.labels.push_back((*far)[perm[i]]);
      if (fbuf) {
        uint32_t r = (*edge_no)[perm[i]];
        rows.push_back(r);
        props_est += fbuf->est(r);
      }
      i++;
    }
    uint64_t est = lists::est_chunk_owner(o.dsts.size(), props_est);
    owners.push_back(std::move(o));
    if (chunk.add(est)) flush();
  }
  if (!owners.empty()) flush();
  sink.end_run();
}

void Meta::finalize_packed_adjacency(KvSink& sink) {
  std::map<EdgePart, std::vector<uint32_t>> perms;
  // own placement with props: etype -> the sorted (src, dst) OUT list, and where each vid group's edges start in
  // it. An edge's ordinal = its rank among the out edges whose src is in the same group (the ep chunks are per
  // (etype, group): keys.h pep_prefix), so an ep row is found by the same ordinal from either direction.
  std::map<std::string, std::vector<std::pair<uint64_t, uint64_t>>> sorted_out;
  std::map<std::string, std::map<uint64_t, size_t>> group_start;
  for (auto& [key, eb] : padj_) {
    perms[key] = eb.sorted_perm();
    auto& [etype, out] = key;
    if (out) refuse_parallel(etype, eb.src, eb.dst, perms[key]);
    if (out && !ecols(etype).empty() && cfg_.adj_of(etype, true).props_own()) {
      auto& v = sorted_out[etype];
      v.reserve(eb.size());
      for (uint32_t i : perms[key]) v.emplace_back(eb.src[i], eb.dst[i]);
      for (size_t i = 1; i < v.size(); i++)
        if (v[i] == v[i - 1])
          throw std::runtime_error("meta packed ingest: duplicate edge (ambiguous ordinal)");
      auto& gs = group_start[etype];
      for (size_t i = 0; i < v.size(); i++) gs.emplace(tag_of_uid(v[i].first), i);   // the first index per group
    }
  }
  auto out_ordinal = [&](uint64_t src, const std::string& etype, uint64_t dst) -> uint64_t {
    auto& v = sorted_out.at(etype);
    auto jt = std::lower_bound(v.begin(), v.end(), std::make_pair(src, dst));
    if (jt == v.end() || *jt != std::make_pair(src, dst)) throw std::runtime_error("meta packed ingest: no out-edge to number");
    return uint64_t(jt - v.begin()) - group_start.at(etype).at(tag_of_uid(src));
  };
  for (auto& [key, eb] : padj_) {
    auto& [etype, out] = key;
    uint64_t target = cfg_.adj_of(etype, out).owners.pack;   // this direction's chunk target
    if (!cfg_.adj_of(etype, true).props_own()) {   // inline placement: codec/lists chunks, the props in each chunk
      finalize_inline_chunks(sink, key, eb, perms[key], target);
      continue;
    }
    // own placement: the inline chunk's form (codec/lists.h) with one ordinal per edge where the columns block is
    bool with_ords = !ecols(etype).empty();
    unsigned bits = far_bits(etype, out);
    const std::vector<uint8_t>* far = bits ? &pfar_.at(key) : nullptr;   // parallel to the partition's appends
    const std::vector<uint32_t>& perm = perms[key];
    std::string prefix = padj_prefix(uint8_t(schema_.edge(etype).id), out);
    sink.begin_run("adj-" + etype + (out ? "-out" : "-in"));
    std::vector<lists::ChunkOwner> buf;
    std::vector<uint64_t> ords;   // the chunk's edges' ordinals, owner order then dst order
    GreedyChunker chunk(target);
    auto flush = [&] {
      sink.put(with_owner(prefix, buf.front().src), lists::encode_chunk(buf, bits, kNoCols, nullptr, with_ords ? &ords : nullptr));
      buf.clear();
      ords.clear();
      chunk.reset();
    };
    size_t i = 0;
    while (i < perm.size()) {
      lists::ChunkOwner g{eb.src[perm[i]], {}, {}};
      if (!buf.empty() && other_group(buf.front().src, g.src)) flush();
      while (i < perm.size() && eb.src[perm[i]] == g.src) {
        uint64_t d = eb.dst[perm[i]];
        g.dsts.push_back(d);
        if (far) g.labels.push_back((*far)[perm[i]]);
        if (with_ords) ords.push_back(out ? out_ordinal(g.src, etype, d) : out_ordinal(d, etype, g.src));
        i++;
      }
      uint64_t est = est_adj(g.dsts.size(), with_ords);
      buf.push_back(std::move(g));
      if (chunk.add(est)) flush();
    }
    if (!buf.empty()) flush();
    sink.end_run();
  }
  for (auto& [etype, buf] : peprops_) {   // ep chunks (own placement), per (etype, src group): the edges that HAVE
    const EdgeBuffer& eb = padj_.at({etype, true});                       // props, in ordinal order
    const std::vector<uint32_t>& perm = perms.at({etype, true});
    std::vector<uint64_t> rank(perm.size());
    for (size_t k = 0; k < perm.size(); k++) rank[perm[k]] = k;
    std::vector<std::tuple<uint64_t, uint64_t, uint32_t>> rows;   // (group tag, ordinal, buffer row)
    rows.reserve(buf->size());
    for (size_t j = 0; j < buf->size(); j++) {
      size_t e = size_t(buf->key(j));                               // the edge's number in the OUT buffer
      uint64_t tag = tag_of_uid(eb.src[e]);
      rows.emplace_back(tag, rank[e] - group_start.at(etype).at(tag), uint32_t(j));
    }
    std::sort(rows.begin(), rows.end());
    const std::vector<ColDecl>& cols = ecols(etype);
    uint64_t target = cfg_.adj_of(etype, true).owners.pack;   // edge props follow their etype's adjacency (own: one component)
    uint8_t eid = uint8_t(schema_.edge(etype).id);
    size_t r0 = 0;
    while (r0 < rows.size()) {   // one run per group
      uint64_t tag = std::get<0>(rows[r0]);
      std::vector<uint64_t> ordinals;
      std::vector<uint32_t> order;
      for (size_t r = r0; r < rows.size() && std::get<0>(rows[r]) == tag; r++) {
        ordinals.push_back(std::get<1>(rows[r]));
        order.push_back(std::get<2>(rows[r]));
      }
      r0 += order.size();
      std::string prefix = pep_prefix(eid, uint8_t(tag));
      sink.begin_run("ep-" + etype + "-" + std::to_string(tag));
      size_t begin = 0;
      GreedyChunker chunk(target);
      for (size_t k = 0; k < order.size(); k++) {
        if (chunk.add(buf->est(order[k])) || k + 1 == order.size()) {
          OrdinalRows window(*buf, order, ordinals, begin, k + 1);
          sink.put(with_owner(prefix, ordinals[begin]), encode_ep(cc_, cols, window));
          begin = k + 1;
        }
      }
      sink.end_run();
    }
  }
}

}  // namespace evalcpp::meta
