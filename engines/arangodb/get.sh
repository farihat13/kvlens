#!/usr/bin/env bash
# get.sh: fetch the ArangoDB source our patch applies to (v3.12.8, with its submodules) at an exact commit.
# Large (several GB with submodules). Then: bash build.sh (applies patches/, builds bin/arangod).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ../fetch-at-sha.sh
fetch_at_sha https://github.com/arangodb/arangodb.git 63654bf292d393cb6ffaadde1f4c33f410231496 arangodb-3.12.8   # v3.12.8
git -C arangodb-3.12.8 submodule update -q --init --recursive --depth 1 --jobs 8
