#!/bin/bash
# Runs inside the image at build time (via virt-customize). Installs the pinned NVIDIA stack, Fabric Manager, container toolkit,
# compiles STREAM and (for verify) nccl-tests + nvbandwidth, creates the racksdock-agent user, enables units.
set -euo pipefail
OS="$1"; RELEASE="$2"; DRIVER="$3"; CUDA="${4:-}"
. /etc/os-release
DIST="ubuntu$(echo "$VERSION_ID" | tr -d .)"
curl -fsSL "https://developer.download.nvidia.com/compute/cuda/repos/${DIST}/x86_64/cuda-keyring_1.1-1_all.deb" -o /tmp/cuda-keyring.deb
dpkg -i /tmp/cuda-keyring.deb && apt-get update -q
MAJOR="${DRIVER%%.*}"
apt-get install -y -q "nvidia-driver-${MAJOR}-server=${DRIVER}-*" "nvidia-fabricmanager-${MAJOR}=${DRIVER}-*" nvidia-container-toolkit || apt-get install -y -q "nvidia-driver-${MAJOR}-server" "nvidia-fabricmanager-${MAJOR}" nvidia-container-toolkit
apt-mark hold "nvidia-driver-${MAJOR}-server" "nvidia-fabricmanager-${MAJOR}"
systemctl enable nvidia-fabricmanager nvidia-persistenced docker ssh cloud-init
nvidia-ctk runtime configure --runtime=docker
if [ -n "$CUDA" ]; then apt-get install -y -q "cuda-toolkit-${CUDA/./-}" libcudnn9-cuda-${CUDA%%.*} libnccl2 libnccl-dev; fi
# STREAM (compiled from the official source at build time)
curl -fsSL -o /tmp/stream.c https://www.cs.virginia.edu/stream/FTP/Code/stream.c
gcc -O3 -fopenmp -DSTREAM_ARRAY_SIZE=400000000 -DNTIMES=20 -o /usr/local/bin/stream_c.exe /tmp/stream.c || true
if [ "$OS" = "verify" ]; then
  apt-get install -y -q libnccl2 libnccl-dev "cuda-toolkit-13-0" || true
  git clone --depth 1 https://github.com/NVIDIA/nccl-tests /tmp/nccl-tests && make -C /tmp/nccl-tests -j"$(nproc)" && cp /tmp/nccl-tests/build/*_perf /usr/local/bin/
  git clone --depth 1 https://github.com/NVIDIA/nvbandwidth /tmp/nvbandwidth && (cd /tmp/nvbandwidth && cmake . && make -j"$(nproc)" && cp nvbandwidth /usr/local/bin/)
fi
useradd --system --home /var/lib/racksdock-node-agent --create-home --groups video --shell /usr/sbin/nologin racksdock-agent || true
install -d -o racksdock-agent -g racksdock-agent /var/lib/racksdock-node-agent /run/racksdock
cat > /etc/sudoers.d/racksdock-agent <<'SUDO'
racksdock-agent ALL=(root) NOPASSWD: /usr/sbin/cryptsetup, /usr/bin/mount, /usr/bin/umount, /usr/sbin/mkfs.ext4, /usr/bin/chown ubuntu\:ubuntu /data, /usr/bin/sync
SUDO
chmod 440 /etc/sudoers.d/racksdock-agent
systemctl enable racksdock-node-agent racksdock-launch
mkdir -p /etc/racksdock /data
echo "$OS $RELEASE driver=$DRIVER cuda=$CUDA" > /etc/racksdock/build-info
apt-get clean
