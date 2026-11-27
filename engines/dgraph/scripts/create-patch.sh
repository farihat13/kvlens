#!/bin/bash
set -e

# Usage: ./create-patch.sh [dgraph|badger|all] [-f patchfile]

dgraph=false
badger=false
patchfile_dgraph="dgraph.patch"
patchfile_badger="badger.patch"

usage() {
    echo "Usage: $0 [dgraph|badger|all] [-f patchfile]"
    echo "Options:"
    echo "  dgraph   Create patch for Dgraph"
    echo "  badger   Create patch for Badger"
    echo "  all      Create patches for both Dgraph and Badger"
    echo "  -f       Specify the patch file name (default: dgraph.patch and badger.patch)"
}

if [[ $# -eq 0 ]]; then
    dgraph=true
    badger=true
else
    while [[ $# -gt 0 ]]; do
        case "$1" in
            dgraph)
                dgraph=true
                shift
                ;;
            badger)
                badger=true
                shift
                ;;
            all)
                dgraph=true
                badger=true
                shift
                ;;
            -f)
                if [[ -n "$2" ]]; then
                    patchfile_dgraph="$2"
                    patchfile_badger="$2"
                    shift 2
                else
                    echo "Error: -f requires a patch file argument."
                    exit 1
                fi
                ;;
            *)
                echo "Invalid argument."
                usage
                exit 1
                ;;
        esac
    done
fi

if $dgraph; then
    echo "Creating patch for dgraph"
    cd "${DGRAPH_SRCDIR:?DGRAPH_SRCDIR not set}"
    git add -N .
    git diff --no-prefix . > ../"$patchfile_dgraph"
    cd ..
fi

if $badger; then
    echo "Creating patch for badger"
    cd "${BADGER_SRCDIR:?BADGER_SRCDIR not set}"
    git add -N .
    git diff --no-prefix . > ../"$patchfile_badger"
    cd ..
fi