"""Dual-mode node (docs/01 §10): one bare-metal server that delivers either tier. The GPUs are bound to vfio-pci for a VM rental
and to the host NVIDIA driver for a container rental; this backend rebinds them when a launch asks for the other mode and keeps
the last mode while idle, so consecutive rentals of the same mode pay nothing extra. Everything else is delegated to the two
single-mode backends."""
from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from dataclasses import replace
from pathlib import Path

from racksdock_subnet.core.schemas import VmResources

from .base import VmHandle, VmSpec
from .docker_backend import DockerBackend
from .libvirt_backend import LibvirtBackend

log = logging.getLogger("racksdock.host-agent.dual")
MODES = ("vm", "container")
HUGEPAGE_ATTEMPTS = 3
HUGEPAGE_WRITE_TIMEOUT_S = 900  # the kernel allocates 1 GiB pages synchronously in the sysfs write; hundreds of GiB can take minutes


def plan_rebind(current: str | None, target: str) -> str | None:
    """Which rebind script to run before a launch: 'bind-vfio.sh' for a VM after a container, 'bind-host-driver.sh' for a container
    after a VM, None when the GPUs are already in the right place. Pure (unit-tested)."""
    if target not in MODES:
        raise ValueError(f"unknown mode {target!r}")
    if current == target:
        return None
    return "bind-vfio.sh" if target == "vm" else "bind-host-driver.sh"


def gpu_driver(addr: str) -> str:
    try:
        return Path(f"/sys/bus/pci/devices/{addr}/driver").resolve().name
    except OSError:
        return ""


class DualBackend:
    name = "dual"

    def __init__(self, vm: LibvirtBackend, container: DockerBackend, resources: VmResources, state_dir: Path, default_mode: str = "vm",
                 scripts_dir: Path = Path("/usr/local/bin")):
        self.vm = vm
        self.container = container
        self.resources = resources
        self.state_file = state_dir / "gpu_mode"
        self.default_mode = default_mode
        self.scripts_dir = scripts_dir
        self._current_backend: str | None = None
        self._prereserve: asyncio.Task | None = None

    # ---- mode bookkeeping ------------------------------------------------------
    def current_mode(self) -> str | None:
        """The mode the GPUs are bound for right now, read from the devices themselves (the state file is only a hint)."""
        drivers = {gpu_driver(a) for a in self.resources.gpu_pci}
        if drivers == {"vfio-pci"}:
            return "vm"
        if drivers == {"nvidia"}:
            return "container"
        return self.state_file.read_text().strip() if self.state_file.exists() else None

    async def ensure_mode(self, target: str) -> None:
        script = plan_rebind(self.current_mode(), target)
        if script is None:
            return
        t0 = time.monotonic()
        log.info("rebinding %d GPU function(s) for %s mode (%s)", len(self.resources.gpu_pci) + len(self.resources.extra_pci), target, script)
        proc = await asyncio.create_subprocess_exec("bash", str(self.scripts_dir / script), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
        if proc.returncode != 0:
            raise RuntimeError(f"{script} failed ({proc.returncode}): {out.decode()[-400:]}")
        now = self.current_mode()
        if now != target:
            raise RuntimeError(f"GPUs are bound for {now!r} after {script}, expected {target!r}: {out.decode()[-300:]}")
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(target + "\n")
        if target == "container":
            await self.release_hugepages()
        log.info("GPUs now in %s mode after %.1fs", target, time.monotonic() - t0)

    def _backend(self, mode: str):
        return self.vm if mode == "vm" else self.container

    def _backend_for(self, handle: VmHandle):
        return self._backend(handle.extra.get("deployment") or self.current_mode() or self.default_mode)

    # ---- lifecycle -------------------------------------------------------------
    async def preflight(self, resources: VmResources) -> list[str]:
        problems = []
        for script in ("bind-vfio.sh", "bind-host-driver.sh"):
            if not (self.scripts_dir / script).exists():
                problems.append(f"{self.scripts_dir / script} missing (setup-host.sh with DEPLOYMENT=dual installs it)")
        mode = self.current_mode()
        if mode is None:
            problems.append("GPUs are bound to a mix of drivers; run bind-vfio.sh or bind-host-driver.sh")
        # each backend's own preflight, except the checks that only hold while in its mode
        for p in await self.vm.preflight(resources):
            if "bound to" not in p:
                problems.append(f"vm: {p}")
        for p in await self.container.preflight(resources):
            problems.append(f"container: {p}")
        return problems

    async def launch(self, spec: VmSpec) -> VmHandle:
        target = spec.deployment or self.current_mode() or self.default_mode
        if target not in MODES:
            raise RuntimeError(f"unknown deployment {target!r}")
        await self.ensure_mode(target)
        if target == "vm" and self._hp_dir() is not None:
            await self._await_prereserve()
            if await self.reserve_hugepages() < self.hugepages_needed():
                spec = replace(spec, resources=spec.resources.model_copy(update={"hugepage_kib": 0}))
        try:
            handle = await self._backend(target).launch(spec)
        except Exception:
            if target == "container":
                self.prereserve_hugepages()  # the pool was released for a container that never started: get it back for VMs
            raise
        handle.extra["deployment"] = target
        return handle

    async def destroy(self, handle: VmHandle, graceful_timeout_s: float = 30.0) -> None:
        await self._backend_for(handle).destroy(handle)
        if (handle.extra.get("deployment") or self.current_mode()) == "container":
            self.prereserve_hugepages()

    async def reboot(self, handle: VmHandle, hard: bool = False) -> None:
        await self._backend_for(handle).reboot(handle, hard=hard)

    async def state(self, handle: VmHandle) -> str:
        return await self._backend_for(handle).state(handle)

    async def refresh_forwarding(self, handle: VmHandle) -> None:
        fn = getattr(self._backend_for(handle), "refresh_forwarding", None)
        if fn is not None:
            await fn(handle)

    async def cleanup_stale(self, vm_root: Path, keep: VmHandle | None = None) -> list[str]:
        names = await self.vm.cleanup_stale(vm_root, keep=keep)
        names += await self.container.cleanup_stale(vm_root, keep=keep)
        return names

    # ---- hugepages: a VM takes its memory from the 1 GiB hugepage pool reserved at boot; a container cannot use that pool, so the
    # pages are released for a container rental and re-reserved for the next VM (compaction + retries; if the kernel cannot hand
    # back the full pool the next VM is launched on ordinary pages, which only slows its start)
    def _hp_dir(self) -> Path | None:
        kib = self.resources.hugepage_kib
        d = Path(f"/sys/kernel/mm/hugepages/hugepages-{kib}kB")
        return d if kib and d.exists() else None

    def hugepages_needed(self) -> int:
        """Pages the VM's guest RAM takes from the pool (the domain XML sizes memory in GiB from the same resources)."""
        kib = self.resources.hugepage_kib or 0
        return 0 if not kib else -(-(self.resources.memory_gb * 1024 * 1024) // kib)

    @property
    def _hp_hint(self) -> Path:
        return self.state_file.parent / "hugepages_reserved"

    async def _await_prereserve(self) -> None:
        t = self._prereserve
        if t is not None and not t.done():
            await t
        self._prereserve = None

    async def release_hugepages(self) -> None:
        await self._await_prereserve()
        d = self._hp_dir()
        if d is None:
            return
        have = int((d / "nr_hugepages").read_text())
        if have:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self._hp_hint.write_text(f"{have}\n")
            await asyncio.to_thread((d / "nr_hugepages").write_text, "0\n")  # freeing is quick, but never block the event loop
            log.info("released %d hugepages for the container tier", have)

    async def reserve_hugepages(self) -> int:
        """Returns the number of free hugepages after the attempt (0 when the host has no hugepage pool). The sysfs write that
        grows the pool runs in a thread (the kernel compacts and allocates synchronously inside it, minutes for 400+ GiB) while the
        event loop keeps serving heartbeats and commands."""
        d = self._hp_dir()
        if d is None:
            return 0
        free = int((d / "free_hugepages").read_text())
        if free >= self.hugepages_needed():
            return free  # the boot-time pool is intact (nothing was released, or it came back in full)
        want = max(self.hugepages_needed(), int(self._hp_hint.read_text()) if self._hp_hint.exists() else 0)
        t0 = time.monotonic()
        for attempt in range(HUGEPAGE_ATTEMPTS):
            if attempt:
                await asyncio.to_thread(Path("/proc/sys/vm/drop_caches").write_text, "3\n")
                await asyncio.to_thread(Path("/proc/sys/vm/compact_memory").write_text, "1\n")
            try:
                await asyncio.wait_for(asyncio.to_thread((d / "nr_hugepages").write_text, f"{want}\n"), timeout=HUGEPAGE_WRITE_TIMEOUT_S)
            except TimeoutError:
                log.warning("hugepage reservation still running in the kernel after %ds", HUGEPAGE_WRITE_TIMEOUT_S)
            free = int((d / "free_hugepages").read_text())
            if free >= self.hugepages_needed():
                log.info("reserved %d hugepages (%d free) after %d attempt(s), %.0fs", int((d / "nr_hugepages").read_text()), free, attempt + 1, time.monotonic() - t0)
                return free
            log.info("hugepage pool at %d of %d after attempt %d (%.0fs); compacting and retrying", free, want, attempt + 1, time.monotonic() - t0)
        free = int((d / "free_hugepages").read_text())
        log.warning("only %d of %d hugepages could be reserved after %.0fs; the next VM starts on ordinary pages", free, self.hugepages_needed(), time.monotonic() - t0)
        return free

    def prereserve_hugepages(self) -> None:
        """After a container rental ends: get the VM pool back in the background so the next VM launch does not wait for it."""
        if self._hp_dir() is None or (self._prereserve is not None and not self._prereserve.done()):
            return
        self._prereserve = asyncio.create_task(self.reserve_hugepages())

    async def reset_devices(self, resources: VmResources) -> list[str]:
        return await self._backend(self.current_mode() or self.default_mode).reset_devices(resources)

    async def sanitize_nvme(self, resources: VmResources) -> list[str]:
        return await self._backend(self.current_mode() or self.default_mode).sanitize_nvme(resources)

    def host_inventory(self) -> dict:
        return self.vm.host_inventory()
