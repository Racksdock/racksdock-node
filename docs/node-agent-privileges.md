# node-agent privileges

The node-agent runs **as root inside the VM**. Its work is privileged by nature: it formats and mounts the LUKS data disk, reads
cloud-init's copy of the user-data (root-only) to bind its hash into the attestation nonce, and reads the GPUs. The VM is the
tenant's trust domain: the tenant is root too, can read the agent's source (it ships in the image), and can stop it (the node is
then scored on heartbeat and reachability only, docs/03 §5).

What limits the agent is its protocol, not its user id:

- it only acts on messages from the host over vsock, and only two kinds: `metadata` (boot) and signed `challenge` records whose
  fields are allowlisted (attestation, health counters, 2-second micro-benchmark); everything else is ignored;
- it has **no shutdown, reboot or power command** (a test greps the package source for them); after `teardown` it erases the disk
  key, acknowledges and exits, and the host-agent ends the VM;
- it never opens listening sockets except the optional tenant metrics endpoint;
- the data-disk erase path only touches a device the agent itself set up (`DataDisk.opened`).

An earlier draft ran the agent as a dedicated user with `sudo` for the disk commands. That did not work together with the
systemd sandboxing it relied on (`NoNewPrivileges` blocks `sudo`; `ProtectSystem` puts `mount` in a private namespace the tenant
never sees), so the unit now runs as root with `ProtectHome=read-only` only.
