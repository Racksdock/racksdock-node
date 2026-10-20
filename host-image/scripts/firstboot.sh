#!/bin/bash
# First boot on the miner's server: collect the few inputs the miner must provide and discover the rest.
set -euo pipefail
read -rp "Miner hotkey (ss58): " HOTKEY
read -rp "Platform WebSocket URL [wss://api.example.com/v1/hosts/ws]: " WS; WS=${WS:-wss://api.example.com/v1/hosts/ws}
read -rp "Region label (e.g. eu-north-1): " REGION
read -rp "Public IPv4 for tenant VMs (blank for NAT): " PUBIP
# GPUs (+ companion functions in their IOMMU groups), NVSwitches, every free non-root NVMe controller, CPU/memory budget,
# 1 GiB hugepages (the host image reserves them at boot) and the wide OVMF MMIO window eight datacenter GPUs need
racksdock-discover-resources --out /etc/racksdock/resources.json --passthrough-nvme --hugepage-kib 1048576 --pci-mmio64-mb 2048000
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
