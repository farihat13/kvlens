// Predict mode's terms: every value an estimate formula (formats/meta/predict.cc, Estimate) reads besides the step's plan,
// and the step's input counts (estimate_input.h: rows, nothers, ... at the list's end), named once here. A term is a count or a mean size the formula takes from statistics;
// everything else in a formula (balls in bins, a landing charged a record's key, a lane it leaves at 0) is the formula.
// Each term has a kind (a count, a size, the chunks touched); each level of `predict --accuracy` (kLevels) takes some kinds
// from the step's truth card (Meta::predict_step_truth's terms_out) and the rest from the statistics: a count as the card
// counts it, a size as the card's sum over the items of the term's kind the call touched, over their count. A term of a
// kind the level takes from the card that no item filled keeps its statistic, and the row says how many did
// (terms_from_statistics). A term is keyed "<name>@<part>"; the part names the
// store part (adj:<etype>:<dir>, props:<Label>[:<attr or group>], index:<Label>[.<prop>]) or the Chunked family
// (<etype>:<dir>, props:<Label>).
#pragma once
#include <algorithm>
#include <cctype>
#include <map>
#include <stdexcept>
#include <string>

namespace evalcpp::predict {

// a term's kind: what a level takes from the truth card by (kLevels)
enum class TermKind { count, size, chunks };

struct TermDef {
  const char* name;
  TermKind kind;
  const char* meaning;
  const char* statistics;   // its source at level predicted
  const char* truth;        // its source at a level that takes its kind from the truth card
  bool per_read = false;    // keyed per read of an edge check: "<name><i>@" (eown0, eprobe1)
};

// the one list (the predict-mode design doc's "4. The statistics" of each operator names these)
inline const TermDef kTerms[] = {
    {"present", TermKind::count, "the probes of a direction that find a list (a write's Condensed RMW: its owner has a list)",
     "owners with a list / the label's vertices (stats owners), or the anchor's own degree; a write: the degree statistics "
     "(degree_by_far_label, degree_hist)", "HopDirCard.present; a chase's FirstCard.hits; a write: the RMW found the list"},
    {"entries", TermKind::count, "the entries a direction's lists hold", "the step's entries split evenly over its directions, or the "
     "anchor's own degree", "HopDirCard.entries"},
    {"adj_key_mean", TermKind::size, "an adjacency record's key bytes (Split; a landing is charged it)", "the part's key bytes / keys "
     "(tally)", "the edge key's width, when a record was read"},
    {"adj_value_mean", TermKind::size, "a record's value bytes (Split), an entry's list bytes (Condensed); a write: the bytes "
     "an entry adds to a list or a chunk", "the part's value bytes / keys (Split) or / edges (Condensed, Chunked) (tally, stats "
     "edge totals)", "HopDirCard.value_bytes / entries; a write: the list read's bytes / its entries, or a new list's bytes "
     "(Condensed), the rewritten chunk's growth (Chunked)"},
    {"list_value_mean", TermKind::size, "a Condensed list's value bytes (a chase or closure)", "the part's value bytes / keys (tally)",
     "HopDirCard.value_bytes / present"},
    {"list_entries_mean", TermKind::count, "a Condensed list's entries (a chase, a closure, an edge check; a write's RMW: its "
     "owner's degree)", "the edge type's edges / its lists (stats edge totals, owners; tally keys); a write: the degree "
     "statistics", "HopDirCard.entries / present; a write: the list (or split part) the RMW read"},
    {"chunk_key_mean", TermKind::size, "a Chunked chunk's key bytes", "the family's key bytes / chunks (tally)",
     "the charged chunks' key bytes / their count (the step's landings, replayed)"},
    {"chunk_value_mean", TermKind::size, "a Chunked chunk's value bytes (a write: the chunk it rewrites)",
     "the family's value bytes / chunks (tally)", "the charged chunks' value bytes / their count (the step's landings, "
     "replayed; a write: the chunks its RMW read)"},
    {"chunk_last_share", TermKind::count, "of a write's chunk RMWs in a family, those that land on its last chunk",
     "1 for a vertex created in the request when no vid group above its own (the schema's vid_tags) owns lists in the "
     "family (the statistics' owners), else 0", "the chunks the RMWs read that are the family's final key"},
    {"last_chunk_value", TermKind::size, "a Chunked family's last chunk's value bytes (where an insert of a new vertex lands)",
     "the tally's chunks.<family>.last_chunk_value_bytes (a full tally: its own; a sampled family: assumed, the pack "
     "target x last_chunk_fill)", "the family's last chunk as the write finds it (the request's earlier writes in)"},
    {"props_n", TermKind::count, "the vertices a props read reads under one label", "the step's inputs by the label's share (label mix, "
     "far-label share, or type counts)", "PropsCard.asked (Split: the most of any attr), the family's probes (Chunked)"},
    {"prop_hit_share", TermKind::count, "of the vertices asked, those holding the prop (Split)", "the vertices holding it / the label's "
     "(stats prop content)", "PropsCard.hits / asked"},
    {"prop_value_mean", TermKind::size, "a found prop's value bytes (Split)", "the prop's bytes / its vertices (stats prop content)",
     "PropsCard.value_bytes / hits"},
    {"row_value_mean", TermKind::size, "a vertex row's value bytes (Condensed props, a Condensed existence GET; a write: the row "
     "a Chunked insert adds)", "the part's value bytes / the label's vertices (tally, stats type counts)",
     "PropsCard.value_bytes / hits; a write: the rewritten chunk's growth"},
    {"label_hit_share", TermKind::count, "of the probes of a label probe, those that hold the label", "the label's vertices / all "
     "vertices (stats type counts)", "ExistCard hits / probes; Condensed GETs hits / asked"},
    {"props_key_mean", TermKind::size, "a props record's key bytes (Split existence probe)", "the part's key bytes / keys (tally)",
     "ExistCard.hit_key_bytes / hits"},
    {"index_key_mean", TermKind::size, "an index entry's key bytes", "the index part's key bytes / keys (tally)",
     "WalkCard.match_key_bytes / matches; a top-k: its walk's scan key bytes / NEXTs"},
    {"index_matches", TermKind::count, "an index range's matches under one label", "the step's matches by the labels' type counts",
     "the label's walk's matches"},
    {"chunks_touched", TermKind::chunks, "the chunks of a Chunked family a read charges (the formula's t)", "balls in "
     "bins over the family's chunks (tally), or the producer's locality (predict/landing.h)", "the step's landings on the "
     "family, replayed: the chunks charged (SEEKs that found a chunk)"},
    // the step's counts (key "<name>@", no part): the estimate's inputs (estimate_input.h), filled from the run's traced
    // arguments and the truth card at every level that takes counts from the truth card
    {"rows", TermKind::count, "an edge check's candidates", "the cardinality layer", "the candidates' distinct vids"},
    {"nothers", TermKind::count, "an edge check's anchors", "the cardinality layer", "the anchors' distinct vids"},
    {"nvia", TermKind::count, "an edge check's via walk: its distinct sources", "the cardinality layer", "the via srcs"},
    {"nvnbrs", TermKind::count, "an edge check's via walk: its entries", "the cardinality layer", "the walk's entries"},
    {"eown", TermKind::count, "an edge check's read i (key eown<i>@): the owners whose lists it reads",
     "the cardinality layer", "the owners read", true},
    {"eprobe", TermKind::count, "an edge check's read i (key eprobe<i>@): its Split GETs", "the cardinality layer",
     "the GETs made", true},
    {"matches", TermKind::count, "an index walk's matches", "the cardinality layer", "WalkCard.matches"},
    {"nodes", TermKind::count, "a chase's or closure's nodes", "the cardinality layer", "the walk's probes"},
    {"links", TermKind::count, "a chase's or closure's nodes with a next", "the cardinality layer", "the lists found"},
    {"crossed", TermKind::count, "a top-k walk's index entries crossed", "the cardinality layer", "the walk's NEXTs"},
    {"winners", TermKind::count, "a top-k walk's winners", "the cardinality layer", "the step's output vids"},
};

// the levels `predict --accuracy` costs each KV step at, in their row order: which term kinds come from the step's
// truth card (every other term from the statistics). A level with `card` false takes its counts from the cardinality
// layer (inputs_b), with or without the anchor statistics. A write step has no cardinality layer (its vertices come from
// the params at every level) and no chunks-touched term, so on a write true_chunks = true_terms. Each level's error against the one before it, on a workload
// total, is that kind's share: true_chunks -> true_terms the chunks touched, -> true_counts the sizes, -> predicted the
// counts, -> predicted_sampled the owner sample, -> predicted_pick the plan choice (the signed differences, from measured,
// add up to predicted_pick - measured)
struct LevelDef {
  const char* name;
  bool card;          // counts from the truth card (else the cardinality layer)
  bool sizes;         // the size terms from the truth card
  bool chunks;        // the chunks touched from the truth card
  bool anchor;        // the anchor statistics (a statistics level)
  bool chain;         // in the chain measured -> ... -> predicted whose steps add up (predicted_anchor is beside it)
  const char* measures;   // what this level minus the chain's level before it measures (the first: minus measured)
  const char* meaning;
  // made by `kvcost predict` at the default tier from the tallies counted on the paper's sample of the owners (its
  // predicted capture, read by the analysis), not by `predict --accuracy`: at the body the run picked, or (`pick`) at
  // each layout's own predicted pick -- the prediction the paper reports
  bool sampled = false;
  bool pick = false;
};
inline const LevelDef kLevels[] = {
    {"true_chunks", true, true, true, false, true,
     "what no formula models (missed SEEKs, a chunk read again; a write: the chunk a new owner lands in)",
     "every term from the truth card, the chunks touched too"},
    {"true_terms", true, true, false, false, true, "the chunks touched (the formula's t)",
     "every term but the chunks touched from the truth card"},
    {"true_counts", true, false, false, false, true, "the sizes (record, list, prop, index entry and chunk means)",
     "the counts from the truth card; sizes and chunks touched from the statistics"},
    {"predicted", false, false, false, false, true, "the counts (the cardinality layer)",
     "every input from the statistics (the default tier) and the tallies of every owner, at the body the run picked"},
    {"predicted_anchor", false, false, false, true, false, "", "predicted plus the anchor statistics"},
    {"predicted_sampled", false, false, false, false, true, "the owner sample (the tallies counted on a sample)",
     "predicted from the tallies counted on the paper's sample of the owners", true, false},
    {"predicted_pick", false, false, false, false, true,
     "the plan choice: each layout's own predicted pick, the body its predicted cost ranks cheapest (so at most "
     "predicted_sampled on the op-byte cost)",
     "predicted_sampled at each layout's own predicted pick: on the read items the paper's prediction (a write plan has one "
     "body)", true, true},
};

// a term kind a level takes from the truth card
inline bool from_card(const LevelDef& l, TermKind k) {
  return l.card && (k == TermKind::count || (k == TermKind::size && l.sizes) || (k == TermKind::chunks && l.chunks));
}

inline const TermDef& term_def(const std::string& key_name) {
  // a per-read count (eown0, eprobe1) is its name with the read's index
  std::string name = key_name;
  while (!name.empty() && std::isdigit(static_cast<unsigned char>(name.back()))) name.pop_back();
  bool indexed = name.size() != key_name.size();
  auto it = std::find_if(std::begin(kTerms), std::end(kTerms), [&](const TermDef& d) { return name == d.name; });
  if (it == std::end(kTerms) || it->per_read != indexed)
    throw std::runtime_error("predict: no term '" + key_name + "' in predict/terms.h");
  return *it;
}

inline std::string term_key(const std::string& name, const std::string& part) { return name + "@" + part; }

// a step's term values, "<name>@<part>" -> value
using Terms = std::map<std::string, double>;

// the level's own view of a step's truth terms: those of a kind it takes from the truth card
inline Terms terms_for(const LevelDef& l, const Terms& t) {
  Terms out;
  for (auto& [k, v] : t)
    if (from_card(l, term_def(k.substr(0, k.find('@'))).kind)) out[k] = v;
  return out;
}

}  // namespace evalcpp::predict
