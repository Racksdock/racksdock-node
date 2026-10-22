# Container-tier catalog image (docs/01 §10). Built by container-image/build.sh:
#   tenant image:   BASE=ubuntu:24.04                      OS=ubuntu24.04   (sshd, nested Docker, GPU via the host's container toolkit)
#   verify image:   BASE=nvidia/cuda:13.0.1-devel-ubuntu24.04  OS=verify   (+ DCGM, fio, iperf3, sysbench, memtester, nccl-tests, nvbandwidth)
# The NVIDIA driver is never inside the image: the host's nvidia-container-toolkit injects libcuda/nvidia-smi at `docker run --gpus all`.
ARG BASE=ubuntu:24.04
FROM ${BASE}
ARG OS=ubuntu24.04
ARG RELEASE=dev
ARG GUEST_PY=3.12
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -q && apt-get install -y -q --no-install-recommends \
      openssh-server sudo ca-certificates curl jq pciutils python3 python3-venv build-essential gcc iproute2 iptables \
      docker.io cryptsetup nvme-cli util-linux procps \
    && rm -rf /var/lib/apt/lists/*
# verify image extras (validator diagnostics and benchmarks)
RUN if [ "$OS" = "verify" ]; then \
      . /etc/os-release && DIST="ubuntu$(echo "$VERSION_ID" | tr -d .)" \
      && (grep -rqs "developer.download.nvidia.com/compute/cuda" /etc/apt/sources.list.d/ || \
          (curl -fsSL "https://developer.download.nvidia.com/compute/cuda/repos/${DIST}/x86_64/cuda-keyring_1.1-1_all.deb" -o /tmp/cuda-keyring.deb && dpkg -i /tmp/cuda-keyring.deb)) \
      && apt-get update -q \
      && apt-get install -y -q --no-install-recommends --allow-change-held-packages fio iperf3 sysbench memtester git cmake libboost-program-options-dev libnccl2 libnccl-dev \
      && (apt-get install -y -q datacenter-gpu-manager-4-cuda-all || apt-get install -y -q datacenter-gpu-manager-4-cuda13 || apt-get install -y -q datacenter-gpu-manager-4-cuda12 || echo "WARNING: no DCGM") \
      && git clone --depth 1 https://github.com/NVIDIA/nccl-tests /tmp/nccl-tests && make -C /tmp/nccl-tests -j"$(nproc)" CUDA_HOME=/usr/local/cuda \
      && cp /tmp/nccl-tests/build/*_perf /usr/local/bin/ \
      && git clone --depth 1 https://github.com/NVIDIA/nvbandwidth /tmp/nvbandwidth && cd /tmp/nvbandwidth && cmake -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc . \
      && make -j"$(nproc)" && cp nvbandwidth /usr/local/bin/ && rm -rf /tmp/nccl-tests /tmp/nvbandwidth && rm -rf /var/lib/apt/lists/*; \
    fi
# PyTorch templates (OS=pytorch-<ver>-cuda<ver>): wheels for the CUDA line of the base image + JupyterLab into the system Python
RUN if [ "${OS#pytorch-}" != "$OS" ]; then \
      PT="$(echo "$OS" | sed -E 's/^pytorch-([0-9]+\.[0-9]+(\.[0-9]+)?).*/\1/')"; CU="cu$(echo "$OS" | grep -oE 'cuda[0-9]+\.[0-9]+' | sed 's/cuda//' | tr -d .)" \
      && apt-get update -q && apt-get install -y -q --no-install-recommends python3-venv python3-pip && rm -rf /var/lib/apt/lists/* \
      && python3 -m venv --system-site-packages /opt/pytorch \
      && /opt/pytorch/bin/pip install --no-cache-dir "torch==${PT}.*" torchvision torchaudio --index-url "https://download.pytorch.org/whl/${CU}" \
      && /opt/pytorch/bin/pip install --no-cache-dir jupyterlab \
      && for b in python python3 pip pip3 jupyter jupyter-lab; do ln -sf "/opt/pytorch/bin/$b" "/usr/local/bin/$b"; done \
      && echo 'export PATH=/opt/pytorch/bin:$PATH' > /etc/profile.d/pytorch.sh \
      && mkdir -p /etc/racksdock && /opt/pytorch/bin/python -c "import torch; print('torch', torch.__version__, 'cuda', torch.version.cuda)" | tee /etc/racksdock/pytorch-version; \
    fi
# STREAM (fallback racksdock-stream is pure Python)
RUN (curl -fsSL --max-time 30 -o /tmp/stream.c https://www.cs.virginia.edu/stream/FTP/Code/stream.c \
    && gcc -O3 -fopenmp -DSTREAM_ARRAY_SIZE=400000000 -DNTIMES=20 -o /usr/local/bin/stream_c.exe /tmp/stream.c) || echo "STREAM not compiled; racksdock-stream is the fallback"
# in-image tools shared with the VM images, and the node-agent from wheels built on the build host
COPY overlay/usr/local/bin/ /usr/local/bin/
COPY wheels/ /opt/racksdock/wheels/
RUN python3 -m venv /opt/racksdock/venv && /opt/racksdock/venv/bin/pip install -q --no-index --find-links /opt/racksdock/wheels 'racksdock-node[nvml]' \
    && ln -sf /opt/racksdock/venv/bin/racksdock-node-agent /usr/local/bin/racksdock-node-agent \
    && ln -sf /opt/racksdock/venv/bin/racksdock-container-init /usr/local/bin/racksdock-container-init \
    && rm -rf /opt/racksdock/wheels && chmod +x /usr/local/bin/racksdock-* \
    && (id ubuntu >/dev/null 2>&1 || useradd -m -s /bin/bash ubuntu) && usermod -aG docker ubuntu && usermod -p '*' ubuntu \
    && echo "ubuntu ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/ubuntu && chmod 440 /etc/sudoers.d/ubuntu \
    && mkdir -p /etc/racksdock /data /run/sshd /var/lib/racksdock-node-agent && echo "${OS}_${RELEASE}" > /etc/racksdock/image-version
EXPOSE 22
VOLUME ["/var/lib/docker"]
ENTRYPOINT ["/usr/local/bin/racksdock-container-init"]
