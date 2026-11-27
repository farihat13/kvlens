// A capture's files as the Python measuring driver writes them (the layout: output_tree.py), for a capture the
// binary writes itself (predict --plans). The column lists are inputs/contract/capture_columns.json, compiled in; Python
// reads the same file (capture/capture_columns.py). `kvevaluator predict --capture-columns` prints them.
#pragma once
#include <map>
#include <string>
#include <vector>

namespace evalcpp::capture {

using Row = std::map<std::string, std::string>;   // column -> text; a column the row lacks is written empty

const std::vector<std::string>& run_cols();    // counts/<cell>.csv: one row per (query, param, body)
const std::vector<std::string>& write_run_cols();   // counts/<cell>.csv of a cell that holds a write request:
                                                    // run_cols + P, D, write_key, write_value, the last-chunk lane
const std::vector<std::string>& step_cols();   // steps/<cell>.csv: one row per step
const std::vector<std::string>& write_step_cols();   // steps/<cell>.csv of a cell that holds a write request: step_cols
                                                     // + the last-chunk lane (Counts::LastChunk)
const std::vector<std::string>& part_cols();   // parts/<cell>.csv: one row per (step, store part)
const std::vector<std::string>& write_part_cols();   // parts/<cell>.csv of a cell that holds a write request: part_cols
                                                     // + P, D, write_key, write_value, the last-chunk lane

// one CSV as Python's csv module writes it (minimal quoting, "\r\n" line ends), so a reader sees one dialect
void write_csv(const std::string& path, const std::vector<std::string>& cols, const std::vector<Row>& rows);

std::string file_sha256(const std::string& path);   // sha256 of a file's bytes, hex

}  // namespace evalcpp::capture
