from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from racksdock_subnet.core.agentproto import Metadata
from racksdock_subnet.core.schemas import VmResources


@dataclass
class VmSpec:
    instance_id: str
    image: str
    image_version: str
    user_data: str
    metadata: Metadata
    resources: VmResources
    vm_dir: Path
    image_path: Path
    hostname: str = "racksdock"
    public_ip: str | None = None
    ssh_port: int = 22
    deployment: str | None = None  # dual nodes: vm | container for this rental
    operator_keys: str | None = None  # dev trust mode, bootstrap launches only: operator SSH public keys added to the guest


@dataclass
class VmHandle:
    instance_id: str
    name: str
    ip: str | None = None
    ssh_port: int = 22
    extra: dict = field(default_factory=dict)


class HypervisorBackend(Protocol):
    name: str

    async def launch(self, spec: VmSpec) -> VmHandle: ...

    async def destroy(self, handle: VmHandle) -> None: ...

    async def reboot(self, handle: VmHandle, hard: bool = False) -> None: ...

    async def state(self, handle: VmHandle) -> str: ...

    async def reset_devices(self, resources: VmResources) -> list[str]: ...

    async def sanitize_nvme(self, resources: VmResources) -> list[str]: ...

    def host_inventory(self) -> dict: ...
