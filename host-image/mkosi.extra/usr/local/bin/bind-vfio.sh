#!/bin/bash
# Bind every NVIDIA GPU (class 0302/0300) and NVSwitch (class 0680, vendor 10de), plus the data NVMe controllers listed
# in /etc/racksdock/resources.json, to vfio-pci. Idempotent.
set -euo pipefail
modprobe vfio-pci
bind() {
  local addr="$1"
  local dev="/sys/bus/pci/devices/$addr"
  [ -e "$dev" ] || return 0
  if [ -e "$dev/driver" ] && [ "$(basename "$(readlink -f "$dev/driver")")" != "vfio-pci" ]; then
    echo "$addr" > "$dev/driver/unbind"
  fi
  echo vfio-pci > "$dev/driver_override"
  echo "$addr" > /sys/bus/pci/drivers_probe
}
for addr in $(lspci -Dn | awk '$2 ~ /^030[02]:/ && $3 ~ /^10de:/ {print $1}'); do bind "$addr"; done
for addr in $(lspci -Dn | awk '$2 ~ /^0680:/ && $3 ~ /^10de:/ {print $1}'); do bind "$addr"; done
if [ -f /etc/racksdock/resources.json ]; then
  for addr in $(python3 -c 'import json,sys; print("\n".join(json.load(open("/etc/racksdock/resources.json")).get("nvme_pci", [])))'); do bind "$addr"; done
  vf=$(python3 -c 'import json; print(json.load(open("/etc/racksdock/resources.json")).get("nic_vf_pci") or "")')
  [ -n "$vf" ] && bind "$vf"
fi
exit 0
