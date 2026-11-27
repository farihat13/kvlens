#include "formats/meta/parts.h"

#include <stdexcept>

#include "formats/meta/keys.h"
#include "util/vid.h"

namespace evalcpp::meta {

static const char* dir_word(uint8_t d) { return d == kDirOut ? "out" : "in"; }

size_t MetaParts::add(const std::string& name) {
  auto it = ids_.find(name);
  if (it != ids_.end()) return it->second;
  ids_[name] = names_.size();
  names_.push_back(name);
  return names_.size() - 1;
}

MetaParts::MetaParts(const Schema& schema) : props_(256, 0), adj_(16 * 256 * 2, 0), eprops_(16 * 256, 0) {
  add("unknown");   // id 0: the catch-all (store/counting.h)
  // the names are parsed by splitting on ':' (and '.' for an index's prop): refuse a schema name
  // that would make one ambiguous
  auto plain = [](const std::string& n) {
    if (n.find_first_of(":.") != std::string::npos)
      throw std::runtime_error("meta parts: schema name '" + n + "' contains ':' or '.' (part names split on them)");
  };
  for (auto& v : schema.vertices()) { plain(v.label); plain(v.group); }
  for (auto& e : schema.edges()) plain(e.etype);
  for (auto& ix : schema.indexes()) plain(ix.prop);
  auto byte_id = [](int id, const std::string& what) {
    if (id < 0 || id > 255) throw std::runtime_error("meta parts: " + what + " id out of the 1-byte key range");
    return size_t(id);
  };
  for (auto& v : schema.vertices()) props_[byte_id(v.id, v.label)] = uint32_t(add("props:" + v.label));
  for (auto& e : schema.edges()) {
    size_t eid = byte_id(e.id, e.etype);
    for (auto& v : schema.vertices()) {   // one name per (group, etype, dir): the owner's tag names its group
      if (v.tag > 15) throw std::runtime_error("meta parts: vid tag of " + v.label + " beyond 4 bits");
      for (uint8_t d : {kDirOut, kDirIn})
        adj_[(size_t(v.tag) * 256 + eid) * 2 + d] = uint32_t(add("adj:" + v.group + ":" + e.etype + ":" + dir_word(d)));
      eprops_[size_t(v.tag) * 256 + eid] = uint32_t(add("eprops:" + v.group + ":" + e.etype));
    }
  }
  for (auto& ix : schema.indexes())
    index_[uint32_t(ix.id)] = uint32_t(add("index:" + ix.label + (ix.prop.empty() ? "" : "." + ix.prop)));
}

size_t MetaParts::part_of(std::string_view key) const {
  if (key.empty()) return 0;
  auto b = [&](size_t i) { return uint8_t(key[i]); };
  switch (key[0]) {
    case kVertex:                                    // 'v'·vtype·vid·[attr]
      return key.size() >= 2 ? props_[b(1)] : 0;
    case kEdge:                                      // 'e'·etype·dir·owner(8)·[dst | part suffix]
      if (key.size() < 4) return 0;
      if (b(2) == kEpDir) return eprops_[size_t(b(3) & 15) * 256 + b(1)];   // 'e'·etype·0x02·tag·first_ordinal
      if (b(2) > 1) return 0;
      return adj_[(size_t(b(3) >> 4) * 256 + b(1)) * 2 + b(2)];             // the owner's tag: byte 3's top 4 bits
    case 'i': {                                      // 'i'·index_id:u32·... (index/sorted_index.h)
      if (key.size() < 5) return 0;
      uint32_t id = (uint32_t(b(1)) << 24) | (uint32_t(b(2)) << 16) | (uint32_t(b(3)) << 8) | b(4);
      auto it = index_.find(id);
      return it == index_.end() ? 0 : it->second;
    }
    default:
      return 0;
  }
}

}  // namespace evalcpp::meta
