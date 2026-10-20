"""Simulated backend: the 'VM' is a racksdock-node-agent process in sim mode plus a fake SSH banner listener."""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

from racksdock_subnet.core.schemas import VmResources

from ..cloudinit import build_seed_iso
from .base import VmHandle, VmSpec


class SimBackend:
    name = "sim"

    def __init__(self, agent_port: int, dev_ca_dir: Path, sim_node_dir: Path, node_agent_cmd: str = "racksdock-node-agent",
                 faults_file: Path | None = None):
        self.agent_port = agent_port
        self.dev_ca_dir = dev_ca_dir
        self.sim_node_dir = sim_node_dir
        self.node_agent_cmd = node_agent_cmd
        self.faults_file = faults_file
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._ssh_servers: dict[str, asyncio.AbstractServer] = {}
        self.sanitized: list[str] = []
        self.reset_count = 0

    async def _start_fake_ssh(self) -> tuple[asyncio.AbstractServer, int]:
        async def handle(reader, writer):
            writer.write(b"SSH-2.0-OpenSSH_9.6 racksdock-sim\r\n")
            try:
                await writer.drain()
                await asyncio.wait_for(reader.read(256), timeout=2)
            except Exception:
                pass
            writer.close()

        srv = await asyncio.start_server(handle, "127.0.0.1", 0)
        return srv, srv.sockets[0].getsockname()[1]

    def _agent_argv(self) -> list[str]:
        exe = shutil.which(self.node_agent_cmd)
        if exe:
            return [exe]
        return [sys.executable, "-m", "racksdock_node.node_agent.main"]

    async def launch(self, spec: VmSpec) -> VmHandle:
        spec.vm_dir.mkdir(parents=True, exist_ok=True)
        build_seed_iso(spec.vm_dir / "seed.iso", spec.user_data, spec.metadata, spec.hostname)
        srv, port = await self._start_fake_ssh()
        env = dict(os.environ)
        env.update({
            "RACKSDOCK_NODE_AGENT_MODE": "sim", "RACKSDOCK_AGENT_TRANSPORT": "tcp", "RACKSDOCK_AGENT_PORT": str(self.agent_port),
            "RACKSDOCK_STATE_DIR": str(spec.vm_dir / "state"), "RACKSDOCK_SIM_NODE_DIR": str(self.sim_node_dir),
            "RACKSDOCK_TRUST_MODE": "dev", "RACKSDOCK_DEV_CA_DIR": str(self.dev_ca_dir), "RACKSDOCK_IMAGE_VERSION": spec.image_version,
            "RACKSDOCK_METRICS_PORT": "0",
        })
        if self.faults_file:
            env["RACKSDOCK_SIM_FAULTS"] = str(self.faults_file)
        log = open(spec.vm_dir / "node-agent.log", "ab")
        proc = await asyncio.create_subprocess_exec(*self._agent_argv(), env=env, stdout=log, stderr=log)
        self._procs[spec.instance_id] = proc
        self._ssh_servers[spec.instance_id] = srv
        return VmHandle(instance_id=spec.instance_id, name=f"sim-{spec.instance_id}", ip="127.0.0.1", ssh_port=port, extra={"pid": proc.pid})

    async def destroy(self, handle: VmHandle) -> None:
        proc = self._procs.pop(handle.instance_id, None)
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        srv = self._ssh_servers.pop(handle.instance_id, None)
        if srv:
            srv.close()
            await srv.wait_closed()

    async def reboot(self, handle: VmHandle, hard: bool = False) -> None:
        # a restart keeps the disk; in sim we restart the process and keep the ssh port
        proc = self._procs.get(handle.instance_id)
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                proc.kill()
        handle.extra["rebooted"] = handle.extra.get("rebooted", 0) + 1

    async def state(self, handle: VmHandle) -> str:
        proc = self._procs.get(handle.instance_id)
        return "running" if proc and proc.returncode is None else "shut off"

    async def reset_devices(self, resources: VmResources) -> list[str]:
        self.reset_count += 1
        return list(resources.gpu_pci) + list(resources.nvswitch_pci)

    async def sanitize_nvme(self, resources: VmResources) -> list[str]:
        self.sanitized.extend(resources.nvme_pci)
        return list(resources.nvme_pci)

    def host_inventory(self) -> dict:
        from ..inventory import sim_inventory

        return sim_inventory(self.sim_node_dir)
