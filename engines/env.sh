# engines/env.sh: source before any engine script (`source engines/env.sh`). Every path comes from this file's location.
set -a
ENGINES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # ALWAYS from this file: an inherited value never wins
# the repo venv: `just setup` installs kvengines + kvevaluator. Not named PYTHON: this file exports what it sets,
# and ArangoDB's bundled ICU 64 configure runs $PYTHON when set (the venv's 3.12 has no distutils)
ENGINES_PYTHON="${ENGINES_PYTHON:-$ENGINES_DIR/../.venv/bin/python3}"
# The LDBC SNB raw data and substitution parameters (LDBC's datagen output), one dir per SF: the repo's
# <data dir>/datasets/ldbc (`just fetch-dataset`), as python/kvevaluator/paths.py resolves the data dir.
if [ -z "${LDBC_SNB_DATA_DIR:-}" ]; then
  _datasets=$("$ENGINES_PYTHON" -m kvevaluator.paths datasets_dir) && LDBC_SNB_DATA_DIR="$_datasets/ldbc" \
    || echo "engines/env.sh: no datasets dir (the reason is above); LDBC_SNB_DATA_DIR is unset" >&2
  unset _datasets
fi

# Go toolchain for the Dgraph build (the old root .env)
PATH="$PATH:/usr/local/go/bin"
GOPATH="${GOPATH:-$HOME/go}"
PATH="$GOPATH/bin:$PATH"

# Each engine's wiring (source dirs, runs/, bin/), relative to its own dir
source "$ENGINES_DIR/dgraph/.env"
source "$ENGINES_DIR/nebulagraph/.env"
source "$ENGINES_DIR/arangodb/.env"
source "$ENGINES_DIR/tugraph/.env"

# The measured operating point (the kvformat experiment's .env at the freeze). SF is overridable: `export SF=0.3` first.
SF="${SF:-0.1}"
DGRAPH_DETSORT=enable
DGRAPH_EQFILTERPATH=index
DGRAPH_TYPEFILTERPATH=index
DGRAPH_INEQFILTERPATH=index
DGRAPH_LOCALCACHE=disable          # LocalCache off: deterministic KV counts
DGRAPH_CACHE_MB=4096
DGRAPH_CACHE_PERCENTAGE="0,90,10"
NEBULA_CACHE_MB=4096
# ENGINES_SETTING=equivalent (Figure 2's times; the default is counting, the point above): every process of the engine on
# one CPU, ENGINES_CPU (pinned after start; Dgraph also GOMAXPROCS=1, NebulaGraph and ArangoDB their in-query parallelism
# off), every engine cache off (NebulaGraph's RocksDB row cache too), and the KV block cache of the regime in all
# three. The KV counts do not move: each engine counts at its call into the KV store.
ENGINES_SETTING="${ENGINES_SETTING:-counting}"
ENGINES_CPU="${ENGINES_CPU:-4}"
case "$ENGINES_SETTING" in
  counting|equivalent) ;;
  *) echo "engines/env.sh: ENGINES_SETTING must be counting or equivalent, got '$ENGINES_SETTING'" >&2; return 1 ;;
esac
# ENGINES_REGIME (with equivalent): a cache regime of inputs/contract/store_config.json, the registry the evaluator's
# timed runs read too (kvevaluator.measure.store_config.regime: every name and its settings). What it sets here:
#   ENGINES_REGIME_BLOCK_CACHE_MIB        the KV block cache of all three (warm: 8 GiB, everything fits; steady and the
#                                         bounded regimes: the steady size for SF, 30 MiB at SF 0.3). TuGraph has none
#   ENGINES_REGIME_DIRECT_IO              1: a block-cache miss reads the disk (steady). NebulaGraph and ArangoDB only:
#                                         Badger and LMDB map their files, so Dgraph's and TuGraph's starts refuse it
#   ENGINES_REGIME_INDEX_FILTER_IN_CACHE  0: index and filter blocks outside the block cache (the bounded regimes; set
#                                         on ArangoDB, as NebulaGraph and Badger always keep them); empty: the engine's own
#   ENGINES_REGIME_COLD_START             1: every timed run is on a server started anew (cold-bounded): between its
#                                         restore and its launch the start puts the restored store's files out of
#                                         the OS page cache (lc_evict_files: those files alone, so the engine's own
#                                         code stays in memory, as the evaluator's cold keeps its process), and it
#                                         runs no check that reads the store
ENGINES_REGIME="${ENGINES_REGIME:-warm}"
unset ENGINES_REGIME_NAME ENGINES_REGIME_BLOCK_CACHE_MIB ENGINES_REGIME_DIRECT_IO ENGINES_REGIME_INDEX_FILTER_IN_CACHE ENGINES_REGIME_COLD_START   # of an earlier source
if [ "$ENGINES_SETTING" = equivalent ]; then
  _regime=$("$ENGINES_PYTHON" -m kvevaluator.measure.store_config "$ENGINES_REGIME" "$SF") \
    || { echo "engines/env.sh: ENGINES_REGIME=$ENGINES_REGIME at SF=$SF: not a regime of the registry (the reason is above)" >&2; return 1; }
  while IFS== read -r _k _v; do printf -v "ENGINES_REGIME_${_k^^}" %s "$_v"; done <<< "$_regime"
  unset _regime _k _v
  DGRAPH_CACHE_MB=$ENGINES_REGIME_BLOCK_CACHE_MIB
  DGRAPH_CACHE_PERCENTAGE="0,100,0"   # Badger: all of it to the block cache; index cache 0 keeps every table index in memory
  NEBULA_CACHE_MB=$ENGINES_REGIME_BLOCK_CACHE_MIB
  ARANGODB_CACHE_MB=$ENGINES_REGIME_BLOCK_CACHE_MIB
elif [ "$ENGINES_REGIME" != warm ]; then
  echo "engines/env.sh: ENGINES_REGIME=$ENGINES_REGIME needs ENGINES_SETTING=equivalent" >&2; return 1
fi
set +a

# engine_dir <db>: the engine's dir name under engines/ (the code's db key "nebula" is the dir nebulagraph; a second
# configuration of an engine, tugraph-detached, lives in its engine's dir)
engine_dir() { case "$1" in nebula) echo nebulagraph ;; tugraph-detached) echo tugraph ;; *) echo "$1" ;; esac; }
# engine_backup_tag <db>: what a configuration's backup and load record carry after sf<SF>, so two configurations of
# one engine keep two backups (backup/runs-sf0.3 and backup/runs-sf0.3-detached); empty for an engine's first
engine_backup_tag() { case "$1" in tugraph-detached) echo -detached ;; *) echo "" ;; esac; }
# engine_store_dir <db>: the dir of the running server's KV store (what a start restores from the backup): the files a
# cold start must find out of the OS page cache (time_queries.py counts their cached pages before each cold run)
engine_store_dir() {
  case "$(engine_dir "$1")" in
    dgraph) echo "$DGRAPH_RUNSDIR/p" ;;
    nebulagraph) echo "$NEBULA_RUNSDIR/data/storage" ;;
    arangodb) echo "$ARANGO_RUNSDIR/data/engine-rocksdb" ;;
    tugraph) echo "$TUGRAPH_RUNSDIR/data" ;;
    *) echo "engine_store_dir: no engine '$1'" >&2; return 1 ;;
  esac
}
export -f engine_dir engine_backup_tag engine_store_dir
