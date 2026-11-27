#!/usr/bin/env bash
# Every figure of the paper, in order: each figure's `just results` then `just figure`, then the archive-only items, each
# step timed. The figures and their order come from run/reproduce.py's registry (`just results` lists them), where the
# chain that times stores (latency-vs-cost) is last. A chain the registry lists as "paper's settings only" (the one
# that builds and runs the three live databases) runs at the paper's settings, however they are given (no --dataset,
# or the paper's datasets and scales typed out), at its own scale; any other --dataset leaves it out, with a line.
# An item the registry lists as "optional" is never run here: one line names it and the command that runs it alone. A failed step is recorded and the run goes on; the summary
# names it. Nothing is deleted: on an empty output/ this is a fresh run, else every chain reuses what output/ holds.
#
#   just reproduce-all                                              # the paper's settings (ldbc=3,linkbench=n1m,imdb=full)
#   just reproduce-all --dataset ldbc=0.1,imdb=sample1pct          # the small set (minutes to an hour)
#   just reproduce-all --jobs 4                                     # cap the stores measured at once (memory)
#   just reproduce-all --only latency-vs-cost --dataset ldbc=3,imdb=full   # one chain (e.g. on a second machine)
#   just reproduce-all --skip latency-vs-cost                       # everything else
#   just reproduce-all --dry-run                                    # print the steps, run none
#   just reproduce-all --archive DIR                                # the archive-only items from a local clone of the results repo
#
# Writes output/logs/reproduce-<dataset>-<UTC time>/: times.log (each step's exit code and seconds, then the total and
# the failed count), and one log per step under results/ and figure/.
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

dataset="" jobs="" only="" skip="" archive_file="" dry=0
while [ $# -gt 0 ]; do
  case $1 in
    --dataset) dataset=$2; shift 2 ;;
    --jobs) jobs=$2; shift 2 ;;
    --only) only=,$2,; shift 2 ;;
    --skip) skip=,$2,; shift 2 ;;
    --archive) archive_file=$2; shift 2 ;;
    --dry-run) dry=1; shift ;;
    -h|--help) sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "reproduce-all: unknown argument $1 (--help)" >&2; exit 2 ;;
  esac
done
ds=${dataset:+--dataset $dataset}
jb=${jobs:+--jobs $jobs}
wanted() { [ -z "$only" ] || [[ $only == *",$1,"* ]] || return 1; [[ $skip != *",$1,"* ]]; }

# the registry's figures: a chain (re-measures) or archive-only, in its order
mapfile -t listing < <(uv run --locked python run/reproduce.py results 2>/dev/null)
chains=() archive=() name=""
declare -A paper_only=()      # chain -> why it runs at the paper's settings only (the registry's paper_settings_only)
optional=()                   # "item|why|its own command": the registry's optional items, never run here
for line in "${listing[@]}"; do
  if [[ $line =~ ^([a-z0-9-]+)[[:space:]] ]]; then name=${BASH_REMATCH[1]}
  elif [[ -n $name && $line =~ optional\ \(([^\)]*)\):\ (.*)$ ]]; then optional+=("$name|${BASH_REMATCH[1]}|${BASH_REMATCH[2]}"); name=""
  elif [[ -n $name && $line == *"archive-only:"* ]]; then archive+=("$name"); name=""
  elif [[ -n $name && $line =~ paper\'s\ settings\ only\ \(([^\)]*)\): ]]; then chains+=("$name"); paper_only[$name]=${BASH_REMATCH[1]}; name=""
  elif [[ -n $name && $line == *"paper's settings:"* ]]; then chains+=("$name"); name=""
  fi
done
[ ${#chains[@]} -gt 0 ] || { echo "reproduce-all: no figure listed by run/reproduce.py results" >&2; exit 1; }
# the paper's settings: no --dataset, or one that resolves to the paper's own (python/kvevaluator/sizes.py PAPER)
at_paper=1
if [ -n "$dataset" ]; then
  given=$(uv run --locked python -m kvevaluator.sizes text "$dataset") || exit 2
  [ "$given" = "$(uv run --locked python -m kvevaluator.sizes text)" ] || at_paper=0
fi
order=("${chains[@]}")     # the registry's order: the chain that times stores is last there

slug=${dataset:-paper}; slug=${slug//[=,]/_}
logs=output/logs/reproduce-$slug-$(date -u +%Y%m%dT%H%M%SZ)
for o in "${optional[@]}"; do
  IFS='|' read -r f why own <<< "$o"
  wanted "$f" && echo "$f: optional, not in reproduce-all ($why); run it alone: $own"
done
steps=()
steps+=("smoke|just smoke")
steps+=("fetch-dataset|just fetch-dataset ${dataset}")
for f in "${order[@]}"; do
  wanted "$f" || continue
  if [ -n "${paper_only[$f]:-}" ]; then      # at the paper's settings, at its own scale (the registry's); else left out
    if [ "$at_paper" = 0 ]; then
      echo "$f: paper's settings only (${paper_only[$f]}); run it alone: just results $f"
      continue
    fi
    steps+=("results/$f|just results $f $jb")
    steps+=("figure/$f|just figure $f")
    continue
  fi
  steps+=("results/$f|just results $f $ds $jb")
  steps+=("figure/$f|just figure $f $ds")
done
if [ ${#archive[@]} -gt 0 ] && { [ -z "$only" ] || [[ $only == *",archive,"* ]]; } && [[ $skip != *",archive,"* ]]; then
  steps+=("fetch-results|just fetch-results ${archive_file:+--source $archive_file}")
  for f in "${archive[@]}"; do steps+=("figure/$f|just figure $f"); done
fi

if [ "$dry" = 1 ]; then
  for s in "${steps[@]}"; do echo "${s#*|}"; done
  exit 0
fi

mkdir -p "$logs/results" "$logs/figure" || exit 1
n=$(find output -mindepth 1 -maxdepth 1 ! -name logs 2>/dev/null | wc -l)
state=$([ "$n" -eq 0 ] && echo "output/ empty: a fresh run" || echo "output/ holds $n entries: the chains reuse them")
echo "start $(date -u +%FT%TZ) commit $(git rev-parse --short HEAD) dirty=$(git status --porcelain --untracked-files=no | wc -l); $state" | tee "$logs/times.log"
T0=$SECONDS
for s in "${steps[@]}"; do
  log=${s%%|*} cmd=${s#*|}
  echo "== $(date -u +%T) $cmd"
  t=$SECONDS
  $cmd > "$logs/$log.log" 2>&1
  rc=$?
  echo "rc=$rc $((SECONDS - t)) s: $cmd" | tee -a "$logs/times.log"
done
failed=$(grep -c '^rc=[1-9]' "$logs/times.log")
echo "total $((SECONDS - T0)) s, end $(date -u +%FT%TZ); failed: $failed of ${#steps[@]}" | tee -a "$logs/times.log"
echo "logs: $logs"
[ "$failed" -eq 0 ]
