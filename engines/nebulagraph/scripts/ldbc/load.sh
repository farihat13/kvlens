#!/bin/bash

set -e  # Exit on error

SF="0.3"
SCHEMA_PATH="./data/ldbc/schema.gql"

usage() {
    cat <<EOF
Usage: $0 [--sf SCALE] [--schema PATH]
  --sf      Scale factor (default: ${SF})
  --schema  Path to schema file (default: ${SCHEMA_PATH})
  -h|--help Show this help
EOF
}

# Flags (was positional \$1=SF \$2=SCHEMA; switched to match dgraph/arango load.sh).
while [[ $# -gt 0 ]]; do
    case "$1" in
        --sf) SF="$2"; shift 2 ;;
        --schema) SCHEMA_PATH="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown arg: $1" >&2; usage >&2; exit 1 ;;
    esac
done

# Activate the venv for the python3 scripts (kvengines)
source "${MY_NEBULA_ROOTDIR}/../../.venv/bin/activate"     # the repo venv (just setup)
nebula_console_cmd="nebula-console -u root -p nebula -addr 127.0.0.1 -port 9669"
DATA_DIR="./data/ldbc/sf${SF}/CsvBasic-LongDateFormatter"

which nebula-console > /dev/null || { echo "nebula-console not found in PATH"; exit 1; }
which nebula-importer > /dev/null || { echo "nebula-importer not found in PATH"; exit 1; }
[ -d "${DATA_DIR}" ] || { echo "Data directory ${DATA_DIR} not found"; exit 1; }

echo ""
echo "Loading LDBC SF${SF} data into Nebula Graph..."

START_TIME=$(date +%s)

# ========================================
# Step 0: Preprocess IDs
# ========================================
echo ""
echo "Preprocessing LDBC SF${SF} data..."

# Run preprocessing
PREFIXED_DIR="${DATA_DIR}_prefixed"
python3 ../../python/kvengines/nebula/ldbc/preprocess_ids.py "$DATA_DIR"
DATA_DIR="${PREFIXED_DIR}"
echo "✓ IDs prefixed"

# ========================================
# Step 1: Create space and schema
# ========================================
echo ""
echo "Creating LDBC space and schema..."


# Create space
${nebula_console_cmd} -e "DROP SPACE IF EXISTS ldbc;" > /dev/null
${nebula_console_cmd} -e "CREATE SPACE IF NOT EXISTS ldbc(PARTITION_NUM = 100, REPLICA_FACTOR = 1, vid_type = FIXED_STRING(30));" > /dev/null
echo "✓ Space 'ldbc' created"

# Wait for space to become ready (instead of fixed sleep)
echo "Waiting for space 'ldbc' to be ready..."
space_ready=false
for i in $(seq 1 30); do
    if ${nebula_console_cmd} -e "USE ldbc;" 2>/dev/null | grep -q "Execution succeeded"; then
        space_ready=true
        break
    fi
    sleep 2
done
if [ "$space_ready" = false ]; then
    echo "ERROR: Space 'ldbc' not ready after 60s" >&2
    exit 1
fi
echo "✓ Space 'ldbc' is ready"

# Copy schema files before using them
cp "${SCHEMA_PATH}" ${DATA_DIR}/schema.gql
cp ./data/ldbc/importer.yaml ${DATA_DIR}/importer.yaml

# Create schema
python3 ../../python/kvengines/nebula/query_run.py -f ${DATA_DIR}/schema.gql --exist-ok
echo "✓ Schema created"

# Wait for schema to propagate (instead of fixed sleep)
echo "Waiting for schema to propagate..."
schema_ready=false
for i in $(seq 1 30); do
    if ${nebula_console_cmd} -e "USE ldbc; SHOW TAGS;" 2>/dev/null | grep -q "Person"; then
        schema_ready=true
        break
    fi
    sleep 2
done
if [ "$schema_ready" = false ]; then
    echo "ERROR: Schema not ready after 60s" >&2
    exit 1
fi
echo "✓ Schema is ready"

# NebulaGraph requires ~2 heartbeat cycles (default 10s each) for schema
# to propagate from metad to all storaged nodes before writes can succeed.
echo "Waiting 20s for schema to propagate to storaged..."
sleep 20

# ========================================
# Step 2: Import data
# ========================================
echo ""
echo "Importing LDBC data..."

rm -f ${DATA_DIR}/nebula-importer.log || true
nebula-importer --config ${DATA_DIR}/importer.yaml

echo ""
echo "Import log summary:"
head -n 20 ${DATA_DIR}/nebula-importer.log || true

# ========================================
# Step 3: Update emails and languages
# ========================================
echo ""
echo "Updating emails and languages..."

python3 ../../python/kvengines/nebula/ldbc/update_emails.py --csv ${DATA_DIR}/dynamic/person_email_emailaddress_0_0.csv
python3 ../../python/kvengines/nebula/ldbc/update_languages.py --csv ${DATA_DIR}/dynamic/person_speaks_language_0_0.csv
python3 ../../python/kvengines/nebula/query_run.py -f update_emails.gql --space ldbc > /dev/null
python3 ../../python/kvengines/nebula/query_run.py -f update_languages.gql --space ldbc > /dev/null
rm -f update_emails.gql update_languages.gql

echo "✓ Emails and languages updated"

# python3 ../../python/kvengines/nebula/query_run.py -f ./data/ldbc/index.gql --space ldbc > /dev/null
# echo "✓ Indexes created and rebuilt"
# sleep 120

# ========================================
# Flush RocksDB memtables to SST
# ========================================
# During bulk import, each of the 100 partitions accumulates WAL entries.
# RocksDB auto-flushes memtables to SST when they hit write_buffer_size
# (default 64 MB), but smaller partitions may never auto-flush. A clean
# stop (nebula.service stop) does flush memtables, but the old WAL files
# written during the import remain on disk — RocksDB only prunes WAL
# files whose data has been fully flushed to SST. Flushing explicitly
# here lets RocksDB prune those WAL files before the backup copies them.
echo ""
echo "Flushing RocksDB memtables..."
FLUSH_RESULT=$(curl -s "http://127.0.0.1:19779/admin?space=ldbc&op=flush")
if echo "$FLUSH_RESULT" | grep -qi "ok"; then
    echo "✓ Memtables flushed"
else
    echo "WARNING: Flush response: $FLUSH_RESULT" >&2
fi
sleep 5

# ========================================
# Done
# ========================================
END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))

echo ""
echo "=========================================="
echo "✓ LDBC SF${SF} data loaded successfully!"
echo "Time: $((ELAPSED / 60))m $((ELAPSED % 60))s"
echo "=========================================="