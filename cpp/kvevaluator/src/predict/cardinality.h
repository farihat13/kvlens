// Predict mode's cardinality layer: a bound plan and the statistics file in, each step's estimated rows and the
// sizes its rule worked out (StepCard, e.g. a top-k's crossed entries) out, the env the cost rules read; and each step's
// set facts (StepSet). Format-free: it walks the plan's steps, never a store. Each rule switch below has a fixture in
// tests/unit/check_predict_rules.py: a tiny graph where the rule's rows are the true count, and the switch off must
// miss it.
#pragma once
#include <functional>
#include <map>
#include <set>
#include <optional>
#include <string>
#include <vector>

#include "ir/plan.h"
#include "predict/estimate_input.h"   // Size
#include "predict/stats.h"

namespace evalcpp::predict {

// a step's estimated sizes: its rows, and what its rule worked out on the way. The sizes the cost rules read are named
// fields (unset when the step's op has none); the rest stay in `aux` by name (e.g. a top-k's tokens)
struct StepCard {
  double rows = 0;
  Size sd;               // a hop's distinct sources
  Size nv;               // a props read's distinct vids
  Size crossed;          // a top-k: the entries it crosses
  Size nprobe;           // a label probe: the inputs that probe
  Size steps, edges;     // a chase: the vertices and links it walks
  Size nvia, nvnbrs, nothers;           // read_edge_exists: via sources, their neighbours, the anchors
  std::vector<Size> eown, eprobe;       // read_edge_exists, per read: the lists it owns, the pairs it checks
  std::map<std::string, double> aux;   // no cost rule reads these today
};
using Env = std::map<std::string, StepCard>;


// what predict knows about the set a step outputs beyond its rows, filled once by the cardinality walk (the cost
// rules read it, not the plan). Each hop field names a read_neighbors step, followed back through the ops that keep
// rows as they are (dedup, exclude, pluck but not of "src", rename, filter, filter_in, top_k); "" when none made it
struct StepSet {
  std::string hop;            // the hop that made the set
  std::string hop_via_props;  // the same, also through a vertex props read but not a concat (a subset of the hop's
                              // targets, so the set sits where they sit: Chunked locality)
  std::string hop_via_concat; // the same, also through a concat's first side but not a props read (the set's far-end
                              // labels: far_labels)
  std::string label;          // set_label: the set's one label, "" when it may hold several
};
using StepSets = std::map<std::string, StepSet>;

// The rules that improve on the rough model, each a named switch (--rules-off turns some off). Every prediction runs
// with all on; the accuracy check reports each one's effect. A "why" line gives the miss the rule fixed when it was
// added (sf0.1 unless it says SF3; "measured" = the run's own rows).
struct Rules {
  // a concat of two sets reached the same way keeps how they were reached (off: dropped at every concat)
  // why: lets frontier_inclusion apply to friends + friends-of-friends (a concat that dropped it costed them at the
  // mean)
  bool concat_keeps_via = true;
  // a hop from a set reached through an edge prices its sources' degree from the via histogram
  // (DatasetStats::mean_given_via) instead of the population mean times a linear coverage damp
  // why: messages of friends + fofs (IC3, IC5, IC9) were 0.71 of the measured rows, now 1.05-1.10; IC5's memberships
  // 0.57 -> 1.12
  bool frontier_inclusion = true;
  // a dedup of rows reached through an edge keeps the expected distinct vertices (DatasetStats::distinct_given_via)
  // instead of every row
  // why: the fof set at tier 2: 578 without it, 387 with it (measured 451)
  bool dedup_by_degree = true;
  // a predicate filter keeps how its rows were reached, for dedup_by_degree only: the rows are a subset of the same
  // edges, but the predicate may select by activity, so frontier_inclusion does not apply past it (off: dropped)
  // why: IC5's forums at tier 2: 30,941 without it, 7,833 with it (measured 5,861)
  bool filter_keeps_via = true;
  // a date window's rows from the per-day histogram (DatasetStats::in_window) instead of a uniform share of the span
  // why: IC3's date window: 0.70-1.52 of the measured rows over 5 params, exact with it
  bool date_histogram = true;
  // a props read de-duplicates its vids (the run's distinct_vids): its vertices, and its rows, are the expected distinct
  // count of its input when the input was reached through an edge (DatasetStats::distinct_given_via), not every row
  // why: the run reads each vid once; no step's verdict changed, 40 IC4 steps moved by under 1%
  bool props_distinct = true;
  // a filter that requires a prop not null, read for one label from the far end of a hop (set_label), keeps that
  // label's share of the hop's far end (DatasetStats::far_label_share) instead of half its rows
  // why: with date_filter, IC4 at SF3 (tier 2 + anchor, Condensed): old_tags / win_tags sources 5,200 / 5,200 -> 747 /
  // 144 (measured 447 / 173)
  bool notnull_label = true;
  // a filter that bounds a date prop of a one-label set (set_label) keeps the label's share of vertices in the window:
  // from the per-day histogram, else (both bounds) the window's share of the date axis's span; one bound and no histogram:
  // half its rows, as before
  // why: with notnull_label (above); and a message's length is skewed (89% of posts have length 0): an even spread over
  // [min, max] missed BI12's window by 120x
  bool date_filter = true;
  // a filter_in keeps the allowed set's share of the tested field's domain (|allowed| / the vertices the field can
  // name: Walker::field_domain) instead of half its rows, when the allowed set shares no step with the rows; half
  // when it does (a semi-join back onto the rows' own values) or when the walk back finds no domain
  // why: a filter_in kept half its rows whatever the allowed set: BI's bi18 and bi8 at SF3 now keep the persons
  // interested in one tag, not half of every interest edge
  bool filter_in_share = true;
  // a dedup keeps at most the vertices its values can name (Walker::field_domain), so no set of distinct vertices
  // outgrows its labels' population
  // why: a dedup of rows from a join kept every row: BI's bi14 at SF3 read the messages of 7.01e6 distinct persons, of
  // 24,328
  bool dedup_domain_cap = true;
  // a plan's size_biased hint costs a hop at a neighbour's degree only when its sources were reached through that
  // edge type (Walker::reached_through); other hops of the type take the population's degree (off: every hop of it)
  // why: BI's bi14 and bi11 at SF3 read a country's persons through knows at 188 friends each, against a mean of 53.7
  bool size_biased_via = true;
  // field_domain walks a concat chain up to 64 deep (off: 8), and a concat's domain is at most its sides' labels'
  // population together when both sides name their labels (off: the sum of its sides' domains, which counts a label
  // the sides share once per side)
  bool concat_domain = true;
  // an exclude whose removed set shares no step with its items removes the items it holds by chance, both sets taken as
  // drawn from their labels (Walker::field_domain's labels, type_counts): removed on the shared labels / the items'
  // labels' population; at least items - removed (off: items - removed, which assumes the removed set holds only items)
  bool exclude_share = true;
  // a group step (group_count, group_sum, group_top, a keyed group_agg) keeps at most its keys' domains multiplied
  // (Walker::field_domain), and one row at least when a row comes in (off: half its rows, which rounds one row to 0)
  bool group_key_cap = true;
  // a hop from a set of distinct sources (Walker::distinct_set: a dedup, a label read, ...) returns at most its edge
  // type's edges (x2 for both directions); off: no cap on a degree times its sources
  bool hop_edge_cap = true;
  // an index read on an indexed property's value keeps that value's rows (DatasetStats::value_rows: its count when it is
  // one of the most common values, else the rest spread over the other values) instead of the label's population over
  // its distinct values (off: rows / ndv)
  bool index_value_rows = true;
  // a filter's eq / in / ne of an indexed property against literals, on a one-label set (set_label), keeps that label's
  // share of the values' rows (DatasetStats::value_rows over its population); a notnull on the same field is implied.
  // Off (or another conjunct the rules do not read): half its rows, as before
  bool filter_value_share = true;
  // a hop whose rows every consumer reads as one label L (a hop from L's side of its edge type, a filter_in testing a
  // hop to L against them; through the ops that keep rows, joins aside), while its far end holds L and other labels,
  // keeps L's share of the far end's edges (DatasetStats::far_label_share: the owners rows). LDBC's Place ->
  // located-in hop whose rows go on to knows (bi11, bi14). Off: every far label's edges. The premise: the share stands
  // in for the far labels the hop's sources do not hold (LDBC's cities hold persons, not messages). The hop's own rows
  // are scaled, so where the sources' lists really mix labels (a person's posts and comments) its edges read are under
  // by the share
  // why: LDBC's cities -> located-in hop going on to knows was costed at the mean over every Place, messages located in
  // countries included
  bool far_label_consumer = true;
  // a hop straight from an equality index read (read_index_eq with a value, the hop's source side its label) reaches
  // that value's own edges (DatasetStats::entry_degree, top_degrees: a listed value's, else the rest's spread), not
  // the label's mean degree. LDBC's Country -> its cities, a Tag -> its messages. The first hop only
  bool entry_value_degree = true;
  // far_label_consumer's consumers include a props read naming a label (read_vertex_props with label L: the plan reads
  // the rows as L). A filter on that read that needs a prop not null (notnull_label) then keeps them all: the hop
  // already holds L's share, applied once
  bool props_read_consumer = true;
  // a filter's eq / in / ne on several indexed props of one label (filter_value_share) keeps the smallest field's share,
  // the conjunction's upper bound: the fields assumed correlated (JOB's info with info_type_id), not independent
  bool value_share_correlated = true;
  static const std::vector<std::string>& names();   // the switch names, for --rules
  bool set(const std::string& name, bool on);       // false: no switch by that name
};

// a step's arguments with params and lets replaced by their values (a ref stays {"ref": step}): the step as predict
// mode sees it, with no run
json bound_args(const json& args, const Bindings& b);

// the plan's anchor: the first read_vertex_by_id whose bound vid is one vertex (a string), as (step name, vid). The
// cardinality layer and the cost rules both take it from here
std::optional<std::pair<std::string, std::string>> anchor_step(const Plan& plan);

// a step as the label walk sees it: its op and an object holding its arguments; nullopt for a name that is no step
using StepLookup = std::function<std::optional<std::pair<std::string, const json*>>(const std::string&)>;

// the one label of the set an argument names, walking back through the ops that keep rows as they are (dedup,
// exclude, pluck but not of "src", rename, filter, filter_in, top_k) and through props reads: a props read of one label
// whose prop a later filter requires not null (rows of other labels read null and are dropped), else the hop that
// made the set when it filters to one label. "" when neither says (the set may hold every far-end label).
// `required`: fields already required not null after `arg` (a filter's own). `props_step`: the props read that fixed
// the label, when one did. Shared by the cardinality rules and the cost rules' Chunked locality (cli/predict.cc)
std::string set_label(const json& arg, const StepLookup& step, std::set<std::string> required = {},
                      std::string* props_step = nullptr);
// the fields a filter predicate requires not null: {"notnull": {"field": f}}, alone or inside an "and"
void notnull_fields(const json& pred, std::set<std::string>& out);

// nullopt when a step's op has no rule (the whole query is then not estimated); `why` names the op
std::optional<Env> estimate_cardinalities(const Plan& plan, const DatasetStats& stats, const Rules& rules,
                                          std::string* why = nullptr);

// every step's StepSet, in plan order (a step's inputs come first). Apart from the estimate: a body whose rows the
// layer refuses still has its sets (level true_terms reads them with its true counts)
StepSets step_sets(const Plan& plan);

// the far end's labels of a hop (a set's hop_via_concat: its etype and dir arguments), by share of its edges; empty
// when no edge has that end. Reads the statistics' owners, refused by name when the file has none, so only a step
// that needs the shares calls it
std::map<std::string, double> far_labels(const json& hop, const DatasetStats& stats);

}  // namespace evalcpp::predict
