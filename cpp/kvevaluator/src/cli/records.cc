// `records --schema schema.json --root <CsvBasic-LongDateFormatter dir>`: the C++ producer,
// emitting the neutral record stream as JSON lines byte-identical to Python records_dump.py.
// The record-hash gate diffs its sha256 against Python's;
// the Python oracle consumes the same stream (section 7).
#include <cstdio>
#include <iostream>

#include "cli/commands.h"
#include "ingest/ldbc_csv.h"
#include "schema/schema.h"

namespace evalcpp {

int cmd_records(const std::vector<std::string>& args) {
  std::string schema_path, root;
  for (size_t i = 0; i + 1 < args.size(); i += 2) {
    if (args[i] == "--schema") schema_path = args[i + 1];
    else if (args[i] == "--root") root = args[i + 1];
    else { std::cerr << "records: unknown option " << args[i] << "\n"; return 2; }
  }
  if (schema_path.empty() || root.empty()) {
    std::cerr << "usage: kvevaluator records --schema schema.json --root <CsvBasic-LongDateFormatter dir>\n";
    return 2;
  }
  Schema schema = Schema::load(schema_path);
  std::string line, buf;
  buf.reserve(1 << 22);
  uint64_t n = 0;
  ldbc_csv::stream(root, schema, [&](const ldbc_csv::Record& r) {
    ldbc_csv::jsonl_line(r, line);
    buf += line;
    buf.push_back('\n');
    n++;
    if (buf.size() > (1 << 21)) { std::fwrite(buf.data(), 1, buf.size(), stdout); buf.clear(); }
  });
  std::fwrite(buf.data(), 1, buf.size(), stdout);
  std::cerr << "[records] " << n << " records\n";
  return 0;
}

}  // namespace evalcpp
