"""host-agent main: executes platform commands against the hypervisor backend and relays node-agent traffic."""
from __future__ import annotations

import asyncio
import logging
import secrets
from pathlib import Path

from racksdock_subnet.core.agentproto import AgentMessage, Metadata
from racksdock_subnet.core.envelope import SignedEnvelope
from racksdock_subnet.core.identity import gpu_id_from_cert_pem, node_id_from_gpu_ids
from racksdock_subnet.core.keys import SigningKey
from racksdock_subnet.core.profile import current as current_profile
from racksdock_subnet.core.schemas import (
    BootEvidence,
    ClaimedSpec,
    HostCommand,
    HostEvent,
    HostHeartbeat,
    InstanceStatus,
    LaunchCommand,
    RegistrationRecord,
    StatusUpdate,
    VmMode,
)

from . import __version__
from .actionlog import ActionLog
from .agentlink import AgentLink
from .config import HostAgentConfig
from .hypervisor.base import HypervisorBackend, VmHandle, VmSpec
from .platformlink import PlatformLink

log = logging.getLogger("racksdock.host-agent")


class HostAgent:
    def __init__(self, cfg: HostAgentConfig, backend: HypervisorBackend | None = None):
        self.cfg = cfg
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.key = SigningKey.load_or_create(cfg.key_path)
        self.backend = backend or self._make_backend()
        self.agentlink = AgentLink(cfg.agent_transport, cfg.agent_port)
        self.agentlink.on_message = self._on_agent_message
        self.platform = PlatformLink(cfg.platform_ws, self.key, self._hello)
        self.platform.on_command = self._on_command
        self.log = ActionLog(cfg.state_dir / "actions.log", self.key)
        self.node_id: str | None = self._load_node_id()
        self.current: VmHandle | None = None
        self.current_launch: LaunchCommand | None = None
        self.maintenance_until: str | None = None
        self._pending_identity: asyncio.Future | None = None
        self._teardown_ack: asyncio.Event = asyncio.Event()
        self._boot_timer: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ---- setup ---------------------------------------------------------------
    def _make_backend(self) -> HypervisorBackend:
        if self.cfg.sim:
            from .hypervisor.sim_backend import SimBackend

            return SimBackend(self.cfg.agent_port, self.cfg.trust.dev_ca_dir, self.cfg.sim_node_dir, self.cfg.sim_node_agent_cmd, self.cfg.sim_faults_file)
        if self.cfg.static:
            from .hypervisor.static_backend import StaticBackend

            env = {}
            if self.cfg.trust.dev:
                env.update({"RACKSDOCK_TRUST_MODE": "dev", "RACKSDOCK_DEV_CA_DIR": str(self.cfg.trust.dev_ca_dir), "RACKSDOCK_SIM_NODE_DIR": str(self.cfg.sim_node_dir)})
            return StaticBackend(self.cfg.agent_port, self.cfg.static_public_ip, self.cfg.static_ssh_port, self.cfg.static_authorized_keys,
                                 self.cfg.sim_node_agent_cmd, env, self.cfg.state_dir / "static", ssh_service=self.cfg.static_ssh_service)
        from .hypervisor.libvirt_backend import LibvirtBackend

        n = self.cfg.network
        return LibvirtBackend(bridge=n.bridge, public_ip=n.public_ip, net_mode=n.mode, nat_network=n.nat_network, nat_ssh_port=n.nat_ssh_port,
                              guest_files=self._dev_guest_files())

    def _dev_guest_files(self) -> dict[str, str]:
        """Dev trust mode on real hardware (GPUs without device attestation): the VM needs the dev CA certificates and this node's
        simulated identity, which the host hands over through cloud-init vendor-data. Private CA keys never leave the platform;
        the node's simulated device keys are the dev stand-in for keys a real GPU holds in hardware. Empty in production."""
        t = self.cfg.trust
        if not t.dev or t.dev_ca_dir is None or self.cfg.sim_node_dir is None:
            return {}
        files = {"/etc/racksdock/node-agent.env": "RACKSDOCK_TRUST_MODE=dev\nRACKSDOCK_DEV_CA_DIR=/etc/racksdock/devca\nRACKSDOCK_SIM_NODE_DIR=/etc/racksdock/node\n"}
        if self.cfg.dev_ssh_pubkey:
            # operator access to every VM for debugging the real path on a lab box; cloud-init adds it to the default user
            files["__ssh_authorized_keys__"] = self.cfg.dev_ssh_pubkey.strip() + "\n"
        for src, dst in ((Path(t.dev_ca_dir), "/etc/racksdock/devca"), (Path(self.cfg.sim_node_dir), "/etc/racksdock/node")):
            if not src.exists():
                raise RuntimeError(f"dev trust mode: {src} missing (racksdock-verify devca fetch / fetch-node)")
            for f in sorted(src.rglob("*")):
                if f.is_file() and not (dst.endswith("devca") and f.suffix == ".key"):
                    files[f"{dst}/{f.relative_to(src)}"] = f.read_text()
        return files

    def _load_node_id(self) -> str | None:
        p = self.cfg.state_dir / "node_id"
        return p.read_text().strip() if p.exists() else None

    def _hello(self) -> HostEvent:
        return HostEvent(kind="hello", node_id=self.node_id, payload={
            "host_pubkey": self.key.public.hex, "host_image_version": self.cfg.host_image_version, "host_agent_version": __version__,
            "miner_hotkey": self.cfg.miner_hotkey, "region": self.cfg.region, "inventory": self.backend.host_inventory(),
            "vm_state": "running" if self.current else "none", "instance_id": self.current.instance_id if self.current else None,
        })

    async def run(self) -> None:
        log.info("host-agent started: mode=%s node_id=%s host_key=%s platform=%s region=%s", self.cfg.mode, (self.node_id or "unregistered")[:24],
                 self.key.public.hex[:12], self.cfg.platform_ws, self.cfg.region)
        await self.agentlink.start()
        if self.cfg.sim or self.cfg.static:
            # process-based backends need the actual bound port
            self.backend.agent_port = self.agentlink.port  # type: ignore[attr-defined]
        else:
            cleanup = getattr(self.backend, "cleanup_stale", None)
            if cleanup is not None:
                stale = await cleanup(self.cfg.vm_dir)
                if stale:
                    log.warning("removed %d stale VM(s) left by a previous host-agent: %s", len(stale), ", ".join(stale))
            preflight = getattr(self.backend, "preflight", None)
            if preflight is not None:
                for problem in await preflight(self.cfg.resources):
                    log.warning("preflight: %s", problem)
        hb = asyncio.create_task(self._heartbeat_loop())
        try:
            await self.platform.run()
        finally:
            hb.cancel()
            await self.agentlink.stop()

    def stop(self) -> None:
        self.platform.stop()

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.heartbeat_interval_s)
            await self.platform.send(HostEvent(kind="heartbeat", node_id=self.node_id, instance_id=self.current.instance_id if self.current else None,
                                               payload=HostHeartbeat(node_id=self.node_id or "", host_pubkey=self.key.public.hex,
                                                                     host_image_version=self.cfg.host_image_version,
                                                                     vm_state=(await self.backend.state(self.current)) if self.current else None,
                                                                     instance_id=self.current.instance_id if self.current else None,
                                                                     maintenance_until=self.maintenance_until).model_dump()))

    # ---- platform commands ---------------------------------------------------
    async def _on_command(self, cmd: HostCommand) -> None:
        if cmd.kind != "ping":
            log.info("command %s instance=%s", cmd.kind, cmd.instance_id or "-")
        self.log.record(cmd.kind, command_id=cmd.command_id, instance_id=cmd.instance_id)
        handler = getattr(self, f"_cmd_{cmd.kind}", None)
        if handler is None:
            await self.platform.send(HostEvent(kind="error", command_id=cmd.command_id, payload={"error": f"unknown command {cmd.kind}"}))
            return
        await handler(cmd)

    async def _status(self, instance_id: str, status: InstanceStatus, details: str | None = None, **extra) -> None:
        upd = StatusUpdate(status=status, status_details=details, **extra)
        await self.platform.send(HostEvent(kind="status", node_id=self.node_id, instance_id=instance_id, payload=upd.model_dump(mode="json")))

    async def _cmd_ping(self, cmd: HostCommand) -> None:
        await self.platform.send(HostEvent(kind="ack", command_id=cmd.command_id, node_id=self.node_id, payload={"pong": True}))

    async def _cmd_launch(self, cmd: HostCommand) -> None:
        lc = LaunchCommand.model_validate(cmd.payload)
        async with self._lock:
            if self.current is not None:
                if self.current.instance_id == lc.instance_id:
                    await self.platform.send(HostEvent(kind="ack", command_id=cmd.command_id, instance_id=lc.instance_id, payload={"already": True}))
                    return
                # a canary or stale VM is in the way: tear it down first (docs/02 §10)
                await self._teardown_current(reason="preempted by launch")
            await self._status(lc.instance_id, InstanceStatus.pending_provider, "launching VM")
            meta = Metadata(instance_id=lc.instance_id, node_id=self.node_id, tenant_nonce=lc.tenant_nonce, mode=lc.mode.value,
                            image_version=lc.image_version, user_data=lc.user_data, ssh_port=lc.ssh_port)
            spec = VmSpec(instance_id=lc.instance_id, image=lc.image, image_version=lc.image_version, user_data=lc.user_data, metadata=meta,
                          resources=self.cfg.resources, vm_dir=self.cfg.vm_dir / lc.instance_id,
                          image_path=self.cfg.image_dir / f"{lc.image_version}.qcow2", hostname=f"racksdock-{lc.instance_id[-8:]}",
                          public_ip=self.cfg.network.public_ip, ssh_port=lc.ssh_port)
            self.current_launch = lc
            self._teardown_ack.clear()
            try:
                self.current = await self.backend.launch(spec)
            except Exception as e:
                self.current_launch = None
                await self._status(lc.instance_id, InstanceStatus.error, f"launch failed: {e}")
                return
            self.log.record("vm_started", instance_id=lc.instance_id, mode=lc.mode.value, image=lc.image)
            await self.platform.send(HostEvent(kind="ack", command_id=cmd.command_id, instance_id=lc.instance_id, payload={"ip": self.current.ip, "ssh_port": self.current.ssh_port}))
            self._boot_timer = asyncio.create_task(self._boot_watchdog(lc.instance_id))

    async def _boot_watchdog(self, instance_id: str) -> None:
        await asyncio.sleep(self.cfg.boot_timeout_s)
        if self.current and self.current.instance_id == instance_id and self.current.extra.get("active") is not True:
            await self._status(instance_id, InstanceStatus.error, "boot timeout on host")
            async with self._lock:
                await self._teardown_current(reason="boot timeout")

    async def _cmd_teardown(self, cmd: HostCommand) -> None:
        async with self._lock:
            if self.current is None or (cmd.instance_id and self.current.instance_id != cmd.instance_id):
                await self.platform.send(HostEvent(kind="ack", command_id=cmd.command_id, instance_id=cmd.instance_id, payload={"nothing_to_do": True}))
                if cmd.instance_id:
                    await self._status(cmd.instance_id, InstanceStatus.deleted, "no VM present")
                return
            iid = self.current.instance_id
            await self._teardown_current(reason="platform teardown")
            await self._status(iid, InstanceStatus.deleted, "VM destroyed, devices reset, NVMe sanitized")
            await self.platform.send(HostEvent(kind="ack", command_id=cmd.command_id, instance_id=iid, payload={}))

    async def _teardown_current(self, reason: str) -> None:
        assert self.current is not None
        handle = self.current
        if self._boot_timer:
            self._boot_timer.cancel()
        acked = False
        if await self.agentlink.send(AgentMessage(type="teardown", payload={})):
            try:
                await asyncio.wait_for(self._teardown_ack.wait(), self.cfg.teardown_ack_timeout_s)
                acked = True
            except TimeoutError:
                pass
        await self.agentlink.drop()
        await self.backend.destroy(handle)
        reset = await self.backend.reset_devices(self.cfg.resources)
        sanitized = await self.backend.sanitize_nvme(self.cfg.resources)
        self.log.record("vm_destroyed", instance_id=handle.instance_id, reason=reason, node_agent_acked=acked, devices_reset=len(reset), nvme_sanitized=len(sanitized))
        self.current = None
        self.current_launch = None

    async def _cmd_restart(self, cmd: HostCommand) -> None:
        if self.current is None:
            await self.platform.send(HostEvent(kind="error", command_id=cmd.command_id, payload={"error": "no VM"}))
            return
        hard = bool(cmd.payload.get("hard", False))
        await self.backend.reboot(self.current, hard=hard)
        self.log.record("vm_rebooted", instance_id=self.current.instance_id, hard=hard)
        if self.cfg.sim or self.cfg.static:
            # process-based backends: relaunch the node-agent so it reconnects (disk is kept)
            lc = self.current_launch
            if lc:
                meta = Metadata(instance_id=lc.instance_id, node_id=self.node_id, tenant_nonce=lc.tenant_nonce, mode=lc.mode.value, image_version=lc.image_version, user_data=lc.user_data, ssh_port=lc.ssh_port)
                spec = VmSpec(instance_id=lc.instance_id, image=lc.image, image_version=lc.image_version, user_data=lc.user_data, metadata=meta,
                              resources=self.cfg.resources, vm_dir=self.cfg.vm_dir / lc.instance_id, image_path=Path("/dev/null"))
                old = self.current
                self.current = await self.backend.launch(spec)
                self.current.ssh_port = old.ssh_port
        await self.platform.send(HostEvent(kind="ack", command_id=cmd.command_id, instance_id=self.current.instance_id, payload={"restarted": True}))

    async def _cmd_gpu_reset(self, cmd: HostCommand) -> None:
        if self.current is not None:
            await self.platform.send(HostEvent(kind="error", command_id=cmd.command_id, payload={"error": "VM running; teardown first"}))
            return
        reset = await self.backend.reset_devices(self.cfg.resources)
        self.log.record("gpu_reset", devices=reset)
        await self.platform.send(HostEvent(kind="ack", command_id=cmd.command_id, payload={"reset": reset}))

    async def _cmd_challenge(self, cmd: HostCommand) -> None:
        env = SignedEnvelope.model_validate(cmd.payload)
        if not env.verify() or env.kind != "challenge":
            await self.platform.send(HostEvent(kind="error", command_id=cmd.command_id, payload={"error": "challenge not signed"}))
            return
        if self.current is None or not await self.agentlink.send(AgentMessage(type="challenge", payload=env.model_dump())):
            await self.platform.send(HostEvent(kind="challenge_response", command_id=cmd.command_id, instance_id=cmd.instance_id, node_id=self.node_id,
                                               payload={"error": "node-agent unreachable"}))
            return
        self._pending_challenges[env.payload.get("nonce")] = cmd.command_id

    _pending_challenges: dict[str, str] = {}

    async def _cmd_announce_maintenance(self, cmd: HostCommand) -> None:
        self.maintenance_until = cmd.payload.get("ends_at")
        await self.platform.send(HostEvent(kind="ack", command_id=cmd.command_id, node_id=self.node_id, payload={"maintenance_until": self.maintenance_until}))

    async def _cmd_bootstrap_identity(self, cmd: HostCommand) -> None:
        """Boot a verify VM with a random nonce, derive gpu_ids from its evidence, write a registration draft."""
        loop = asyncio.get_running_loop()
        self._pending_identity = loop.create_future()
        image = cmd.payload.get("image", "verify")
        image_version = cmd.payload.get("image_version", "v0")
        lc = LaunchCommand(instance_id=f"bootstrap-{secrets.token_hex(4)}", node_id=self.node_id or "", mode=VmMode.verify, image=image,
                           image_version=image_version, user_data="#cloud-config\n{}\n", tenant_nonce=secrets.token_hex(32))
        await self._cmd_launch(HostCommand(kind="launch", command_id=cmd.command_id + ":launch", instance_id=lc.instance_id, payload=lc.model_dump(mode="json")))
        try:
            ev: BootEvidence = await asyncio.wait_for(self._pending_identity, timeout=self.cfg.boot_timeout_s)
        except TimeoutError:
            await self.platform.send(HostEvent(kind="error", command_id=cmd.command_id, payload={"error": "no evidence from bootstrap VM"}))
            return
        finally:
            self._pending_identity = None
        from cryptography.hazmat.primitives import serialization
        from racksdock_subnet.verifier.verifiers import split_chain

        gpu_ids = [gpu_id_from_cert_pem(split_chain(d.certificate)[0].public_bytes(serialization.Encoding.PEM)) for d in ev.bundle.devices]
        node_id = node_id_from_gpu_ids(gpu_ids)
        inv = self.backend.host_inventory()
        rec = RegistrationRecord(
            node_id=node_id, gpu_ids=gpu_ids, profile=current_profile().name, miner_hotkey=self.cfg.miner_hotkey, miner_service_key=self.key.public.hex,
            claimed_spec=ClaimedSpec(vcpus=self.cfg.resources.vcpus, cpu_model=inv.get("cpu_model", ""), memory_gb=self.cfg.resources.memory_gb,
                                     nvme_tb=round(sum(d.get("PhysicalSize", 0) for d in inv.get("nvme", [])) / 1e12, 1),
                                     network_gbps=float(cmd.payload.get("network_gbps", 25)), region=self.cfg.region, oem_platform=self.cfg.oem_platform),
            host_image_version=self.cfg.host_image_version, board_serial=inv.get("board_serial"),
        )
        self.node_id = node_id
        (self.cfg.state_dir / "node_id").write_text(node_id + "\n")
        (self.cfg.state_dir / "registration.json").write_text(rec.model_dump_json(by_alias=True, indent=1))
        self.log.record("identity_bootstrapped", node_id=node_id)
        reg_env = SignedEnvelope.create("registration", rec.model_dump(by_alias=True), self.key)
        (self.cfg.state_dir / "registration.envelope.json").write_text(reg_env.model_dump_json(indent=1))
        await self.platform.send(HostEvent(kind="identity", command_id=cmd.command_id, node_id=node_id, payload={
            "registration": rec.model_dump(by_alias=True), "registration_envelope": reg_env.model_dump(), "evidence": ev.model_dump(mode="json")}))
        async with self._lock:
            if self.current and self.current.instance_id == lc.instance_id:
                await self._teardown_current(reason="bootstrap complete")

    # ---- node-agent messages -------------------------------------------------
    async def _on_agent_message(self, msg: AgentMessage) -> None:
        if self.current is None:
            log.warning("node-agent message %s with no VM registered; ignored", msg.type)
            return
        iid = self.current.instance_id
        if msg.type not in ("status", "log"):
            log.info("node-agent %s for %s", msg.type, iid)
        if msg.type == "hello":
            lc = self.current_launch
            if lc is None:
                return
            meta = Metadata(instance_id=iid, node_id=self.node_id, tenant_nonce=lc.tenant_nonce, mode=lc.mode.value, image_version=lc.image_version,
                            user_data=lc.user_data if self.cfg.metadata_carries_user_data else None, ssh_port=lc.ssh_port)
            await self.agentlink.send(AgentMessage(type="metadata", payload=meta.model_dump()))
        elif msg.type == "evidence":
            env = SignedEnvelope.model_validate(msg.payload)
            if env.verify() and env.kind == "boot_evidence":
                ev = BootEvidence.model_validate(env.payload)
                if self._pending_identity is not None and not self._pending_identity.done():
                    self._pending_identity.set_result(ev)
            await self.platform.send(HostEvent(kind="evidence", node_id=self.node_id, instance_id=iid, payload=env.model_dump()))
        elif msg.type == "status":
            upd = StatusUpdate.model_validate(msg.payload)
            log.info("instance %s -> %s %s", iid, upd.status.value, upd.status_details or "")
            upd.ip = self.current.ip
            upd.ssh_port = self.current.ssh_port
            if upd.status == InstanceStatus.active:
                self.current.extra["active"] = True
            await self.platform.send(HostEvent(kind="status", node_id=self.node_id, instance_id=iid, payload=upd.model_dump(mode="json")))
        elif msg.type == "challenge_response":
            nonce = (msg.payload.get("payload") or {}).get("nonce")
            command_id = self._pending_challenges.pop(nonce, None)
            await self.platform.send(HostEvent(kind="challenge_response", command_id=command_id, node_id=self.node_id, instance_id=iid, payload=msg.payload))
        elif msg.type == "teardown_ack":
            self._teardown_ack.set()
        elif msg.type == "log":
            lvl = logging.WARNING if msg.payload.get("level") in ("warning", "error") else logging.INFO
            log.log(lvl, "node-agent %s: %s", iid, msg.payload.get("msg"))
