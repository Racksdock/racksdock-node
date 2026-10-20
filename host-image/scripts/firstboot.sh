#!/bin/bash
# First boot on the miner's server: collect the few inputs the miner must provide and discover the rest.
set -euo pipefail
read -rp "Miner hotkey (ss58): " HOTKEY
read -rp "Platform WebSocket URL [wss://api.example.com/v1/hosts/ws]: " WS; WS=${WS:-wss://api.example.com/v1/hosts/ws}
read -rp "Region label (e.g. eu-north-1): " REGION
read -rp "Public IPv4 for tenant VMs (blank for NAT): " PUBIP
GPUS=$(lspci -Dn | awk '$2 ~ /^030[02]:/ && $3 ~ /^10de:/ {print $1}' | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read().split()))')
SW=$(lspci -Dn | awk '$2 ~ /^0680:/ && $3 ~ /^10de:/ {print $1}' | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read().split()))')
# data NVMe = every NVMe controller except the one holding the root filesystem
ROOTDEV=$(findmnt -n -o SOURCE / | sed -E 's/p?[0-9]+$//' | xargs basename)
NVME=$(for c in /sys/class/nvme/nvme*; do n=$(basename "$c"); [ "$n" = "$ROOTDEV" ] && continue; basename "$(readlink -f "$c/device")"; done | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read().split()))')
CORES=$(nproc); MEMGB=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
cat > /etc/racksdock/resources.json <<JSON
{"gpu_pci": $GPUS, "nvswitch_pci": $SW, "nvme_pci": $NVME, "nic_vf_pci": null, "vcpus": $((CORES-8)), "memory_gb": $((MEMGB-64)), "os_disk_gb": 200}
JSON
cat > /etc/racksdock/host-agent.env <<ENV
RACKSDOCK_HOST_AGENT_MODE=real
RACKSDOCK_PLATFORM_WS=$WS
RACKSDOCK_MINER_HOTKEY=$HOTKEY
RACKSDOCK_REGION=$REGION
RACKSDOCK_HOST_IMAGE_VERSION=$(cat /etc/racksdock/host-image-version)
RACKSDOCK_RESOURCES_JSON=/etc/racksdock/resources.json
RACKSDOCK_NET_MODE=$([ -n "$PUBIP" ] && echo bridge || echo nat)
RACKSDOCK_PUBLIC_IP=$PUBIP
RACKSDOCK_TRUST_MODE=prod
ENV
systemctl restart racksdock-bind-vfio.service racksdock-host-agent.service
echo "Done. The agent will enroll and the miner neuron can now run 'racksdock-miner bootstrap' to register this node."
