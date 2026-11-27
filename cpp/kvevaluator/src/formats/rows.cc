#include "formats/rows.h"

#include <stdexcept>

namespace evalcpp {

const std::string& string_payload(const ColDecl& c, const Value& v) {
  if (c.type == "bytes") {
    if (!v.is_bytes())
      throw std::runtime_error("column '" + c.name + "' is bytes, got a " + v.type_name() + " (" + repr(v) +
                               "): an input written before the bytes type? re-dump the records / params");
    return v.as_bytes();
  }
  return v.as_str();
}

const Value* ValueRows::cell(size_t row, size_t col) const {
  const Value* v = rows_[row].props->find(cols_[col].name);
  return (v && !v->is_null()) ? v : nullptr;
}
bool ValueRows::present(size_t row, size_t col) const { return cell(row, col) != nullptr; }
int64_t ValueRows::int_at(size_t row, size_t col) const { return cell(row, col)->as_int(); }
std::string_view ValueRows::str_at(size_t row, size_t col) const { return string_payload(cols_[col], *cell(row, col)); }
size_t ValueRows::multi_count(size_t row, size_t col) const { return cell(row, col)->as_list().size(); }
std::string_view ValueRows::multi_at(size_t row, size_t col, size_t k) const { return cell(row, col)->as_list()[k].as_str(); }

}  // namespace evalcpp
