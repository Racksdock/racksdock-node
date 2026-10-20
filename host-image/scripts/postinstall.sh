#!/bin/bash
# Runs inside the image at build time: install the agent into a venv and enable units.
set -euo pipefail
python3 -m venv /opt/racksdock/venv
/opt/racksdock/venv/bin/pip install --no-index --find-links /opt/racksdock/wheels racksdock-node
systemctl enable racksdock-bind-vfio.service racksdock-host-agent.service libvirtd.service chrony.service ssh.service
mkdir -p /etc/racksdock /var/lib/racksdock/images /var/lib/racksdock/vms /var/lib/racksdock-host-agent
echo "host_v2026.10.1" > /etc/racksdock/host-image-version
