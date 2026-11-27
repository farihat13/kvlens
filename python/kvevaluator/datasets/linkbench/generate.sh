#!/bin/bash
# generate.sh -- make LinkBench's data at one scale (the LinkBench dataset's data step; `just fetch-dataset linkbench=<scale>`
# runs it through fetch.py, beside this file): build the LinkBench tool -> datagen (its real LOAD phase -> JSONL) -> the
# seeded REQUEST phase, logged (requests.jsonl: the workload's params) -> the data's manifest -> the anchors. Each step
# only if missing. Writes <data dir>/datasets/linkbench/<scale>/ (data/, anchors.txt); opens no store.
#
#   python/kvevaluator/datasets/linkbench/generate.sh --scale n1m                 # what `just fetch-dataset linkbench=n1m` runs
#   python/kvevaluator/datasets/linkbench/generate.sh --scale n10m --data <dir>   # another size, into its own dir
#
# Flags: --scale <tag> (the dir; also picks --nodes from tiny=1000 / n1m=1e6 / n10m=1e7 unless --nodes is given);
#        --nodes N; --nparam N (anchors, default 60); --requests N (logged LinkBench requests, default 100000);
#        --data <dir> (JSONL dir; default <data dir>/datasets/linkbench/<scale>/data);
#        --clock MS (the ONE clock value the generators stamp into link and node times; default 1791797291000,
#        LinkBench's own wall-clock reads are patched out, so two generations give the same bytes);
#        --clock-trace FILE | none (a recorded clock put in the fixed one's place per output line; default for
#        --scale n1m at its default size: inputs/data-sources/linkbench/n1m/clock-20261012.csv, the clock of the run the
#        n1m results were measured on, which reproduces that data byte for byte; `none` = the fixed value alone);
#        --skip-datagen (nodes.jsonl / links.jsonl already present); -h.
#
# LinkBench (facebookarchive/linkbench) is cloned + patched (Java 7->8) + built ONCE into <build dir>/linkbench/ (or set
# $LINKBENCH_HOME); it needs git, Java and Maven. FileDumpGraphStore.java (beside this file) exports the LOAD phase to
# JSONL and logs the REQUEST phase; LinkBenchClock.java is the one clock. LinkBench scales by NODE COUNT -- its own axis.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$(dirname "${BASH_SOURCE[0]}")/../../../../run/env.sh"

SCALE=""; NODES=""; NPARAM=60; REQUESTS=100000; DATA=""; SKIP_DATAGEN=0
CLOCK=1791797291000; TRACE=""          # the generators' clock (LinkBenchClock.java): never the wall clock
LB_COMMIT=ac67d54bf291eb9546d5480ffa4b2c2ef9ff0740   # facebookarchive/linkbench, the commit our patches are written for
while [[ $# -gt 0 ]]; do
  case "$1" in
    --scale) SCALE="${2:?--scale needs a value}"; shift 2 ;;
    --nodes) NODES="${2:?--nodes needs a value}"; shift 2 ;;
    --nparam) NPARAM="${2:?--nparam needs a value}"; shift 2 ;;
    --requests) REQUESTS="${2:?--requests needs a value}"; shift 2 ;;
    --data) DATA="${2:?--data needs a dir}"; shift 2 ;;
    --clock) CLOCK="${2:?--clock needs a value in ms}"; shift 2 ;;
    --clock-trace) TRACE="${2:?--clock-trace needs a file, or none}"; shift 2 ;;
    --skip-datagen) SKIP_DATAGEN=1; shift ;;
    -h|--help) awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next} {exit}' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac
done

t0=$(date +%s)
log() { echo "[linkbench-data $(date +%H:%M:%S) +$(( $(date +%s) - t0 ))s] $*"; }

: "${SCALE:?--scale required (e.g. n1m)}"
if [[ -z "$NODES" ]]; then
  case "$SCALE" in
    tiny) NODES=1000 ;; n1m) NODES=1000000 ;; n10m) NODES=10000000 ;;
    *) echo "no default node count for --scale $SCALE; pass --nodes N" >&2; exit 1 ;;
  esac
fi
DATA="${DATA:-$("$PYTHON" -m kvevaluator.datasets data linkbench "$SCALE")/data}"
LB_TOOL="${LINKBENCH_HOME:-$BUILD_DIR/linkbench}"   # the LinkBench tool, built once
[[ "$CLOCK" =~ ^[0-9]+$ ]] || { echo "--clock needs a whole number of ms (got: $CLOCK)" >&2; exit 1; }
# n1m at its default size replays the measured run's clock, so the data the results were measured on comes back
N1M_TRACE=inputs/data-sources/linkbench/n1m/clock-20261012.csv
if [[ -z "$TRACE" && "$SCALE" == n1m && "$NODES" == 1000000 && "$REQUESTS" == 100000 && "$CLOCK" == 1791797291000 ]]; then TRACE="$N1M_TRACE"; fi
[[ "$TRACE" == none ]] && TRACE=""
if [[ -n "$TRACE" ]]; then
  [[ -f "$TRACE" ]] || { echo "--clock-trace: no file $TRACE" >&2; exit 1; }
  TRACE="$(realpath "$TRACE")"
fi
CLOCK_ARGS=(-Dlinkbench.clock="$CLOCK"); [[ -n "$TRACE" ]] && CLOCK_ARGS+=(-Dlinkbench.clocktrace="$TRACE")
MANIFEST_ARGS=(--data "$DATA" --tool "$LB_TOOL" --clock "$CLOCK"); [[ -n "$TRACE" ]] && MANIFEST_ARGS+=(--clock-trace "$TRACE")
FRESH=1; [[ -f "$DATA/nodes.jsonl" || -f "$DATA/requests.jsonl" ]] && FRESH=0
if (( ! FRESH )); then      # data already there: it must be the data this clock setting gives, or say it is not
  if [[ -f "$DATA/manifest.json" ]]; then
    "$PYTHON" "$HERE/data_manifest.py" "${MANIFEST_ARGS[@]}" --same-settings || exit 1
  else
    log "WARNING: $DATA has no manifest.json: it was generated with the WALL CLOCK and cannot be"
    log "WARNING: regenerated; it is used as it is. For reproducible data: a fresh --data dir."
  fi
fi

patch_clock() {  # <file> <how many reads> <sed script>: put LinkBenchClock.now() in place of the wall clock; fail if a line moved
  local f="$LB_SRC/$1" want="$2" got
  git -C "$LB_TOOL" checkout -- "src/main/java/com/facebook/LinkBench/$1"      # pristine, so re-runs are idempotent
  sed -i "$3" "$f"
  got=$(grep -c "LinkBenchClock.now()" "$f" || true)
  [[ "$got" == "$want" ]] || { echo "tool: $1: $got of $want wall-clock reads patched -- the LinkBench source is not the one this patch knows" >&2; exit 1; }
}
tool_commit() {  # the clone must be the commit the patches are written for: checked before a reuse and before a build
  local have; have=$(git -C "$LB_TOOL" rev-parse HEAD)
  [[ "$have" == "$LB_COMMIT" ]] || { echo "tool: $LB_TOOL is at $have, the clock patch is written for $LB_COMMIT -- refusing to generate" >&2; exit 1; }
}
build_tool() {   # clone + patch (Java 7->8, the clock) + our two classes + build, once -- and again when our classes change
  LB_SRC="$LB_TOOL/src/main/java/com/facebook/LinkBench"
  [[ -d "$LB_TOOL/.git" ]] && tool_commit
  local ours=$HERE/FileDumpGraphStore.java clock=$HERE/LinkBenchClock.java
  if [[ -f "$LB_TOOL/target/FacebookLinkBench.jar" ]] && cmp -s "$ours" "$LB_SRC/FileDumpGraphStore.java" && cmp -s "$clock" "$LB_SRC/LinkBenchClock.java" \
     && [[ "$(grep -l "LinkBenchClock.now()" "$LB_SRC"/LinkBenchLoad.java "$LB_SRC"/NodeLoader.java "$LB_SRC"/LinkBenchRequest.java 2>/dev/null | wc -l)" == 3 ]] \
     && [[ "$LB_TOOL/target/FacebookLinkBench.jar" -nt "$LB_SRC/LinkBenchClock.java" ]]; then log "tool: reuse $LB_TOOL"; return; fi
  log "tool: clone + build LinkBench at $LB_TOOL (one-time, ~1 min)"
  mkdir -p "$(dirname "$LB_TOOL")"
  [[ -d "$LB_TOOL/.git" ]] || git clone --depth 1 https://github.com/facebookarchive/linkbench.git "$LB_TOOL"
  tool_commit
  git -C "$LB_TOOL" checkout -- pom.xml 2>/dev/null || true    # pristine pom, so re-runs are idempotent
  # Java 7 -> 8 (JDK 21's javac refuses 7), and bump the ancient compiler plugin (3.0). The version
  # bump is SCOPED to the maven-compiler-plugin <plugin> block -- a global sub would also rewrite
  # commons-math3 (also 3.0) to a nonexistent 3.13.0 and break dependency resolution.
  sed -i '/maven-compiler-plugin/,/<\/plugin>/ s#<version>3.0</version>#<version>3.13.0</version>#' "$LB_TOOL/pom.xml"
  sed -i 's#<source>1.7</source>#<source>8</source>#; s#<target>1.7</target>#<target>8</target>#' "$LB_TOOL/pom.xml"
  # the wall clock, out of everything the data holds: a loaded link's time (2 reads), a node's time, and the request
  # phase's add / update of a link (2) and of a node (2). The other clock reads time the run and are never written.
  patch_clock LinkBenchLoad.java 2 's/link\.time = System\.currentTimeMillis();/link.time = LinkBenchClock.now();/; s/return (System\.currentTimeMillis() - Integer\.MAX_VALUE - 1L)/return (LinkBenchClock.now() - Integer.MAX_VALUE - 1L)/'
  patch_clock NodeLoader.java 1 's#(int)(System\.currentTimeMillis()/1000),#(int)(LinkBenchClock.now()/1000),#'
  patch_clock LinkBenchRequest.java 4 's/link\.time = System\.currentTimeMillis();/link.time = LinkBenchClock.now();/; s#(int)(System\.currentTimeMillis()/1000), data);#(int)(LinkBenchClock.now()/1000), data);#'
  cp "$ours" "$clock" "$LB_SRC/"
  ( cd "$LB_TOOL" && mvn -q -Dmaven.test.skip=true package )
}

datagen() {      # real LinkBench LOAD -> nodes.jsonl / links.jsonl via FileDumpGraphStore
  if [[ -f "$DATA/nodes.jsonl" && -f "$DATA/links.jsonl" ]]; then
    log "datagen: $DATA already populated -- skip (fresh --data to regenerate)"; return; fi
  build_tool
  mkdir -p "$DATA"
  local cfg="$DATA/linkbench.properties"
  cat "$LB_TOOL/config/LinkConfigMysql.properties" "$LB_TOOL/config/FBWorkload.properties" > "$cfg"
  cat >> "$cfg" <<EOF

linkstore = com.facebook.LinkBench.FileDumpGraphStore
nodestore = com.facebook.LinkBench.FileDumpGraphStore
loaders = 1
generate_nodes = true
maxid1 = $(( NODES + 1 ))
# a fixed seed: LinkBench otherwise draws a new one per LOAD, so the graph is the same but every payload
# (node/link data, sizes) differs run to run -- bytes would not reproduce across machines
load_random_seed = 12345
EOF
  log "datagen: LinkBench LOAD $NODES nodes -> $DATA"
  LINKBENCH_HOME="$LB_TOOL" java -Dlinkbench.dumpdir="$DATA" "${CLOCK_ARGS[@]}" -cp "$LB_TOOL/target/FacebookLinkBench.jar" \
      com.facebook.LinkBench.LinkBenchDriver -c "$cfg" -l
}

requests() {     # LinkBench's REQUEST phase, LOGGED (not executed) by FileDumpGraphStore -> requests.jsonl: the
                 # workload's params, drawn by LinkBench's own op mix and access distributions, seeded
  local out="$DATA/requests.jsonl" cfg="$DATA/linkbench.request.properties"
  if [[ -s "$out" && -f "$cfg" ]] && grep -qx "requests = $REQUESTS" "$cfg"; then
    log "requests: $out already holds $REQUESTS seeded requests -- skip"; return; fi
  build_tool
  cp "$DATA/linkbench.properties" "$cfg"
  printf '\n%s\n' "requesters = 1" "requests = $REQUESTS" "request_random_seed = 12345" "maxtime = 100000000" \
      "warmup_time = 0" "progressfreq = 100000000" "displayfreq = 100000000" >> "$cfg"
  log "requests: LinkBench REQUEST phase, $REQUESTS requests (seeded) -> $out"
  LINKBENCH_HOME="$LB_TOOL" java -Dlinkbench.dumpdir="$DATA" "${CLOCK_ARGS[@]}" -cp "$LB_TOOL/target/FacebookLinkBench.jar" \
      com.facebook.LinkBench.LinkBenchDriver -c "$cfg" -r
}

(( SKIP_DATAGEN )) || datagen
requests
if (( FRESH )) || [[ -f "$DATA/manifest.json" ]]; then   # what made this data, and its checksums (a recorded clock also
  "$PYTHON" "$HERE/data_manifest.py" "${MANIFEST_ARGS[@]}" --write   # checks the run it reproduces: sha256 per file)
fi

log "anchors: $NPARAM degree-stratified anchors (scale $SCALE) from $DATA"
"$PYTHON" -m kvevaluator.datasets.linkbench.gen_linkbench_anchors --data "$DATA" --tag "$SCALE" --count "$NPARAM"
log "DONE (data: $DATA)"
