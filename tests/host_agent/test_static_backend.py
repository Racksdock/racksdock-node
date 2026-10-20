"""Static backend against a real local sshd: tenant keys applied, node-agent in real mode (dev CA evidence), pinned-host-key SSH."""
import asyncio
import os
import shutil
import socket
import subprocess
from pathlib import Path

import asyncssh
import pytest
import websockets
from racksdock_subnet.core.envelope import SignedEnvelope
from racksdock_subnet.core.keys import SigningKey
from racksdock_subnet.core.nonce import new_nonce_hex
from racksdock_subnet.core.schemas import BootEvidence, HostCommand, HostEvent, LaunchCommand, UserDataInput, VmMode, VmResources
from racksdock_subnet.core.userdata import render
from racksdock_subnet.validator.suite.runner import SSHRunner
from racksdock_subnet.verifier.config import TrustConfig
from racksdock_subnet.verifier.devca import DevCA
from racksdock_subnet.verifier.verifiers import SimVerifier

from racksdock_node.host_agent.agent import HostAgent
from racksdock_node.host_agent.config import HostAgentConfig
from racksdock_node.host_agent.hypervisor.static_backend import StaticBackend

SSHD = shutil.which("sshd") or "/usr/sbin/sshd"
pytestmark = pytest.mark.skipif(not Path(SSHD).exists(), reason="sshd not installed")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class LocalSshd:
    def __init__(self, tmp: Path):
        self.dir = tmp / "sshd"
        self.dir.mkdir()
        self.port = free_port()
        self.authorized_keys = self.dir / "authorized_keys"
        self.authorized_keys.write_text("")
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.dir / "host_key")], check=True)
        self.host_key_pub = (self.dir / "host_key.pub").read_text().strip()
        (self.dir / "sshd_config").write_text(f"""Port {self.port}
ListenAddress 127.0.0.1
HostKey {self.dir}/host_key
PidFile {self.dir}/sshd.pid
AuthorizedKeysFile {self.authorized_keys}
PasswordAuthentication no
PermitRootLogin yes
StrictModes no
UsePAM no
LogLevel ERROR
""")
        self.proc = subprocess.Popen([SSHD, "-D", "-e", "-f", str(self.dir / "sshd_config")], stderr=subprocess.DEVNULL)

    def stop(self):
        self.proc.terminate()
        self.proc.wait(5)


def test_tenant_blocks_never_accumulate(tmp_path):
    ak = tmp_path / "authorized_keys"
    ak.write_text("ssh-ed25519 OPERATOR operator\n# >>> racksdock tenant keys >>>\nssh-ed25519 OLD stale-validator\n# <<< racksdock tenant keys <<<\n")
    b = StaticBackend(0, "127.0.0.1", 22, ak, ssh_service=None)
    b._apply_keys(["ssh-ed25519 NEW tenant"])
    text = ak.read_text()
    assert "OPERATOR" in text and "NEW" in text and "OLD" not in text and text.count("# >>> racksdock tenant keys >>>") == 1
    b._restore_keys()
    assert ak.read_text() == "ssh-ed25519 OPERATOR operator\n"


def test_tenant_keys_are_parsed_from_user_data():
    ud = render(UserDataInput(ssh_public_keys=["ssh-ed25519 AAAA a", "ssh-ed25519 BBBB b"]))
    assert StaticBackend.tenant_keys(ud) == ["ssh-ed25519 AAAA a", "ssh-ed25519 BBBB b"]


@pytest.mark.asyncio
async def test_static_backend_end_to_end_with_real_sshd(tmp_path):
    sshd = LocalSshd(tmp_path)
    await asyncio.sleep(0.5)
    ca = DevCA.create(tmp_path / "ca")
    node_dir = ca.issue_node(tmp_path / "node", count=8)
    trust = TrustConfig(mode="dev", dev_ca_dir=ca.dir)
    events: asyncio.Queue = asyncio.Queue()
    ws_holder = {}

    async def handler(ws):
        ws_holder["ws"] = ws
        async for raw in ws:
            env = SignedEnvelope.model_validate_json(raw)
            assert env.verify()
            await events.put(HostEvent.model_validate(env.payload))

    server = await websockets.serve(handler, "127.0.0.1", 0, max_size=16 * 1024 * 1024)
    port = server.sockets[0].getsockname()[1]
    cfg = HostAgentConfig(mode="static", platform_ws=f"ws://127.0.0.1:{port}/v1/hosts/ws", state_dir=tmp_path / "hstate", key_path=tmp_path / "hstate" / "host.seed",
                          miner_hotkey="5Fminer", region="lab", vm_dir=tmp_path / "vms", agent_transport="tcp", agent_port=0, heartbeat_interval_s=60,
                          sim_node_dir=node_dir, static_public_ip="127.0.0.1", static_ssh_port=sshd.port, static_authorized_keys=sshd.authorized_keys,
                          resources=VmResources(vcpus=2, memory_gb=4), trust=trust, static_ssh_service=None)
    # the node-agent must report the real sshd host key, not generate one
    os.environ["RACKSDOCK_SSH_HOST_KEY_PUB"] = str(sshd.dir / "host_key.pub")
    agent = HostAgent(cfg)
    task = asyncio.create_task(agent.run())

    async def expect(kind, timeout=60, **match):
        while True:
            ev = await asyncio.wait_for(events.get(), timeout)
            if ev.kind == kind and all(ev.payload.get(k) == v for k, v in match.items()):
                return ev
            if ev.kind == "error":
                raise AssertionError(ev.payload)

    try:
        await expect("hello")
        tenant = SigningKey.generate()
        key_path = tmp_path / "tenant_key"
        key_path.write_bytes(tenant.to_openssh_private())
        key_path.chmod(0o600)
        user_data = render(UserDataInput(ssh_public_keys=[tenant.public.ssh_public_line("tenant")]))
        tn = new_nonce_hex()
        lc = LaunchCommand(instance_id="inst_static", node_id="n", mode=VmMode.tenant, image="ubuntu24.04", image_version="v1", user_data=user_data, tenant_nonce=tn, ssh_port=sshd.port)
        await ws_holder["ws"].send(HostCommand(kind="launch", command_id="c1", instance_id="inst_static", payload=lc.model_dump(mode="json")).model_dump_json())
        ev = await expect("evidence")
        env = SignedEnvelope.model_validate(ev.payload)
        be = BootEvidence.model_validate(env.payload)
        assert be.ssh_host_key_line.split()[:2] == sshd.host_key_pub.split()[:2], "node-agent must present the real sshd host key"
        assert SimVerifier(ca, expected_devices=8).verify(be.bundle, be.boot_nonce).ok
        st = await expect("status", status="active")
        assert st.payload["ip"] == "127.0.0.1" and st.payload["ssh_port"] == sshd.port
        assert "tenant" in sshd.authorized_keys.read_text()
        # the tenant can log in with the key from the user-data, pinned to the attested host key
        runner = SSHRunner("127.0.0.1", sshd.port, os.environ.get("USER", "root"), str(key_path), be.ssh_host_key_line)
        res = await runner.run("echo racksdock-ok")
        await runner.close()
        assert res.rc == 0 and "racksdock-ok" in res.stdout
        # a wrong pinned host key must be refused
        bad = SSHRunner("127.0.0.1", sshd.port, os.environ.get("USER", "root"), str(key_path), SigningKey.generate().public.ssh_public_line("fake"))
        with pytest.raises((asyncssh.Error, OSError)):
            await bad.run("echo nope")
        await bad.close()
        # teardown restores the baseline authorized_keys
        await ws_holder["ws"].send(HostCommand(kind="teardown", command_id="c2", instance_id="inst_static").model_dump_json())
        await expect("status", status="deleted")
        assert "tenant" not in sshd.authorized_keys.read_text()
    finally:
        agent.stop()
        task.cancel()
        server.close()
        sshd.stop()
        os.environ.pop("RACKSDOCK_SSH_HOST_KEY_PUB", None)
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
