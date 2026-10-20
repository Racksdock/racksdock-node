"""Outbound WebSocket to the marketplace. Events are signed; commands are parsed into HostCommand."""
from __future__ import annotations

import asyncio
import collections
import json
import logging
from collections.abc import Awaitable, Callable

import websockets
from racksdock_subnet.core.envelope import SignedEnvelope
from racksdock_subnet.core.keys import SigningKey
from racksdock_subnet.core.schemas import HostCommand, HostEvent

log = logging.getLogger("racksdock.host-agent.platform")


class PlatformLink:
    def __init__(self, url: str, key: SigningKey, hello_factory: Callable[[], HostEvent]):
        self.url = url
        self.key = key
        self.hello_factory = hello_factory
        self.on_command: Callable[[HostCommand], Awaitable[None]] | None = None
        self._ws = None
        self._outbox: collections.deque[str] = collections.deque(maxlen=5000)
        self._stop = asyncio.Event()
        self.connected = asyncio.Event()

    def _wrap(self, ev: HostEvent) -> str:
        return SignedEnvelope.create("host_event", ev.model_dump(mode="json"), self.key).model_dump_json()

    async def send(self, ev: HostEvent) -> None:
        data = self._wrap(ev)
        if self._ws is not None:
            try:
                await self._ws.send(data)
                return
            except Exception:
                pass
        self._outbox.append(data)

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, max_size=16 * 1024 * 1024, ping_interval=20) as ws:
                    self._ws = ws
                    await ws.send(self._wrap(self.hello_factory()))
                    while self._outbox:
                        await ws.send(self._outbox.popleft())
                    self.connected.set()
                    backoff = 1.0
                    log.info("connected to platform %s", self.url)
                    async for raw in ws:
                        try:
                            cmd = HostCommand.model_validate(json.loads(raw))
                        except ValueError as e:
                            log.warning("bad command: %s", e)
                            continue
                        if self.on_command:
                            asyncio.create_task(self._safe(cmd))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("platform link down: %s; retry in %.0fs", e, backoff)
            finally:
                self._ws = None
                self.connected.clear()
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _safe(self, cmd: HostCommand) -> None:
        try:
            await self.on_command(cmd)  # type: ignore[misc]
        except Exception as e:
            log.exception("command %s failed: %s", cmd.kind, e)
            await self.send(HostEvent(kind="error", command_id=cmd.command_id, instance_id=cmd.instance_id, payload={"error": str(e)}))

    def stop(self) -> None:
        self._stop.set()
