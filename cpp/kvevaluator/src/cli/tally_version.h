#pragma once
// The tally's version: bump it by hand when a tally's rows can change (cli/tally.cc, the meta encoder it runs,
// cli/census_groups.h). The tally library reuses an entry of the same version whatever binary made it
// (python/kvevaluator/predict/tally_library.py); tests/gates/check_tally_version.py fails a change made without a bump.
// Its own header, not cli/commands.h: the record stream's freshness key hashes cli/records.cc's includes, and a bump must
// not make every record stream stale (python/kvevaluator/measure/records_key.py).
namespace evalcpp {
constexpr int kTallyVersion = 2;   // 2: a chunk's size estimate counts a string's UTF-8 bytes (codec/chunks.cc est_props)
}
