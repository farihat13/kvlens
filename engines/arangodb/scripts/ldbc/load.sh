#!/bin/bash
# Load LDBC SNB data into ArangoDB.
#
# Usage: bash scripts/ldbc/load.sh [--sf SCALE] [--schema PATH]   (default sf=0.3)
#
# The physical design is set with --schema PATH (or the SCHEMA_FILE env fallback): a JSON description
# of collections + edges + indexes, applied by the shared create_schema.py. (A .py path is still run
# directly, for legacy callers like the kvformat evaluator.) Default = the canonical
# data/ldbc/ldbc_schema.json, so nothing changes for `just ldbc-load`. This is the arango analogue of
# dgraph's data/ldbcTypes.schema / nebula's data/ldbc/schema.gql: the schema is declarative data in
# data/ldbc/, the applier (create_schema.py) is shared in scripts/.
#
# Steps:
#   1. Preprocess CSVs (add _key, _from, _to columns)
#   2. Create schema (drop + recreate)
#   3. Import vertex collections via arangoimport
#   4. Import edge collections via arangoimport
#   5. Update multi-value attributes (emails, languages)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SF="0.3"
# The schema applied in step 2. --schema (or SCHEMA_FILE env) overrides it; default = the canonical
# data/ldbc/ldbc_schema.json. A .json is applied by create_schema.py; a .py is run directly (legacy).
SCHEMA="${SCHEMA_FILE:-$SCRIPT_DIR/../../data/ldbc/ldbc_schema.json}"

usage() {
    cat <<EOF
Usage: $0 [--sf SCALE] [--schema PATH]
  --sf      Scale factor (default: ${SF})
  --schema  JSON schema description (or a legacy .py script); default: ldbc_schema.json
  -h|--help Show this help
EOF
}

# Flags (was positional \$1=SF; switched to match dgraph/nebula load.sh).
while [[ $# -gt 0 ]]; do
    case "$1" in
        --sf) SF="$2"; shift 2 ;;
        --schema) SCHEMA="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown arg: $1" >&2; usage >&2; exit 1 ;;
    esac
done

ROOT="${MY_ARANGO_ROOTDIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
ENGINES_PYTHON="${ENGINES_PYTHON:-${ROOT}/../../.venv/bin/python3}"     # the repo venv (just setup)
ARANGOIMPORT="${ARANGO_RUNSDIR}/bin/arangoimport"
PORT="${ARANGO_PORT:-8529}"

RAW_DIR="${ROOT}/data/ldbc/sf${SF}/CsvBasic-LongDateFormatter"
PROCESSED_DIR="${ROOT}/data/ldbc/sf${SF}/CsvBasic-LongDateFormatter_arangodb"

if [ ! -d "$RAW_DIR" ]; then
    echo "ERROR: Raw data not found: $RAW_DIR"
    echo "Run: just ldbc-download ${SF}"
    exit 1
fi

if [ ! -f "$SCHEMA" ]; then
    echo "ERROR: schema not found: $SCHEMA"
    exit 1
fi

echo "============================================"
echo "Loading LDBC sf${SF} into ArangoDB"
echo "============================================"

# -----------------------------------------------
# Step 1: Preprocess CSVs
# -----------------------------------------------
echo -e "\n=== Step 1/5: Preprocessing CSVs ==="
$ENGINES_PYTHON "$SCRIPT_DIR/../../../../python/kvengines/arangodb/ldbc/preprocess_csvs.py" "$RAW_DIR" "$PROCESSED_DIR"

# -----------------------------------------------
# Step 2: Create schema (drop + recreate)
# -----------------------------------------------
echo -e "\n=== Step 2/5: Creating schema ==="
echo "  schema: $SCHEMA"
case "$SCHEMA" in
    *.json) "$ENGINES_PYTHON" "$SCRIPT_DIR/../../../../python/kvengines/arangodb/ldbc/create_schema.py" --desc "$SCHEMA" ;;
    *)      "$ENGINES_PYTHON" "$SCHEMA" ;;   # legacy: a runnable schema script (e.g. the kvformat evaluator's)
esac

# -----------------------------------------------
# Step 3: Import vertex collections
# -----------------------------------------------
echo -e "\n=== Step 3/5: Importing vertex collections ==="

import_collection() {
    local file="$1"
    local collection="$2"

    if [ ! -f "$file" ]; then
        echo "  SKIP (not found): $file"
        return
    fi

    echo "  Importing $collection from $(basename "$file")..."
    # Capture the output: arangoimport exits 0 even when individual records fail (it reports them as
    # "warnings/errors: N"), so `set -e` alone would let a PARTIAL import look like a clean load — and
    # that store then becomes a backup. Tee so the progress output still reaches the log.
    local out
    out=$("$ARANGOIMPORT" \
        --configuration none \
        --file "$file" \
        --type csv \
        --separator "|" \
        --collection "$collection" \
        --server.database ldbc \
        --server.endpoint "tcp://127.0.0.1:${PORT}" \
        --server.authentication false \
        --on-duplicate update \
        --progress true 2>&1 | tee /dev/stderr)

    local errs
    errs=$(echo "$out" | sed -n 's/^warnings\/errors: *\([0-9]*\).*/\1/p' | tail -1)
    if [ -n "$errs" ] && [ "$errs" -ne 0 ]; then
        echo "ERROR: arangoimport reported ${errs} warnings/errors importing ${collection} from ${file}." >&2
        echo "  The store is now PARTIAL — do not back it up. Fix the input and re-run the load." >&2
        exit 1
    fi
}

# Vertices
import_collection "$PROCESSED_DIR/static/place_0_0.csv"         Place
import_collection "$PROCESSED_DIR/static/organisation_0_0.csv"  Organisation
import_collection "$PROCESSED_DIR/static/tag_0_0.csv"           Tag
import_collection "$PROCESSED_DIR/static/tagclass_0_0.csv"      TagClass
import_collection "$PROCESSED_DIR/dynamic/person_0_0.csv"       Person
import_collection "$PROCESSED_DIR/dynamic/post_0_0.csv"         Post
import_collection "$PROCESSED_DIR/dynamic/comment_0_0.csv"      Comment
import_collection "$PROCESSED_DIR/dynamic/forum_0_0.csv"        Forum

# -----------------------------------------------
# Step 4: Import edge collections
# -----------------------------------------------
echo -e "\n=== Step 4/5: Importing edge collections ==="

# Static edges
import_collection "$PROCESSED_DIR/static/place_isPartOf_place_0_0.csv"              isPartOf
import_collection "$PROCESSED_DIR/static/organisation_isLocatedIn_place_0_0.csv"    isLocatedIn
import_collection "$PROCESSED_DIR/static/tag_hasType_tagclass_0_0.csv"              hasType
import_collection "$PROCESSED_DIR/static/tagclass_isSubclassOf_tagclass_0_0.csv"    isSubclassOf

# Dynamic edges
import_collection "$PROCESSED_DIR/dynamic/person_knows_person_0_0.csv"              knows
import_collection "$PROCESSED_DIR/dynamic/person_likes_post_0_0.csv"                likes
import_collection "$PROCESSED_DIR/dynamic/person_likes_comment_0_0.csv"             likes
import_collection "$PROCESSED_DIR/dynamic/person_hasInterest_tag_0_0.csv"           hasInterest
import_collection "$PROCESSED_DIR/dynamic/person_studyAt_organisation_0_0.csv"      studyAt
import_collection "$PROCESSED_DIR/dynamic/person_workAt_organisation_0_0.csv"       workAt
import_collection "$PROCESSED_DIR/dynamic/person_isLocatedIn_place_0_0.csv"         isLocatedIn
import_collection "$PROCESSED_DIR/dynamic/post_isLocatedIn_place_0_0.csv"           isLocatedIn
import_collection "$PROCESSED_DIR/dynamic/comment_isLocatedIn_place_0_0.csv"        isLocatedIn
import_collection "$PROCESSED_DIR/dynamic/post_hasCreator_person_0_0.csv"           hasCreator
import_collection "$PROCESSED_DIR/dynamic/comment_hasCreator_person_0_0.csv"        hasCreator
import_collection "$PROCESSED_DIR/dynamic/comment_replyOf_post_0_0.csv"             replyOf
import_collection "$PROCESSED_DIR/dynamic/comment_replyOf_comment_0_0.csv"          replyOf
import_collection "$PROCESSED_DIR/dynamic/forum_containerOf_post_0_0.csv"           containerOf
import_collection "$PROCESSED_DIR/dynamic/forum_hasMember_person_0_0.csv"           hasMember
import_collection "$PROCESSED_DIR/dynamic/forum_hasModerator_person_0_0.csv"        hasModerator
import_collection "$PROCESSED_DIR/dynamic/forum_hasTag_tag_0_0.csv"                 hasTag
import_collection "$PROCESSED_DIR/dynamic/comment_hasTag_tag_0_0.csv"               hasTag
import_collection "$PROCESSED_DIR/dynamic/post_hasTag_tag_0_0.csv"                  hasTag

# -----------------------------------------------
# Step 5: Update multi-value attributes
# -----------------------------------------------
echo -e "\n=== Step 5/5: Updating emails and languages ==="
$ENGINES_PYTHON "$SCRIPT_DIR/../../../../python/kvengines/arangodb/ldbc/update_emails_languages.py" "$RAW_DIR"

echo -e "\n============================================"
echo "LDBC sf${SF} load complete!"
echo "============================================"
