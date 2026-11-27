#!/bin/bash
set -eu
# schema_path is the first argument or defaults to data/ldbcTypes.schema
schema_path="${1:-data/ldbcTypes.schema}"
curl -X POST localhost:8080/alter \
    -H "Content-Type: application/dql" \
    --data-binary @"$schema_path" | jq
echo "Schema set from ${schema_path}."