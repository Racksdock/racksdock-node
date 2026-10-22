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

## Container tier (`RACKSDOCK_HOST_AGENT_MODE=docker`)

Docs/01 §10. The rental is a root Docker container on the host's kernel: `--gpus all` (NVIDIA container toolkit), CPU and memory
from `resources.json`, `--shm-size` for NCCL, sshd on the forwarded port (`-p <nat_ssh_port>:22`), nested Docker inside
(privileged by default; `RACKSDOCK_DOCKER_PRIVILEGED=0` for a sysbox-style runtime), an encrypted data volume the host creates per
rental (LUKS2 on a sparse file, key in `/run` for the rental's lifetime, erased at delete) mounted at `/data`, and the node-agent
talking to the host over TCP at the docker bridge gateway instead of vsock. The container is pinned to its first `vcpus` host CPUs (`--cpuset-cpus`) so `nproc` inside reports the rented count; `free`
still shows the host's memory (cgroup v2 enforces the `--memory` limit, but /proc/meminfo is not virtualized without lxcfs).
The container's PID 1 is `racksdock-container-init`,
which applies the tenant's cloud-config subset (users, keys, write_files, runcmd), starts sshd, dockerd, DCGM (verify image) and
the launch configuration, then supervises the node-agent. Images are docker images `racksdock/<os>:<release>` pinned by image id in
the signed manifest (`racksdock-images manifest build --containers`). `restart` = `docker restart`, `teardown` = container removal
plus volume erase; stale `racksdock-*` containers are removed at host-agent start. Set up a host with
`DEPLOYMENT=container bash host-image/scripts/setup-host.sh`; no VFIO, libvirt or hugepages are involved.

## Dual mode (`RACKSDOCK_HOST_AGENT_MODE=dual`)

Docs/01 §10.1. One bare-metal host offers both tiers; the platform sends the customer's pick in `LaunchCommand.deployment`
(`vm` by default). `DualBackend` wraps `LibvirtBackend` and `DockerBackend`: before a launch it reads which driver the GPUs are on
(`vfio-pci` = VM mode, `nvidia` = container mode) and, only when the pick differs, runs `bind-host-driver.sh` (unbind from
vfio-pci with `driver_override=nvidia`, load the blacklisted `nvidia` module, bind, wait for `nvidia-smi` to see every GPU) or
`bind-vfio.sh`; the last mode stays while idle. `nvidia-persistenced` is masked on dual hosts (setup-host.sh) and stopped by
`bind-host-driver.sh`: udev starts it on every driver bind, it pins the devices (blocking the hand-back to VFIO) and when it
crash-loops its socket flaps, which makes the NVIDIA container runtime fail container starts (`failed to fulfil mount request:
/run/nvidia-persistenced/socket`). The docker backend retries a container start once on `OCI runtime create failed`. The 1 GiB hugepage pool reserved at boot for VMs is released
(`nr_hugepages=0`, the reservation remembered in `<state_dir>/hugepages_reserved`) when switching to container mode and re-reserved
before the next VM (drop caches + `compact_memory`, up to 15 attempts; if fewer pages come back than the VM needs, that VM is
launched with `hugepage_kib=0`). The agent link listens on vsock (VMs) and TCP at the docker bridge gateway (containers) at the
same port. Docker rentals on a dual node use an encrypted volume on the OS disk sized by `RACKSDOCK_DOCKER_DATA_GB`
(default: `data_disk_gb`, else 200 GiB); the passthrough NVMe drives stay for VMs. Set up with
`DEPLOYMENT=dual bash host-image/scripts/setup-host.sh` (installs both package sets, the NVIDIA open driver of the container
tier's reference hosts, both bind scripts); a node already in VM mode switches by installing the driver + container toolkit,
building the container images (`dev/vmtest/build-container-images.sh verify ubuntu24.04`, which also lists the VM images in one
manifest), setting `RACKSDOCK_HOST_AGENT_MODE=dual`, restarting the host-agent and re-registering with
`racksdock-miner bootstrap --reuse-identity`.

## Catalog sync

`RACKSDOCK_CATALOG_URL` (default `http://<platform>/catalog/current`, derived from `RACKSDOCK_PLATFORM_WS`; empty disables) is
synced at start and every `RACKSDOCK_CATALOG_SYNC_S` (600 s) by `racksdock_node.images.sync`: the signed manifest is verified
against the key pinned in `/etc/racksdock/catalog.pub` (written on first contact), missing images of the node's kinds are
downloaded and hash-checked (container archives are `docker load`ed and their ids checked), then `manifest.signed.json` and
`catalog.pub` are written into the image dir. Docker storage drivers assign different ids to the same archive (containerd
snapshotter: manifest digest; classic overlay2: config digest), so the id a hash-verified archive gets on this host is recorded
in `<version>.container.loaded` and accepted next to the catalog's id. The present images (`<version>:<kind>`) travel in hello
and heartbeat, and the marketplace only offers those. Manual: `racksdock-images fetch <url> --dest /var/lib/racksdock/images --kinds vm,container`,
`racksdock-images present`.

## Ports (NAT mode)

`RACKSDOCK_NAT_EXPOSE=all` (default): the whole public IP is the tenant's. `hypervisor/nat.py` installs, per rental and tagged
`racksdock-nat-<instance_id>`, a TCP catch-all DNAT to the rental for every port except `RACKSDOCK_NAT_RESERVED_PORTS` (default
`22`, the host's sshd) and the advertised ssh port, a UDP catch-all, the matching OUTPUT rule for host-local clients, a FORWARD
accept, and the specific ssh forward `<public_ip>:<nat_ssh_port> -> rental:22` on top. VMs get the rules once their DHCP lease is
known; containers get them right after `docker run` on their bridge address (Docker's own `-p` publishes ssh). `=ssh` keeps only the
ssh forward. Rules are removed at teardown and at host-agent start for rentals the agent has no record of.

## Restarts

The running rental is recorded in `<state_dir>/current.json` (handle + launch command) and re-adopted at start when it is still
running (`virsh domstate` / `docker inspect`); `cleanup_stale(keep=...)` then leaves that domain or container, its NAT rules and
its files alone and destroys only `racksdock-*` leftovers the agent has no record of. Upgrading or restarting the host-agent under a
tenant is therefore safe; a rental that was still booting gets a fresh boot watchdog.
