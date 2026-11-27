#!/bin/bash
# engines/ldbc-params-dir.sh <sf> -- print the dir of LDBC's substitution parameters for one SF, the one each engine's
# `just ldbc-download` links as data/ldbc/substitution_parameters-sf<SF>: LDBC's download
# (${LDBC_SNB_DATA_DIR}/substitution_parameters-sf<SF>) when present, else the artifact's tracked copy
# (inputs/params/ldbc/sf<SF>/interactive: LDBC's hosted files copied verbatim, the ones python/kvevaluator/datasets/ldbc/params.py
# reads; `just fetch-dataset` downloads no substitution parameters for sf0.3 and sf1). Exit 1 when neither exists.
set -euo pipefail
sf=${1:?usage: ldbc-params-dir.sh <sf>}
hosted="${LDBC_SNB_DATA_DIR:-}/substitution_parameters-sf${sf}"
tracked="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/inputs/params/ldbc/sf${sf}/interactive"
if [ -n "${LDBC_SNB_DATA_DIR:-}" ] && [ -d "$hosted" ]; then
    echo "$hosted"
elif [ -f "$tracked/interactive_1_param.txt" ]; then
    echo "$tracked"
else
    echo "ldbc-params-dir.sh: no substitution parameters for sf${sf}: neither $hosted nor $tracked" >&2
    exit 1
fi
