#!/bin/bash
set -e

# Usage: ./apply-patch.sh [dgraph|badger|all] [-f patchfile]

dgraph=false
badger=false
patchfile_dgraph="dgraph.patch"
patchfile_badger="badger.patch"


usage() {
    echo "Usage: $0 [dgraph|badger|all] [-f patchfile]"
    echo "Options:"
    echo "  dgraph   Apply patch for Dgraph"
    echo "  badger   Apply patch for Badger"
    echo "  all      Apply patches for both Dgraph and Badger"
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
                echo "Invalid argument. Use 'dgraph', 'badger', 'all', or '-f patchfile'."
                exit 1
                ;;
        esac
    done
fi

if $dgraph; then
    echo "Applying patch for dgraph"
    # restore resets tracked files; clean removes the files an earlier patch created (no build dir lives in the source)
    git -C dgraph-25.0.0 restore . || { echo "ERROR: git restore dgraph-25.0.0 failed"; exit 1; }
    git -C dgraph-25.0.0 clean -fdq || { echo "ERROR: git clean dgraph-25.0.0 failed"; exit 1; }
    patch --forward --batch -p1 < "$patchfile_dgraph"   # never prompts; an already-applied patch fails
fi

if $badger; then
    echo "Applying patch for badger"
    git -C badger restore . || { echo "ERROR: git restore badger failed"; exit 1; }
    git -C badger clean -fdq || { echo "ERROR: git clean badger failed"; exit 1; }
    patch --forward --batch -p1 < "$patchfile_badger"
fi