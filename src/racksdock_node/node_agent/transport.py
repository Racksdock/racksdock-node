"""Line-delimited JSON over vsock or TCP."""
from __future__ import annotations

import asyncio
import socket

from racksdock_subnet.core.agentproto import AgentMessage


class AgentConnection:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer

    async def send(self, msg: AgentMessage) -> None:
        self.writer.write(msg.encode())
        await self.writer.drain()

    async def recv(self) -> AgentMessage | None:
        line = await self.reader.readline()
        if not line:
            return None
        return AgentMessage.decode(line)

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass


async def connect(transport: str, *, host_cid: int = 2, port: int = 5300, tcp_host: str = "127.0.0.1") -> AgentConnection:
    if transport == "vsock":
        if not hasattr(socket, "AF_VSOCK"):
            raise RuntimeError("AF_VSOCK unsupported on this kernel/python")
        s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)  # type: ignore[attr-defined]
        s.setblocking(False)
        loop = asyncio.get_running_loop()
        await loop.sock_connect(s, (host_cid, port))
        reader, writer = await asyncio.open_connection(sock=s)
    else:
        reader, writer = await asyncio.open_connection(tcp_host, port)
    return AgentConnection(reader, writer)
