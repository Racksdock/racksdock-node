from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from racksdock_subnet.core.agentproto import HOST_CID, VSOCK_PORT
from racksdock_subnet.verifier.config import TrustConfig


@dataclass
class NodeAgentConfig:
    mode: str = "real"  # real | sim
    transport: str = "vsock"  # vsock | tcp
    host_cid: int = HOST_CID
    port: int = VSOCK_PORT
    tcp_host: str = "127.0.0.1"
    state_dir: Path = Path("/var/lib/racksdock-node-agent")
    ssh_host_key_pub: Path = Path("/etc/ssh/ssh_host_ed25519_key.pub")
    cloudinit_user_data: Path = Path("/var/lib/cloud/instance/user-data.txt")
    data_device: str = "/dev/disk/by-id/racksdock-data"
    data_mount: Path = Path("/data")
    data_host_managed: bool = False  # container tier: the host mounts an encrypted volume at data_mount; nothing to format or erase here
    metrics_port: int = 9100
    image_version: str = "unknown"
    sim_node_dir: Path | None = None
    sim_faults_file: Path | None = None
    trust: TrustConfig = field(default_factory=TrustConfig.from_env)

    @classmethod
    def from_env(cls) -> NodeAgentConfig:
        e = os.environ.get
        return cls(
            mode=e("RACKSDOCK_NODE_AGENT_MODE", "real"),
            transport=e("RACKSDOCK_AGENT_TRANSPORT", "vsock"),
            host_cid=int(e("RACKSDOCK_HOST_CID", str(HOST_CID))),
            port=int(e("RACKSDOCK_AGENT_PORT", str(VSOCK_PORT))),
            tcp_host=e("RACKSDOCK_AGENT_TCP_HOST", "127.0.0.1"),
            state_dir=Path(e("RACKSDOCK_STATE_DIR", "/var/lib/racksdock-node-agent")),
            ssh_host_key_pub=Path(e("RACKSDOCK_SSH_HOST_KEY_PUB", "/etc/ssh/ssh_host_ed25519_key.pub")),
            cloudinit_user_data=Path(e("RACKSDOCK_CLOUDINIT_USER_DATA", "/var/lib/cloud/instance/user-data.txt")),
            data_device=e("RACKSDOCK_DATA_DEVICE", "/dev/disk/by-id/racksdock-data"),
            data_mount=Path(e("RACKSDOCK_DATA_MOUNT", "/data")),
            data_host_managed=e("RACKSDOCK_DATA_HOST_MANAGED", "0") == "1",
            metrics_port=int(e("RACKSDOCK_METRICS_PORT", "9100")),
            image_version=e("RACKSDOCK_IMAGE_VERSION", _read_image_version()),
            sim_node_dir=Path(e("RACKSDOCK_SIM_NODE_DIR")) if e("RACKSDOCK_SIM_NODE_DIR") else None,
            sim_faults_file=Path(e("RACKSDOCK_SIM_FAULTS")) if e("RACKSDOCK_SIM_FAULTS") else None,
            trust=TrustConfig.from_env(),
        )

    @property
    def sim(self) -> bool:
        return self.mode == "sim"


def _read_image_version() -> str:
    p = Path("/etc/racksdock/image-version")
    try:
        return p.read_text().strip()
    except OSError:
        return "unknown"
