"""Static backend: the machine the host-agent runs on IS the node (no hypervisor, no passthrough).

For workflow tests on hardware where a VM cannot be created (rented VMs or containers). "Launch" applies the tenant's
SSH keys to the configured user, starts a node-agent in real mode on this machine, and reports this machine's address.
"Destroy" stops the node-agent and restores the baseline authorized_keys. Nothing is reset or sanitized.
Never use in production: the tenant shares the box with the host-agent and all previous tenants' state.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import yaml
from racksdock_subnet.core.schemas import VmResources

from .base import VmHandle, VmSpec


class StaticBackend:
    name = "static"

    def __init__(self, agent_port: int, public_ip: str, ssh_port: int, authorized_keys: Path, node_agent_cmd: str = "racksdock-node-agent",
                 node_agent_env: dict | None = None, state_dir: Path | None = None, ssh_service: str | None = "ssh"):
        self.ssh_service = ssh_service  # systemd unit restarted on "reboot" (a VM reboot would bring sshd back); None disables
        self.agent_port = agent_port
        self.public_ip = public_ip
        self.ssh_port = ssh_port
        self.authorized_keys = Path(authorized_keys)
        self.node_agent_cmd = node_agent_cmd
        self.node_agent_env = node_agent_env or {}
        self.state_dir = state_dir or Path("/var/lib/racksdock-host-agent/static")
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._baseline: str | None = None

    def _agent_argv(self) -> list[str]:
        exe = shutil.which(self.node_agent_cmd)
        return [exe] if exe else [sys.executable, "-m", "racksdock_node.node_agent.main"]

    @staticmethod
    def tenant_keys(user_data: str) -> list[str]:
        try:
            cfg = yaml.safe_load(user_data) or {}
        except yaml.YAMLError:
            return []
        keys: list[str] = []
        for u in cfg.get("users", []) or []:
            if isinstance(u, dict):
                keys += [k for k in (u.get("ssh_authorized_keys") or []) if isinstance(k, str)]
        return keys

    MARK_START, MARK_END = "# >>> racksdock tenant keys >>>", "# <<< racksdock tenant keys <<<"

    @classmethod
    def strip_tenant_blocks(cls, text: str) -> str:
        """Remove every racksdock tenant block, so a previous session's keys never survive into the baseline."""
        out, skipping = [], False
        for line in text.splitlines():
            if line.strip() == cls.MARK_START:
                skipping = True
                continue
            if line.strip() == cls.MARK_END:
                skipping = False
                continue
            if not skipping:
                out.append(line)
        return "\n".join(out).rstrip("\n")

    def _apply_keys(self, keys: list[str]) -> None:
        self.authorized_keys.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        current = self.authorized_keys.read_text() if self.authorized_keys.exists() else ""
        self._baseline = self.strip_tenant_blocks(current)  # always recomputed: only the operator's own keys stay
        block = "\n".join([self.MARK_START, *keys, self.MARK_END])
        self.authorized_keys.write_text((self._baseline + "\n" if self._baseline else "") + block + "\n")
        self.authorized_keys.chmod(0o600)

    def _restore_keys(self) -> None:
        if not self.authorized_keys.exists():
            return
        base = self.strip_tenant_blocks(self.authorized_keys.read_text())
        self.authorized_keys.write_text(base + "\n" if base else "")
        self.authorized_keys.chmod(0o600)

    async def launch(self, spec: VmSpec) -> VmHandle:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._apply_keys(self.tenant_keys(spec.user_data))
        env = dict(os.environ)
        env.update({
            "RACKSDOCK_NODE_AGENT_MODE": "real", "RACKSDOCK_AGENT_TRANSPORT": "tcp", "RACKSDOCK_AGENT_PORT": str(self.agent_port),
            "RACKSDOCK_STATE_DIR": str(self.state_dir / "node-agent"), "RACKSDOCK_IMAGE_VERSION": spec.image_version, "RACKSDOCK_METRICS_PORT": "0",
        })
        env.update(self.node_agent_env)
        log = open(self.state_dir / "node-agent.log", "ab")
        proc = await asyncio.create_subprocess_exec(*self._agent_argv(), env=env, stdout=log, stderr=log)
        self._procs[spec.instance_id] = proc
        return VmHandle(instance_id=spec.instance_id, name=f"static-{spec.instance_id}", ip=self.public_ip, ssh_port=self.ssh_port, extra={"pid": proc.pid})

    async def _stop(self, instance_id: str) -> None:
        proc = self._procs.pop(instance_id, None)
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                proc.kill()
                await proc.wait()

    async def destroy(self, handle: VmHandle) -> None:
        await self._stop(handle.instance_id)
        self._restore_keys()

    async def _restart_ssh(self) -> None:
        """The admission test stops sshd on the node and expects a restart to bring it back; on a static node that is our job."""
        if not self.ssh_service:
            return
        for cmd in (["systemctl", "restart", self.ssh_service], ["service", self.ssh_service, "restart"]):
            try:
                proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                if await proc.wait() == 0:
                    return
            except FileNotFoundError:
                continue

    async def reboot(self, handle: VmHandle, hard: bool = False) -> None:
        await self._stop(handle.instance_id)
        await self._restart_ssh()
        handle.extra["rebooted"] = handle.extra.get("rebooted", 0) + 1

    async def state(self, handle: VmHandle) -> str:
        proc = self._procs.get(handle.instance_id)
        return "running" if proc and proc.returncode is None else "shut off"

    async def reset_devices(self, resources: VmResources) -> list[str]:
        return []

    async def sanitize_nvme(self, resources: VmResources) -> list[str]:
        return []

    def host_inventory(self) -> dict:
        from ..inventory import real_inventory

        return real_inventory()
