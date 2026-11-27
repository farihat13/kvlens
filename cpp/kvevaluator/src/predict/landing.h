// Predict mode's landing layer: how many chunks of a Chunked family a step's probes touch, from statistics alone (the
// closed form). The run's own count comes from the landing replay (exec/replay.h); predict --accuracy compares the two
// (level true_terms: the closed form at the step's true terms). Format-free: it reads a tally's locality rows (cli/tally.cc), never a store.
#pragma once
#include <string>

#include "predict/estimate_input.h"   // EstimateInput::Producer
#include "value/value.h"              // json

namespace evalcpp::predict {

// balls in bins: n probes spread uniformly over B chunks touch B (1 - (1 - 1/B)^n) of them
double balls(double B, double n);

// the chunks of `family` (C of them) n vertices touch: balls in bins, unless `producer` (the hop that made the set:
// etype, dir, owners P, and the set's one label) and the tally's locality rows say they come in blocks: P owners each
// spanning span(n / P) chunks (by the owner's degree), taken as independent: C (1 - (1 - span / C)^P), never above
// balls in bins
double chunks_touched(double C, double n, const std::string& family, const EstimateInput::Producer* producer,
                      const json& tally);

}  // namespace evalcpp::predict
