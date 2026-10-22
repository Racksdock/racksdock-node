#!/bin/bash
# Dual-mode node: hand the GPUs (and their IOMMU-group companions) back to the host NVIDIA driver for a container rental.
# The inverse of bind-vfio.sh. Idempotent. The NVIDIA driver is blacklisted from auto-loading on racksdock hosts and only loads
# when it finds a device, so the GPUs are unbound from vfio-pci (with driver_override=nvidia) BEFORE the module is loaded.
# Fails if any GPU does not come up under the driver.
set -euo pipefail
RES=/etc/racksdock/resources.json
GPUS=$(python3 -c 'import json; print("\n".join(json.load(open("'"$RES"'")).get("gpu_pci", [])))')
EXTRA=$(python3 -c 'import json; print("\n".join(json.load(open("'"$RES"'")).get("extra_pci", [])))')
modinfo -n nvidia >/dev/null 2>&1 || { echo "bind-host-driver: NVIDIA driver module not installed"; exit 1; }
release() {  # $1 = address, $2 = driver the device should end up on ("" = whatever the kernel picks)
  local addr="$1" drv="$2" dev="/sys/bus/pci/devices/$1"
  [ -e "$dev" ] || return 0
  if [ -e "$dev/driver" ]; then
    local cur; cur="$(basename "$(readlink -f "$dev/driver")")"
    [ "$cur" = "${drv:-$cur}" ] && [ "$cur" != "vfio-pci" ] && return 0   # already where it belongs
    echo "$addr" > "$dev/driver/unbind"
  fi
  echo "${drv}" > "$dev/driver_override" 2>/dev/null || true
}
for g in $GPUS; do release "$g" nvidia; done
for x in $EXTRA; do release "$x" ""; done   # audio functions etc.: let the kernel pick (snd_hda_intel) or leave unbound
if ! lsmod | grep -q '^nvidia '; then
  modprobe nvidia   # probes every unbound device whose driver_override says nvidia
fi
modprobe nvidia_uvm 2>/dev/null || true
# nvidia-persistenced must not run on a dual-mode host: it keeps the devices open (blocks the hand-back to vfio-pci) and the
# container runtime mounts its socket into every container when it sees one, which fails if the daemon flaps after driver load
systemctl stop nvidia-persistenced 2>/dev/null || true
pkill -x nvidia-persistenced 2>/dev/null || true
rm -rf /run/nvidia-persistenced
for g in $GPUS; do [ -e "/sys/bus/pci/devices/$g/driver" ] || echo "$g" > /sys/bus/pci/drivers_probe; done
for x in $EXTRA; do [ -e "/sys/bus/pci/devices/$x/driver" ] || echo "$x" > /sys/bus/pci/drivers_probe || true; done
for i in $(seq 1 30); do
  ok=1
  for g in $GPUS; do [ "$(basename "$(readlink -f /sys/bus/pci/devices/$g/driver 2>/dev/null)" 2>/dev/null)" = nvidia ] || ok=0; done
  [ "$ok" = 1 ] && break
  sleep 1
done
[ "$ok" = 1 ] || { echo "bind-host-driver: not every GPU is bound to nvidia"; for g in $GPUS; do echo "  $g -> $(basename "$(readlink -f /sys/bus/pci/devices/$g/driver 2>/dev/null)" 2>/dev/null)"; done; exit 1; }
n=$(echo "$GPUS" | grep -c .)
seen=$(nvidia-smi -L 2>/dev/null | grep -c "^GPU" || true)
[ "$seen" = "$n" ] || { echo "bind-host-driver: nvidia-smi sees $seen of $n GPUs"; nvidia-smi -L 2>&1 | head -3; exit 1; }
[ -e /run/nvidia-persistenced/socket ] && { echo "bind-host-driver: nvidia-persistenced came back; mask it (systemctl mask nvidia-persistenced)"; exit 1; }
modprobe -r nvidia_drm nvidia_modeset 2>/dev/null || true   # display stack is never needed on a rental host; keeps the hand-back to vfio-pci simple
nvidia-smi -pm 1 >/dev/null 2>&1 || true   # persistence mode: containers see initialized GPUs (fast nvidia-smi / CUDA start); bind-vfio.sh turns it off again
echo "bind-host-driver: $n GPU(s) on the host driver"
