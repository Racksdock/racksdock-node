"""NoCloud seed ISO: user-data (tenant's, untouched), meta-data, vendor-data (platform metadata), network-config."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pycdlib
import yaml
from racksdock_subnet.core.agentproto import Metadata


def vendor_data(meta: Metadata, extra_files: dict[str, str] | None = None) -> str:
    """Platform metadata for the node-agent. `extra_files` (path -> content) are written root-only; the host-agent uses them to
    hand dev-trust-mode material (dev CA, simulated node identity, node-agent env) to a real VM. Never used in production."""
    files = [
        {"path": "/etc/racksdock/metadata.json", "permissions": "0644", "content": json.dumps(meta.model_dump(exclude={"user_data"}), sort_keys=True) + "\n"},
        {"path": "/etc/racksdock/image-version", "permissions": "0644", "content": meta.image_version + "\n"},
    ]
    cfg: dict = {"write_files": files}
    for path, content in sorted((extra_files or {}).items()):
        if path == "__ssh_authorized_keys__":
            cfg["ssh_authorized_keys"] = [line for line in content.splitlines() if line.strip()]
            continue
        files.append({"path": path, "permissions": "0600", "owner": "root:root", "content": content})
    return "#cloud-config\n" + yaml.safe_dump(cfg, sort_keys=True)


def meta_data(instance_id: str, hostname: str) -> str:
    return yaml.safe_dump({"instance-id": instance_id, "local-hostname": hostname}, sort_keys=True)


def network_config(public_ip: str | None, gateway: str | None = None, prefix: int = 24, nameservers=("1.1.1.1", "8.8.8.8")) -> str | None:
    if not public_ip:
        return None
    cfg = {"version": 2, "ethernets": {"primary": {"match": {"name": "en*"}, "addresses": [f"{public_ip}/{prefix}"],
                                                   "nameservers": {"addresses": list(nameservers)}}}}
    if gateway:
        cfg["ethernets"]["primary"]["routes"] = [{"to": "default", "via": gateway}]
    return yaml.safe_dump(cfg, sort_keys=True)


def build_seed_iso(path: Path, user_data: str, meta: Metadata, hostname: str, net_cfg: str | None = None,
                   extra_files: dict[str, str] | None = None) -> Path:
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, rock_ridge="1.09", vol_ident="CIDATA")
    files = {"user-data": user_data, "meta-data": meta_data(meta.instance_id, hostname), "vendor-data": vendor_data(meta, extra_files)}
    if net_cfg:
        files["network-config"] = net_cfg
    for i, (name, content) in enumerate(files.items()):
        data = content.encode()
        iso_name = f"/{name.upper().replace('-', '_')[:8]}.;1"
        iso.add_fp(io.BytesIO(data), len(data), iso_name, rr_name=name, joliet_path=f"/{name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    iso.write(str(path))
    iso.close()
    return path


def read_seed_iso(path: Path) -> dict[str, str]:
    iso = pycdlib.PyCdlib()
    iso.open(str(path))
    out = {}
    for child in iso.list_children(joliet_path="/"):
        if child.is_dot() or child.is_dotdot() or child.is_dir():
            continue
        name = child.file_identifier().decode("utf-16-be")
        buf = io.BytesIO()
        iso.get_file_from_iso_fp(buf, joliet_path=f"/{name}")
        out[name] = buf.getvalue().decode()
    iso.close()
    return out
