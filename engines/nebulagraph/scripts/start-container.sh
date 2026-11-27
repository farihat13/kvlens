#!/bin/bash
set -euo pipefail

# This script starts a Docker container with specified memory and CPU limits.

if [ $# -ne 1 ]; then
    echo "Usage: $0 <container_name>"
    exit 1
fi

CONTAINER_NAME=$1

MEM_LIMIT="${NEBULA_CONTAINER_MEM_LIMIT:-120g}"
CPU_LIMIT="${NEBULA_CONTAINER_CPU_LIMIT:-20}"

sudo docker pull vesoft/nebula-dev:ubuntu2004

# Start the container if not already running
if ! sudo docker ps -a --format '{{.Names}}' | grep -q "^$CONTAINER_NAME$"; then
    echo "Starting new container: $CONTAINER_NAME"
    # --init: handle PID 1 issues
    # --rm: remove the container when it stops
    # -d: detached mode
    # -memory & --cpus: resource limits
    # -security-opt seccomp=unconfined: disable seccomp for compatibility
    # -v: mount current directory to /workspace in the container
    # -w: set working directory inside the container
    # -p: port mappings
    sudo docker run --init --rm -d \
        --memory=$MEM_LIMIT \
        --cpus=$CPU_LIMIT \
        --security-opt seccomp=unconfined \
        --ulimit nofile=65536:65536 \
        -v "$PWD":/workspace \
        -w /workspace \
        --name $CONTAINER_NAME \
        vesoft/nebula-dev:ubuntu2004 tail -f /dev/null

    echo "Installing python dependencies inside the container..."
    sudo docker exec $CONTAINER_NAME bash -c '
    set -e
    apt-get -qq update
    apt-get install -y -qq libboost-all-dev
    cd nebula/tests && make init-all
    ' > /dev/null 2>&1
    
    echo "Done! Container '$CONTAINER_NAME' is up and running."
else
    echo "Container '$CONTAINER_NAME' is already running."
fi