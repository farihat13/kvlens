#!/bin/bash
# run-latency-stability-under-load.sh -- test_latency_stability N times under a synthetic load (owner: cpp/kvevaluator
# tests: it must pass when a load comes between its fresh and after windows).
# Usage: tests/run-latency-stability-under-load.sh BINARY LOAD [N, default 5]
#   BINARY  a kvevaluator_tests build (build/cpp/kvevaluator_tests, or a glibc build's; it runs its case
#           test_latency_stability); the tests are not in the default build: `cmake --build build/cpp --target
#           kvevaluator_tests` (or run ctest once)
#   LOAD    step13  2 stress-ng --stream threads on the test CPU's CCD from 1.3 s into each run (between the old
#                   test's two windows)
#           square  the same 2 threads throughout, stopped and resumed (SIGSTOP / SIGCONT) every 0.7 s
#           steady  stress-ng --cpu 16 --stream 8 on LOAD_WIDE (test CPU included) for the whole run
#           none    no load
# TEST_CPU (default 18) and LOAD_CPUS (default 17,49: the same CCD on the measurement machine's EPYC 9354P, 4 cores per CCD) and
# LOAD_WIDE (default 8-31,40-63: NUMA nodes 1-3) are environment settings: the load must share the test CPU's L3.
# Prints one line per run (rc and the three cells' ratios) and "SUMMARY ... K of N passed"; logs in OUT (default a
# new dir under /tmp). Each load runs in its own process group and is killed by group, never by name. About 10 s a run.
BIN=${1:?binary}; LOAD=${2:?load}; N=${3:-5}
[ -x "$BIN" ] || { echo "no binary $BIN: build it first (cmake --build <build dir> --target kvevaluator_tests)" >&2; exit 2; }
TEST_CPU=${TEST_CPU:-18}; LOAD_CPUS=${LOAD_CPUS:-17,49}; LOAD_WIDE=${LOAD_WIDE:-8-31,40-63}
OUT=${OUT:-$(mktemp -d /tmp/latency-stability-load-XXXXXX)}
command -v stress-ng >/dev/null || { echo "no stress-ng on this machine" >&2; exit 2; }
pass=0
for i in $(seq 1 "$N"); do
  case $LOAD in
    step13) setsid bash -c "sleep 1.3; taskset -c $LOAD_CPUS timeout 12 stress-ng --stream 2 --quiet" >/dev/null 2>&1 & LP=$! ;;
    square) setsid bash -c "setsid taskset -c $LOAD_CPUS timeout 16 stress-ng --stream 2 --quiet & s=\$!; "'trap "kill -CONT -- -$s 2>/dev/null; kill -- -$s 2>/dev/null; exit" TERM; sleep 0.3; end=$((SECONDS+14)); while (( SECONDS < end )); do sleep 0.7; kill -STOP -- -$s; sleep 0.7; kill -CONT -- -$s; done; kill -CONT -- -$s; kill -- -$s' >/dev/null 2>&1 & LP=$! ;;
    steady) setsid bash -c "taskset -c $LOAD_WIDE timeout 14 stress-ng --cpu 16 --stream 8 --quiet" >/dev/null 2>&1 & LP=$!; sleep 1 ;;
    none)   LP= ;;
    *)      echo "unknown load $LOAD" >&2; exit 2 ;;
  esac
  f=$OUT/$LOAD-$i.log
  ( cd /tmp && taskset -c "$TEST_CPU" "$BIN" test_latency_stability > "$f" 2>&1 ); rc=$?
  [ -n "$LP" ] && { kill -- -"$LP" 2>/dev/null; wait "$LP" 2>/dev/null; }   # the load's own process group only
  sleep 0.5
  [ $rc = 0 ] && pass=$((pass + 1))
  echo "$LOAD run $i rc=$rc $(grep -o 'seek.*ratio [0-9.]*\|copy.*ratio [0-9.]*\|flow.*ratio [0-9.]*' "$f" | sed 's/ *fresh.*ratio / /' | tr '\n' ';')"
done
echo "SUMMARY $LOAD: $pass of $N passed ($BIN; logs in $OUT)"
