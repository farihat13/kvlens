#!/usr/bin/env bash
# run/build-cpp.sh -- build the C++ evaluator binary, the way `just build` does (run/build_cpp.py).
#
#   run/build-cpp.sh            # build into the machine's build cache once per source state (reused by every worktree
#                               # with the same C++), then hard-link it into $BUILD_DIR/cpp/kvevaluator. Cheap to call
#                               # as a prerequisite step: an unchanged tree is a lookup.
#   run/build-cpp.sh --rebuild  # wipe $BUILD_DIR/cpp and build in place, linking the shared RocksDB (a corrupt build
#                               # dir, or to compare with a cache build). --clean is an alias.
# A tree with uncommitted C++ (or schema, spec) changes always builds in place, incrementally.
#   JOBS=8 run/build-cpp.sh     # override parallelism (default: nproc).
#
# The binary is what `--backend cpp` drives (python/kvevaluator/measure/server.py). The build dir is $BUILD_DIR/cpp
# (KVEVAL_BUILD_DIR, env or .env); the cache is <main clone>/build/cache (kvevaluator.paths.build_cache_dir()).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../run/env.sh"

BUILD="$BUILD_DIR/cpp"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 4)}"
bash "$(dirname "${BASH_SOURCE[0]}")/check-build-tools.sh"   # a missing tool stops here by name

case "${1:-}" in
    --rebuild|--clean) echo "[build-cpp] $1: wiping $BUILD"; rm -rf "$BUILD"
                       exec "$PYTHON" run/build_cpp.py --in-place --jobs "$JOBS" ;;
    "") exec "$PYTHON" run/build_cpp.py --jobs "$JOBS" ;;
    -h|--help) awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next} {exit}' "$0"; exit 0 ;;
    *) echo "[build-cpp] unknown flag '$1' (use --rebuild, or no flag)" >&2; exit 2 ;;
esac
