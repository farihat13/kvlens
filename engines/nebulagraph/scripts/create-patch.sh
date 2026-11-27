#!/bin/bash
set -euo pipefail

# Usage: ./create-patch.sh [nebula|rocksdb|all] [-f patchfile]

nebula=false
rocksdb=false
patchfile_nebula="patches/nebula.patch"
patchfile_rocksdb="patches/rocksdb.patch"

usage() {
    echo "Usage: $0 [nebula|rocksdb|all] [-f patchfile]"
    echo "Options:"
    echo "  nebula   Create patch for NebulaGraph"
    echo "  rocksdb  Create patch for RocksDB"
    echo "  all      Create patches for both NebulaGraph and RocksDB"
    echo "  -f       Specify the patch file (default: patches/nebula.profile.patch and patches/rocksdb.patch)"
}

if [[ $# -eq 0 ]]; then
    nebula=true
    rocksdb=true
else
    while [[ $# -gt 0 ]]; do
        case "$1" in
            nebula)
                nebula=true
                shift
                ;;
            rocksdb)
                rocksdb=true
                shift
                ;;
            all)
                nebula=true
                rocksdb=true
                shift
                ;;
            -f)
                if [[ -n "${2:-}" ]]; then
                    patchfile_nebula="$2"
                    patchfile_rocksdb="$2"
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

if $nebula; then
    echo "Creating patch for nebula"
    cd "${MY_NEBULA_SRCDIR:?MY_NEBULA_SRCDIR not set}"
    git add -N .
    git diff --no-prefix . > ../"$patchfile_nebula"
    cd ..
fi

if $rocksdb; then
    echo "Creating patch for rocksdb"
    cd "${MY_ROCKS_SRCDIR:?MY_ROCKS_SRCDIR not set}"
    git add -N .
    git diff --no-prefix . > ../"$patchfile_rocksdb"
    cd ..
fi
