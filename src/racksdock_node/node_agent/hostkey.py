"""SSH host key handling: real VMs use sshd's ed25519 host key; simulation generates one."""
from __future__ import annotations

import subprocess
from pathlib import Path

from racksdock_subnet.core.keys import SigningKey


def ensure_host_key_line(cfg) -> str:
    if cfg.sim:
        key = SigningKey.load_or_create(cfg.state_dir / "sim_ssh_host_key.seed")
        return key.public.ssh_public_line("racksdock-sim-host")
    pub = Path(cfg.ssh_host_key_pub)
    if not pub.exists():
        subprocess.run(["ssh-keygen", "-A"], check=False, capture_output=True)
    return pub.read_text().strip()
