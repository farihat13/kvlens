#include "formats/pertype_csr/keys.h"

#include "codec/varint.h"

namespace evalcpp::pertype_csr {

std::string props_prefix(int label_id) {
  std::string k = "t";
  put_u16be(k, uint16_t(label_id));
  k.push_back('p');
  return k;
}

std::string adj_prefix(int label_id, int etype_id, bool out) {
  std::string k = "t";
  put_u16be(k, uint16_t(label_id));
  k.push_back('a');
  put_u16be(k, uint16_t(etype_id));
  k.push_back(out ? 'o' : 'i');
  return k;
}

std::string ep_prefix(int label_id, int etype_id) {
  std::string k = "t";
  put_u16be(k, uint16_t(label_id));
  k.push_back('e');
  put_u16be(k, uint16_t(etype_id));
  return k;
}

std::string with_id(const std::string& prefix, uint64_t id) {
  std::string k = prefix;
  put_u64be(k, id);
  return k;
}

uint64_t trailing_id(std::string_view key) { return get_u64be(key, key.size() - 8); }

}  // namespace evalcpp::pertype_csr
