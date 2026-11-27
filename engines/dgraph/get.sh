#!/usr/bin/env bash
# get.sh: fetch the Dgraph sources our patches apply to (Dgraph v25.0.0, Badger v4.8.0), each at an exact commit, and
# make data/ldbc, where `just ldbc-download` writes the RDF it converts from LDBC's CSVs and links the params and update
# streams (dgraph-benchmarks' LDBC checkout is not fetched: its RDF files are Git LFS objects, which fail to fetch, and
# nothing reads them). Then: bash build.sh (applies patches/, builds bin/dgraph-kvstats).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ../fetch-at-sha.sh
fetch_at_sha https://github.com/dgraph-io/dgraph.git            e6980befe54103c67f353ffaa311345747ebb147 dgraph-25.0.0  # v25.0.0
fetch_at_sha https://github.com/dgraph-io/badger.git            b43f4aa69abb80cdbaadfc038d8cd7031d7892fd badger         # v4.8.0
if [ -L data/ldbc ]; then       # an older get.sh linked it into the dgraph-benchmarks checkout (LFS placeholders)
    echo "get.sh: data/ldbc is a link from an older get.sh ($(readlink data/ldbc)): move it aside, then re-run" >&2
    exit 1
fi
mkdir -p data/ldbc
