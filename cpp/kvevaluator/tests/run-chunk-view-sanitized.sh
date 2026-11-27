#!/usr/bin/env bash
# lists::ChunkView's two tests (test_chunk_find_paths, test_chunk_view_lazy) under ASan + UBSan, then each with a
# planted bug in the reader (it must fail): find_array off by one owner, and get_field's 8-byte fast path off by one
# bit. Builds with g++ from this tree's sources into OUT (default: a temp dir). ~2 min.
#   tests/run-chunk-view-sanitized.sh [OUT]
# Exit 0 = both clean tests pass AND every planted build fails.
set -u
cd "$(dirname "$0")/.."
OUT=${1:-$(mktemp -d)}
mkdir -p "$OUT"
echo "tree: $(git rev-parse --short=10 HEAD)$(git diff --quiet HEAD -- src tests || echo -dirty), out: $OUT"
SRCS="src/codec/columns.cc src/value/value.cc src/formats/rows.cc src/util/base64.cc"
FLAGS="-std=c++17 -O1 -g -fsanitize=address,undefined -fno-sanitize-recover=all -Isrc -Ithird_party -pthread"
TESTS="test_chunk_find_paths test_chunk_view_lazy"

# build TEST against LISTS_CC into OUT/NAME, run it; prints the test's last line, returns its rc (2 = build failed)
run() {
  local test=$1 lists=$2 name=$3
  g++ $FLAGS -Isrc/codec "tests/$test.cc" "$lists" $SRCS -o "$OUT/$name" || return 2
  "$OUT/$name" > "$OUT/$name.log" 2>&1
  local rc=$?
  echo "  $name: rc $rc: $(tail -1 "$OUT/$name.log")"
  return $rc
}

for t in $TESTS; do
  run "$t" src/codec/lists.cc "$t-clean"
  rc=$?
  [ $rc -eq 0 ] || { echo "FAIL: clean $t (rc $rc): $OUT/$t-clean.log"; exit 1; }
done

# plant NAME, sed EXPR on lists.cc; refuses when the plant no longer applies
plant() {
  local name=$1 expr=$2
  sed "$expr" src/codec/lists.cc > "$OUT/lists-$name.cc"
  if cmp -s src/codec/lists.cc "$OUT/lists-$name.cc"; then echo "FAIL: the plant $name no longer applies"; exit 2; fi
  local caught=0
  for t in $TESTS; do
    run "$t" "$OUT/lists-$name.cc" "$t-$name"
    case $? in 0) ;; 2) echo "FAIL: planted build $name"; exit 2 ;; *) caught=1 ;; esac
  done
  [ $caught -eq 1 ] || { echo "FAIL: the plant $name was not caught"; exit 1; }
  echo "plant $name: caught"
}
plant find-off-by-one 's/return it != a.end() \&\& \*it == src ? int64_t(it - a.begin()) : -1;/return it != a.end() \&\& *it == src ? int64_t(it - a.begin()) + 1 : -1;/'
plant fastpath-shift 's/    x >>= sh;/    x >>= sh + 1;/'
echo "PASS: both tests pass clean; every plant is caught"
