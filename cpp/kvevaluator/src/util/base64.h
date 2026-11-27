// Standard base64 (RFC 4648, with '=' padding): how a bytes value crosses JSON ({"$bytes": "<base64>"}).
#pragma once
#include <string>
#include <string_view>

namespace evalcpp {

std::string base64_encode(std::string_view raw);
std::string base64_decode(std::string_view text);   // throws on a character outside the alphabet or a bad length

}  // namespace evalcpp
