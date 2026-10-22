"""node-agent main loop."""
from __future__ import annotations

import asyncio
import logging
import time

from racksdock_subnet.core.agentproto import AgentMessage, Metadata
from racksdock_subnet.core.envelope import SignedEnvelope
from racksdock_subnet.core.keys import SigningKey
from racksdock_subnet.core.nonce import compose_boot_nonce, ssh_host_key_bytes_from_line
from racksdock_subnet.core.schemas import BootEvidence, Challenge, ChallengeResponse, InstanceStatus, StatusUpdate, VmMode
from racksdock_subnet.core.userdata import user_data_hash
from racksdock_subnet.verifier.collectors import NvattestCliCollector, SimulatedCollector
from racksdock_subnet.verifier.devca import DevCA

from . import __version__
from .config import NodeAgentConfig
from .health import build_health
from .hostkey import ensure_host_key_line
from .metrics import serve_metrics
from .system import DataDisk, launch_status, read_user_data, wait_cloud_init
from .transport import AgentConnection, connect

log = logging.getLogger("racksdock.node-agent")


class NodeAgent:
    def __init__(self, cfg: NodeAgentConfig):
        self.cfg = cfg
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.key = SigningKey.generate()  # fresh per boot; its public key is in the boot evidence
        self.collector = self._make_collector()
        self.disk = DataDisk(cfg)
        self.meta: Metadata | None = None
        self.gpu_ids: list[str] = []
        self.measurements_hash: str | None = None
        self.host_key_line = ensure_host_key_line(cfg)
        self._stop = asyncio.Event()
        self._metrics_server = None
        self._last_status: dict | None = None

    def _make_collector(self):
        # dev trust mode uses the dev CA even on real hardware (e.g. Ampere GPUs, which have no device attestation)
        if self.cfg.sim or self.cfg.trust.dev:
            if self.cfg.trust.dev_ca_dir is None or self.cfg.sim_node_dir is None:
                raise RuntimeError("dev trust mode needs RACKSDOCK_DEV_CA_DIR and RACKSDOCK_SIM_NODE_DIR")
            return SimulatedCollector(DevCA(self.cfg.trust.dev_ca_dir), self.cfg.sim_node_dir)
        return NvattestCliCollector(self.cfg.trust.nvattest_bin)

    # ---- boot sequence -----------------------------------------------------
    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                conn = await connect(self.cfg.transport, host_cid=self.cfg.host_cid, port=self.cfg.port, tcp_host=self.cfg.tcp_host)
            except OSError as e:
                log.warning("host connection failed: %s; retrying in %.0fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            backoff = 1.0
            try:
                await self._session(conn)
            except (ConnectionError, asyncio.IncompleteReadError) as e:
                log.warning("session ended: %s", e)
            finally:
                await conn.close()
            if self._stop.is_set():
                break
            await asyncio.sleep(1.0)
        if self._metrics_server:
            self._metrics_server.close()

    async def _hello(self, conn: AgentConnection) -> None:
        await conn.send(AgentMessage(type="hello", payload={
            "node_agent_pubkey": self.key.public.hex, "node_agent_version": __version__,
            "image_version": self.cfg.image_version, "ssh_host_key_line": self.host_key_line,
            "booted": self.meta is not None,
        }))

    async def _session(self, conn: AgentConnection) -> None:
        await self._hello(conn)
        while True:
            try:
                # a real VM can boot before the host-agent has registered it; repeat hello until metadata arrives
                msg = await asyncio.wait_for(conn.recv(), timeout=10 if self.meta is None else None)
            except TimeoutError:
                await self._hello(conn)
                continue
            if msg is None:
                raise ConnectionError("host closed")
            if msg.type == "metadata":
                meta = Metadata.model_validate(msg.payload)
                if self.meta is not None and self.meta.instance_id == meta.instance_id and self._last_status is not None:
                    await conn.send(AgentMessage(type="status", payload=self._last_status))  # already booted; host-agent reconnected
                    continue
                self.meta = meta
                await self._boot(conn)
            elif msg.type == "challenge":
                await self._challenge(conn, msg)
            elif msg.type == "teardown":
                await self._teardown(conn)
                return
            elif msg.type == "ping":
                await conn.send(AgentMessage(type="log", payload={"level": "debug", "msg": "pong"}))

    async def _boot(self, conn: AgentConnection) -> None:
        m = self.meta
        assert m is not None
        user_data = read_user_data(self.cfg, m.user_data)
        ud_hash = user_data_hash(user_data)
        boot_nonce = compose_boot_nonce(m.tenant_nonce, ssh_host_key_bytes_from_line(self.host_key_line), user_data)
        t0 = time.perf_counter()
        bundle = await asyncio.get_running_loop().run_in_executor(None, self.collector.collect, boot_nonce)
        log.info("collected evidence for %d devices in %.1fs", len(bundle.devices), time.perf_counter() - t0)
        # local identity extraction (the platform/validator verifies; we only need ids for health hashes)
        try:
            from cryptography.hazmat.primitives import serialization
            from racksdock_subnet.core.identity import gpu_id_from_cert_pem
            from racksdock_subnet.verifier.verifiers import split_chain

            self.gpu_ids = [gpu_id_from_cert_pem(split_chain(d.certificate)[0].public_bytes(serialization.Encoding.PEM)) for d in bundle.devices]
        except Exception as e:
            log.warning("could not derive gpu ids locally: %s", e)
        ev = BootEvidence(
            instance_id=m.instance_id, mode=VmMode(m.mode), ssh_host_key_line=self.host_key_line,
            node_agent_pubkey=self.key.public.hex, user_data_hash=ud_hash, tenant_nonce=m.tenant_nonce,
            boot_nonce=boot_nonce, bundle=bundle, image_version=self.cfg.image_version, node_agent_version=__version__,
        )
        env = SignedEnvelope.create("boot_evidence", ev.model_dump(mode="json"), self.key)
        await conn.send(AgentMessage(type="evidence", payload=env.model_dump()))
        try:
            disk_ok = self.disk.open()
        except Exception as e:
            log.error("data disk setup failed: %s", e)
            disk_ok = False
        await conn.send(AgentMessage(type="status", payload=StatusUpdate(
            status=InstanceStatus.pending, status_details="attested; applying cloud-init" + ("" if disk_ok else "; data disk unavailable"),
            ssh_port=m.ssh_port, ssh_host_key_line=self.host_key_line).model_dump(mode="json")))
        ok = await wait_cloud_init(self.cfg)
        if self.cfg.sim or (user_data and b"metrics.enabled" in user_data):
            try:
                self._metrics_server = await serve_metrics(self.cfg.metrics_port)
            except OSError as e:
                log.warning("metrics server not started: %s", e)
        ls = launch_status(self.cfg)
        self._last_status = StatusUpdate(status=InstanceStatus.active, status_details=None if ok else "cloud-init reported errors",
                                         ssh_port=m.ssh_port, ssh_host_key_line=self.host_key_line, launch_status=ls).model_dump(mode="json")
        await conn.send(AgentMessage(type="status", payload=self._last_status))

    # ---- validator challenges ----------------------------------------------
    async def _challenge(self, conn: AgentConnection, msg: AgentMessage) -> None:
        env = SignedEnvelope.model_validate(msg.payload)
        if not env.verify() or env.kind != "challenge":
            await conn.send(AgentMessage(type="log", payload={"level": "warning", "msg": "rejected unsigned challenge"}))
            return
        ch = Challenge.model_validate(env.payload)
        if self.meta is None or ch.instance_id != self.meta.instance_id:
            resp = ChallengeResponse(instance_id=ch.instance_id, nonce=ch.nonce, node_agent_pubkey=self.key.public.hex, error="unknown instance")
        else:
            bundle = None
            if "attestation" in ch.fields:
                bundle = await asyncio.get_running_loop().run_in_executor(None, self.collector.collect, ch.nonce)
            health = None
            if "health" in ch.fields or "microbench" in ch.fields:
                health = await asyncio.get_running_loop().run_in_executor(
                    None, build_health, self.cfg, self.gpu_ids, self.measurements_hash, VmMode(self.meta.mode),
                    launch_status(self.cfg), __version__, "microbench" in ch.fields)
            resp = ChallengeResponse(instance_id=ch.instance_id, nonce=ch.nonce, node_agent_pubkey=self.key.public.hex, bundle=bundle, health=health)
        out = SignedEnvelope.create("challenge_response", resp.model_dump(mode="json"), self.key)
        await conn.send(AgentMessage(type="challenge_response", payload=out.model_dump()))

    # ---- teardown ----------------------------------------------------------
    async def _teardown(self, conn: AgentConnection) -> None:
        self.disk.erase()
        await conn.send(AgentMessage(type="teardown_ack", payload={"instance_id": self.meta.instance_id if self.meta else None}))
        self._stop.set()
        # The agent never stops the machine it runs on: after this ack the host-agent ends the VM itself.
        log.info("teardown complete; disk key erased; exiting")
