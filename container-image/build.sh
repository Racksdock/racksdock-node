#!/bin/bash
# Build one container-tier catalog image on a host with Docker. Usage: container-image/build.sh <os> <release>
#   container-image/build.sh ubuntu24.04 v2026.10.1      -> racksdock/ubuntu24.04:v2026.10.1
#   container-image/build.sh verify v2026.10.1           -> racksdock/verify:v2026.10.1 (CUDA devel base, DCGM, benchmarks)
# Needs: docker, a Python >= 3.11 with pip (PYTHON) to build the node-agent wheels for the guest Python (3.12 on Ubuntu 24.04).
# Afterwards: racksdock-images manifest build <image_dir> <release> --containers ; sign ; os-options (docs/images.md).
set -euo pipefail
trap 'echo "container-image/build.sh: FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
OS="$1"; RELEASE="$2"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="${IMAGE_REPO:-racksdock}"; PY="${PYTHON:-python3}"; GUEST_PY=3.12
case "$OS" in
  verify) BASE="${BASE_IMAGE:-nvidia/cuda:13.0.1-devel-ubuntu24.04}";;
  pytorch-*cuda13*|ubuntu24.04-cuda13*) BASE="${BASE_IMAGE:-nvidia/cuda:13.0.1-devel-ubuntu24.04}";;
  pytorch-*cuda12*|ubuntu24.04-cuda12*) BASE="${BASE_IMAGE:-nvidia/cuda:12.8.1-devel-ubuntu24.04}";;
  ubuntu24.04*) BASE="${BASE_IMAGE:-ubuntu:24.04}";;
  *) echo "unknown os $OS (container tier builds ubuntu24.04[-cudaX.Y], pytorch-X.Y-cudaX.Y and verify)"; exit 1;;
esac
CTX="$(mktemp -d)"; trap 'rm -rf "$CTX"' EXIT
mkdir -p "$CTX/wheels" "$CTX/overlay"
cp -r "$HERE/../image-build/overlay/usr" "$CTX/overlay/"
"$PY" -m pip wheel -q --no-deps -w "$CTX/wheels" "$HERE/../../racksdock-subnet" "$HERE/.."
NODE_WHL="$(ls "$CTX/wheels"/racksdock_node-*.whl)"
"$PY" -m pip download -q --only-binary=:all: --python-version "$GUEST_PY" --implementation cp \
  --platform manylinux_2_28_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux2014_x86_64 \
  --find-links "$CTX/wheels" -d "$CTX/wheels" "racksdock-node[nvml] @ file://$NODE_WHL"
cp "$HERE/Dockerfile" "$CTX/"
docker build --build-arg "BASE=$BASE" --build-arg "OS=$OS" --build-arg "RELEASE=$RELEASE" --build-arg "GUEST_PY=$GUEST_PY" -t "$REPO/$OS:$RELEASE" "$CTX"
echo "built $REPO/$OS:$RELEASE = $(docker image inspect --format '{{.Id}}' "$REPO/$OS:$RELEASE")"
