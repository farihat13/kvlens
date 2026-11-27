#!/usr/bin/env bash
# run/check-build-tools.sh -- does this machine have what `just build` and `just fetch-dataset` need? `just build` runs it
# first. Silent when everything is there; otherwise one line per missing tool or library, and exit 1.
#   bash run/check-build-tools.sh [--packages]   --packages: the apt packages it checks (equal to the README's Needs block)
set -u
# <command>:<apt package>, then <header or file>:<apt package>
CMDS="git:git curl:curl python3:python3 cmake:cmake g++:build-essential make:build-essential unzstd:zstd"   # unzstd: LDBC's fetch unpacks .tar.zst
LIBS="snappy.h:libsnappy-dev zlib.h:zlib1g-dev bzlib.h:libbz2-dev lz4.h:liblz4-dev zstd.h:libzstd-dev"
TCMALLOC="/usr/lib/$(uname -m)-linux-gnu/libtcmalloc_minimal.a:libgoogle-perftools-dev"   # the file CMakeLists.txt links

if [ "${1:-}" = --packages ]; then
  for x in $CMDS $LIBS $TCMALLOC; do echo "${x##*:}"; done | sort -u
  exit 0
fi

missing=""
for x in $CMDS; do command -v "${x%%:*}" > /dev/null || missing="$missing ${x%%:*}"; done
if command -v g++ > /dev/null; then   # a header is checked by compiling it; without g++ that is already reported
  for x in $LIBS; do echo "#include <${x%%:*}>" | g++ -x c++ -fsyntax-only - 2> /dev/null || missing="$missing ${x%%:*}"; done
fi
[ -f "${TCMALLOC%%:*}" ] || missing="$missing libtcmalloc_minimal.a"

[ -z "$missing" ] && exit 0
for m in $missing; do echo "missing: $m"; done
echo "Install them with the Needs block in README.md"
exit 1
