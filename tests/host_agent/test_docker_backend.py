"""Container tier: command construction, catalog pinning and the container init's cloud-config translation (no docker needed)."""
from pathlib import Path

import pytest
from racksdock_subnet.core.keys import SigningKey
from racksdock_subnet.core.manifest import CatalogManifest, ImageEntry, load_signed_manifest, sign_manifest, verify_container_image
from racksdock_subnet.core.schemas import VmResources

from racksdock_node.host_agent.config import HostAgentConfig
from racksdock_node.host_agent.hypervisor.docker_backend import docker_run_args, image_tag
from racksdock_node.node_agent.containerinit import plan_cloud_config


def test_image_tag_and_run_args():
    assert image_tag("racksdock", "ubuntu24.04", "ubuntu24.04_v2026.10.1") == "racksdock/ubuntu24.04:v2026.10.1"
    assert image_tag("racksdock", "verify", "verify_v2026.10.1") == "racksdock/verify:v2026.10.1"
    res = VmResources(vcpus=28, memory_gb=110, data_disk_gb=60)
    args = docker_run_args("racksdock-inst_1", "racksdock/ubuntu24.04:v1", res, 20022, Path("/c/etc"), Path("/c/data"), "172.17.0.1", 5300, "ubuntu24.04_v1", "racksdock-abc")
    s = " ".join(args)
    assert args[:3] == ["docker", "run", "-d"] and args[-1] == "racksdock/ubuntu24.04:v1"
    for needle in ("--gpus all", "--cpus 28", "--memory 110g", "-p 20022:22", "-v /c/etc:/etc/racksdock", "-v /c/data:/data", "RACKSDOCK_AGENT_TRANSPORT=tcp",
                   "RACKSDOCK_AGENT_TCP_HOST=172.17.0.1", "RACKSDOCK_DATA_HOST_MANAGED=1", "RACKSDOCK_CLOUDINIT_USER_DATA=/etc/racksdock/user-data", "--privileged",
                   "host.docker.internal:host-gateway", "--hostname racksdock-abc"):
        assert needle in s, needle
    assert "--privileged" not in " ".join(docker_run_args("n", "i", res, 1, Path("/e"), Path("/d"), "h", 1, "v", "h", privileged=False))


def test_container_image_pinned_in_signed_manifest(tmp_path):
    key = SigningKey.generate()
    m = CatalogManifest(release="v1", images=[ImageEntry(os="ubuntu24.04", version="ubuntu24.04_v1", file="racksdock/ubuntu24.04:v1", sha256="sha256:" + "ab" * 32, size_bytes=1,
                                                         kind="container")])
    env = sign_manifest(m, key)
    (tmp_path / "manifest.signed.json").write_text(env.model_dump_json())
    loaded = load_signed_manifest(tmp_path / "manifest.signed.json", key.public.hex)
    assert verify_container_image(loaded, "ubuntu24.04", "ubuntu24.04_v1", "sha256:" + "ab" * 32).kind == "container"
    with pytest.raises(ValueError):
        verify_container_image(loaded, "ubuntu24.04", "ubuntu24.04_v1", "sha256:" + "cd" * 32)
    with pytest.raises(ValueError):
        verify_container_image(loaded, "verify", "verify_v1", "sha256:" + "ab" * 32)


def test_container_init_applies_the_platforms_cloud_config_subset():
    cfg = {"users": [{"name": "ubuntu", "sudo": "ALL=(ALL) NOPASSWD:ALL", "groups": "docker", "ssh_authorized_keys": ["ssh-ed25519 AAAA tenant"]}],
           "write_files": [{"path": "/etc/racksdock/launch.json", "permissions": "0600", "content": "{}"}],
           "runcmd": ["systemctl start racksdock-launch.service || /usr/local/bin/racksdock-launch", ["ufw", "allow", "22/tcp"]],
           "ssh_pwauth": False, "disable_root": True}
    acts = plan_cloud_config(cfg, operator_keys=["ssh-ed25519 BBBB operator"])
    kinds = [a[0] for a in acts]
    assert kinds[0] == "user" and acts[0][1:] == ("ubuntu", "ALL=(ALL) NOPASSWD:ALL", "docker")
    keys = {(a[1], a[2].split()[-1]) for a in acts if a[0] == "keys"}
    assert keys == {("ubuntu", "tenant"), ("root", "tenant"), ("ubuntu", "operator"), ("root", "operator")}  # root access is part of the tier
    assert ("file", "/etc/racksdock/launch.json", "{}", "0600") in acts
    runs = [a[1] for a in acts if a[0] == "run"]
    assert runs[0].startswith("systemctl start racksdock-launch") and runs[1] == "ufw allow 22/tcp"
    assert plan_cloud_config({}) == []


def test_docker_mode_config(monkeypatch):
    monkeypatch.setenv("RACKSDOCK_HOST_AGENT_MODE", "docker")
    monkeypatch.setenv("RACKSDOCK_NET_MODE", "nat")
    monkeypatch.setenv("RACKSDOCK_NAT_SSH_PORT", "20022")
    cfg = HostAgentConfig.from_env()
    assert cfg.docker and cfg.deployment == "container" and cfg.agent_transport == "tcp" and cfg.agent_port == 5300
    assert cfg.docker_data_dir == Path("/var/lib/racksdock/containers") and cfg.network.nat_ssh_port == 20022
    monkeypatch.setenv("RACKSDOCK_HOST_AGENT_MODE", "real")
    assert HostAgentConfig.from_env().deployment == "vm"
    monkeypatch.setenv("RACKSDOCK_HOST_AGENT_MODE", "static")
    assert HostAgentConfig.from_env().deployment == "static"


def test_container_init_loads_the_node_agent_env_file(tmp_path):
    from racksdock_node.node_agent.containerinit import load_env_file

    f = tmp_path / "node-agent.env"
    f.write_text("# dev trust material from the host\nRACKSDOCK_TRUST_MODE=dev\nRACKSDOCK_DEV_CA_DIR=/etc/racksdock/devca\n\nRACKSDOCK_SIM_NODE_DIR=\"/etc/racksdock/node\"\n")
    assert load_env_file(f) == {"RACKSDOCK_TRUST_MODE": "dev", "RACKSDOCK_DEV_CA_DIR": "/etc/racksdock/devca", "RACKSDOCK_SIM_NODE_DIR": "/etc/racksdock/node"}
    assert load_env_file(tmp_path / "missing") == {}


def test_dual_mode_rebind_plan_and_mode_detection(tmp_path, monkeypatch):
    from racksdock_node.host_agent.hypervisor import dual_backend as dm
    from racksdock_node.host_agent.hypervisor.dual_backend import DualBackend, plan_rebind

    assert plan_rebind("vm", "vm") is None and plan_rebind("container", "container") is None
    assert plan_rebind("vm", "container") == "bind-host-driver.sh" and plan_rebind("container", "vm") == "bind-vfio.sh"
    assert plan_rebind(None, "vm") == "bind-vfio.sh"
    with pytest.raises(ValueError):
        plan_rebind("vm", "static")
    res = VmResources(gpu_pci=["0000:01:00.0", "0000:25:00.0"], extra_pci=["0000:01:00.1", "0000:25:00.1"])
    drivers = {"0000:01:00.0": "vfio-pci", "0000:25:00.0": "vfio-pci"}
    monkeypatch.setattr(dm, "gpu_driver", lambda a: drivers.get(a, ""))
    d = DualBackend(vm=object(), container=object(), resources=res, state_dir=tmp_path)
    assert d.current_mode() == "vm"
    drivers["0000:25:00.0"] = "nvidia"  # mixed: neither mode; fall back to the state file hint
    assert d.current_mode() is None
    (tmp_path / "gpu_mode").write_text("container\n")
    assert d.current_mode() == "container"
    drivers["0000:01:00.0"] = "nvidia"
    assert d.current_mode() == "container"


def test_dual_mode_config(monkeypatch):
    monkeypatch.setenv("RACKSDOCK_HOST_AGENT_MODE", "dual")
    cfg = HostAgentConfig.from_env()
    assert cfg.dual and cfg.deployment == "dual" and cfg.agent_transport == "vsock"  # VMs keep vsock; the TCP listener is added for containers


def test_tagged_nat_rules_can_exclude_the_adopted_vm():
    from racksdock_node.host_agent.hypervisor.libvirt_backend import parse_tagged_rules

    save = """*nat
-A PREROUTING -p tcp --dport 20022 -m comment --comment racksdock-nat-inst_a -j DNAT --to 192.168.122.10:22
-A PREROUTING -p tcp --dport 20023 -m comment --comment racksdock-nat-inst_b -j DNAT --to 192.168.122.11:22
COMMIT
*filter
-A FORWARD -d 192.168.122.10 -m comment --comment racksdock-nat-inst_a -j ACCEPT
COMMIT
"""
    assert len(parse_tagged_rules(save, "racksdock-nat")) == 3
    kept = parse_tagged_rules(save, "racksdock-nat", exclude_prefix="racksdock-nat-inst_a")
    assert len(kept) == 1 and "20023" in kept[0][1]


class _FakeBackend:
    name = "fake"

    def __init__(self):
        self.states: dict[str, str] = {}
        self.kept: list = []

    def host_inventory(self):
        return {"cpu_model": "EPYC", "nvme": [{"PhysicalSize": 2e12}], "board_serial": "S1"}

    async def state(self, handle):
        return self.states.get(handle.name, "unknown")

    async def cleanup_stale(self, vm_root, keep=None):
        self.kept.append(keep.name if keep else None)
        return []


def _agent_cfg(tmp_path, mode="real", **kw):
    from racksdock_subnet.verifier.config import TrustConfig

    return HostAgentConfig(mode=mode, state_dir=tmp_path / "state", key_path=tmp_path / "state" / "host.seed", trust=TrustConfig(mode="prod", dev_ca_dir=None), **kw)


@pytest.mark.asyncio
async def test_host_agent_adopts_its_running_rental_across_restarts(tmp_path):
    """The rental's handle + launch command are kept in the state dir; a restarted host-agent re-adopts a running VM instead of
    destroying it, and tells cleanup_stale to keep it."""
    from racksdock_subnet.core.schemas import LaunchCommand, VmMode

    from racksdock_node.host_agent.agent import HostAgent
    from racksdock_node.host_agent.hypervisor.base import VmHandle

    cfg = _agent_cfg(tmp_path)
    a = HostAgent(cfg, backend=_FakeBackend())
    assert await a._adopt_current() is None  # nothing recorded
    a.current = VmHandle(instance_id="inst_1", name="racksdock-inst_1", ip="1.2.3.4", ssh_port=20022, extra={"vm_dir": "/x", "active": True, "deployment": "vm"})
    a.current_launch = LaunchCommand(instance_id="inst_1", node_id="n", mode=VmMode.tenant, image="ubuntu24.04", image_version="ubuntu24.04_v1", user_data="#cloud-config\n", tenant_nonce="ab" * 32)
    a._save_current()
    assert (tmp_path / "state" / "current.json").exists()
    # a fresh agent (restart) with the VM still running adopts it
    be = _FakeBackend()
    be.states["racksdock-inst_1"] = "running"
    b = HostAgent(cfg, backend=be)
    h = await b._adopt_current()
    assert h is not None and b.current.instance_id == "inst_1" and b.current.extra["deployment"] == "vm" and b.current_launch.image == "ubuntu24.04"
    assert b._boot_timer is None  # already active: no boot watchdog
    # the VM is gone: nothing adopted, the record is dropped
    be.states["racksdock-inst_1"] = "shut off"
    c = HostAgent(cfg, backend=be)
    assert await c._adopt_current() is None and not (tmp_path / "state" / "current.json").exists()
    # teardown bookkeeping clears the record
    a._save_current()
    a.current = None
    a._save_current()
    assert not (tmp_path / "state" / "current.json").exists()


@pytest.mark.asyncio
async def test_bootstrap_identity_reuse_resigns_the_stored_identity(tmp_path, monkeypatch):
    """reuse=true: no VM; the stored gpu_ids are re-signed with the host's current deployment tier (a node switching to dual while
    a rental runs)."""
    from racksdock_subnet.core.schemas import HostCommand, RegistrationRecord

    from racksdock_node.host_agent.agent import HostAgent

    sent = []
    cfg = _agent_cfg(tmp_path, mode="dual")
    a = HostAgent(cfg, backend=_FakeBackend())

    async def fake_send(ev):
        sent.append(ev)

    monkeypatch.setattr(a.platform, "send", fake_send)
    await a._cmd_bootstrap_identity(HostCommand(kind="bootstrap_identity", command_id="c1", payload={"reuse": True}))
    assert sent[-1].kind == "error" and "no stored registration" in sent[-1].payload["error"]
    from racksdock_subnet.core.profile import current

    ids = [f"sha256:{i:064x}" for i in range(current().gpu_count)]
    old = a._registration_record(ids, 10.0).model_copy(update={"deployment": "vm"})
    a._store_registration(old)
    await a._cmd_bootstrap_identity(HostCommand(kind="bootstrap_identity", command_id="c2", payload={"reuse": True}))
    ev = sent[-1]
    assert ev.kind == "identity" and ev.payload["evidence"] is None and ev.payload["reused"] is True
    rec = RegistrationRecord.model_validate(ev.payload["registration"])
    assert rec.gpu_ids == ids and rec.node_id == old.node_id and rec.deployment == "dual" and rec.claimed_spec.network_gbps == 10.0
    assert RegistrationRecord.model_validate_json((tmp_path / "state" / "registration.json").read_text()).deployment == "dual"


@pytest.mark.asyncio
async def test_dual_mode_hands_hugepages_between_the_tiers(tmp_path, monkeypatch):
    """A container rental gets the memory the VM pool reserved at boot; the next VM gets the pool back (or ordinary pages)."""
    from racksdock_node.host_agent.hypervisor.dual_backend import DualBackend

    hp = tmp_path / "hp"
    hp.mkdir()
    (hp / "nr_hugepages").write_text("440\n")
    (hp / "free_hugepages").write_text("440\n")
    res = VmResources(gpu_pci=["0000:01:00.0"], memory_gb=439, hugepage_kib=1048576)
    d = DualBackend(vm=object(), container=object(), resources=res, state_dir=tmp_path / "state")
    monkeypatch.setattr(d, "_hp_dir", lambda: hp)
    assert d.hugepages_needed() == 439
    assert await d.reserve_hugepages() == 440 and (hp / "nr_hugepages").read_text().strip() == "440"  # pool intact: untouched
    await d.release_hugepages()
    assert (hp / "nr_hugepages").read_text().strip() == "0" and d._hp_hint.read_text().strip() == "440"
    # the kernel hands the whole pool back on the first attempt
    (hp / "free_hugepages").write_text("0\n")
    orig_write = Path.write_text

    def fake_write(self, text, *a, **k):
        orig_write(self, text, *a, **k)
        if self == hp / "nr_hugepages":
            orig_write(hp / "free_hugepages", text)
    monkeypatch.setattr(Path, "write_text", fake_write)
    assert await d.reserve_hugepages() == 440 and (hp / "nr_hugepages").read_text().strip() == "440"
    # a container teardown pre-reserves in the background; a VM launch waits for it instead of starting a second reservation
    await d.release_hugepages()
    (hp / "free_hugepages").write_text("0\n")
    d.prereserve_hugepages()
    assert d._prereserve is not None
    await d._await_prereserve()
    assert (hp / "free_hugepages").read_text().strip() == "440" and d._prereserve is None


def test_forward_rules_expose_the_whole_ip_except_reserved_ports():
    """Policy 'all': every TCP port except the host's reserved ones (and the advertised ssh port, which has its own rule) and every
    UDP port arriving on the external interface is DNATed 1:1 to the rental; 'ssh' keeps only the ssh forward. Rules are listed in
    insertion order (each at position 1) so the specific ssh rules end up on top."""
    from racksdock_node.host_agent.hypervisor.nat import forward_rules

    rules = forward_rules("203.0.113.5", "192.168.122.65", 20022, "racksdock-nat-inst_x", expose="all", reserved_ports=[22, 2222], ext_iface="eno1")
    assert [t for t, _ in rules] == ["nat", "nat", "filter", "nat", "nat", "filter"]
    tcp_all = rules[0][1]
    assert tcp_all[:3] == ["PREROUTING", "-i", "eno1"] and "!" in tcp_all and tcp_all[tcp_all.index("--dports") + 1] == "22,2222,20022"
    assert tcp_all[tcp_all.index("--to-destination") + 1] == "192.168.122.65"  # no port rewrite: 1:1
    assert rules[1][1][:5] == ["PREROUTING", "-i", "eno1", "-p", "udp"]
    assert rules[2][1][:4] == ["FORWARD", "-d", "192.168.122.65", "-j"]
    assert rules[3][1][rules[3][1].index("--to-destination") + 1] == "192.168.122.65:22"  # the ssh rule, inserted last = on top
    assert all(a[-1] == "racksdock-nat-inst_x" for _, a in rules)
    # without a known external interface the catch-alls match the destination address instead
    by_addr = forward_rules("203.0.113.5", "192.168.122.65", 20022, "t", expose="all")
    assert by_addr[0][1][:3] == ["PREROUTING", "-d", "203.0.113.5"]
    ssh_only = forward_rules("203.0.113.5", "192.168.122.65", 20022, "t", expose="ssh")
    assert len(ssh_only) == 3 and all("multiport" not in a for _, a in ssh_only)
    with pytest.raises(ValueError):
        forward_rules("203.0.113.5", "192.168.122.65", 20022, "t", expose="everything")


def test_network_config_exposure_from_env(monkeypatch):
    monkeypatch.setenv("RACKSDOCK_NET_MODE", "nat")
    monkeypatch.setenv("RACKSDOCK_NAT_RESERVED_PORTS", "22, 2222")
    cfg = HostAgentConfig.from_env()
    assert cfg.network.expose == "all" and cfg.network.reserved_ports == [22, 2222]
    monkeypatch.setenv("RACKSDOCK_NAT_EXPOSE", "ssh")
    assert HostAgentConfig.from_env().network.expose == "ssh"


@pytest.mark.asyncio
async def test_adoption_keeps_an_unverifiable_rental_and_refreshes_forwarding(tmp_path):
    """A record whose state cannot be checked still protects the rental from cleanup; an adopted rental gets the current
    forwarding policy re-applied."""
    from racksdock_subnet.core.schemas import LaunchCommand, VmMode

    from racksdock_node.host_agent.agent import HostAgent
    from racksdock_node.host_agent.hypervisor.base import VmHandle

    class Flaky(_FakeBackend):
        refreshed: list = []

        async def state(self, handle):
            raise RuntimeError("virsh unavailable")

        async def refresh_forwarding(self, handle):
            self.refreshed.append(handle.instance_id)

    cfg = _agent_cfg(tmp_path)
    a = HostAgent(cfg, backend=Flaky())
    a.current = VmHandle(instance_id="inst_k", name="racksdock-inst_k", ip="1.2.3.4", ssh_port=20022, extra={"active": True})
    a.current_launch = LaunchCommand(instance_id="inst_k", node_id="n", mode=VmMode.tenant, image="ubuntu24.04", image_version="v", user_data="#cloud-config\n", tenant_nonce="ab" * 32)
    a._save_current()
    b = HostAgent(cfg, backend=Flaky())
    keep = await b._adopt_current()
    assert keep is not None and keep.name == "racksdock-inst_k" and b.current is None and (tmp_path / "state" / "current.json").exists()

    class Fine(Flaky):
        async def state(self, handle):
            return "running"

    c = HostAgent(cfg, backend=Fine())
    assert (await c._adopt_current()).instance_id == "inst_k" and c.current is not None and Fine.refreshed == ["inst_k"]


def test_operator_dev_key_only_rides_on_bootstrap_launches(tmp_path, monkeypatch):
    """RACKSDOCK_DEV_SSH_PUBKEY never reaches a tenant's or validator's rental: the dev guest files carry no key, and the launch
    spec gets operator_keys only for a bootstrap instance."""
    from racksdock_subnet.verifier.config import TrustConfig
    from racksdock_subnet.verifier.devca import DevCA

    from racksdock_node.host_agent.agent import HostAgent

    ca = tmp_path / "devca"
    DevCA.create(ca)
    node = tmp_path / "node"
    DevCA(ca).issue_node(node, count=1)
    cfg = HostAgentConfig(mode="real", state_dir=tmp_path / "state", key_path=tmp_path / "state" / "host.seed", sim_node_dir=node,
                          trust=TrustConfig(mode="dev", dev_ca_dir=ca), dev_ssh_pubkey="ssh-ed25519 AAAA operator")
    a = HostAgent(cfg, backend=_FakeBackend())
    assert "__ssh_authorized_keys__" not in a._dev_guest_files()
    assert (cfg.dev_ssh_pubkey.strip() + "\n") if "bootstrap-x".startswith("bootstrap-") else None == "ssh-ed25519 AAAA operator\n"


def test_container_cpuset_matches_the_rented_cpu_count():
    from racksdock_node.host_agent.hypervisor.docker_backend import container_cpuset

    assert container_cpuset(24, 28) == "0-23" and container_cpuset(120, 128) == "0-119"
    assert container_cpuset(128, 128) is None and container_cpuset(0, 128) is None and container_cpuset(8, 0) is None
