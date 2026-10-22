"""host-agent main: executes platform commands against the hypervisor backend and relays node-agent traffic."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from dataclasses import asdict
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
EVIDENCE_MIN_INTERVAL_S = 60.0  # boot evidence relayed at most this often once an instance is active


class HostAgent:
    def __init__(self, cfg: HostAgentConfig, backend: HypervisorBackend | None = None):
        self.cfg = cfg
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.key = SigningKey.load_or_create(cfg.key_path)
        self.backend = backend or self._make_backend()
        bind = cfg.agent_bind_host
        if (cfg.docker or cfg.dual) and not bind:
            from .hypervisor.docker_backend import DockerBackend

            bind = DockerBackend.bridge_gateway(cfg.docker_bridge)
        if cfg.dual:
            from .agentlink import MultiAgentLink

            self.agentlink = MultiAgentLink([AgentLink("vsock", cfg.agent_port), AgentLink("tcp", cfg.agent_port, bind_host=bind or "127.0.0.1")])
        else:
            self.agentlink = AgentLink(cfg.agent_transport, cfg.agent_port, bind_host=bind or "127.0.0.1")
        self.agentlink.on_message = self._on_agent_message
        self.platform = PlatformLink(cfg.platform_ws, self.key, self._hello)
        self.platform.on_command = self._on_command
        self.log = ActionLog(cfg.state_dir / "actions.log", self.key)
        self.node_id: str | None = self._load_node_id()
        self.current: VmHandle | None = None
        self.current_launch: LaunchCommand | None = None
        self.images: list[str] | None = None  # catalog images present ("<version>:<kind>"); None until the first inventory
        self.last_launch_error: str | None = None
        self.maintenance_until: str | None = None
        self._pending_identity: asyncio.Future | None = None
        self._teardown_ack: asyncio.Event = asyncio.Event()
        self._last_evidence_at: float = 0.0
        self._hello_count: int = 0
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
        if self.cfg.docker or self.cfg.dual:
            from .hypervisor.docker_backend import DockerBackend

            n = self.cfg.network
            docker = DockerBackend(public_ip=n.public_ip, ssh_port=n.nat_ssh_port, data_dir=self.cfg.docker_data_dir, image_dir=self.cfg.image_dir,
                                   image_repo=self.cfg.docker_image_repo, guest_files=self._dev_guest_files(), agent_port=self.cfg.agent_port,
                                   agent_host=self.cfg.agent_bind_host, privileged=self.cfg.docker_privileged, shm_gb=self.cfg.docker_shm_gb, bridge=self.cfg.docker_bridge,
                                   data_gb=self.cfg.docker_data_gb, expose=n.expose, reserved_ports=n.reserved_ports)
            if self.cfg.docker:
                return docker
            from .hypervisor.dual_backend import DualBackend
            from .hypervisor.libvirt_backend import LibvirtBackend

            vm = LibvirtBackend(bridge=n.bridge, public_ip=n.public_ip, net_mode=n.mode, nat_network=n.nat_network, nat_ssh_port=n.nat_ssh_port, guest_files=self._dev_guest_files(), expose=n.expose, reserved_ports=n.reserved_ports)
            return DualBackend(vm, docker, self.cfg.resources, self.cfg.state_dir)
        from .hypervisor.libvirt_backend import LibvirtBackend

        n = self.cfg.network
        return LibvirtBackend(bridge=n.bridge, public_ip=n.public_ip, net_mode=n.mode, nat_network=n.nat_network, nat_ssh_port=n.nat_ssh_port,
                              guest_files=self._dev_guest_files(), expose=n.expose, reserved_ports=n.reserved_ports)

    def _dev_guest_files(self) -> dict[str, str]:
        """Dev trust mode on real hardware (GPUs without device attestation): the VM needs the dev CA certificates and this node's
        simulated identity, which the host hands over through cloud-init vendor-data. Private CA keys never leave the platform;
        the node's simulated device keys are the dev stand-in for keys a real GPU holds in hardware. Empty in production."""
        t = self.cfg.trust
        if not t.dev or t.dev_ca_dir is None or self.cfg.sim_node_dir is None:
            return {}
        files = {"/etc/racksdock/node-agent.env": "RACKSDOCK_TRUST_MODE=dev\nRACKSDOCK_DEV_CA_DIR=/etc/racksdock/devca\nRACKSDOCK_SIM_NODE_DIR=/etc/racksdock/node\n"}
        # the operator's dev SSH key (RACKSDOCK_DEV_SSH_PUBKEY) is NOT part of these files: it only rides on bootstrap launches
        # (VmSpec.operator_keys); a tenant's or validator's rental never carries a key its owner did not choose
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

    # ---- the running rental survives a host-agent restart (upgrade, crash): its handle and launch command are kept on disk and
    # re-adopted at start when the VM/container is still running; anything else named racksdock-* is stale and destroyed
    @property
    def _current_file(self) -> Path:
        return self.cfg.state_dir / "current.json"

    def _save_current(self) -> None:
        if self.current is None or self.current_launch is None:
            self._current_file.unlink(missing_ok=True)
            return
        self._current_file.write_text(json.dumps({"handle": asdict(self.current), "launch": self.current_launch.model_dump(mode="json")}, indent=1))

    async def _adopt_current(self) -> VmHandle | None:
        """Returns the handle cleanup_stale() must keep. A record that cannot be fully understood still protects its VM/container
        from being destroyed (the worst outcome of a host-agent upgrade is a tenant losing a rental, not a stale handle)."""
        p = self._current_file
        if not p.exists():
            return None
        try:
            d = json.loads(p.read_text())
            handle = VmHandle(**{k: v for k, v in d["handle"].items() if k in VmHandle.__dataclass_fields__})
        except Exception as e:
            log.warning("could not read the previous rental record (%s); starting clean", e)
            p.unlink(missing_ok=True)
            return None
        try:
            lc = LaunchCommand.model_validate(d["launch"])
            state = await self.backend.state(handle)
        except Exception as e:
            log.error("previous rental %s: cannot verify its state (%s); keeping it untouched, not adopting", handle.instance_id, e)
            return handle
        if state != "running":
            log.warning("previous rental %s is %s; not adopting", handle.instance_id, state)
            p.unlink(missing_ok=True)
            return None
        self.current, self.current_launch = handle, lc
        log.info("adopted running %s for instance %s (%s)", handle.name, handle.instance_id, "active" if handle.extra.get("active") else "still booting")
        if handle.extra.get("active") is not True:
            self._boot_timer = asyncio.create_task(self._boot_watchdog(handle.instance_id))
        refresh = getattr(self.backend, "refresh_forwarding", None)
        if refresh is not None:
            try:
                await refresh(handle)  # a newer exposure policy applies to the adopted rental too
            except Exception as e:
                log.warning("could not refresh port forwarding for %s: %s", handle.instance_id, e)
        return handle

    def _hello(self) -> HostEvent:
        return HostEvent(kind="hello", node_id=self.node_id, payload={
            "host_pubkey": self.key.public.hex, "host_image_version": self.cfg.host_image_version, "host_agent_version": __version__,
            "miner_hotkey": self.cfg.miner_hotkey, "region": self.cfg.region, "inventory": self.backend.host_inventory(),
            "vm_state": "running" if self.current else "none", "instance_id": self.current.instance_id if self.current else None,
            "images": self.images,
        })

    # ---- catalog: the platform publishes signed image releases; the host keeps the kinds it runs in sync and reports what it has
    def _inventory(self) -> list[str] | None:
        if self.cfg.sim or self.cfg.static:
            return None
        from racksdock_node.images.sync import local_images

        return local_images(self.cfg.image_dir, self.cfg.image_kinds)

    async def _catalog_loop(self) -> None:
        from racksdock_node.images.sync import sync_catalog

        url = self.cfg.effective_catalog_url
        while True:
            if url:
                try:
                    r = await asyncio.to_thread(sync_catalog, url, self.cfg.image_dir, self.cfg.image_kinds, Path("/etc/racksdock/catalog.pub"),
                                                lambda s: log.info("catalog: %s", s))
                    if r["fetched"]:
                        log.info("catalog %s: fetched %s", r["release"], ", ".join(r["fetched"]))
                except Exception as e:
                    log.warning("catalog sync failed: %s", str(e)[:300])
            before = self.images
            self.images = await asyncio.to_thread(self._inventory)
            if self.images != before:
                log.info("catalog images present: %s", ", ".join(self.images or []) or "none")
            await asyncio.sleep(self.cfg.catalog_sync_s)

    async def run(self) -> None:
        log.info("host-agent started: mode=%s node_id=%s host_key=%s platform=%s region=%s", self.cfg.mode, (self.node_id or "unregistered")[:24],
                 self.key.public.hex[:12], self.cfg.platform_ws, self.cfg.region)
        await self.agentlink.start()
        if self.cfg.sim or self.cfg.static:
            # process-based backends need the actual bound port
            self.backend.agent_port = self.agentlink.port  # type: ignore[attr-defined]
        else:
            keep = await self._adopt_current()
            cleanup = getattr(self.backend, "cleanup_stale", None)
            if cleanup is not None:
                stale = await cleanup(self.cfg.vm_dir, keep=keep)
                if stale:
                    log.warning("removed %d stale VM(s) left by a previous host-agent: %s", len(stale), ", ".join(stale))
            preflight = getattr(self.backend, "preflight", None)
            if preflight is not None:
                for problem in await preflight(self.cfg.resources):
                    if keep is not None and "hugepages free" in problem:
                        continue  # the adopted rental holds the pool; that is not a host problem
                    log.warning("preflight: %s", problem)
        hb = asyncio.create_task(self._heartbeat_loop())
        cat = asyncio.create_task(self._catalog_loop()) if not (self.cfg.sim or self.cfg.static) else None
        try:
            await self.platform.run()
        finally:
            hb.cancel()
            if cat is not None:
                cat.cancel()
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
                                                                     maintenance_until=self.maintenance_until, images=self.images).model_dump()))

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
                          image_path=self.cfg.image_dir / f"{lc.image_version}.qcow2", hostname=f"racksdock-{lc.instance_id[-8:]}", deployment=lc.deployment,
                          public_ip=self.cfg.network.public_ip, ssh_port=lc.ssh_port,
                          operator_keys=(self.cfg.dev_ssh_pubkey.strip() + "\n") if self.cfg.dev_ssh_pubkey and lc.instance_id.startswith("bootstrap-") else None)
            self.current_launch = lc
            self._teardown_ack.clear()
            self._last_evidence_at = 0.0
            self._hello_count = 0
            try:
                self.current = await self.backend.launch(spec)
            except Exception as e:
                self.current_launch = None
                self.last_launch_error = f"launch failed: {str(e)[:600]}"
                log.error("launch of %s (%s) failed: %s", lc.instance_id, lc.deployment or self.cfg.deployment, str(e)[:1500])
                await self._status(lc.instance_id, InstanceStatus.error, f"launch failed: {e}")
                return
            self.log.record("vm_started", instance_id=lc.instance_id, mode=lc.mode.value, image=lc.image)
            self._save_current()
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
        self._save_current()

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
        self._save_current()
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
        """Boot a verify VM with a random nonce, derive gpu_ids from its evidence, write a registration draft. With reuse=true the
        stored identity (gpu_ids proven at the first bootstrap) is re-signed with this host's current profile, deployment tier and
        claimed spec: no VM is needed, so a node can change its offer while a rental runs; validators re-verify the hardware at the
        admission test that follows every re-registration."""
        if cmd.payload.get("reuse"):
            prev = self.cfg.state_dir / "registration.json"
            if not prev.exists():
                await self.platform.send(HostEvent(kind="error", command_id=cmd.command_id, payload={"error": "no stored registration to reuse; bootstrap without reuse"}))
                return
            old = RegistrationRecord.model_validate_json(prev.read_text())
            rec = self._registration_record(old.gpu_ids, float(cmd.payload.get("network_gbps", old.claimed_spec.network_gbps)))
            self._store_registration(rec)
            self.log.record("identity_reused", node_id=rec.node_id, deployment=rec.deployment, profile=rec.profile)
            reg_env = SignedEnvelope.create("registration", rec.model_dump(by_alias=True), self.key)
            (self.cfg.state_dir / "registration.envelope.json").write_text(reg_env.model_dump_json(indent=1))
            await self.platform.send(HostEvent(kind="identity", command_id=cmd.command_id, node_id=rec.node_id, payload={
                "registration": rec.model_dump(by_alias=True), "registration_envelope": reg_env.model_dump(), "evidence": None, "reused": True}))
            return
        loop = asyncio.get_running_loop()
        image = cmd.payload.get("image", "verify")
        image_version = cmd.payload.get("image_version", "v0")
        # a fresh host is usually still downloading the catalog when the miner bootstraps: wait for the verify image instead of
        # failing, and tell the platform (the miner CLI shows these notes) what we are waiting for
        def have_image() -> bool:
            # the inventory is None until the first catalog sync has finished (the signed manifest is written last), so an
            # unknown inventory means "still downloading", not "present"
            return self.images is not None and any(i.startswith(image_version + ":") for i in self.images)

        if not (self.cfg.sim or self.cfg.static) and not have_image():
            waited = 0.0
            while waited < self.cfg.catalog_wait_s and not have_image():
                if waited % 60 == 0:
                    msg = f"waiting for catalog image {image_version} (first download in progress, {int(waited)}s so far)"
                    log.info("bootstrap: %s", msg)
                    await self.platform.send(HostEvent(kind="log", command_id=cmd.command_id, payload={"message": msg}))
                await asyncio.sleep(10)
                waited += 10
                self.images = await asyncio.to_thread(self._inventory)
            if not have_image():
                await self.platform.send(HostEvent(kind="error", command_id=cmd.command_id, payload={"error": f"catalog image {image_version} is not on this host after {int(waited)}s; check `journalctl -u racksdock-host-agent` for the catalog sync"}))
                return
        self._pending_identity = loop.create_future()
        self.last_launch_error = None
        lc = LaunchCommand(instance_id=f"bootstrap-{secrets.token_hex(4)}", node_id=self.node_id or "", mode=VmMode.verify, image=image,
                           image_version=image_version, user_data="#cloud-config\n{}\n", tenant_nonce=secrets.token_hex(32))
        await self._cmd_launch(HostCommand(kind="launch", command_id=cmd.command_id + ":launch", instance_id=lc.instance_id, payload=lc.model_dump(mode="json")))
        if self.current is None or self.current.instance_id != lc.instance_id:
            self._pending_identity = None
            await self.platform.send(HostEvent(kind="error", command_id=cmd.command_id, payload={"error": self.last_launch_error or "bootstrap VM did not start"}))
            return
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
        rec = self._registration_record(gpu_ids, float(cmd.payload.get("network_gbps", 25)))
        node_id = rec.node_id
        self._store_registration(rec)
        self.log.record("identity_bootstrapped", node_id=node_id)
        reg_env = SignedEnvelope.create("registration", rec.model_dump(by_alias=True), self.key)
        (self.cfg.state_dir / "registration.envelope.json").write_text(reg_env.model_dump_json(indent=1))
        await self.platform.send(HostEvent(kind="identity", command_id=cmd.command_id, node_id=node_id, payload={
            "registration": rec.model_dump(by_alias=True), "registration_envelope": reg_env.model_dump(), "evidence": ev.model_dump(mode="json")}))
        async with self._lock:
            if self.current and self.current.instance_id == lc.instance_id:
                await self._teardown_current(reason="bootstrap complete")

    def _registration_record(self, gpu_ids: list[str], network_gbps: float) -> RegistrationRecord:
        inv = self.backend.host_inventory()
        return RegistrationRecord(
            node_id=node_id_from_gpu_ids(gpu_ids), gpu_ids=gpu_ids, profile=current_profile().name, deployment=self.cfg.deployment, miner_hotkey=self.cfg.miner_hotkey,
            miner_service_key=self.key.public.hex,
            claimed_spec=ClaimedSpec(vcpus=self.cfg.resources.vcpus, cpu_model=inv.get("cpu_model", ""), memory_gb=self.cfg.resources.memory_gb,
                                     nvme_tb=round(sum(d.get("PhysicalSize", 0) for d in inv.get("nvme", [])) / 1e12, 1),
                                     network_gbps=network_gbps, region=self.cfg.region, oem_platform=self.cfg.oem_platform),
            host_image_version=self.cfg.host_image_version, board_serial=inv.get("board_serial"),
        )

    def _store_registration(self, rec: RegistrationRecord) -> None:
        self.node_id = rec.node_id
        (self.cfg.state_dir / "node_id").write_text(rec.node_id + "\n")
        (self.cfg.state_dir / "registration.json").write_text(rec.model_dump_json(by_alias=True, indent=1))

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
            self._hello_count += 1
            if self._hello_count in (5, 50, 500):
                log.warning("node-agent for %s has reconnected %d times: it is probably crash-looping inside the VM", iid, self._hello_count)
            meta = Metadata(instance_id=iid, node_id=self.node_id, tenant_nonce=lc.tenant_nonce, mode=lc.mode.value, image_version=lc.image_version,
                            user_data=lc.user_data if self.cfg.metadata_carries_user_data else None, ssh_port=lc.ssh_port)
            await self.agentlink.send(AgentMessage(type="metadata", payload=meta.model_dump()))
        elif msg.type == "evidence":
            env = SignedEnvelope.model_validate(msg.payload)
            if env.verify() and env.kind == "boot_evidence":
                ev = BootEvidence.model_validate(env.payload)
                if self._pending_identity is not None and not self._pending_identity.done():
                    self._pending_identity.set_result(ev)
            now = asyncio.get_running_loop().time()
            if self.current.extra.get("active") is True and now - self._last_evidence_at < EVIDENCE_MIN_INTERVAL_S:
                return  # already active: re-attestations come through validator challenges; a crash-looping agent is not relayed every 2 s
            self._last_evidence_at = now
            await self.platform.send(HostEvent(kind="evidence", node_id=self.node_id, instance_id=iid, payload=env.model_dump()))
        elif msg.type == "status":
            upd = StatusUpdate.model_validate(msg.payload)
            log.info("instance %s -> %s %s", iid, upd.status.value, upd.status_details or "")
            upd.ip = self.current.ip
            upd.ssh_port = self.current.ssh_port
            if upd.status == InstanceStatus.active:
                self.current.extra["active"] = True
                self._save_current()
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
