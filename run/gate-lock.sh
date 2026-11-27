# run/gate-lock.sh -- one gate round per store root at a time (sourced, like run/env.sh; used by run/meta-gates.sh and
# run/predict-gate.sh).
#
# Two rounds on one machine open the same RocksDB stores; the second one's open collides on a store's LOCK partway
# through and fails a stage for no fault of the code. So a round takes a lock on its SF's store root first, with
# `flock -n`: a busy root fails at once, naming the round that holds it.
#
#   source run/gate-lock.sh
#   gate_lock "$SF" meta-gates             # <store root>/sf<SF>/.gate.lock, held until this shell exits
#
# The store root is stores.cpp_stores_root (KVEVAL_STORE_DIR moves it); each SF has its own lock. A round started
# from inside a round that holds the same lock (KVEVAL_GATE_LOCK_HELD) runs under its parent's lock.

gate_lock() {   # gate_lock <sf> <round name>: take the store root's lock for this shell, or exit 3 when it is busy
  local sf="$1" name="$2" root lock
  root=$("$PYTHON" -c 'import sys; from kvevaluator.measure.stores import scale_root
print(scale_root("ldbc", sys.argv[1]))' "$sf")
  lock="${root%/}/.gate.lock"
  if [[ "${KVEVAL_GATE_LOCK_HELD:-}" == "$lock" ]]; then
    echo "$name: under the parent round's lock $lock"
    return 0
  fi
  mkdir -p "$root"
  exec 9>>"$lock"                     # append: the holder's line stays readable until we hold the lock ourselves
  if ! flock -n 9; then
    echo "REFUSED: $name: another gate round holds $lock: $(tail -n 1 "$lock" 2>/dev/null)" >&2
    echo "  two rounds on one store root collide on the stores' LOCK files; wait for it, or run this one with" >&2
    echo "  KVEVAL_STORE_DIR=<another root> (its own stores)" >&2
    exit 3
  fi
  : > "$lock"                         # ours now: say who holds it
  echo "$name pid $$ since $(date -u +%Y-%m-%dT%H:%M:%SZ) in $(pwd)" >&9
  export KVEVAL_GATE_LOCK_HELD="$lock"
  echo "$name: holds $lock"
}
