"""Real backend: QEMU/KVM through virsh. Uses subprocess so no python-libvirt binding is required.

Networking modes (docs/01 §4):
  bridge  the VM gets `public_ip` on a host bridge (one public IP per node); cloud-init network-config sets the address.
  nat     the VM sits on a libvirt NAT network and the host forwards host:`nat_ssh_port` -> VM:22 with iptables (single-IP
          hosts, lab boxes). Tenants and validators connect to the host address; everything else (attestation, host key pinning,
          user-data hash) is unchanged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from racksdock_subnet.core.schemas import VmResources

from ..cloudinit import build_seed_iso, network_config
from .base import VmHandle, VmSpec
from .libvirt_xml import OVMF_CODE, OVMF_VARS, build_domain_xml, passthrough_addresses

log = logging.getLogger("racksdock.host-agent.libvirt")
NAT_TAG = "racksdock-nat"
VM_FILES = ("os.qcow2", "data.qcow2", "seed.iso", "nvram.fd", "domain.xml")  # console.log is kept for diagnostics


async def _run(*cmd: str, check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    cp = subprocess.CompletedProcess(cmd, proc.returncode, out.decode(), err.decode())
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({proc.returncode}): {cp.stderr.strip()}")
    return cp


def nat_rules(host_ip: str, port: int, vm_ip: str, tag: str) -> list[tuple[str, list[str]]]:
    """iptables rules (table, args) forwarding host:port -> vm_ip:22, tagged with a comment so they can be removed by tag.
    PREROUTING serves remote clients, OUTPUT serves clients on the host itself, FORWARD admits the new connection past
    libvirt's default 'no inbound to the NAT network' rule."""
    c = ["-m", "comment", "--comment", tag]
    return [
        ("nat", ["PREROUTING", "-p", "tcp", "--dport", str(port), "-j", "DNAT", "--to-destination", f"{vm_ip}:22", *c]),
        ("nat", ["OUTPUT", "-p", "tcp", "-d", host_ip, "--dport", str(port), "-j", "DNAT", "--to-destination", f"{vm_ip}:22", *c]),
        ("filter", ["FORWARD", "-d", vm_ip, "-p", "tcp", "--dport", "22", "-j", "ACCEPT", *c]),
    ]


def parse_tagged_rules(iptables_save: str, tag_prefix: str) -> list[tuple[str, list[str]]]:
    """From `iptables-save` output, every rule whose comment starts with tag_prefix, as (table, args-after-'-A')."""
    out: list[tuple[str, list[str]]] = []
    table = ""
    for line in iptables_save.splitlines():
        if line.startswith("*"):
            table = line[1:].strip()
        elif line.startswith("-A "):
            args = shlex.split(line[3:])
            if "--comment" in args and args[args.index("--comment") + 1].startswith(tag_prefix):
                out.append((table, args))
    return out


class LibvirtBackend:
    name = "libvirt"

    def __init__(self, bridge: str = "br0", host_cores: list[list[int]] | None = None, public_ip: str | None = None, gateway: str | None = None,
                 net_mode: str = "bridge", nat_network: str = "default", nat_ssh_port: int = 20022, guest_files: dict[str, str] | None = None):
        self.bridge = bridge
        self.host_cores = host_cores or self._discover_host_cores()
        self.public_ip = public_ip
        self.gateway = gateway
        self.net_mode = net_mode
        self.nat_network = nat_network
        self.nat_ssh_port = nat_ssh_port
        self.guest_files = guest_files or {}
        self._nat_tasks: dict[str, asyncio.Task] = {}

    @property
    def nat(self) -> bool:
        return self.net_mode == "nat"

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
            if cpus:
                nodes.append(cpus)
        if not nodes:
            return []
        # reserve the first `reserve` cpus spread over nodes for the host
        per = max(1, reserve // len(nodes))
        return [c[per:] for c in nodes]

    # ---- preflight -------------------------------------------------------------
    async def preflight(self, resources: VmResources) -> list[str]:
        """Human-readable problems that would make a launch fail. Empty list = ready."""
        problems = []
        for tool in ("virsh", "qemu-img", "qemu-system-x86_64", "iptables"):
            if shutil.which(tool) is None:
                problems.append(f"{tool} not installed")
        for f in (OVMF_CODE, OVMF_VARS):
            if not Path(f).exists():
                problems.append(f"{f} missing (apt-get install ovmf)")
        if not Path("/dev/kvm").exists():
            problems.append("/dev/kvm missing (KVM not enabled in BIOS or kernel)")
        if not Path("/dev/vhost-vsock").exists():
            problems.append("/dev/vhost-vsock missing (modprobe vhost_vsock)")
        if shutil.which("virsh"):
            cp = await _run("virsh", "version", check=False)
            if cp.returncode != 0:
                problems.append(f"libvirtd not reachable: {cp.stderr.strip()[:200]}")
            elif self.nat:
                cp = await _run("virsh", "net-info", self.nat_network, check=False)
                if cp.returncode != 0 or not re.search(r"Active:\s+yes", cp.stdout):
                    problems.append(f"libvirt network {self.nat_network!r} not active (virsh net-start {self.nat_network})")
        for addr in passthrough_addresses(resources):
            drv = Path(f"/sys/bus/pci/devices/{addr}/driver")
            if not Path(f"/sys/bus/pci/devices/{addr}").exists():
                problems.append(f"PCI device {addr} not present")
            elif drv.exists() and drv.resolve().name != "vfio-pci":
                problems.append(f"PCI device {addr} bound to {drv.resolve().name}, not vfio-pci (run bind-vfio.sh)")
        if resources.hugepage_kib:
            p = Path(f"/sys/kernel/mm/hugepages/hugepages-{resources.hugepage_kib}kB/free_hugepages")
            free_gib = int(p.read_text()) * resources.hugepage_kib // (1024 * 1024) if p.exists() else 0
            if free_gib < resources.memory_gb:
                problems.append(f"{free_gib} GiB of {resources.hugepage_kib} KiB hugepages free, VM needs {resources.memory_gb} GiB (set hugepage_kib 0 or reserve pages)")
        return problems

    # ---- lifecycle -------------------------------------------------------------
    async def launch(self, spec: VmSpec) -> VmHandle:
        name = f"racksdock-{spec.instance_id}"
        problems = await self.preflight(spec.resources)
        if problems:
            raise RuntimeError("host not ready: " + "; ".join(problems))
        self._verify_image(spec)
        spec.vm_dir.mkdir(parents=True, exist_ok=True)
        overlay = spec.vm_dir / "os.qcow2"
        nvram = spec.vm_dir / "nvram.fd"
        seed = spec.vm_dir / "seed.iso"
        await _run("qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", str(spec.image_path), str(overlay), f"{spec.resources.os_disk_gb}G")
        data_disk = None
        if spec.resources.data_disk_gb and not spec.resources.nvme_pci:
            data_disk = spec.vm_dir / "data.qcow2"
            await _run("qemu-img", "create", "-q", "-f", "qcow2", str(data_disk), f"{spec.resources.data_disk_gb}G")
        net_cfg = None if self.nat else network_config(spec.public_ip, self.gateway)
        build_seed_iso(seed, spec.user_data, spec.metadata, spec.hostname, net_cfg, extra_files=self.guest_files or None)
        xml = build_domain_xml(name, spec.resources, overlay, seed, nvram, self.host_cores, bridge=None if self.nat else self.bridge,
                               nat_network=self.nat_network if self.nat else None, data_disk=data_disk, console_log=spec.vm_dir / "console.log")
        (spec.vm_dir / "domain.xml").write_text(xml)
        log.info("defining %s: %d vcpus, %d GiB, %d PCI functions, net=%s", name, spec.resources.vcpus, spec.resources.memory_gb,
                 len(passthrough_addresses(spec.resources)), self.net_mode)
        await _run("virsh", "define", str(spec.vm_dir / "domain.xml"))
        try:
            await _run("virsh", "start", name, timeout=900)  # pinning hundreds of GiB for VFIO takes a while
        except Exception:
            await _run("virsh", "undefine", "--nvram", name, check=False)
            raise
        handle = VmHandle(instance_id=spec.instance_id, name=name, ip=spec.public_ip, ssh_port=spec.ssh_port, extra={"vm_dir": str(spec.vm_dir)})
        if self.nat:
            handle.ip = self.public_ip or await self.detect_host_ip()
            handle.ssh_port = self.nat_ssh_port
            # the VM's address is only known once it has a DHCP lease; the forward is installed in the background so the
            # node-agent's first messages (which arrive over vsock as soon as it boots) are not delayed
            self._nat_tasks[spec.instance_id] = asyncio.create_task(self._setup_nat(handle))
        log.info("%s running; tenants connect to %s:%s", name, handle.ip, handle.ssh_port)
        return handle

    async def _setup_nat(self, handle: VmHandle) -> None:
        try:
            vm_ip = await self._wait_lease(handle.name, timeout=300)
            handle.extra["vm_ip"] = vm_ip
            await self._nat_clear(f"{NAT_TAG}-{handle.instance_id}")
            for table, args in nat_rules(handle.ip or "", handle.ssh_port, vm_ip, f"{NAT_TAG}-{handle.instance_id}"):
                await _run("iptables", "-w", "-t", table, "-I", args[0], "1", *args[1:])
            log.info("%s: forwarding %s:%d -> %s:22", handle.name, handle.ip, handle.ssh_port, vm_ip)
        except Exception as e:
            log.error("%s: NAT setup failed: %s", handle.name, e)
            handle.extra["nat_error"] = str(e)

    async def _wait_lease(self, name: str, timeout: float) -> str:
        for _ in range(int(timeout / 2)):
            cp = await _run("virsh", "domifaddr", name, "--source", "lease", check=False)
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)/\d+", cp.stdout)
            if m:
                return m.group(1)
            await asyncio.sleep(2)
        raise TimeoutError(f"no DHCP lease for {name} within {timeout:.0f}s (is the guest's NIC up? virsh console {name})")

    async def _nat_clear(self, tag_prefix: str) -> None:
        if shutil.which("iptables-save") is None:
            return
        cp = await _run("iptables-save", check=False)
        for table, args in parse_tagged_rules(cp.stdout, tag_prefix):
            await _run("iptables", "-w", "-t", table, "-D", *args, check=False)

    async def detect_host_ip(self) -> str:
        cp = await _run("ip", "-j", "route", "get", "1.1.1.1", check=False)
        try:
            src = json.loads(cp.stdout or "[]")[0].get("prefsrc")
            if src:
                return src
        except (json.JSONDecodeError, IndexError, AttributeError):
            pass
        cp = await _run("hostname", "-I", check=False)
        return (cp.stdout.split() or ["127.0.0.1"])[0]

    def _verify_image(self, spec: VmSpec) -> None:
        """Refuse images that are not in the signed catalog manifest (docs/01 §6.2). Manifest and key live next to the images."""
        from racksdock_subnet.core.manifest import load_signed_manifest, verify_image_file

        if not spec.image_path.exists():
            raise RuntimeError(f"image {spec.image_path} not found (build it with image-build/build.sh and sign the manifest)")
        manifest = spec.image_path.parent / "manifest.signed.json"
        pub = spec.image_path.parent / "catalog.pub"
        if not manifest.exists() or not pub.exists():
            raise RuntimeError("signed catalog manifest or catalog.pub missing from image dir")
        m = load_signed_manifest(manifest, pub.read_text().strip())
        verify_image_file(m, spec.image_path, spec.image, spec.image_version)

    async def destroy(self, handle: VmHandle, graceful_timeout_s: float = 30.0) -> None:
        task = self._nat_tasks.pop(handle.instance_id, None)
        if task and not task.done():
            task.cancel()
        # graceful ACPI shutdown of the VM (by libvirt domain name; cannot affect the host), then hard destroy
        await _run("virsh", "shutdown", handle.name, check=False)
        for _ in range(int(graceful_timeout_s)):
            if await self.state(handle) in ("shut off", "unknown"):
                break
            await asyncio.sleep(1)
        await _run("virsh", "destroy", handle.name, check=False)
        await _run("virsh", "undefine", "--nvram", handle.name, check=False)
        if self.nat:
            await self._nat_clear(f"{NAT_TAG}-{handle.instance_id}")
        vm_dir = Path(handle.extra.get("vm_dir", ""))
        if vm_dir.exists():
            for f in VM_FILES:
                try:
                    (vm_dir / f).unlink()
                except OSError:
                    pass

    async def cleanup_stale(self, vm_root: Path) -> list[str]:
        """On host-agent start: no racksdock VM may survive a host-agent restart (it holds the GPUs and the agent has no record
        of it). Destroys every `racksdock-*` domain, drops every tagged NAT rule, removes leftover VM files."""
        cp = await _run("virsh", "list", "--all", "--name", check=False)
        names = [n for n in cp.stdout.split() if n.startswith("racksdock-")]
        for n in names:
            log.warning("destroying stale VM %s", n)
            await _run("virsh", "destroy", n, check=False)
            await _run("virsh", "undefine", "--nvram", n, check=False)
        await self._nat_clear(NAT_TAG)
        if vm_root.exists():
            for d in vm_root.iterdir():
                if d.is_dir():
                    for f in VM_FILES:
                        try:
                            (d / f).unlink()
                        except OSError:
                            pass
                    try:
                        d.rmdir()
                    except OSError:
                        pass
        return names

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
        for addr in list(resources.gpu_pci) + list(resources.extra_pci) + list(resources.nvswitch_pci):
            p = Path(f"/sys/bus/pci/devices/{addr}/reset")
            try:
                p.write_text("1")
                done.append(addr)
            except OSError:
                pass
        return done

    async def sanitize_nvme(self, resources: VmResources) -> list[str]:
        if shutil.which("nvme") is None or not resources.nvme_pci:
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
