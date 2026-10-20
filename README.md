# racksdock-node

Everything that runs on a miner's hardware. One Python package, `racksdock_node`, installed as `racksdock-node`, plus the non-Python assets for the host OS and the VM images.

```
src/racksdock_node/
  host_agent/    runs on the bare-metal server: libvirt VM with all 8 GPUs passed through, cloud-init seed, platform WebSocket,
                 challenge relay over vsock, recovery without tenant cooperation, signed action log, identity bootstrap
  node_agent/    runs inside every VM: SSH host key, nonce-bound attestation, status, fixed health allowlist, user-only metrics, disk-key erase
  images/        catalog manifest tooling (racksdock-images manifest build|sign|verify|os-options)
host-image/      turnkey host OS: mkosi config, vfio binding, systemd units, first-boot script
image-build/     catalog image build script, package lists, overlay with in-image tools (racksdock-launch, racksdock-memtest, racksdock-ramtest, racksdock-marker)
tests/           one directory per subpackage
docs/            per-subpackage notes
```

Install and test (requires the sibling `racksdock-subnet`):

```
pip install -e ../racksdock-subnet -e ".[test]"
pytest
```

Entry points: `racksdock-host-agent`, `racksdock-node-agent`, `racksdock-images`. Modes: `RACKSDOCK_HOST_AGENT_MODE=real|sim`, `RACKSDOCK_NODE_AGENT_MODE=real|sim`; in `sim` the "VM" is a node-agent process and GPU evidence comes from the dev CA, so the full lifecycle runs on any machine.
