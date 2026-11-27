#!/usr/bin/env bash
#
# LDBC → Dgraph Bulk Loader (offline, much faster than live loader)
#
# Writes Badger p-directory directly — no running Alpha/Zero needed during load.
# Zero must be running briefly at the start to assign a lease, then is stopped.
# After completion, the generated p/ directory is placed in DGRAPH_RUNSDIR.
#
# This produces already rolled-up data — no separate force-rollup needed.

set -euo pipefail

# --------------------
# Defaults (override with flags)
# --------------------
SCALE_FACTOR="0.3"
NUM_GOROUTINES="32"

DATA_ROOT="$(pwd)/data/ldbc"
ZERO_ADDR="localhost:5080"
SCHEMA_PATH="data/ldbcTypes.schema"
valid_sfs=("0.1" "0.3" "1" "3" "10")

usage() {
  cat <<EOF
Usage: $0 [--sf SCALE] [--threads N] [--schema PATH]
	--sf       Scale factor (default: ${SCALE_FACTOR})
	--threads  Number of worker goroutines (default: ${NUM_GOROUTINES})
	--schema   Path to schema file (default: ${SCHEMA_PATH})
EOF
}

# --------------------
# Parse flags
# --------------------
while [[ $# -gt 0 ]]; do
	case "$1" in
		--threads) NUM_GOROUTINES="$2"; shift 2 ;;
		--schema) SCHEMA_PATH="$2"; shift 2 ;;
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
[[ -f "${SCHEMA_PATH}" ]] || { echo "Error: Schema file '${SCHEMA_PATH}' not found."; exit 1; }
SCHEMA_PATH="$(realpath "${SCHEMA_PATH}")"
command -v dgraph >/dev/null 2>&1 || { echo "Error: dgraph binary not found"; exit 1; }

if ! ls "${DATA_DIR}"/*.rdf >/dev/null 2>&1; then
	echo "Error: No RDF files found in ${DATA_DIR}"
	exit 1
fi

# Build comma-separated list of RDF files
mapfile -t rdf_list < <(ls -1 "${DATA_DIR}"/*.rdf)
rdf_files=$(IFS=, ; echo "${rdf_list[*]}")

BULK_OUT="${DGRAPH_RUNSDIR}/bulk_out"
BULK_TMP="${DGRAPH_RUNSDIR}/bulk_tmp"

# Clean previous bulk output
rm -rf "${BULK_OUT}" "${BULK_TMP}"

START_TIME=$(date +%s)

echo "==> Bulk loading LDBC SF${SCALE_FACTOR} data (${NUM_GOROUTINES} threads)"
echo "    Schema: ${SCHEMA_PATH}"
echo "    Data:   ${DATA_DIR}"
echo "    Output: ${BULK_OUT}"

# Zero must be running for bulk loader to get a UID lease
echo "==> Starting Zero for UID lease assignment..."
mkdir -p "${DGRAPH_RUNSDIR}" && cd "${DGRAPH_RUNSDIR}"
rm -rf ./zw  # clean zero WAL
dgraph zero > /dev/null 2>&1 &
ZERO_PID=$!
sleep 3

# Verify zero is up
if ! kill -0 $ZERO_PID 2>/dev/null; then
	echo "Error: Zero failed to start"
	exit 1
fi

cd "${DGRAPH_RUNSDIR}"
dgraph bulk \
	--files "${rdf_files}" \
	--schema "${SCHEMA_PATH}" \
	--format rdf \
	--zero "${ZERO_ADDR}" \
	--num_go_routines "${NUM_GOROUTINES}" \
	--reduce_shards 1 \
	--map_shards 1 \
	--out "${BULK_OUT}" \
	--tmp "${BULK_TMP}" \
	--replace_out

# Stop Zero gracefully, escalate if needed
kill $ZERO_PID 2>/dev/null || true
for i in $(seq 1 10); do
	kill -0 $ZERO_PID 2>/dev/null || break
	sleep 1
done
kill -9 $ZERO_PID 2>/dev/null || true
wait $ZERO_PID 2>/dev/null || true

# Move the generated p directory into place
# Bulk loader outputs to: out/0/p (for shard 0)
if [[ -d "${BULK_OUT}/0/p" ]]; then
	rm -rf "${DGRAPH_RUNSDIR}/p"
	mv "${BULK_OUT}/0/p" "${DGRAPH_RUNSDIR}/p"
	echo "==> Moved bulk output to ${DGRAPH_RUNSDIR}/p"
else
	echo "Error: Expected bulk output at ${BULK_OUT}/0/p not found"
	ls -la "${BULK_OUT}/" 2>/dev/null || true
	exit 1
fi

# Clean up
rm -rf "${BULK_OUT}" "${BULK_TMP}"

END_TIME=$(date +%s)
TOTAL_TIME=$((END_TIME - START_TIME))

echo "$(date)"
echo "Done! LDBC dataset bulk loaded in $((TOTAL_TIME/60))m $((TOTAL_TIME%60))s"
