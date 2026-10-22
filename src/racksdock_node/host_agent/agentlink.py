"""Accepts node-agent connections (vsock or TCP) and routes messages for the current instance."""
from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable

from racksdock_subnet.core.agentproto import AgentMessage

log = logging.getLogger("racksdock.host-agent.agentlink")


class AgentLink:
    def __init__(self, transport: str, port: int, bind_host: str = "127.0.0.1"):
        self.transport = transport
        self.port = port
        self.bind_host = bind_host
        self.server: asyncio.AbstractServer | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.on_message: Callable[[AgentMessage], Awaitable[None]] | None = None
        self.connected = asyncio.Event()

    async def start(self) -> None:
        if self.transport == "vsock":
            if not hasattr(socket, "AF_VSOCK"):
                raise RuntimeError("AF_VSOCK unsupported")
            s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)  # type: ignore[attr-defined]
            s.bind((socket.VMADDR_CID_ANY, self.port))  # type: ignore[attr-defined]
            s.listen(4)
            s.setblocking(False)
            self.server = await asyncio.start_server(self._handle, sock=s)
        else:
            self.server = await asyncio.start_server(self._handle, self.bind_host, self.port)
            self.port = self.server.sockets[0].getsockname()[1]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.writer is not None and not self.writer.is_closing():
            try:
                self.writer.close()
            except Exception:
                pass
        self.writer = writer
        self.connected.set()
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = AgentMessage.decode(line)
                except ValueError:
                    log.warning("bad agent message")
                    continue
                if self.on_message:
                    await self.on_message(msg)
        finally:
            if self.writer is writer:
                self.writer = None
                self.connected.clear()
            writer.close()

    async def send(self, msg: AgentMessage) -> bool:
        if self.writer is None or self.writer.is_closing():
            return False
        self.writer.write(msg.encode())
        await self.writer.drain()
        return True

    async def wait_connected(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self.connected.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def drop(self) -> None:
        if self.writer is not None:
            self.writer.close()
            self.writer = None
            self.connected.clear()

    async def stop(self) -> None:
        await self.drop()
        if self.server:
            self.server.close()
            await self.server.wait_closed()


class MultiAgentLink:
    """Dual-mode hosts: one listener per transport (vsock for VMs, TCP on the docker bridge for containers). Messages from either
    reach the same handler; sends go to whichever node-agent is connected."""

    def __init__(self, links: list[AgentLink]):
        self.links = links
        self._on_message: Callable[[AgentMessage], Awaitable[None]] | None = None

    @property
    def on_message(self):
        return self._on_message

    @on_message.setter
    def on_message(self, fn) -> None:
        self._on_message = fn
        for link in self.links:
            link.on_message = fn

    @property
    def port(self) -> int:
        return self.links[0].port

    async def start(self) -> None:
        for link in self.links:
            await link.start()

    async def send(self, msg: AgentMessage) -> bool:
        for link in self.links:
            if link.writer is not None and not link.writer.is_closing():
                return await link.send(msg)
        return False

    async def wait_connected(self, timeout: float) -> bool:
        tasks = [asyncio.create_task(link.wait_connected(timeout)) for link in self.links]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        return any(t.result() for t in done)

    async def drop(self) -> None:
        for link in self.links:
            await link.drop()

    async def stop(self) -> None:
        for link in self.links:
            await link.stop()
