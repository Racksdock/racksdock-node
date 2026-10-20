import asyncio
import json

import pytest
from racksdock_subnet.core.agentproto import AgentMessage, Metadata
from racksdock_subnet.core.envelope import SignedEnvelope
from racksdock_subnet.core.identity import node_id_from_gpu_ids
from racksdock_subnet.core.keys import SigningKey
from racksdock_subnet.core.nonce import compose_boot_nonce, new_nonce_hex, ssh_host_key_bytes_from_line
from racksdock_subnet.core.schemas import BootEvidence, Challenge, ChallengeResponse
from racksdock_subnet.core.userdata import user_data_hash
from racksdock_subnet.verifier.config import TrustConfig
from racksdock_subnet.verifier.devca import DevCA
from racksdock_subnet.verifier.verifiers import SimVerifier

from racksdock_node.node_agent.agent import NodeAgent
from racksdock_node.node_agent.config import NodeAgentConfig


class FakeHost:
    """Minimal host-agent side: accepts one node-agent, drives the protocol."""

    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.writer = None
        self.server = None
        self.port = None

    async def start(self):
        async def handle(reader, writer):
            self.writer = writer
            while True:
                line = await reader.readline()
                if not line:
                    break
                await self.inbox.put(AgentMessage.decode(line))

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def send(self, msg: AgentMessage):
        self.writer.write(msg.encode())
        await self.writer.drain()

    async def expect(self, typ: str, timeout=15) -> AgentMessage:
        while True:
            m = await asyncio.wait_for(self.inbox.get(), timeout)
            if m.type == typ:
                return m
            if m.type == "log":
                continue
            raise AssertionError(f"expected {typ}, got {m.type}: {m.payload}")


@pytest.fixture
def ca(tmp_path):
    return DevCA.create(tmp_path / "ca")


@pytest.fixture
def cfg(tmp_path, ca):
    node_dir = ca.issue_node(tmp_path / "node")
    return NodeAgentConfig(
        mode="sim", transport="tcp", state_dir=tmp_path / "state", sim_node_dir=node_dir, image_version="ubuntu24.04_v2026.10.1",
        metrics_port=0, trust=TrustConfig(mode="dev", dev_ca_dir=ca.dir),
    )


@pytest.mark.asyncio
async def test_full_boot_challenge_teardown_flow(cfg, ca, tmp_path):
    host = FakeHost()
    await host.start()
    cfg.port = host.port
    cfg.metrics_port = 0  # disabled port 0 -> OS picks; fine for test
    agent = NodeAgent(cfg)
    task = asyncio.create_task(agent.run())

    hello = await host.expect("hello")
    assert hello.payload["node_agent_pubkey"] == agent.key.public.hex
    tenant_nonce = new_nonce_hex()
    user_data = "#cloud-config\nusers: []\n"
    await host.send(AgentMessage(type="metadata", payload=Metadata(
        instance_id="inst_1", tenant_nonce=tenant_nonce, mode="tenant", image_version=cfg.image_version, user_data=user_data).model_dump()))

    ev_msg = await host.expect("evidence")
    env = SignedEnvelope.model_validate(ev_msg.payload)
    assert env.verify(expected_signer=agent.key.public.hex)
    ev = BootEvidence.model_validate(env.payload)
    assert ev.user_data_hash == user_data_hash(user_data)
    expected = compose_boot_nonce(tenant_nonce, ssh_host_key_bytes_from_line(ev.ssh_host_key_line), user_data.encode())
    assert ev.boot_nonce == expected
    res = SimVerifier(ca).verify(ev.bundle, expected)
    assert res.ok, res.errors
    node_id = node_id_from_gpu_ids(res.gpu_ids)

    st = await host.expect("status")
    assert st.payload["status"] == "pending"
    st2 = await host.expect("status")
    assert st2.payload["status"] == "active" and st2.payload["launch_status"] == "started"

    # validator challenge relayed by the host
    vkey = SigningKey.generate()
    ch_nonce = new_nonce_hex()
    ch = Challenge(instance_id="inst_1", nonce=ch_nonce, fields=["attestation", "health", "microbench"], validator_key=vkey.public.hex)
    await host.send(AgentMessage(type="challenge", payload=SignedEnvelope.create("challenge", ch.model_dump(), vkey).model_dump()))
    resp_msg = await host.expect("challenge_response", timeout=30)
    renv = SignedEnvelope.model_validate(resp_msg.payload)
    assert renv.verify(expected_signer=agent.key.public.hex)
    resp = ChallengeResponse.model_validate(renv.payload)
    r2 = SimVerifier(ca).verify(resp.bundle, ch_nonce)
    assert r2.ok and node_id_from_gpu_ids(r2.gpu_ids) == node_id
    assert resp.health is not None and len(resp.health.gpus) == 8 and resp.health.microbench is not None
    forbidden = {"util", "memory_used", "power_w", "temp", "processes", "rx_bytes"}
    assert not (set(json.dumps(resp.health.model_dump()).lower().split('"')) & forbidden)

    # unsigned challenge is rejected
    bad = SignedEnvelope.create("challenge", ch.model_dump(), vkey)
    bad.signature = "00" * 64
    await host.send(AgentMessage(type="challenge", payload=bad.model_dump()))
    m = await asyncio.wait_for(host.inbox.get(), 5)
    assert m.type == "log" and "rejected" in m.payload["msg"]

    await host.send(AgentMessage(type="teardown", payload={}))
    ack = await host.expect("teardown_ack")
    assert ack.payload["instance_id"] == "inst_1"
    assert agent.disk.key is None
    await asyncio.wait_for(task, 10)
    host.server.close()


@pytest.mark.asyncio
async def test_real_mode_teardown_never_powers_off(tmp_path, ca, monkeypatch):
    """Regression: a node-agent once ran `systemctl poweroff` on teardown and shut down a lab host. The agent must not contain
    any power-off/reboot command, and real-mode teardown must not spawn system commands beyond the disk erase it owns."""
    import subprocess as sp
    from pathlib import Path as _P

    import racksdock_node.node_agent as pkg

    src = "".join(p.read_text() for p in _P(pkg.__file__).parent.glob("*.py"))
    for forbidden in ("poweroff", "reboot", "shutdown -", "halt"):
        assert forbidden not in src.replace("shuts the VM down", ""), forbidden
    from racksdock_node.node_agent import agent as agent_mod

    calls = []
    monkeypatch.setattr(sp, "run", lambda cmd, **kw: calls.append(list(cmd)) or sp.CompletedProcess(cmd, 0, "", ""))
    node_dir = ca.issue_node(tmp_path / "node")
    cfg = NodeAgentConfig(mode="real", transport="tcp", state_dir=tmp_path / "state", sim_node_dir=node_dir, trust=TrustConfig(mode="dev", dev_ca_dir=ca.dir),
                          ssh_host_key_pub=tmp_path / "missing.pub")
    monkeypatch.setattr(agent_mod, "ensure_host_key_line", lambda c: SigningKey.generate().public.ssh_public_line("h"))
    ag = NodeAgent(cfg)

    class Conn:
        async def send(self, msg):
            pass

    await ag._teardown(Conn())
    assert calls == [], calls  # nothing opened -> nothing erased -> no system commands at all
    assert ag.disk.opened is False
