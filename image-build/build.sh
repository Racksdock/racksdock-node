#!/bin/bash
# Build one catalog image. Usage: image-build/build.sh <os> <release> [driver_version] [cuda_version]
# Example: image-build/build.sh ubuntu24.04_cuda13.x v2026.10.1 595.58.03 13.0
set -euo pipefail
OS="$1"; RELEASE="$2"; DRIVER="${3:-595.58.03}"; CUDA="${4:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${OUT_DIR:-$HERE/out}"; mkdir -p "$OUT" "$HERE/cache"
case "$OS" in
  ubuntu22.04*) BASE_URL="https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img";;
  ubuntu24.04*|verify) BASE_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img";;
  *) echo "unknown os $OS"; exit 1;;
esac
BASE="$HERE/cache/$(basename "$BASE_URL")"
[ -f "$BASE" ] || curl -fsSL -o "$BASE" "$BASE_URL"
IMG="$OUT/${OS}_${RELEASE}.qcow2"
qemu-img create -q -f qcow2 -b "$BASE" -F qcow2 "$IMG" 200G
PKGS="$(grep -v '^#' "$HERE/packages/base.txt" | tr '\n' ',' | sed 's/,$//')"
[ -f "$HERE/packages/$OS.txt" ] && PKGS="$PKGS,$(grep -v '^#' "$HERE/packages/$OS.txt" | tr '\n' ',' | sed 's/,$//')"
# the node-agent wheel and its dependencies are built on the build host so the image never needs PyPI at first boot
WHEELS="$HERE/cache/wheels"; mkdir -p "$WHEELS"
python3 -m pip wheel -q -w "$WHEELS" "$HERE/../../racksdock-subnet" "$HERE/.."
virt-customize -a "$IMG" \
  --install "$PKGS" \
  --copy-in "$HERE/overlay/usr:/" --copy-in "$HERE/overlay/etc:/" --copy-in "$WHEELS:/opt/racksdock/" \
  --run-command "python3 -m venv /opt/racksdock/venv && /opt/racksdock/venv/bin/pip install --no-index --find-links /opt/racksdock/wheels racksdock-node && ln -sf /opt/racksdock/venv/bin/racksdock-node-agent /usr/local/bin/racksdock-node-agent" \
  --run-command "bash /usr/local/bin/racksdock-image-finalize.sh '$OS' '$RELEASE' '$DRIVER' '$CUDA'" \
  --write "/etc/racksdock/image-version:${OS}_${RELEASE}" \
  --selinux-relabel
qemu-img convert -O qcow2 -c "$IMG" "$IMG.tmp" && mv "$IMG.tmp" "$IMG"
sha256sum "$IMG" > "$IMG.sha256"
echo "built $IMG"
