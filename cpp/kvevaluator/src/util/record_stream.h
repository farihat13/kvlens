// The record stream a tally or a statistics file was made from, as each names it: the path, the line count, and the
// sha256 of the stream's freshness key (<stream>.key.json, python/kvevaluator/measure/records_key.py) when it has one,
// and the stream's content_sha256 (its bytes) when the key names it: two streams that both name it match by it alone.
// kvevaluator tally and graph-stats write it; predict refuses a tally and statistics that name two streams.
#pragma once
#include <cstdint>
#include <string>

#include "value/value.h"

namespace evalcpp {

// read once when the stream is opened, before its first record (a re-dump during a long run must not relabel it);
// the caller adds "lines" when it has read them
json record_stream_at_open(const std::string& path);

// sha256 of a file's bytes (a tally's schema_sha256: the compiled schema it encodes with)
std::string file_sha256(const std::string& path);

// "" when a and b name one stream (or one of them names none: a fixture); else what differs, in words
std::string record_stream_mismatch(const json& a, const json& b);

}  // namespace evalcpp
