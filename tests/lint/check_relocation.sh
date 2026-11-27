#!/usr/bin/env bash
# A clone of this repo's HEAD at another path (with a space in it), set up under an empty environment, finds its own
# root and imports. The clone is kept and reused at <output>/relocation/a clone.
#     tests/lint/check_relocation.sh
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(git -C "$here" rev-parse --show-toplevel)"
out="$(cd "$repo" && uv run --locked -q python -m kvevaluator.paths output_dir)"
clone="$out/relocation/a clone"
head="$(git -C "$repo" rev-parse HEAD)"
if [[ -d "$clone/.git" ]]; then
    git -C "$clone" fetch -q "$repo" HEAD
    git -C "$clone" checkout -q --detach FETCH_HEAD
else
    mkdir -p "$out/relocation"
    git clone -q "$repo" "$clone"
    git -C "$clone" checkout -q --detach "$head"
fi
uv="$(command -v uv)"
run() { env -i PATH="$(dirname "$uv"):/usr/bin:/bin" HOME="$HOME" "$@"; }
(cd "$clone" && run "$uv" sync --locked -q)
root="$(cd "$clone" && run "$uv" run --locked -q python -m kvevaluator.paths | awk -F'\t' '$1 == "root" {print $2}')"
if [[ "$root" != "$clone" ]]; then
    echo "check_relocation FAIL: the clone at '$clone' reports root '$root'"
    exit 1
fi
(cd "$clone" && run "$uv" run --locked -q python -I tests/lint/check_import_all.py) | tail -n 1
(cd / && run "$clone/.venv/bin/python" -I -c "import kvevaluator.paths as p, sys; sys.exit(str(p.ROOT) != sys.argv[1])" "$clone")
echo "check_relocation OK: HEAD ${head:0:10} cloned at '$clone' (env -i) finds its root, from its own dir and from /"
