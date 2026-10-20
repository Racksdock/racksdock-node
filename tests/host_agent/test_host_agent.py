import asyncio
import json
import xml.etree.ElementTree as ET

import pytest
import websockets
from racksdock_subnet.core.agentproto import Metadata
from racksdock_subnet.core.envelope import SignedEnvelope
from racksdock_subnet.core.identity import node_id_from_gpu_ids
from racksdock_subnet.core.keys import SigningKey
from racksdock_subnet.core.nonce import new_nonce_hex
from racksdock_subnet.core.schemas import (
    BootEvidence,
    Challenge,
    ChallengeResponse,
    HostCommand,
    HostEvent,
    LaunchCommand,
    RegistrationRecord,
    VmMode,
    VmResources,
)
from racksdock_subnet.verifier.config import TrustConfig
from racksdock_subnet.verifier.devca import DevCA
from racksdock_subnet.verifier.verifiers import SimVerifier

from racksdock_node.host_agent.agent import HostAgent
from racksdock_node.host_agent.cloudinit import build_seed_iso, read_seed_iso
from racksdock_node.host_agent.config import HostAgentConfig
from racksdock_node.host_agent.hypervisor.libvirt_xml import build_domain_xml, numa_plan, parse_pci


def test_domain_xml_has_all_passthrough_devices(tmp_path):
    res = VmResources(gpu_pci=[f"0000:{17 + i * 8:02x}:00.0" for i in range(8)], nvswitch_pci=["0000:05:00.0", "0000:06:00.0"],
                      nvme_pci=["0000:a0:00.0"], nic_vf_pci="0000:b0:00.1", vcpus=120, memory_gb=1850)
    xml = build_domain_xml("racksdock-x", res, tmp_path / "os.qcow2", tmp_path / "seed.iso", tmp_path / "nvram.fd",
                           host_cores=[list(range(4, 64)), list(range(68, 128))], bridge="br0")
    root = ET.fromstring(xml)
    hostdevs = root.findall("./devices/hostdev")
    assert len(hostdevs) == 8 + 2 + 1 + 1
    assert root.find("./cpu").get("mode") == "host-passthrough"
    assert root.find("./memoryBacking/hugepages/page").get("size") == "1048576"
    assert root.find("./devices/vsock") is not None
    assert len(root.findall("./cputune/vcpupin")) == 120
    assert root.find("./devices/interface") is None  # VF used instead of bridge
    assert root.find("./os/loader").text.endswith(".fd")
    assert parse_pci("0000:17:00.0") == ("0x0000", "0x17", "0x00", "0x0")
    plan = numa_plan(4, [[0, 1], [10, 11]])
    assert plan == [(0, 0), (1, 10), (2, 1), (3, 11)]


def test_seed_iso_roundtrip(tmp_path):
    meta = Metadata(instance_id="inst_1", tenant_nonce="ab" * 32, mode="tenant", image_version="ubuntu24.04_v1")
    p = build_seed_iso(tmp_path / "seed.iso", "#cloud-config\nusers: []\n", meta, "racksdock-test")
    files = read_seed_iso(p)
    assert files["user-data"] == "#cloud-config\nusers: []\n"
    assert "instance-id: inst_1" in files["meta-data"]
    assert "/etc/racksdock/metadata.json" in files["vendor-data"] and "ubuntu24.04_v1" in files["vendor-data"]


class FakePlatform:
    def __init__(self):
        self.events: asyncio.Queue = asyncio.Queue()
        self.ws = None
        self.server = None
        self.url = None

    async def start(self):
        async def handler(ws):
            self.ws = ws
            async for raw in ws:
                env = SignedEnvelope.model_validate_json(raw)
                assert env.verify() and env.kind == "host_event"
                await self.events.put((env.signer, HostEvent.model_validate(env.payload)))

        self.server = await websockets.serve(handler, "127.0.0.1", 0, max_size=16 * 1024 * 1024)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/v1/hosts/ws"

    async def send(self, cmd: HostCommand):
        await self.ws.send(cmd.model_dump_json())

    async def expect(self, kind: str, timeout=40, **match) -> HostEvent:
        while True:
            _, ev = await asyncio.wait_for(self.events.get(), timeout)
            if ev.kind == "log":
                continue
            if ev.kind == kind and all(ev.payload.get(k) == v for k, v in match.items()):
                return ev
            if ev.kind == "error":
                raise AssertionError(f"error event: {ev.payload}")


@pytest.fixture
def env(tmp_path):
    ca = DevCA.create(tmp_path / "ca")
    node_dir = ca.issue_node(tmp_path / "node")
    cfg = HostAgentConfig(mode="sim", state_dir=tmp_path / "state", key_path=tmp_path / "state" / "host.seed", miner_hotkey="5Fminer",
                          host_image_version="host_vtest", region="sim-1", image_dir=tmp_path / "images", vm_dir=tmp_path / "vms",
                          agent_transport="tcp", agent_port=0, heartbeat_interval_s=1.0, sim_node_dir=node_dir,
                          resources=VmResources(gpu_pci=[f"0000:{17 + i:02x}:00.0" for i in range(8)], nvme_pci=["0000:a0:00.0"], vcpus=8, memory_gb=16),
                          trust=TrustConfig(mode="dev", dev_ca_dir=ca.dir))
    return ca, cfg


@pytest.mark.asyncio
async def test_bootstrap_launch_challenge_teardown(env):
    ca, cfg = env
    platform = FakePlatform()
    await platform.start()
    cfg.platform_ws = platform.url
    agent = HostAgent(cfg)
    task = asyncio.create_task(agent.run())
    _, hello = await asyncio.wait_for(platform.events.get(), 20)
    assert hello.kind == "hello" and hello.payload["host_pubkey"] == agent.key.public.hex
    assert hello.payload["inventory"]["physical_cores"] == 128

    # identity bootstrap -> registration draft
    await platform.send(HostCommand(kind="bootstrap_identity", command_id="c1", payload={"image": "verify", "image_version": "v0"}))
    ident = await platform.expect("identity", timeout=60)
    rec = RegistrationRecord.model_validate(ident.payload["registration"])
    assert rec.miner_hotkey == "5Fminer" and rec.miner_service_key == agent.key.public.hex and len(rec.gpu_ids) == 8
    assert rec.node_id == node_id_from_gpu_ids(rec.gpu_ids)
    assert (cfg.state_dir / "registration.json").exists()
    # bootstrap VM is torn down afterwards
    for _ in range(50):
        if agent.current is None:
            break
        await asyncio.sleep(0.2)
    assert agent.current is None

    # tenant launch
    tn = new_nonce_hex()
    lc = LaunchCommand(instance_id="inst_42", node_id=rec.node_id, mode=VmMode.tenant, image="ubuntu24.04", image_version="v1",
                       user_data="#cloud-config\nusers: []\n", tenant_nonce=tn)
    await platform.send(HostCommand(kind="launch", command_id="c2", instance_id="inst_42", payload=lc.model_dump(mode="json")))
    await platform.expect("status", status="pending_provider")
    ev = await platform.expect("evidence")
    env_ = SignedEnvelope.model_validate(ev.payload)
    be = BootEvidence.model_validate(env_.payload)
    assert be.tenant_nonce == tn and be.instance_id == "inst_42"
    res = SimVerifier(ca).verify(be.bundle, be.boot_nonce)
    assert res.ok and node_id_from_gpu_ids(res.gpu_ids) == rec.node_id
    st = await platform.expect("status", status="active")
    assert st.payload["ip"] == "127.0.0.1" and st.payload["ssh_port"] > 0
    # fake ssh banner reachable
    r, w = await asyncio.open_connection("127.0.0.1", st.payload["ssh_port"])
    banner = await asyncio.wait_for(r.readline(), 5)
    assert banner.startswith(b"SSH-2.0")
    w.close()

    # challenge relay
    vkey = SigningKey.generate()
    nonce = new_nonce_hex()
    ch = Challenge(instance_id="inst_42", nonce=nonce, fields=["attestation", "health"], validator_key=vkey.public.hex)
    await platform.send(HostCommand(kind="challenge", command_id="c3", instance_id="inst_42", payload=SignedEnvelope.create("challenge", ch.model_dump(), vkey).model_dump()))
    cr = await platform.expect("challenge_response")
    assert cr.command_id == "c3"
    renv = SignedEnvelope.model_validate(cr.payload)
    assert renv.verify(expected_signer=be.node_agent_pubkey)
    resp = ChallengeResponse.model_validate(renv.payload)
    assert SimVerifier(ca).verify(resp.bundle, nonce).ok and resp.health is not None

    # heartbeat carries the instance
    hb = await platform.expect("heartbeat", timeout=10)
    assert hb.payload["instance_id"] == "inst_42" and hb.payload["vm_state"] == "running"

    # teardown
    await platform.send(HostCommand(kind="teardown", command_id="c4", instance_id="inst_42"))
    d = await platform.expect("status", status="deleted")
    assert d.instance_id == "inst_42"
    assert "0000:a0:00.0" in agent.backend.sanitized and agent.backend.reset_count >= 2  # bootstrap VM + tenant VM
    log_lines = (cfg.state_dir / "actions.log").read_text().strip().splitlines()
    kinds = [json.loads(line)["payload"]["action"] for line in log_lines]
    assert "vm_started" in kinds and "vm_destroyed" in kinds and "identity_bootstrapped" in kinds
    agent.stop()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    platform.server.close()
