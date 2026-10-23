"""Container tier (docs/01 §10): the rental is a root Docker container on the host's kernel with every GPU, nested Docker inside,
an encrypted data volume managed by the host, sshd on a forwarded port, and the same node-agent as a VM (over TCP instead of
vsock). Everything the host-agent does to a VM (launch, restart, destroy, wipe) maps to Docker commands here.

Used where a KVM VM is impossible (cloud VMs without nested virtualization) or where a tenant prefers fast starts; validators
score this tier at half (docs/04 §3.4)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from racksdock_subnet.core.manifest import load_signed_manifest, verify_container_image
from racksdock_subnet.core.schemas import VmResources

from .base import VmHandle, VmSpec
from .nat import NAT_TAG, apply_rules, clear_rules, detect_ext_iface, forward_rules

log = logging.getLogger("racksdock.host-agent.docker")


async def _run(*cmd: str, check: bool = True, timeout: float = 300) -> subprocess.CompletedProcess:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    cp = subprocess.CompletedProcess(cmd, proc.returncode, out.decode(), err.decode())
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])} failed ({proc.returncode}): {cp.stderr.strip()[:300]}")
    return cp


def image_tag(repo: str, os_name: str, version: str) -> str:
    """catalog version '<os>_<release>' -> docker tag 'repo/<os>:<release>'"""
    release = version[len(os_name) + 1:] if version.startswith(os_name + "_") else version
    return f"{repo}/{os_name}:{release}"


def container_cpuset(vcpus: int, host_cpus: int) -> str | None:
    """Pin the rental to its first `vcpus` host CPUs when the host has more: `nproc` inside then shows the rented count instead of
    the host's (the --cpus quota alone does not change what the container sees). None = no pinning."""
    if vcpus <= 0 or host_cpus <= 0 or vcpus >= host_cpus:
        return None
    return f"0-{vcpus - 1}"


def docker_run_args(name: str, image: str, resources: VmResources, ssh_port: int, etc_dir: Path, data_dir: Path, agent_host: str, agent_port: int,
                    image_version: str, hostname: str, privileged: bool = True, shm_gb: int = 64, cpuset: str | None = None) -> list[str]:
    """The `docker run` command for a rental container. Pure function (unit-tested)."""
    args = ["docker", "run", "-d", "--name", name, "--hostname", hostname, "--restart", "unless-stopped",
            "--gpus", "all", "--shm-size", f"{shm_gb}g", "--ulimit", "memlock=-1:-1", "--ulimit", "nofile=1048576:1048576",
            "--cpus", str(resources.vcpus), "--memory", f"{resources.memory_gb}g",
            "-p", f"{ssh_port}:22", "--add-host", "host.docker.internal:host-gateway",
            "-v", f"{etc_dir}:/etc/racksdock", "-v", f"{data_dir}:/data",
            "-e", "RACKSDOCK_NODE_AGENT_MODE=real", "-e", "RACKSDOCK_AGENT_TRANSPORT=tcp", "-e", f"RACKSDOCK_AGENT_TCP_HOST={agent_host}",
            "-e", f"RACKSDOCK_AGENT_PORT={agent_port}", "-e", "RACKSDOCK_CLOUDINIT_USER_DATA=/etc/racksdock/user-data",
            "-e", "RACKSDOCK_DATA_HOST_MANAGED=1", "-e", f"RACKSDOCK_IMAGE_VERSION={image_version}"]
    if cpuset:
        args += ["--cpuset-cpus", cpuset]
    if privileged:
        args += ["--privileged"]  # nested dockerd, GPU reset and DCGM inside; sysbox is the non-privileged alternative
    else:
        args += ["--cap-add", "SYS_ADMIN", "--security-opt", "apparmor=unconfined"]
    args.append(image)
    return args


class DockerBackend:
    name = "docker"

    def __init__(self, public_ip: str | None, ssh_port: int, data_dir: Path, image_dir: Path, image_repo: str = "racksdock", guest_files: dict[str, str] | None = None,
                 agent_port: int = 5300, agent_host: str = "", privileged: bool = True, shm_gb: int = 64, bridge: str = "docker0", data_gb: int = 0,
                 expose: str = "all", reserved_ports: list[int] | None = None):
        self.public_ip = public_ip
        self.ssh_port = ssh_port
        self.data_dir = data_dir
        self.image_dir = image_dir
        self.image_repo = image_repo
        self.guest_files = guest_files or {}
        self.agent_port = agent_port
        self.agent_host = agent_host
        self.privileged = privileged
        self.shm_gb = shm_gb
        self.bridge = bridge
        self.data_gb = data_gb  # per-rental encrypted volume size (0: the node's data_disk_gb, else 200 GiB)
        self.expose = expose
        self.reserved_ports = list(reserved_ports or [22])

    # ---- host facts ------------------------------------------------------------
    @staticmethod
    def bridge_gateway(bridge: str = "docker0") -> str:
        """The host address containers reach via host.docker.internal (the docker bridge gateway)."""
        try:
            out = subprocess.run(["ip", "-j", "-4", "addr", "show", bridge], capture_output=True, text=True, timeout=10).stdout
            for iface in json.loads(out or "[]"):
                for a in iface.get("addr_info", []):
                    if a.get("family") == "inet":
                        return a["local"]
        except Exception:
            pass
        return "172.17.0.1"

    async def detect_host_ip(self) -> str:
        cp = await _run("ip", "-j", "route", "get", "1.1.1.1", check=False)
        try:
            src = json.loads(cp.stdout or "[]")[0].get("prefsrc")
            if src:
                return src
        except (json.JSONDecodeError, IndexError, AttributeError):
            pass
        return "127.0.0.1"

    async def preflight(self, resources: VmResources) -> list[str]:
        problems = []
        for tool in ("docker", "cryptsetup", "nvidia-smi"):
            if shutil.which(tool) is None:
                problems.append(f"{tool} not installed")
        if shutil.which("docker"):
            cp = await _run("docker", "info", "--format", "{{json .Runtimes}}", check=False)
            if cp.returncode != 0:
                problems.append(f"docker daemon not reachable: {cp.stderr.strip()[:120]}")
            elif "nvidia" not in cp.stdout:
                problems.append("NVIDIA container runtime not registered with docker (nvidia-ctk runtime configure --runtime=docker)")
        if not (self.image_dir / "manifest.signed.json").exists() or not (self.image_dir / "catalog.pub").exists():
            problems.append(f"signed catalog manifest or catalog.pub missing from {self.image_dir}")
        return problems

    # ---- lifecycle -------------------------------------------------------------
    async def launch(self, spec: VmSpec) -> VmHandle:
        name = f"racksdock-{spec.instance_id}"
        problems = await self.preflight(spec.resources)
        if problems:
            raise RuntimeError("host not ready: " + "; ".join(problems))
        tag = image_tag(self.image_repo, spec.image, spec.image_version)
        cp = await _run("docker", "image", "inspect", "--format", "{{.Id}}", tag, check=False)
        if cp.returncode != 0:
            raise RuntimeError(f"container image {tag} not present on this host (build it with container-image/build.sh)")
        manifest = load_signed_manifest(self.image_dir / "manifest.signed.json", (self.image_dir / "catalog.pub").read_text().strip())
        try:
            verify_container_image(manifest, spec.image, spec.image_version, cp.stdout.strip())
        except ValueError:
            # a signed archive loaded by the catalog sync may carry a store-specific id (recorded next to the manifest)
            from racksdock_node.images.sync import container_id_ok

            entry = next((e for e in manifest.images if e.os == spec.image and e.version == spec.image_version and e.kind == "container"), None)
            if entry is None or not container_id_ok(self.image_dir, entry, cp.stdout.strip()):
                raise
        cdir = self.data_dir / spec.instance_id
        etc = cdir / "etc-racksdock"
        etc.mkdir(parents=True, exist_ok=True)
        (etc / "user-data").write_text(spec.user_data)
        (etc / "image-version").write_text(spec.image_version + "\n")
        (etc / "metadata.json").write_text(json.dumps(spec.metadata.model_dump(exclude={"user_data"}), sort_keys=True) + "\n")
        if spec.operator_keys:
            (etc / "operator_authorized_keys").write_text(spec.operator_keys)
        for path, content in self.guest_files.items():
            if path == "__ssh_authorized_keys__":
                continue  # legacy key: operator keys come from spec.operator_keys (bootstrap launches only)
            if path.startswith("/etc/racksdock/"):
                dst = etc / path[len("/etc/racksdock/"):]
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(content)
                if dst.suffix == ".key":
                    dst.chmod(0o600)
        data = cdir / "data"
        await self._volume_create(cdir, self.data_gb or spec.resources.data_disk_gb or 200)
        host_ip = self.public_ip or await self.detect_host_ip()
        agent_host = self.agent_host or self.bridge_gateway(self.bridge)
        args = docker_run_args(name, tag, spec.resources, self.ssh_port, etc, data, agent_host, self.agent_port, spec.image_version, spec.hostname,
                               privileged=self.privileged, shm_gb=self.shm_gb, cpuset=container_cpuset(spec.resources.vcpus, os.cpu_count() or 0))
        await _run("docker", "rm", "-f", name, check=False)
        log.info("starting container %s from %s: %d cpus, %d GiB, port %d, data volume %d GiB", name, tag, spec.resources.vcpus, spec.resources.memory_gb,
                 self.ssh_port, self.data_gb or spec.resources.data_disk_gb or 200)
        try:
            try:
                await _run(*args, timeout=600)
            except RuntimeError as e:
                if "OCI runtime create failed" not in str(e) and "nvidia-container" not in str(e):
                    raise
                # seen right after the GPUs were handed to the host driver (dual nodes): the runtime hook fails once; retry once
                log.warning("%s: container runtime failed once (%s); retrying in 5s", name, str(e)[-300:])
                await _run("docker", "rm", "-f", name, check=False)
                await asyncio.sleep(5)
                await _run(*args, timeout=600)
        except Exception:
            await self._volume_destroy(cdir)
            raise
        handle = VmHandle(instance_id=spec.instance_id, name=name, ip=host_ip, ssh_port=self.ssh_port, extra={"cdir": str(cdir), "container": name})
        if self.expose == "all":
            await self.refresh_forwarding(handle)
        return handle

    async def container_ip(self, name: str) -> str:
        """The container's address on whichever docker network it joined (empty when docker has not assigned one)."""
        cp = await _run("docker", "inspect", "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", name, check=False)
        ips = cp.stdout.split()
        return ips[0] if ips else ""

    async def refresh_forwarding(self, handle: VmHandle) -> None:
        """Every port of the host's external interface except the reserved ones -> the container, 1:1 (docker's own -p publishes
        the ssh port, so the ssh-specific rules are left out). Also used when a restarted host-agent adopts a running container."""
        if self.expose != "all":
            return
        cip = handle.extra.get("container_ip") or await self.container_ip(handle.name)
        if not cip:
            log.warning("%s: no bridge address from docker inspect; only the ssh port is published", handle.name)
            return
        handle.extra["container_ip"] = cip
        tag = f"{NAT_TAG}-{handle.instance_id}"
        await clear_rules(_run, shutil.which, tag)
        rules = forward_rules(handle.ip or "", cip, self.ssh_port, tag, expose="all", reserved_ports=self.reserved_ports, ext_iface=await detect_ext_iface(_run))
        await apply_rules(_run, [r for r in rules if f"{cip}:22" not in r[1] and not (r[0] == "filter" and "22" in r[1])])
        log.info("%s: every external port except %s forwarded 1:1 to %s (ssh via docker -p %d:22)", handle.name,
                 ",".join(str(p) for p in sorted(set(self.reserved_ports) | {self.ssh_port})), cip, self.ssh_port)

    async def _volume_create(self, cdir: Path, size_gb: int) -> None:
        """LUKS2 volume on a sparse file; the key lives only in /run (tmpfs) for the life of the rental."""
        img = cdir / "data.img"
        key = Path("/run/racksdock-host") / f"{cdir.name}.key"
        key.parent.mkdir(parents=True, exist_ok=True)
        mapper = f"racksdock-{cdir.name}"
        (cdir / "data").mkdir(parents=True, exist_ok=True)
        await _run("truncate", "-s", f"{size_gb}G", str(img))
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(64))
        await _run("cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode", str(img), str(key))
        await _run("cryptsetup", "open", "--key-file", str(key), str(img), mapper)
        await _run("mkfs.ext4", "-q", "-F", f"/dev/mapper/{mapper}")
        await _run("mount", f"/dev/mapper/{mapper}", str(cdir / "data"))
        await _run("chown", "1000:1000", str(cdir / "data"), check=False)

    async def _volume_destroy(self, cdir: Path) -> None:
        mapper = f"racksdock-{cdir.name}"
        img = cdir / "data.img"
        key = Path("/run/racksdock-host") / f"{cdir.name}.key"
        await _run("umount", "-l", str(cdir / "data"), check=False)
        await _run("cryptsetup", "close", mapper, check=False)
        if img.exists():
            await _run("cryptsetup", "luksErase", "--batch-mode", str(img), check=False)
            img.unlink(missing_ok=True)
        if key.exists():
            key.write_bytes(b"\0" * 64)
            key.unlink()
        shutil.rmtree(cdir, ignore_errors=True)

    async def destroy(self, handle: VmHandle, graceful_timeout_s: float = 20.0) -> None:
        await _run("docker", "stop", "-t", str(int(graceful_timeout_s)), handle.name, check=False, timeout=graceful_timeout_s + 30)
        await _run("docker", "rm", "-f", "-v", handle.name, check=False)
        await clear_rules(_run, shutil.which, f"{NAT_TAG}-{handle.instance_id}")
        cdir = Path(handle.extra.get("cdir", self.data_dir / handle.instance_id))
        await self._volume_destroy(cdir)

    async def reboot(self, handle: VmHandle, hard: bool = False) -> None:
        if hard:
            await _run("docker", "kill", handle.name, check=False)
            await _run("docker", "start", handle.name)
        else:
            await _run("docker", "restart", "-t", "15", handle.name, timeout=120)

    async def state(self, handle: VmHandle) -> str:
        cp = await _run("docker", "inspect", "--format", "{{.State.Status}}", handle.name, check=False)
        st = cp.stdout.strip()
        return {"running": "running", "exited": "shut off", "created": "shut off", "paused": "paused", "restarting": "running"}.get(st, st or "unknown")

    async def cleanup_stale(self, vm_root: Path, keep: VmHandle | None = None) -> list[str]:
        """A rental container the agent has no record of is removed with its volume; the adopted one (`keep`) stays."""
        cp = await _run("docker", "ps", "-a", "--filter", "name=^racksdock-", "--format", "{{.Names}}", check=False)
        names = [n for n in cp.stdout.split() if n.startswith("racksdock-") and (keep is None or n != keep.name)]
        for n in names:
            log.warning("removing stale container %s", n)
            await _run("docker", "rm", "-f", "-v", n, check=False)
        await clear_rules(_run, shutil.which, NAT_TAG, exclude_prefix=f"{NAT_TAG}-{keep.instance_id}" if keep else None)
        if self.data_dir.exists():
            for d in self.data_dir.iterdir():
                if d.is_dir() and not (keep is not None and d.name == keep.instance_id):
                    await self._volume_destroy(d)
        return names

    async def reset_devices(self, resources: VmResources) -> list[str]:
        """Best effort: a GPU reset only succeeds with no process on the GPU; cloud VMs often refuse it (not fatal)."""
        cp = await _run("nvidia-smi", "--gpu-reset", check=False, timeout=120)
        return ["all"] if cp.returncode == 0 else []

    async def sanitize_nvme(self, resources: VmResources) -> list[str]:
        return []  # the per-rental LUKS volume is erased in _volume_destroy; no raw NVMe is handed to containers

    def host_inventory(self) -> dict:
        from ..inventory import real_inventory

        return real_inventory()
