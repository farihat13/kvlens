#!/usr/bin/env bash
#
# LDBC → Dgraph Loader (schema + RDF via live loader)
#
# Defaults assume: Alpha HTTP :8080, Alpha gRPC :9080, Zero :5080

set -euo pipefail

# --------------------
# Defaults (override with flags)
# --------------------
SCALE_FACTOR="0.3"
DO_METRICS=false
OUTDIR="logs"
LIVE_CONC="32"
LIVE_BATCH="5000"

DATA_ROOT="`pwd`/data/ldbc"
ALPHA_HTTP="localhost:8080"
ALPHA_GRPC="localhost:9080"
ZERO_ADDR="localhost:5080"
RDF_GLOB="*.rdf"
PAUSE_BETWEEN=false
SCHEMA_PATH="data/ldbcTypes.schema"
valid_sfs=("0.1" "0.3" "1" "3" "10")


usage() {
  cat <<EOF
Usage: $0 [--sf SCALE] [--conc N] [--batch N] [--schema PATH] [--metrics] [-o DIR]
	--sf      Scale factor (default: ${SCALE_FACTOR})
	--conc    Concurrent requests to Dgraph (default: ${LIVE_CONC})
	--batch   N-Quads per mutation batch (default: ${LIVE_BATCH})
	--schema  Path to schema file (default: ${SCHEMA_PATH})
	--metrics Enable metrics during load
	-o|--outdir	Directory to save metrics logs (default: ${OUTDIR})
EOF
}

# --------------------
# Parse flags
# --------------------
while [[ $# -gt 0 ]]; do
	case "$1" in
		--conc) LIVE_CONC="$2"; shift 2 ;;
		--batch) LIVE_BATCH="$2"; shift 2 ;;
		--schema) SCHEMA_PATH="$2"; shift 2 ;;
		--metrics) DO_METRICS=true; shift ;;
		-o|--outdir) OUTDIR="$2"; shift 2 ;;
		-h|--help) usage; exit 0 ;;
		--sf|--scale) 
			SCALE_FACTOR="$2"
            valid=false
            for sf in "${valid_sfs[@]}"; do
                if [[ "$SCALE_FACTOR" == "$sf" ]]; then
                    valid=true
                    break
                fi
            done
            if ! $valid; then
                echo "Error: --sf must be one of: ${valid_sfs[*]}"
                exit 1
            fi
            shift 2
            ;;
		*) echo "Unknown arg: $1"; usage; exit 1 ;;
	esac
done


# --------------------
# Preflight checks
# --------------------

DATA_DIR="${DATA_ROOT}/sf${SCALE_FACTOR}/ldbc_rdf_${SCALE_FACTOR}"
[[ -d "${DATA_DIR}" ]] || { echo "Error: Data dir '${DATA_DIR}' does not exist."; exit 1; }
command -v dgraph >/dev/null 2>&1 || { echo "Error: dgraph binary not found"; exit 1; }

if ! bash scripts/status.sh >/dev/null 2>&1; then
	echo "Error: Dgraph Alpha/Zero not running. Please start them first."
	exit 1
fi

mkdir -p "${OUTDIR}"

START_TIME=$(date +%s)
# --------------------
# Load schema (.schema files)
# --------------------
echo -e "==> Setting LDBC schema from ${SCHEMA_PATH}"

# set schema
bash scripts/ldbc/schema-set.sh "$SCHEMA_PATH"
sleep 5 # wait for schema to propagate
# verify schema
bash scripts/ldbc/schema-verify.sh
# get schema
# curl -s -X POST localhost:8080/query -H "Content-Type: application/graphql+-" -d 'schema {}' | jq


# --------------------
# Load RDF via live loader
# --------------------
echo -e "==> Loading LDBC RDF data from ${DATA_DIR}"

if $DO_METRICS; then
	curl -X POST localhost:8080/admin/trace/reset 
fi


# rdf_files="${DATA_DIR}/*.rdf"

if ! ls ${DATA_DIR}/*.rdf >/dev/null 2>&1; then
    echo "WARN: No RDF files found in ${DATA_DIR}"
else
	# create a comma-separated list of RDF files
	mapfile -t rdf_list < <(ls -1 "${DATA_DIR}"/*.rdf)
	rdf_files=$(IFS=, ; echo "${rdf_list[*]}")
	echo ">> Loading RDF files: ${rdf_files}"

	# dgraph live handles batching and best-effort retries.
	cd "${DGRAPH_RUNSDIR}" # so that \t dir for alpha live is created there
    dgraph live \
        --files "${rdf_files}" \
        --format rdf \
        --alpha "${ALPHA_GRPC}" \
        --zero  "${ZERO_ADDR}" \
        --conc  "${LIVE_CONC}" \
        --batch "${LIVE_BATCH}"
    sleep 10
fi

END_TIME=$(date +%s)
TOTAL_TIME=$((END_TIME - START_TIME))

if $DO_METRICS; then
    echo ">> Collecting metrics…"
    curl -sX POST localhost:8080/admin/trace/counts -o "$OUTDIR/trace.json"
    curl -s localhost:8080/debug/vars > "$OUTDIR/debug_vars.txt"
    curl -s localhost:8080/metrics > "$OUTDIR/metrics.txt"
fi

echo $(date) 
echo "Done! LDBC dataset loaded in $((TOTAL_TIME/60))m $((TOTAL_TIME%60))s"