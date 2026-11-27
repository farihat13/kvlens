// The facts C++ and Python share, read from the tracked data files inputs/contract/*.json (the KV-op table, the
// capability vocabulary, the capture columns). The files are compiled in (cmake/embed_contract.cmake), so a binary
// carries its own commit's facts; Python reads the same files (kvevaluator.contract). Each file is parsed once.
#pragma once
#include <cstdint>
#include <string>
#include <vector>

#include "value/value.h"

namespace evalcpp::contract {

const json& kv_ops();            // kv_ops.json
const json& capabilities();      // capabilities.json
const json& capture_columns();   // capture_columns.json
const json& rocksdb();           // store_config.json's `rocksdb`: every setting a store opens with

uint64_t chunk_coarse();         // capabilities.json chunk_coarse: coarse_chunks from this pack target (bytes) up
double last_chunk_fill();        // capabilities.json last_chunk_fill: a last chunk's assumed share of its pack target
// the columns a name stands for in capture_columns.json: a group, a response_keys entry (its keys, in order), a
// sequence or a file kind (each a list of names, expanded in order)
std::vector<std::string> columns(const std::string& name);

}  // namespace evalcpp::contract
