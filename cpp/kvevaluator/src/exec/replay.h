// The P=1 rule, replayed (plan 4.5): the SEEK count of a chunk-reader op depends on the
// order in which its probes land, so the logical counts are defined by replaying Python's
// _ChunkReader over the landing records in task-index order -- identical at every P.
#pragma once
#include <vector>

#include "exec/tasks.h"
#include "store/counting.h"

namespace evalcpp {

// landings: every task's records, concatenated in task-index order.
// parts (optional, with a PartScheme): every replayed charge is also added to the bucket of the
// landing's PROBE key (the chunk prefix + the probed id) -- the part the probe asked about, so a miss's foreign key
// goes there too (counting.h's rule). The buckets sum exactly to the returned counts.
// hists (optional): one range-read entry per charged SEEK (the chunk key it landed on + the chunk value where this
// read is the one charged for it), in the probe key's part (id 0 without a scheme) -- store/size_hist.h.
// chunks (optional): the step's chunk statistics, all from the landing records, so the same at every P.
// rt (optional): the round trips of the replayed reads (store/round_trips.h). A batch (a Landing's group) holds the
// probes whose keys are known before the first is read, so it is ONE level: every SEEK the replay charges is a range
// read of that level (+ its pages), and each batch that charged one adds 1 to critical.
// rt_text (optional, with rt): the same reads as levels (round_trips.h RtText), one job: a level per batch that charged
// a SEEK (its SEEKs as ranges), then a level per page depth (the charged chunks with that many further pages).
struct ChunkStats {
  uint64_t probes = 0;               // chunk probes (landings), free ones included
  uint64_t charged = 0;              // chunk values charged: summed over the step's batches
  uint64_t batches = 0;              // batches = the readers that share a chunk cache (a Landing's group)
  uint64_t distinct_max_batch = 0;   // the most chunks one batch charged
  uint64_t held_bytes_peak = 0;      // the most chunk bytes one batch held (stored size; a batch never drops a chunk)
  json to_json() const {
    return {{"probes", probes}, {"charged", charged}, {"batches", batches}, {"distinct_max_batch", distinct_max_batch},
            {"held_bytes_peak", held_bytes_peak}};
  }
};
Counts replay_landings(const std::vector<Landing>& landings, const PartScheme* scheme = nullptr,
                       PartCounts* parts = nullptr, HistParts* hists = nullptr, ChunkStats* chunks = nullptr,
                       RtTotals* rt = nullptr, RtText* rt_text = nullptr);

// The shared chunks' parses (ChunkParse) by part, under the replay's rule: a chunk's parse goes to the part of the
// first landing on it (the same cache, prefix and first id) in task-index order -- the landing whose part the
// replay charges the chunk's value to -- so the credit is the same at every P and in every run. Added into
// `decoded` (sized to the scheme). Throws on a parse no landing reached, or a chunk parsed twice in one cache.
void credit_chunk_parses(const std::vector<Landing>& landings, const std::vector<ChunkParse>& parses,
                         const PartScheme& scheme, std::vector<DecodeCounts>& decoded);

}  // namespace evalcpp
