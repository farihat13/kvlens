#!/bin/bash
# Build dgraph from source (make install); the binary lands in $GOPATH/bin.
# Args: extra make flags (e.g. BUILD_TAGS=kvstats). set -o pipefail so a failed make
# is not hidden by tee's exit status.
set -o pipefail
cd "${DGRAPH_SRCDIR}" && make install "$@" |& tee build.log
