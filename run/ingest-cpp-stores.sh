#!/bin/bash
# run/ingest-cpp-stores.sh — build the C++ evaluator's stores for a (schema, codec, chunk) combination,
# one per format, without hand-writing `kvevaluator ingest` commands. Existing stores are reused.
#
#   run/ingest-cpp-stores.sh                       # SF from $SF (.env): nebula + dgraph, canonical schema
#   run/ingest-cpp-stores.sh 0.3                   # positional SF
#   run/ingest-cpp-stores.sh --sf 0.1 --schema label_only --formats dgraph,nebula,arangodb
#   run/ingest-cpp-stores.sh --sf 0.1 --formats nebula --codec zstd
#   run/ingest-cpp-stores.sh --sf 0.1 --formats pertype_csr --chunk 16k
#   run/ingest-cpp-stores.sh --sf 0.1 --formats dgraph --rebuild     # remove + re-ingest (destructive)
#   run/ingest-cpp-stores.sh --sf 3 --formats dgraph,nebula,arangodb,pertype_csr --multi   # ONE pass over
#                                                     # the LDBC CSVs, one encoder per format (sf1+: hours -> minutes
#                                                     # of parsing saved; byte-identical stores); --ram-budget-gb N
#   run/ingest-cpp-stores.sh --sf 0.1 --dry-run                      # name the store dirs, build nothing
#
# Flags: --sf <SF> | bare SF | $SF; --schema canonical|label_only|<name>; --formats a,b,c
#        (default nebula,dgraph); --codec none|zstd|zstd-dict16k|zstd-dict64k|lz4|snappy (default: inputs/contract/
#        store_config.json's rocksdb.compression, a store name with no codec suffix);
#        --chunk 64k|16k|<bytes> (per-type formats; on dgraph the opt-in chunk knob -> dgraph-c<N>k);
#        --rebuild; --dry-run; --multi [--ram-budget-gb N]; -h.
# Stores land under output/stores/ldbc/sf<SF>/<format>[-<schema>][-c<N>k][-<codec>]/ with a manifest.json.
# Needs the binary (run/build-cpp.sh). sf1+ takes minutes per store: run it in tmux with a .log.
# Sibling: python/kvevaluator/measure/reingest_stores.py rebuilds EXISTING stores from their manifests.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../run/env.sh"

ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sf)         SF="${2:?--sf needs a value}"; shift 2 ;;
    --schema|--formats|--codec|--chunk|--ram-budget-gb)
                  ARGS+=("$1" "${2:?$1 needs a value}"); shift 2 ;;
    --rebuild|--dry-run|--multi)
                  ARGS+=("$1"); shift ;;
    -h|--help)    awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next} {exit}' "$0"; exit 0 ;;
    -*)           echo "unknown flag: $1" >&2; exit 1 ;;
    *)            SF="$1"; shift ;;   # bare positional -> SF
  esac
done
: "${SF:?set SF (positional, --sf, or .env)}"

"$PYTHON" -u -m kvevaluator.measure.ingest_stores --sf "$SF" "${ARGS[@]}"
echo "[$(basename "$0")] DONE${SF:+ (sf$SF)}"
