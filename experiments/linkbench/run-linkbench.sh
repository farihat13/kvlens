#!/bin/bash
# run-linkbench.sh — end-to-end LinkBench wide-read pipeline (the path-(b) analog of run/kvstats.sh):
#   the data (`just fetch-dataset linkbench=<scale>`: the tool, datagen, the logged request phase, anchors, params; each
#   only if missing) -> cpp capture (ingest the 3 stores on first use, then run getNode / composed_getNode / getLinkList
#   on dgraph/nebula/arango) -> figures. One command per scale.
#
#   experiments/linkbench/run-linkbench.sh --scale n1m                  # 1M nodes, full pipeline
#   experiments/linkbench/run-linkbench.sh --scale tiny                 # 1,000 nodes: a quick smoke run
#   experiments/linkbench/run-linkbench.sh --plots-only                 # just re-render figures from the wide-read captures
#
# Flags: --scale <tag> (a scale of the LinkBench dataset's manifest: tiny, n1m); --nparam N (anchors read per query,
#        default 60: the data step makes 60); --db a,b,c (default dgraph,nebula,arangodb); --plots-only; -h.
# Data of another size or clock: python/kvevaluator/datasets/linkbench/generate.sh (its -h).
#
# LinkBench scales by NODE COUNT -- its own axis: stores live at output/stores/linkbench/<scale>/, never the LDBC sf
# slot. The wide-read capture lands in its own output tree,
# output/studies/linkbench-wide-read/captures/linkbench/<scale>/ (python/kvevaluator/output_tree.py), the figures in
# output/figures/linkbench-wide-read/<date>/.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../../run/env.sh"

SCALE=""; NPARAM=60; DBS="dgraph,nebula,arangodb"; PLOTS_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --scale) SCALE="${2:?--scale needs a value}"; shift 2 ;;
    --nparam) NPARAM="${2:?--nparam needs a value}"; shift 2 ;;
    --db) DBS="${2:?--db needs a comma list}"; shift 2 ;;
    --plots-only) PLOTS_ONLY=1; shift ;;
    -h|--help) awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next} {exit}' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac
done

t0=$(date +%s)
log() { echo "[run-linkbench $(date +%H:%M:%S) +$(( $(date +%s) - t0 ))s] $*"; }

if (( PLOTS_ONLY )); then
  log "figures from the wide-read captures -> figures/linkbench-wide-read/<date>/ (the output tree)"
  "$PYTHON" experiments/linkbench/plot.py
  echo "[run-linkbench] DONE (plots-only)"; exit 0
fi

: "${SCALE:?--scale required (e.g. n1m) unless --plots-only}"
log "data: just fetch-dataset linkbench=$SCALE (each step only if missing)"
"$PYTHON" -m kvevaluator.fetch_dataset "linkbench=$SCALE"
DATA="$("$PYTHON" -m kvevaluator.datasets data linkbench "$SCALE")/data"

log "stores: $DBS at scale $SCALE from $DATA (ingested on first use)"
for db in ${DBS//,/ }; do
  "$PYTHON" -c 'import sys; from kvevaluator.measure import stores; stores.ensure_store("linkbench", sys.argv[1], sys.argv[2], data=sys.argv[3])' \
      "$SCALE" "$db" "$DATA"
done
# the four reads with anchor params (getlink and the writes need the request log's: --params quick|quick-next), in an output
# tree of their own (a slice of the workload's queries), read by plot.py (its WIDE_READ)
WIDE_READ=$("$PYTHON" -m kvevaluator.output_tree study --name linkbench-wide-read) || exit 1
log "capture: kvcost measure --workload l01 ${DBS//,/ }, the anchored reads (nparam=$NPARAM) -> $WIDE_READ"
in_tree "$WIDE_READ" "$PYTHON" -m kvcost.cli.cli measure --dataset "linkbench=$SCALE" --workload l01 ${DBS//,/ } \
    --params anchors --nparam "$NPARAM" --queries getnode,composed_getnode,getlinklist_link1,getlinklist_link2

log "figures: figures/linkbench-wide-read/<date>/ (the output tree) (all scales with a wide-read capture)"
"$PYTHON" experiments/linkbench/plot.py
echo "[run-linkbench] DONE (scale $SCALE)"
