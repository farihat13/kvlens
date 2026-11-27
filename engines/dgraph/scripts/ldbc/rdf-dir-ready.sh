#!/bin/bash
# rdf-dir-ready.sh DIR: exit 0 if DIR holds real *.rdf files and no Git LFS placeholders, else exit 1.
# The dgraph-benchmarks checkout ships its published RDF as LFS placeholders (small text files starting
# "version https://git-lfs.github.com/spec/v1"), so "the directory exists" does not mean "the data is there".
set -u
dir="${1:?usage: rdf-dir-ready.sh DIR}"
real=0
lfs=0
for f in "$dir"/*.rdf; do
    [ -f "$f" ] || continue
    if head -c 64 "$f" | grep -q '^version https://git-lfs.github.com/spec/v1'; then
        lfs=$((lfs + 1))
        echo "LFS placeholder: $f" >&2
    else
        real=$((real + 1))
    fi
done
echo "$dir: $real real .rdf files, $lfs LFS placeholders" >&2
[ "$real" -gt 0 ] && [ "$lfs" -eq 0 ]
