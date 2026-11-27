#!/bin/bash
# build.sh: build our counter library, bin/libtugraph_kvstats.so (kvstats/tugraph_kvstats.c, one C file, seconds).
# TuGraph itself is not built: get.sh fetches the release. The library is loaded before the server (LD_PRELOAD) and
# receives the calls TuGraph makes into LMDB; that needs liblgraph.so to export LMDB's functions and to call them
# through the dynamic linker, which this script checks on the fetched release.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../env.sh"   # always this tree's ENGINES_DIR
source "$ENGINES_DIR/check-tools.sh"
check_tools tugraph build
LIB="$TUGRAPH_HOME/lib64/lgraph/liblgraph.so"
[[ -f "$LIB" ]] || { echo "build.sh: no $LIB; run engines/tugraph/get.sh first" >&2; exit 1; }
exported="$(nm -D --defined-only "$LIB")"      # read whole, then searched: grep -q on a pipe would stop nm early
for fn in mdb_get mdb_put mdb_del mdb_cursor_get mdb_cursor_put mdb_cursor_del mdb_txn_commit mdb_dbi_open; do
  grep -q " T ${fn}\$" <<<"$exported" \
    || { echo "build.sh: liblgraph.so does not export $fn: a preloaded library cannot count it" >&2; exit 1; }
done
dynamic="$(readelf -d "$LIB")"
if grep -q SYMBOLIC <<<"$dynamic"; then
  echo "build.sh: liblgraph.so is linked -Bsymbolic: its LMDB calls do not pass a preloaded library" >&2; exit 1
fi
mkdir -p "$TUGRAPH_BINARYDIR"
gcc -O2 -g -fPIC -shared -Wall -Wextra -o "$TUGRAPH_BINARYDIR/libtugraph_kvstats.so" \
    "$TUGRAPH_ROOTDIR/kvstats/tugraph_kvstats.c" -ldl -lpthread
echo "build.sh: built engines/tugraph/bin/libtugraph_kvstats.so (TuGraph ${TUGRAPH_VERSION} release, unchanged)"
