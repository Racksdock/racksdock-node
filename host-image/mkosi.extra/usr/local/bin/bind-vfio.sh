#!/bin/bash
# Bind the PCI functions the VM receives to vfio-pci. Idempotent; runs at boot (racksdock-bind-vfio.service) and from setup-host.sh.
#   with /etc/racksdock/resources.json: gpu_pci + extra_pci + nvswitch_pci + nvme_pci + nic_vf_pci (the host-agent's view)
#   without it:                          every NVIDIA GPU (class 0300/0302) and NVSwitch (class 0680)
# For every GPU, the other endpoint functions in its IOMMU group are bound as well (VFIO needs the whole group; e.g. a GeForce
# card's HDMI audio function). PCI bridges are never bound. NVMe/NIC groups are NOT expanded: an unrelated device in such a
# group is a host layout problem that discover-resources reports instead.
set -euo pipefail
modprobe vfio-pci
modprobe vhost_vsock 2>/dev/null || true
bind() {
  local addr="$1" dev="/sys/bus/pci/devices/$1"
  [ -e "$dev" ] || { echo "bind-vfio: $addr not present" >&2; return 0; }
  case "$(cat "$dev/class")" in 0x0604*|0x0600*) return 0;; esac
  if [ -e "$dev/driver" ]; then
    local cur; cur="$(basename "$(readlink -f "$dev/driver")")"
    [ "$cur" = "vfio-pci" ] && return 0
    echo "$addr" > "$dev/driver/unbind"
  fi
  echo vfio-pci > "$dev/driver_override"
  echo "$addr" > /sys/bus/pci/drivers_probe
  echo "bind-vfio: $addr -> vfio-pci"
}
group_members() { local d="/sys/bus/pci/devices/$1/iommu_group/devices"; [ -d "$d" ] && ls "$d" || echo "$1"; }
RES=/etc/racksdock/resources.json
if [ -f "$RES" ]; then
  GPUS=$(python3 -c 'import json; print("\n".join(json.load(open("'"$RES"'")).get("gpu_pci", [])))')
  OTHERS=$(python3 -c 'import json; r=json.load(open("'"$RES"'")); print("\n".join(r.get("extra_pci", []) + r.get("nvswitch_pci", []) + r.get("nvme_pci", []) + ([r["nic_vf_pci"]] if r.get("nic_vf_pci") else [])))')
else
  GPUS=$(lspci -Dn | awk '$2 ~ /^030[02]:/ && $3 ~ /^10de:/ {print $1}')
  OTHERS=$(lspci -Dn | awk '$2 ~ /^0680:/ && $3 ~ /^10de:/ {print $1}')
fi
for g in $GPUS; do for m in $(group_members "$g"); do bind "$m"; done; done
for o in $OTHERS; do bind "$o"; done
exit 0
