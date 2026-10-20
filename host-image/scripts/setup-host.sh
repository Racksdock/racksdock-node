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
#   REGION         region label (default: lab)
#   PROFILE        RACKSDOCK_PROFILE (default: b300x8)
#   TRUST_MODE     prod | dev (dev: GPUs without device attestation, e.g. Ampere/Ada test boxes; default prod)
#   NET_MODE       nat | bridge (default nat: single-IP hosts; VMs are reached at host:NAT_SSH_PORT)
#   NAT_SSH_PORT   default 20022      PUBLIC_IP   address tenants connect to (default: auto from the default route)
#   BRIDGE         bridge name for NET_MODE=bridge (default br0)
#   RESERVE_CPUS / RESERVE_MEM_GB   kept for the host (default 8 / 64)
#   DATA_DISK_GB   virtio data disk for the VM (default: auto, 500 GB when space allows); PASSTHROUGH_NVME=1 passes free
#                  non-root NVMe controllers through instead (they are CRYPTO-ERASED after every rental!)
#   HUGEPAGE_KIB   1048576 to back the VM with 1 GiB hugepages (reserve them at boot first); default 0
#   PYTHON         interpreter >= 3.11 for the racksdock venv (default: python3.11 or python3; deadsnakes is added on 22.04)
set -euo pipefail
trap 'echo "setup-host.sh: FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
REPOS="${REPOS:-$(cd "$HERE/../../.." && pwd)}"
PLATFORM_URL="${PLATFORM_URL:-}"
PROFILE="${PROFILE:-b300x8}"; TRUST_MODE="${TRUST_MODE:-prod}"; NET_MODE="${NET_MODE:-nat}"; NAT_SSH_PORT="${NAT_SSH_PORT:-20022}"
REGION="${REGION:-lab}"; MINER_HOTKEY="${MINER_HOTKEY:-5FminerTest}"
export DEBIAN_FRONTEND=noninteractive
step() { echo; echo "== $*"; }

step "1. packages (KVM, libvirt, OVMF, libguestfs for image builds, iptables, NVMe/PCI tools)"
apt-get update -q
apt-get install -y -q qemu-kvm qemu-utils libvirt-daemon-system libvirt-clients ovmf libguestfs-tools iptables pciutils nvme-cli jq curl chrony python3 python3-venv
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

step "2. kernel: IOMMU, KVM, vfio-pci, vhost_vsock, host NVIDIA driver blacklist"
[ -e /dev/kvm ] || { echo "ERROR: /dev/kvm missing: enable virtualization (AMD-V / VT-x) in the BIOS"; exit 1; }
if [ -z "$(ls /sys/kernel/iommu_groups 2>/dev/null)" ]; then
  echo "ERROR: no IOMMU groups. Enable IOMMU in the BIOS and add 'amd_iommu=on iommu=pt' (Intel: 'intel_iommu=on iommu=pt') to GRUB_CMDLINE_LINUX_DEFAULT, update-grub, reboot, rerun."; exit 1
fi
printf 'vfio-pci\nvhost_vsock\n' > /etc/modules-load.d/racksdock.conf
modprobe vfio-pci; modprobe vhost_vsock
grep -v '^softdep nvme' "$HERE/../mkosi.extra/etc/modprobe.d/racksdock-vfio.conf" > /etc/modprobe.d/racksdock-vfio.conf   # host driver never claims the GPUs again
update-initramfs -u >/dev/null 2>&1 || true
if ! grep -q 'iommu=pt' /proc/cmdline; then echo "NOTE: add 'iommu=pt' to GRUB_CMDLINE_LINUX_DEFAULT for best passthrough performance (not required)"; fi

step "3. libvirt + NAT network"
systemctl enable --now libvirtd >/dev/null
virsh net-info default >/dev/null 2>&1 || virsh net-define /usr/share/libvirt/networks/default.xml
virsh net-autostart default >/dev/null; virsh net-start default 2>/dev/null || true
virsh net-info default | grep -E 'Active|Bridge'

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

step "5. unload the host NVIDIA driver (the GPUs belong to the VM from now on)"
systemctl stop nvidia-persistenced nvidia-fabricmanager nvidia-dcgm 2>/dev/null || true
if grep -q '^nvidia' /proc/modules; then
  if ! (rmmod nvidia_uvm nvidia_drm nvidia_modeset nvidia 2>/dev/null || rmmod nvidia_uvm nvidia 2>/dev/null); then
    echo "WARNING: NVIDIA modules still in use (GPU processes or containers running: nvidia-smi / fuser -v /dev/nvidia*)."
    echo "         Stop them and rerun, or reboot (the blacklist is installed) and rerun."; exit 1
  fi
fi

step "6. discover VM resources -> /etc/racksdock/resources.json"
install -m 755 "$HERE/../mkosi.extra/usr/local/bin/racksdock-discover-resources" /usr/local/bin/racksdock-discover-resources
DISC=(--reserve-cpus "${RESERVE_CPUS:-8}" --reserve-mem-gb "${RESERVE_MEM_GB:-64}" --hugepage-kib "${HUGEPAGE_KIB:-0}")
[ -n "${DATA_DISK_GB:-}" ] && DISC+=(--data-disk-gb "$DATA_DISK_GB")
[ "${PASSTHROUGH_NVME:-0}" = 1 ] && DISC+=(--passthrough-nvme)
[ -n "${PCI_MMIO64_MB:-}" ] && DISC+=(--pci-mmio64-mb "$PCI_MMIO64_MB")
racksdock-discover-resources --out /etc/racksdock/resources.json "${DISC[@]}" || { echo "fix the PROBLEM lines above, then rerun"; exit 1; }

step "7. bind GPUs (and their IOMMU-group companions) to vfio-pci, now and at every boot"
install -m 755 "$HERE/../mkosi.extra/usr/local/bin/bind-vfio.sh" /usr/local/bin/bind-vfio.sh
install -m 644 "$HERE/../mkosi.extra/etc/systemd/system/racksdock-bind-vfio.service" /etc/systemd/system/
systemctl daemon-reload; systemctl enable racksdock-bind-vfio.service >/dev/null
/usr/local/bin/bind-vfio.sh
for a in $(jq -r '.gpu_pci[], .extra_pci[]' /etc/racksdock/resources.json); do printf '  %s -> %s\n' "$a" "$(basename "$(readlink -f /sys/bus/pci/devices/$a/driver)")"; done

step "8. racksdock software -> /opt/racksdock/venv"
[ -d /opt/racksdock/venv ] || "$PY" -m venv /opt/racksdock/venv
/opt/racksdock/venv/bin/pip install -q --upgrade pip
# a miner's server needs racksdock-subnet (schemas, verifier, miner neuron) and racksdock-node (host-agent); racksdock-platform
# (marketplace + tenant CLI) is the owner's software and is installed only when present, for single-box tests
PKGS=(-e "$REPOS/racksdock-subnet[validator]" -e "$REPOS/racksdock-node[nvml]")
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
    RACKSDOCK_PROFILE=$PROFILE $V devca fetch-node "$PLATFORM_URL" /etc/racksdock/node --count "$COUNT" --pci-id "$PCI_ID" --memory-mib "$MEM_MIB"
  else
    [ -f /etc/racksdock/devca/root.pem ] || $V devca init /etc/racksdock/devca
    RACKSDOCK_PROFILE=$PROFILE $V devca issue-node /etc/racksdock/devca /etc/racksdock/node --count "$COUNT" --pci-id "$PCI_ID" --memory-mib "$MEM_MIB"
  fi
  TRUST_ENV=$'RACKSDOCK_TRUST_MODE=dev\nRACKSDOCK_DEV_CA_DIR=/etc/racksdock/devca\nRACKSDOCK_SIM_NODE_DIR=/etc/racksdock/node'
else
  TRUST_ENV="RACKSDOCK_TRUST_MODE=prod"
fi

step "10. host-agent configuration"
PUBLIC_IP="${PUBLIC_IP:-$(ip -j route get 1.1.1.1 2>/dev/null | jq -r '.[0].prefsrc' 2>/dev/null || hostname -I | awk '{print $1}')}"
WS="${PLATFORM_WS:-$(echo "${PLATFORM_URL:-http://127.0.0.1:8000}" | sed -E 's#^http#ws#')/v1/hosts/ws}"
cat > /etc/racksdock/host-agent.env <<ENV
RACKSDOCK_HOST_AGENT_MODE=real
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
install -m 644 "$HERE/../mkosi.extra/etc/systemd/system/racksdock-host-agent.service" /etc/systemd/system/racksdock-host-agent.service
systemctl daemon-reload
cat /etc/racksdock/host-agent.env
echo
echo "Host ready. Next:"
echo "  build + sign the catalog images:  bash $REPOS/racksdock-platform/dev/vmtest/build-images.sh verify ubuntu24.04"
echo "  single-box workflow test:          /opt/racksdock/venv/bin/python $REPOS/racksdock-platform/dev/vmtest/run_vm_test.py"
echo "  or run the host-agent as a service: systemctl enable --now racksdock-host-agent   (journalctl -fu racksdock-host-agent)"
