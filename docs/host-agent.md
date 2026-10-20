# host-agent (`racksdock_node.host_agent`)

Runs on the miner's bare-metal HGX B300 server as part of the turnkey host image (docs/01 §2, §5). Treated as untrusted by validators: everything it reports is cross-checked from inside the VM.

Responsibilities
- Dial out to the marketplace over mTLS WebSocket (`RACKSDOCK_PLATFORM_WS`), authenticate with the host's ed25519 key, receive `HostCommand`s and send signed `HostEvent`s.
- `launch`: build the cloud-init NoCloud seed (user-data from the request, vendor-data with instance metadata), create a qcow2 overlay on the signed catalog image, generate the libvirt domain (OVMF, 1 GB hugepages, NUMA-pinned vCPUs, all GPU + NVSwitch + data-NVMe + NIC-VF PCI functions as `hostdev`, vsock), start it, then relay the node-agent's evidence and status.
- `challenge`: relay a validator's signed challenge to the node-agent over vsock and return the signed response.
- `teardown`: ask the node-agent to erase its disk key, destroy the VM, reset GPUs/NVSwitches (FLR), `nvme sanitize` the data NVMe, delete the overlay.
- `restart`, `gpu_reset`, `announce_maintenance`, `bootstrap_identity` (boots a verify VM to collect the eight GPU identities and writes a registration draft for the miner neuron).
- Heartbeats, host inventory for cross-checks, a signed action log streamed to the platform.

Modes: `RACKSDOCK_HOST_AGENT_MODE=real` (libvirt/QEMU) or `sim` (the "VM" is a `racksdock-node-agent --mode sim` process plus a fake SSH banner listener; used by the simulator and tests).

`host-image/` (repo root) holds the mkosi configuration and first-boot scripts for the host OS.

## Networking modes (`RACKSDOCK_NET_MODE`)

- `bridge` (default): the VM gets `RACKSDOCK_PUBLIC_IP` on `RACKSDOCK_NET_BRIDGE`; one public IP per node.
- `nat`: the VM sits on the libvirt NAT network `RACKSDOCK_NAT_NETWORK` (default `default`) and the host forwards
  `host:RACKSDOCK_NAT_SSH_PORT` (default 20022) to VM:22 with tagged iptables rules (`racksdock-nat-<instance>`), installed once the
  guest has its DHCP lease and removed at teardown. `RACKSDOCK_PUBLIC_IP` is then the host address tenants connect to (auto-detected
  from the default route when unset). For single-IP hosts and lab boxes.
- A passed-through NIC VF (`nic_vf_pci` in resources.json) replaces both.

## Host preparation and resources

`host-image/scripts/setup-host.sh` turns an existing Ubuntu 22.04/24.04 bare-metal server into a host without reimaging
(packages, modules, libvirt network, driver unbinding, resource discovery, vfio binding, venv, env). `racksdock-discover-resources`
writes `/etc/racksdock/resources.json` (`VmResources`): GPUs, `extra_pci` (other endpoint functions in a GPU's IOMMU group, e.g.
GeForce audio), NVSwitches, free non-root NVMe controllers (only with `--passthrough-nvme`: they are crypto-erased after every
rental), CPU/memory budget, `data_disk_gb` (virtio data disk when no NVMe is passed through), `hugepage_kib` (0 = no hugepage
backing), `pci_mmio64_mb` (OVMF 64-bit MMIO window; 8xB300 needs about 2 TB). `bind-vfio.sh` binds the GPUs plus their IOMMU-group
companions and the listed NVMe/NIC functions.

At start in `real` mode the host-agent destroys stale `racksdock-*` VMs and tagged NAT rules (no VM may survive a host-agent
restart; it would hold the GPUs) and logs `preflight:` problems (tools, OVMF, /dev/kvm, vhost_vsock, libvirt network, vfio binding,
hugepages). Images are addressed by catalog version: `<image_dir>/<os>_<release>.qcow2`, verified against
`manifest.signed.json` + `catalog.pub` in the same directory.

In dev trust mode on real hardware (GPUs without device attestation) the host passes the dev CA certificates (never its private
keys), the node's simulated identity and `/etc/racksdock/node-agent.env` to the VM through cloud-init vendor-data. Production hosts
have no dev CA material and pass nothing.
