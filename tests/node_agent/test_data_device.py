
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
