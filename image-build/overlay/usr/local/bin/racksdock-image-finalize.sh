#!/bin/bash
# Runs inside the image at build time (via virt-customize, see image-build/build.sh). Installs the pinned NVIDIA stack (open
# kernel modules, required on Blackwell), Fabric Manager where available, the container toolkit, a Python >= 3.11 for the
# node-agent, compiles STREAM and (for the verify image) installs DCGM, the CUDA toolkit, nccl-tests and nvbandwidth, and enables
# the racksdock units. The node-agent venv is created by build.sh after this script.
set -euo pipefail
OS="$1"; RELEASE="$2"; DRIVER="$3"; CUDA="${4:-}"; PYTORCH="${5:-}"
export DEBIAN_FRONTEND=noninteractive
. /etc/os-release
DIST="ubuntu$(echo "$VERSION_ID" | tr -d .)"
apt-get update -q

# DKMS builds the NVIDIA modules for the image's kernel at install time (uname -r here is the build appliance's kernel)
for k in /lib/modules/*; do apt-get install -y -q "linux-headers-$(basename "$k")" || echo "WARNING: no headers for $(basename "$k")"; done

# Python >= 3.11 for the node-agent (22.04 ships 3.10)
if [ "$VERSION_ID" = "22.04" ]; then
  apt-get install -y -q software-properties-common
  add-apt-repository -y ppa:deadsnakes/ppa && apt-get update -q
  apt-get install -y -q python3.11 python3.11-venv
fi

curl -fsSL "https://developer.download.nvidia.com/compute/cuda/repos/${DIST}/x86_64/cuda-keyring_1.1-1_all.deb" -o /tmp/cuda-keyring.deb
dpkg -i /tmp/cuda-keyring.deb && apt-get update -q

# driver: pinned open-module server branch, then unpinned, then the proprietary branch, then the newest open server branch available
MAJOR="${DRIVER%%.*}"
if apt-get install -y -q "nvidia-driver-${MAJOR}-server-open=${DRIVER}-*"; then :
elif apt-get install -y -q "nvidia-driver-${MAJOR}-server-open"; then echo "WARNING: exact driver ${DRIVER} unavailable; installed the ${MAJOR}-server-open branch"
elif apt-get install -y -q "nvidia-driver-${MAJOR}-server"; then echo "WARNING: open modules unavailable; installed proprietary ${MAJOR}-server (not usable on Blackwell)"
else
  NEWEST=$(apt-cache search --names-only '^nvidia-driver-[0-9]+-server-open$' | awk '{print $1}' | sort -t- -k3,3n | tail -1)
  [ -n "$NEWEST" ] || { echo "ERROR: no NVIDIA server driver in the ${DIST} repository"; exit 1; }
  echo "WARNING: driver branch ${MAJOR} unavailable; installing ${NEWEST}"
  apt-get install -y -q "$NEWEST"
  MAJOR=$(echo "$NEWEST" | grep -oE '[0-9]+')
fi
apt-mark hold $(dpkg-query -W -f='${Package}\n' 'nvidia-*' 2>/dev/null | tr '\n' ' ') >/dev/null 2>&1 || true
apt-get install -y -q nvidia-container-toolkit
apt-get install -y -q "nvidia-fabricmanager-${MAJOR}" && systemctl enable nvidia-fabricmanager || echo "NOTE: Fabric Manager not installed (only needed with NVSwitch)"
# NVIDIA's unit runs persistenced with --no-persistence-mode and has no [Install] section: override both so the GPUs stay
# initialized between processes (fast nvidia-smi / CUDA start on an 8-GPU rental)
mkdir -p /etc/systemd/system/nvidia-persistenced.service.d
cat > /etc/systemd/system/nvidia-persistenced.service.d/racksdock.conf <<'EOT'
[Service]
ExecStart=
ExecStart=/usr/bin/nvidia-persistenced --user nvidia-persistenced --verbose
[Install]
WantedBy=multi-user.target
EOT
systemctl enable nvidia-persistenced docker ssh cloud-init 2>/dev/null || true
nvidia-ctk runtime configure --runtime=docker

if [ -n "$CUDA" ]; then
  apt-get install -y -q "cuda-toolkit-${CUDA/./-}" libnccl2 libnccl-dev
  apt-get install -y -q "libcudnn9-cuda-${CUDA%%.*}" || echo "NOTE: cuDNN package for CUDA ${CUDA} not available"
fi

if [ -n "$PYTORCH" ]; then
  # PyTorch template: torch/torchvision/torchaudio wheels for this CUDA line into the system Python (so `python3 -c "import torch"`
  # just works), plus JupyterLab; the exact versions are recorded for the attestation panel
  # a dedicated environment (system site-packages visible) so pip never fights Debian-owned packages; it becomes the default
  # python for every shell (/etc/profile.d) and for `python3` on PATH (/usr/local/bin symlinks); /usr/bin/python3 stays untouched
  CU="cu$(echo "$CUDA" | tr -d .)"
  apt-get install -y -q python3-venv python3-pip
  python3 -m venv --system-site-packages /opt/pytorch
  /opt/pytorch/bin/pip install --no-cache-dir "torch==${PYTORCH}.*" torchvision torchaudio --index-url "https://download.pytorch.org/whl/${CU}"
  /opt/pytorch/bin/pip install --no-cache-dir jupyterlab
  for b in python python3 pip pip3 jupyter jupyter-lab; do ln -sf "/opt/pytorch/bin/$b" "/usr/local/bin/$b"; done
  echo 'export PATH=/opt/pytorch/bin:$PATH' > /etc/profile.d/pytorch.sh
  mkdir -p /etc/racksdock
  /opt/pytorch/bin/python -c "import torch; print('torch', torch.__version__, 'cuda', torch.version.cuda)" | tee /etc/racksdock/pytorch-version
fi

# STREAM (compiled from the official source at build time); racksdock-stream is the Python fallback
curl -fsSL -o /tmp/stream.c https://www.cs.virginia.edu/stream/FTP/Code/stream.c \
  && gcc -O3 -fopenmp -DSTREAM_ARRAY_SIZE=400000000 -DNTIMES=20 -o /usr/local/bin/stream_c.exe /tmp/stream.c || echo "NOTE: STREAM not compiled"

if [ "$OS" = "verify" ]; then
  # DCGM: package names changed across releases (not available for GeForce-only test boxes either); Q4 is skipped when absent
  # cuda-all ships the diag plugins for every CUDA line: DCGM picks the plugin set matching the HOST driver, so a cuda13-only
  # install fails with "Cannot load plugins ... plugins/cuda12" on hosts still running a CUDA 12 driver
  apt-get install -y -q datacenter-gpu-manager-4-cuda-all || apt-get install -y -q datacenter-gpu-manager-4-cuda13 || apt-get install -y -q datacenter-gpu-manager-4-cuda12 \
    || apt-get install -y -q datacenter-gpu-manager || echo "WARNING: DCGM not installed; Q4 cannot run on this image"
  systemctl enable nvidia-dcgm 2>/dev/null || true
  VCUDA="${CUDA:-13.0}"
  apt-get install -y -q "cuda-toolkit-${VCUDA/./-}" libnccl2 libnccl-dev
  export PATH=/usr/local/cuda/bin:$PATH CUDA_HOME=/usr/local/cuda
  git clone --depth 1 https://github.com/NVIDIA/nccl-tests /tmp/nccl-tests
  make -C /tmp/nccl-tests -j"$(nproc)" CUDA_HOME=/usr/local/cuda && cp /tmp/nccl-tests/build/*_perf /usr/local/bin/
  git clone --depth 1 https://github.com/NVIDIA/nvbandwidth /tmp/nvbandwidth
  (cd /tmp/nvbandwidth && cmake -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc . && make -j"$(nproc)" && cp nvbandwidth /usr/local/bin/)
  rm -rf /tmp/nccl-tests /tmp/nvbandwidth
fi

install -d /var/lib/racksdock-node-agent /etc/racksdock /data
systemctl enable racksdock-node-agent racksdock-launch
# every VM generates its own SSH host keys and machine id at first boot (cloud-init regenerates deleted host keys)
rm -f /etc/ssh/ssh_host_*
truncate -s 0 /etc/machine-id
echo "$OS $RELEASE driver=$DRIVER cuda=$CUDA" > /etc/racksdock/build-info
apt-get clean
