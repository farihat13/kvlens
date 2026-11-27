#!/bin/bash
set -euo pipefail

# Download LDBC Social Network Benchmark RDF data and schema using this script, if lfs limit exceeded


BENCHDIR=`pwd`/data/ldbc
SF=0.3


usage() {
  cat <<EOF
Usage: $0 [--sf SCALE] [-d DATA_ROOT]
    --sf   Scale factor (default: ${SF}) 
    -d     Root dataset dir (default: ${BENCHDIR})
EOF
}

# --------------------
# Parse flags
# --------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        -d) BENCHDIR="$2"; shift 2 ;;
        --sf|--scale) 
            SF="$2"
            valid_sfs=("0.1" "0.3" "1" "3" "10")
            valid=false
            for sf in "${valid_sfs[@]}"; do
                if [[ "$SF" == "$sf" ]]; then
                    valid=true
                    break
                fi
            done
            if ! $valid; then
                echo "Error: --sf must be one of: ${valid_sfs[*]}"
                exit 1
            fi
            shift 2
            ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown arg: $1"; usage; exit 1 ;;
    esac
done

echo "Downloading LDBC RDF data for scale factor $SF into $BENCHDIR/sf$SF/"

start_time=$(date +%s)

mkdir -p $BENCHDIR
cd $BENCHDIR

mkdir -p sf$SF
cd sf$SF
[ -f ldbcTypes.schema ] || wget https://github.com/dgraph-io/dgraph-benchmarks/raw/refs/heads/main/ldbc/sf$SF/ldbcTypes.schema

mkdir -p ldbc_rdf_$SF
cd ldbc_rdf_$SF
[ -f Deltas.rdf ] || wget https://media.githubusercontent.com/media/dgraph-io/dgraph-benchmarks/refs/heads/main/ldbc/sf$SF/ldbc_rdf_$SF/Deltas.rdf
[ -f comment_0.rdf ] || wget https://media.githubusercontent.com/media/dgraph-io/dgraph-benchmarks/refs/heads/main/ldbc/sf$SF/ldbc_rdf_$SF/comment_0.rdf
[ -f containerOf_0.rdf ] || wget https://media.githubusercontent.com/media/dgraph-io/dgraph-benchmarks/refs/heads/main/ldbc/sf$SF/ldbc_rdf_$SF/containerOf_0.rdf
[ -f forum_0.rdf ] || wget https://media.githubusercontent.com/media/dgraph-io/dgraph-benchmarks/refs/heads/main/ldbc/sf$SF/ldbc_rdf_$SF/forum_0.rdf
[ -f hasCreator_0.rdf ] || wget https://media.githubusercontent.com/media/dgraph-io/dgraph-benchmarks/refs/heads/main/ldbc/sf$SF/ldbc_rdf_$SF/hasCreator_0.rdf
[ -f hasInterest_0.rdf ] || wget https://media.githubusercontent.com/media/dgraph-io/dgraph-benchmarks/refs/heads/main/ldbc/sf$SF/ldbc_rdf_$SF/hasInterest_0.rdf
[ -f hasMember_0.rdf ] || wget https://media.githubusercontent.com/media/dgraph-io/dgraph-benchmarks/refs/heads/main/ldbc/sf$SF/ldbc_rdf_$SF/hasMember_0.rdf
[ -f hasModerator_0.rdf ] || wget https://media.githubusercontent.com/media/dgraph-io/dgraph-benchmarks/refs/heads/main/ldbc/sf$SF/ldbc_rdf_$SF/hasModerator_0.rdf
[ -f hasTag_0.rdf ] || wget https://media.githubusercontent.com/media/dgraph-io/dgraph-benchmarks/refs/heads/main/ldbc/sf$SF/ldbc_rdf_$SF/hasTag_0.rdf
[ -f hasType_0.rdf ] || wget https://media.githubusercontent.com/media/dgraph-io/dgraph-benchmarks/refs/heads/main/ldbc/sf$SF/ldbc_rdf_$SF/hasType_0.rdf
[ -f isLocatedIn_0.rdf ] || wget https://media.githubusercontent.com/media/dgraph-io/dgraph-benchmarks/refs/heads/main/ldbc/sf$SF/ldbc_rdf_$SF/isLocatedIn_0.rdf
[ -f isPartOf_0.rdf ] || wget https://media.githubusercontent.com/media/dgraph-io/dgraph-benchmarks/refs/heads/main/ldbc/sf$SF/ldbc_rdf_$SF/isPartOf_0.rdf
[ -f isSubclassOf_0.rdf ] || wget https://media.githubusercontent.com/media/dgraph-io/dgraph-benchmarks/refs/heads/main/ldbc/sf$SF/ldbc_rdf_$SF/isSubclassOf_0.rdf
[ -f knows_0.rdf ] || wget https://media.githubusercontent.com/media/dgraph-io/dgraph-benchmarks/refs/heads/main/ldbc/sf$SF/ldbc_rdf_$SF/knows_0.rdf
[ -f likes_0.rdf ] || wget https://media.githubusercontent.com/media/dgraph-io/dgraph-benchmarks/refs/heads/main/ldbc/sf$SF/ldbc_rdf_$SF/likes_0.rdf
[ -f organisation_0.rdf ] || wget https://media.githubusercontent.com/media/dgraph-io/dgraph-benchmarks/refs/heads/main/ldbc/sf$SF/ldbc_rdf_$SF/organisation_0.rdf
[ -f person_0.rdf ] || wget https://media.githubusercontent.com/media/dgraph-io/dgraph-benchmarks/refs/heads/main/ldbc/sf$SF/ldbc_rdf_$SF/person_0.rdf
[ -f place_0.rdf ] || wget https://media.githubusercontent.com/media/dgraph-io/dgraph-benchmarks/refs/heads/main/ldbc/sf$SF/ldbc_rdf_$SF/place_0.rdf
[ -f post_0.rdf ] || wget https://media.githubusercontent.com/media/dgraph-io/dgraph-benchmarks/refs/heads/main/ldbc/sf$SF/ldbc_rdf_$SF/post_0.rdf
[ -f replyOf_0.rdf ] || wget https://media.githubusercontent.com/media/dgraph-io/dgraph-benchmarks/refs/heads/main/ldbc/sf$SF/ldbc_rdf_$SF/replyOf_0.rdf
[ -f studyAt_0.rdf ] || wget https://media.githubusercontent.com/media/dgraph-io/dgraph-benchmarks/refs/heads/main/ldbc/sf$SF/ldbc_rdf_$SF/studyAt_0.rdf
[ -f tagclass_0.rdf ] || wget https://media.githubusercontent.com/media/dgraph-io/dgraph-benchmarks/refs/heads/main/ldbc/sf$SF/ldbc_rdf_$SF/tagclass_0.rdf
[ -f tag_0.rdf ] || wget https://media.githubusercontent.com/media/dgraph-io/dgraph-benchmarks/refs/heads/main/ldbc/sf$SF/ldbc_rdf_$SF/tag_0.rdf
[ -f workAt_0.rdf ] || wget https://media.githubusercontent.com/media/dgraph-io/dgraph-benchmarks/refs/heads/main/ldbc/sf$SF/ldbc_rdf_$SF/workAt_0.rdf


end_time=$(date +%s)
elapsed=$(( end_time - start_time ))
echo "Downloaded LDBC RDF data for scale factor $SF in $elapsed seconds."


echo "Done! Download took $elapsed seconds, files are in $BENCHDIR/sf$SF/"
