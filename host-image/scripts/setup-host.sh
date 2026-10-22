#!/bin/bash
# Turn a plain Ubuntu 22.04/24.04 bare-metal server into a racksdock host WITHOUT reimaging it: the same result the turnkey host
# image (host-image/) produces, applied to an existing OS. Run as root. Idempotent; rerun after fixing a reported problem.
#
#   REPOS=/root/racksdock PLATFORM_URL=http://127.0.0.1:8000 PROFILE=rtx4090x8-dev TRUST_MODE=dev bash setup-host.sh
#
# Inputs (environment):
#   REPOS          directory holding racksdock-subnet and racksdock-node (plus racksdock-platform for single-box tests;
#                  a real miner node does not need it); default: three levels up from this file
#   PLATFORM_URL   marketplace HTTP URL (dev trust mode fetches the dev CA and this node's simulated identity from it);
#                  empty = single-box test: a local dev CA is created in /etc/racksdock/devca (the marketplace reuses it)
#   PLATFORM_WS    host-agent WebSocket (default: PLATFORM_URL with ws:// and /v1/hosts/ws)
#   MINER_HOTKEY   ss58 hotkey written into host-agent.env (default: placeholder for tests)
#   REGION         optional label (datacenter / city, e.g. de-fra-1). The platform detects the node's country from the host
#                  agent's address; the label is shown alongside and never overrides it
#   PROFILE        RACKSDOCK_PROFILE (default: b300x8)
#   TRUST_MODE     prod | dev (dev: GPUs without device attestation, e.g. Ampere/Ada test boxes; default prod)
#   DEPLOYMENT     vm (default: KVM VM, needs bare metal or nested virtualization) | dual (bare metal offering BOTH tiers: the
#                  customer picks at launch; the host-agent rebinds the GPUs between VFIO and the NVIDIA driver) | container (Docker tier: a root container with
#                  all GPUs via the NVIDIA container toolkit, nested Docker, host-managed encrypted /data; works on any machine with
#                  the NVIDIA driver, e.g. cloud VMs without nested virtualization; validators score it at half)
#   NET_MODE       nat | bridge (default nat: single-IP hosts; VMs are reached at host:NAT_SSH_PORT)
#   NAT_SSH_PORT   default 20022      PUBLIC_IP   address tenants connect to (default: auto from the default route)
#   BRIDGE         bridge name for NET_MODE=bridge (default br0)
#   RESERVE_CPUS / RESERVE_MEM_GB   kept for the host (default 8 / 64)
#   DATA_DISK_GB   virtio data disk for the VM (default: auto, 500 GB when space allows); PASSTHROUGH_NVME=1 passes free
#                  non-root NVMe controllers through instead (they are CRYPTO-ERASED after every rental!)
#   HUGEPAGE_KIB   1048576 to back the VM with 1 GiB hugepages; default 0
#   HUGEPAGES_GB   reserve this many 1 GiB pages at boot (adds default_hugepagesz=1G hugepagesz=1G hugepages=N iommu=pt to GRUB,
#                  needs a reboot; implies HUGEPAGE_KIB=1048576). Recommended: the VM's memory_gb + 1. Without hugepages, GPU DMA
#                  into guest memory goes through 4 KiB IOMMU mappings (NCCL over host memory measured 1.3 GB/s on the 8x4090 box).
#   PYTHON         interpreter >= 3.11 for the racksdock venv (default: python3.11 or python3; deadsnakes is added on 22.04)
set -euo pipefail
trap 'echo "setup-host.sh: FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
REPOS="${REPOS:-$(cd "$HERE/../../.." && pwd)}"
PLATFORM_URL="${PLATFORM_URL:-}"
PROFILE="${PROFILE:-b300x8}"; TRUST_MODE="${TRUST_MODE:-prod}"; NET_MODE="${NET_MODE:-nat}"; NAT_SSH_PORT="${NAT_SSH_PORT:-20022}"
DEPLOYMENT="${DEPLOYMENT:-vm}"
REGION="${REGION:-}"; MINER_HOTKEY="${MINER_HOTKEY:-}"
# the node's location is detected by the platform from the address this host agent connects from (country-level GeoIP);
# REGION is an optional label the miner may add (datacenter, city) and never overrides the detected country
if [ -n "$REGION" ] && ! [[ "$REGION" =~ ^[a-z][a-z0-9]*(-[a-z0-9]+){0,3}$ ]]; then
  echo "REGION must be a short lowercase label such as de-fra-1 or eu-north-1 (got '$REGION'); it is optional" >&2
  exit 1
fi
if [ -z "$MINER_HOTKEY" ]; then
  echo "MINER_HOTKEY is required: the ss58 hotkey registered on the subnet that will own this node" >&2
  exit 1
fi
export DEBIAN_FRONTEND=noninteractive
step() { echo; echo "== $*"; }

if [ "$DEPLOYMENT" = dual ]; then
  step "1. packages for both tiers (KVM/libvirt/OVMF for VMs; Docker, NVIDIA container toolkit and the NVIDIA driver for containers)"
  apt-get update -q
  apt-get install -y -q qemu-kvm qemu-utils libvirt-daemon-system libvirt-clients ovmf libguestfs-tools iptables pciutils nvme-cli jq curl chrony iperf3 python3 python3-venv cryptsetup
  command -v docker >/dev/null || apt-get install -y -q docker.io
  if ! command -v nvidia-ctk >/dev/null; then
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' > /etc/apt/sources.list.d/nvidia-container-toolkit.list
    apt-get update -q && apt-get install -y -q nvidia-container-toolkit
  fi
  docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia || { nvidia-ctk runtime configure --runtime=docker && systemctl restart docker; }
  if ! modinfo -n nvidia >/dev/null 2>&1; then
    # the host driver for container rentals (same major as the container tier's reference hosts); blacklisted from auto-loading
    # (step 2) and loaded on demand by bind-host-driver.sh. Ubuntu's own packages first, NVIDIA's CUDA repository as fallback.
    DRV="${HOST_DRIVER:-580}"
    if ! apt-get install -y -q "nvidia-driver-${DRV}-open"; then
      . /etc/os-release; DIST="ubuntu$(echo "$VERSION_ID" | tr -d .)"
      curl -fsSL "https://developer.download.nvidia.com/compute/cuda/repos/${DIST}/x86_64/cuda-keyring_1.1-1_all.deb" -o /tmp/cuda-keyring.deb && dpkg -i /tmp/cuda-keyring.deb && apt-get update -q
      apt-get install -y -q "nvidia-driver-${DRV}-open" || apt-get install -y -q "nvidia-driver-${DRV}-server-open" || { echo "ERROR: no NVIDIA ${DRV} open driver for this release (set HOST_DRIVER=<major>)"; exit 1; }
    fi
  fi
  systemctl disable --now nvidia-persistenced nvidia-dcgm nvidia-fabricmanager >/dev/null 2>&1 || true
  systemctl mask nvidia-persistenced >/dev/null 2>&1 || true   # never on a dual host: it pins the devices and its socket breaks container starts when it flaps
  # the driver package's udev rules start persistenced and modprobe nvidia-drm/modeset/uvm on every bind; on a dual host the GPUs
  # are rebound many times a day and those rules stalled udevd (which then blocks cryptsetup/device-mapper): neutralize them
  echo "# racksdock dual-mode host: NVIDIA udev automation is handled by bind-host-driver.sh" > /etc/udev/rules.d/71-nvidia.rules
  udevadm control --reload >/dev/null 2>&1 || true
elif [ "$DEPLOYMENT" = container ]; then
  step "1. packages (Docker, NVIDIA container toolkit, cryptsetup for the per-rental data volume)"
  apt-get update -q
  apt-get install -y -q ca-certificates curl jq pciutils cryptsetup iperf3 python3 python3-venv chrony
  command -v docker >/dev/null || apt-get install -y -q docker.io
  if ! command -v nvidia-ctk >/dev/null; then
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' > /etc/apt/sources.list.d/nvidia-container-toolkit.list
    apt-get update -q && apt-get install -y -q nvidia-container-toolkit
  fi
  docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia || { nvidia-ctk runtime configure --runtime=docker && systemctl restart docker; }
  systemctl enable --now docker >/dev/null 2>&1 || true
  nvidia-smi -L | head -3
else
step "1. packages (KVM, libvirt, OVMF, libguestfs for image builds, iptables, NVMe/PCI tools)"
apt-get update -q
apt-get install -y -q qemu-kvm qemu-utils libvirt-daemon-system libvirt-clients ovmf libguestfs-tools iptables pciutils nvme-cli jq curl chrony iperf3 python3 python3-venv
fi
. /etc/os-release
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if command -v python3.11 >/dev/null; then PY=python3.11; elif python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then PY=python3; fi
fi
if [ -z "$PY" ]; then
  echo "system python3 is older than 3.11; adding deadsnakes python3.11"
  apt-get install -y -q software-properties-common && add-apt-repository -y ppa:deadsnakes/ppa && apt-get update -q
  apt-get install -y -q python3.11 python3.11-venv python3.11-dev && PY=python3.11
fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "PYTHON=$PY is older than 3.11"; exit 1; }

if [ "$DEPLOYMENT" = container ]; then
  step "2-3. container tier: the host keeps its NVIDIA driver; no VFIO, libvirt or hugepages"
else
step "2. kernel: IOMMU, KVM, vfio-pci, vhost_vsock, host NVIDIA driver blacklist"
[ -e /dev/kvm ] || { echo "ERROR: /dev/kvm missing: enable virtualization (AMD-V / VT-x) in the BIOS"; exit 1; }
if [ -z "$(ls /sys/kernel/iommu_groups 2>/dev/null)" ]; then
  echo "ERROR: no IOMMU groups. Enable IOMMU in the BIOS and add 'amd_iommu=on iommu=pt' (Intel: 'intel_iommu=on iommu=pt') to GRUB_CMDLINE_LINUX_DEFAULT, update-grub, reboot, rerun."; exit 1
fi
printf 'vfio-pci\nvhost_vsock\n' > /etc/modules-load.d/racksdock.conf
modprobe vfio-pci; modprobe vhost_vsock
grep -v '^softdep nvme' "$HERE/../mkosi.extra/etc/modprobe.d/racksdock-vfio.conf" > /etc/modprobe.d/racksdock-vfio.conf   # host driver never claims the GPUs again
update-initramfs -u >/dev/null 2>&1 || true
if [ -n "${HUGEPAGES_GB:-}" ]; then
  HUGEPAGE_KIB=1048576
  if ! grep -q "hugepagesz=1G" /etc/default/grub; then
    sed -i "s/^GRUB_CMDLINE_LINUX_DEFAULT=\"\(.*\)\"/GRUB_CMDLINE_LINUX_DEFAULT=\"\1 default_hugepagesz=1G hugepagesz=1G hugepages=${HUGEPAGES_GB} iommu=pt\"/" /etc/default/grub
    update-grub >/dev/null 2>&1 && echo "GRUB: ${HUGEPAGES_GB} x 1 GiB hugepages + iommu=pt will apply at the next reboot"
  fi
  grep -q "HugePages_Total: *${HUGEPAGES_GB}" /proc/meminfo || echo "NOTE: reboot before starting the host-agent (hugepages are reserved at boot)"
elif ! grep -q 'iommu=pt' /proc/cmdline; then echo "NOTE: add 'iommu=pt' to GRUB_CMDLINE_LINUX_DEFAULT for best passthrough performance (not required)"; fi
# HUGEPAGES_GB=<n>: reserve n x 1 GiB hugepages at boot for the VM and set iommu=pt (production host image default). Guest memory
# backed by 1 GiB pages maps the IOMMU with large entries: GPU DMA to host memory (NCCL over PCIe, H2D copies) is much faster
# than with 4 KiB pages, and VM start no longer spends minutes pinning memory. Takes effect after a reboot; then use
# HUGEPAGE_KIB=1048576 so resources.json asks for hugepage backing.
if [ -n "${HUGEPAGES_GB:-}" ]; then
  if grep -q "hugepages=${HUGEPAGES_GB}" /proc/cmdline; then echo "hugepages already reserved: $(grep HugePages_Total /proc/meminfo)"; else
    sed -i -E "s/^GRUB_CMDLINE_LINUX_DEFAULT=\"([^\"]*)\"/GRUB_CMDLINE_LINUX_DEFAULT=\"\1 default_hugepagesz=1G hugepagesz=1G hugepages=${HUGEPAGES_GB} iommu=pt\"/" /etc/default/grub
    sed -i -E 's/  +/ /g; s/=" /="/' /etc/default/grub
    update-grub >/dev/null 2>&1 && echo "GRUB: reserving ${HUGEPAGES_GB} x 1 GiB hugepages + iommu=pt at next boot. REBOOT, then rerun with HUGEPAGE_KIB=1048576."
  fi
fi

step "3. libvirt + NAT network"
systemctl enable --now libvirtd >/dev/null
virsh net-info default >/dev/null 2>&1 || virsh net-define /usr/share/libvirt/networks/default.xml
virsh net-autostart default >/dev/null; virsh net-start default 2>/dev/null || true
virsh net-info default | grep -E 'Active|Bridge'
fi

step "4. GPU facts (before the host driver is unloaded)"
mkdir -p /etc/racksdock
# (query GPU 0 only: "| head -1" after an 8-line nvidia-smi trips pipefail with SIGPIPE and aborts the script silently)
if command -v nvidia-smi >/dev/null && nvidia-smi -i 0 -L >/dev/null 2>&1; then
  MEM_MIB=$(nvidia-smi -i 0 --query-gpu=memory.total --format=csv,noheader,nounits | tr -d ' ')
  PCI_ID=$(nvidia-smi -i 0 --query-gpu=pci.device_id --format=csv,noheader | tr -d ' ' | sed -E 's/^0x([0-9A-Fa-f]{4})([0-9A-Fa-f]{4})$/\2:\1/' | tr 'A-F' 'a-f')
  NAME=$(nvidia-smi -i 0 --query-gpu=name --format=csv,noheader)
  printf '{"pci_id": "%s", "memory_mib": %s, "name": "%s"}\n' "$PCI_ID" "$MEM_MIB" "$NAME" > /etc/racksdock/gpu-facts.json
fi
[ -f /etc/racksdock/gpu-facts.json ] || { PCI_ID=$(lspci -Dn | awk '$2 ~ /^030[02]:/ && $3 ~ /^10de:/ && !found {print $3; found=1}'); printf '{"pci_id": "%s", "memory_mib": %s, "name": "unknown"}\n' "$PCI_ID" "${GPU_MEMORY_MIB:-0}" > /etc/racksdock/gpu-facts.json; }
cat /etc/racksdock/gpu-facts.json

if [ "$DEPLOYMENT" != container ]; then
step "5. unload the host NVIDIA driver (the GPUs belong to the VM from now on)"
systemctl stop nvidia-persistenced nvidia-fabricmanager nvidia-dcgm 2>/dev/null || true
if grep -q '^nvidia' /proc/modules; then
  if ! (rmmod nvidia_uvm nvidia_drm nvidia_modeset nvidia 2>/dev/null || rmmod nvidia_uvm nvidia 2>/dev/null); then
    echo "WARNING: NVIDIA modules still in use (GPU processes or containers running: nvidia-smi / fuser -v /dev/nvidia*)."
    echo "         Stop them and rerun, or reboot (the blacklist is installed) and rerun."; exit 1
  fi
fi
fi

step "5b. IP forwarding (every tier forwards the public address to the rental; some provider images pin it off)"
printf 'net.ipv4.ip_forward = 1\n' > /etc/sysctl.d/99-racksdock-forward.conf
sysctl -q -w net.ipv4.ip_forward=1
[ "$(sysctl -n net.ipv4.ip_forward)" = 1 ] || { echo "ERROR: could not enable net.ipv4.ip_forward (check /etc/sysctl.d for an override)"; exit 1; }

step "6. discover VM resources -> /etc/racksdock/resources.json"
install -m 755 "$HERE/../mkosi.extra/usr/local/bin/racksdock-discover-resources" /usr/local/bin/racksdock-discover-resources
DISC=(--reserve-cpus "${RESERVE_CPUS:-8}" --reserve-mem-gb "${RESERVE_MEM_GB:-64}" --hugepage-kib "${HUGEPAGE_KIB:-0}" --deployment "$DEPLOYMENT")
[ -n "${DATA_DISK_GB:-}" ] && DISC+=(--data-disk-gb "$DATA_DISK_GB")
[ "${PASSTHROUGH_NVME:-0}" = 1 ] && DISC+=(--passthrough-nvme)
[ -n "${PCI_MMIO64_MB:-}" ] && DISC+=(--pci-mmio64-mb "$PCI_MMIO64_MB")
racksdock-discover-resources --out /etc/racksdock/resources.json "${DISC[@]}" || { echo "fix the PROBLEM lines above, then rerun"; exit 1; }

if [ "$DEPLOYMENT" = container ]; then
step "7. container tier: GPUs stay with the host driver (docker --gpus all hands them to the rental container)"
else
step "7. bind GPUs (and their IOMMU-group companions) to vfio-pci, now and at every boot"
install -m 755 "$HERE/../mkosi.extra/usr/local/bin/bind-vfio.sh" /usr/local/bin/bind-vfio.sh
[ "$DEPLOYMENT" = dual ] && install -m 755 "$HERE/../mkosi.extra/usr/local/bin/bind-host-driver.sh" /usr/local/bin/bind-host-driver.sh
install -m 644 "$HERE/../mkosi.extra/etc/systemd/system/racksdock-bind-vfio.service" /etc/systemd/system/
systemctl daemon-reload; systemctl enable racksdock-bind-vfio.service >/dev/null
/usr/local/bin/bind-vfio.sh
for a in $(jq -r '.gpu_pci[], .extra_pci[]' /etc/racksdock/resources.json); do printf '  %s -> %s\n' "$a" "$(basename "$(readlink -f /sys/bus/pci/devices/$a/driver)")"; done
fi

step "8. racksdock software -> /opt/racksdock/venv"
[ -d /opt/racksdock/venv ] || "$PY" -m venv /opt/racksdock/venv
/opt/racksdock/venv/bin/pip install -q --upgrade pip
# a miner's server needs racksdock-subnet (schemas, verifier, miner neuron) and racksdock-node (host-agent); racksdock-platform
# (marketplace + tenant CLI) is the owner's software and is installed only when present, for single-box tests
PKGS=(-e "$REPOS/racksdock-subnet[validator,chain]" -e "$REPOS/racksdock-node[nvml]")  # chain: racksdock-miner bootstrap can run here when the hotkey lives on this server
[ -d "$REPOS/racksdock-platform" ] && PKGS+=(-e "$REPOS/racksdock-platform")
/opt/racksdock/venv/bin/pip install -q "${PKGS[@]}"
mkdir -p /var/lib/racksdock/images /var/lib/racksdock/vms /var/lib/racksdock-host-agent

TRUST_ENV=""
if [ "$TRUST_MODE" = dev ]; then
  step "9. dev trust: dev CA + simulated identity for this node's GPUs (GPUs without device attestation)"
  COUNT=$(jq '.gpu_pci | length' /etc/racksdock/resources.json)
  PCI_ID=$(jq -r .pci_id /etc/racksdock/gpu-facts.json); MEM_MIB=$(jq -r .memory_mib /etc/racksdock/gpu-facts.json)
  V=/opt/racksdock/venv/bin/racksdock-verify
  if [ -n "$PLATFORM_URL" ]; then
    RACKSDOCK_PROFILE=$PROFILE $V devca fetch "$PLATFORM_URL" /etc/racksdock/devca
    # keep an identity already issued for this host (its node_id must not change on a rerun); issue one only if missing
    if [ "$(ls /etc/racksdock/node/devices 2>/dev/null | grep -c '\.pem$')" = "$COUNT" ] && [ -z "${REISSUE_IDENTITY:-}" ]; then
      echo "node identity present (${COUNT} devices); keeping it (REISSUE_IDENTITY=1 to replace)"
    else
      RACKSDOCK_PROFILE=$PROFILE $V devca fetch-node "$PLATFORM_URL" /etc/racksdock/node --count "$COUNT" --pci-id "$PCI_ID" --memory-mib "$MEM_MIB"
    fi
  else
    [ -f /etc/racksdock/devca/root.pem ] || $V devca init /etc/racksdock/devca
    RACKSDOCK_PROFILE=$PROFILE $V devca issue-node /etc/racksdock/devca /etc/racksdock/node --count "$COUNT" --pci-id "$PCI_ID" --memory-mib "$MEM_MIB"
  fi
  TRUST_ENV=$'RACKSDOCK_TRUST_MODE=dev\nRACKSDOCK_DEV_CA_DIR=/etc/racksdock/devca\nRACKSDOCK_SIM_NODE_DIR=/etc/racksdock/node'
  # operator SSH key for every VM (debugging only; the key you use to reach this box, unless DEV_SSH_PUBKEY names another file)
  for k in "${DEV_SSH_PUBKEY:-}" /root/.ssh/id_ed25519.pub /root/.ssh/id_rsa.pub /root/.ssh/authorized_keys; do
    [ -n "$k" ] && [ -s "$k" ] && { TRUST_ENV="$TRUST_ENV"$'\n'"RACKSDOCK_DEV_SSH_PUBKEY=$k"; break; }
  done
else
  TRUST_ENV="RACKSDOCK_TRUST_MODE=prod"
fi

step "10. host-agent configuration"
PUBLIC_IP="${PUBLIC_IP:-$(ip -j route get 1.1.1.1 2>/dev/null | jq -r '.[0].prefsrc' 2>/dev/null || hostname -I | awk '{print $1}')}"
WS="${PLATFORM_WS:-$(echo "${PLATFORM_URL:-http://127.0.0.1:8000}" | sed -E 's#^http#ws#')/v1/hosts/ws}"
cat > /etc/racksdock/host-agent.env <<ENV
RACKSDOCK_HOST_AGENT_MODE=$(case "$DEPLOYMENT" in container) echo docker;; dual) echo dual;; *) echo real;; esac)
RACKSDOCK_PROFILE=$PROFILE
RACKSDOCK_PLATFORM_WS=$WS
RACKSDOCK_MINER_HOTKEY=$MINER_HOTKEY
RACKSDOCK_REGION=$REGION
RACKSDOCK_HOST_IMAGE_VERSION=host_setup_$(date +%Y.%m.%d)
RACKSDOCK_IMAGE_DIR=/var/lib/racksdock/images
RACKSDOCK_VM_DIR=/var/lib/racksdock/vms
RACKSDOCK_RESOURCES_JSON=/etc/racksdock/resources.json
RACKSDOCK_NET_MODE=$NET_MODE
RACKSDOCK_NET_BRIDGE=${BRIDGE:-br0}
RACKSDOCK_NAT_SSH_PORT=$NAT_SSH_PORT
RACKSDOCK_PUBLIC_IP=$PUBLIC_IP
$TRUST_ENV
ENV
if [ "$DEPLOYMENT" = container ]; then
  # container hosts have no libvirt: the unit depends on docker instead
  sed -e 's/^After=.*/After=network-online.target docker.service/' -e 's/^Requires=.*/Requires=docker.service/' \
    "$HERE/../mkosi.extra/etc/systemd/system/racksdock-host-agent.service" > /etc/systemd/system/racksdock-host-agent.service
else
  install -m 644 "$HERE/../mkosi.extra/etc/systemd/system/racksdock-host-agent.service" /etc/systemd/system/racksdock-host-agent.service
fi
systemctl daemon-reload
cat /etc/racksdock/host-agent.env
echo
# the host-agent fetches the platform's signed image catalog by itself (docs/01 §6.1): no image build on the host
if [ "$DEPLOYMENT" = container ]; then
  systemctl enable --now racksdock-host-agent >/dev/null 2>&1 || true
  echo "Host ready: racksdock-host-agent is running and syncing the image catalog from $PLATFORM_URL (journalctl -fu racksdock-host-agent)."
else
  systemctl enable racksdock-host-agent >/dev/null 2>&1 || true
  echo "Host ready: reboot now (IOMMU/hugepages); racksdock-host-agent starts at boot and syncs the image catalog from $PLATFORM_URL."
fi
echo "Next, on the machine holding the miner wallet:"
echo "  host key:   (on this host) set -a; . /etc/racksdock/host-agent.env; set +a; /opt/racksdock/venv/bin/racksdock-host-agent --show-key"
echo "  bootstrap:  RACKSDOCK_PLATFORM_URL=$PLATFORM_URL RACKSDOCK_HOTKEY=$MINER_HOTKEY RACKSDOCK_CHAIN=subtensor RACKSDOCK_CHAIN_NETWORK=<net> RACKSDOCK_NETUID=<netuid> \\"
echo "              RACKSDOCK_WALLET_NAME=<coldkey> RACKSDOCK_WALLET_HOTKEY=<hotkey> racksdock-miner bootstrap --host-pubkey <host key>"
echo "  (single-box lab test instead: /opt/racksdock/venv/bin/python $REPOS/racksdock-platform/dev/vmtest/run_vm_test.py)"
