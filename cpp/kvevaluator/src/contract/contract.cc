#include "contract/contract.h"

#include <stdexcept>

namespace evalcpp::contract {

// contract_data.cc, generated at build time (cmake/embed_contract.cmake)
extern const char* const kContract_kv_ops;
extern const char* const kContract_capabilities;
extern const char* const kContract_capture_columns;
extern const char* const kContract_store_config;

namespace {
json parse(const char* text, const char* name) {
  try {
    return json::parse(text);
  } catch (const std::exception& e) {
    throw std::runtime_error(std::string("inputs/contract/") + name + ".json: " + e.what());
  }
}
}  // namespace

const json& kv_ops() {
  static const json j = parse(kContract_kv_ops, "kv_ops");
  return j;
}

const json& capabilities() {
  static const json j = parse(kContract_capabilities, "capabilities");
  return j;
}

const json& rocksdb() {
  static const json j = parse(kContract_store_config, "store_config").at("rocksdb");
  return j;
}

const json& capture_columns() {
  static const json j = parse(kContract_capture_columns, "capture_columns");
  return j;
}

uint64_t chunk_coarse() {
  static const uint64_t v = capabilities().at("chunk_coarse").get<uint64_t>();
  return v;
}

double last_chunk_fill() {
  static const double v = capabilities().at("last_chunk_fill").get<double>();
  return v;
}

namespace {
std::vector<std::string> expand(const json& c, const std::string& name) {
  std::vector<std::string> out;
  if (c.at("groups").contains(name)) {
    for (auto& col : c["groups"][name]) out.push_back(col.get<std::string>());
  } else if (c.at("response_keys").contains(name)) {
    for (auto& [col, key] : c["response_keys"][name].items()) out.push_back(col);
  } else {
    const json* list = c.at("sequences").contains(name) ? &c["sequences"][name]
                       : c.at("files").contains(name)   ? &c["files"][name]
                                                        : nullptr;
    if (!list) throw std::runtime_error("no group, sequence or file '" + name + "'");
    for (auto& part : *list)
      for (auto& col : expand(c, part.get<std::string>())) out.push_back(col);
  }
  return out;
}
}  // namespace

std::vector<std::string> columns(const std::string& name) {
  try {
    return expand(capture_columns(), name);
  } catch (const std::exception& e) {   // a missing key or a name that does not resolve: say which file
    throw std::runtime_error(std::string("inputs/contract/capture_columns.json: ") + e.what());
  }
}

}  // namespace evalcpp::contract
