// The CLI subcommands. Each takes the argv after its own name and returns an exit code.
#pragma once
#include <string>
#include <vector>

namespace evalcpp {
int cmd_ingest(const std::vector<std::string>& args);
int cmd_run(const std::vector<std::string>& args);
int cmd_serve(const std::vector<std::string>& args);
int cmd_dump(const std::vector<std::string>& args);
int cmd_checkpoint(const std::vector<std::string>& args);
int cmd_size(const std::vector<std::string>& args);
int cmd_census(const std::vector<std::string>& args);   // per-type keys and bytes of one store (read-only)
int cmd_sst_props(const std::vector<std::string>& args);   // per-SST table properties (index / filter / data bytes)
int cmd_records(const std::vector<std::string>& args);
int cmd_caps(const std::vector<std::string>& args);   // the capability set per (format, schema)
int cmd_schema_path(const std::vector<std::string>& args);   // the manifest schema path rule on one path
int cmd_graph_stats(const std::vector<std::string>& args);   // predict mode's graph statistics: degrees and the via histograms, format-free, no store
int cmd_tally(const std::vector<std::string>& args);     // predict mode's layout statistics (meta): an ingest into a counting sink, no store
int cmd_predict(const std::vector<std::string>& args);   // predict mode (meta): the exact gate, the accuracy check
}
