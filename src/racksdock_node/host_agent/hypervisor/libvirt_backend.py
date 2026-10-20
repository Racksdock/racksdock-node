"""Real backend: QEMU/KVM through virsh. Uses subprocess so no python-libvirt binding is required."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path

from racksdock_subnet.core.schemas import VmResources

from ..cloudinit import build_seed_iso, network_config
from .base import VmHandle, VmSpec
from .libvirt_xml import build_domain_xml


async def _run(*cmd: str, check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    cp = subprocess.CompletedProcess(cmd, proc.returncode, out.decode(), err.decode())
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({proc.returncode}): {cp.stderr.strip()}")
    return cp


class LibvirtBackend:
    name = "libvirt"

    def __init__(self, bridge: str = "br0", host_cores: list[list[int]] | None = None, public_ip: str | None = None, gateway: str | None = None):
        self.bridge = bridge
        self.host_cores = host_cores or self._discover_host_cores()
        self.public_ip = public_ip
        self.gateway = gateway

    @staticmethod
    def _discover_host_cores(reserve: int = 8) -> list[list[int]]:
        nodes: list[list[int]] = []
        base = Path("/sys/devices/system/node")
        for n in sorted(base.glob("node[0-9]*")):
            cpulist = (n / "cpulist").read_text().strip()
            cpus: list[int] = []
            for part in cpulist.split(","):
                if "-" in part:
                    a, b = part.split("-")
                    cpus.extend(range(int(a), int(b) + 1))
                elif part:
                    cpus.append(int(part))
            nodes.append(cpus)
        if not nodes:
            return []
        # reserve the first `reserve` cpus spread over nodes for the host
        per = max(1, reserve // len(nodes))
        return [c[per:] for c in nodes]

    async def launch(self, spec: VmSpec) -> VmHandle:
        name = f"racksdock-{spec.instance_id}"
        self._verify_image(spec)
        spec.vm_dir.mkdir(parents=True, exist_ok=True)
        overlay = spec.vm_dir / "os.qcow2"
        nvram = spec.vm_dir / "nvram.fd"
        seed = spec.vm_dir / "seed.iso"
        await _run("qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", str(spec.image_path), str(overlay), f"{spec.resources.os_disk_gb}G")
        build_seed_iso(seed, spec.user_data, spec.metadata, spec.hostname, network_config(spec.public_ip, self.gateway))
        xml = build_domain_xml(name, spec.resources, overlay, seed, nvram, self.host_cores, self.bridge)
        (spec.vm_dir / "domain.xml").write_text(xml)
        await _run("virsh", "define", str(spec.vm_dir / "domain.xml"))
        await _run("virsh", "start", name)
        return VmHandle(instance_id=spec.instance_id, name=name, ip=spec.public_ip, ssh_port=spec.ssh_port, extra={"vm_dir": str(spec.vm_dir)})

    def _verify_image(self, spec: VmSpec) -> None:
        """Refuse images that are not in the signed catalog manifest (docs/01 §6.2). Manifest and key live next to the images."""
        from racksdock_subnet.core.manifest import load_signed_manifest, verify_image_file

        manifest = spec.image_path.parent / "manifest.signed.json"
        pub = spec.image_path.parent / "catalog.pub"
        if not manifest.exists() or not pub.exists():
            raise RuntimeError("signed catalog manifest or catalog.pub missing from image dir")
        m = load_signed_manifest(manifest, pub.read_text().strip())
        verify_image_file(m, spec.image_path, spec.image, spec.image_version)

    async def destroy(self, handle: VmHandle, graceful_timeout_s: float = 30.0) -> None:
        # graceful ACPI shutdown of the VM (by libvirt domain name; cannot affect the host), then hard destroy
        await _run("virsh", "shutdown", handle.name, check=False)
        for _ in range(int(graceful_timeout_s)):
            if await self.state(handle) in ("shut off", "unknown"):
                break
            await asyncio.sleep(1)
        await _run("virsh", "destroy", handle.name, check=False)
        await _run("virsh", "undefine", "--nvram", handle.name, check=False)
        vm_dir = Path(handle.extra.get("vm_dir", ""))
        if vm_dir.exists():
            for f in ("os.qcow2", "seed.iso", "nvram.fd", "domain.xml"):
                try:
                    (vm_dir / f).unlink()
                except OSError:
                    pass

    async def reboot(self, handle: VmHandle, hard: bool = False) -> None:
        if hard:
            await _run("virsh", "reset", handle.name)
            return
        await _run("virsh", "reboot", handle.name, check=False)
        for _ in range(60):
            await asyncio.sleep(1)
            if await self.state(handle) == "running":
                return
        await _run("virsh", "reset", handle.name, check=False)

    async def state(self, handle: VmHandle) -> str:
        cp = await _run("virsh", "domstate", handle.name, check=False)
        return cp.stdout.strip() or "unknown"

    async def discover_ip(self, handle: VmHandle) -> str | None:
        if handle.ip:
            return handle.ip
        cp = await _run("virsh", "domifaddr", handle.name, "--source", "agent", check=False)
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)/\d+", cp.stdout)
        if not m:
            cp = await _run("virsh", "domifaddr", handle.name, "--source", "lease", check=False)
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)/\d+", cp.stdout)
        return m.group(1) if m else None

    async def reset_devices(self, resources: VmResources) -> list[str]:
        done = []
        for addr in list(resources.gpu_pci) + list(resources.nvswitch_pci):
            p = Path(f"/sys/bus/pci/devices/{addr}/reset")
            try:
                p.write_text("1")
                done.append(addr)
            except OSError:
                pass
        return done

    async def sanitize_nvme(self, resources: VmResources) -> list[str]:
        if shutil.which("nvme") is None:
            return []
        cp = await _run("nvme", "list", "-o", "json", check=False)
        try:
            devices = json.loads(cp.stdout or "{}").get("Devices", [])
        except json.JSONDecodeError:
            devices = []
        done = []
        for d in devices:
            dev = d.get("DevicePath")
            # only sanitize controllers that are passed through (data NVMe), never the host OS disk
            if not dev:
                continue
            ctrl = re.sub(r"n\d+$", "", dev)
            cp2 = await _run("readlink", "-f", f"/sys/class/nvme/{Path(ctrl).name}/device", check=False)
            if not any(addr in cp2.stdout for addr in resources.nvme_pci):
                continue
            r = await _run("nvme", "sanitize", ctrl, "--sanact=4", check=False, timeout=600)  # crypto erase
            if r.returncode != 0:
                r = await _run("nvme", "format", dev, "--ses=2", "--force", check=False, timeout=600)
            done.append(ctrl)
        return done

    def host_inventory(self) -> dict:
        from ..inventory import real_inventory

        return real_inventory()
