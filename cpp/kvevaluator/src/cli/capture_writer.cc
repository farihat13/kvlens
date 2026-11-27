#include "cli/capture_writer.h"

#include <algorithm>
#include <fstream>
#include <sstream>
#include <stdexcept>

#include "contract/contract.h"
#include "util/sha256.h"

namespace evalcpp::capture {

// the six lists are inputs/contract/capture_columns.json's file kinds (compiled in; Python's capture_columns reads the
// same file): a read-only cell keeps run_cols / part_cols, a cell that holds a write request adds the write lanes
const std::vector<std::string>& run_cols() {
  static const std::vector<std::string> c = contract::columns("counts");
  return c;
}

const std::vector<std::string>& write_run_cols() {
  static const std::vector<std::string> c = contract::columns("counts_writes");
  return c;
}

const std::vector<std::string>& write_part_cols() {
  static const std::vector<std::string> c = contract::columns("parts_writes");
  return c;
}

const std::vector<std::string>& step_cols() {
  static const std::vector<std::string> c = contract::columns("steps");
  return c;
}

const std::vector<std::string>& write_step_cols() {
  static const std::vector<std::string> c = contract::columns("steps_writes");
  return c;
}

const std::vector<std::string>& part_cols() {
  static const std::vector<std::string> c = contract::columns("parts");
  return c;
}

namespace {
std::string field(const std::string& s) {   // Python csv QUOTE_MINIMAL: quote a field holding , " \r or \n
  if (s.find_first_of(",\"\r\n") == std::string::npos) return s;
  std::string q = "\"";
  for (char ch : s) q += ch == '"' ? std::string("\"\"") : std::string(1, ch);
  return q + "\"";
}
}  // namespace

void write_csv(const std::string& path, const std::vector<std::string>& cols, const std::vector<Row>& rows) {
  std::ofstream f(path, std::ios::binary);
  if (!f) throw std::runtime_error("cannot write " + path);
  for (size_t i = 0; i < cols.size(); i++) f << (i ? "," : "") << field(cols[i]);
  f << "\r\n";
  for (auto& r : rows) {
    for (auto& [k, v] : r)
      if (std::find(cols.begin(), cols.end(), k) == cols.end()) throw std::runtime_error(path + ": no column " + k);
    for (size_t i = 0; i < cols.size(); i++) {
      auto it = r.find(cols[i]);
      f << (i ? "," : "") << (it == r.end() ? "" : field(it->second));
    }
    f << "\r\n";
  }
  if (!f) throw std::runtime_error("cannot write " + path);
}

std::string file_sha256(const std::string& path) {
  std::ifstream f(path, std::ios::binary);
  if (!f) throw std::runtime_error("cannot open " + path);
  Sha256 h;
  char buf[1 << 16];
  while (f.read(buf, sizeof buf) || f.gcount() > 0) h.update(buf, static_cast<size_t>(f.gcount()));
  return h.hexdigest();
}

}  // namespace evalcpp::capture
