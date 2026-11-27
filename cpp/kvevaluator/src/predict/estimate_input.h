// What a KV step's estimate is priced from, beside the statistics and the tally (formats/meta/predict.cc, Estimate):
// the step's input sizes and what is known about its set. The caller fills it per level: level true_terms from the traced run
// and its truth card, levels predicted and predicted_anchor from the cardinality layer's env and StepSets (cli/predict.cc). A size is unset until the
// caller gives it; reading an unset one (need) refuses the step by name, never prices it as 0.
#pragma once
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "predict/terms.h"
#include "value/value.h"   // json

namespace evalcpp::predict {

using Size = std::optional<double>;   // unset: the caller gave no value

// a size the step needs: refused by name when unset
inline double need(const Size& v, const char* name) {
  if (!v) throw std::runtime_error(std::string("predict: no estimate input '") + name + "'");
  return *v;
}

struct EstimateInput {
  Size n;                 // the distinct inputs (a hop's sources, a props read's vids)
  Size entries;           // a hop's output rows
  Size matches;           // an index walk's matches
  Size crossed, winners;  // a top-k: the entries it crosses, the rows it keeps
  Size probes, walk;      // a label probe: the inputs that probe, and whether the membership is walked (0 or 1)
  Size nodes, links;      // a closure or a chase: the vertices and edges it reads
  Size rows, nvia, nvnbrs, nothers;   // read_edge_exists: candidates, via sources, their neighbours, the anchors
  std::vector<Size> eown, eprobe;     // read_edge_exists, per read: the lists it owns, the (candidate, anchor) pairs
  // level true_terms: (label id, the distinct inputs carrying it), "?" for no label carried, in the order the run's inputs
  // first show each (the estimate sums over it in this order)
  std::optional<std::vector<std::pair<std::string, double>>> label_mix;
  // level predicted: the far end's labels of the hop that made the set, by share of its edges (predict::far_labels)
  std::optional<std::map<std::string, double>> label_share;
  // the hop that made the set and its distinct sources (owners), and the set's one label if any: Chunked locality
  struct Producer {
    std::string etype, dir, label;
    double owners = 0;
  };
  std::optional<Producer> producer;
  // set: the step reads from the plan's anchor, this vid (a parameter, so never at level predicted)
  std::optional<std::string> anchor_vid;
  // a level that takes terms from the truth card (predict/terms.h kLevels): the step's terms of the kinds it takes
  // (terms_for); the estimate reads a term given here in place of its statistic. `level`: which level, so a term of a kind
  // it takes that no item filled is counted (terms_from_statistics)
  std::optional<Terms> true_terms;
  const LevelDef* level = nullptr;
  // a write: the vertices the request's earlier steps created, and the edges they added ("vid|etype|dir" -> edges)
  std::vector<std::string> created;
  std::map<std::string, double> added;
};

}  // namespace evalcpp::predict
