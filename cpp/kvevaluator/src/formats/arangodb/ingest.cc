// ArangoDB's bulk ingest: documents, edge documents, the primary and edge indexes (the document codec is in format.cc).
#include "formats/arangodb/format.h"

#include <algorithm>
#include <optional>
#include <stdexcept>
#include <unordered_map>
#include <unordered_set>

#include "exec/label_tags.h"

#include "codec/rowv2.h"
#include "formats/arangodb/keys.h"
#include "formats/arangodb/vpack.h"
#include "formats/common.h"
#include "index/sorted_index.h"
#include "ir/expr.h"
#include "util/uidset.h"
#include "util/vid.h"

namespace evalcpp::arangodb {

// --- ingest -----------------------------------------------------------------------------------

void Arango::add_vertex(KvSink& sink, uint64_t uid, const std::string& label, const Value& props) {
  if (labels_.changes(label)) {
    flush_pk(sink);
    labels_.start(label);
    sink.begin_run("docs-" + label);
    doc_run_open_ = true;
  }
  note_vertex(uid, label);
  n_vertices_++;
  uint64_t docid = ++docn_;
  sink.put(doc_key(schema_.vertex(label).id, docid), doc_encode(vdoc_cols(label), {{"_key", Value(hex16(uid))}}, without_key_id(props), label));
  cur_pk_.emplace_back(uid, docid);
  for (auto& [name, val] : props.as_row()) {
    const IndexDecl* ix = schema_.find_index(label, name);
    if (!ix || ix->kind == "type" || val.is_null()) continue;
    sink.put_index(ix->id, IndexKeys::key(IndexKeys::prefix(ix->id),
                                          IndexKeys::refuse_null_slot(term_bytes(label, name, val, false), "ingest " + label + "." + name),
                                          docid));
  }
}

void Arango::flush_pk(KvSink& sink) {
  if (doc_run_open_) { sink.end_run(); doc_run_open_ = false; }
  if (!labels_.open()) return;
  std::sort(cur_pk_.begin(), cur_pk_.end());
  sink.begin_run("pk-" + labels_.label());
  int coll = schema_.vertex(labels_.label()).id;
  for (auto& [vid, docid] : cur_pk_) sink.put(pk_key(coll, vid), loc_value(coll, docid));
  sink.end_run();
  cur_pk_.clear();
  labels_.finish();
}

void Arango::add_edge(KvSink& sink, const std::string& etype, uint64_t src, uint64_t dst, const Value& props,
                      const std::string& src_label, const std::string& dst_label) {
  if (labels_.open()) flush_pk(sink);               // the first edge ends the vertex stream
  // both ends' collections come from the record: a vid does not name its collection
  if (src_label.empty() || dst_label.empty())
    throw std::runtime_error("arangodb ingest: edge " + etype + " " + hex16(src) + " -> " + hex16(dst) + " has no src / dst label");
  const std::string& sl = src_label;
  const std::string& dl = dst_label;
  n_edges_++;
  uint64_t eid = ++edgen_;
  int coll = schema_.edge(etype).id;
  // edge docs stream, one run per contiguous etype segment (segments of one etype overlap in
  // key range across segments; the sink batches overlapping runs)
  if (etype != cur_etype_) {
    if (doc_run_open_) sink.end_run();
    sink.begin_run("edocs-" + etype + "-" + std::to_string(eid));
    doc_run_open_ = true;
    cur_etype_ = etype;
  }
  sink.put(doc_key(edge_doc_coll(coll), eid),
           doc_encode(edoc_cols(etype), {{"_key", Value(hex16(eid))}, {"_from", Value(sl + "/" + hex16(src))},
                                         {"_to", Value(dl + "/" + hex16(dst))}}, props, etype));   // _key: as a vertex doc's
  eix_f_[etype].push_back({src, eid, schema_.vertex(dl).id, dst});
  eix_t_[etype].push_back({dst, eid, schema_.vertex(sl).id, src});
}

void Arango::finalize(KvSink& sink) {
  if (doc_run_open_ && !cur_etype_.empty()) { sink.end_run(); doc_run_open_ = false; cur_etype_.clear(); }
  flush_pk(sink);
  // every document has its primary-index entry (collection, _key) -> loc, edges too (as real arango): one run per etype
  for (auto& [etype, entries] : eix_f_) {
    int ecoll = edge_doc_coll(schema_.edge(etype).id);
    std::vector<uint64_t> eids;
    for (auto& e : entries) eids.push_back(e.eid);
    std::sort(eids.begin(), eids.end());
    sink.begin_run("epk-" + etype);
    for (uint64_t eid : eids) sink.put(pk_key(ecoll, eid), loc_value(ecoll, eid));
    sink.end_run();
  }
  // the covering edge indexes: one run per (etype, side), sorted by (vid, eid)
  for (auto* side : {&eix_f_, &eix_t_}) {
    bool from = side == &eix_f_;
    for (auto& [etype, entries] : *side) {
      std::sort(entries.begin(), entries.end(), [](const EdgeIx& a, const EdgeIx& b) {
        return a.vid != b.vid ? a.vid < b.vid : a.eid < b.eid;
      });
      int eid_c = schema_.edge(etype).id;
      std::map<int, std::string> by_id;
      for (auto& v : schema_.vertices()) by_id[v.id] = v.label;
      sink.begin_run(std::string(from ? "eix-f-" : "eix-t-") + etype);
      for (auto& e : entries)
        sink.put(edge_key(from, eid_c, e.vid, e.eid), vp_encode_str(by_id.at(e.other_coll) + "/" + hex16(e.other_vid)));
      sink.end_run();
    }
  }
  eix_f_.clear();
  eix_t_.clear();
}

json Arango::ingest_counts() const { return {{"vertices", n_vertices_}, {"edges", n_edges_}}; }

}  // namespace evalcpp::arangodb
