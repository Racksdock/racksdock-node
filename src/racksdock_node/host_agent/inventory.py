"""Host inventory for cross-checks only (docs/01 §2). Never used as verification evidence."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from racksdock_subnet.core.schemas import HostInventory


def _sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return ""


def real_inventory() -> dict:
    inv = HostInventory()
    lspci = _sh(["lspci", "-Dnn"])
    for line in lspci.splitlines():
        m = re.match(r"^(\S+)\s+(.*?)\s+\[([0-9a-f]{4})\]:\s+.*\[(10de):([0-9a-f]{4})\]", line)
        if not m:
            continue
        addr, cls_name, cls_code, vendor, dev = m.groups()
        if cls_code in ("0302", "0300"):
            inv.gpu_pci.append(addr)
        elif cls_code == "0680" and vendor == "10de":
            inv.nvswitch_pci.append(addr)
    if shutil.which("nvme"):
        try:
            inv.nvme = json.loads(_sh(["nvme", "list", "-o", "json"]) or "{}").get("Devices", [])
        except json.JSONDecodeError:
            pass
    if shutil.which("dmidecode"):
        inv.board_serial = _sh(["dmidecode", "-s", "baseboard-serial-number"]).strip() or None
        inv.bmc_serial = _sh(["dmidecode", "-s", "system-serial-number"]).strip() or None
    lscpu = _sh(["lscpu"])
    m = re.search(r"Model name:\s+(.*)", lscpu)
    inv.cpu_model = m.group(1).strip() if m else ""
    cores = re.search(r"Core\(s\) per socket:\s+(\d+)", lscpu)
    sockets = re.search(r"Socket\(s\):\s+(\d+)", lscpu)
    if cores and sockets:
        inv.physical_cores = int(cores.group(1)) * int(sockets.group(1))
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                inv.memory_gb = int(line.split()[1]) // (1024 * 1024)
    except OSError:
        pass
    try:
        inv.nics = [{"name": p.name} for p in Path("/sys/class/net").iterdir() if p.name != "lo"]
    except OSError:
        pass
    return inv.model_dump()


def sim_inventory(sim_node_dir: Path) -> dict:
    inv = HostInventory(
        board_serial="SIM-BOARD-0001", bmc_serial="SIM-BMC-0001",
        gpu_pci=[f"0000:{17 + i * 8:02x}:00.0" for i in range(8)],
        nvswitch_pci=[f"0000:{5 + i:02x}:00.0" for i in range(4)],
        nvme=[{"DevicePath": f"/dev/nvme{i}n1", "PhysicalSize": 3840755982336} for i in range(8)],
        cpu_model="Simulated Xeon 6", physical_cores=128, memory_gb=2048, nics=[{"name": "ens1f0"}, {"name": "ens1f1"}],
    )
    return inv.model_dump()
