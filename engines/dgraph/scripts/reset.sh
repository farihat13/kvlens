#!/bin/bash
# Kill dgraph and remove all data directories + logs (fresh slate).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../.env"
bash "${SCRIPT_DIR}/kill.sh"    # exits nonzero if a process survived; set -e then aborts before the wipe
mkdir -p "${DGRAPH_RUNSDIR}" "${DGRAPH_LOGDIR}"   # gitignored: absent in a fresh clone
cd "${DGRAPH_RUNSDIR}" && rm -rf ./p ./w ./zw ./t
cd "${DGRAPH_LOGDIR}" && rm -f *
