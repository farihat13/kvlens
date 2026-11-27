#!/bin/bash
# ============================================================
# Kill ArangoDB and wipe all data (fresh slate)
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../.env"

bash "${SCRIPT_DIR}/kill.sh"
rm -rf "${ARANGO_RUNSDIR}/data"
rm -f "${ARANGO_RUNSDIR}"/*.log
echo "Wiped ${ARANGO_RUNSDIR}/data and logs"
