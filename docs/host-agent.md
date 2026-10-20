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
