"""The systemd units baked into the catalog images."""
from pathlib import Path

UNITS = Path(__file__).parents[2] / "image-build/overlay/etc/systemd/system"


def _section(text: str, name: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    cur = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            cur = line[1:-1]
        elif cur == name and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out.setdefault(k.strip(), []).extend(v.split())
    return out


def test_multi_user_units_do_not_order_after_late_cloud_init_units():
    """cloud-init.target and cloud-final.service run after multi-user.target on Ubuntu 24.04. A unit wanted by multi-user.target
    that is ordered After= them forms an ordering cycle; systemd then drops one job at random, which is how the node-agent
    silently failed to start in some VMs during the 8x4090 test."""
    for unit in sorted(UNITS.glob("*.service")):
        text = unit.read_text()
        install = _section(text, "Install")
        if "multi-user.target" not in install.get("WantedBy", []):
            continue
        after = _section(text, "Unit").get("After", [])
        for bad in ("cloud-init.target", "cloud-final.service"):
            assert bad not in after, f"{unit.name}: After={bad} creates an ordering cycle with multi-user.target"


def test_node_agent_unit_waits_for_cloud_init_init_stage():
    unit = (UNITS / "racksdock-node-agent.service").read_text()
    after = _section(unit, "Unit")["After"]
    assert "cloud-init.service" in after  # user-data copy, host keys and vendor-data files exist after the init stage
    assert "ExecStart" in _section(unit, "Service") and "User" not in _section(unit, "Service")  # runs as root (docs/node-agent-privileges.md)
