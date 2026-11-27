// meta's PartScheme (store/counting.h): which store PART a meta key belongs to, decoded from the key
// alone (the naming scheme is documented in keys.h, "Parts"). Built once per Meta from the schema. An adjacency key
// has one shape in every layout (meta-v4, key family D: 'e'·etype·dir·owner...), so it decodes the same way whatever
// the etype's layout: the owner vid's group from its tag (the top 4 bits of byte 3); dir 2 = own-props chunks.
#pragma once
#include <cstdint>
#include <map>
#include <set>
#include <string>
#include <string_view>
#include <vector>

#include "schema/schema.h"
#include "store/counting.h"

namespace evalcpp::meta {

class MetaParts : public PartScheme {
 public:
  explicit MetaParts(const Schema& schema);
  size_t size() const override { return names_.size(); }
  size_t part_of(std::string_view key) const override;
  std::string name(size_t id) const override { return names_.at(id); }

 private:
  size_t add(const std::string& name);   // the id of `name` (new, or the existing one: names are unique)
  std::vector<std::string> names_;       // id -> name; id 0 = "unknown"
  std::map<std::string, size_t> ids_;
  // dense lookups by the key's discriminator bytes (0 = unknown; a family the layout does not write stays 0)
  std::vector<uint32_t> props_;          // [vtype]
  std::vector<uint32_t> adj_;            // [(vid tag * 256 + etype) * 2 + dir]
  std::vector<uint32_t> eprops_;         // [vid tag * 256 + etype] (own placement)
  std::map<uint32_t, uint32_t> index_;   // index id -> part
};

}  // namespace evalcpp::meta
