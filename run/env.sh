#!/bin/bash
# run/env.sh: the shell drivers' one setup. A driver's first command sources it relative to itself:
#   source "$(dirname "${BASH_SOURCE[0]}")/../run/env.sh"         # a driver in run/
#   source "$(dirname "${BASH_SOURCE[0]}")/../../run/env.sh"      # a driver in experiments/<result>/
# It cds to the repo root and sets $PYTHON (the venv `just setup` made: $UV_PROJECT_ENVIRONMENT, else .venv), SF, and the four locations
# (DATA_DIR, OUTPUT_DIR, BUILD_DIR, CPP_BIN) as python/kvevaluator/paths.py resolves them (env or .env), the
# data dir's one part: DATASETS_DIR (<data>/datasets: ldbc/, imdb/), and STORE_DIR (the stores: <output>/stores, or
# KVEVAL_STORE_DIR). And in_tree, below.
set -a
cd "$(dirname "${BASH_SOURCE[0]}")/.." || return 1     # this file is <root>/run/env.sh
PYTHON="${UV_PROJECT_ENVIRONMENT:-$PWD/.venv}/bin/python3"   # the venv uv uses: UV_PROJECT_ENVIRONMENT (relative = to the root), else .venv
if [ ! -x "$PYTHON" ]; then
  echo "run/env.sh: no $PYTHON; run \`just setup\` first" >&2
  set +a
  return 1
fi
_env_paths=$("$PYTHON" -m kvevaluator.paths) || { set +a; return 1; }
# the venv must import this tree's code (kvevaluator and kvcost share python/): another tree's venv runs its code
_root=$(sed -n 's/^root\t//p' <<< "$_env_paths")
if [ "$(realpath "$_root")" != "$(realpath "$PWD")" ]; then
  echo "run/env.sh: $PYTHON imports kvevaluator and kvcost from $_root, not from this tree ($PWD): give this tree its own venv (\`just setup\` here)" >&2
  set +a
  return 1
fi
unset _root
DATA_DIR=$(sed -n 's/^data_dir\t//p' <<< "$_env_paths")
OUTPUT_DIR=$(sed -n 's/^output_dir\t//p' <<< "$_env_paths")
BUILD_DIR=$(sed -n 's/^build_dir\t//p' <<< "$_env_paths")
CPP_BIN=$(sed -n 's/^cpp_bin\t//p' <<< "$_env_paths")
DATASETS_DIR=$(sed -n 's/^datasets_dir\t//p' <<< "$_env_paths")
STORE_DIR=$(sed -n 's/^store_dir\t//p' <<< "$_env_paths")
STATS_DIR=$(sed -n 's/^stats_dir\t//p' <<< "$_env_paths")
SPECS_DIR=$(sed -n 's/^specs_dir\t//p' <<< "$_env_paths")
unset _env_paths
# The measured operating point. SF is overridable: `export SF=0.3` before sourcing.
SF="${SF:-0.1}"
KVBENCH_NPARAM="${KVBENCH_NPARAM:-35}"
set +a
# in_tree <dir> <command...>: run the command with the output tree <dir> (KVEVAL_OUTPUT_DIR: a kvcost command writes
# there), its stores, stats and generated specs still this tree's (python/kvevaluator/output_tree.py own_tree:
# KVEVAL_STORE_DIR, KVEVAL_STATS_DIR and KVEVAL_SPECS_DIR)
in_tree() {
  local dir; dir=$("$PYTHON" -m kvevaluator.output_tree own-tree --root "$1") || return 1; shift
  KVEVAL_OUTPUT_DIR="$dir" KVEVAL_STORE_DIR="$STORE_DIR" KVEVAL_STATS_DIR="$STATS_DIR" KVEVAL_SPECS_DIR="$SPECS_DIR" "$@"
}
# tree_of <place>: the root of the tree whose captures/<dataset>/<scale> (or composed/) place <place> is (what --from names)
tree_of() { "$PYTHON" -m kvevaluator.output_tree tree-of --root "$1"; }
