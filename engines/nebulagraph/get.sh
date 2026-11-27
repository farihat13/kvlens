#!/usr/bin/env bash
# get.sh: fetch the NebulaGraph sources our patches apply to (nebula at 5d43e44b, the 3.x line) and the RocksDB it
# builds with (v7.5.3), each at an exact commit. Then: bash build.sh (applies patches/, builds bin/nebula-*).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ../fetch-at-sha.sh
fetch_at_sha https://github.com/vesoft-inc/nebula.git 5d43e44b43ae5239400897f664e68b034a0d46e5 nebula
fetch_at_sha https://github.com/facebook/rocksdb.git  540d5aae516265170564ec27b3e67a54a11b7045 rocksdb   # v7.5.3
