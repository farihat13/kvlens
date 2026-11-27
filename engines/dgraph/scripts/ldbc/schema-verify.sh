#!/bin/bash
set -euo pipefail

# Retry settings — schema propagation can take time after /alter
MAX_RETRIES=5
RETRY_DELAY=3

keywords=( person post comment knows hasModerator isSubclassOf )

for attempt in $(seq 1 $MAX_RETRIES); do
    schema_text=$(just getschema)
    all_found=true
    missing=""

    for kw in "${keywords[@]}"; do
        if ! echo "$schema_text" | grep -q "$kw"; then
            all_found=false
            missing="$kw"
            break
        fi
    done

    if $all_found; then
        echo "Schema verification passed."
        exit 0
    fi

    if [ "$attempt" -lt "$MAX_RETRIES" ]; then
        echo "Waiting for schema propagation (attempt $attempt/$MAX_RETRIES, missing: $missing)..."
        sleep $RETRY_DELAY
    fi
done

echo "ERROR: '$missing' not found in loaded schema after $MAX_RETRIES attempts"
exit 1