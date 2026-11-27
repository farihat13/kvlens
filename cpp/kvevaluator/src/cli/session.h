// A session holds one open store + format + schema and answers plan requests against it.
// `run` uses it once; `serve` keeps it across requests so a WARM block cache persists.
#pragma once
#include <memory>
#include <string>

#include "formats/format.h"
#include "schema/schema.h"
#include "store/rocksdb_store.h"
#include "value/value.h"

namespace evalcpp {

class Session {
 public:
  // Handles one request. Never throws: errors come back in the response.
  json handle(const json& request);
  // the format and store the last request opened (predict --gate reads them after the run; null before any request)
  Format* format() const { return format_.get(); }
  Store* store() const { return store_.get(); }
  const Schema* schema() const { return schema_.get(); }
  const std::string& scale() const { return scale_; }   // the open store's scale (its manifest, else the request's)
 private:
  void open(const json& store_req);
  json lsm_state(const json& opt);   // the open store's LSM shape and background work (optionally: wait until idle)
  json batch(const json& spec);      // a requests file run back to back on the open store, responses to a file
  std::string dir_;
  std::string schema_path_;
  json manifest_;
  std::string scale_;    // the store's dataset tag: manifest `scale`, cross-checked with the request's store.scale
  std::unique_ptr<Schema> schema_;
  std::unique_ptr<Format> format_;
  std::unique_ptr<Store> store_;
};

}  // namespace evalcpp
