#!/usr/bin/env bash
# kvevaluator under `numactl --interleave=all` with a memory ceiling: the drop-in binary for a large run.
#   point kvevaluator.paths' binary location (cpp_bin, see `python -m kvevaluator.paths`) at this script
# Why: on a machine with 4 NUMA nodes of ~48 GB, a single-node process is OOM-killed at that node's limit while the machine
# has 100+ GB free (the sf3 level-wise chase on dgraph died at 50.8 GB anon RSS, twice). Interleaving
# spreads the pages over the nodes; `ulimit -v` makes the process fail on its own, with a clear error, instead of the
# kernel picking a victim (another job on the same machine). Set KVEVAL_CPP_VLIMIT_KB to change the
# ceiling (default 120 GB of virtual memory). The binary run is <build dir>/cpp/kvevaluator (`just build`).
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
BIN="$("$REPO/.venv/bin/python" -m kvevaluator.paths build_dir)/cpp/kvevaluator"
ulimit -v "${KVEVAL_CPP_VLIMIT_KB:-125829120}"
if command -v numactl >/dev/null 2>&1; then
  exec numactl --interleave=all "$BIN" "$@"
fi
exec "$BIN" "$@"
