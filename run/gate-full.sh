#!/usr/bin/env bash
# run/gate-full.sh -- `just gate full`: every check a merge needs, on this tree's clean commit, in one fixed order; a
# pass writes one record, which the merge check requires before a merge.
#
#   just gate full            (= run/gate-full.sh)            ~18 min on a worktree with its stores, ~25 min fresh
#   just gate analysis        (= run/gate-full.sh --level analysis)   build, binary and check only (~5 min): its
#                             record says "level": "analysis", and the merge check takes it only for a branch whose
#                             every changed path is in run/gate_levels.py's ANALYSIS_ONLY (paper/, docs/, Markdown)
#   just gate analysis-code   (= run/gate-full.sh --level analysis-code)   build, binary, check, capture, then a partial
#                             round (~12 min with a capture to reuse, ~18 without): its record says
#                             "level": "analysis-code", and the merge check takes it only for a branch whose every
#                             changed path is in ANALYSIS_CODE_ONLY (also python/kvcost, experiments/, tests/unit/)
#
# Refused at once (exit 2), before anything runs: a tree with any change (tracked or untracked, ignored files aside);
# another `full` on this clone (build/gate-records/.full.lock, the holder named); KVEVAL_CPP_BIN set (the binary must
# be the build dir's, made from this commit); KVEVAL_ALLOW_STALE_CPP set; a venv without the engines and lint extras
# (fix: just setup).
# The stages, through run/chain-stages.sh; the first failure prints GATE_FULL_FAIL <stage> and exits 1, no record:
#   build          just build (the build cache relinks this commit's binary)
#   binary         its commit is HEAD and it is clean
#   preflight      the gate stores and data, built when missing (gate_preflight.py --prep: ~7 min on a fresh output
#                  dir), with the tallies of the layouts a gate predicts from an output tree of its own
#                  (`predict sizes`), the record cache's key, the round's own refusals
#                  (run/meta-gates.sh --preflight: the store root's lock, memory, disk, stale stores)
#   check          just check (lint, import-all, relocation, ctest, tests-fast, golden-costs); lint reads tracked files
#   records        only when the record cache's key moved: the cache dumped again (the old one is set aside)
#   golden-counts  just golden-counts
#   tally-version  just tally-version
#   controls       run/meta-build-controls.sh (the known-bad binaries; a lookup once built)
#   round          run/meta-gates.sh --sf 0.1 --controls <them> (all 14 streams: 13 side by side, then the latency test alone), verdict line META_GATES_DONE
# The analysis-code level, after check:
#   records        as in full: the sf0.1 record cache the round's predict gates read, dumped again if its key moved
#   stores         the one named store its round opens (stream yc's chooser gate), made if missing
#                  (tests/gates/gate_preflight.py --gate chooser --prep); stream k builds its cell stores itself
#   tallies        the tallies `predict sizes` reads, made if missing
#                  (tests/gates/gate_preflight.py --gate predict-sizes --prep)
#   capture        the newest full pass record on this clone whose capture key (run/gate_levels.py capture_key: the git
#                  trees the measured rows are made from) is this commit's and whose capture still holds the files its
#                  record's digest names: its measured rows are copied (cp -a: a real copy, the source is never written)
#                  into this gate's logs dir and the copy's digest checked; with none, the round measures its own
#                  (streams g and m), said in this stage's log
#   round          run/meta-gates.sh --sf 0.1 --only zn,ps,yc,d,k --capture <the copy> (or zn,ps,yc,g,m,k),
#                  verdict line META_GATES_PARTIAL_DONE: z's store-free gates, `predict sizes`, y's chooser gate, the copy's check and
#                  compose steps on this commit's kvcost (kvcost measure --check-only, kvcost compose), and every composition and search
#                  gate of stream k
# Then the record: <main clone>/build/gate-records/<commit>-<UTC time>.json (kvevaluator.paths.gate_records_dir), with
# the commit, its tree, the level ("full", "analysis-code" or "analysis"), the binary, each stage's rc, seconds and log,
# the wall time, the host and whether a remote branch holds the commit. A full record names its round's capture
# ("capture": path, key and the digest of its files), which a later analysis-code gate may reuse; an analysis-code record names the capture it
# reused (path, key, the record it came from) or says it measured its own. Logs: output/logs/gate-<level>/<UTC time>-<sha7>/.
set -uo pipefail
LEVEL=full
case "${1:-}" in
  -h|--help) sed -n '2,/^set -uo/p' "$0" | sed '$d' | cut -c3-; exit 0 ;;
  "") ;;
  --level) LEVEL="${2:-}"; [[ "$LEVEL" == full || "$LEVEL" == analysis || "$LEVEL" == analysis-code ]] && [[ $# -eq 2 ]] \
             || { echo "usage: run/gate-full.sh [--level full|analysis|analysis-code]" >&2; exit 2; } ;;
  *) echo "usage: run/gate-full.sh [--level full|analysis|analysis-code]" >&2; exit 2 ;;
esac
source "$(dirname "${BASH_SOURCE[0]}")/../run/env.sh"

refuse() { echo "GATE_FULL_REFUSED: $*" >&2; exit 2; }
# the tree under test is exactly a commit
CHANGED=$(git status --porcelain --untracked-files=all)
[ -z "$CHANGED" ] || refuse "the tree differs from its commit (commit or move these first):"$'\n'"$(printf '%s\n' "$CHANGED" | head -n 20)"
[ -z "${KVEVAL_ALLOW_STALE_CPP:-}" ] || refuse "KVEVAL_ALLOW_STALE_CPP is set: a gate runs this commit's binary only"
[ "$CPP_BIN" = "$BUILD_DIR/cpp/kvevaluator" ] || refuse "KVEVAL_CPP_BIN names $CPP_BIN (environment or .env): a gate runs the build dir's binary"
uv sync --locked --extra engines --extra lint --check > /dev/null 2>&1 || refuse "the venv lacks the engines or lint extra: run \`just setup\`"
RECORDS=$("$PYTHON" -m kvevaluator.paths gate_records_dir) || refuse "no gate records dir"
[ "$RECORDS" != None ] || refuse "no git clone holds this tree: a gate record needs one"
mkdir -p "$RECORDS"
exec 8>>"$RECORDS/.full.lock"
flock -n 8 || refuse "another \`just gate full\` holds $RECORDS/.full.lock: $(tail -n 1 "$RECORDS/.full.lock" 2>/dev/null)"
: > "$RECORDS/.full.lock"
echo "gate-full pid $$ since $(date -u +%FT%TZ) in $PWD" >&8

HEAD_SHA=$(git rev-parse HEAD); TREE=$(git rev-parse "HEAD^{tree}")
START=$(date -u +%Y%m%dT%H%M%SZ)
CHAIN_TAG=GATE_FULL
unset CHAIN_KEEP_GOING   # a full run stops at its first failure
CHAIN_LOGS=$("$PYTHON" -m kvevaluator.output_tree logs --name "gate-$LEVEL" --name "$START-${HEAD_SHA:0:7}") || exit 1
source run/chain-stages.sh
CONTROLS="$CHAIN_LOGS/controls"

binary_ok() {   # the build dir's binary is this commit's, clean
  local v; v=$("$CPP_BIN" --version) || return 1
  echo "$v" | grep -E '"binary_(git_sha|dirty)"'
  echo "$v" | grep -q "\"binary_git_sha\": \"$HEAD_SHA\"" || { echo "the binary is not HEAD's ($HEAD_SHA)"; return 1; }
  "$PYTHON" -m kvevaluator.capture.binary may-cite "$CPP_BIN" || return 1   # clean and known (binary.may_cite, the one rule)
}
preflight() {   # every part runs, then one verdict; a moved record key is left to the records stage
  local rc=0
  "$PYTHON" tests/gates/gate_preflight.py --gate all --sf 0.1 --prep || rc=1
  if "$PYTHON" -m kvevaluator.measure.records_key --sf 0.1 --check; then echo "records key: current"
  else echo "records key: moved or missing: the records stage dumps the cache again"; fi
  run/meta-gates.sh --sf 0.1 --preflight || rc=1
  return $rc
}
records() {   # dump the record cache again only when its key moved
  if "$PYTHON" -m kvevaluator.measure.records_key --sf 0.1 --check; then echo "records key current: nothing to do"; return 0; fi
  KVEVAL_RECORDS_FRESH=1 "$PYTHON" -c "from kvevaluator.measure import records; records.records_file('ldbc', '0.1')"   # records.fresh_records_command's call
}

capture_stage() {   # analysis-code: a full gate's capture with this commit's key, copied; else the round measures its own
  local key found src rec want got
  key=$(python3 run/gate_levels.py --capture-key) || return 1
  found=$(python3 run/gate_levels.py --reusable-capture "$RECORDS" "$key") || return 1
  if [[ -n "$found" ]]; then
    IFS=$'\t' read -r src rec <<< "$found"
    mkdir -p "$CHAIN_LOGS/capture" && cp -a "$src/captures" "$CHAIN_LOGS/capture/" || return 1
    want=$("$PYTHON" -c 'import json, sys; print(json.load(open(sys.argv[1]))["capture"]["digest"])' "$rec") || return 1
    got=$(python3 run/gate_levels.py --capture-digest "$CHAIN_LOGS/capture") || return 1
    [[ "$got" == "$want" ]] || { echo "the copy's digest $got is not the record's $want"; return 1; }
    echo "reused: $src (record $rec), copied to $CHAIN_LOGS/capture ($(du -sh "$CHAIN_LOGS/capture" | cut -f1), digest ${got:0:12} = the record's)"
    printf '%s\t%s\t%s\t%s\n' zn,ps,yc,d,k "$CHAIN_LOGS/capture" "$src" "$rec" > "$CHAIN_LOGS/round-plan.tsv"
  else
    echo "measured: no full gate's capture has this commit's key; the round runs g and m (~7 min more)"
    printf '%s\t\t\t\n' zn,ps,yc,g,m,k > "$CHAIN_LOGS/round-plan.tsv"
  fi
  echo "capture key $key"
}
round_analysis_code() {   # the round the capture stage chose
  local streams copy
  IFS=$'\t' read -r streams copy _ _ < "$CHAIN_LOGS/round-plan.tsv" || return 1
  run/meta-gates.sh --sf 0.1 --only "$streams" ${copy:+--capture "$copy"}
}

CHAIN_EXPECT="$CHAIN_LOGS/expected.txt"; : > "$CHAIN_EXPECT"
run_stages() {
  stage build just build
  stage binary binary_ok
  if [[ "$LEVEL" == analysis ]]; then   # the stages a paper/ or docs-only change can move
    stage check env KVEVAL_LINT_TRACKED_ONLY=1 just check
    return
  fi
  if [[ "$LEVEL" == analysis-code ]]; then   # the stages a kvcost, experiments or unit-test change can move
    stage check env KVEVAL_LINT_TRACKED_ONLY=1 just check
    stage records records   # the sf0.1 record cache, dumped again if its key moved (the tree's last dump was another commit's)
    # the one named store the round opens (stream yc's; k builds its own): made if missing, as gate-prep would
    stage stores "$PYTHON" tests/gates/gate_preflight.py --gate chooser --sf 0.1 --prep
    stage tallies "$PYTHON" tests/gates/gate_preflight.py --gate predict-sizes --sf 0.1 --prep
    stage capture capture_stage
    stage -v '^META_GATES_PARTIAL_DONE' round round_analysis_code
    return
  fi
  stage preflight preflight
  stage check env KVEVAL_LINT_TRACKED_ONLY=1 just check
  stage records records
  stage golden-counts just golden-counts
  stage tally-version just tally-version
  stage controls run/meta-build-controls.sh "$CONTROLS"
  stage -v '^META_GATES_DONE' round run/meta-gates.sh --sf 0.1 --controls "$CONTROLS"
}
(CHAIN_DRY=1 run_stages)          # the list of stages that must each have a row
chain_begin "$CPP_BIN"
run_stages
chain_end || exit 1

PUSHED=$(git branch -r --contains "$HEAD_SHA" 2>/dev/null | tr -d ' ' | paste -sd, -)
REC="$RECORDS/$HEAD_SHA-$START.json"
"$PYTHON" - "$REC" "$HEAD_SHA" "$TREE" "$CPP_BIN" "$CHAIN_STAGES" "$START" "$SECONDS" "$PWD" "$CHAIN_LOGS" "$PUSHED" "$LEVEL" <<'EOF'
import datetime, json, os, re, socket, subprocess, sys
rec, sha, tree, binary, stages, start, wall, worktree, logs, pushed, level = sys.argv[1:]
v = json.loads(subprocess.run([binary, "--version"], capture_output=True, text=True, check=True).stdout)
rows = [line.rstrip("\n").split("\t") for line in open(stages) if line.strip()]
out = {"commit": sha, "tree": tree, "verdict": "pass", "level": level,
       "binary": {"path": binary, "sha": v["binary_git_sha"], "dirty": v["binary_dirty"]},
       "stages": [{"name": n, "rc": int(rc), "secs": int(s), "log": log} for n, rc, s, log in rows],
       "start": start, "end": datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
       "wall_s": int(wall), "host": socket.gethostname(), "worktree": worktree, "logs": logs,
       "pushed": pushed.split(",") if pushed else []}
import importlib.util
spec = importlib.util.spec_from_file_location("gate_levels", os.path.join(worktree, "run", "gate_levels.py"))
gate_levels = importlib.util.module_from_spec(spec)      # the capture key's one rule
spec.loader.exec_module(gate_levels)
key = gate_levels.capture_key(sha)
if level == "full":     # the round's capture, for a later analysis-code gate
    m = re.search(r"^=== capture dir: (.+)$", open(os.path.join(logs, "round.log")).read(), re.M)
    path = m.group(1) if m else None   # the digest of its files: a capture written after this gate is not reused
    out["capture"] = {"path": path, "key": key, "digest": gate_levels.capture_digest(path) if path else None}
elif level == "analysis-code":
    streams, copy, src, src_rec = open(os.path.join(logs, "round-plan.tsv")).read().rstrip("\n").split("\t")
    out["capture"] = {"key": key, "reused_from": src or None, "record": src_rec or None, "copy": copy or None,
                      "streams": streams}
with open(rec + ".tmp", "w") as f:
    json.dump(out, f, indent=1)
os.replace(rec + ".tmp", rec)   # whole or absent: check_merge never reads a half-written record
print(f"gate record: {rec}")
EOF
[ -f "$REC" ] || { echo "GATE_FULL_FAIL record (every stage passed; the record was not written)"; exit 1; }
