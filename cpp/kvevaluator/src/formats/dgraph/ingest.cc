// Dgraph's bulk ingest: scalars, adjacency postings, indexes (the incremental writes are in writes.cc).
#include "formats/dgraph/format.h"
#include "codec/decode_counts.h"

#include <algorithm>
#include <cstring>
#include <numeric>
#include <optional>
#include <stdexcept>

#include "codec/prim.h"
#include "codec/rowv2.h"
#include "codec/varint.h"
#include "formats/common.h"
#include "formats/dgraph/keys.h"
#include "codec/posting.h"
#include "index/sorted_index.h"
#include "store/unit_value.h"
#include "util/uidset.h"
#include "util/vid.h"

namespace evalcpp::dgraph {

// --- ingest -----------------------------------------------------------------------------------

void Dgraph::add_vertex(KvSink& sink, uint64_t uid, const std::string& label, const Value& props) {
  if (labels_.changes(label)) {
    flush_scalars(sink);
    labels_.start(label);
  }
  n_vertices_++;
  cur_scalars_["dgraph.type"].emplace_back(uid, prim::encode_leaf(Value::list({Value(label)})));   // the per-vertex type record
  if (schema_.has_type_index(label)) index_[{"dgraph.type", label}].push_back(uid);             // the type-enumeration posting
  for (auto& [name, val] : props.as_row()) {
    if (name == "id") continue;                            // the vid IS the uid: derived, not stored
    if (val.is_null()) continue;
    cur_scalars_[data_pred(label, name)].emplace_back(uid, prim::encode_leaf(val));   // per label when the schema says so
    if (!index_entry(label, name)) continue;
    if (indexed_.count(name)) index_[{index_pred(label, name), index_term(label, name, val)}].push_back(uid);   // per type when type-scoped
    else if (date_preds_.count(name)) {
      if (is_key_sorted(label, name))   // one key-only entry per vertex through the sink's index lane (SstSink
                                        // buffers those keys until finish(): ~80 B per dated vertex -- sf3 ~0.7 GB)
        sink.put_index(schema_.index_decl(label, name).id, term_key(index_pred(label, name), key_sorted_term(val.as_int(), uid)));
      else if (cfg_.chunked() && type_scoped_.count({label, name}))   // the per-type day walks read one KV per token
        throw std::runtime_error("dgraph ingest: the type-scoped day-token date index on " + label + "." + name +
                                 " is built on the unbounded store only (no chunk_target)");
      else if (is_value_sorted(label, name))   // the bucket keeps each entry's ms: (ms, uid), date-ordered at flush
        index_vs_[{index_pred(label, name), day_token(val.as_int())}].push_back({val.as_int(), uid});
      else index_[{index_pred(label, name), day_token(val.as_int())}].push_back(uid);   // @index(day), per type when scoped
    }
  }
}

// one label's scalar postings: a run per predicate, uid-ascending (the sink batches the runs of
// one predicate across labels, whose uids may interleave)
void Dgraph::flush_scalars(KvSink& sink) {
  for (auto& [pred, cells] : cur_scalars_) {
    std::stable_sort(cells.begin(), cells.end(), [](auto& a, auto& b) { return a.first < b.first; });
    sink.begin_run("data-" + pred + "-" + labels_.label());
    for (size_t i = 0; i < cells.size(); i++) {
      if (i > 0 && cells[i].first == cells[i - 1].first)
        throw std::runtime_error("dgraph ingest: duplicate value for predicate " + pred + " at " + hex16(cells[i].first));
      sink.put(uid_key(pred, kData, cells[i].first), cells[i].second);
    }
    sink.end_run();
  }
  cur_scalars_.clear();
  labels_.finish();
}

void Dgraph::add_edge(KvSink& sink, const std::string& etype, uint64_t src, uint64_t dst, const Value& props,
                      const std::string&, const std::string&) {
  flush_scalars(sink);                                     // the first edge ends the vertex stream
  const std::vector<ColDecl>* cols = nullptr;
  {
    auto it = ecols_.find(etype);
    if (it == ecols_.end()) throw std::runtime_error("dgraph ingest: unknown etype '" + etype + "'");
    cols = &it->second;
  }
  edges_[etype].append(src, dst);
  if (!cols->empty()) {                                    // the facet row, aligned with the pair
    auto& buf = efacets_[etype];
    if (!buf) buf = std::make_unique<RowBuffer>(*cols);
    buf->append(edges_[etype].size() - 1, props.is_row() ? props : Value::row({}), 0);
  }
  n_edges_++;
}

// one edge's facet bytes: schema = its RowV2 row over the declared columns ("" for a prop-less
// etype, meta's Condensed-list rule); named = the prim dict of its present columns ({} when none)
std::string Dgraph::facet_bytes(const posting::AdjCodec& codec, const std::vector<ColDecl>& cols,
                                const RowBuffer* buf, uint32_t row) {
  if (codec.is_schema()) {
    if (cols.empty()) return "";
    if (!buf) throw std::runtime_error("dgraph ingest: an etype with declared props has no facet buffer");
    std::vector<uint32_t> one = {row};
    BufferRows view(*buf, one, 0, 1);
    return rowv2_encode(cols, view, 0, codec.row_timestamp);
  }
  RowFields f;
  if (buf)
    for (size_t c = 0; c < cols.size(); c++) {
      if (!buf->present(row, c)) continue;
      if (cols[c].multi) throw std::runtime_error("dgraph ingest: multi-valued facets are not modeled");
      if (stringlike(cols[c])) f.emplace_back(cols[c].name, string_value(cols[c], buf->str_at(row, c)));
      else f.emplace_back(cols[c].name, Value(buf->int_at(row, c)));
    }
  std::string out;
  prim::encode(out, Value::row(std::move(f)));
  return out;
}

void Dgraph::finalize(KvSink& sink) {
  flush_scalars(sink);
  flush_adjacency(sink);
  flush_indexes(sink);
}

// adjacency postings: one blob per (etype, kind, vertex), uid-ascending (real dgraph's
// invariant). Virtual: the hybrid's flush orders a dated etype's postings by date and
// injects the inline copy -- everything else here is shared as-is.
void Dgraph::flush_adjacency(KvSink& sink) {
  for (auto& [etype, eb] : edges_) {
    const std::vector<ColDecl>& cols = ecols(etype);
    const RowBuffer* fbuf = efacets_.count(etype) ? efacets_[etype].get() : nullptr;
    for (char kind : {kData, kReverse}) {
      if (kind == kReverse && !reverse_.count(etype)) continue;
      bool rev = kind == kReverse;
      posting::AdjCodec codec = adj_codec(etype, kind);
      std::vector<uint32_t> perm = eb.sorted_perm(rev);
      sink.begin_run("adj-" + etype + (rev ? "-R" : "-D"));
      size_t i = 0;
      while (i < perm.size()) {
        uint64_t owner = rev ? eb.dst[perm[i]] : eb.src[perm[i]];
        std::vector<uint64_t> others;
        std::vector<std::string> fb;
        while (i < perm.size() && (rev ? eb.dst[perm[i]] : eb.src[perm[i]]) == owner) {
          others.push_back(rev ? eb.src[perm[i]] : eb.dst[perm[i]]);
          fb.push_back(facet_bytes(codec, cols, fbuf, perm[i]));
          i++;
        }
        std::vector<std::string_view> fv(fb.begin(), fb.end());
        std::string key = uid_key(etype, kind, owner);
        if (!cfg_.chunked()) sink.put(key, posting::encode_adjacency_bytes(others, fv, true, codec));   // ONE KV, whatever the degree
        else unit::write(sink, key, others.size(), [&](size_t lo, size_t hi) {       // dgraph-c64k: parts over the target
          std::vector<uint64_t> d(others.begin() + lo, others.begin() + hi);
          std::vector<std::string_view> f(fv.begin() + lo, fv.begin() + hi);
          return posting::encode_adjacency_bytes(d, f, true, codec);
        }, cfg_.chunk_target, [&](size_t e) { return others[e]; });
      }
      sink.end_run();
    }
  }
  edges_.clear();
  efacets_.clear();
}

// index postings: one run per predicate, terms in byte order, uids ascending in the value
void Dgraph::flush_indexes(KvSink& sink) {
  std::string cur_pred;
  for (auto& [key, uids] : index_) {
    if (key.first != cur_pred) {
      if (!cur_pred.empty()) sink.end_run();
      cur_pred = key.first;
      sink.begin_run("index-" + cur_pred);
    }
    std::sort(uids.begin(), uids.end());   // encode_posting throws on a duplicate, as Python's codec does
    if (!cfg_.chunked()) { sink.put(term_key(key.first, key.second), encode_posting(uids)); continue; }
    // dgraph-c64k: a term over the target splits into parts `term | 0x00 | start`, told apart
    // from sibling terms by that NUL (unit_value's key contract: str terms are not prefix-free
    // -- "Tag" / "TagClass" -- but no term carries a NUL). A term that does is refused.
    if (key.second.find('\0') != std::string::npos)
      throw std::runtime_error("dgraph ingest: an index term of " + key.first + " contains a NUL byte; chunked terms cannot be told from parts");
    unit::write(sink, term_key(key.first, key.second), uids.size(), [&](size_t lo, size_t hi) {
      return encode_posting(std::vector<uint64_t>(uids.begin() + lo, uids.begin() + hi));
    }, cfg_.chunk_target, [&](size_t e) { return uids[e]; });
  }
  if (!cur_pred.empty()) sink.end_run();
  index_.clear();
  // value-sorted buckets: their own predicates (never in index_), so their runs are disjoint from the ones above
  cur_pred.clear();
  for (auto& [key, entries] : index_vs_) {
    if (key.first != cur_pred) {
      if (!cur_pred.empty()) sink.end_run();
      cur_pred = key.first;
      sink.begin_run("index-vs-" + cur_pred);
    }
    sink.put(term_key(key.first, key.second), encode_value_sorted(entries));
  }
  if (!cur_pred.empty()) sink.end_run();
  index_vs_.clear();
}

std::string Dgraph::encode_value_sorted(const std::vector<std::pair<int64_t, uint64_t>>& ms_uid) {
  std::vector<std::pair<int64_t, uint64_t>> e = ms_uid;   // date-DESCENDING, uid the tie-break: deterministic (Python)
  std::sort(e.begin(), e.end(), [](auto& a, auto& b) { return a.first != b.first ? a.first > b.first : a.second < b.second; });
  std::string out;
  put_uvarint(out, e.size());
  for (auto& [ms, u] : e) put_u64be(out, u);
  for (auto& [ms, u] : e) prim::encode(out, Value(ms));
  return out;
}

std::vector<std::pair<uint64_t, int64_t>> Dgraph::decode_value_sorted(std::string_view raw) {
  uint64_t bytes0 = decode_counts().bytes;
  size_t i = 0;
  uint64_t n = get_uvarint(raw, i);
  std::vector<std::pair<uint64_t, int64_t>> out(n);
  for (uint64_t j = 0; j < n; j++, i += 8) out[j].first = get_u64be(raw, i);
  for (uint64_t j = 0; j < n; j++) out[j].second = prim::decode(raw, i).as_int();
  if (i != raw.size()) throw std::runtime_error("dgraph: a value-sorted bucket has trailing bytes");
  DecodeCounts& d = decode_counts();    // codec/decode_counts.h: one posting, a field per uid (each ms counted itself)
  d.postings++; d.fields += n; d.bytes = bytes0 + raw.size();
  return out;
}

json Dgraph::ingest_counts() const { return {{"vertices", n_vertices_}, {"edges", n_edges_}}; }

}  // namespace evalcpp::dgraph
