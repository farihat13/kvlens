// arangodb_engine: what the REAL ArangoDB engine does over arangodb's layout (Python formats/arangodb_engine.py), for
// the engine-fidelity check only (a variant of arangodb, so it lives here, never in a sibling dir). The store is
// arangodb's (with the engine's named documents); the engine's own choices are the ops below: the extra SEEK of the
// first traversal, path enumeration (uniqueEdges: "path", no vertex dedup), the filtered traversal's double read,
// the LIMIT-cut 1-hop traversal, and a document fetch per path step. It is never a compared format: never fed to the
// layout search, the pins or the paper's format comparisons, and absent from key_layouts() (engine_key_layouts()
// lists it). Reads only.
#pragma once
#include <mutex>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

#include "formats/arangodb/format.h"

namespace evalcpp::arangodb {

class ArangoEngine : public Arango {
 public:
  ArangoEngine(const Schema& schema, const Config& cfg);
  std::string name() const override { return "arangodb_engine"; }
  // the engine twin runs the engine's own plans: it does not advertise the fair format's scan of a collection's
  // documents (label_rows_one_run), so a capability-selected bundle run on its store keeps the body it had
  std::set<std::string> capabilities() const override {
    std::set<std::string> c = Arango::capabilities();
    c.erase("label_rows_one_run");
    return c;
  }
  bool has_kv_op(const std::string& op) const override;
  Value run_kv_op(const Step& step, const Args& a, StepExec& ex) override;
  // THE EXCEPTION to the opaque-id rule, confined to this twin: a real ArangoDB id is
  // "collection/key", so the engine holds a vertex's collection as its own storage fact. Our vid has no such part,
  // so the twin keeps it in a map from vid to collection, rebuilt from the pk index at store open (uncounted setup,
  // outside every step's ticker window). The fair format (format.h) has no such map: there the collection reaches a
  // step on the row or by a charged read. tests/check_vid_opaque.py lists these members as the one allowance.
  void warm(Store& store) override;
  const std::string& coll_of(uint64_t uid);                     // a vertex's collection; throws when unknown

 private:
  void ensure_coll(CountingStore& st);
  void note_vertex(uint64_t uid, const std::string& label) override;
  std::mutex coll_mutex_;
  std::unordered_map<uint64_t, std::string> coll_;   // uid -> collection (ingest and vertex inserts fill it)
  bool coll_loaded_ = false;

  // a docs_per_step `label`: a str names the collection; any list (BY_VID = empty, or candidates) means "the
  // collection the engine learned from the edge _id", recovered from the vid -> collection map (Python _collection)
  struct DocLabel { bool named = false; std::string coll; };
  static DocLabel doc_label(const Value& v, const char* op);
  // the path walk (Python _walk): level by level, one edge-index SEEK per (expanded vertex, side) and 1 NEXT per
  // entry, an edge never repeated within a path, no vertex dedup; the last landing of each scan is NOT declared a miss
  struct WalkStep { uint64_t src; uint64_t eid; uint64_t dst; int64_t depth; size_t path; };   // vids as uids
  struct Walk {
    std::vector<uint64_t> sources;                      // every vertex expanded FROM, in expansion order
    std::vector<WalkStep> steps;                        // every emission, one per path
    std::vector<std::vector<uint64_t>> path_verts;      // per path: its vertices, start included
    std::vector<std::vector<uint64_t>> path_eids;       // per path: its edge ids
  };
  Walk walk(CountingStore& st, const std::vector<uint64_t>& srcs, const std::string& etype, const std::string& dir,
            int64_t depth);
  // a document per vid, NO dedup: pk GET, then the doc GET only when the pk hit (Python docs_per_step)
  ValueList docs(CountingStore& st, const std::vector<uint64_t>& vids, const std::vector<std::string>& props,
                 const DocLabel& label);
  // an edge document's wanted props: 1 GET (its id is in the index key)
  Value edge_doc(CountingStore& st, const std::string& etype, uint64_t eid, const std::vector<std::string>& props);

  Value traversal_open(StepExec& ex, const Value& items, const std::string& etype);
  Value traverse_paths(StepExec& ex, const ValueList& srcs, const std::string& etype, const std::string& dir,
                       int64_t depth, bool pairs);
  Value traverse_filtered(const Step& step, const Args& a, StepExec& ex);
  Value traverse_filtered_limit(const Step& step, const Args& a, StepExec& ex);
  Value docs_per_step(StepExec& ex, const ValueList& vids, const std::vector<std::string>& props, const Value& label);
};

}  // namespace evalcpp::arangodb
