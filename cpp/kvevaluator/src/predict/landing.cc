#include "predict/landing.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <map>

namespace evalcpp::predict {

double balls(double B, double n) { return B > 0 ? B * (1.0 - std::pow(1.0 - 1.0 / B, n)) : 0.0; }

double chunks_touched(double C, double n, const std::string& family, const EstimateInput::Producer* producer,
                      const json& tally) {
  double uniform = balls(C, n);
  if (!producer || !tally.contains("locality") || C <= 0) return uniform;
  const EstimateInput::Producer& p = *producer;
  double P = p.owners;
  if (P <= 0) return uniform;
  // the owner-degree buckets, over the owner labels; over the target labels too, unless the set holds one label
  // (producer.label): then that label's lists only (a person's comments span far more chunks than their posts)
  // (the tally holds no row of that label for the family: every label's, as before)
  std::map<int, std::array<double, 3>> h;
  auto buckets = [&](const std::string& lab) {
    for (auto& r : tally["locality"])
      if (r.at("etype") == p.etype && r.at("direction") == p.dir && r.at("family") == family &&
          (lab.empty() || r.value("target_label", "") == lab))
        for (auto& b : r.at("by_degree")) {
          auto& x = h[b[0].get<int>()];
          for (int i = 0; i < 3; i++) x[i] += b[i + 1].get<double>();
        }
  };
  buckets(p.label);
  if (h.empty()) buckets("");
  if (h.empty()) return uniform;
  double d = n / P;
  int want = 1;
  for (double x = d; x >= 2; x /= 2) want++;
  auto it = h.lower_bound(want);
  if (it == h.end()) --it;
  double span = it->second[0] > 0 ? it->second[2] / it->second[0] : 1.0;
  span = std::min(span, std::max(1.0, d));
  return std::min(uniform, C * (1.0 - std::pow(1.0 - std::min(1.0, span / C), P)));
}

}  // namespace evalcpp::predict
