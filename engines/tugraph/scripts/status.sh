#!/bin/bash
# Is the TuGraph server up (REST), and do its counters answer.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../.env"
curl -sf -m 3 -o /dev/null "http://127.0.0.1:${TUGRAPH_PORT}/" && echo "TuGraph: up on ${TUGRAPH_PORT}" || { echo "TuGraph: not up on ${TUGRAPH_PORT}"; exit 1; }
curl -sf -m 3 "http://127.0.0.1:${TUGRAPH_KVSTATS_PORT}/kvstats" | head -c 300; echo
