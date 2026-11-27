#include "exec/replay.h"

#include <algorithm>
#include <map>
#include <stdexcept>
#include <utility>
#include <vector>

#include "util/uidset.h"

namespace evalcpp {

// the part of a landing's PROBE key (the family prefix + the probed id): a family prefix alone may not name the part
// (meta-v4's 'e'.etype.dir: the owner's group is in the id)
static size_t probe_part(const PartScheme& scheme, const Landing& L) {
  std::string probe_key = L.prefix;
  for (int i = 7; i >= 0; i--) probe_key.push_back(char((L.probe >> (8 * i)) & 0xff));
  return scheme.part_of(probe_key);
}

Counts replay_landings(const std::vector<Landing>& landings, const PartScheme* scheme, PartCounts* parts,
                       HistParts* hists, ChunkStats* chunks, RtTotals* rt, RtText* rt_text) {
  if (scheme && parts && parts->size() < scheme->size()) parts->resize(scheme->size());
  if (hists && hists->size() < (scheme ? scheme->size() : 1)) hists->resize(scheme ? scheme->size() : 1);
  struct Batch { uint64_t charged = 0, bytes = 0, seeks = 0; };
  uint64_t pages_extra = 0;
  std::vector<uint64_t> charged_pages;    // rt_text: each charged chunk's further pages (those with any)
  std::map<uint64_t, Batch> batches;      // per group: what the batch's cache came to hold
  Batch* bt = nullptr;
  KvPairHists hsink;
  KvPairHists* hp = hists ? &(*hists)[0] : &hsink;   // the current landing's histograms
  // Per (group, prefix): the decoded chunks by their lo id (ranges are disjoint, so the last
  // chunk with lo <= probe is the only one that can cover it) and the set of chunk firsts
  // already charged. Both O(log n) / O(1) per landing -- the old per-landing linear scans
  // were O(landings x chunks) and 40 % of a pertype ic5 run (perf sweep).
  // INVARIANTS this relies on (the old per-landing scan did not): a chunk's lo IS its first
  // (the producer sets both from the landed key), and the decoded chunks of one (group,
  // prefix) are DISJOINT ranges. Both are checked on insert -- a producer that breaks them
  // must fail loudly here, never mis-count silently.
  struct Group {
    std::map<uint64_t, uint64_t> by_lo;   // lo (== first) -> hi
    UidSet known;                         // chunk firsts decoded earlier in this call
  };
  std::map<std::pair<uint64_t, std::string>, Group> cache;
  Group* g = nullptr;                     // the last landing's group: consecutive landings
  uint64_t g_group = 0;                   // mostly share one (group, prefix)
  std::string g_prefix;
  Counts c;
  Counts sink;                            // parts off: the part charges land here, unused
  Counts* pc = &sink;                     // the current landing's part bucket
  for (const Landing& L : landings) {
    if (!g || L.group != g_group || L.prefix != g_prefix) {
      g = &cache[{L.group, L.prefix}];
      g_group = L.group;
      g_prefix = L.prefix;
      bt = &batches[L.group];
    }
    if (scheme && parts) {   // the part of the PROBE key
      size_t id = probe_part(*scheme, L);
      pc = &(*parts)[id];
      if (hists) hp = &(*hists)[id];
    }
    auto it = g->by_lo.upper_bound(L.probe);
    if (it != g->by_lo.begin() && L.probe <= std::prev(it)->second) continue;   // inside a decoded chunk's range: free
    c.S++;                                       // seek_for_prev
    bt->seeks++;
    pc->S++;
    if (!L.landed) {                             // no chunk of this prefix at or before the probe:
      c.S_miss++;                                // a miss -- the foreign landing key (if any) goes
      c.seek_miss_key += L.key_valid ? L.key_bytes : 0;   // to the miss lane, never to read
      pc->S_miss++;
      pc->seek_miss_key += L.key_valid ? L.key_bytes : 0;
      hp->add_range_bytes(0, 0, 0);              // a range read that returned nothing
      hp->range_keys.add(0);
      continue;
    }
    c.scan_key += L.key_bytes;                   // the chunk key it landed on
    pc->scan_key += L.key_bytes;
    hp->range_keys.add(1);
    if (!g->known.insert(L.first)) {             // decoded earlier in this call: no value read
      hp->add_range_bytes(L.key_bytes, 0, L.key_bytes);
      hp->keys_crossed++;                        // a chunk key landed on again: a key crossed (its value not read)
      continue;
    }
    hp->values_read++;                           // the landing whose value (the chunk) is read
    c.scan_value += L.chunk_bytes;               // the whole chunk, once
    pc->scan_value += L.chunk_bytes;
    {                                            // the chunk is the read's one KV pair: key + value
      const uint64_t kvpair = uint64_t(L.key_bytes) + L.chunk_bytes;
      hp->add_range_bytes(kvpair, kvpair >= 4096 ? 1 : 0, kvpair >= 4096 ? 0 : kvpair);
    }
    if (uint64_t pg = rt_pages_after_first(uint64_t(L.key_bytes) + L.chunk_bytes); pg) {
      pages_extra += pg;
      if (rt_text) charged_pages.push_back(pg);
    }
    bt->charged++;
    bt->bytes += L.chunk_bytes;
    if (L.lo != L.first || L.hi < L.lo)
      throw std::runtime_error("replay_landings: a landing whose lo != first (or hi < lo): the chunk-range invariant is broken");
    auto nx = g->by_lo.lower_bound(L.lo);        // the neighbours must not overlap [lo, hi]
    if ((nx != g->by_lo.end() && nx->first <= L.hi) ||
        (nx != g->by_lo.begin() && std::prev(nx)->second >= L.lo))
      throw std::runtime_error("replay_landings: overlapping chunk ranges under one (group, prefix)");
    g->by_lo.emplace_hint(nx, L.lo, L.hi);
  }
  if (chunks) {
    chunks->probes += landings.size();
    chunks->batches += batches.size();
    for (auto& [group, b] : batches) {
      chunks->charged += b.charged;
      if (b.charged > chunks->distinct_max_batch) chunks->distinct_max_batch = b.charged;
      if (b.bytes > chunks->held_bytes_peak) chunks->held_bytes_peak = b.bytes;
    }
  }
  if (rt) {
    rt->round_trips += c.S + pages_extra;
    rt->pages_extra += pages_extra;
    for (auto& [group, b] : batches) rt->critical += b.seeks ? 1 : 0;
    // a chunk over one page: its further pages follow its first (one more level each); the longest decides, and a
    // batch's chunks are fetched side by side, so the step waits for at most the largest chunk's pages
    if (pages_extra) {
      uint64_t deepest = 0;
      for (const Landing& L : landings)
        if (L.landed) deepest = std::max(deepest, rt_pages_after_first(uint64_t(L.key_bytes) + L.chunk_bytes));
      rt->critical += deepest;
    }
    if (rt_text) {                               // the same, level by level
      RtLevels job;
      for (auto& [group, b] : batches)
        if (b.seeks) job.lv.push_back({0, b.seeks});
      std::sort(charged_pages.begin(), charged_pages.end());
      for (uint64_t depth = 1; !charged_pages.empty() && depth <= charged_pages.back(); depth++)
        job.lv.push_back({0, uint64_t(charged_pages.end() - std::lower_bound(charged_pages.begin(), charged_pages.end(), depth))});
      job.pages_extra = pages_extra;
      rt_text->add(job.text());
    }
  }
  return c;
}

void credit_chunk_parses(const std::vector<Landing>& landings, const std::vector<ChunkParse>& parses,
                         const PartScheme& scheme, std::vector<DecodeCounts>& decoded) {
  if (parses.empty()) return;
  if (decoded.size() < scheme.size()) decoded.resize(scheme.size());
  // (cache, first) -> the parses of that chunk start, by prefix (a cache holds a few prefixes)
  std::map<std::pair<uint64_t, uint64_t>, std::vector<const ChunkParse*>> open;
  for (const ChunkParse& p : parses) {
    auto& at = open[{p.cache, p.first}];
    for (const ChunkParse* q : at)
      if (q->prefix == p.prefix) throw std::runtime_error("credit_chunk_parses: a chunk parsed twice in one cache");
    at.push_back(&p);
  }
  size_t left = parses.size();
  for (const Landing& L : landings) {
    if (!L.landed || !L.cache) continue;
    auto it = open.find({L.cache, L.first});
    if (it == open.end()) continue;
    auto& at = it->second;
    for (size_t k = 0; k < at.size(); k++)
      if (at[k]->prefix == L.prefix) {
        decoded[probe_part(scheme, L)].add(at[k]->decoded);
        at.erase(at.begin() + long(k));
        left--;
        break;
      }
    if (at.empty()) open.erase(it);
    if (!left) return;
  }
  throw std::runtime_error("credit_chunk_parses: " + std::to_string(left) + " chunk parse(s) with no landing on the chunk");
}

}  // namespace evalcpp
