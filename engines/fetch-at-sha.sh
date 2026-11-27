# engines/fetch-at-sha.sh: fetch_at_sha <url> <sha> <dir>, sourced by each engine's get.sh.
# One commit, no history, as its own git checkout (build.sh restores it with `git -C <dir> restore .` after patching).
# A dir already at <sha> is kept; a dir at another commit, or not a checkout, is an error (never overwritten).
fetch_at_sha() {
    local url=$1 sha=$2 dir=$3 have
    if [ -d "$dir/.git" ]; then
        have=$(git -C "$dir" rev-parse HEAD)
        [ "$have" = "$sha" ] || { echo "ERROR: $dir is at $have, expected $sha" >&2; return 1; }
        echo "$dir: already at $sha"
        return 0
    fi
    [ ! -e "$dir" ] || { echo "ERROR: $dir exists and is not a git checkout" >&2; return 1; }
    git init -q "$dir"
    git -C "$dir" fetch -q --depth 1 "$url" "$sha"
    git -C "$dir" checkout -q FETCH_HEAD
    echo "$dir: fetched $sha"
}
