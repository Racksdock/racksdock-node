"""Interactions with the guest OS: cloud-init, launch status, LUKS data disk. All no-ops in simulation."""
from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import subprocess
from pathlib import Path

LAUNCH_STATUS_FILE = Path("/run/racksdock/launch.status")


async def wait_cloud_init(cfg, timeout: float = 540) -> bool:
    if cfg.sim:
        await asyncio.sleep(0.2)
        return True
    if shutil.which("cloud-init") is None:
        return True
    proc = await asyncio.create_subprocess_exec("cloud-init", "status", "--wait", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return False
    return proc.returncode == 0


def read_user_data(cfg, fallback: str | None) -> bytes:
    """Hash input must be the user-data this node booted with.

    Real VMs read cloud-init's own copy (the host sends none). Simulated and static nodes receive the copy from the
    host-agent, which is authoritative there: a static host may carry an unrelated cloud-init user-data from its provider.
    """
    if cfg.sim or fallback is not None:
        return (fallback or "").encode()
    try:
        return Path(cfg.cloudinit_user_data).read_bytes()
    except OSError:
        return b""


def launch_status(cfg) -> str | None:
    if cfg.sim:
        return "started"
    try:
        return LAUNCH_STATUS_FILE.read_text().strip()
    except OSError:
        return None


class DataDisk:
    """LUKS2 data disk keyed by a random key held only in memory (docs/05 §3.2)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.key: bytearray | None = None
        self.mapper = "racksdockdata"
        self.opened = False  # erase() only touches what open() set up

    def open(self) -> bool:
        if self.cfg.sim:
            self.key = bytearray(secrets.token_bytes(64))
            self.opened = True
            return True
        dev = self.cfg.data_device
        if not Path(dev).exists() or shutil.which("cryptsetup") is None:
            return False
        self.key = bytearray(secrets.token_bytes(64))
        keyfile = Path("/run/racksdock/data.key")
        keyfile.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(bytes(self.key))
        try:
            subprocess.run(["cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode", dev, str(keyfile)], check=True, capture_output=True)
            subprocess.run(["cryptsetup", "open", "--key-file", str(keyfile), dev, self.mapper], check=True, capture_output=True)
            subprocess.run(["mkfs.ext4", "-q", "-F", f"/dev/mapper/{self.mapper}"], check=True, capture_output=True)
            self.cfg.data_mount.mkdir(parents=True, exist_ok=True)
            subprocess.run(["mount", f"/dev/mapper/{self.mapper}", str(self.cfg.data_mount)], check=True, capture_output=True)
            subprocess.run(["chown", "ubuntu:ubuntu", str(self.cfg.data_mount)], check=False, capture_output=True)
            self.opened = True
            return True
        finally:
            try:
                keyfile.write_bytes(b"\0" * 64)
                keyfile.unlink()
            except OSError:
                pass

    def erase(self) -> None:
        if self.key is not None:
            for i in range(len(self.key)):
                self.key[i] = 0
            self.key = None
        if self.cfg.sim or not self.opened or shutil.which("cryptsetup") is None:
            return  # never unmount or erase devices this agent did not set up
        subprocess.run(["umount", "-l", str(self.cfg.data_mount)], check=False, capture_output=True)
        subprocess.run(["cryptsetup", "close", self.mapper], check=False, capture_output=True)
        subprocess.run(["cryptsetup", "luksErase", "--batch-mode", self.cfg.data_device], check=False, capture_output=True)
        subprocess.run(["sync"], check=False)
