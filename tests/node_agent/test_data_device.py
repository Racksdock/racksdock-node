from pathlib import Path

from racksdock_node.node_agent.config import NodeAgentConfig
from racksdock_node.node_agent.system import DataDisk, resolve_data_device


def test_resolve_data_device_prefers_configured_then_virtio_serial(tmp_path, monkeypatch):
    cfg = NodeAgentConfig(mode="real", data_device=str(tmp_path / "configured"))
    assert resolve_data_device(cfg) != cfg.data_device  # configured path does not exist yet
    (tmp_path / "configured").write_bytes(b"")
    assert resolve_data_device(cfg) == str(tmp_path / "configured")


def test_data_disk_open_is_idempotent_and_erase_only_touches_opened(tmp_path):
    cfg = NodeAgentConfig(mode="sim", state_dir=tmp_path)
    d = DataDisk(cfg)
    assert d.open() and d.opened
    key = bytes(d.key)
    assert d.open() and bytes(d.key) == key  # second open: no reformat, same key
    d.erase()
    assert d.key is None
    cold = DataDisk(NodeAgentConfig(mode="real", state_dir=tmp_path, data_device=str(tmp_path / "missing")))
    cold.erase()  # nothing opened: must not run cryptsetup/umount
    assert cold.device is None


def test_data_disk_adopts_an_already_open_disk_after_an_agent_restart(tmp_path, monkeypatch):
    """A node-agent restarted inside a running VM (OOM-killed during a tenant job on the 8x4090 box) found the LUKS mapper already
    open and crashed re-formatting it, every 2 s for hours. It must adopt the open disk instead."""
    import subprocess as sp

    from racksdock_node.node_agent import system

    cfg = NodeAgentConfig(mode="real", state_dir=tmp_path, data_device=str(tmp_path / "vdb"), data_mount=tmp_path / "data")
    (tmp_path / "vdb").write_bytes(b"")
    calls = []
    monkeypatch.setattr(system.shutil, "which", lambda name: "/usr/sbin/cryptsetup")
    monkeypatch.setattr(system.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or sp.CompletedProcess(cmd, 0))
    real_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda self: True if str(self) == "/dev/mapper/racksdockdata" else real_exists(self))
    monkeypatch.setattr(system.DataDisk, "_mounted", lambda self: False)
    d = DataDisk(cfg)
    assert d.open() is True and d.opened and d.key is None
    assert not any("luksFormat" in " ".join(c) for c in calls)  # never re-format a disk that is already open
    assert any(c[0] == "mount" for c in calls)  # but make sure the tenant sees it
