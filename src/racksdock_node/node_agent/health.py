"""Health allowlist (docs/03 §7). This module is the complete list of what leaves the VM towards validators."""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from racksdock_subnet.core.identity import gpu_set_hash
from racksdock_subnet.core.schemas import GpuCounters, HealthReport, MicroBench, VmMode

_BOOT = time.monotonic()


def _uptime_s() -> float:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except OSError:
        return time.monotonic() - _BOOT


def _xid_counts_from_kernel_log() -> dict[str, int]:
    """Counts Xid events by Xid number from dmesg (real mode). Classes are the Xid numbers as strings."""
    try:
        out = subprocess.run(["dmesg", "--kernel"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return {}
    counts: dict[str, int] = {}
    for m in re.finditer(r"NVRM: Xid \(PCI:[0-9a-f:.]+\): (\d+)", out):
        counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts


def _nvml_counters() -> list[GpuCounters]:
    import pynvml  # type: ignore

    pynvml.nvmlInit()
    try:
        n = pynvml.nvmlDeviceGetCount()
        out = []
        for i in range(n):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            try:
                plimit = pynvml.nvmlDeviceGetPowerManagementLimit(h) / 1000.0
            except pynvml.NVMLError:
                plimit = 0.0
            try:
                throttle = int(pynvml.nvmlDeviceGetCurrentClocksThrottleReasons(h))
            except pynvml.NVMLError:
                throttle = 0
            try:
                ecc = int(pynvml.nvmlDeviceGetTotalEccErrors(h, pynvml.NVML_MEMORY_ERROR_TYPE_UNCORRECTED, pynvml.NVML_VOLATILE_ECC))
            except pynvml.NVMLError:
                ecc = 0
            nvl = 0
            for link in range(18):
                try:
                    for ctr in range(4):
                        nvl += int(pynvml.nvmlDeviceGetNvLinkErrorCounter(h, link, ctr))
                except pynvml.NVMLError:
                    break
            out.append(GpuCounters(index=i, power_limit_w=plimit, clocks_throttle_reasons=throttle, ecc_uncorrectable_total=ecc, nvlink_error_total=nvl))
        return out
    finally:
        pynvml.nvmlShutdown()


def _sim_counters(cfg) -> list[GpuCounters]:
    from racksdock_subnet.verifier.inventory import load_sim_inventory

    faults = {}
    if cfg.sim_faults_file and Path(cfg.sim_faults_file).exists():
        faults = json.loads(Path(cfg.sim_faults_file).read_text())
    out = []
    for g in load_sim_inventory(cfg.sim_node_dir):
        f = faults.get(str(g.index), {})
        out.append(GpuCounters(
            index=g.index,
            power_limit_w=float(f.get("power_limit_w", g.power_limit_w)),
            clocks_throttle_reasons=int(f.get("clocks_throttle_reasons", 0)),
            ecc_uncorrectable_total=int(f.get("ecc_uncorrectable_total", 0)),
            nvlink_error_total=int(f.get("nvlink_error_total", 0)),
        ))
    return out


def microbench() -> MicroBench:
    """2-second memory copy + 1-second CPU loop. Coarse, but enough to notice a starved VM."""
    size = 64 * 1024 * 1024
    src = bytearray(size)
    dst = bytearray(size)
    mv_src, mv_dst = memoryview(src), memoryview(dst)
    t0 = time.perf_counter()
    copied = 0
    while time.perf_counter() - t0 < 1.0:
        mv_dst[:] = mv_src
        copied += size
    stream_gbps = copied / (time.perf_counter() - t0) / 1e9 * 2  # read + write
    t1 = time.perf_counter()
    x, n = 0, 0
    while time.perf_counter() - t1 < 0.5:
        for _ in range(10000):
            x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        n += 10000
    cpu_score = n / (time.perf_counter() - t1) / 1e6
    return MicroBench(stream_gbps=round(stream_gbps, 2), cpu_loop_score=round(cpu_score, 3))


def build_health(cfg, gpu_ids: list[str], measurements_hash: str | None, mode: VmMode, launch_status: str | None,
                 node_agent_version: str, include_microbench: bool) -> HealthReport:
    if cfg.sim:
        gpus = _sim_counters(cfg)
        xids = {}
        if cfg.sim_faults_file and Path(cfg.sim_faults_file).exists():
            xids = json.loads(Path(cfg.sim_faults_file).read_text()).get("xid_counts_by_class", {})
    else:
        try:
            gpus = _nvml_counters()
        except Exception:
            gpus = []
        xids = _xid_counts_from_kernel_log()
    return HealthReport(
        gpu_ids_hash=gpu_set_hash(gpu_ids),
        firmware_measurements_hash=measurements_hash,
        image_version=cfg.image_version,
        node_agent_version=node_agent_version,
        gpus=gpus,
        xid_counts_by_class=xids,
        uptime_s=_uptime_s(),
        microbench=microbench() if include_microbench else None,
        mode=mode,
        launch_status=launch_status,
    )
