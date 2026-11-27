// The VPack-mirror document codec, byte-identical to Python formats/arangodb.py's
// _vp_encode_obj / _vp_decode_val: VelocyPack's COST structure (self-describing members, an
// object frame whose widths step up with total size) with this repo's own head-byte registry.
// System fields (_key, _rev, _id, _from, _to) get 1-byte dictionary codes; a string that IS a
// canonical vid (16 lowercase hex) or a "Label/vid" reference is stored packed (1+8 /
// 1+1+len+8 bytes) -- reversible, and why arango's physical doc bytes undercut the Python
// MODEL's on purpose.
#pragma once
#include <string>
#include <string_view>

#include "value/value.h"

namespace evalcpp::arangodb {

void vp_encode_obj(std::string& out, const Value& doc);          // a row Value, insertion order
std::string vp_encode_str(const std::string& s);                 // packed vid/ref, else str form
Value vp_decode(std::string_view b, size_t& i);                  // one value at b[i]; advances i
Value vp_decode_doc(std::string_view raw);                       // whole buffer; throws on trailing bytes
size_t vp_object_size(std::string_view raw);                    // the byte length of the object at raw[0] (its frame's byteLength)
// the named members' framed bytes (key + value span) in a doc; object frame unattributed
uint64_t vp_doc_field_bytes(std::string_view raw, const std::vector<std::string>& fields);
// Only the named members of a doc, as a row (absent ones are simply not in it -- a get() on the
// row is null either way): the other members are skipped, never built into Values. Every read
// site wants 1-3 members of a doc that carries 10+, so this is what read_vertex_props,
// read_neighbors-with-props, the index walks and top_k decode with (perf sweep).
RowFields vp_decode_fields(std::string_view raw, const std::vector<std::string>& fields);
// the "Label/vid" covering-index value's vid part, without building the ref string
std::string vp_ref_vid(std::string_view raw);
uint64_t vp_ref_uid(std::string_view raw);   // the same vid part as a uid
// its "Label" part: the far vertex's collection, stored with every edge-index entry
std::string vp_ref_label(std::string_view raw);

}  // namespace evalcpp::arangodb
