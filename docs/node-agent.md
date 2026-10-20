# node-agent (`racksdock_node.node_agent`)

Runs inside every node VM (docs/01 §2, docs/03 §7). Baked into every catalog image as a systemd unit.

What it does, in order:
1. Connects to the host-agent over vsock (real) or TCP (simulation) and says hello.
2. Receives metadata (instance id, tenant nonce, mode). Reads the user-data cloud-init actually applied.
3. Ensures the VM's SSH host key exists, composes `boot_nonce = SHA256(tenant_nonce || host_key || SHA256(user_data))`, collects GPU attestation evidence for that nonce, and sends the boot evidence.
4. Reports `pending`, waits for cloud-init to finish, reports `active` with the launch-config status.
5. Answers validator challenges relayed by the host: fresh attestation plus the fixed health allowlist. There is no code path for utilization, memory use, power draw, temperatures, processes or network counters in tenant mode.
6. Serves user-only metrics on 127.0.0.1:9100 when the tenant enabled them (reach it through an SSH tunnel).
7. On teardown: zeroizes the data-disk key, erases the LUKS header, acknowledges, exits. It never powers off or reboots the machine; the host-agent shuts the VM down and destroys it.

Runs as the unprivileged `racksdock-agent` user with access to `/dev/nvidia*` and the vsock device only (see `systemd/`). The tenant is root and may stop it; the platform then scores the node on heartbeat and reachability only.

Modes: `RACKSDOCK_NODE_AGENT_MODE=real|sim`. In `sim`, GPU evidence comes from `racksdock-verifier`'s dev CA and the "VM" is just this process.
