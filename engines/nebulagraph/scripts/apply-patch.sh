#!/bin/bash
set -euo pipefail

# Usage: ./apply-patch.sh [nebula|rocksdb|all] [-f patchfile]

nebula=false
rocksdb=false
patchfile_nebula="patches/nebula.patch"
patchfile_rocksdb="patches/rocksdb.patch"

usage() {
    echo "Usage: $0 [nebula|rocksdb|all] [-f patchfile]"
    echo "Options:"
    echo "  nebula   Apply patch for NebulaGraph"
    echo "  rocksdb  Apply patch for RocksDB"
    echo "  all      Apply patches for both NebulaGraph and RocksDB"
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
                echo "Invalid argument. Use 'nebula', 'rocksdb', 'all', or '-f patchfile'."
                usage
                exit 1
                ;;
        esac
    done
fi

if $nebula; then
    echo "Applying patch for nebula"
    rm -rf "${MY_NEBULA_SRCDIR:?MY_NEBULA_SRCDIR not set}"/src
    git -C "${MY_NEBULA_SRCDIR}" restore . || { echo "ERROR: git restore ${MY_NEBULA_SRCDIR} failed"; exit 1; }
    patch -p1 < "$patchfile_nebula"
fi

if $rocksdb; then
    if [[ ! -f "$patchfile_rocksdb" ]]; then
        echo "WARNING: $patchfile_rocksdb not found, skipping rocksdb patch"
    else
        echo "Applying patch for rocksdb"
        git -C "${MY_ROCKS_SRCDIR:?MY_ROCKS_SRCDIR not set}" clean -fdq   # its own checkout: never delete the dir (and its .git)
        git -C "${MY_ROCKS_SRCDIR}" restore . || { echo "ERROR: git restore ${MY_ROCKS_SRCDIR} failed"; exit 1; }
        patch -p1 < "$patchfile_rocksdb"
    fi
fi
