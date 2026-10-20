#!/bin/bash
# Build one catalog image. Usage: image-build/build.sh <os> <release> [driver_version] [cuda_version]
# Example: image-build/build.sh ubuntu24.04_cuda13.x v2026.10.1 580.82.07 13.0
#          OUT_DIR=/var/lib/racksdock/images PYTHON=/opt/racksdock/venv/bin/python image-build/build.sh verify v2026.10.1
# Needs: qemu-utils, libguestfs-tools (virt-customize), curl, a Python >= 3.11 with pip (PYTHON) to build the racksdock wheels.
# Afterwards: racksdock-images manifest build <OUT_DIR> <release> && racksdock-images manifest sign ... (docs/images.md)
set -euo pipefail
trap 'echo "build.sh: FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
OS="$1"; RELEASE="$2"; DRIVER="${3:-580.82.07}"; CUDA="${4:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${OUT_DIR:-$HERE/out}"; CACHE="${CACHE_DIR:-$HERE/cache}"; mkdir -p "$OUT" "$CACHE"
PY="${PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "PYTHON=$PY is older than 3.11 (needed to build the racksdock wheels)"; exit 1; }
case "$OS" in
  ubuntu22.04*) BASE_URL="https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img"; GUEST_PY=3.11;;
  ubuntu24.04*|verify) BASE_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"; GUEST_PY=3.12;;
  *) echo "unknown os $OS"; exit 1;;
esac
BASE="$CACHE/$(basename "$BASE_URL")"
[ -f "$BASE" ] || curl -fsSL -o "$BASE" "$BASE_URL"
IMG="$OUT/${OS}_${RELEASE}.qcow2"
rm -f "$IMG" "$IMG.tmp"
# The cloud image's root filesystem is ~2 GB. Enlarging the qcow2 alone does not grow the partition or the filesystem (the
# install would run out of space), so virt-resize copies the image into a fresh ${IMAGE_SIZE} disk and expands the root partition.
qemu-img create -q -f qcow2 "$IMG" "${IMAGE_SIZE:-200G}"
virt-resize --format qcow2 --output-format qcow2 --expand /dev/sda1 "$BASE" "$IMG"
PKGS="$(grep -v '^#' "$HERE/packages/base.txt" | grep . | tr '\n' ',' | sed 's/,$//')"
[ -f "$HERE/packages/$OS.txt" ] && EXTRA="$(grep -v '^#' "$HERE/packages/$OS.txt" | grep . | tr '\n' ',' | sed 's/,$//')" && [ -n "$EXTRA" ] && PKGS="$PKGS,$EXTRA"
# The node-agent and its dependencies are fetched on the build host FOR THE GUEST's Python, so the image never needs PyPI.
WHEELS="$CACHE/wheels-$GUEST_PY"; rm -rf "$WHEELS"; mkdir -p "$WHEELS"
"$PY" -m pip wheel -q --no-deps -w "$WHEELS" "$HERE/../../racksdock-subnet" "$HERE/.."
NODE_WHL="$(ls "$WHEELS"/racksdock_node-*.whl)"
"$PY" -m pip download -q --only-binary=:all: --python-version "$GUEST_PY" --implementation cp \
  --platform manylinux_2_28_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux2014_x86_64 \
  --find-links "$WHEELS" -d "$WHEELS" "racksdock-node[nvml] @ file://$NODE_WHL"
virt-customize -a "$IMG" --smp "${BUILD_CPUS:-8}" --memsize "${BUILD_MEM_MB:-8192}" \
  --install "$PKGS" \
  --mkdir /opt/racksdock --copy-in "$HERE/overlay/usr:/" --copy-in "$HERE/overlay/etc:/" --copy-in "$WHEELS:/opt/racksdock/" \
  --run-command "bash /usr/local/bin/racksdock-image-finalize.sh '$OS' '$RELEASE' '$DRIVER' '$CUDA'" \
  --run-command "PYBIN=\$(command -v python$GUEST_PY || command -v python3); \$PYBIN -m venv /opt/racksdock/venv && /opt/racksdock/venv/bin/pip install -q --no-index --find-links /opt/racksdock/wheels-$GUEST_PY 'racksdock-node[nvml]' && ln -sf /opt/racksdock/venv/bin/racksdock-node-agent /usr/local/bin/racksdock-node-agent && rm -rf /opt/racksdock/wheels-$GUEST_PY" \
  --write "/etc/racksdock/image-version:${OS}_${RELEASE}" \
  --selinux-relabel
if [ -z "${NO_COMPRESS:-}" ]; then
  qemu-img convert -O qcow2 -c "$IMG" "$IMG.tmp" && mv "$IMG.tmp" "$IMG"   # NO_COMPRESS=1 skips this (faster, larger file)
fi
sha256sum "$IMG" > "$IMG.sha256"
echo "built $IMG"
