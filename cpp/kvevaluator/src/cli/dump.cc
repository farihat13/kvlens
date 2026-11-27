// `dump <store-dir>`: a sha256 over every (key, value) of the store in key order, plus counts.
// Two stores built by different ingest paths must print the same digest (byte identity).
// `dump <store-dir> --prefix <hex> --digest`: the same digest over the pairs under one key prefix (one key family:
// e.g. meta's 'e'·etype·dir), so one family of two stores can be compared key by key.
#include <cstring>
#include <iostream>

#include "cli/commands.h"
#include "ingest/ingest.h"
#include "store/rocksdb_store.h"
#include "util/sha256.h"

namespace evalcpp {

static int hexval(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  char l = char(c | 0x20);
  if (l >= 'a' && l <= 'f') return l - 'a' + 10;
  throw std::runtime_error(std::string("dump --prefix: '") + c + "' is not a hex digit");
}

int cmd_dump(const std::vector<std::string>& args) {
  if (args.empty()) {
    std::cerr << "usage: kvevaluator dump <store-dir> [--prefix <hex> (--limit N | --digest)]\n";
    return 2;
  }
  std::string prefix;
  size_t limit = 5;
  bool digest = false;
  for (size_t i = 1; i < args.size(); i++) {
    if (args[i] == "--digest") { digest = true; continue; }
    if (i + 1 >= args.size()) break;
    if (args[i] == "--prefix") {
      const std::string& h = args[i + 1];
      for (size_t j = 0; j + 1 < h.size(); j += 2) prefix.push_back(char(hexval(h[j]) * 16 + hexval(h[j + 1])));
    } else if (args[i] == "--limit") limit = std::stoull(args[i + 1]);
    i++;
  }
  if (digest && prefix.empty()) {
    std::cerr << "dump --digest needs --prefix <hex> (the whole store: dump <store-dir>)\n";
    return 2;
  }
  if (!prefix.empty()) {   // diagnostic: the first `limit` (key, value) pairs under a byte prefix
    json m = read_manifest(args[0]);
    StoreOptions so = store_options_of(m, args[0]);
    auto store = Store::open_read_write(args[0] + "/db", so, false);
    std::unique_ptr<rocksdb::Iterator> it(store->db()->NewIterator(rocksdb::ReadOptions()));
    size_t n = 0;
    if (digest) {   // every pair under the prefix, hashed as the whole-store digest hashes them
      Sha256 h;
      uint64_t kb = 0, vb = 0;
      for (it->Seek(prefix); it->Valid(); it->Next(), n++) {
        rocksdb::Slice k = it->key();
        if (k.size() < prefix.size() || memcmp(k.data(), prefix.data(), prefix.size()) != 0) break;
        h.update(k.data(), k.size());
        h.update("\x01", 1);
        h.update(it->value().data(), it->value().size());
        h.update("\x02", 1);
        kb += k.size();
        vb += it->value().size();
      }
      std::cout << json{{"prefix", rocksdb::Slice(prefix).ToString(true)}, {"keys", n}, {"key_bytes", kb}, {"value_bytes", vb},
                        {"sha256", h.hexdigest()}}.dump() << "\n";
      return 0;
    }
    for (it->Seek(prefix); it->Valid() && n < limit; it->Next(), n++) {
      rocksdb::Slice k = it->key();
      if (k.size() < prefix.size() || memcmp(k.data(), prefix.data(), prefix.size()) != 0) break;
      std::cout << json{{"key", k.ToString(true)}, {"vsize", it->value().size()},
                        {"value", it->value().ToString(true)}}.dump() << "\n";
    }
    return 0;
  }
  json m = read_manifest(args[0]);
  StoreOptions so = store_options_of(m, args[0]);
  auto store = Store::open_read_write(args[0] + "/db", so, false);
  Sha256 h;
  uint64_t n = 0, kb = 0, vb = 0;
  std::unique_ptr<rocksdb::Iterator> it(store->db()->NewIterator(rocksdb::ReadOptions()));
  for (it->SeekToFirst(); it->Valid(); it->Next()) {
    h.update(it->key().data(), it->key().size());
    h.update("\x01", 1);
    h.update(it->value().data(), it->value().size());
    h.update("\x02", 1);
    n++;
    kb += it->key().size();
    vb += it->value().size();
  }
  std::cout << json{{"keys", n}, {"key_bytes", kb}, {"value_bytes", vb}, {"sha256", h.hexdigest()}}.dump() << "\n";
  return 0;
}

}  // namespace evalcpp
