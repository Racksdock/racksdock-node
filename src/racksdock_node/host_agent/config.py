from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from racksdock_subnet.core.agentproto import VSOCK_PORT
from racksdock_subnet.core.schemas import VmResources
from racksdock_subnet.verifier.config import TrustConfig


@dataclass
class NetworkConfig:
    """How the VM reaches the internet and how tenants reach its SSH port.

    bridge: the VM gets `public_ip` on `bridge` (one public IP per node).
    nat:    the VM sits on the libvirt NAT network `nat_network`; the host forwards host:`nat_ssh_port` to VM:22 (single-IP hosts,
            lab boxes). `public_ip` is then the host address tenants connect to (auto-detected from the default route when unset).
    """

    mode: str = "bridge"  # bridge | nat
    bridge: str = "br0"
    public_ip: str | None = None
    nat_network: str = "default"
    nat_ssh_port: int = 20022
    sriov_vf_pci: str | None = None


@dataclass
class HostAgentConfig:
    mode: str = "real"  # real | sim | static
    platform_ws: str = "ws://127.0.0.1:8000/v1/hosts/ws"
    state_dir: Path = Path("/var/lib/racksdock-host-agent")
    key_path: Path = Path("/var/lib/racksdock-host-agent/host.seed")
    miner_hotkey: str = ""
    host_image_version: str = "host_v0"
    region: str = "unknown"
    oem_platform: str = ""
    image_dir: Path = Path("/var/lib/racksdock/images")
    vm_dir: Path = Path("/var/lib/racksdock/vms")
    agent_port: int = VSOCK_PORT
    agent_transport: str = "vsock"  # vsock | tcp
    resources: VmResources = field(default_factory=VmResources)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    heartbeat_interval_s: float = 30.0
    boot_timeout_s: float = 900.0
    teardown_ack_timeout_s: float = 60.0
    # simulation
    sim_node_dir: Path | None = None
    sim_node_agent_cmd: str = "racksdock-node-agent"
    sim_faults_file: Path | None = None
    # static mode (this machine is the node; workflow tests only)
    static_public_ip: str = "127.0.0.1"
    static_ssh_port: int = 22
    static_authorized_keys: Path = Path("/home/ubuntu/.ssh/authorized_keys")
    static_ssh_service: str | None = "ssh"  # restarted on "reboot" of a static node; None disables (tests)
    trust: TrustConfig = field(default_factory=TrustConfig.from_env)

    @property
    def sim(self) -> bool:
        return self.mode == "sim"

    @property
    def static(self) -> bool:
        return self.mode == "static"

    @property
    def metadata_carries_user_data(self) -> bool:
        """sim and static VMs cannot read cloud-init's copy of the user-data, so the host sends it over the agent link."""
        return self.mode in ("sim", "static")

    @classmethod
    def from_env(cls) -> HostAgentConfig:
        e = os.environ.get
        cfg = cls(
            mode=e("RACKSDOCK_HOST_AGENT_MODE", "real"),
            platform_ws=e("RACKSDOCK_PLATFORM_WS", "ws://127.0.0.1:8000/v1/hosts/ws"),
            state_dir=Path(e("RACKSDOCK_STATE_DIR", "/var/lib/racksdock-host-agent")),
            miner_hotkey=e("RACKSDOCK_MINER_HOTKEY", ""),
            host_image_version=e("RACKSDOCK_HOST_IMAGE_VERSION", "host_v0"),
            region=e("RACKSDOCK_REGION", "unknown"),
            oem_platform=e("RACKSDOCK_OEM_PLATFORM", ""),
            image_dir=Path(e("RACKSDOCK_IMAGE_DIR", "/var/lib/racksdock/images")),
            vm_dir=Path(e("RACKSDOCK_VM_DIR", "/var/lib/racksdock/vms")),
            agent_port=int(e("RACKSDOCK_AGENT_PORT", str(VSOCK_PORT))),
            agent_transport=e("RACKSDOCK_AGENT_TRANSPORT", "vsock"),
            heartbeat_interval_s=float(e("RACKSDOCK_HEARTBEAT_S", "30")),
            sim_node_dir=Path(e("RACKSDOCK_SIM_NODE_DIR")) if e("RACKSDOCK_SIM_NODE_DIR") else None,
            sim_node_agent_cmd=e("RACKSDOCK_SIM_NODE_AGENT_CMD", "racksdock-node-agent"),
            sim_faults_file=Path(e("RACKSDOCK_SIM_FAULTS")) if e("RACKSDOCK_SIM_FAULTS") else None,
            trust=TrustConfig.from_env(),
        )
        cfg.key_path = cfg.state_dir / "host.seed"
        rp = e("RACKSDOCK_RESOURCES_JSON")
        if rp and Path(rp).exists():
            cfg.resources = VmResources.model_validate(json.loads(Path(rp).read_text()))
        if e("RACKSDOCK_NET_MODE"):
            cfg.network = NetworkConfig(mode=e("RACKSDOCK_NET_MODE", "bridge"), bridge=e("RACKSDOCK_NET_BRIDGE", "br0"),
                                        public_ip=e("RACKSDOCK_PUBLIC_IP") or None, nat_network=e("RACKSDOCK_NAT_NETWORK", "default"),
                                        nat_ssh_port=int(e("RACKSDOCK_NAT_SSH_PORT", "20022")), sriov_vf_pci=e("RACKSDOCK_SRIOV_VF_PCI"))
        if cfg.sim or cfg.static:
            cfg.agent_transport = "tcp"
            if not e("RACKSDOCK_AGENT_PORT"):
                cfg.agent_port = 0  # ephemeral: several simulated hosts can share one machine
        cfg.static_public_ip = e("RACKSDOCK_STATIC_PUBLIC_IP", "127.0.0.1")
        cfg.static_ssh_port = int(e("RACKSDOCK_STATIC_SSH_PORT", "22"))
        cfg.static_authorized_keys = Path(e("RACKSDOCK_STATIC_AUTHORIZED_KEYS", "/home/ubuntu/.ssh/authorized_keys"))
        cfg.static_ssh_service = e("RACKSDOCK_STATIC_SSH_SERVICE", "ssh") or None
        return cfg
