"""User-only metrics endpoint on 127.0.0.1 (reach via SSH tunnel). Never relayed to validators or the platform."""
from __future__ import annotations

import asyncio
import json
import time


def _collect() -> dict:
    data: dict = {"ts": time.time(), "gpus": []}
    try:
        import pynvml  # type: ignore

        pynvml.nvmlInit()
        try:
            for i in range(pynvml.nvmlDeviceGetCount()):
                h = pynvml.nvmlDeviceGetHandleByIndex(i)
                util = pynvml.nvmlDeviceGetUtilizationRates(h)
                mem = pynvml.nvmlDeviceGetMemoryInfo(h)
                data["gpus"].append({
                    "index": i, "util_gpu": util.gpu, "util_mem": util.memory, "mem_used_mib": mem.used // 2**20,
                    "mem_total_mib": mem.total // 2**20, "power_w": pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0,
                    "temp_c": pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU),
                })
        finally:
            pynvml.nvmlShutdown()
    except Exception:
        data["note"] = "pynvml unavailable; simulated"
        data["gpus"] = [{"index": i, "util_gpu": 0, "mem_used_mib": 0} for i in range(8)]
    try:
        la = open("/proc/loadavg").read().split()[:3]
        data["loadavg"] = [float(x) for x in la]
    except OSError:
        pass
    return data


async def serve_metrics(port: int, host: str = "127.0.0.1") -> asyncio.AbstractServer:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        except Exception:
            writer.close()
            return
        body = json.dumps(_collect()).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, host, port)
