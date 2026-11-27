// Per-type CSR's bulk ingest: row chunks, adjacency and edge-prop chunks (the incremental writes are in writes.cc).
#include "formats/pertype_csr/format.h"
#include "ingest/chunker.h"
#include "schema/far_labels.h"

#include <memory>
#include <unordered_map>
#include <algorithm>
#include <stdexcept>
#include <numeric>

#include "formats/common.h"
#include "formats/pertype_csr/keys.h"
#include "ir/expr.h"
#include "util/uidset.h"
#include "util/vid.h"

namespace evalcpp::pertype_csr {

// --- ingest -----------------------------------------------------------------------------------

namespace {
// Edge-prop rows in ordinal order: row k of the window is buffer row order[k] with key
// ordinals[k] (the chunk's key is the ordinal, not the buffer key).
class OrdinalRows : public RowSource {
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

void PerTypeCsr::add_vertex(KvSink& sink, uint64_t uid, const std::string& label, const Value& props) {
  if (labels_.changes(label)) {
    flush_vertices(sink);
    labels_.start(label);
    cur_rows_ = std::make_unique<RowBuffer>(vcols(label));
  }
  cur_rows_->append(uid, props, uint32_t(est_vertex(vcols(label), props)));
  n_vertices_++;
  index_->ingest_vertex(sink, label, uid, props);
}

// emit the chunks of one label's rows, uid-sorted, greedy on the logical estimate
void PerTypeCsr::flush_vertices(KvSink& sink) {
  if (!cur_rows_) return;
  RowBuffer& rows = *cur_rows_;
  std::vector<uint32_t> perm(rows.size());
  std::iota(perm.begin(), perm.end(), 0);
  std::stable_sort(perm.begin(), perm.end(), [&](uint32_t a, uint32_t b) { return rows.key(a) < rows.key(b); });
  const std::vector<ColDecl>& cols = vcols(labels_.label());
  std::string prefix = props_prefix(schema_.vertex(labels_.label()).id);
  sink.begin_run("props-" + labels_.label());
  size_t begin = 0;
  GreedyChunker chunk(cfg_.chunk_target);
  for (size_t i = 0; i < perm.size(); i++) {
    if (chunk.add(rows.est(perm[i])) || i + 1 == perm.size()) {
      BufferRows window(rows, perm, begin, i + 1);
      sink.put(with_id(prefix, rows.key(perm[begin])), encode_props(cc_, cols, window));
      begin = i + 1;
    }
  }
  sink.end_run();
  cur_rows_.reset();
  labels_.finish();
}

void PerTypeCsr::add_edge(KvSink& sink, const std::string& etype, uint64_t src, uint64_t dst, const Value& props,
                          const std::string& src_label, const std::string& dst_label) {
  flush_vertices(sink);                                    // the first edge ends the vertex stream
  // the reads trust the schema's endpoints (a hop tags a one-label far end; an unlabelled vid probes the owners):
  // an edge whose label pair the schema does not declare would be read wrongly, so it is refused here
  check_edge_endpoints(schema_, etype, src_label, dst_label, "pertype_csr");   // the one refusal rule (schema/far_labels.h)
  AdjKey out{src_label, etype, true}, in{dst_label, etype, false};
  adj_[out].append(src, dst);
  adj_[in].append(dst, src);
  // the far end's code of each entry, where the direction has more than one far label (an out entry's far end is
  // the dst, an in entry's the src)
  if (far_names(etype, true).size() > 1) adj_far_[out].push_back(far_code(etype, true, dst_label, "ingest"));
  if (far_names(etype, false).size() > 1) adj_far_[in].push_back(far_code(etype, false, src_label, "ingest"));
  n_edges_++;
  if (!ecols(etype).empty() && props.is_row() && !props.as_row().empty()) {
    // keyed by the edge's position in adj_[out] (its ordinal is known only after the sort);
    // an edge WITHOUT props gets no ep row, exactly as in Python
    auto& buf = eprops_[out];
    if (!buf) buf = std::make_unique<RowBuffer>(ecols(etype));
    buf->append(adj_[out].size() - 1, props, uint32_t(est_ep(props)));
  }
}

void PerTypeCsr::finalize(KvSink& sink) {
  flush_vertices(sink);
  // sorted (src, dst) order per key; the out keys of prop-carrying etypes also give the ordinals
  std::map<AdjKey, std::vector<uint32_t>> perms;
  std::map<std::pair<std::string, std::string>, std::vector<std::pair<uint64_t, uint64_t>>> sorted_out;
  for (auto& [key, eb] : adj_) {
    perms[key] = eb.sorted_perm();
    if (key.out && !ecols(key.etype).empty()) {
      auto& v = sorted_out[{key.label, key.etype}];
      v.reserve(eb.size());
      for (uint32_t i : perms[key]) v.emplace_back(eb.src[i], eb.dst[i]);
      for (size_t i = 1; i < v.size(); i++)
        if (v[i] == v[i - 1])
          throw std::runtime_error("pertype_csr ingest: duplicate edge " + hex16(v[i].first) + " -" + key.etype +
                                   "-> " + hex16(v[i].second) + " (its ordinal would be ambiguous)");
    }
  }
  // ordinal of the out-edge src -etype-> dst: its rank in the sorted out list of the src's label (`lab`: the
  // record's own, kept per edge at ingest -- never taken from the vid)
  auto out_ordinal = [&](const std::string& lab, uint64_t src, const std::string& etype, uint64_t dst) -> uint64_t {
    auto it = sorted_out.find({lab, etype});
    if (it != sorted_out.end()) {
      auto& v = it->second;
      auto jt = std::lower_bound(v.begin(), v.end(), std::make_pair(src, dst));
      if (jt != v.end() && *jt == std::make_pair(src, dst)) return uint64_t(jt - v.begin());
    }
    throw std::runtime_error("pertype_csr: no out-edge " + hex16(src) + " -" + etype + "-> " + hex16(dst) + " to number");
  };
  for (auto& [key, eb] : adj_) {
    bool with_ords = !ecols(key.etype).empty();
    const std::vector<uint32_t>& perm = perms[key];
    std::string prefix = adj_prefix(schema_.vertex(key.label).id, schema_.edge(key.etype).id, key.out);
    sink.begin_run("adj-" + key.label + "-" + key.etype + (key.out ? "-out" : "-in"));
    // each entry's far-end code (kept when the direction has more than one far label), and whether it is STORED
    const std::vector<std::string>& far = far_names(key.etype, key.out);
    auto ft = adj_far_.find(key);
    const std::vector<uint8_t>* codes = ft == adj_far_.end() ? nullptr : &ft->second;
    if (far.size() > 1 && (!codes || codes->size() != eb.size()))
      throw std::logic_error("pertype_csr ingest: far-end codes out of step with the edges of " + key.etype);
    unsigned bits = far_bits(key.etype, key.out);
    std::vector<AdjGroup> buf;
    GreedyChunker chunk(cfg_.chunk_target);
    size_t i = 0;
    uint64_t next_ordinal = 0;
    while (i < perm.size()) {
      AdjGroup g;
      g.src = eb.src[perm[i]];
      while (i < perm.size() && eb.src[perm[i]] == g.src) {
        uint64_t d = eb.dst[perm[i]];
        g.dsts.push_back(d);
        // an in entry's edge is numbered in its SOURCE label's out list: the far end's label
        if (with_ords) g.ords.push_back(key.out ? next_ordinal++ : out_ordinal(codes ? far[(*codes)[perm[i]]] : far.at(0), d, key.etype, g.src));
        if (bits) g.labels.push_back((*codes)[perm[i]]);
        i++;
      }
      uint64_t est = est_adj(g.dsts.size(), with_ords);   // the chunker's logical size: unchanged by the label block
      buf.push_back(std::move(g));
      if (chunk.add(est) || i == perm.size()) {
        sink.put(with_id(prefix, buf.front().src), encode_adj(cc_, buf, with_ords, bits));
        buf.clear();
        chunk.reset();
      }
    }
    sink.end_run();
  }
  // edge-prop chunks: the rows of the edges that HAVE props, in ordinal order (ordinal = the
  // pair's rank in the sorted out list; sparse when some edges carry no props)
  for (auto& [key, buf] : eprops_) {
    const std::vector<uint32_t>& perm = perms[key];
    std::vector<uint64_t> rank(perm.size());
    for (size_t k = 0; k < perm.size(); k++) rank[perm[k]] = k;
    std::vector<std::pair<uint64_t, uint32_t>> rows;        // (ordinal, buffer row), sorted
    rows.reserve(buf->size());
    for (size_t i = 0; i < buf->size(); i++) rows.emplace_back(rank[size_t(buf->key(i))], uint32_t(i));
    std::sort(rows.begin(), rows.end());
    std::vector<uint64_t> ordinals;
    std::vector<uint32_t> order;
    for (auto& r : rows) { ordinals.push_back(r.first); order.push_back(r.second); }
    const std::vector<ColDecl>& cols = ecols(key.etype);
    std::string prefix = ep_prefix(schema_.vertex(key.label).id, schema_.edge(key.etype).id);
    sink.begin_run("ep-" + key.label + "-" + key.etype);
    size_t begin = 0;
    GreedyChunker chunk(cfg_.chunk_target);
    for (size_t k = 0; k < order.size(); k++) {
      if (chunk.add(buf->est(order[k])) || k + 1 == order.size()) {
        OrdinalRows window(*buf, order, ordinals, begin, k + 1);
        sink.put(with_id(prefix, ordinals[begin]), encode_ep(cc_, cols, window));
        begin = k + 1;
      }
    }
    sink.end_run();
  }
  adj_.clear();
  adj_far_.clear();
  eprops_.clear();
  index_->finalize(sink);   // whatever the index layout buffered (posting: its runs)
}

json PerTypeCsr::ingest_counts() const { return {{"vertices", n_vertices_}, {"edges", n_edges_}}; }

}  // namespace evalcpp::pertype_csr
