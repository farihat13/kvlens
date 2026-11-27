// NebulaGraph's bulk ingest: vertex rows and edge keys (the incremental writes are in writes.cc).
#include "formats/nebula/format.h"

#include <algorithm>
#include <numeric>
#include <optional>
#include <stdexcept>

#include <unordered_map>

#include "exec/label_tags.h"
#include "formats/common.h"
#include "formats/nebula/keys.h"
#include "codec/rowv2.h"
#include "util/uidset.h"
#include "util/vid.h"

namespace evalcpp::nebula {

// --- ingest -----------------------------------------------------------------------------------

void Nebula::add_vertex(KvSink& sink, uint64_t uid, const std::string& label, const Value& props) {
  if (labels_.changes(label)) {
    flush_vertices(sink);
    labels_.start(label);
    cur_rows_ = std::make_unique<RowBuffer>(vcols(label));
  }
  cur_rows_->append(uid, props, 0);
  n_vertices_++;
  for (auto& k : index_.vertex_keys(label, uid, props))                // nebula add_vertex's index writes
    sink.put_index(int(IndexKeys::index_id_of(k)), k);
}

// one key run per label: (tag, vid) rows in vid order, each a RowV2 value
void Nebula::flush_vertices(KvSink& sink) {
  if (!cur_rows_) return;
  RowBuffer& rows = *cur_rows_;
  std::vector<uint32_t> perm(rows.size());
  std::iota(perm.begin(), perm.end(), 0);
  std::stable_sort(perm.begin(), perm.end(), [&](uint32_t a, uint32_t b) { return rows.key(a) < rows.key(b); });
  const std::vector<ColDecl>& cols = vcols(labels_.label());
  int tag = schema_.vertex(labels_.label()).id;
  BufferRows view(rows, perm, 0, rows.size());
  sink.begin_run("vertex-" + labels_.label());
  for (size_t i = 0; i < perm.size(); i++) {
    if (i > 0 && rows.key(perm[i]) == rows.key(perm[i - 1]))
      throw std::runtime_error("nebula ingest: duplicate vertex " + hex16(rows.key(perm[i])) + " under " + labels_.label());
    sink.put(vertex_key(tag, rows.key(perm[i])), rowv2_encode(cols, view, i, cfg_.row_timestamp));
  }
  sink.end_run();
  cur_rows_.reset();
  labels_.finish();
}

void Nebula::add_edge(KvSink& sink, const std::string& etype, uint64_t src, uint64_t dst, const Value& props,
                      const std::string&, const std::string&) {
  flush_vertices(sink);                                    // the first edge ends the vertex stream
  edges_[etype].append(src, dst);
  n_edges_++;
  if (!ecols(etype).empty()) {                             // the props row, aligned with the pair
    auto& buf = eprops_[etype];
    if (!buf) buf = std::make_unique<RowBuffer>(ecols(etype));
    buf->append(edges_[etype].size() - 1, props.is_row() ? props : Value::row({}), 0);
  }
}

void Nebula::finalize(KvSink& sink) {
  flush_vertices(sink);
  flush_edges(sink);
}

// every directed edge under BOTH key orientations, each with a full copy of its props:
// one run per (etype, direction), sorted by (src, dst) / (dst, src). Virtual: the hybrid
// flushes its DATED etypes date-keyed, then delegates the rest here.
void Nebula::flush_edges(KvSink& sink) {
  for (auto& [etype, eb] : edges_) {
    int eid = schema_.edge(etype).id;
    const std::vector<ColDecl>& cols = ecols(etype);
    std::string no_props;
    if (cols.empty()) {
      no_props.assign(1, '\0');
      no_props.append(size_t(cfg_.row_timestamp), '\0');
    }
    for (bool out : {true, false}) {
      std::vector<uint32_t> perm = eb.sorted_perm(!out);
      sink.begin_run("edge-" + etype + (out ? "-out" : "-in"));
      for (size_t i = 0; i < perm.size(); i++) {
        uint32_t k = perm[i];
        uint64_t s = out ? eb.src[k] : eb.dst[k], d = out ? eb.dst[k] : eb.src[k];
        if (i > 0 && eb.src[perm[i - 1]] == eb.src[k] && eb.dst[perm[i - 1]] == eb.dst[k])
          throw std::runtime_error("nebula ingest: duplicate edge " + hex16(eb.src[k]) + " -" + etype + "-> " + hex16(eb.dst[k]));
        if (cols.empty()) sink.put(edge_key(eid, out, s, d), no_props);
        else {
          BufferRows view(*eprops_[etype], perm, i, i + 1);
          sink.put(edge_key(eid, out, s, d), rowv2_encode(cols, view, 0, cfg_.row_timestamp));
        }
      }
      sink.end_run();
    }
  }
  edges_.clear();
  eprops_.clear();
}

json Nebula::ingest_counts() const { return {{"vertices", n_vertices_}, {"edges", n_edges_}}; }

}  // namespace evalcpp::nebula
